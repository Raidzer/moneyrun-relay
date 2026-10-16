"""Проверки без Telegram, токенов и отправки сообщений."""

import asyncio
from datetime import datetime, timezone
from http.client import IncompleteRead
import json
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.error import HTTPError
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.tl.types import (Message, MessageReplyHeader, PeerChannel,
                              MessageEntityBold, MessageEntityTextUrl)
from telethon.errors import FloodWaitError
from relay import (check_unattended_config, main, message_payload, process_message,
                   poll_interval_seconds, read_state, resolve_pending, selected,
                   single_instance, validate_config, write_json, reminder_settings,
                   quiet_hours, reminder_payloads, run_tracking, bot_call, bot_request, BotRateLimitError)
from tracking import TicketStore


CONFIG = {
    "source_chat_id": -1001234567890,
    "source_topic_id": None,
    "destination_chat_id": -1009876543210,
    "destination_topic_id": None,
    "club_hashtag": "#KhromushkinTeam",
}
TEXT = ("#KhromushkinTeam\n\nПользователь Тестовый Атлет написал в тикет #123456\n\n"
        "Хорошо, если ещё возникнут вопросы, мы всегда на связи!\n\nХороших тренировок 🙂")
CREATED = TEXT.replace("написал в тикет #123456", "создал новый тикет #123456 - Вопрос")
BOT = {"type": "group", "id": 1234, "username": "test_relay_bot", "status": "administrator"}
SENT = {"message_id": 900}
REAL_SLEEP = asyncio.sleep


async def idle_commands(*args):
    await args[4].wait()


async def no_delay(seconds):
    await REAL_SLEEP(0)


async def skip_poll_delay(event, seconds):
    await no_delay(seconds)


def stored_state(path):
    store = TicketStore(path, CONFIG)
    try:
        return store.state
    finally:
        store.close()


def message(text=TEXT, message_id=101, **fields):
    fields.setdefault("date", datetime.now(timezone.utc))
    return Message(id=message_id, peer_id=PeerChannel(1234567890), message=text, **fields)


class RelayTests(unittest.TestCase):
    def test_interrupted_http_body_becomes_sanitized_retryable_error(self):
        for http_error in (False, True):
            with self.subTest(http_error=http_error):
                response = MagicMock()
                response.read.side_effect = IncompleteRead(b'{"ok":', 12)
                if http_error:
                    error = HTTPError("https://example.test/botsecret", 502, "gateway", {}, response)
                    opener = patch("relay.urlopen", side_effect=error)
                else:
                    response.__enter__.return_value = response
                    opener = patch("relay.urlopen", return_value=response)
                with opener, self.assertRaises(RuntimeError) as caught:
                    bot_request("secret", "setMessageReaction", {})
                self.assertNotIn("secret", str(caught.exception))

    def test_reminder_configuration_and_moscow_quiet_boundaries(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = reminder_settings()
        self.assertEqual(settings, {"interval": 14400, "start": 1380, "end": 540})
        for hour, minute, quiet in ((22, 59, False), (23, 0, True), (8, 59, True), (9, 0, False)):
            now = datetime(2026, 10, 15, hour, minute, tzinfo=timezone.utc).timestamp() - 3 * 3600
            self.assertEqual(quiet_hours(now, settings), quiet)
        for update in ({"REMINDER_INTERVAL_HOURS": "0"}, {"REMINDER_INTERVAL_HOURS": "nan"},
                       {"QUIET_HOURS_START": "24:00"}, {"QUIET_HOURS_END": "9:00"},
                       {"QUIET_HOURS_START": "09:00", "QUIET_HOURS_END": "09:00"}):
            with patch.dict("os.environ", update, clear=True), self.assertRaises(ValueError):
                reminder_settings()
        with patch.dict("os.environ", {"REMINDER_INTERVAL_HOURS": "2",
                                        "QUIET_HOURS_START": "12:30", "QUIET_HOURS_END": "14:00"}, clear=True):
            settings = reminder_settings()
        self.assertEqual(settings["interval"], 7200)
        self.assertTrue(quiet_hours(datetime(2026, 10, 15, 10, tzinfo=timezone.utc).timestamp(), settings))

    def test_long_reminder_keeps_all_links_and_topic_with_utf16_limit(self):
        tickets = [{"ticket_id": i, "author": "🙂" * 100, "title": "🙂" * 200,
                    "created_at": 0, "source_message_id": i + 100} for i in range(1, 41)]
        payloads = reminder_payloads({"regular_due": False, "tickets": tickets},
                                    {**CONFIG, "destination_topic_id": 73}, 25 * 3600)
        self.assertGreater(len(payloads), 1)
        for payload in payloads:
            self.assertEqual(payload["message_thread_id"], 73)
            self.assertLessEqual(len(payload["text"].encode("utf-16-le")) // 2, 4096)
        full_text = "\n".join(payload["text"] for payload in payloads)
        for ticket in tickets:
            self.assertIn(f"#{ticket['ticket_id']} ·", full_text)
            self.assertIn(f"https://t.me/c/1234567890/{ticket['source_message_id']}", full_text)

    def test_second_process_cannot_share_reader_folder(self):
        with TemporaryDirectory() as folder, patch("relay.LOCAL", Path(folder)):
            with single_instance():
                with self.assertRaisesRegex(RuntimeError, "Другая копия"):
                    with single_instance():
                        self.fail("Reader lock was bypassed")

    def test_poll_interval_default_and_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(poll_interval_seconds(), 15)
            for value, expected in (("60", 60), ("1", 1), (" 30 ", 30)):
                with patch.dict("os.environ", {"POLL_INTERVAL_SECONDS": value}):
                    self.assertEqual(poll_interval_seconds(), expected)

    def test_invalid_poll_interval_is_rejected(self):
        for value in ("0", "-1", "", "abc", "1.5", "nan", "inf"):
            with self.subTest(value=value), patch.dict("os.environ", {"POLL_INTERVAL_SECONDS": value}):
                with self.assertRaisesRegex(ValueError, "POLL_INTERVAL_SECONDS"):
                    poll_interval_seconds()

    def test_real_format_and_no_foreign_clubs(self):
        self.assertTrue(selected(message(), CONFIG))
        self.assertTrue(selected(message(TEXT.lower()), CONFIG))
        for text in (TEXT.replace("#KhromushkinTeam", "#OtherClub"),
                     TEXT.replace("#KhromushkinTeam", "#KhromushkinTeam2"),
                     "#OtherClub\nВ тексте упомянут #KhromushkinTeam", "", " "):
            self.assertFalse(selected(message(text), CONFIG))
        self.assertFalse(selected(message(), {**CONFIG, "source_chat_id": -1001111111111}))

    def test_topic_scope(self):
        direct = message(reply_to=MessageReplyHeader(reply_to_msg_id=50, forum_topic=True))
        reply = message(reply_to=MessageReplyHeader(
            reply_to_msg_id=90, reply_to_top_id=50, forum_topic=True))
        config = {**CONFIG, "source_topic_id": 50}
        self.assertTrue(selected(direct, config))
        self.assertTrue(selected(reply, config))
        self.assertFalse(selected(message(), config))
        self.assertTrue(selected(message(), {**config, "source_topic_id": 1}))

    def test_text_preserved_and_original_link(self):
        payload = message_payload(message(), {**CONFIG, "destination_topic_id": 73})
        self.assertEqual(payload["text"], TEXT)
        self.assertNotIn("parse_mode", payload)
        self.assertNotIn("entities", payload)
        self.assertEqual(payload["message_thread_id"], 73)
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://t.me/c/1234567890/101")

    def test_author_and_ticket_links_keep_utf16_offsets(self):
        author = "Иван 🙂 & Пётр"
        text = CREATED.replace("Тестовый Атлет", author)
        links = []
        expected = []
        for label, url in ((author, "https://example.test/users/42?from=tg&mode=care"),
                           ("#123456", "https://example.test/tickets/123456")):
            offset = len(text[:text.index(label)].encode("utf-16-le")) // 2
            length = len(label.encode("utf-16-le")) // 2
            links.append(MessageEntityTextUrl(offset, length, url))
            expected.append({"type": "text_link", "offset": offset, "length": length, "url": url})
        payload = message_payload(message(text, entities=links), CONFIG)
        self.assertEqual(payload["text"], text)
        self.assertEqual(payload["entities"], expected)
        self.assertNotIn("parse_mode", payload)
        self.assertGreater(expected[1]["offset"], text.index("#123456"))
        for entity, label in zip(payload["entities"], (author, "#123456")):
            start = entity["offset"] * 2
            end = start + entity["length"] * 2
            self.assertEqual(payload["text"].encode("utf-16-le")[start:end].decode("utf-16-le"), label)

    def test_unknown_or_invalid_link_entities_are_skipped(self):
        invalid = [MessageEntityBold(0, 4)]
        for offset, length, url in ((-1, 2, "https://example.test/"), (0, 0, "https://example.test/"),
                                    (0, len(TEXT.encode("utf-16-le")) // 2 + 1, "https://example.test/"),
                                    (0, 4, ""),
                                    (True, 4, "https://example.test/")):
            invalid.append(MessageEntityTextUrl(offset, length, url))
        self.assertNotIn("entities", message_payload(message(entities=invalid), CONFIG))

    def test_send_and_restart_do_not_repeat(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = {"route": CONFIG, "last_id": 100}
            send = AsyncMock()
            save = lambda data: write_json(path, data)
            self.assertTrue(asyncio.run(process_message(message(), CONFIG, state, save, send)))
            restored = read_state(path, CONFIG)
            self.assertFalse(asyncio.run(process_message(message(), CONFIG, restored, save, send)))
            self.assertEqual(send.await_count, 1)
            self.assertEqual(restored["last_id"], 101)

    def test_uncertain_send_blocks_retry_after_restart(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = {"route": CONFIG, "last_id": 100}
            send = AsyncMock(side_effect=RuntimeError("test failure"))
            with self.assertRaises(RuntimeError):
                asyncio.run(process_message(message(), CONFIG, state,
                                            lambda data: write_json(path, data), send))
            self.assertEqual(state["last_id"], 100)
            self.assertEqual(state["pending_id"], 101)
            with self.assertRaisesRegex(RuntimeError, "101"):
                read_state(path, CONFIG)
            with self.assertRaises(RuntimeError):
                asyncio.run(process_message(message(), CONFIG, state, lambda data: None, send))
            self.assertEqual(send.await_count, 1)

    def test_failed_pending_write_does_not_send(self):
        send = AsyncMock()
        with self.assertRaises(OSError):
            asyncio.run(process_message(message(), CONFIG, {"last_id": 100},
                                        lambda data: self.raise_disk_error(), send))
        send.assert_not_awaited()

    @staticmethod
    def raise_disk_error():
        raise OSError("disk full")

    def test_failed_checkpoint_keeps_persisted_pending(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = {"route": CONFIG, "last_id": 100}

            def save(data):
                if data["last_id"] == 101:
                    raise OSError("disk full")
                write_json(path, data)

            send = AsyncMock()
            with self.assertRaises(OSError):
                asyncio.run(process_message(message(), CONFIG, state, save, send))
            self.assertEqual(send.await_count, 1)
            self.assertEqual(read_state(path, CONFIG, allow_pending=True)["pending_id"], 101)
            with self.assertRaises(RuntimeError):
                read_state(path, CONFIG)

    def test_manual_resolution_sent_or_retry(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            for result, last_id in (("sent", 101), ("retry", 100)):
                write_json(path, {"route": CONFIG, "last_id": 100, "pending_id": 101})
                with self.assertRaises(ValueError):
                    resolve_pending(path, CONFIG, 102, result)
                resolve_pending(path, CONFIG, 101, result)
                restored = read_state(path, CONFIG)
                self.assertEqual(restored["last_id"], last_id)
                self.assertIsNone(restored["pending_id"])
                send = AsyncMock()
                asyncio.run(process_message(message(), CONFIG, restored,
                                            lambda data: write_json(path, data), send))
                self.assertEqual(send.await_count, int(result == "retry"))

    def test_non_matching_and_protected_messages(self):
        state, saved, send = {"last_id": 100}, [], AsyncMock()
        self.assertFalse(asyncio.run(process_message(message("#OtherClub\ntext"), CONFIG,
                                                     state, saved.append, send)))
        self.assertEqual(state["last_id"], 101)
        with self.assertRaises(RuntimeError):
            asyncio.run(process_message(message(message_id=102, noforwards=True), CONFIG,
                                        state, saved.append, send))
        self.assertEqual(state["last_id"], 101)
        send.assert_not_awaited()

    def test_incomplete_or_looping_route_is_rejected(self):
        validate_config(CONFIG, sending=True)
        for update in ({"source_chat_id": None}, {"destination_chat_id": CONFIG["source_chat_id"]},
                       {"club_hashtag": ""}, {"source_topic_id": -1}):
            with self.assertRaises(ValueError):
                validate_config({**CONFIG, **update}, sending=True)

    def test_changed_route_and_corrupt_state_are_not_reset(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            write_json(path, {"route": CONFIG, "last_id": 101})
            with self.assertRaises(ValueError):
                read_state(path, {**CONFIG, "destination_chat_id": -1234})
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                read_state(path, CONFIG)

    def test_invalid_pending_is_not_reset(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            for pending in (True, 99, 100, "101"):
                write_json(path, {"route": CONFIG, "last_id": 100, "pending_id": pending})
                with self.assertRaises(ValueError):
                    read_state(path, CONFIG, allow_pending=True)

    def test_unattended_requires_credentials_and_session(self):
        config = {"api_id": 1234, "api_hash": "dummy", "bot_token": "dummy"}
        with TemporaryDirectory() as folder, patch("relay.LOCAL", Path(folder)):
            for key in config:
                with self.assertRaisesRegex(ValueError, key):
                    check_unattended_config({**config, key: ""}, "run")
            with self.assertRaisesRegex(ValueError, "reader.session"):
                check_unattended_config(config, "run")
            (Path(folder) / "reader.session").touch()
            check_unattended_config(config, "run")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.joke_choice = AsyncMock(return_value=None)
        joke_patch = patch("relay.JokeSource.choose", self.joke_choice)
        joke_patch.start()
        self.addCleanup(joke_patch.stop)
        env_patch = patch.dict("os.environ", {"JOKES_ENABLED": "false"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    async def test_rate_limit_after_first_summary_part_does_not_repeat_delivered_tickets(self):
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
        with TemporaryDirectory() as folder:
            store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100, now=now-25*3600)
            for msg_id in range(101, 121):
                store.process(msg_id, {"kind": "created", "ticket_id": msg_id,
                                      "author": "🙂" * 100, "title": "🙂" * 200}, now-25*3600)
                store.confirm_sent(msg_id)
            stop = asyncio.Event()
            client = AsyncMock()
            client.get_messages.return_value = []
            sent = 0

            async def api(*args, **kwargs):
                nonlocal sent
                sent += 1
                if sent == 2:
                    raise BotRateLimitError(60)
                return SENT

            async def wait(event, seconds):
                event.set()

            try:
                with patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.time.time", return_value=now), \
                     patch("relay.asyncio.sleep", new_callable=AsyncMock), \
                     patch("relay.wait_or_stop", side_effect=wait), \
                     patch("relay.bot_call", side_effect=api), patch("relay.LOG"), patch("builtins.print"):
                    await run_tracking(client, object(), CONFIG, store, stop, 60,
                                       {"interval": 14400, "start": 1380, "end": 540}, "dummy", BOT)
                self.assertEqual(sent, 2)
                self.assertEqual(store.state["send_retry_at"], now+60)
                self.assertEqual(store.state["last_regular_at"], now)
                self.assertEqual(store.reminder_plan(now+60, 14400, False)["tickets"], [])
                self.assertEqual(len(store.reminder_plan(now+1800, 14400, False)["tickets"]), 20)
            finally:
                store.close()

    async def test_rate_limited_reminder_yields_to_new_replies_and_rebuilds_after_cooldown(self):
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
        clock = [now]
        with TemporaryDirectory() as folder:
            store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100, now=now-25*3600)
            for msg_id, ticket_id in ((101, 1), (102, 2)):
                store.process(msg_id, {"kind": "created", "ticket_id": ticket_id,
                                      "author": "Атлет", "title": "Вопрос"}, now-25*3600)
                store.confirm_sent(msg_id)
            reads = 0
            stop = asyncio.Event()

            async def read(*args, **kwargs):
                nonlocal reads
                reads += 1
                if reads == 2:
                    clock[0] = now + 60
                    return [message("#KhromushkinTeam\n\nПользователь Другой написал в тикет #1\n\nОтвет", message_id=103)]
                if reads == 3:
                    clock[0] = now + 600
                return []

            sent = []

            async def api(token, method, payload, **kwargs):
                sent.append(payload["text"])
                if len(sent) == 1:
                    raise BotRateLimitError(600)
                self.assertNotIn("#1 ·", payload["text"])
                self.assertIn("#2 ·", payload["text"])
                stop.set()
                return SENT

            client = AsyncMock()
            client.get_messages.side_effect = read
            try:
                with patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.time.time", side_effect=lambda: clock[0]), \
                     patch("relay.asyncio.sleep", new_callable=AsyncMock), \
                     patch("relay.wait_or_stop", new_callable=AsyncMock), \
                     patch("relay.bot_call", side_effect=api), patch("relay.LOG"), patch("builtins.print"):
                    await asyncio.wait_for(run_tracking(client, object(), CONFIG, store, stop, 60,
                                           {"interval": 14400, "start": 1380, "end": 540}, "dummy", BOT), 2)
                self.assertEqual(len(sent), 2)
                self.assertEqual(reads, 3)
                self.assertIsNone(store.state["pending_id"])
                self.assertEqual(store.state["send_retry_at"], now+600)
            finally:
                store.close()

    async def test_worker_failure_and_stop_interrupt_blocked_source_read(self):
        for fail in (True, False):
            with self.subTest(fail=fail), TemporaryDirectory() as folder:
                store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100)
                entered, stop = asyncio.Event(), asyncio.Event()

                async def read(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                async def commands(*args):
                    await entered.wait()
                    if fail:
                        raise RuntimeError("worker failed")
                    stop.set()

                client = AsyncMock()
                client.get_messages.side_effect = read
                try:
                    with patch("relay.command_loop", side_effect=commands):
                        task = run_tracking(client, object(), CONFIG, store, stop, 60,
                                            {"interval": 14400, "start": 1380, "end": 540}, "dummy", BOT)
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                                await asyncio.wait_for(task, 1)
                        else:
                            await asyncio.wait_for(task, 1)
                    self.assertEqual(store.state["last_id"], 100)
                finally:
                    store.close()

    async def test_quiet_boundary_cancels_queued_regular_summary_but_sends_urgent_list(self):
        evening = datetime(2026, 10, 15, 19, 59, 59, tzinfo=timezone.utc).timestamp()
        clock = [evening]
        with TemporaryDirectory() as folder:
            store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100, now=evening-25*3600)
            store.process(101, {"kind": "created", "ticket_id": 1, "author": "Атлет", "title": "Вопрос"}, evening-25*3600)
            store.confirm_sent(101)
            stop = asyncio.Event()
            client = AsyncMock()
            client.get_messages.return_value = []

            async def cross_boundary(seconds):
                clock[0] = evening + 2

            async def api(token, method, payload, **kwargs):
                self.assertTrue(payload["text"].startswith("Срочно ❗❗❗:"))
                stop.set()
                return SENT

            try:
                with patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.time.time", side_effect=lambda: clock[0]), \
                     patch("relay.asyncio.sleep", side_effect=cross_boundary), \
                     patch("relay.wait_or_stop", new_callable=AsyncMock), \
                     patch("relay.bot_call", side_effect=api) as calls, patch("builtins.print"):
                    await asyncio.wait_for(run_tracking(client, object(), CONFIG, store, stop, 60,
                                           {"interval": 14400, "start": 1380, "end": 540}, "dummy", BOT), 2)
                calls.assert_awaited_once()
                self.assertEqual(client.get_messages.await_count, 2)
                self.assertEqual(store.state["last_regular_at"], evening-25*3600)
                self.assertEqual(store.pending_tickets()[0]["urgent_at"], evening+2)
            finally:
                store.close()

    async def test_pause_during_rate_limit_wait_prevents_retry(self):
        allowed = [True]

        async def pause(seconds):
            allowed[0] = False

        with patch("relay.bot_request", return_value={"ok": False, "error_code": 429,
                                                       "parameters": {"retry_after": 30}}) as request, \
             patch("relay.asyncio.sleep", side_effect=pause):
            self.assertIsNone(await bot_call("dummy", "sendMessage", {}, allowed=lambda: allowed[0]))
        request.assert_called_once()

    async def test_admin_pause_is_received_while_source_waits_and_new_ticket_still_sends(self):
        now = datetime.now(timezone.utc).timestamp()
        with TemporaryDirectory() as folder:
            store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100, now=now-10)
            paused, stop = asyncio.Event(), asyncio.Event()
            apply = store.apply_command

            def apply_and_signal(value, update_id):
                apply(value, update_id)
                paused.set()

            store.apply_command = apply_and_signal

            async def read(*args, **kwargs):
                await paused.wait()
                return [message(CREATED)]

            client = AsyncMock()
            client.get_messages.side_effect = read
            polls = 0
            sent = []

            async def api(token, method, payload, **kwargs):
                nonlocal polls
                if method == "getUpdates":
                    polls += 1
                    if polls == 1:
                        return [{"update_id": 1, "message": {"chat": {"id": CONFIG["destination_chat_id"]},
                                 "from": {"id": 7, "is_bot": False}, "date": int(now), "text": "/pause"}}]
                    await stop.wait()
                    return []
                if method == "getChatMember":
                    return {"status": "administrator"}
                sent.append(payload["text"])
                if payload["text"] == CREATED:
                    self.assertTrue(store.state["paused"])
                    stop.set()
                return SENT

            try:
                with patch("relay.bot_call", side_effect=api), \
                     patch("relay.asyncio.sleep", new_callable=AsyncMock), patch("builtins.print"):
                    await asyncio.wait_for(run_tracking(client, object(), CONFIG, store, stop, 60,
                                           {"interval": 14400, "start": 1380, "end": 540}, "dummy", BOT), 2)
                self.assertIn(CREATED, sent)
                self.assertTrue(store.state["paused"])
                self.assertEqual(store.state["command_offset"], 2)
                self.assertEqual(store.state["last_id"], 101)
                self.assertIsNone(store.state["pending_id"])
            finally:
                store.close()

    async def test_first_tracking_start_skips_legacy_history_then_resume_keeps_tickets(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "reader.session").touch()
            write_json(root / "config.json", {**CONFIG, "api_id": 1234,
                                             "api_hash": "dummy", "bot_token": "dummy"})
            write_json(root / "state.json", {"route": CONFIG, "last_id": 3})
            client = AsyncMock()
            client.is_user_authorized.return_value = True
            client.is_bot.return_value = False
            client.get_entity.return_value = SimpleNamespace(noforwards=False)
            client.get_messages.side_effect = [[message(message_id=1000)], [message(CREATED, message_id=1020)]]

            async def stop_wait(stop, seconds):
                stop.set()

            with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                 patch("telethon.TelegramClient", return_value=client), patch("relay.signal.signal"), \
                 patch("relay.bot_call", side_effect=lambda token, method, payload, **kwargs:
                       SENT if method == "sendMessage" else BOT) as api, patch("builtins.print"), \
                 patch("relay.command_loop", side_effect=idle_commands), \
                 patch("relay.asyncio.sleep", new_callable=AsyncMock), \
                 patch("relay.wait_or_stop", side_effect=stop_wait):
                await main(SimpleNamespace(command="run", non_interactive=True))
                self.assertEqual(client.get_messages.await_args_list[0].kwargs, {"limit": 1})
                self.assertEqual(client.get_messages.await_args_list[1].kwargs["min_id"], 1000)
                self.assertEqual(sum(call.args[1] == "sendMessage" for call in api.await_args_list), 1)
                self.assertTrue((root / "tracking.initialized").exists())
                api.reset_mock()
                client.get_messages.reset_mock()
                reply = TEXT.replace("Тестовый Атлет", "Другой Человек")
                client.get_messages.side_effect = [[message(reply, message_id=1030)]]
                await main(SimpleNamespace(command="run", non_interactive=True))
                self.assertEqual(client.get_messages.await_count, 1)
                self.assertEqual(client.get_messages.await_args.kwargs["min_id"], 1020)
                self.assertFalse(any(call.args[1] == "sendMessage" for call in api.await_args_list))
            store = TicketStore(root / "tickets.sqlite3", CONFIG)
            try:
                self.assertEqual(store.pending_tickets(), [])
                self.assertEqual(store.state["last_id"], 1030)
            finally:
                store.close()
            self.joke_choice.assert_not_awaited()

    async def test_global_joke_setting_reaches_worker_and_invalid_value_fails_before_connect(self):
        for value, expected in (("false", False), ("true", True), ("wrong", None)):
            with self.subTest(value=value), TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "reader.session").touch()
                write_json(root / "config.json", {**CONFIG, "api_id": 1234,
                                                 "api_hash": "dummy", "bot_token": "dummy"})
                client = AsyncMock()
                client.is_user_authorized.return_value = True
                client.is_bot.return_value = False
                client.get_entity.return_value = SimpleNamespace(noforwards=False)
                client.get_messages.return_value = []
                with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                     patch.dict("os.environ", {"JOKES_ENABLED": value}), \
                     patch("telethon.TelegramClient", return_value=client) as factory, \
                     patch("relay.bot_call", return_value=BOT), patch("relay.signal.signal"), \
                     patch("relay.run_tracking", new_callable=AsyncMock) as worker, patch("builtins.print"):
                    if expected is None:
                        with self.assertRaisesRegex(ValueError, "JOKES_ENABLED"):
                            await main(SimpleNamespace(command="run", non_interactive=True))
                        factory.assert_not_called()
                        worker.assert_not_awaited()
                    else:
                        await main(SimpleNamespace(command="run", non_interactive=True))
                        worker.assert_awaited_once()
                        self.assertEqual(worker.await_args.kwargs, {"use_jokes": expected})

    async def test_disappeared_tracking_database_does_not_bootstrap_again(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "config.json", CONFIG)
            (root / "tracking.initialized").touch()
            with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                 patch("telethon.TelegramClient") as client:
                with self.assertRaisesRegex(ValueError, "Пропала база"):
                    await main(SimpleNamespace(command="run", non_interactive=True))
            client.assert_not_called()
            self.assertFalse((root / "tickets.sqlite3").exists())

    async def test_uncertain_creation_is_resolved_without_losing_card(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "reader.session").touch()
            write_json(root / "config.json", {**CONFIG, "api_id": 1234,
                                             "api_hash": "dummy", "bot_token": "dummy"})
            client = AsyncMock()
            client.is_user_authorized.return_value = True
            client.is_bot.return_value = False
            client.get_entity.return_value = SimpleNamespace(noforwards=False)
            client.get_messages.side_effect = [[message(message_id=100)], [message(CREATED)]]

            async def api(token, method, payload):
                if method == "sendMessage":
                    raise RuntimeError("uncertain delivery")
                return BOT

            with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                 patch("telethon.TelegramClient", return_value=client), patch("relay.signal.signal"), \
                 patch("relay.bot_call", side_effect=api) as calls, patch("builtins.print"), \
                 patch("relay.command_loop", side_effect=idle_commands):
                with self.assertRaisesRegex(RuntimeError, "uncertain delivery"):
                    await main(SimpleNamespace(command="run", non_interactive=True))
                self.assertEqual(stored_state(root / "tickets.sqlite3")["pending_id"], 101)
                calls.reset_mock()
                await main(SimpleNamespace(command="resolve", message_id=101, result="sent"))
                calls.assert_not_awaited()
            store = TicketStore(root / "tickets.sqlite3", CONFIG)
            try:
                self.assertEqual(store.state["last_id"], 101)
                self.assertIsNone(store.state["pending_id"])
                self.assertEqual(len(store.pending_tickets()), 1)
            finally:
                store.close()

    async def test_pending_legacy_send_blocks_first_tracking_start(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "config.json", CONFIG)
            write_json(root / "state.json", {"route": CONFIG, "last_id": 100, "pending_id": 101})
            with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                 patch("telethon.TelegramClient") as client:
                with self.assertRaisesRegex(RuntimeError, "101"):
                    await main(SimpleNamespace(command="run", non_interactive=True))
            client.assert_not_called()

    async def test_backlog_reply_prevents_stale_reminder_and_network_failure_defers_it(self):
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
        for offline in (False, True):
            with self.subTest(offline=offline), TemporaryDirectory() as folder:
                store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100, now=now-25*3600)
                event = {"kind": "created", "ticket_id": 123456, "author": "Тестовый Атлет", "title": "Вопрос"}
                store.process(101, event, now-25*3600)
                store.confirm_sent(101)
                client = AsyncMock()
                if offline:
                    client.get_messages.side_effect = OSError("offline")
                else:
                    client.get_messages.side_effect = [
                        [message("#OtherClub\ntext", message_id=i) for i in range(102, 202)],
                        [message(TEXT.replace("Тестовый Атлет", "Другой Человек"), message_id=202)],
                    ]
                stop = asyncio.Event()

                async def wait(event, seconds):
                    event.set()

                try:
                    with patch("relay.bot_call", new_callable=AsyncMock) as api, \
                         patch("relay.command_loop", side_effect=idle_commands), \
                         patch("relay.wait_or_stop", side_effect=wait), patch("relay.time.time", return_value=now), \
                         patch("relay.LOG"), patch("builtins.print"):
                        await run_tracking(client, object(), CONFIG, store, stop, 60,
                                           {"interval": 14400, "start": 1380, "end": 540}, "dummy", BOT)
                    api.assert_not_awaited()
                    self.assertEqual(len(store.pending_tickets()), int(offline))
                    self.assertEqual(store.state["last_regular_at"], now-25*3600 if offline else now)
                finally:
                    store.close()

    async def test_reminder_failure_does_not_block_reader_or_create_pending_send(self):
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
        with TemporaryDirectory() as folder:
            store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100, now=now-25*3600)
            store.process(101, {"kind": "created", "ticket_id": 1, "author": "Атлет", "title": "Вопрос"}, now-25*3600)
            store.confirm_sent(101)
            client = AsyncMock()
            client.get_messages.return_value = []
            waits = 0

            async def wait(stop, seconds):
                nonlocal waits
                waits += 1
                if waits == 2:
                    stop.set()

            try:
                with patch("relay.bot_call", side_effect=RuntimeError("uncertain delivery")) as api, \
                     patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.wait_or_stop", side_effect=wait), patch("relay.time.time", return_value=now), \
                     patch("relay.LOG"), patch("builtins.print"):
                    await run_tracking(client, object(), CONFIG, store, asyncio.Event(), 60,
                                       {"interval": 14400, "start": 1380, "end": 540}, "dummy", BOT)
                api.assert_awaited_once()
                self.assertEqual(client.get_messages.await_count, 2)
                self.assertIsNone(store.state["pending_id"])
                self.assertEqual(store.pending_tickets()[0]["urgent_at"], now)
            finally:
                store.close()
    async def test_backlog_drains_full_batches_before_poll_wait(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "reader.session").touch()
            write_json(root / "config.json", {**CONFIG, "api_id": 1234,
                                             "api_hash": "dummy", "bot_token": "dummy"})
            write_json(root / "state.json", {"route": CONFIG, "last_id": 100})
            seed = TicketStore(root / "tickets.sqlite3", CONFIG, initial_id=100)
            seed.close()
            history = [message(CREATED.replace("#123456", f"#{i}") if i in (201, 299, 301) else "#OtherClub\ntext",
                               message_id=i) for i in range(101, 303)]
            events = []

            async def read(source, **kwargs):
                self.assertEqual(kwargs["limit"], 100)
                self.assertTrue(kwargs["reverse"])
                events.append(("read", kwargs["min_id"]))
                return [item for item in history if item.id > kwargs["min_id"]][:100]

            async def api(token, method, payload):
                if method == "sendMessage":
                    url = payload["reply_markup"]["inline_keyboard"][0][0]["url"]
                    events.append(("send", int(url.rsplit("/", 1)[1])))
                    return SENT
                return BOT

            async def wait(stop, seconds):
                events.append(("wait", seconds))
                if seconds == 60:
                    stop.set()

            client = AsyncMock()
            client.is_user_authorized.return_value = True
            client.is_bot.return_value = False
            client.get_entity.return_value = SimpleNamespace(noforwards=False)
            client.get_messages.side_effect = read
            with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                 patch.dict("os.environ", {"POLL_INTERVAL_SECONDS": "60"}), \
                 patch("telethon.TelegramClient", return_value=client), \
                 patch("relay.signal.signal"), patch("builtins.print"), \
                 patch("relay.command_loop", side_effect=idle_commands), \
                 patch("relay.asyncio.sleep", new_callable=AsyncMock), \
                 patch("relay.bot_call", side_effect=api), \
                 patch("relay.wait_or_stop", side_effect=wait):
                await asyncio.wait_for(main(SimpleNamespace(command="run", non_interactive=True)), 2)
            self.assertEqual(events, [("read", 100), ("read", 200),
                                      ("send", 201), ("send", 299), ("read", 300),
                                      ("send", 301), ("wait", 60)])
            self.assertEqual(stored_state(root / "tickets.sqlite3")["last_id"], 302)
            client.disconnect.assert_awaited_once()

    async def test_recent_preview_send_failure_and_protection_keep_live_state(self):
        ticket_offset = len(TEXT[:TEXT.index("#123456")].encode("utf-16-le")) // 2
        ticket_url = "https://example.test/tickets/123456"
        ticket_link = MessageEntityTextUrl(ticket_offset, 7, ticket_url)
        for mode in ("preview", "send", "failure", "protected", "empty"):
            with self.subTest(mode=mode), TemporaryDirectory() as folder:
                root = Path(folder)
                config = {**CONFIG, "source_topic_id": 50, "destination_topic_id": 73}
                write_json(root / "config.json", {**config, "api_id": 1234,
                                                 "api_hash": "dummy", "bot_token": "dummy"})
                (root / "reader.session").touch()
                write_json(root / "state.json", {"route": config, "last_id": 999, "pending_id": None})
                original = (root / "state.json").read_bytes()
                reply = MessageReplyHeader(reply_to_msg_id=50, forum_topic=True)
                history = [message(message_id=110),  # Другой топик.
                           message("#OtherClub\ntext", message_id=109, reply_to=reply)]
                if mode != "empty":
                    history += [message(message_id=i, reply_to=reply, entities=[ticket_link],
                                        noforwards=(mode == "protected" and i == 103))
                                for i in (104, 103, 102, 101)]

                async def iterate(*args, **kwargs):
                    self.assertEqual(kwargs, {"limit": 1000})
                    for item in history:
                        yield item

                client = AsyncMock()
                client.is_user_authorized.return_value = True
                client.is_bot.return_value = False
                client.get_entity.return_value = SimpleNamespace(noforwards=False)
                client.iter_messages = iterate
                payloads = []

                async def api(token, method, payload):
                    if method == "sendMessage":
                        payloads.append(payload)
                        if mode == "failure":
                            raise RuntimeError("uncertain delivery")
                        return SENT
                    return {"type": "supergroup"}

                with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                     patch("telethon.TelegramClient", return_value=client), \
                     patch("relay.bot_call", side_effect=api) as bot, \
                     patch("relay.asyncio.sleep", new_callable=AsyncMock), patch("builtins.print"):
                    task = main(SimpleNamespace(command="test", non_interactive=True,
                                                count=3, send=(mode != "preview")))
                    if mode in ("failure", "protected"):
                        with self.assertRaises(RuntimeError):
                            await task
                    else:
                        await task
                self.assertEqual((root / "state.json").read_bytes(), original)
                client.disconnect.assert_awaited_once()
                receipts = list(root.glob("test-send-*.json"))
                if mode in ("preview", "protected", "empty"):
                    bot.assert_not_awaited()
                    self.assertEqual(receipts, [])
                else:
                    self.assertEqual(len(receipts), 1)
                    receipt = read_state(receipts[0], config, allow_pending=True)
                    self.assertEqual(receipt["message_ids"], [102, 103, 104])
                    self.assertEqual(receipt["pending_id"], 102 if mode == "failure" else None)
                    self.assertEqual(receipt["last_id"], 0 if mode == "failure" else 104)
                    self.assertEqual(len(payloads), 1 if mode == "failure" else 3)
                    for payload, msg_id in zip(payloads, (102, 103, 104)):
                        self.assertEqual(payload["chat_id"], config["destination_chat_id"])
                        self.assertEqual(payload["message_thread_id"], 73)
                        self.assertEqual(payload["text"], TEXT)
                        self.assertEqual(payload["entities"], [{"type": "text_link", "offset": ticket_offset,
                                                               "length": 7, "url": ticket_url}])
                        self.assertTrue(payload["reply_markup"]["inline_keyboard"][0][0]["url"].endswith(f"/{msg_id}"))
        self.joke_choice.assert_not_awaited()

    async def test_poll_interval_controls_reads_and_retries_but_not_flood_wait(self):
        for outcome, expected in (([], 60), (OSError("offline"), 60),
                                  (FloodWaitError(request=None, capture=37), 37)):
            with self.subTest(outcome=outcome), TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "reader.session").touch()
                write_json(root / "config.json", {**CONFIG, "api_id": 1234,
                                                 "api_hash": "dummy", "bot_token": "dummy"})
                client = AsyncMock()
                client.is_user_authorized.return_value = True
                client.is_bot.return_value = False
                client.get_entity.return_value = SimpleNamespace(noforwards=False)
                client.get_messages.side_effect = [[message(message_id=100)], outcome]

                async def stop_after_wait(stop, seconds):
                    stop.set()

                with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                     patch.dict("os.environ", {"POLL_INTERVAL_SECONDS": "60"}), \
                     patch("telethon.TelegramClient", return_value=client), \
                     patch("relay.LOG") as log, \
                     patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.signal.signal"), patch("builtins.print"), \
                     patch("relay.bot_call", return_value=BOT), \
                     patch("relay.wait_or_stop", side_effect=stop_after_wait) as wait:
                    await asyncio.wait_for(main(SimpleNamespace(command="run", non_interactive=True)), 2)
                wait.assert_awaited_once()
                self.assertEqual(wait.await_args.args[1], expected)
                self.assertEqual(stored_state(root / "tickets.sqlite3")["last_id"], 100)
                if isinstance(outcome, list):
                    log.info.assert_called_once()
                    self.assertEqual(log.info.call_args.args[1], 0)
                else:
                    log.info.assert_not_called()

    async def test_sigterm_finishes_in_flight_send_and_disconnects(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            local = root / ".local"
            local.mkdir()
            (local / "reader.session").touch()
            write_json(root / "config.json", {**CONFIG, "api_id": 1234,
                                             "api_hash": "dummy", "bot_token": "dummy"})
            client = AsyncMock()
            client.is_user_authorized.return_value = True
            client.is_bot.return_value = False
            client.get_entity.return_value = SimpleNamespace(noforwards=False)
            client.get_messages.side_effect = [[message(message_id=100)], [message(CREATED)]]
            handlers = {}

            async def api(token, method, payload):
                if method == "sendMessage":
                    self.assertEqual(stored_state(local / "tickets.sqlite3")["pending_id"], 101)
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                    await asyncio.sleep(0)  # Остановка пришла до подтверждения отправки.
                    return {"message_id": 1}
                return BOT

            with patch("relay.ROOT", root), patch("relay.LOCAL", local), \
                 patch("telethon.TelegramClient", return_value=client), \
                 patch("relay.signal.signal", side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                 patch("relay.command_loop", side_effect=idle_commands), \
                 patch("relay.bot_call", side_effect=api) as bot, patch("builtins.print"):
                await asyncio.wait_for(main(SimpleNamespace(command="run", non_interactive=True)), 2)
            self.assertEqual(stored_state(local / "tickets.sqlite3")["last_id"], 101)
            self.assertEqual(client.get_messages.await_count, 2)
            self.assertEqual(sum(call.args[1] == "sendMessage" for call in bot.await_args_list), 1)
            client.disconnect.assert_awaited_once()

    async def test_expired_session_fails_without_prompt(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "reader.session").touch()
            write_json(root / "config.json", {**CONFIG, "api_id": 1234,
                                             "api_hash": "dummy", "bot_token": "dummy"})
            client = AsyncMock()
            client.is_user_authorized.return_value = False
            with patch("relay.ROOT", root), patch("relay.LOCAL", root), \
                 patch("telethon.TelegramClient", return_value=client), \
                 patch("relay.signal.signal"), patch("builtins.input") as prompt:
                with self.assertRaisesRegex(ValueError, "Сессия Telegram недействительна"):
                    await main(SimpleNamespace(command="run", non_interactive=True))
            prompt.assert_not_called()
            client.start.assert_not_awaited()
            client.disconnect.assert_awaited_once()


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
    settings = {"interval": 14400, "start": 1380, "end": 540}

    def setUp(self):
        self.joke_choice = AsyncMock(return_value=None)
        joke_patch = patch("relay.JokeSource.choose", self.joke_choice)
        joke_patch.start()
        self.addCleanup(joke_patch.stop)

    async def test_first_other_reply_reacts_once_to_delivered_card_without_forwarding_replies(self):
        self.joke_choice.return_value = "Тестовый анекдот."
        ticket_offset = len(CREATED[:CREATED.index("#123456")].encode("utf-16-le")) // 2
        ticket_url = "https://example.test/tickets/123456"
        config = {**CONFIG, "destination_topic_id": 73}
        with TemporaryDirectory() as folder:
            store = TicketStore(Path(folder) / "tickets.sqlite3", config, initial_id=100, now=self.now)
            store.process(101, {"kind": "created", "ticket_id": 2, "author": "Старый Атлет",
                                "title": "Старая карточка"}, self.now)
            store.confirm_sent(101)  # Старую карточку без целевого ID не разыскиваем.
            stop = asyncio.Event()
            reads = 0
            calls = []

            async def read(*args, **kwargs):
                nonlocal reads
                reads += 1
                if reads == 1:
                    return [message(CREATED, message_id=102,
                                    entities=[MessageEntityTextUrl(ticket_offset, 7, ticket_url)]),
                            message(TEXT, message_id=103)]
                if reads == 2:
                    self.assertEqual(store.pending_reactions(self.now), [])
                    self.assertEqual(len(store.pending_tickets()), 2)
                    other = TEXT.replace("Тестовый Атлет", "Другой Человек")
                    return [message(other, message_id=104), message(other, message_id=105),
                            message(other.replace("#123456", "#2"), message_id=106)]
                await stop.wait()
                return []

            async def api(token, method, payload, **kwargs):
                calls.append((method, payload))
                if method == "sendMessage":
                    return SENT
                self.assertEqual(method, "setMessageReaction")
                self.assertEqual(payload, {"chat_id": config["destination_chat_id"], "message_id": 900,
                                           "reaction": [{"type": "emoji", "emoji": "👀"}]})
                self.assertFalse(kwargs["wait_on_rate_limit"])
                stop.set()
                return True

            client = AsyncMock()
            client.get_messages.side_effect = read
            try:
                with patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.bot_call", side_effect=api), patch("relay.asyncio.sleep", side_effect=no_delay), \
                     patch("relay.wait_or_stop", side_effect=skip_poll_delay), \
                     patch("relay.time.time", return_value=self.now), patch("builtins.print"):
                    await asyncio.wait_for(run_tracking(client, object(), config, store, stop, 60,
                                                        self.settings, "dummy", BOT, use_jokes=True), 2)
                self.assertEqual([method for method, _ in calls], ["sendMessage", "setMessageReaction"])
                self.assertEqual(calls[0][1]["text"], CREATED + "\n\n────────────\n😄 Анекдот\n"
                                 "Тестовый анекдот.\n\nИсточник: https://www.anekdot.ru/")
                self.assertEqual(calls[0][1]["message_thread_id"], 73)
                self.assertEqual(calls[0][1]["entities"], [{"type": "text_link", "offset": ticket_offset,
                                                          "length": 7, "url": ticket_url}])
                self.assertTrue(calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/102"))
                self.joke_choice.assert_awaited_once()
                self.assertEqual(store.state["last_id"], 106)
                self.assertEqual(store.pending_tickets(), [])
                self.assertEqual(store.pending_reactions(self.now + 86400), [])
            finally:
                store.close()

    async def test_pending_reaction_survives_restart_and_sends_while_paused_at_night(self):
        night = datetime(2026, 10, 15, 21, tzinfo=timezone.utc).timestamp()
        self.assertTrue(quiet_hours(night, self.settings))
        with TemporaryDirectory() as folder:
            path = Path(folder) / "tickets.sqlite3"
            store = TicketStore(path, CONFIG, initial_id=100, now=night)
            store.process(101, {"kind": "created", "ticket_id": 1, "author": "Атлет", "title": "Вопрос"}, night)
            store.confirm_sent(101, 900)
            store.process(102, {"kind": "reply", "ticket_id": 1, "author": "Другой"}, night)
            store.apply_command(True, 1)
            store.close()
            store = TicketStore(path, CONFIG)
            stop = asyncio.Event()

            async def read(*args, **kwargs):
                await stop.wait()
                return []

            async def api(token, method, payload, **kwargs):
                self.assertEqual(method, "setMessageReaction")
                self.assertEqual(payload["message_id"], 900)
                self.assertTrue(store.state["paused"])
                stop.set()
                return True

            client = AsyncMock()
            client.get_messages.side_effect = read
            try:
                self.assertEqual(store.pending_reactions(night), [{"ticket_id": 1, "destination_message_id": 900}])
                with patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.bot_call", side_effect=api) as calls, \
                     patch("relay.asyncio.sleep", side_effect=no_delay), \
                     patch("relay.time.time", return_value=night), patch("builtins.print"):
                    await asyncio.wait_for(run_tracking(client, object(), CONFIG, store, stop, 60,
                                                        self.settings, "dummy", BOT), 2)
                calls.assert_awaited_once()
                self.assertEqual(store.pending_reactions(night), [])
            finally:
                store.close()
            store = TicketStore(path, CONFIG)
            try:
                self.assertEqual(store.pending_reactions(night), [])
            finally:
                store.close()

    async def test_failed_reaction_defers_durably_without_blocking_reader_or_commands(self):
        for error, delay in ((RuntimeError("reaction failed"), 300), (BotRateLimitError(600), 600)):
            with self.subTest(error=error), TemporaryDirectory() as folder:
                path = Path(folder) / "tickets.sqlite3"
                store = TicketStore(path, CONFIG, initial_id=100, now=self.now - 86400)
                store.process(101, {"kind": "created", "ticket_id": 1, "author": "Атлет", "title": "Вопрос"}, self.now - 86400)
                store.confirm_sent(101, 900)
                store.process(102, {"kind": "reply", "ticket_id": 1, "author": "Другой"}, self.now)
                failed, commanded, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
                reads = 0

                async def read(*args, **kwargs):
                    nonlocal reads
                    reads += 1
                    await failed.wait()
                    if reads == 1:
                        return [message("#OtherClub\ntext", message_id=103)]
                    await commanded.wait()
                    stop.set()
                    return []

                async def commands(*args):
                    await failed.wait()
                    store.apply_command(True, 1)
                    commanded.set()
                    await stop.wait()

                async def api(token, method, payload, **kwargs):
                    self.assertEqual(method, "setMessageReaction")
                    self.assertFalse(kwargs["wait_on_rate_limit"])
                    failed.set()
                    raise error

                client = AsyncMock()
                client.get_messages.side_effect = read
                try:
                    with patch("relay.command_loop", side_effect=commands), \
                         patch("relay.bot_call", side_effect=api) as calls, \
                         patch("relay.asyncio.sleep", side_effect=no_delay), \
                         patch("relay.wait_or_stop", side_effect=skip_poll_delay), \
                         patch("relay.time.time", return_value=self.now), patch("relay.LOG"), patch("builtins.print"):
                        await asyncio.wait_for(run_tracking(client, object(), CONFIG, store, stop, 60,
                                                            self.settings, "dummy", BOT), 2)
                    calls.assert_awaited_once()
                    self.assertEqual(reads, 2)
                    self.assertEqual(store.state["last_id"], 103)
                    self.assertTrue(store.state["paused"])
                    self.assertIsNone(store.state["pending_id"])
                    self.assertEqual(store.pending_tickets(), [])
                    self.assertEqual(store.reminder_plan(self.now + 86400, 14400, False)["tickets"], [])
                    self.assertEqual(store.state["send_retry_at"], self.now + delay if isinstance(error, BotRateLimitError) else 0)
                finally:
                    store.close()
                store = TicketStore(path, CONFIG)
                try:
                    self.assertEqual(store.pending_reactions(self.now + delay - 1), [])
                    self.assertEqual(store.pending_reactions(self.now + delay),
                                     [{"ticket_id": 1, "destination_message_id": 900}])
                    self.assertEqual(store.pending_tickets(), [])
                finally:
                    store.close()

    async def test_stop_cancels_in_flight_reaction_and_keeps_it_pending(self):
        with TemporaryDirectory() as folder:
            store = TicketStore(Path(folder) / "tickets.sqlite3", CONFIG, initial_id=100, now=self.now)
            store.process(101, {"kind": "created", "ticket_id": 1, "author": "Атлет", "title": "Вопрос"}, self.now)
            store.confirm_sent(101, 900)
            store.process(102, {"kind": "reply", "ticket_id": 1, "author": "Другой"}, self.now)
            entered, stop = asyncio.Event(), asyncio.Event()

            async def read(*args, **kwargs):
                await entered.wait()
                stop.set()
                return []

            async def api(token, method, payload, **kwargs):
                self.assertEqual(method, "setMessageReaction")
                entered.set()
                await asyncio.Event().wait()

            client = AsyncMock()
            client.get_messages.side_effect = read
            try:
                with patch("relay.command_loop", side_effect=idle_commands), \
                     patch("relay.bot_call", side_effect=api) as calls, \
                     patch("relay.asyncio.sleep", side_effect=no_delay), \
                     patch("relay.time.time", return_value=self.now), patch("builtins.print"):
                    await asyncio.wait_for(run_tracking(client, object(), CONFIG, store, stop, 60,
                                                        self.settings, "dummy", BOT), 2)
                calls.assert_awaited_once()
                self.assertEqual(store.pending_reactions(self.now), [{"ticket_id": 1, "destination_message_id": 900}])
                self.assertEqual(store.pending_tickets(), [])
                self.assertIsNone(store.state["pending_id"])
            finally:
                store.close()

    async def test_creation_without_valid_destination_id_keeps_uncertain_delivery_pending(self):
        for result in ({}, None, {"message_id": True}, {"message_id": "900"}, {"message_id": 0}):
            with self.subTest(result=result), TemporaryDirectory() as folder:
                path = Path(folder) / "tickets.sqlite3"
                store = TicketStore(path, CONFIG, initial_id=100, now=self.now)
                client = AsyncMock()
                client.get_messages.return_value = [message(CREATED)]
                try:
                    with patch("relay.command_loop", side_effect=idle_commands), \
                         patch("relay.bot_call", return_value=result) as calls, \
                         patch("relay.asyncio.sleep", side_effect=no_delay), \
                         patch("relay.time.time", return_value=self.now), patch("builtins.print"):
                        with self.assertRaises(RuntimeError):
                            await asyncio.wait_for(run_tracking(client, object(), CONFIG, store, asyncio.Event(),
                                                                60, self.settings, "dummy", BOT), 2)
                    calls.assert_awaited_once()
                    self.assertEqual(calls.await_args.args[1], "sendMessage")
                finally:
                    store.close()
                store = TicketStore(path, CONFIG)
                try:
                    self.assertEqual(store.state["last_id"], 100)
                    self.assertEqual(store.state["pending_id"], 101)
                    self.assertEqual(store.pending_reactions(self.now + 86400), [])
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
