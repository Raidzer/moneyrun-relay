"""Проверки RSS, кеша и длины сообщений без сети и Telegram."""

import asyncio
from http.client import IncompleteRead
import os
from urllib.error import HTTPError, URLError
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree import ElementTree

from jokes import (CACHE_SECONDS, FETCH_TIMEOUT, FEED_URL, JokeSource, MAX_FEED_BYTES,
                   RETRY_SECONDS, SOURCE_URL, _fetch_jokes, append_joke, jokes_enabled)


REAL_TO_THREAD = asyncio.to_thread


class EnabledTests(unittest.TestCase):
    def test_default_is_false(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(jokes_enabled())

    def test_true_and_false_ignore_case_and_surrounding_whitespace(self):
        for value, expected in (("true", True), (" TrUe\t", True),
                                ("false", False), ("\nFaLsE ", False)):
            with self.subTest(value=value), patch.dict(os.environ, {"JOKES_ENABLED": value}):
                self.assertIs(jokes_enabled(), expected)

    def test_empty_and_invalid_values_are_rejected(self):
        for value in ("", " \t", "1", "yes", "true false"):
            with self.subTest(value=value), patch.dict(os.environ, {"JOKES_ENABLED": value}):
                with self.assertRaisesRegex(ValueError, "JOKES_ENABLED.*true.*false"):
                    jokes_enabled()


def rss(*descriptions, encoding="utf-8"):
    items = "".join(f"<item><description><![CDATA[{text}]]></description></item>"
                    for text in descriptions)
    return (f'<?xml version="1.0" encoding="{encoding}"?>'
            f"<rss><channel>{items}</channel></rss>").encode(encoding)


class FeedTests(unittest.TestCase):
    def fetch(self, body):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        with patch("jokes.urlopen", return_value=response) as opener:
            result = _fetch_jokes()
        self.assertEqual(opener.call_args.args[0].full_url, FEED_URL)
        self.assertEqual(opener.call_args.kwargs, {"timeout": FETCH_TIMEOUT})
        response.read.assert_called_once_with(MAX_FEED_BYTES + 1)
        response.__exit__.assert_called_once()
        return result

    def test_russian_encoding_html_entities_and_line_order(self):
        for encoding in ("utf-8", "windows-1251", "utf-16"):
            with self.subTest(encoding=encoding):
                self.assertEqual(self.fetch(rss(
                    "<b>Первая</b> &amp; вторая<br />Третья\nЧетвёртая",
                    "  ", "<br>", "&lt;b&gt;Текст&lt;/b&gt;", encoding=encoding)),
                    ["Первая & вторая\nТретья\nЧетвёртая", "<b>Текст</b>"])
        self.assertEqual(self.fetch(b"<rss><channel><item><description>First<br/>"
                                   b"Second <b>third</b></description></item></channel></rss>"),
                         ["First\nSecond third"])

    def test_malformed_unknown_encoding_and_empty_feed(self):
        for body in (b"<rss>", b'<?xml version="1.0" encoding="unknown"?><rss/>',
                     rss(), rss("  ", "<br>")):
            with self.subTest(body=body), self.assertRaises(
                    (ElementTree.ParseError, ValueError, LookupError)):
                self.fetch(body)

    def test_oversized_body_and_dtd_are_rejected_before_xml_parser(self):
        dtd = '<!DOCTYPE rss [<!ENTITY joke "unsafe">]><rss><channel/></rss>'
        for body in (b"x" * (MAX_FEED_BYTES + 1), dtd.encode(),
                     dtd.encode("utf-16"), dtd.encode("utf-32")):
            with self.subTest(length=len(body)), patch("jokes.ElementTree.fromstring") as parse:
                with self.assertRaises(ValueError):
                    self.fetch(body)
                parse.assert_not_called()


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = JokeSource()
        clock_patch = patch("jokes.time")
        self.clock = clock_patch.start().monotonic
        self.clock.return_value = 100
        self.addCleanup(clock_patch.stop)
        fetch_patch = patch("jokes.asyncio.to_thread", new_callable=AsyncMock,
                            return_value=["Первый", "Второй"])
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    async def test_cache_hour_and_random_selection(self):
        with patch("jokes.random.choice", side_effect=lambda pool: pool[-1]) as choose:
            self.assertEqual(await self.source.choose(), "Второй")
            self.clock.return_value = 100 + CACHE_SECONDS - 1
            self.assertEqual(await self.source.choose(), "Второй")
            self.fetch.assert_awaited_once_with(_fetch_jokes)
            self.clock.return_value += 1
            self.fetch.return_value = ["Новый"]
            self.assertEqual(await self.source.choose(), "Новый")
            self.assertEqual(self.fetch.await_count, 2)
            self.assertEqual(choose.call_count, 3)

    async def test_expected_failures_return_none_and_sanitize_warning(self):
        for error in (URLError("secret"), HTTPError(FEED_URL, 500, "secret", {}, None),
                      IncompleteRead(b"secret", 10), OSError("secret"),
                      ElementTree.ParseError("secret"), ValueError("secret"),
                      LookupError("secret")):
            with self.subTest(error=type(error).__name__):
                self.source = JokeSource()
                self.fetch.side_effect = error
                with self.assertLogs("moneyrun_relay", level="WARNING") as logs:
                    self.assertIsNone(await self.source.choose())
                self.assertNotIn("secret", "".join(logs.output))
                self.assertIsNone(logs.records[0].exc_info)

    async def test_error_cooldown_and_previous_pool_survive_failed_refresh(self):
        self.fetch.side_effect = URLError("offline")
        with self.assertLogs("moneyrun_relay", level="WARNING"):
            self.assertIsNone(await self.source.choose())
        self.clock.return_value = 100 + RETRY_SECONDS - 1
        self.assertIsNone(await self.source.choose())
        self.fetch.assert_awaited_once()
        self.clock.return_value += 1
        self.fetch.side_effect = None
        self.fetch.return_value = ["Старый"]
        self.assertEqual(await self.source.choose(), "Старый")
        self.clock.return_value += CACHE_SECONDS
        self.fetch.side_effect = ElementTree.ParseError("bad feed")
        with self.assertLogs("moneyrun_relay", level="WARNING"):
            self.assertEqual(await self.source.choose(), "Старый")
        self.clock.return_value += RETRY_SECONDS - 1
        self.assertEqual(await self.source.choose(), "Старый")
        self.assertEqual(self.fetch.await_count, 3)

    async def test_async_timeout_keeps_previous_pool(self):
        cancelled = asyncio.Event()

        async def blocked(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.source._jokes = ["Старый"]
        self.fetch.side_effect = blocked
        with patch("jokes.FETCH_TIMEOUT", 0.01), self.assertLogs("moneyrun_relay", level="WARNING"):
            self.assertEqual(await self.source.choose(), "Старый")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.source._next_fetch_at, 100 + RETRY_SECONDS)

    async def test_cancellation_propagates_and_concurrent_calls_do_not_refetch(self):
        started = asyncio.Event()

        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()

        self.fetch.side_effect = blocked
        task = asyncio.create_task(self.source.choose())
        await started.wait()
        self.assertIsNone(await self.source.choose())
        self.fetch.assert_awaited_once()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_thread_fetch_is_used_without_network(self):
        with patch("jokes.asyncio.to_thread", wraps=REAL_TO_THREAD), \
                patch("jokes._fetch_jokes", return_value=["Из потока"]) as fetch:
            self.assertEqual(await self.source.choose(), "Из потока")
        fetch.assert_called_once_with()


class AppendTests(unittest.TestCase):
    def test_plain_text_source_and_original_are_preserved(self):
        text = "Исходная карточка\n\n"
        joke = "<b>Слова</b> *без форматирования*\nВторая строка 🙂"
        self.assertEqual(append_joke(text, joke),
                         text + "\n\n────────────\n😄 Анекдот\n" + joke + "\n\nИсточник: " + SOURCE_URL)
        for empty in (None, "", " \n "):
            self.assertIs(append_joke(text, empty), text)

    def test_utf16_exact_boundary_and_overflow_skip_entire_joke(self):
        joke = "🙂"
        suffix = append_joke("", joke)
        remaining = 4096 - len(suffix.encode("utf-16-le")) // 2
        text = "Я" * remaining
        self.assertEqual(append_joke(text, joke), text + suffix)
        for original, too_long in ((text + "Я", joke), (text, joke + "🙂"),
                                   ("🙂" * 2048, "короткий"), ("Карточка", "Я" * 4096)):
            with self.subTest(length=len(original)):
                self.assertIs(append_joke(original, too_long), original)


if __name__ == "__main__":
    unittest.main()
