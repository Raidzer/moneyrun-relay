"""Проверки учёта и расписания на временных SQLite-файлах."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tracking import (TicketStore, URGENT_AFTER_SECONDS, URGENT_INTERVAL_SECONDS,
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

    def create_and_confirm(self, message_id=101, ticket_id=42, when=0, **kwargs):
        self.assertTrue(self.store.process(message_id, created(ticket_id, **kwargs), when))
        self.store.confirm_sent(message_id)


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
            self.store.confirm_sent(101)
        restored = self.open()
        self.assertEqual(restored.state["last_id"], 100)
        self.assertEqual(restored.state["pending_id"], 101)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT announced FROM tickets").fetchone()[0], 0)

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
        for event in (created(ticket_id=0), created(author=""), created(title=""),
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
