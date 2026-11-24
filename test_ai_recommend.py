"""Проверки рекомендаций без Telegram и запросов к поставщику ИИ."""

import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from zipfile import ZipFile

import ai_recommend as ai
from tracking import parse_ticket_event


TICKET = ("#KhromushkinTeam\n\nПользователь Иван Иванов создал новый тикет #123456 - XP\n\n"
          "ИВАН, проверьте XP у Иванова? Иванов, вот #123456 и https://t.me/Иван, "
          "athlete@example.com, @athlete и +7 (912) 345-67-89. Пульс 175.")
SETTINGS = ai.AiSettings("https://api.example.test/v1", "test-secret", "test-model")


class SettingsTests(unittest.TestCase):
    def test_default_disabled_and_explicit_enabled(self):
        with patch.dict(os.environ, clear=True):
            self.assertIsNone(ai.ai_settings())
        env = {"AI_RECOMMENDATIONS_ENABLED": "true", "AI_API_BASE_URL": SETTINGS.base_url + "/",
               "AI_API_KEY": SETTINGS.api_key, "AI_MODEL": SETTINGS.model}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(ai.ai_settings(), SETTINGS)
            self.assertNotIn(SETTINGS.api_key, repr(ai.ai_settings()))

    def test_insecure_or_credential_bearing_endpoint_rejected(self):
        for endpoint in ("http://api.example.test", "https://user:secret@api.example.test",
                         "https://api.example.test/v1?api_key=secret", "https://api.example.test:99999"):
            env = {"AI_RECOMMENDATIONS_ENABLED": "true", "AI_API_BASE_URL": endpoint,
                   "AI_API_KEY": SETTINGS.api_key, "AI_MODEL": SETTINGS.model}
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "^AI_API_BASE_URL"):
                    ai.ai_settings()


class PromptTests(unittest.TestCase):
    def test_only_sanitized_topic_and_body_in_user_message(self):
        event = parse_ticket_event(TICKET)
        topic, body = ai.sanitize_ticket(TICKET, event)
        self.assertEqual(topic, "XP")
        self.assertIn("Пульс 175.", body)
        self.assertIn("[скрыто]", body)
        response = {"choices": [{"message": {"content": "Сверить расчёт XP."}}]}
        with patch.object(ai, "_read_response", return_value=json.dumps(response).encode()) as read:
            self.assertEqual(ai._complete(SETTINGS, topic, body, ["Официальный гайд Moneyrun"]),
                             "Сверить расчёт XP.")
        request = read.call_args.args[0]
        self.assertEqual(request.full_url, SETTINGS.base_url + "/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SETTINGS.api_key)
        payload = json.loads(request.data)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        prompt_text = payload["messages"][1]["content"]
        prompt = json.loads(prompt_text)
        self.assertEqual(prompt["тема"], "XP")
        for secret in ("KhromushkinTeam", "создал новый тикет", "ИВАН", "Иванов", "123456", "https://t.me/",
                       "athlete@example.com", "@athlete", "345-67-89", SETTINGS.api_key):
            self.assertNotIn(secret, prompt_text)

    def test_ticket_is_limited_and_control_characters_are_removed(self):
        long_ticket = TICKET + "\u202e\x00" + "а" * (ai.MAX_TICKET_CHARS + 100)
        _, body = ai.sanitize_ticket(long_ticket, parse_ticket_event(long_ticket))
        self.assertEqual(len(body), ai.MAX_TICKET_CHARS)
        self.assertNotIn("\u202e", body)
        self.assertNotIn("\x00", body)

    def test_text_and_utf16_boundary(self):
        recommendation = "Сверить расчёт XP 🙂"
        suffix = ai.RECOMMENDATION_HEADER + recommendation
        available = 4096 - len(suffix.encode("utf-16-le")) // 2
        original = "A" * (available - 2) + "🏃"
        self.assertEqual(ai.append_recommendation(original, recommendation), original + suffix)
        self.assertEqual(ai.append_recommendation(original + "A", recommendation), original + "A")
        self.assertIs(ai.append_recommendation(original, "   "), original)


class SourceTests(unittest.TestCase):
    def test_xlsx_resolves_worksheet_and_shared_and_inline_cells(self):
        buffer = BytesIO()
        ns = ai._XML_NS[1:-1]
        rel = ai._REL_ID[1:].split("}")[0]
        with ZipFile(buffer, "w") as sheet:
            sheet.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{rel}">'
                           '<sheets><sheet name="Пульс" r:id="rId5"/></sheets></workbook>')
            sheet.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId5" '
                           'Target="/xl/worksheets/sheet5.xml"/></Relationships>')
            sheet.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}"><si><r><t>Сверить </t></r>'
                           '<r><t>пульс</t></r></si></sst>')
            sheet.writestr("xl/worksheets/sheet5.xml", f'<worksheet xmlns="{ns}"><sheetData><row>'
                           '<c t="inlineStr"><is><t>Почему 0 XP?</t></is></c>'
                           '<c t="s"><v>0</v></c></row></sheetData></worksheet>')
        self.assertEqual(list(ai._sheet_documents(buffer.getvalue())),
                         ["Таблица «MR Забота» · Пульс\nПочему 0 XP?\nСверить пульс"])

    def test_csv_and_html(self):
        csv = '"Почему 0 XP?","Сверить пульс\nи расчёт"\r\n'.encode()
        self.assertEqual(list(ai._sheet_documents(csv)),
                         ["Таблица «MR Забота»\nПочему 0 XP?\nСверить пульс\nи расчёт"])
        html = ("<head><title>Заголовок страницы</title></head><body><h1>Правила начисления XP</h1>"
                "<script>скрытый код</script><p>Перед переходом на D1 нужно заполнить ЧССМ. "
                "На D1 пульс обязателен &amp; учитывается при расчёте XP.</p></body>")
        document, = ai._guide_documents(html.encode())
        self.assertNotIn("скрытый код", document)
        self.assertNotIn("Заголовок страницы", document)
        self.assertIn("& учитывается", document)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "ai-knowledge.json"
        self.now = datetime(2026, 11, 22, 23, 58, tzinfo=timezone.utc).timestamp()
        self.clock = SimpleNamespace(time=lambda: self.now, monotonic=lambda: self.now)
        clock_patch = patch.object(ai, "time", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    async def test_refresh_at_03_moscow_and_use_last_good_on_partial_failure(self):
        cache = ai.KnowledgeCache(self.path)
        documents = {ai.SHEET_URL: ["Пульс и начисление XP в таблице"],
                     ai.GUIDE_URL: ["Пульс и начисление XP в гайде"]}

        def fetch(url, _):
            value = documents[url]
            if isinstance(value, OSError):
                raise value
            return value

        with patch.object(ai, "_fetch_documents", side_effect=fetch) as mocked:
            await asyncio.gather(cache.refresh(), cache.refresh())
            original = self.path.read_bytes()
            reloaded = ai.KnowledgeCache(self.path)
            self.assertEqual(reloaded.excerpts("Начисление XP", "Пульс"),
                             [documents[ai.SHEET_URL][0], documents[ai.GUIDE_URL][0]])
            self.now += 119
            await reloaded.refresh()
            self.assertEqual(mocked.call_count, 2)

            self.now += 1
            documents[ai.SHEET_URL] = ["Исправленный расчёт XP в таблице"]
            documents[ai.GUIDE_URL] = OSError("конфиденциальный текст ошибки")
            with self.assertLogs("moneyrun_relay", level="WARNING") as log:
                await reloaded.refresh()
            self.assertNotIn("конфиденциальный", "".join(log.output))
            self.assertEqual(self.path.read_bytes(), original)
            self.assertTrue(reloaded.excerpts("Пульс", ""))
            await reloaded.refresh()
            self.assertEqual(mocked.call_count, 4)

            self.now += ai.RETRY_SECONDS
            documents[ai.GUIDE_URL] = ["Исправленный расчёт XP в гайде"]
            await reloaded.refresh()
            self.assertEqual(mocked.call_count, 6)
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])
            self.assertEqual(ai.KnowledgeCache(self.path).excerpts("расчёт XP", ""),
                             [documents[ai.SHEET_URL][0], documents[ai.GUIDE_URL][0]])

    async def test_invalid_initial_cache_and_fetch_error_yield_no_context(self):
        self.path.write_text('[]', encoding="utf-8")
        with self.assertLogs("moneyrun_relay", level="WARNING"):
            cache = ai.KnowledgeCache(self.path)
        with patch.object(ai, "_fetch_documents", side_effect=OSError), \
                self.assertLogs("moneyrun_relay", level="WARNING"):
            await cache.refresh()
        self.assertEqual(cache.excerpts("XP", "Пульс"), [])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.recommender = ai.RecommendationSource(SETTINGS, directory.name)

    async def test_absent_context_skips_model(self):
        with patch.object(ai, "_complete") as complete:
            self.assertIsNone(await self.recommender.recommend(TICKET))
        complete.assert_not_called()

    async def test_full_card_skips_model(self):
        with patch.object(self.recommender.knowledge, "excerpts", return_value=["Пульс и XP"]), \
                patch.object(ai, "_complete") as complete:
            self.assertIsNone(await self.recommender.recommend(TICKET + "🙂" * 2000))
        complete.assert_not_called()

    async def test_http_error_and_malformed_response_fall_back_after_three_attempts(self):
        scenarios = (HTTPError(SETTINGS.base_url, 429, "test-secret", {}, None),
                     b'{"choices": []}', b'{"choices":[42]}', b'<html>error</html>')
        for scenario in scenarios:
            with self.subTest(scenario=type(scenario).__name__), \
                    patch.object(self.recommender.knowledge, "excerpts", return_value=["Пульс и XP"]), \
                    patch.object(ai, "_read_response", side_effect=[scenario] * 3) as read, \
                    self.assertLogs("moneyrun_relay", level="WARNING") as log:
                self.assertIsNone(await self.recommender.recommend(TICKET))
            self.assertEqual(read.call_count, 3)
            self.assertNotIn("test-secret", "".join(log.output))
            self.assertNotIn("Иванов", "".join(log.output))
            self.assertIsNone(log.records[0].exc_info)

    async def test_retries_unusable_answers_and_stops_when_an_answer_fits(self):
        answer = "Сверить расчёт XP."
        for failures in ([None], [None, "🙂" * 40]):
            remaining = 4096 - len(ai.RECOMMENDATION_HEADER.encode("utf-16-le")) // 2 - 50
            raw_text = TICKET + "a" * (remaining - len(TICKET.encode("utf-16-le")) // 2)
            with self.subTest(failures=len(failures)), \
                    patch.object(self.recommender.knowledge, "excerpts", return_value=["Пульс и XP"]), \
                    patch.object(ai, "_complete", side_effect=[*failures, answer]) as complete, \
                    self.assertLogs("moneyrun_relay", level="WARNING"):
                self.assertEqual(await self.recommender.recommend(raw_text), answer)
            self.assertEqual(complete.call_count, len(failures) + 1)

    async def test_api_timeout_applies_to_each_attempt(self):
        async def blocked(*args):
            await asyncio.Event().wait()

        worker = AsyncMock(side_effect=blocked)
        with patch.object(ai, "API_TIMEOUT", 0.01), patch.object(ai.asyncio, "to_thread", worker), \
                patch.object(self.recommender.knowledge, "excerpts", return_value=["Пульс и XP"]), \
                self.assertLogs("moneyrun_relay", level="WARNING"):
            self.assertIsNone(await self.recommender.recommend(TICKET))
        self.assertEqual(worker.await_count, 3)


if __name__ == "__main__":
    unittest.main()
