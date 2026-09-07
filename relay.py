"""Локальная трансляция текстов клуба из Telegram через отдельного бота."""

import argparse
import asyncio
from contextlib import contextmanager
from getpass import getpass
import json
import os
from pathlib import Path
import signal
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
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
    if message.chat_id != config["source_chat_id"]:
        return False
    topic = config.get("source_topic_id")
    if topic is not None and topic_id(message) != topic:
        return False
    text = message.raw_text or ""
    # Формат подтверждён примером пользователя: хэштег клуба — вся первая строка.
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first_line.casefold() == config["club_hashtag"].casefold()


def message_payload(message, config):
    payload = {"chat_id": config["destination_chat_id"], "text": message.raw_text,
               "link_preview_options": {"is_disabled": True}}
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


def poll_interval_seconds():
    try:
        seconds = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
        if seconds < 1:
            raise ValueError
    except ValueError:
        raise ValueError("POLL_INTERVAL_SECONDS: нужно целое число секунд не меньше 1.") from None
    return seconds


def check_unattended_config(config, command):
    required = ["api_id", "api_hash"]
    if command == "run":
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
        except (ValueError, OSError):
            raise RuntimeError(f"Telegram Bot API: HTTP {error.code}.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("Не получено подтверждение Telegram Bot API. "
                           "Перед повторным запуском проверь группу: сообщение могло дойти.") from None


async def bot_call(token, method, payload):
    while True:
        result = await asyncio.to_thread(bot_request, token, method, payload)
        if result.get("ok"):
            return result["result"]
        if result.get("error_code") == 429:
            await asyncio.sleep(max(1, result.get("parameters", {}).get("retry_after", 15)))
            continue
        code = result.get("error_code", "unknown")
        raise RuntimeError(f"Telegram Bot API: ошибка {code}. "
                           "Проверь токен, ID группы/топика и право бота отправлять сообщения.")


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
    from telethon.errors import FloodWaitError

    path = ROOT / "config.json"
    if not path.exists():
        raise ValueError("Сначала скопируй config.example.json в config.json.")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if args.command != "chats":
        validate_config(config, sending=args.command in ("run", "resolve"))
    route = {key: config.get(key) for key in ROUTE_KEYS}
    state_path = LOCAL / "state.json"
    if args.command == "resolve":
        if args.message_id is None or args.result is None:
            raise ValueError("Для resolve нужны --message-id и --result sent|retry.")
        resolve_pending(state_path, route, args.message_id, args.result)
        print("Результат проверки сохранён. Можно запускать пересылку.")
        return
    state = read_state(state_path, route) if args.command == "run" else None
    if args.non_interactive:
        check_unattended_config(config, args.command)
    stop = asyncio.Event()
    if args.command == "run":
        poll_interval = poll_interval_seconds()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    api_id = int(config.get("api_id") or input("API ID с my.telegram.org: "))
    api_hash = config.get("api_hash") or getpass("API hash (ввод скрыт): ")
    client = TelegramClient(str(LOCAL / "reader"), api_id, api_hash, receive_updates=False)
    try:
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
        await bot_call(token, "getMe", {})
        target = await bot_call(token, "getChat", {"chat_id": config["destination_chat_id"]})
        if target.get("type") not in ("group", "supergroup"):
            raise ValueError("Получателем должна быть группа клуба.")
        if state is None:
            latest = await client.get_messages(source, limit=1)
            state = {"route": route, "last_id": latest[0].id if latest else 0}
            write_json(state_path, state)
            print("Начальная точка сохранена. История не пересылается.")
        print(f"Ожидание новых сообщений {config['club_hashtag']}. "
              f"Интервал опроса: {poll_interval} с. Остановка: Ctrl+C.")

        async def send(payload):
            await bot_call(token, "sendMessage", payload)

        while not stop.is_set():
            try:
                messages = await client.get_messages(
                    source, min_id=state["last_id"], reverse=True, limit=100,
                )
            except FloodWaitError as error:
                print(f"Telegram запросил паузу {error.seconds} с.")
                await wait_or_stop(stop, error.seconds)
                continue
            except (OSError, asyncio.TimeoutError):
                print(f"Нет связи с источником. Повтор чтения через {poll_interval} с.")
                await wait_or_stop(stop, poll_interval)
                continue
            for message in messages:
                if stop.is_set():
                    break
                sent = await process_message(
                    message, config, state,
                    lambda data: write_json(state_path, data), send,
                )
                if sent:
                    print(f"Передано сообщение {message.id}.")
                    await wait_or_stop(stop, 3.2)
            await wait_or_stop(stop, poll_interval)
        print("Пересылка остановлена; подтверждённые отправки сохранены.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("chats", "preview", "run", "resolve"),
                        help="chats — ID групп; preview — просмотр; run — пересылка; resolve — проверка отправки")
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
