"""Локальная трансляция текстов клуба из Telegram через отдельного бота."""

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from getpass import getpass
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from tracking import TicketStore, parse_ticket_event
from commands import command_loop
from jokes import JokeSource, append_joke, jokes_enabled

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
LOG = logging.getLogger("moneyrun_relay")
TRUSTED_PUBLISHER_ID = 265658465  # @FBR2BOT: ID сохраняется при смене username.
ROUTE_KEYS = (
    "source_chat_id", "source_topic_id", "destination_chat_id",
    "destination_topic_id", "club_hashtag",
)


def validate_config(config, sending=False):
    source = config.get("source_chat_id")
    if type(source) is not int or source >= 0:
        raise ValueError("Нужно указать отрицательный source_chat_id из команды chats.")
    tag = config.get("club_hashtag", "")
    if not isinstance(tag, str) or not tag.startswith("#") or not tag[1:].isidentifier():
        raise ValueError("club_hashtag должен содержать один хэштег клуба.")
    for key in ("source_topic_id", "destination_topic_id"):
        value = config.get(key)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"{key}: нужен положительный номер топика или null.")
    if sending:
        target = config.get("destination_chat_id")
        if type(target) is not int or target >= 0:
            raise ValueError("Нужно указать отрицательный destination_chat_id.")
        if source == target:
            raise ValueError("Источник и получатель должны быть разными группами.")


def topic_id(message):
    reply = getattr(message, "reply_to", None)
    if reply and getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_top_id", None) or reply.reply_to_msg_id
    return 1  # В группе-форуме это общий топик.


def selected(message, config):
    if (message.chat_id != config["source_chat_id"]
            or getattr(message, "sender_id", None) != TRUSTED_PUBLISHER_ID):
        return False
    topic = config.get("source_topic_id")
    if topic is not None and topic_id(message) != topic:
        return False
    text = message.raw_text or ""
    # Формат подтверждён примером пользователя: хэштег клуба — вся первая строка.
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first_line.casefold() == config["club_hashtag"].casefold()


def message_payload(message, config):
    from telethon.tl.types import MessageEntityTextUrl

    payload = {"chat_id": config["destination_chat_id"], "text": message.raw_text,
               "link_preview_options": {"is_disabled": True}}
    text_length = len((message.raw_text or "").encode("utf-16-le")) // 2
    links = [{"type": "text_link", "offset": entity.offset,
              "length": entity.length, "url": entity.url}
             for entity in (getattr(message, "entities", None) or [])
             if isinstance(entity, MessageEntityTextUrl)
             and isinstance(entity.url, str) and entity.url
             and type(entity.offset) is int and type(entity.length) is int
             and 0 <= entity.offset < entity.offset + entity.length <= text_length]
    if links:
        payload["entities"] = links
    if config.get("destination_topic_id") is not None:
        payload["message_thread_id"] = config["destination_topic_id"]
    source = str(config["source_chat_id"])
    if config["source_chat_id"] < -1000000000000:
        url = f"https://t.me/c/{source[4:]}/{message.id}"
        payload["reply_markup"] = {"inline_keyboard": [
            [{"text": "Открыть в источнике", "url": url}]
        ]}
    return payload


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_state(path, route, allow_pending=False):
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("route") != route:
        raise ValueError("Маршрут изменён. Сначала переименуй .local/state.json; "
                         "новый запуск начнёт с новых сообщений.")
    if type(state.get("last_id")) is not int or state["last_id"] < 0:
        raise ValueError("Некорректный .local/state.json; автоматический сброс отменён.")
    pending = state.get("pending_id")
    if pending is not None:
        if type(pending) is not int or pending <= state["last_id"]:
            raise ValueError("Некорректный pending_id в .local/state.json.")
        if not allow_pending:
            raise RuntimeError(f"Отправка сообщения {pending} не подтверждена. "
                               "Проверь группу-получатель и выполни resolve по PORTAINER.md.")
    return state


def resolve_pending(path, route, message_id, result):
    state = read_state(path, route, allow_pending=True)
    if state is None or state.get("pending_id") != message_id:
        raise ValueError("Указанный ID не совпадает с ожидающим проверки сообщением.")
    if result not in ("sent", "retry"):
        raise ValueError("Нужно указать результат проверки: sent или retry.")
    if result == "sent":
        state["last_id"] = message_id
    state["pending_id"] = None
    write_json(path, state)


async def process_message(message, config, state, save, send):
    if state.get("pending_id") is not None:
        raise RuntimeError("Есть неподтверждённая отправка; сначала нужна проверка.")
    if message.id <= state["last_id"]:
        return False
    matches = selected(message, config)
    if matches:
        if getattr(message, "noforwards", False):
            raise RuntimeError("У сообщения включена защита содержимого; пересылка остановлена.")
        pending = {**state, "pending_id": message.id}
        save(pending)
        state.update(pending)
        await send(message_payload(message, config))
    next_state = {**state, "last_id": message.id, "pending_id": None}
    save(next_state)  # После подтверждения Telegram, без продвижения при ошибке отправки.
    state.update(next_state)
    return matches


async def wait_or_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def test_recent(client, source, config, count=3, sending=False):
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError("Для теста допустимо от 1 до 3 сообщений.")
    if getattr(source, "noforwards", False):
        raise RuntimeError("В источнике включена защита содержимого; тест остановлен.")
    messages = []
    # Не читаем всю историю большого форума ради разовой проверки.
    async for message in client.iter_messages(source, limit=1000):
        if selected(message, config):
            messages.append(message)
            if len(messages) == count:
                break
    messages.reverse()
    print(f"Найдено сообщений клуба: {len(messages)} из запрошенных {count}. "
          "Поиск ограничен последними 1000 сообщениями источника.")
    print(f"Получатель: {config['destination_chat_id']}, "
          f"тема: {config.get('destination_topic_id')}.")
    for message in messages:
        if getattr(message, "noforwards", False):
            raise RuntimeError("У сообщения включена защита содержимого; тест остановлен.")
        print(f"\nСообщение {message.id}, топик {topic_id(message)}:\n{message.raw_text}")
    if not sending or not messages:
        print("Ничего не отправлено. Для тестовой отправки добавь --send.")
        return
    print("Тест повторно отправляет выбранные тексты, даже если они уже были доставлены.")
    token = config.get("bot_token") or getpass("Токен бота-получателя (ввод скрыт): ")
    target = await bot_call(token, "getChat", {"chat_id": config["destination_chat_id"]})
    if target.get("type") not in ("group", "supergroup"):
        raise ValueError("Получателем должна быть группа клуба.")
    receipt = LOCAL / f"test-send-{uuid4().hex}.json"
    state = {"route": {key: config.get(key) for key in ROUTE_KEYS},
             "last_id": 0, "pending_id": None,
             "message_ids": [message.id for message in messages]}
    print(f"Результат теста: {receipt.name}. Рабочий state.json не меняется.")

    async def send(payload):
        await bot_call(token, "sendMessage", payload)

    for index, message in enumerate(messages):
        await process_message(message, config, state,
                              lambda data: write_json(receipt, data), send)
        print(f"Тест: передано сообщение {message.id}.")
        if index < len(messages) - 1:
            await asyncio.sleep(3.2)
    print(f"Тест завершён: отправлено {len(messages)} сообщений.")


def poll_interval_seconds():
    try:
        seconds = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
        if seconds < 1:
            raise ValueError
    except ValueError:
        raise ValueError("POLL_INTERVAL_SECONDS: нужно целое число секунд не меньше 1.") from None
    return seconds


def reminder_settings():
    try:
        hours = int(os.environ.get("REMINDER_INTERVAL_HOURS", "4"))
        if hours < 1:
            raise ValueError
    except ValueError:
        raise ValueError("REMINDER_INTERVAL_HOURS: нужно целое число часов не меньше 1.") from None
    values = []
    for key, default in (("QUIET_HOURS_START", "23:00"), ("QUIET_HOURS_END", "09:00")):
        value = os.environ.get(key, default)
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{key}: нужно время в формате ЧЧ:ММ от 00:00 до 23:59.")
        hour, minute = map(int, value.split(":"))
        values.append(hour * 60 + minute)
    if values[0] == values[1]:
        raise ValueError("Начало и конец тихих часов должны различаться.")
    return {"interval": hours * 3600, "start": values[0], "end": values[1]}


def quiet_hours(now, settings):
    local = datetime.fromtimestamp(now, timezone(timedelta(hours=3)))
    minute = local.hour * 60 + local.minute
    start, end = settings["start"], settings["end"]
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def reminder_payloads(plan, config, now):
    title = ("Ожидают первого ответа" if plan["regular_due"]
             else "Срочно ❗❗❗: обращения ожидают ответа более 20 часов")
    heading = title + ":\n\n"
    chunks, text = [], heading
    for ticket in plan["tickets"]:
        hours = max(0, int((now - ticket["created_at"]) / 3600))
        minutes = max(0, int((now - ticket["created_at"]) / 60)) % 60
        urgent = "СРОЧНО ❗❗❗ · " if now - ticket["created_at"] > 20 * 3600 else ""
        source = str(config["source_chat_id"])
        link = (f"https://t.me/c/{source[4:]}/{ticket['source_message_id']}\n"
                if config["source_chat_id"] < -1000000000000 else "")
        entry = (f"{urgent}#{ticket['ticket_id']} · {ticket['author'][:100]}\n"
                 f"{ticket['title'][:200]}\nОжидание: {hours} ч {minutes} мин\n{link}\n")
        if len((text + entry).encode("utf-16-le")) // 2 > 3900:
            chunks.append(text.rstrip())
            text = heading
        text += entry
    if text != heading:
        chunks.append(text.rstrip())
    return [{"chat_id": config["destination_chat_id"], "text": chunk,
             "link_preview_options": {"is_disabled": True},
             **({"message_thread_id": config["destination_topic_id"]}
                if config.get("destination_topic_id") is not None else {})}
            for chunk in chunks]


def check_unattended_config(config, command):
    required = ["api_id", "api_hash"]
    if command in ("run", "test"):
        required.append("bot_token")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError("Для запуска без терминала заполни config.json: " + ", ".join(missing))
    if not (LOCAL / "reader.session").is_file():
        raise ValueError("Нет .local/reader.session. Сначала выполни вход локально "
                         "и перенеси сохранённую сессию в папку данных контейнера.")


def bot_request(token, method, payload):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=40) as response:
            return json.load(response)
    except HTTPError as error:
        # Не выводим URL исключения: он содержит токен.
        try:
            return json.loads(error.read())
        except (ValueError, OSError, HTTPException):
            raise RuntimeError(f"Telegram Bot API: HTTP {error.code}.") from None
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        raise RuntimeError("Не получено подтверждение Telegram Bot API. "
                           "Перед повторным запуском проверь группу: сообщение могло дойти.") from None


class BotRateLimitError(RuntimeError):
    def __init__(self, seconds):
        super().__init__("Telegram запросил паузу отправки.")
        self.seconds = seconds


async def bot_call(token, method, payload, *, allowed=None, wait_on_rate_limit=True):
    while True:
        if allowed is not None and not allowed():
            return None
        result = await asyncio.to_thread(bot_request, token, method, payload)
        if result.get("ok"):
            return result["result"]
        if result.get("error_code") == 429:
            seconds = max(1, result.get("parameters", {}).get("retry_after", 15))
            if not wait_on_rate_limit:
                raise BotRateLimitError(seconds)
            await asyncio.sleep(seconds)
            continue
        code = result.get("error_code", "unknown")
        raise RuntimeError(f"Telegram Bot API: ошибка {code}. "
                           "Проверь токен, ID группы/топика и право бота отправлять сообщения.")


async def run_tracking(client, source, config, store, stop, poll_interval, settings, token, bot,
                       *, use_jokes=False):
    from telethon.errors import FloodWaitError

    send_lock, acknowledgements = asyncio.Lock(), asyncio.Queue(maxsize=100)
    reaction_ready = asyncio.Event()
    joke_source = JokeSource() if use_jokes else None
    last_send = 0.0

    async def send(payload, *, reminder=False, regular=False, before_send=None, reaction=False):
        nonlocal last_send
        async with send_lock:
            while store.state["send_retry_at"] > time.time():
                if reminder or reaction:
                    return False
                await asyncio.sleep(store.state["send_retry_at"] - time.time())
            await asyncio.sleep(max(0, 3.2 - (time.monotonic() - last_send)))
            allowed = lambda: (not stop.is_set() and not store.state["paused"]
                               and (not regular or not quiet_hours(time.time(), settings)))
            if reminder and not allowed():
                return False
            if before_send is not None:
                before_send()
            try:
                if reminder:
                    result = await bot_call(token, "sendMessage", payload, allowed=allowed,
                                            wait_on_rate_limit=False)
                elif reaction:
                    result = await bot_call(token, "setMessageReaction", payload,
                                            wait_on_rate_limit=False)
                else:
                    result = await bot_call(token, "sendMessage", payload)
                return result
            finally:
                last_send = time.monotonic()

    async def enqueue_ack(payload):
        try:
            acknowledgements.put_nowait(payload)
        except asyncio.QueueFull:
            LOG.warning("Очередь подтверждений команд заполнена; команда уже сохранена.")

    async def acknowledge():
        while True:
            payload = await acknowledgements.get()
            try:
                await send(payload)
            except RuntimeError:
                LOG.warning("Не удалось подтвердить команду в Telegram; состояние сохранено.")
            finally:
                acknowledgements.task_done()

    async def react_to_handled():
        while not stop.is_set():
            reaction_ready.clear()
            for ticket in store.pending_reactions(time.time()):
                if stop.is_set():
                    return
                payload = {"chat_id": config["destination_chat_id"],
                           "message_id": ticket["destination_message_id"],
                           "reaction": [{"type": "emoji", "emoji": "👀"}]}
                try:
                    if not await send(payload, reaction=True):
                        break
                except BotRateLimitError as error:
                    store.defer_reaction(ticket["ticket_id"], time.time() + error.seconds,
                                         rate_limited=True)
                    LOG.warning("Telegram отложил отправки на %s с.; срок сохранён, бот продолжает работу.",
                                error.seconds)
                    break
                except RuntimeError:
                    store.defer_reaction(ticket["ticket_id"], time.time() + 300)
                    LOG.warning("Не удалось поставить 👀 на тикет #%s; повтор через 5 минут. "
                                "Проверь, что карточка существует и реакция разрешена в группе.",
                                ticket["ticket_id"])
                else:
                    store.confirm_reaction(ticket["ticket_id"])
                    LOG.info("Поставлена реакция 👀 на тикет #%s.", ticket["ticket_id"])
            try:
                await asyncio.wait_for(reaction_ready.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass

    async def read_and_remind():
        while not stop.is_set():
            try:
                messages = await client.get_messages(
                    source, min_id=store.state["last_id"], reverse=True, limit=100)
            except FloodWaitError as error:
                print(f"Telegram запросил паузу {error.seconds} с.")
                await wait_or_stop(stop, error.seconds)
                continue
            except (OSError, asyncio.TimeoutError):
                LOG.warning("Нет связи с источником. Повтор чтения через %s с.", poll_interval)
                await wait_or_stop(stop, poll_interval)
                continue
            LOG.info("Источник ответил: получено новых сообщений — %s (до отбора по клубу).",
                     len(messages))
            sent_cards = False
            for message in messages:
                if stop.is_set():
                    break
                matches = selected(message, config)
                event = parse_ticket_event(message.raw_text or "") if matches else None
                if matches and event is None:
                    LOG.warning("Не распознан заголовок уведомления %s; состояние тикетов не изменено.",
                                message.id)
                if event and getattr(message, "noforwards", False):
                    raise RuntimeError("У сообщения включена защита содержимого; обработка остановлена.")
                created_at = message.date.timestamp() if event else 0
                if store.process(message.id, event, created_at):
                    payload = message_payload(message, config)
                    if joke_source is not None:
                        payload["text"] = append_joke(payload["text"], await joke_source.choose())
                    delivery = await send(payload)
                    target_id = delivery.get("message_id") if isinstance(delivery, dict) else None
                    if type(target_id) is not int or target_id <= 0:
                        raise RuntimeError("Telegram не подтвердил ID доставленной карточки; "
                                           "сначала нужна проверка и resolve.")
                    store.confirm_sent(message.id, target_id)
                    sent_cards = True
                    print(f"Передано сообщение {message.id}.")
                if event and event["kind"] == "reply":
                    reaction_ready.set()
            if len(messages) == 100 or sent_cards:
                continue
            if stop.is_set():
                break
            now = time.time()
            previous_regular_at = store.state["last_regular_at"]
            plan = store.reminder_plan(now, settings["interval"], quiet_hours(now, settings))
            payloads = reminder_payloads(plan, config, now)
            if plan["regular_due"] and not payloads:
                store.mark_reminder_attempt(plan, now)
            for index, payload in enumerate(payloads):
                try:
                    recorded = (lambda: store.mark_reminder_attempt(plan, time.time())) if index == 0 else None
                    if not await send(payload, reminder=True, regular=plan["regular_due"],
                                      before_send=recorded):
                        break
                    LOG.info("Отправлена часть сводки %s/%s.", index + 1, len(payloads))
                except BotRateLimitError as error:
                    # После частичной доставки не откатывать сроки уже отправленных частей.
                    rejected = plan if index == 0 else {"regular_due": False, "tickets": [], "urgent_ids": []}
                    store.defer_reminder(rejected, time.time() + error.seconds, previous_regular_at)
                    LOG.warning("Telegram отложил рассылку на %s с.; чтение источника продолжается.",
                                error.seconds)
                    break
                except RuntimeError:
                    LOG.warning("Не получено подтверждение рассылки; следующая попытка по расписанию.")
                    break
            await wait_or_stop(stop, poll_interval)
        print("Пересылка остановлена; подтверждённые отправки и тикеты сохранены.")

    reader = asyncio.create_task(read_and_remind())
    tasks = [reader, asyncio.create_task(command_loop(token, bot["username"], config, store,
                                                      stop, bot_call, enqueue_ack)),
             asyncio.create_task(acknowledge())]
    tasks.append(asyncio.create_task(react_to_handled()))
    disconnected = client.disconnected  # Telethon защищает свою Future от отмены наблюдателя.
    tasks.append(disconnected)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        connection_lost = disconnected in done and not stop.is_set()
        if connection_lost:
            stop.set()
        for task in done:
            if task is not disconnected:
                task.result()
        if stop.is_set():
            if store.state["pending_id"] is None and not reader.done():
                reader.cancel()  # Ожидание чтения можно прервать без изменения позиции.
                await asyncio.gather(reader, return_exceptions=True)
            else:
                await reader  # Сохранить результат начатой отправки нового тикета.
        elif reader not in done:
            raise RuntimeError("Одна из фоновых задач неожиданно остановилась.")
        if connection_lost:
            raise RuntimeError("Соединение с источником окончательно потеряно. "
                               "Процесс завершён для перезапуска; подтверждённые отправки сохранены.")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@contextmanager
def single_instance():
    LOCAL.mkdir(exist_ok=True)
    with (LOCAL / "run.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Другая копия программы уже использует эту папку.") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


async def main(args):
    from telethon import TelegramClient

    path = ROOT / "config.json"
    if not path.exists():
        raise ValueError("Сначала скопируй config.example.json в config.json.")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if args.command != "chats":
        validate_config(config, sending=args.command in ("run", "resolve", "test"))
    route = {key: config.get(key) for key in ROUTE_KEYS}
    state_path = LOCAL / "state.json"
    store_path = LOCAL / "tickets.sqlite3"
    marker = LOCAL / "tracking.initialized"
    if args.command in ("run", "resolve") and marker.exists() and not store_path.exists():
        raise ValueError("Пропала база tickets.sqlite3. Восстанови папку Local из резервной копии; "
                         "автоматический запуск с нуля отменён.")
    if args.command == "resolve":
        if args.message_id is None or args.result is None:
            raise ValueError("Для resolve нужны --message-id и --result sent|retry.")
        if store_path.exists():
            with_store = TicketStore(store_path, route)
            try:
                with_store.resolve(args.message_id, args.result)
            finally:
                with_store.close()
        else:
            resolve_pending(state_path, route, args.message_id, args.result)
        print("Результат проверки сохранён. Можно запускать пересылку.")
        return
    if args.command == "run" and not store_path.exists():
        read_state(state_path, route)  # Сначала разрешить старую неподтверждённую отправку.
    if args.non_interactive:
        check_unattended_config(config, args.command)
    stop = asyncio.Event()
    if args.command == "run":
        poll_interval = poll_interval_seconds()
        settings = reminder_settings()
        use_jokes = jokes_enabled()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    api_id = int(config.get("api_id") or input("API ID с my.telegram.org: "))
    api_hash = config.get("api_hash") or getpass("API hash (ввод скрыт): ")
    client = TelegramClient(str(LOCAL / "reader"), api_id, api_hash, receive_updates=False)
    store = None
    try:
        if args.command == "run" and store_path.exists():
            store = TicketStore(store_path, route)
            store.ensure_no_pending()
        if args.non_interactive:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("Сессия Telegram недействительна. Нужен повторный вход локально.")
        else:
            await client.start()
        if await client.is_bot():
            raise ValueError("Для чтения источника нужен вход в пользовательский аккаунт.")
        if args.command == "chats":
            async for dialog in client.iter_dialogs():
                if dialog.is_group:
                    print(f"{dialog.id}\t{dialog.name}")
            return
        source = await client.get_entity(config["source_chat_id"])
        if args.command == "test":
            await test_recent(client, source, config, args.count, args.send)
            return
        if args.command == "preview":
            messages = await client.get_messages(source, limit=50)
            count = 0
            for message in reversed(messages):
                if selected(message, config):
                    count += 1
                    print(f"\nСообщение {message.id}, топик {topic_id(message)}:\n{message.raw_text}")
            print(f"\nПодходящих сообщений среди последних 50: {count}. Ничего не отправлено.")
            return
        if getattr(source, "noforwards", False):
            raise RuntimeError("В источнике включена защита содержимого; пересылка остановлена.")
        token = config.get("bot_token") or getpass("Токен бота-получателя (ввод скрыт): ")
        bot = await bot_call(token, "getMe", {})
        target = await bot_call(token, "getChat", {"chat_id": config["destination_chat_id"]})
        if target.get("type") not in ("group", "supergroup"):
            raise ValueError("Получателем должна быть группа клуба.")
        webhook = await bot_call(token, "getWebhookInfo", {})
        if webhook.get("url"):
            raise ValueError("У бота настроен webhook. Для команд нужен getUpdates; "
                             "отключи прежний webhook перед запуском.")
        member = await bot_call(token, "getChatMember",
                                {"chat_id": config["destination_chat_id"], "user_id": bot["id"]})
        if member.get("status") not in ("creator", "administrator"):
            raise ValueError("Для проверки команд бот должен быть администратором группы-получателя.")
        if store is None:
            latest = await client.get_messages(source, limit=1)
            store = TicketStore(store_path, route, initial_id=latest[0].id if latest else 0,
                                now=time.time())
            print("Учёт тикетов начат с нуля. Старая история не читается и не пересылается.")
        marker.write_text("1\n", encoding="ascii")
        print(f"Ожидание новых тикетов {config['club_hashtag']}. "
              f"Интервал опроса: {poll_interval} с. Остановка: Ctrl+C.")
        print(f"Обычная сводка: каждые {settings['interval'] // 3600} ч. "
              f"Тихие часы: {settings['start'] // 60:02}:{settings['start'] % 60:02}–"
              f"{settings['end'] // 60:02}:{settings['end'] % 60:02} по Москве. "
              f"Ручная пауза: {'включена' if store.state['paused'] else 'выключена'}.")
        print(f"Анекдоты: {'включены' if use_jokes else 'выключены'}.")
        await run_tracking(client, source, config, store, stop, poll_interval, settings, token, bot,
                           use_jokes=use_jokes)
    finally:
        try:
            await client.disconnect()
        finally:
            if store is not None:
                store.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
    LOG.setLevel(logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("chats", "preview", "run", "resolve", "test"),
                        help="chats — ID групп; preview — просмотр; run — пересылка; "
                             "resolve — проверка отправки; test — последние сообщения клуба")
    parser.add_argument("--count", type=int, choices=(1, 2, 3), default=3,
                        help="Число сообщений для test (по умолчанию 3)")
    parser.add_argument("--send", action="store_true", help="Отправить сообщения в режиме test")
    parser.add_argument("--non-interactive", action="store_true", help="Без запросов ввода, для Docker")
    parser.add_argument("--message-id", type=int, help="ID неподтверждённой отправки для resolve")
    parser.add_argument("--result", choices=("sent", "retry"), help="Результат ручной проверки для resolve")
    args = parser.parse_args()
    try:
        with single_instance():
            asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except (ValueError, RuntimeError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"Ошибка {type(error).__name__}. Проверь подключение и настройки; "
              "для ID закрытой группы сначала запусти chats.", file=sys.stderr)
        sys.exit(1)
