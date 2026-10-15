import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from commands import command_loop


CONFIG = {"destination_chat_id": -1001234567890, "destination_topic_id": 73}


def update(update_id=10, text="/pause", **changes):
    message = {"message_id": 20, "chat": {"id": CONFIG["destination_chat_id"]},
               "from": {"id": 123, "is_bot": False}, "date": 101,
               "text": text, "message_thread_id": 9}
    message.update(changes)
    return {"update_id": update_id, "message": message}


class Store:
    def __init__(self, state=None):
        self.state = state or {"command_offset": 0, "started_at": 100.0, "paused": False}
        self.changes = []

    def advance_commands(self, update_id):
        self.state["command_offset"] = max(self.state["command_offset"], update_id + 1)

    def apply_command(self, paused, update_id):
        self.state.update(paused=paused, command_offset=update_id + 1)
        self.changes.append((paused, update_id))


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_precision_command_at_initialization_is_not_dropped(self):
        store = Store({"command_offset": 0, "started_at": 101.9, "paused": False})
        store, sent, _ = await self.run_batches([[update(date=101)]], store=store)
        self.assertTrue(store.state["paused"])
        self.assertEqual(len(sent), 1)

    async def run_batches(self, batches, roles=None, store=None, send=None):
        store = store or Store()
        stop = asyncio.Event()
        sent, requests = [], []
        batches = iter(batches)
        roles = iter(roles or [])

        async def call(token, method, payload):
            self.assertEqual(token, "dummy")
            requests.append((method, copy.deepcopy(payload)))
            if method == "getUpdates":
                try:
                    result = next(batches)
                except StopIteration:
                    stop.set()
                    return []
            else:
                self.assertEqual(method, "getChatMember")
                result = next(roles, {"status": "administrator"})
            if isinstance(result, Exception):
                raise result
            return result

        async def record(payload):
            sent.append((copy.deepcopy(payload), copy.deepcopy(store.state)))
            if send:
                await send(payload)

        await asyncio.wait_for(command_loop("dummy", "ClubBot", CONFIG, store,
                                            stop, call, record), timeout=2)
        return store, sent, requests

    async def test_admin_and_owner_commands_persist_before_ack_in_command_topic(self):
        store, sent, requests = await self.run_batches(
            [[update(), update(11, "/resume@cLuBbOt", message_thread_id=42)]],
            [{"status": "creator"}, {"status": "administrator"}],
        )
        self.assertEqual(store.changes, [(True, 10), (False, 11)])
        self.assertEqual([entry[0]["message_thread_id"] for entry in sent], [9, 42])
        self.assertEqual([entry[1]["paused"] for entry in sent], [True, False])
        self.assertEqual([entry[1]["command_offset"] for entry in sent], [11, 12])
        self.assertIn("до /resume", sent[0][0]["text"])
        self.assertIn("тихие часы", sent[1][0]["text"])
        self.assertEqual([payload for method, payload in requests if method == "getChatMember"],
                         [{"chat_id": CONFIG["destination_chat_id"], "user_id": 123}] * 2)

    async def test_untrusted_and_unknown_messages_only_advance_cursor(self):
        cases = [
            update(text="/pause@OtherBot"), update(text="/unknown"),
            update(text="/pause extra"), update(text="/pause\n/resume"),
            update(chat={"id": -1009876543210}),
            update(sender_chat={"id": CONFIG["destination_chat_id"]}),
            update(**{"from": {"id": 123, "is_bot": True}}),
            update(**{"from": {}}), update(date=99),
            {"update_id": 10, "edited_message": update()["message"]},
            update(text=None),
        ]
        for item in cases:
            with self.subTest(item=item):
                store, sent, requests = await self.run_batches([[item]])
                self.assertFalse(store.state["paused"])
                self.assertEqual(store.state["command_offset"], 11)
                self.assertEqual(store.changes, [])
                self.assertEqual(sent, [])
                self.assertTrue(all(method == "getUpdates" for method, _ in requests))

    async def test_non_admin_does_not_pause(self):
        for status in ("member", "restricted", "left", "kicked"):
            with self.subTest(status=status):
                store, sent, _ = await self.run_batches([[update()]], [{"status": status}])
                self.assertFalse(store.state["paused"])
                self.assertEqual(store.state["command_offset"], 11)
                self.assertEqual(sent, [])

    async def test_duplicate_command_and_restart_use_persisted_offset(self):
        store, sent, _ = await self.run_batches([[update(), update(), update(11)]])
        self.assertEqual(store.changes, [(True, 10), (True, 11)])
        self.assertEqual(len(sent), 2)
        restored = Store(copy.deepcopy(store.state))
        restored, sent, requests = await self.run_batches(
            [[update(10, "/resume"), update(11, "/resume")]], store=restored,
        )
        self.assertTrue(restored.state["paused"])
        self.assertEqual(sent, [])
        self.assertEqual(requests[0][1]["offset"], 12)

    async def test_poll_payload_and_full_batch_cursor(self):
        batch = [update(i, "/unknown") for i in range(10, 110)]
        store, sent, requests = await self.run_batches([batch, [update(110)]])
        polls = [payload for method, payload in requests if method == "getUpdates"]
        self.assertEqual([payload["offset"] for payload in polls], [0, 110, 111])
        self.assertTrue(all(payload["timeout"] == 20 and payload["limit"] == 100
                            and payload["allowed_updates"] == ["message"] for payload in polls))
        self.assertTrue(store.state["paused"])
        self.assertEqual(len(sent), 1)

    async def test_receive_failure_retries_without_advancing(self):
        with patch("commands._retry_or_stop", new_callable=AsyncMock) as retry, \
             self.assertLogs("moneyrun_relay", "WARNING") as logged:
            store, sent, requests = await self.run_batches(
                [RuntimeError("https://secret-token/message-body"), [update()]],
            )
        retry.assert_awaited_once()
        self.assertEqual([payload["offset"] for method, payload in requests
                          if method == "getUpdates"], [0, 0, 11])
        self.assertTrue(store.state["paused"])
        self.assertEqual(len(sent), 1)
        self.assertNotIn("secret-token", " ".join(logged.output))
        self.assertNotIn("message-body", " ".join(logged.output))

    async def test_role_failure_blocks_later_batch_updates_until_retry(self):
        first = [update(9, "/unknown"), update(), update(11, "/resume")]
        with patch("commands._retry_or_stop", new_callable=AsyncMock) as retry, \
             self.assertLogs("moneyrun_relay", "WARNING") as logged:
            store, sent, requests = await self.run_batches(
                [first, first[1:]], [OSError("https://secret-token"),
                                    {"status": "creator"}, {"status": "administrator"}],
            )
        retry.assert_awaited_once()
        self.assertEqual([payload["offset"] for method, payload in requests
                          if method == "getUpdates"], [0, 10, 12])
        self.assertEqual(store.changes, [(True, 10), (False, 11)])
        self.assertEqual(len(sent), 2)
        self.assertNotIn("secret-token", " ".join(logged.output))

    async def test_ack_failure_leaves_committed_command_for_restart(self):
        store = Store()
        with self.assertRaisesRegex(OSError, "queue failed"):
            await self.run_batches([[update()]], store=store,
                                   send=AsyncMock(side_effect=OSError("queue failed")))
        self.assertTrue(store.state["paused"])
        self.assertEqual(store.state["command_offset"], 11)

    async def test_store_failure_stops_without_ack_or_advancing_cursor(self):
        store, send = Store(), AsyncMock()
        with patch.object(store, "apply_command", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await self.run_batches([[update()]], store=store, send=send)
        send.assert_not_awaited()
        self.assertFalse(store.state["paused"])
        self.assertEqual(store.state["command_offset"], 0)

    async def test_message_without_thread_does_not_use_configured_topic(self):
        item = update()
        del item["message"]["message_thread_id"]
        _, sent, _ = await self.run_batches([[item]])
        self.assertNotIn("message_thread_id", sent[0][0])

    async def test_stop_or_cancel_interrupt_inflight_api_without_task_leaks(self):
        for method, cancel in (("getUpdates", False), ("getUpdates", True),
                               ("getChatMember", False), ("getChatMember", True)):
            with self.subTest(method=method, cancel=cancel):
                stop, entered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

                async def call(token, api_method, payload):
                    if api_method != method:
                        return [update()]
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                task = asyncio.create_task(command_loop("dummy", "ClubBot", CONFIG, Store(),
                                                         stop, call, AsyncMock()))
                await asyncio.wait_for(entered.wait(), 1)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    stop.set()
                    await asyncio.wait_for(task, 1)
                self.assertTrue(cancelled.is_set())

    async def test_stop_interrupts_retry_delay(self):
        stop, failed = asyncio.Event(), asyncio.Event()

        async def call(*args):
            failed.set()
            raise OSError("offline")

        with self.assertLogs("moneyrun_relay", "WARNING"):
            task = asyncio.create_task(command_loop("dummy", "ClubBot", CONFIG, Store(),
                                                     stop, call, AsyncMock()))
            await asyncio.wait_for(failed.wait(), 1)
            stop.set()
            await asyncio.wait_for(task, 1)


if __name__ == "__main__":
    unittest.main()
