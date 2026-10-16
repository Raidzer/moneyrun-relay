"""Проверки учёта и расписания на временных SQLite-файлах."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tracking import (SCHEMA_VERSION, TicketStore, URGENT_AFTER_SECONDS, URGENT_INTERVAL_SECONDS,
                      normalize_author, parse_ticket_event)


ROUTE = {"source_chat_id": -100123, "source_topic_id": None,
         "destination_chat_id": -100456, "destination_topic_id": 17,
         "club_hashtag": "#Club"}
HOUR = 60 * 60


def created(ticket_id=42, author="Атлет Иван", title="Не начислены XP"):
    return {"kind": "created", "ticket_id": ticket_id, "author": author, "title": title}


def reply(ticket_id=42, author="Забота"):
    return {"kind": "reply", "ticket_id": ticket_id, "author": author, "title": ""}


class HeaderTests(unittest.TestCase):
    def test_created_and_reply_preserve_display_name(self):
        event = parse_ticket_event("#Club\n\nПользователь Атлет Иван создал новый тикет #42 - Не начислены XP\n\nТекст")
        self.assertEqual(event, created())
        self.assertEqual(parse_ticket_event("#Club\nПользователь Забота написал в тикет #42\n\nОтвет"), reply())

    def test_case_and_whitespace_are_normalized_only_for_comparison(self):
        text = ("\n #Club \n \n ПОЛЬЗОВАТЕЛЬ  АТЛЕТ\t Иван   СОЗДАЛ НОВЫЙ ТИКЕТ #42 -\n"
                "  Не начислены XP \n\nТело")
        self.assertEqual(parse_ticket_event(text), created(author="АТЛЕТ Иван"))
        self.assertEqual(normalize_author("  АТЛЕТ\tИван  "), "атлет иван")

    def test_ticket_id_boundary_and_decimal_forms_are_preserved(self):
        for template, expected in (
            ("Пользователь Атлет Иван создал новый тикет #{} - Не начислены XP", created),
            ("Пользователь Забота написал в тикет #{}", reply),
        ):
            for number, ticket_id in ((str(2**63 - 1), 2**63 - 1), ("00042", 42),
                                      ("٤٢", 42), ("４２", 42)):
                with self.subTest(kind=expected.__name__, number=number):
                    self.assertEqual(parse_ticket_event("#Club\n" + template.format(number)),
                                     expected(ticket_id=ticket_id))

    def test_zero_and_oversized_ticket_ids_are_not_events(self):
        for template in (
            "Пользователь Атлет Иван создал новый тикет #{} - Не начислены XP",
            "Пользователь Забота написал в тикет #{}",
        ):
            for number in ("0", str(2**63), "9" * 100, "9" * 5000):
                with self.subTest(template=template, digits=len(number)):
                    self.assertIsNone(parse_ticket_event("#Club\n" + template.format(number)))

    def test_body_cannot_create_an_event(self):
        for text in (
            "#Club\n\nОбновление обращения\n\nПользователь Забота написал в тикет #42",
            "#Club\n\nВопрос\nПользователь Забота написал в тикет #42",
            "#Club\n\nТело: Пользователь Забота написал в тикет #42",
            "Вступление\n#Club\nПользователь Забота написал в тикет #42",
            "#Club extra\nПользователь Забота написал в тикет #42",
            "#Club\nПользователь Забота написал в тикет #0",
            "#Club\nПользователь Забота создал новый тикет #42 - ",
            "#Club", "", " \n ",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_ticket_event(text))

    def test_reply_body_does_not_override_header(self):
        text = ("#Club\n\nПользователь Атлет Иван написал в тикет #42\n\n"
                "Пользователь Забота создал новый тикет #99 - Поддельная тема")
        self.assertEqual(parse_ticket_event(text), reply(author="Атлет Иван"))


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "tickets.sqlite3"
        self.store = self.open(initial_id=100, now=0)

    def open(self, **kwargs):
        store = TicketStore(self.path, ROUTE, **kwargs)
        self.addCleanup(store.close)
        return store

    def create_and_confirm(self, message_id=101, ticket_id=42, when=0, destination_message_id=None, **kwargs):
        self.assertTrue(self.store.process(message_id, created(ticket_id, **kwargs), when))
        self.store.confirm_sent(message_id, destination_message_id)


class StoreTests(StoreFixture):
    def test_initial_state_and_restart(self):
        self.assertEqual(self.store.state, {
            "route": ROUTE, "last_id": 100, "pending_id": None, "paused": False,
            "command_offset": 0, "started_at": 0.0, "last_regular_at": 0.0,
            "send_retry_at": 0.0,
        })
        self.create_and_confirm(when=5)
        self.store.apply_command(True, 10)
        self.store.mark_reminder_attempt({"regular_due": True, "urgent_ids": [42]}, 9)
        self.store.close()
        restored = self.open(initial_id=999, now=999)
        self.assertEqual(restored.state["last_id"], 101)
        self.assertEqual(restored.state["started_at"], 0)
        self.assertEqual(restored.state["last_regular_at"], 9)
        self.assertTrue(restored.state["paused"])
        self.assertEqual(restored.state["command_offset"], 11)
        self.assertEqual(restored.pending_tickets(), [{
            "ticket_id": 42, "author": "Атлет Иван", "title": "Не начислены XP",
            "created_at": 5.0, "source_message_id": 101, "urgent_at": 9.0,
        }])

    def test_created_pending_and_confirmation_are_durable(self):
        self.assertTrue(self.store.process(101, created(), 5))
        self.assertEqual(self.store.state["last_id"], 100)
        self.assertEqual(self.store.state["pending_id"], 101)
        self.assertEqual(len(self.store.pending_tickets()), 1)
        restored = self.open()
        with self.assertRaisesRegex(RuntimeError, "101"):
            restored.ensure_no_pending()
        with self.assertRaises(RuntimeError):
            restored.process(102, reply(), 6)
        restored.confirm_sent(101)
        self.assertEqual(restored.state["last_id"], 101)
        self.assertIsNone(restored.state["pending_id"])
        self.assertFalse(restored.process(101, created(), 5))
        self.assertEqual(len(restored.pending_tickets()), 1)

    def test_same_author_does_not_change_age_other_author_handles_forever(self):
        self.create_and_confirm(when=5)
        self.assertFalse(self.store.process(102, reply(author="  АТЛЕТ\tИван "), 999))
        self.assertEqual(self.store.pending_tickets()[0]["created_at"], 5)
        self.assertFalse(self.store.process(103, reply(), 1000))
        self.assertEqual(self.store.pending_tickets(), [])
        self.assertFalse(self.store.process(104, created(author="Другой автор"), 1001))
        self.assertFalse(self.store.process(105, reply(author="Атлет Иван"), 1002))
        self.assertEqual(self.store.pending_tickets(), [])
        self.assertEqual(self.store.state["last_id"], 105)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT author, title, handled FROM tickets").fetchone(), ("", "", 1))

    def test_duplicate_creation_does_not_send_or_change_original_card(self):
        self.create_and_confirm(when=5)
        self.assertFalse(self.store.process(102, created(author="Новое имя", title="Новая тема"), 10))
        ticket = self.store.pending_tickets()[0]
        self.assertEqual((ticket["author"], ticket["title"], ticket["created_at"], ticket["source_message_id"]),
                         ("Атлет Иван", "Не начислены XP", 5, 101))
        self.assertEqual(self.store.state["last_id"], 102)

    def test_unknown_reply_and_nonmatching_text_only_advance_cursor(self):
        self.assertFalse(self.store.process(101, reply(999), 0))
        self.assertFalse(self.store.process(102, None, None))
        self.assertFalse(self.store.process(100, created(), 0))
        self.assertEqual(self.store.pending_tickets(), [])
        self.assertEqual(self.store.state["last_id"], 102)

    def test_resolve_sent_keeps_card_and_retry_allows_same_source(self):
        self.store.process(101, created(), 5)
        for message_id, result in ((102, "sent"), (101, "unknown")):
            with self.assertRaises(ValueError):
                self.store.resolve(message_id, result)
        self.store.resolve(101, "retry")
        self.assertEqual(self.store.state["last_id"], 100)
        self.assertIsNone(self.store.state["pending_id"])
        self.store.close()
        self.store = self.open()
        self.assertTrue(self.store.process(101, created(author="Изменённое имя"), 99))
        self.assertEqual(self.store.pending_tickets()[0]["created_at"], 5)
        self.assertEqual(self.store.pending_tickets()[0]["author"], "Атлет Иван")
        self.store.resolve(101, "sent")
        self.assertEqual(self.store.state["last_id"], 101)
        self.assertFalse(self.store.process(102, created(), 100))
        self.assertEqual(len(self.store.pending_tickets()), 1)

    def test_failure_to_write_pending_rolls_back_card(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_pending BEFORE UPDATE OF pending_id ON state
                          WHEN NEW.pending_id IS NOT NULL
                          BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.process(101, created(), 0)
        self.assertEqual(self.store.pending_tickets(), [])
        self.assertEqual(self.store.state["last_id"], 100)
        self.assertIsNone(self.store.state["pending_id"])
        self.assertEqual(self.open().pending_tickets(), [])

    def test_confirmation_failure_keeps_card_and_pending_together(self):
        self.store.process(101, created(), 0)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_confirm BEFORE UPDATE OF last_id ON state
                          BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.confirm_sent(101, 501)
        restored = self.open()
        self.assertEqual(restored.state["last_id"], 100)
        self.assertEqual(restored.state["pending_id"], 101)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT announced, destination_message_id FROM tickets").fetchone(), (0, None))

    def test_commands_are_atomic_idempotent_and_offsets_never_rewind(self):
        self.store.apply_command(True, 10)
        self.store.apply_command(False, 9)
        self.store.apply_command(False, 10)
        self.assertTrue(self.store.state["paused"])
        self.assertEqual(self.store.state["command_offset"], 11)
        self.store.advance_commands(12)
        self.store.advance_commands(5)
        self.store.apply_command(False, 11)
        self.assertTrue(self.store.state["paused"])
        self.assertEqual(self.store.state["command_offset"], 13)
        self.store.apply_command(False, 13)
        self.assertFalse(self.store.state["paused"])
        self.assertEqual(self.store.state["command_offset"], 14)

    def test_command_failure_does_not_change_pause_or_offset(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_command BEFORE UPDATE OF command_offset ON state
                          BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.apply_command(True, 10)
        self.assertFalse(self.store.state["paused"])
        self.assertEqual(self.store.state["command_offset"], 0)

    def test_invalid_event_does_not_advance_cursor(self):
        for event in (created(ticket_id=0), created(ticket_id=2**63), reply(ticket_id=2**63),
                      created(author=""), created(title=""),
                      {**reply(), "kind": "other"}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.store.process(101, event, 0)
        self.assertEqual(self.store.state["last_id"], 100)
        self.assertEqual(self.store.pending_tickets(), [])


class DatabaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "tickets.sqlite3"

    def test_missing_database_does_not_appear(self):
        with self.assertRaises(FileNotFoundError):
            TicketStore(self.path, ROUTE)
        self.assertFalse(self.path.exists())

    def test_corrupt_existing_database_is_never_recreated(self):
        for content in (b"not a database", b""):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises((sqlite3.DatabaseError, ValueError)):
                    TicketStore(self.path, ROUTE, initial_id=999, now=0)
                self.assertEqual(self.path.read_bytes(), content)

    def test_route_mismatch_and_schema_version_are_rejected(self):
        TicketStore(self.path, ROUTE, initial_id=100, now=0).close()
        with self.assertRaisesRegex(ValueError, "Маршрут"):
            TicketStore(self.path, {**ROUTE, "destination_topic_id": 99})
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT last_id FROM state").fetchone()[0], 100)
            db.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ValueError, "версия"):
            TicketStore(self.path, ROUTE)

    def test_missing_table_and_dangling_pending_are_rejected(self):
        TicketStore(self.path, ROUTE, initial_id=100, now=0).close()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("UPDATE state SET pending_id = 101")
        with self.assertRaisesRegex(ValueError, "отправка"):
            TicketStore(self.path, ROUTE)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("DROP TABLE tickets")
        with self.assertRaisesRegex(ValueError, "схема"):
            TicketStore(self.path, ROUTE)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "tickets.sqlite3"

    def create_v1(self):
        # Исходная схема v1, а не новая БД с подменённым user_version.
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    route TEXT NOT NULL,
                    last_id INTEGER NOT NULL CHECK (last_id >= 0),
                    pending_id INTEGER CHECK (pending_id > last_id),
                    paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
                    command_offset INTEGER NOT NULL CHECK (command_offset >= 0),
                    started_at REAL NOT NULL CHECK (started_at >= 0),
                    last_regular_at REAL NOT NULL CHECK (last_regular_at >= 0),
                    send_retry_at REAL NOT NULL DEFAULT 0 CHECK (send_retry_at >= 0)
                );
                CREATE TABLE tickets (
                    ticket_id INTEGER PRIMARY KEY CHECK (ticket_id > 0),
                    author TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL CHECK (created_at >= 0),
                    source_message_id INTEGER NOT NULL UNIQUE CHECK (source_message_id > 0),
                    handled INTEGER NOT NULL CHECK (handled IN (0, 1)),
                    announced INTEGER NOT NULL CHECK (announced IN (0, 1)),
                    urgent_at REAL CHECK (urgent_at >= 0)
                );
                PRAGMA user_version = 1;
            """)
            db.execute("INSERT INTO state VALUES (1, ?, 101, 102, 1, 11, 7, 9, 15)",
                       (json.dumps(ROUTE),))
            db.executemany("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                (41, "", "", 0, 100, 1, 1, None),
                (42, "Атлет Иван", "Вопрос", 1, 101, 0, 1, 8),
                (43, "Атлет Пётр", "Новый вопрос", 2, 102, 0, 0, None),
            ])
            db.commit()

    def test_v1_migration_preserves_state_cards_and_pending(self):
        self.create_v1()
        with closing(sqlite3.connect(self.path)) as db:
            before_state = db.execute("SELECT * FROM state").fetchall()
            before_tickets = db.execute("SELECT * FROM tickets ORDER BY ticket_id").fetchall()
        with closing(TicketStore(self.path, ROUTE)) as store:
            self.assertEqual(store.state["pending_id"], 102)
            self.assertEqual(store.pending_reactions(100), [])
            with self.assertRaisesRegex(RuntimeError, "102"):
                store.ensure_no_pending()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT * FROM state").fetchall(), before_state)
            self.assertEqual(db.execute("SELECT * FROM tickets ORDER BY ticket_id").fetchall(),
                             [row + (None, None) for row in before_tickets])
        with closing(TicketStore(self.path, ROUTE)) as store:
            store.resolve(102, "sent")
            self.assertEqual(store.state["last_id"], 102)
            self.assertTrue(store.state["paused"])
            self.assertEqual(store.state["send_retry_at"], 15)
            store.process(103, reply(), 20)
            store.process(104, reply(ticket_id=43), 20)
            self.assertEqual(store.pending_tickets(), [])
            self.assertEqual(store.pending_reactions(100), [])

    def test_invalid_v1_is_rejected_without_changing_file(self):
        cases = (
            ("wrong route", None, {**ROUTE, "destination_topic_id": 99}),
            ("unknown version", "PRAGMA user_version = 99", ROUTE),
            ("wrong schema", "ALTER TABLE tickets RENAME COLUMN author TO unknown", ROUTE),
            ("dangling pending", "DELETE FROM tickets WHERE ticket_id = 43", ROUTE),
            ("corrupt time", "UPDATE state SET send_retry_at = 1e999", ROUTE),
        )
        for label, mutation, route in cases:
            with self.subTest(case=label):
                if self.path.exists():
                    self.path.unlink()
                self.create_v1()
                if mutation:
                    with closing(sqlite3.connect(self.path)) as db:
                        db.execute(mutation)
                        db.commit()
                before = self.path.read_bytes()
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    TicketStore(self.path, route, initial_id=999, now=999)
                self.assertEqual(self.path.read_bytes(), before)

    def test_failed_migration_rolls_back_both_columns_and_version(self):
        self.create_v1()
        sqlite_connect = sqlite3.connect

        class FailingMigration(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE tickets ADD COLUMN reaction_due_at"):
                    raise sqlite3.DatabaseError("disk failure")
                return super().execute(sql, *args)

        def connect(*args, **kwargs):
            return sqlite_connect(*args, **kwargs, factory=FailingMigration)

        before = self.path.read_bytes()
        with patch("tracking.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "disk failure"):
                TicketStore(self.path, ROUTE)
        self.assertEqual(self.path.read_bytes(), before)
        with closing(TicketStore(self.path, ROUTE)) as store:
            self.assertEqual(store.state["pending_id"], 102)


class ReactionTests(StoreFixture):
    def test_own_reply_then_first_other_queues_once_and_survives_restart(self):
        self.create_and_confirm(when=5, destination_message_id=501)
        self.store.apply_command(True, 10)
        self.assertFalse(self.store.process(102, reply(author="  АТЛЕТ\tИван "), 99))
        self.assertEqual(self.store.pending_reactions(99), [])
        self.assertEqual(self.store.pending_tickets()[0]["created_at"], 5)
        self.assertFalse(self.store.process(103, reply(), 100))
        self.assertEqual(self.store.state["last_id"], 103)
        self.assertEqual(self.store.pending_tickets(), [])
        self.store.close()
        self.store = self.open()
        expected = [{"ticket_id": 42, "destination_message_id": 501}]
        self.assertTrue(self.store.state["paused"])
        self.assertEqual(self.store.pending_reactions(0), expected)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT author, title, handled, reaction_due_at FROM tickets").fetchone(),
                             ("", "", 1, 0))
        self.store.defer_reaction(42, 200)
        self.assertFalse(self.store.process(103, reply(), 100))
        self.assertFalse(self.store.process(104, reply(author="Другой человек"), 101))
        self.assertFalse(self.store.process(105, created(), 101))
        self.assertEqual(self.store.pending_reactions(199), [])
        self.assertEqual(self.store.pending_reactions(200), expected)
        self.store.confirm_reaction(42)
        self.store.confirm_reaction(42)
        self.assertFalse(self.store.process(106, reply(), 201))
        self.store.defer_reaction(42, 300)
        self.assertEqual(self.open().pending_reactions(999), [])

    def test_no_destination_id_or_manual_resolve_does_not_queue(self):
        self.create_and_confirm()
        self.store.process(102, reply(), 1)
        self.store.process(103, created(ticket_id=43), 2)
        self.store.resolve(103, "sent")
        self.store.process(104, reply(ticket_id=43), 3)
        self.assertEqual(self.store.pending_tickets(), [])
        self.assertEqual(self.store.pending_reactions(999), [])

    def test_handling_and_reaction_queue_rollback_with_cursor(self):
        self.create_and_confirm(destination_message_id=501)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_reply BEFORE UPDATE OF last_id ON state
                          BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.process(102, reply(), 1)
        self.assertEqual(self.store.state["last_id"], 101)
        self.assertEqual(self.store.pending_reactions(999), [])
        self.assertEqual(self.store.pending_tickets()[0]["author"], "Атлет Иван")
        self.assertEqual(len(self.open().pending_tickets()), 1)

    def test_retry_and_global_rate_limit_are_durable_and_never_shorten_barrier(self):
        self.create_and_confirm(destination_message_id=501)
        self.create_and_confirm(message_id=102, ticket_id=43, destination_message_id=502)
        self.store.process(103, reply(), 1)
        self.store.process(104, reply(ticket_id=43), 1)
        self.store.defer_reaction(42, 300)
        self.assertEqual(self.store.state["send_retry_at"], 0)
        self.assertEqual(self.store.pending_reactions(299), [{"ticket_id": 43, "destination_message_id": 502}])
        self.store.defer_reaction(43, 400, rate_limited=True)
        self.store.defer_reaction(42, 350, rate_limited=True)
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.state["send_retry_at"], 400)
        self.assertEqual(self.store.pending_reactions(399), [])
        self.assertEqual(self.store.pending_reactions(400), [
            {"ticket_id": 42, "destination_message_id": 501},
            {"ticket_id": 43, "destination_message_id": 502},
        ])
        self.assertEqual(self.store.reminder_plan(399, 1, False)["tickets"], [])

    def test_rate_limit_defer_is_atomic_with_global_barrier(self):
        self.create_and_confirm(destination_message_id=501)
        self.store.process(102, reply(), 1)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_reaction_barrier BEFORE UPDATE OF send_retry_at ON state
                          BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.defer_reaction(42, 400, rate_limited=True)
        self.assertEqual(self.store.state["send_retry_at"], 0)
        self.assertEqual(self.open().pending_reactions(0), [{"ticket_id": 42, "destination_message_id": 501}])

    def test_invalid_reaction_inputs_do_not_change_state(self):
        self.store.process(101, created(), 0)
        for value in (0, -1, True, 1.5, "501", 2**63):
            with self.subTest(destination_message_id=value), self.assertRaises(ValueError):
                self.store.confirm_sent(101, value)
            with self.subTest(ticket_id=value), self.assertRaises(ValueError):
                self.store.confirm_reaction(value)
            with self.subTest(deferred_ticket_id=value), self.assertRaises(ValueError):
                self.store.defer_reaction(value, 100)
        self.assertEqual(self.store.state["pending_id"], 101)
        self.store.confirm_sent(101, 501)
        self.store.process(102, reply(), 1)
        for value in (-1, True, "10", float("nan"), float("inf")):
            with self.subTest(now=value), self.assertRaises(ValueError):
                self.store.pending_reactions(value)
            with self.subTest(retry_at=value), self.assertRaises(ValueError):
                self.store.defer_reaction(42, value)
        with self.assertRaises(ValueError):
            self.store.defer_reaction(42, 100, rate_limited=1)
        self.assertEqual(self.store.pending_reactions(0), [{"ticket_id": 42, "destination_message_id": 501}])

    def test_illegal_reaction_rows_are_rejected(self):
        self.create_and_confirm(destination_message_id=501)
        self.store.close()
        for mutation in (
            "destination_message_id = 0",
            "destination_message_id = 1.5",
            "reaction_due_at = 0",
            "handled = 1, destination_message_id = NULL, reaction_due_at = 0",
            "handled = 1, reaction_due_at = -1",
            "handled = 1, reaction_due_at = 1e999",
        ):
            with self.subTest(mutation=mutation):
                with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
                    db.execute("PRAGMA ignore_check_constraints = ON")
                    db.execute("UPDATE tickets SET handled = 0, destination_message_id = 501, reaction_due_at = NULL")
                    db.execute("UPDATE tickets SET " + mutation)
                before = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.open()
                self.assertEqual(self.path.read_bytes(), before)


class ReminderTests(StoreFixture):
    def plan(self, now, quiet=False, interval=4 * HOUR):
        return self.store.reminder_plan(now, interval, quiet)

    def test_interval_starts_at_initialization_and_empty_attempt_advances_it(self):
        self.assertFalse(self.plan(4 * HOUR - 1)["regular_due"])
        plan = self.plan(4 * HOUR)
        self.assertEqual(plan, {"regular_due": True, "tickets": [], "urgent_ids": []})
        self.store.mark_reminder_attempt(plan, 4 * HOUR)
        self.assertFalse(self.plan(8 * HOUR - 1)["regular_due"])
        self.assertTrue(self.plan(8 * HOUR)["regular_due"])
        self.assertTrue(self.plan(5 * HOUR, interval=HOUR)["regular_due"])

    def test_twenty_hours_and_thirty_minute_boundaries(self):
        self.create_and_confirm()
        self.assertEqual(self.plan(URGENT_AFTER_SECONDS, quiet=True)["tickets"], [])
        due = URGENT_AFTER_SECONDS + 1
        plan = self.plan(due, quiet=True)
        self.assertFalse(plan["regular_due"])
        self.assertEqual(plan["urgent_ids"], [42])
        self.store.mark_reminder_attempt(plan, due)
        self.assertEqual(self.plan(due + URGENT_INTERVAL_SECONDS - 1, quiet=True)["tickets"], [])
        self.assertEqual(self.plan(due + URGENT_INTERVAL_SECONDS, quiet=True)["urgent_ids"], [42])

    def test_regular_urgent_union_and_independent_urgent_timer(self):
        self.create_and_confirm(ticket_id=42)
        self.create_and_confirm(message_id=102, ticket_id=43, when=20 * HOUR)
        now = 20 * HOUR + 1
        plan = self.plan(now)
        self.assertTrue(plan["regular_due"])
        self.assertEqual([row["ticket_id"] for row in plan["tickets"]], [42, 43])
        self.assertEqual(plan["urgent_ids"], [42])
        self.store.mark_reminder_attempt(plan, now)
        later = now + 60
        plan = self.plan(later, interval=60)
        self.assertTrue(plan["regular_due"])
        self.assertEqual(plan["urgent_ids"], [])
        self.store.mark_reminder_attempt(plan, later)
        tickets = self.store.pending_tickets()
        self.assertEqual(tickets[0]["urgent_at"], now)
        self.assertIsNone(tickets[1]["urgent_at"])
        self.assertEqual(self.plan(now + 30 * 60, quiet=True)["urgent_ids"], [42])

    def test_quiet_and_pause_keep_overdue_regular_deadline(self):
        self.create_and_confirm()
        self.assertFalse(self.plan(5 * HOUR, quiet=True)["regular_due"])
        self.assertEqual(self.store.state["last_regular_at"], 0)
        self.assertTrue(self.plan(6 * HOUR)["regular_due"])
        self.store.apply_command(True, 1)
        self.assertEqual(self.plan(22 * HOUR), {"regular_due": False, "tickets": [], "urgent_ids": []})
        self.assertFalse(self.store.process(102, reply(author="Атлет Иван"), 22 * HOUR))
        self.create_and_confirm(message_id=103, ticket_id=43, when=22 * HOUR)
        self.store.apply_command(False, 2)
        plan = self.plan(23 * HOUR, quiet=True)
        self.assertFalse(plan["regular_due"])
        self.assertEqual(plan["urgent_ids"], [42])
        self.assertEqual(self.store.state["last_regular_at"], 0)
        self.assertTrue(self.plan(24 * HOUR)["regular_due"])

    def test_restart_emits_one_current_plan_and_reply_removes_ticket(self):
        self.create_and_confirm()
        self.store.close()
        self.store = self.open()
        plan = self.plan(80 * HOUR)
        self.assertEqual(len(plan["tickets"]), 1)
        self.assertTrue(plan["regular_due"])
        self.store.mark_reminder_attempt(plan, 80 * HOUR)
        self.assertEqual(self.plan(80 * HOUR)["tickets"], [])
        self.store.process(102, reply(), 81 * HOUR)
        self.assertEqual(self.plan(81 * HOUR, quiet=True)["tickets"], [])

    def test_confirmed_rate_limit_restores_deadlines_and_refreshes_after_restart(self):
        self.create_and_confirm()
        self.create_and_confirm(message_id=102, ticket_id=43)
        now = 21 * HOUR
        plan = self.plan(now)
        previous_regular_at = self.store.state["last_regular_at"]
        self.store.mark_reminder_attempt(plan, now)
        self.store.defer_reminder(plan, now + 60, previous_regular_at)
        self.assertEqual(self.store.state["last_regular_at"], previous_regular_at)
        self.assertEqual([ticket["urgent_at"] for ticket in self.store.pending_tickets()], [None, None])
        self.assertEqual(self.plan(now + 59), {"regular_due": False, "tickets": [], "urgent_ids": []})
        self.assertFalse(self.store.process(103, reply(), now + 10))
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.state["send_retry_at"], now + 60)
        self.assertEqual(self.plan(now + 59)["tickets"], [])
        fresh = self.plan(now + 60)
        self.assertTrue(fresh["regular_due"])
        self.assertEqual([ticket["ticket_id"] for ticket in fresh["tickets"]], [43])
        self.assertEqual(fresh["urgent_ids"], [43])

    def test_rate_limit_restores_previous_urgent_time_and_never_shortens_barrier(self):
        self.create_and_confirm()
        first = 21 * HOUR
        self.store.mark_reminder_attempt(self.plan(first, quiet=True), first)
        plan = self.plan(first + 30 * 60, quiet=True)
        self.store.mark_reminder_attempt(plan, first + 30 * 60)
        self.store.defer_reminder(plan, first + HOUR, 0)
        self.store.defer_reminder(plan, first + 45 * 60, 0)
        self.assertEqual(self.store.state["send_retry_at"], first + HOUR)
        self.assertEqual(self.store.state["last_regular_at"], 0)
        self.assertEqual(self.store.pending_tickets()[0]["urgent_at"], first)

    def test_rate_limit_defer_failure_keeps_attempt_and_barrier_atomic(self):
        self.create_and_confirm()
        now = 21 * HOUR
        plan = self.plan(now)
        self.store.mark_reminder_attempt(plan, now)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_defer BEFORE UPDATE OF urgent_at ON tickets
                          BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.defer_reminder(plan, now + 60, 0)
        self.assertEqual(self.store.state["send_retry_at"], 0)
        self.assertEqual(self.store.state["last_regular_at"], now)
        self.assertEqual(self.store.pending_tickets()[0]["urgent_at"], now)


if __name__ == "__main__":
    unittest.main()
