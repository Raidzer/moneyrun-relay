"""Получение административных команд паузы напоминаний через Bot API."""

import asyncio
import logging
import re


LOG = logging.getLogger("moneyrun_relay")
RETRY_SECONDS = 3


def _command(message, username, config, started_at):
    if (message.get("chat", {}).get("id") != config["destination_chat_id"]
            or message.get("sender_chat") is not None):
        return None
    sender = message.get("from", {})
    if (type(sender.get("id")) is not int or sender["id"] <= 0
            or sender.get("is_bot") is not False):
        return None
    date = message.get("date")
    text = message.get("text")
    if type(date) is not int or date < int(started_at) or not isinstance(text, str):
        return None
    match = re.fullmatch(r"/(pause|resume)(?:@([A-Za-z0-9_]+))?", text.strip())
    if not match or (match[2] and match[2].casefold() != username.casefold()):
        return None
    return match[1] == "pause"


async def _retry_or_stop(stop):
    try:
        await asyncio.wait_for(stop.wait(), timeout=RETRY_SECONDS)
    except asyncio.TimeoutError:
        pass


async def _receive_commands(token, username, config, store, stop, call, send):
    while not stop.is_set():
        try:
            updates = await call(token, "getUpdates", {
                "offset": store.state["command_offset"], "timeout": 20,
                "limit": 100, "allowed_updates": ["message"],
            })
            if not isinstance(updates, list) or any(
                not isinstance(update, dict) or type(update.get("update_id")) is not int
                for update in updates
            ):
                raise ValueError("Некорректный результат getUpdates.")
        except Exception:
            LOG.warning("Не удалось получить команды. Повтор через %s с.", RETRY_SECONDS)
            await _retry_or_stop(stop)
            continue
        for update in updates:
            if stop.is_set():
                return
            update_id = update["update_id"]
            if update_id < store.state["command_offset"]:
                continue
            message = update.get("message")
            paused = (_command(message, username, config, store.state["started_at"])
                      if isinstance(message, dict) else None)
            if paused is None:
                store.advance_commands(update_id)
                continue
            try:
                member = await call(token, "getChatMember", {
                    "chat_id": config["destination_chat_id"], "user_id": message["from"]["id"],
                })
                status = member["status"]
            except Exception:
                LOG.warning("Не удалось проверить права отправителя команды. "
                            "Повтор через %s с.", RETRY_SECONDS)
                await _retry_or_stop(stop)
                break  # Следующий offset не должен подтвердить непроверенную команду.
            if status not in ("creator", "administrator"):
                store.advance_commands(update_id)
                continue
            store.apply_command(paused, update_id)
            payload = {
                "chat_id": config["destination_chat_id"],
                "text": ("Все напоминания приостановлены до /resume. "
                         "Новые тикеты продолжают поступать, ответы учитываются."
                         if paused else
                         "Напоминания возобновлены. Обычные сводки соблюдают тихие часы; "
                         "срочные напоминания разрешены круглосуточно."),
            }
            if message.get("message_thread_id") is not None:
                payload["message_thread_id"] = message["message_thread_id"]
            await send(payload)


async def command_loop(token, username, config, store, stop, call, send):
    """Применять команды и быстро ставить подтверждения в общую очередь отправки."""
    worker = asyncio.create_task(
        _receive_commands(token, username, config, store, stop, call, send),
    )
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((worker, stopping), return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
    finally:
        worker.cancel()
        stopping.cancel()
        await asyncio.gather(worker, stopping, return_exceptions=True)
