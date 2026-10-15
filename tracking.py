"""Локальный учёт первого ответа без обращений к Telegram."""

import json
import math
from pathlib import Path
import re
import sqlite3
import time


URGENT_AFTER_SECONDS = 20 * 60 * 60
URGENT_INTERVAL_SECONDS = 30 * 60
SCHEMA_VERSION = 1
STATE_COLUMNS = ("id", "route", "last_id", "pending_id", "paused", "command_offset",
                 "started_at", "last_regular_at", "send_retry_at")
TICKET_COLUMNS = ("ticket_id", "author", "title", "created_at", "source_message_id",
                  "handled", "announced", "urgent_at")


def normalize_author(author):
    return " ".join(author.split()).casefold()


def parse_ticket_event(text):
    lines = text.strip().splitlines()
    if not lines:
        return None
    tag = lines.pop(0).strip()
    if not tag.startswith("#") or not tag[1:].isidentifier():
        return None
    while lines and not lines[0].strip():
        lines.pop(0)
    header = []
    for line in lines:
        if not line.strip():
            break
        header.append(line.strip())
    header = " ".join(" ".join(header).split())
    for kind, pattern in (
        ("created", r"Пользователь (.+?) создал новый тикет #(\d+) - (.+)"),
        ("reply", r"Пользователь (.+?) написал в тикет #(\d+)"),
    ):
        match = re.fullmatch(pattern, header, flags=re.IGNORECASE)
        if match and int(match[2]) > 0:
            return {"kind": kind, "ticket_id": int(match[2]), "author": match[1],
                    "title": match[3] if kind == "created" else ""}
    return None


def _nonnegative_time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Некорректное время учёта тикетов.")
    return float(value)


def _message_id(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError("Некорректный ID сообщения.")


class TicketStore:
    def __init__(self, path, route, initial_id=None, now=None):
        path = Path(path)
        if not isinstance(route, dict):
            raise ValueError("Некорректный маршрут учёта тикетов.")
        route_json = json.dumps(route, ensure_ascii=False, sort_keys=True)
        if not path.exists():
            if initial_id is None:
                raise FileNotFoundError("База учёта тикетов отсутствует; автоматический сброс отменён.")
            _message_id(initial_id, minimum=0)
            started_at = _nonnegative_time(time.time() if now is None else now)
            # Эксклюзивное создание не перезаписывает БД, появившуюся параллельно.
            with path.open("xb"):
                pass
            new = True
        else:
            new = False
        self._db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA synchronous = FULL")
            if new:
                with self._db:
                    self._db.execute("""CREATE TABLE state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        route TEXT NOT NULL,
                        last_id INTEGER NOT NULL CHECK (last_id >= 0),
                        pending_id INTEGER CHECK (pending_id > last_id),
                        paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
                        command_offset INTEGER NOT NULL CHECK (command_offset >= 0),
                        started_at REAL NOT NULL CHECK (started_at >= 0),
                        last_regular_at REAL NOT NULL CHECK (last_regular_at >= 0),
                        send_retry_at REAL NOT NULL DEFAULT 0 CHECK (send_retry_at >= 0)
                    )""")
                    self._db.execute("""CREATE TABLE tickets (
                        ticket_id INTEGER PRIMARY KEY CHECK (ticket_id > 0),
                        author TEXT NOT NULL,
                        title TEXT NOT NULL,
                        created_at REAL NOT NULL CHECK (created_at >= 0),
                        source_message_id INTEGER NOT NULL UNIQUE CHECK (source_message_id > 0),
                        handled INTEGER NOT NULL CHECK (handled IN (0, 1)),
                        announced INTEGER NOT NULL CHECK (announced IN (0, 1)),
                        urgent_at REAL CHECK (urgent_at >= 0)
                    )""")
                    self._db.execute("INSERT INTO state VALUES (1, ?, ?, NULL, 0, 0, ?, ?, 0)",
                                     (route_json, initial_id, started_at, started_at))
                    self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._validate(route)
        except Exception:
            self._db.close()
            raise

    def _validate(self, route):
        if self._db.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise ValueError("База учёта тикетов повреждена; автоматический сброс отменён.")
        if self._db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Неизвестная версия базы учёта тикетов.")
        for table, columns in (("state", STATE_COLUMNS), ("tickets", TICKET_COLUMNS)):
            found = tuple(row["name"] for row in self._db.execute(f"PRAGMA table_info({table})"))
            if found != columns:
                raise ValueError("Некорректная схема базы учёта тикетов.")
        states = self._db.execute("SELECT * FROM state").fetchall()
        if len(states) != 1 or states[0]["id"] != 1:
            raise ValueError("Некорректное состояние базы учёта тикетов.")
        state = self.state
        if state["route"] != route:
            raise ValueError("Маршрут изменён; база учёта тикетов не сброшена.")
        _message_id(state["last_id"], minimum=0)
        _message_id(state["command_offset"], minimum=0)
        _nonnegative_time(state["started_at"])
        _nonnegative_time(state["last_regular_at"])
        _nonnegative_time(state["send_retry_at"])
        if state["pending_id"] is not None:
            _message_id(state["pending_id"])
            pending = self._db.execute(
                "SELECT * FROM tickets WHERE source_message_id = ?", (state["pending_id"],)
            ).fetchone()
            if (state["pending_id"] <= state["last_id"] or pending is None
                    or pending["announced"] or pending["handled"]):
                raise ValueError("Некорректная неподтверждённая отправка в базе учёта тикетов.")

    @property
    def state(self):
        state = dict(self._db.execute("SELECT * FROM state WHERE id = 1").fetchone())
        del state["id"]
        state["route"] = json.loads(state["route"])
        state["paused"] = bool(state["paused"])
        return state

    def close(self):
        self._db.close()

    def ensure_no_pending(self):
        pending = self.state["pending_id"]
        if pending is not None:
            raise RuntimeError(f"Отправка сообщения {pending} не подтверждена. "
                               "Сначала нужна проверка и resolve.")

    def process(self, message_id, event, created_at):
        _message_id(message_id)
        with self._db:
            state = self.state
            if message_id <= state["last_id"]:
                return False
            self.ensure_no_pending()
            if event is not None:
                ticket_id = event.get("ticket_id")
                _message_id(ticket_id)
                kind, author = event.get("kind"), event.get("author")
                if kind not in ("created", "reply") or not isinstance(author, str) or not author.strip():
                    raise ValueError("Некорректное событие тикета.")
                ticket = self._db.execute(
                    "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
                if kind == "created":
                    if not isinstance(event.get("title"), str) or not event["title"].strip():
                        raise ValueError("В событии создания отсутствует тема тикета.")
                    if ticket is None:
                        self._db.execute(
                            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, 0, 0, NULL)",
                            (ticket_id, author, event["title"], _nonnegative_time(created_at), message_id),
                        )
                    if ticket is None or (ticket["source_message_id"] == message_id
                                          and not ticket["announced"] and not ticket["handled"]):
                        self._db.execute("UPDATE state SET pending_id = ? WHERE id = 1", (message_id,))
                        return True
                elif ticket is not None and not ticket["handled"]:
                    if normalize_author(author) != normalize_author(ticket["author"]):
                        self._db.execute(
                            "UPDATE tickets SET handled = 1, author = '', title = '' WHERE ticket_id = ?",
                            (ticket_id,),
                        )
            self._db.execute("UPDATE state SET last_id = ? WHERE id = 1", (message_id,))
        return False

    def confirm_sent(self, message_id):
        self.resolve(message_id, "sent")

    def resolve(self, message_id, result):
        _message_id(message_id)
        if result not in ("sent", "retry"):
            raise ValueError("Нужно указать результат проверки: sent или retry.")
        with self._db:
            if self.state["pending_id"] != message_id:
                raise ValueError("Указанный ID не совпадает с ожидающим проверки сообщением.")
            if result == "sent":
                self._db.execute("UPDATE tickets SET announced = 1 WHERE source_message_id = ?", (message_id,))
                self._db.execute("UPDATE state SET last_id = ?, pending_id = NULL WHERE id = 1", (message_id,))
            else:
                self._db.execute("UPDATE state SET pending_id = NULL WHERE id = 1")

    def apply_command(self, paused, update_id):
        if type(paused) is not bool:
            raise ValueError("Некорректная пауза напоминаний.")
        _message_id(update_id, minimum=0)
        with self._db:
            self._db.execute(
                "UPDATE state SET paused = ?, command_offset = ? WHERE id = 1 AND command_offset <= ?",
                (paused, update_id + 1, update_id),
            )

    def advance_commands(self, update_id):
        _message_id(update_id, minimum=0)
        with self._db:
            self._db.execute("UPDATE state SET command_offset = max(command_offset, ?) WHERE id = 1",
                             (update_id + 1,))

    def pending_tickets(self):
        return [dict(row) for row in self._db.execute(
            "SELECT ticket_id, author, title, created_at, source_message_id, urgent_at "
            "FROM tickets WHERE handled = 0 ORDER BY created_at, ticket_id"
        )]

    def reminder_plan(self, now, interval_seconds, quiet):
        now = _nonnegative_time(now)
        if (type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
                or interval_seconds <= 0):
            raise ValueError("Интервал напоминаний должен быть положительным числом секунд.")
        state = self.state
        if state["paused"] or now < state["send_retry_at"]:
            return {"regular_due": False, "tickets": [], "urgent_ids": []}
        regular_due = not quiet and now - state["last_regular_at"] >= interval_seconds
        tickets = self.pending_tickets()
        urgent = [ticket for ticket in tickets
                  if now - ticket["created_at"] > URGENT_AFTER_SECONDS
                  and (ticket["urgent_at"] is None
                       or now - ticket["urgent_at"] >= URGENT_INTERVAL_SECONDS)]
        return {"regular_due": regular_due, "tickets": tickets if regular_due else urgent,
                "urgent_ids": [ticket["ticket_id"] for ticket in urgent]}

    def mark_reminder_attempt(self, plan, now):
        now = _nonnegative_time(now)
        with self._db:
            if plan["regular_due"]:
                self._db.execute("UPDATE state SET last_regular_at = ? WHERE id = 1", (now,))
            self._db.executemany("UPDATE tickets SET urgent_at = ? WHERE ticket_id = ? AND handled = 0",
                                 [(now, ticket_id) for ticket_id in plan["urgent_ids"]])

    def defer_reminder(self, plan, retry_at, previous_regular_at):
        retry_at = _nonnegative_time(retry_at)
        previous_regular_at = _nonnegative_time(previous_regular_at)
        with self._db:
            self._db.execute("UPDATE state SET send_retry_at = max(send_retry_at, ?) WHERE id = 1",
                             (retry_at,))
            if plan["regular_due"]:
                self._db.execute("UPDATE state SET last_regular_at = ? WHERE id = 1", (previous_regular_at,))
            self._db.executemany(
                "UPDATE tickets SET urgent_at = ? WHERE ticket_id = ? AND handled = 0",
                [(ticket["urgent_at"], ticket["ticket_id"]) for ticket in plan["tickets"]
                 if ticket["ticket_id"] in plan["urgent_ids"]],
            )
