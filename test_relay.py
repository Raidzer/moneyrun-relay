"""Проверки без Telegram, токенов и отправки сообщений."""

import asyncio
import json
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.tl.types import Message, MessageReplyHeader, PeerChannel
from telethon.errors import FloodWaitError
from relay import (check_unattended_config, main, message_payload, process_message,
                   poll_interval_seconds, read_state, resolve_pending, selected,
                   validate_config, write_json)


CONFIG = {
    "source_chat_id": -1001234567890,
    "source_topic_id": None,
    "destination_chat_id": -1009876543210,
    "destination_topic_id": None,
    "club_hashtag": "#KhromushkinTeam",
}
TEXT = ("#KhromushkinTeam\n\nПользователь Тестовый Атлет написал в тикет #123456\n\n"
        "Хорошо, если ещё возникнут вопросы, мы всегда на связи!\n\nХороших тренировок 🙂")


def message(text=TEXT, message_id=101, **fields):
    return Message(id=message_id, peer_id=PeerChannel(1234567890), message=text, **fields)


class RelayTests(unittest.TestCase):
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
        self.assertEqual(payload["message_thread_id"], 73)
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://t.me/c/1234567890/101")

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
                     patch("relay.signal.signal"), patch("builtins.print"), \
                     patch("relay.bot_call", return_value={"type": "group"}), \
                     patch("relay.wait_or_stop", side_effect=stop_after_wait) as wait:
                    await asyncio.wait_for(main(SimpleNamespace(command="run", non_interactive=True)), 2)
                wait.assert_awaited_once()
                self.assertEqual(wait.await_args.args[1], expected)
                self.assertEqual(read_state(root / "state.json", CONFIG)["last_id"], 100)

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
            client.get_messages.side_effect = [[message(message_id=100)], [message()]]
            handlers = {}

            async def api(token, method, payload):
                if method == "sendMessage":
                    self.assertEqual(read_state(local / "state.json", CONFIG,
                                                allow_pending=True)["pending_id"], 101)
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                    await asyncio.sleep(0)  # Остановка пришла до подтверждения отправки.
                    return {"message_id": 1}
                return {"type": "group"}

            with patch("relay.ROOT", root), patch("relay.LOCAL", local), \
                 patch("telethon.TelegramClient", return_value=client), \
                 patch("relay.signal.signal", side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                 patch("relay.bot_call", side_effect=api) as bot, patch("builtins.print"):
                await asyncio.wait_for(main(SimpleNamespace(command="run", non_interactive=True)), 2)
            self.assertEqual(read_state(local / "state.json", CONFIG)["last_id"], 101)
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


if __name__ == "__main__":
    unittest.main()
