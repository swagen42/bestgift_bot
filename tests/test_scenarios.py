"""Сквозные проверки с настоящими PTB Update/Dispatcher и локальным Telegram API."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault("PTB_TIMEDELTA", "1")

from telegram import Update
from telegram.error import Forbidden, NetworkError, RetryAfter
from telegram.ext import Application
from telegram.request import BaseRequest

from bestgift_bot.catalog import Catalog
from bestgift_bot.config import PROJECT_ROOT, Settings
from bestgift_bot.handlers import BotHandlers
from bestgift_bot.notifications import Notifier
from bestgift_bot.presentation import ORDER_BUTTON
from bestgift_bot.storage import Store

BOT_USER = {"id": 900, "is_bot": True, "first_name": "BestGift", "username": "bestgift_test_bot"}


class LocalTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self.message_id = 1000
        self.failures = {}

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((api_method, params))
        if self.failures.get(api_method):
            raise self.failures[api_method].pop(0)
        if api_method == "getMe":
            result = BOT_USER
        elif api_method in {"answerCallbackQuery", "setMyCommands"}:
            result = True
        else:
            self.message_id += 1
            chat_id = int(params["chat_id"])
            result = {
                "message_id": params.get("message_id", self.message_id),
                "date": 1_790_000_000,
                "from": BOT_USER,
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                "text": params.get("text", ""),
            }
            if "inline_keyboard" in params.get("reply_markup", {}):
                result["reply_markup"] = params["reply_markup"]
        return 200, json.dumps({"ok": True, "result": result}).encode()


class Scenarios(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "orders.sqlite3"
        self.store = Store(self.path)
        self.catalog = Catalog.load(PROJECT_ROOT / "catalog.toml")
        self.settings = Settings(
            token="123456789:abcdefghijklmnopqrstuvwxyz123456789",
            owner_chat_id=777,
            admin_user_ids=frozenset({777}),
            owner_contact="craft_master",
            database_path=self.path,
            catalog_path=PROJECT_ROOT / "catalog.toml",
            timezone=ZoneInfo("Asia/Bangkok"),
            max_attachments=3,
        )
        self.api = LocalTelegram()
        self.app = (Application.builder().token(self.settings.token)
                    .request(self.api).get_updates_request(LocalTelegram()).build())
        self.handlers = BotHandlers(self.store, self.catalog, self.settings)
        self.handlers.register(self.app)
        self.errors = []

        async def collect_error(update, context):
            self.errors.append(context.error)

        self.app.add_error_handler(collect_error)
        self.notifier = Notifier(self.store, self.settings)
        await self.app.initialize()
        self.update_id = 0

    async def asyncTearDown(self):
        await self.app.shutdown()
        self.store.close()
        self.temp.cleanup()
        self.assertEqual(self.errors, [], f"Ошибки обработчика: {self.errors}")

    async def send(self, text=None, *, user_id=111, username="client", message_id=None, **media):
        self.update_id += 1
        user = {"id": user_id, "is_bot": False, "first_name": "Клиент"}
        if username:
            user["username"] = username
        message = {
            "message_id": message_id or self.update_id,
            "date": 1_790_000_000, "from": user,
            "chat": {"id": user_id, "type": "private"}, **media,
        }
        if text is not None:
            message["text"] = text
            if text.startswith("/"):
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(Update.de_json({"update_id": self.update_id, "message": message}, self.app.bot))

    async def click_raw(self, data, *, user_id=111, username="client", chat_id=None):
        self.update_id += 1
        user = {"id": user_id, "is_bot": False, "first_name": "Клиент"}
        if username:
            user["username"] = username
        payload = {
            "update_id": self.update_id,
            "callback_query": {
                "id": str(self.update_id), "chat_instance": "local", "from": user, "data": data,
                "message": {
                    "message_id": 1000, "date": 1_790_000_000, "from": BOT_USER,
                    "chat": {"id": chat_id or user_id, "type": "private"}, "text": "Экран бота",
                },
            },
        }
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    async def click(self, action, *, user_id=111, username="client"):
        # Нажимаем кнопку, которую обработчик действительно отправил клиенту.
        for method, params in reversed(self.api.calls):
            if method != "sendMessage" or params.get("chat_id") != user_id:
                continue
            markup = params.get("reply_markup", {})
            for row in markup.get("inline_keyboard", []):
                for button in row:
                    data = button.get("callback_data", "")
                    if data.startswith("d:") and data.rsplit(":", 1)[-1] == action:
                        self.assertLessEqual(len(data.encode()), 64)
                        await self.click_raw(data, user_id=user_id, username=username)
                        return data
        self.fail(f"Кнопка {action} не найдена")

    async def reach_materials(self, product="magazine", variant="16", *, username="client"):
        await self.send("/start", username=username)
        await self.send(ORDER_BUTTON, username=username)
        await self.click(f"product_{product}", username=username)
        await self.click(f"variant_{variant}", username=username)
        await self.send("Анна", username=username)
        if not username:
            self.assertEqual(self.store.active_draft(111)["step"], "contact")
            await self.send("+7 900 123-45-67", username=username)
        await self.send("Подарок на день рождения", username=username)
        self.assertEqual(self.store.active_draft(111)["step"], "materials")

    async def finish(self, *, username="client"):
        await self.click("done", username=username)
        self.assertEqual(self.store.active_draft(111)["step"], "review")
        data = await self.click("confirm", username=username)
        self.assertIsNone(self.store.active_draft(111))
        return data

    async def drain(self):
        for _ in range(30):
            if not await self.notifier.deliver_step(self.app.bot):
                return
        self.fail("Очередь уведомлений не опустела")

    async def test_magazine_full_flow_materials_delivery_and_duplicate_confirm(self):
        await self.reach_materials()
        await self.send(photo=[{"file_id": "photo1", "file_unique_id": "p1", "width": 640, "height": 480}], caption="Обложка")
        await self.send(document={"file_id": "doc1", "file_unique_id": "d1", "file_name": "тексты.pdf"})
        self.assertIsNone(self.store.order(1))  # До подтверждения заказа нет.
        callback = await self.finish()
        await self.click_raw(callback)
        self.assertIsNone(self.store.order(2))
        order = self.store.order(1)
        self.assertEqual((order["price_rub"], order["customer_name"], order["status"]), (2350, "Анна", "new"))
        self.assertEqual(len(self.store.order_attachments(1)), 2)
        await self.drain()
        self.assertEqual(self.store.order(1)["client_notified"], 1)
        owner_messages = [p for m, p in self.api.calls if m == "sendMessage" and p["chat_id"] == 777]
        self.assertEqual(len(owner_messages), 1)
        self.assertIn("BG-000001", owner_messages[0]["text"])
        self.assertIn("https://t.me/client", owner_messages[0]["text"])
        self.assertIn("Asia/Bangkok", owner_messages[0]["text"])
        self.assertEqual([p["photo"] for m, p in self.api.calls if m == "sendPhoto"], ["photo1"])
        self.assertEqual([p["document"] for m, p in self.api.calls if m == "sendDocument"], ["doc1"])

    async def test_collage_without_username_and_without_attachments(self):
        await self.reach_materials("collage", "a4", username=None)
        await self.finish(username=None)
        await self.drain()
        order = self.store.order(1)
        self.assertEqual(order["price_rub"], 1700)
        self.assertIsNone(order["username"])
        self.assertEqual(order["contact"], "+7 900 123-45-67")
        self.assertEqual(self.store.order_attachments(1), [])
        text = next(p["text"] for m, p in self.api.calls if m == "sendMessage" and p["chat_id"] == 777)
        self.assertIn("Telegram ID: 111", text)
        self.assertIn("tg://user?id=111", text)

    async def test_draft_and_materials_survive_database_reopening(self):
        await self.reach_materials()
        await self.send(document={"file_id": "resume", "file_unique_id": "r"})
        draft_id = self.store.active_draft(111)["id"]
        self.store.close()
        self.store = Store(self.path)
        self.handlers.store = self.store
        self.notifier.store = self.store
        await self.send("/start")
        self.assertEqual(self.store.active_draft(111)["id"], draft_id)
        self.assertEqual(len(self.store.attachments(draft_id)), 1)
        await self.finish()
        await self.drain()
        self.assertEqual(self.store.order(1)["delivered"], 1)

    async def test_pending_delivery_resumes_without_resending_sent_parts(self):
        await self.reach_materials()
        await self.send(document={"file_id": "resume", "file_unique_id": "r"})
        await self.finish()
        await self.notifier.deliver_step(self.app.bot)  # Сводка уже отправлена.
        self.store.close()
        self.store = Store(self.path)
        self.handlers.store = self.store
        self.notifier = Notifier(self.store, self.settings)
        await self.drain()
        messages = [p for m, p in self.api.calls if m == "sendMessage" and p["chat_id"] == 777]
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.store.order(1)["next_attachment"], 1)

    async def test_notification_retries_network_error_and_telegram_rate_limit(self):
        await self.reach_materials()
        await self.send(document={"file_id": "retry", "file_unique_id": "retry"})
        await self.finish()
        self.api.failures["sendMessage"] = [NetworkError("temporary")]
        await self.notifier.deliver_step(self.app.bot)
        self.assertEqual(self.store.order(1)["last_error"], "NetworkError")
        self.assertGreater(self.store.order(1)["retry_at"], 0)
        self.store.delivery_progress(1, retry_at=0)
        await self.notifier.deliver_step(self.app.bot)
        self.api.failures["sendDocument"] = [RetryAfter(2)]
        await self.notifier.deliver_step(self.app.bot)
        self.assertEqual(self.store.order(1)["next_attachment"], 0)
        self.assertEqual(self.store.order(1)["last_error"], "RetryAfter")
        self.store.delivery_progress(1, retry_at=0)
        # RetryAfter приостанавливает всю очередь, а не только текущий заказ.
        with patch("bestgift_bot.notifications.time.time", return_value=time.time() + 10):
            await self.drain()
        self.assertEqual(self.store.order(1)["client_notified"], 1)

    async def test_status_buttons_require_admin_and_configured_chat(self):
        await self.reach_materials()
        await self.finish()
        await self.drain()
        await self.click_raw("s:1:completed", user_id=111, chat_id=777)
        self.assertEqual(self.store.order(1)["status"], "new")
        await self.click_raw("s:1:completed", user_id=777, chat_id=555)
        self.assertEqual(self.store.order(1)["status"], "new")
        await self.click_raw("s:1:in_progress", user_id=777)
        self.assertEqual(self.store.order(1)["status"], "in_progress")
        self.assertEqual(self.store.order(1)["status_updated_by"], 777)
        self.assertTrue(any(m == "editMessageText" for m, _ in self.api.calls))

    async def test_stale_and_other_users_buttons_cannot_change_draft(self):
        await self.send(ORDER_BUTTON)
        old = await self.click("product_magazine")
        draft = self.store.active_draft(111)
        await self.click_raw(old)
        self.assertEqual(self.store.active_draft(111)["step"], "variant")
        await self.click_raw(f"d:{draft['id']}:{draft['revision']}:cancel", user_id=222)
        self.assertIsNotNone(self.store.active_draft(111))
        self.assertIsNone(self.store.active_draft(222))

    async def test_validation_back_and_cancellation(self):
        await self.send(ORDER_BUTTON)
        await self.click("product_magazine")
        await self.click("variant_12")
        await self.send("А")
        self.assertEqual(self.store.active_draft(111)["step"], "name")
        await self.send("Анна")
        await self.send("x" * 1001)
        self.assertEqual(self.store.active_draft(111)["step"], "comment")
        await self.click("back")
        self.assertEqual(self.store.active_draft(111)["data"]["customer_name"], "Анна")
        await self.send("/cancel")
        self.assertIsNone(self.store.active_draft(111))
        self.assertIsNone(self.store.order(1))

    async def test_material_limit_duplicate_and_clear(self):
        await self.reach_materials()
        doc = {"file_id": "f1", "file_unique_id": "u1"}
        await self.send(message_id=90, document=doc)
        await self.send(message_id=90, document=doc)
        await self.send(document={"file_id": "f2", "file_unique_id": "u2"})
        await self.send(document={"file_id": "f3", "file_unique_id": "u3"})
        await self.send(document={"file_id": "f4", "file_unique_id": "u4"})
        draft = self.store.active_draft(111)
        self.assertEqual(len(self.store.attachments(draft["id"])), 3)
        await self.click("clear")
        self.assertEqual(self.store.attachments(draft["id"]), [])

    async def test_editing_review_preserves_other_data_and_materials(self):
        await self.reach_materials()
        await self.send(document={"file_id": "edit", "file_unique_id": "edit"})
        await self.click("done")
        await self.click("edit_name")
        await self.send("Мария")
        await self.click("keep")
        await self.click("done")
        data = self.store.active_draft(111)["data"]
        self.assertEqual(data["customer_name"], "Мария")
        self.assertEqual(data["comment"], "Подарок на день рождения")
        await self.click("confirm")
        self.assertEqual(len(self.store.order_attachments(1)), 1)

    async def test_price_change_requires_review_before_confirmation(self):
        from dataclasses import replace

        await self.reach_materials()
        await self.click("done")
        product = self.catalog.products["magazine"]
        product.variants["16"] = replace(product.variants["16"], price=2500)
        await self.click("confirm")
        self.assertIsNone(self.store.order(1))
        self.assertEqual(self.store.active_draft(111)["data"]["price_rub"], 2500)
        await self.click("confirm")
        self.assertEqual(self.store.order(1)["price_rub"], 2500)

    async def test_client_blocking_bot_does_not_block_queue(self):
        await self.reach_materials()
        await self.finish()
        await self.notifier.deliver_step(self.app.bot)
        await self.notifier.deliver_step(self.app.bot)
        self.api.failures["sendMessage"] = [Forbidden("blocked")]
        await self.notifier.deliver_step(self.app.bot)
        self.assertEqual(self.store.order(1)["delivered"], 1)
        self.assertEqual(self.store.order(1)["client_notified"], 1)
        self.assertIsNone(self.store.next_delivery())

    async def test_username_missing_can_skip_contact_and_comment(self):
        await self.send(ORDER_BUTTON, username=None)
        await self.click("product_collage", username=None)
        await self.click("variant_a4", username=None)
        await self.send("Иван", username=None)
        await self.click("skip", username=None)
        await self.click("skip", username=None)
        await self.finish(username=None)
        self.assertEqual(self.store.order(1)["comment"], "")
        self.assertEqual(self.store.order(1)["contact"], "")

    async def test_all_supported_material_types_are_forwarded_by_file_id(self):
        from dataclasses import replace

        self.handlers.settings = replace(self.settings, max_attachments=20)
        await self.reach_materials()
        samples = {
            "photo": [{"file_id": "photo", "file_unique_id": "photo", "width": 10, "height": 10}],
            "document": {"file_id": "document", "file_unique_id": "document"},
            "video": {"file_id": "video", "file_unique_id": "video", "width": 10, "height": 10, "duration": 1},
            "audio": {"file_id": "audio", "file_unique_id": "audio", "duration": 1},
            "voice": {"file_id": "voice", "file_unique_id": "voice", "duration": 1},
            "animation": {"file_id": "animation", "file_unique_id": "animation", "width": 10, "height": 10, "duration": 1},
            "video_note": {"file_id": "video_note", "file_unique_id": "video_note", "length": 10, "duration": 1},
            "sticker": {"file_id": "sticker", "file_unique_id": "sticker", "width": 10, "height": 10,
                        "type": "regular", "is_animated": False, "is_video": False},
        }
        for kind, payload in samples.items():
            await self.send(**{kind: payload})
        await self.finish()
        await self.drain()
        for kind in samples:
            method = "send" + "".join(word.title() for word in kind.split("_"))
            self.assertTrue(any(m == method and p.get(kind) == kind for m, p in self.api.calls), method)

    async def test_animation_is_detected_before_compatibility_document_and_caption_is_preserved(self):
        await self.reach_materials()
        caption = "Я" * 1024
        await self.send(
            animation={"file_id": "gif", "file_unique_id": "gif", "width": 20, "height": 20, "duration": 1},
            document={"file_id": "gif", "file_unique_id": "gif", "file_name": "animation.gif"},
            caption=caption,
        )
        await self.finish()
        self.assertEqual(self.store.order_attachments(1)[0]["kind"], "animation")
        await self.drain()
        sent = next(p for m, p in self.api.calls if m == "sendAnimation")
        self.assertEqual(sent["caption"], caption)


if __name__ == "__main__":
    unittest.main()
