"""Проверки конфигурации окружения без секретов и подключений к Telegram."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import telethon

import relay
from tracking import TicketStore


ROUTE = {
    "source_chat_id": -1001234567890,
    "source_topic_id": None,
    "destination_chat_id": -1009876543210,
    "destination_topic_id": None,
    "club_hashtag": "#TestClub",
}
CONFIG = {**ROUTE, "api_id": 1234, "api_hash": "dummy-hash", "bot_token": "dummy-token"}
ENV = {key.upper(): "" if value is None else str(value) for key, value in CONFIG.items()}
EVENT = {"kind": "created", "ticket_id": 42, "author": "Тестовый Атлет", "title": "Вопрос"}


class ConfigFixture:
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.local = self.root / ".local"
        self.local.mkdir()
        for setting in (patch("relay.ROOT", self.root), patch("relay.LOCAL", self.local),
                        patch.dict("os.environ", {}, clear=True)):
            setting.start()
            self.addCleanup(setting.stop)


class ConfigTests(ConfigFixture, unittest.TestCase):
    def test_environment_only_does_not_require_config_file(self):
        with patch.dict("os.environ", ENV):
            self.assertEqual(relay.load_config(), CONFIG)
        self.assertFalse((self.root / "config.json").exists())

    def test_legacy_json_without_environment_is_unchanged_including_bom(self):
        legacy = {**CONFIG, "api_id": "1234", "extra": {"keep": True}}
        (self.root / "config.json").write_text(json.dumps(legacy), encoding="utf-8-sig")
        self.assertEqual(relay.load_config(), legacy)

    def test_environment_overrides_all_fields_with_exact_route_types(self):
        relay.write_json(self.root / "config.json", {
            "source_chat_id": -1, "source_topic_id": 50,
            "destination_chat_id": -2, "destination_topic_id": 73,
            "club_hashtag": "#OldClub", "api_id": 9,
            "api_hash": "old-hash", "bot_token": "old-token",
        })
        with patch.dict("os.environ", {key: " " + value + " " for key, value in ENV.items()}):
            config = relay.load_config()
        self.assertEqual(config, CONFIG)
        self.assertEqual({key: config.get(key) for key in relay.ROUTE_KEYS}, ROUTE)
        relay.validate_config(config, sending=True)

    def test_unset_environment_fields_fall_back_to_json(self):
        relay.write_json(self.root / "config.json", CONFIG)
        with patch.dict("os.environ", {"BOT_TOKEN": " new-token "}):
            self.assertEqual(relay.load_config(), {**CONFIG, "bot_token": "new-token"})

    def test_optional_topics_can_clear_json_values(self):
        relay.write_json(self.root / "config.json", {
            **CONFIG, "source_topic_id": 50, "destination_topic_id": 73,
        })
        with patch.dict("os.environ", {"SOURCE_TOPIC_ID": "", "DESTINATION_TOPIC_ID": " \t"}):
            self.assertEqual(relay.load_config(), CONFIG)

    def test_empty_credentials_never_fall_back_to_json(self):
        relay.write_json(self.root / "config.json", CONFIG)
        with patch.dict("os.environ", {"API_ID": " ", "API_HASH": "", "BOT_TOKEN": " \t"}):
            config = relay.load_config()
        self.assertIsNone(config["api_id"])
        self.assertFalse(config["api_hash"])
        self.assertFalse(config["bot_token"])
        with self.assertRaises(ValueError):
            relay.check_unattended_config(config, "run")

    def test_empty_numeric_values_become_none(self):
        for key in ("API_ID", "SOURCE_CHAT_ID", "DESTINATION_CHAT_ID",
                    "SOURCE_TOPIC_ID", "DESTINATION_TOPIC_ID"):
            with self.subTest(key=key), patch.dict("os.environ", {key: " "}):
                self.assertIsNone(relay.load_config()[key.lower()])

    def test_invalid_numbers_name_the_field_without_echoing_the_value(self):
        secret = "invalid-sensitive-number"
        for key in ("API_ID", "SOURCE_CHAT_ID", "DESTINATION_CHAT_ID",
                    "SOURCE_TOPIC_ID", "DESTINATION_TOPIC_ID"):
            with self.subTest(key=key), patch.dict("os.environ", {key: secret}):
                with self.assertRaisesRegex(ValueError, key) as caught:
                    relay.load_config()
                self.assertNotIn(secret, str(caught.exception))
        for value in ("0", "-123", "1.5"):
            with self.subTest(api_id=value), patch.dict("os.environ", {"API_ID": value}):
                with self.assertRaisesRegex(ValueError, "API_ID"):
                    relay.load_config()

    def test_environment_keeps_existing_route_validation(self):
        for update in ({"SOURCE_CHAT_ID": "1"}, {"DESTINATION_CHAT_ID": "0"},
                       {"SOURCE_TOPIC_ID": "-1"}, {"DESTINATION_TOPIC_ID": "0"},
                       {"DESTINATION_CHAT_ID": ENV["SOURCE_CHAT_ID"]},
                       {"CLUB_HASHTAG": "invalid"}):
            with self.subTest(update=update), patch.dict("os.environ", {**ENV, **update}):
                with self.assertRaises(ValueError):
                    relay.validate_config(relay.load_config(), sending=True)

    def test_invalid_json_and_non_object_roots_have_safe_errors(self):
        secret = "sensitive-json-content"
        for content in ("[]", "null", "42", json.dumps(secret), '{"bot_token": "' + secret):
            with self.subTest(content=content), patch.dict("os.environ", ENV):
                (self.root / "config.json").write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError) as caught:
                    relay.load_config()
                self.assertNotIn(secret, str(caught.exception))


class ConfigLifecycleTests(ConfigFixture, unittest.IsolatedAsyncioTestCase):
    async def test_missing_unattended_values_fail_before_client_or_prompts(self):
        (self.local / "reader.session").touch()
        with patch.object(telethon, "TelegramClient") as factory, patch("builtins.input") as prompt, \
             patch("relay.getpass") as password:
            for key in ("API_ID", "API_HASH", "BOT_TOKEN", "SOURCE_CHAT_ID",
                        "DESTINATION_CHAT_ID", "CLUB_HASHTAG"):
                for value in (None, ""):
                    env = {**ENV, key: value}
                    if value is None:
                        del env[key]
                    with self.subTest(key=key, value=value), patch.dict("os.environ", env):
                        with self.assertRaises(ValueError):
                            await relay.main(SimpleNamespace(command="run", non_interactive=True))
            factory.assert_not_called()
            prompt.assert_not_called()
            password.assert_not_called()

    async def test_environment_run_requires_existing_session_before_client(self):
        with patch.dict("os.environ", ENV), patch.object(telethon, "TelegramClient") as factory, \
             patch("builtins.input") as prompt:
            with self.assertRaisesRegex(ValueError, "reader.session"):
                await relay.main(SimpleNamespace(command="run", non_interactive=True))
        factory.assert_not_called()
        prompt.assert_not_called()

    async def test_environment_run_resumes_existing_database_without_bootstrap(self):
        route = {**ROUTE, "source_topic_id": 50, "destination_topic_id": 73}
        config = {**CONFIG, **route}
        env = {**ENV, "SOURCE_TOPIC_ID": "50", "DESTINATION_TOPIC_ID": "73"}
        path = self.local / "tickets.sqlite3"
        store = TicketStore(path, route, initial_id=100, now=0)
        store.process(101, EVENT, 5)
        store.confirm_sent(101, 900)
        store.apply_command(True, 10)
        state, tickets = store.state, store.pending_tickets()
        store.close()
        (self.local / "reader.session").touch()
        (self.local / "tracking.initialized").write_text("1\n", encoding="ascii")
        client = AsyncMock()
        client.is_user_authorized.return_value = True
        client.is_bot.return_value = False
        client.get_entity.return_value = SimpleNamespace(noforwards=False)
        bot = {"type": "supergroup", "id": 1, "username": "test_bot", "status": "administrator"}
        with patch.dict("os.environ", env), \
             patch.object(telethon, "TelegramClient", return_value=client) as factory, \
             patch("relay.bot_call", new_callable=AsyncMock, return_value=bot), \
             patch("relay.run_tracking", new_callable=AsyncMock) as worker, \
             patch("relay.signal.signal"), patch("builtins.print"):
            await relay.main(SimpleNamespace(command="run", non_interactive=True))
            factory.assert_called_once_with(str(self.local / "reader"), 1234, "dummy-hash",
                                            receive_updates=False)
            worker.assert_awaited_once()
            self.assertEqual(worker.await_args.args[2], config)
        client.connect.assert_awaited_once()
        client.start.assert_not_awaited()
        client.get_messages.assert_not_awaited()
        client.disconnect.assert_awaited_once()
        reopened = TicketStore(path, route)
        try:
            self.assertEqual(reopened.state, state)
            self.assertEqual(reopened.pending_tickets(), tickets)
        finally:
            reopened.close()
        self.assertTrue((self.local / "tracking.initialized").is_file())
        self.assertFalse((self.root / "config.json").exists())

    async def test_missing_database_marker_still_blocks_environment_run(self):
        (self.local / "tracking.initialized").touch()
        with patch.dict("os.environ", ENV), patch.object(telethon, "TelegramClient") as factory:
            with self.assertRaisesRegex(ValueError, "Пропала база"):
                await relay.main(SimpleNamespace(command="run", non_interactive=True))
        factory.assert_not_called()
        self.assertFalse((self.local / "tickets.sqlite3").exists())

    async def test_environment_route_mismatch_keeps_database_and_never_connects(self):
        path = self.local / "tickets.sqlite3"
        TicketStore(path, ROUTE, initial_id=100, now=0).close()
        original = path.read_bytes()
        (self.local / "reader.session").touch()
        client = AsyncMock()
        with patch.dict("os.environ", {**ENV, "DESTINATION_TOPIC_ID": "73"}), \
             patch.object(telethon, "TelegramClient", return_value=client), patch("relay.signal.signal"):
            with self.assertRaisesRegex(ValueError, "Маршрут"):
                await relay.main(SimpleNamespace(command="run", non_interactive=True))
        client.connect.assert_not_awaited()
        self.assertEqual(path.read_bytes(), original)

    async def test_resolve_with_route_only_needs_no_credentials_session_or_telegram(self):
        route_env = {key.upper(): "" if value is None else str(value) for key, value in ROUTE.items()}
        for storage in ("sqlite", "legacy"):
            for result in ("sent", "retry"):
                local = self.local / (storage + "-" + result)
                local.mkdir()
                path = local / ("tickets.sqlite3" if storage == "sqlite" else "state.json")
                if storage == "sqlite":
                    store = TicketStore(path, ROUTE, initial_id=100, now=0)
                    store.process(101, EVENT, 5)
                    store.close()
                else:
                    relay.write_json(path, {"route": ROUTE, "last_id": 100, "pending_id": 101})
                with self.subTest(storage=storage, result=result), patch("relay.LOCAL", local), \
                     patch.dict("os.environ", route_env), patch.object(telethon, "TelegramClient") as factory, \
                     patch("relay.bot_call", new_callable=AsyncMock) as bot, \
                     patch("builtins.input") as prompt, patch("relay.getpass") as password, \
                     patch("builtins.print"):
                    await relay.main(SimpleNamespace(command="resolve", message_id=101,
                                                     result=result, non_interactive=True))
                factory.assert_not_called()
                bot.assert_not_awaited()
                prompt.assert_not_called()
                password.assert_not_called()
                if storage == "sqlite":
                    store = TicketStore(path, ROUTE)
                    try:
                        state = store.state
                        self.assertEqual(len(store.pending_tickets()), 1)
                    finally:
                        store.close()
                else:
                    state = relay.read_state(path, ROUTE)
                self.assertIsNone(state["pending_id"])
                self.assertEqual(state["last_id"], 101 if result == "sent" else 100)


if __name__ == "__main__":
    unittest.main()
