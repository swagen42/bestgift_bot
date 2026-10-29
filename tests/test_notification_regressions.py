from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter

from tests import test_scenarios as scenarios


class NotificationRegressions(unittest.IsolatedAsyncioTestCase):
    # Используем тот же локальный Telegram API и настоящую временную SQLite.
    asyncSetUp = scenarios.Scenarios.asyncSetUp
    asyncTearDown = scenarios.Scenarios.asyncTearDown
    send = scenarios.Scenarios.send
    click = scenarios.Scenarios.click
    click_raw = scenarios.Scenarios.click_raw
    reach_materials = scenarios.Scenarios.reach_materials
    finish = scenarios.Scenarios.finish
    drain = scenarios.Scenarios.drain

    async def test_long_caption_is_delivered_in_full_without_blocking_order(self):
        await self.reach_materials()
        caption = "😀" * 1024
        await self.send(
            document={"file_id": "long_caption", "file_unique_id": "long_caption"},
            caption=caption,
        )
        await self.finish()
        await self.drain()
        file_params = next(params for method, params in self.api.calls if method == "sendDocument")
        self.assertLessEqual(len(file_params["caption"].encode("utf-16-le")) // 2, 1024)
        caption_text = "".join(
            params["text"] for method, params in self.api.calls
            if method == "sendMessage" and params["chat_id"] == 777 and "Подпись:" in params["text"]
        )
        self.assertIn(caption, caption_text)
        self.assertEqual(self.store.order(1)["client_notified"], 1)

    async def test_rate_limit_pauses_other_orders_too(self):
        await self.reach_materials()
        await self.finish()
        await self.reach_materials()
        await self.finish()
        self.api.failures["sendMessage"] = [RetryAfter(60)]
        with patch("bestgift_bot.notifications.time.time", return_value=1000.0):
            await self.notifier.deliver_step(self.app.bot)
            calls = len(self.api.calls)
            self.assertFalse(await self.notifier.deliver_step(self.app.bot))
            self.assertEqual(len(self.api.calls), calls)
            self.assertIsNone(self.store.order(2)["owner_message_id"])
        with patch("bestgift_bot.notifications.time.time", return_value=1062.0):
            await self.drain()
        self.assertTrue(all(self.store.order(i)["client_notified"] for i in (1, 2)))

    async def test_parallel_delivery_does_not_duplicate_summary(self):
        await self.reach_materials()
        await self.finish()
        await asyncio.gather(*(self.notifier.deliver_step(self.app.bot) for _ in range(3)))
        owner_messages = [
            params for method, params in self.api.calls
            if method == "sendMessage" and params["chat_id"] == 777
        ]
        self.assertEqual(len(owner_messages), 1)
        self.assertEqual(self.store.order(1)["client_notified"], 1)

    async def test_notifier_cannot_start_duplicate_worker_and_can_restart(self):
        with patch.object(self.notifier, "deliver_step", new=AsyncMock(return_value=False)):
            self.notifier.start(self.app.bot)
            with self.assertRaisesRegex(RuntimeError, "уже запущена"):
                self.notifier.start(self.app.bot)
            await self.notifier.stop()
            self.assertTrue(self.notifier.task.done())
            self.notifier.start(self.app.bot)
            await self.notifier.stop()
            self.assertTrue(self.notifier.task.done())


if __name__ == "__main__":
    unittest.main()
