"""Проверки восстановления диалога и ограничений Telegram без сетевых запросов."""
from __future__ import annotations

import unittest

from telegram import Update
from telegram.error import BadRequest

from bestgift_bot.catalog import Product, Variant
from bestgift_bot.presentation import ORDER_BUTTON, split_text
from tests import test_scenarios as scenarios


class DialogRegressions(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scenarios.Scenarios.asyncSetUp
    asyncTearDown = scenarios.Scenarios.asyncTearDown
    send = scenarios.Scenarios.send
    click = scenarios.Scenarios.click
    click_raw = scenarios.Scenarios.click_raw
    reach_materials = scenarios.Scenarios.reach_materials
    finish = scenarios.Scenarios.finish
    drain = scenarios.Scenarios.drain

    async def test_expired_callback_answer_does_not_lose_product_selection(self):
        await self.send(ORDER_BUTTON)
        self.api.failures["answerCallbackQuery"] = [BadRequest("Query is too old and response timeout expired or query id is invalid")]
        await self.click("product_magazine")
        draft = self.store.active_draft(111)
        self.assertEqual(draft["step"], "variant")
        self.assertEqual(draft["data"]["product_key"], "magazine")

    async def test_expired_callback_answer_still_refreshes_order_status(self):
        await self.reach_materials()
        await self.finish()
        await self.drain()
        self.api.failures["answerCallbackQuery"] = [BadRequest("Query is too old and response timeout expired or query id is invalid")]
        await self.click_raw("s:1:completed", user_id=777)
        self.assertEqual(self.store.order(1)["status"], "completed")
        self.assertTrue(any(method == "editMessageText" for method, _ in self.api.calls))

    async def inaccessible_callback(self, data, *, user_id=111, inline=False):
        self.update_id += 1
        query = {
            "id": str(self.update_id), "chat_instance": "local", "data": data,
            "from": {"id": user_id, "is_bot": False, "first_name": "Клиент"},
        }
        if inline:
            query["inline_message_id"] = "inline-message"
        else:
            query["message"] = {
                "message_id": 1000, "date": 0,
                "chat": {"id": user_id, "type": "private"},
            }
        await self.app.process_update(Update.de_json({"update_id": self.update_id, "callback_query": query}, self.app.bot))

    async def test_inaccessible_draft_message_preserves_draft(self):
        await self.send(ORDER_BUTTON)
        draft = self.store.active_draft(111)
        await self.inaccessible_callback(f"d:{draft['id']}:{draft['revision']}:cancel")
        self.assertEqual(self.store.active_draft(111), draft)

    async def test_inline_callback_without_chat_does_not_crash(self):
        await self.send(ORDER_BUTTON)
        draft = self.store.active_draft(111)
        await self.inaccessible_callback(f"d:{draft['id']}:{draft['revision']}:cancel", inline=True)
        self.assertEqual(self.store.active_draft(111), draft)

    async def test_inaccessible_status_message_does_not_change_order(self):
        await self.reach_materials()
        await self.finish()
        await self.inaccessible_callback("s:1:completed", user_id=777)
        self.assertEqual(self.store.order(1)["status"], "new")

    async def test_large_price_list_is_sent_without_telegram_length_errors(self):
        self.catalog.products["bulk"] = Product("bulk", "Дополнительные варианты", {
            f"v{i}": Variant(f"v{i}", "😀" * 100, 1500) for i in range(100)
        })
        start = len(self.api.calls)
        await self.send("/price")
        messages = [params["text"] for method, params in self.api.calls[start:] if method == "sendMessage"]
        self.assertGreater(len(messages), 1)
        self.assertEqual("".join(messages), self.catalog.price_list())
        self.assertTrue(all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in messages))


class TextSplitting(unittest.TestCase):
    def test_unicode_text_is_preserved_at_caption_boundary(self):
        text = "a" * 1023 + "😀" + "b" * 1024
        chunks = split_text(text, limit=1024)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(chunk.encode("utf-16-le")) // 2 <= 1024 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
