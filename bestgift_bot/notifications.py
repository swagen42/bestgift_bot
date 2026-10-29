from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import timedelta

from telegram import Bot
from telegram.constants import MessageLimit
from telegram.error import Forbidden, RetryAfter, TelegramError

from .config import Settings
from .presentation import MAIN_MENU, owner_text, split_text, status_keyboard
from .storage import Store, order_number

logger = logging.getLogger(__name__)


class Notifier:
    """Сохраненная очередь: одна отправка за шаг с фиксацией прогресса в SQLite."""

    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self._delivery_lock = asyncio.Lock()
        self._rate_limit_until = 0.0

    def start(self, bot: Bot) -> None:
        if self.task and not self.task.done():
            raise RuntimeError("Очередь уведомлений уже запущена")
        self.stop_event.clear()
        self.task = asyncio.create_task(self.run(bot), name="order-notifications")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            with suppress(asyncio.CancelledError):
                await self.task

    async def run(self, bot: Bot) -> None:
        while not self.stop_event.is_set():
            try:
                await self.deliver_step(bot)
            except Exception:
                logger.exception("Ошибка обработки очереди уведомлений")
            try:
                # Не чаще одного сообщения в секунду в чат владельца.
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.1)
            except TimeoutError:
                pass

    async def deliver_step(self, bot: Bot) -> bool:
        # Повторный вызов не должен отправить тот же незавершённый шаг параллельно.
        async with self._delivery_lock:
            if time.time() < self._rate_limit_until:
                return False
            return await self._deliver_step(bot)

    async def _deliver_step(self, bot: Bot) -> bool:
        order = self.store.next_delivery()
        if not order:
            return False
        order_id = order["id"]
        try:
            if not order["owner_message_id"]:
                message = await bot.send_message(
                    chat_id=order["owner_chat_id"],
                    text=owner_text(order, self.store.order_attachments(order_id), self.settings.timezone),
                    reply_markup=status_keyboard(order),
                    disable_web_page_preview=True,
                )
                self.store.delivery_progress(order_id, owner_message_id=message.message_id)
            elif not order["delivered"]:
                attachments = self.store.order_attachments(order_id)
                index = order["next_attachment"]
                if index < len(attachments):
                    await self.send_attachment(bot, order, attachments[index], index)
                    self.store.delivery_progress(order_id, next_attachment=index + 1)
                else:
                    self.store.delivery_progress(order_id, delivered=1)
                    logger.info("Заказ %s передан владельцу", order_number(order_id))
            else:
                await bot.send_message(
                    chat_id=order["chat_id"],
                    text=(f"Заявка {order_number(order_id)} отправлена мастеру вместе с материалами. "
                          "Мастер свяжется с вами, чтобы обсудить детали, оплату и доставку."),
                    reply_markup=MAIN_MENU,
                )
                self.store.delivery_progress(order_id, client_notified=1)
            self.store.delivery_progress(order_id, attempts=0, retry_at=0, last_error=None)
        except TelegramError as exc:
            if order["delivered"] and isinstance(exc, Forbidden):
                # Клиент заблокировал бота, но владелец уже получил заказ.
                self.store.delivery_progress(order_id, client_notified=1, last_error=type(exc).__name__)
                return True
            attempts = order["attempts"] + 1
            delay = min(900, 5 * 2 ** min(attempts - 1, 8))
            if isinstance(exc, RetryAfter):
                retry_after = exc.retry_after
                delay = (retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after) + 1
                # Ограничение Telegram затрагивает и другие заказы этой очереди.
                self._rate_limit_until = time.time() + delay
            # Не сохраняем текст исключения: он может содержать URL API с токеном.
            self.store.delivery_progress(
                order_id, attempts=attempts, retry_at=time.time() + delay, last_error=type(exc).__name__
            )
            logger.warning("Доставка %s: %s, повтор через %s с", order_number(order_id), type(exc).__name__, delay)
        return True

    @staticmethod
    async def send_attachment(bot: Bot, order: dict, attachment: dict, index: int) -> None:
        kind = attachment["kind"]
        # file_id позволяет передать файл без скачивания и без исходного сообщения.
        options = {
            "chat_id": order["owner_chat_id"],
            kind: attachment["file_id"],
            "reply_to_message_id": order["owner_message_id"],
            "allow_sending_without_reply": True,
        }
        label = f"{order_number(order['id'])} · Материал {index + 1}"
        caption = attachment.get("caption") or ""
        # Клиенты Telegram могут присылать более длинные подписи, чем Bot API
        # разрешает отправлять боту. Такой текст передаём отдельными сообщениями.
        long_caption = len(caption.encode("utf-16-le")) // 2 > MessageLimit.CAPTION_LENGTH
        if kind not in {"sticker", "video_note"}:
            options["caption"] = label if long_caption else caption or label
        await getattr(bot, f"send_{kind}")(**options)
        if long_caption or (caption and kind in {"sticker", "video_note"}):
            for chunk in split_text(f"{label} · Подпись:\n{caption}"):
                await bot.send_message(
                    chat_id=order["owner_chat_id"], text=chunk,
                    reply_to_message_id=order["owner_message_id"],
                    allow_sending_without_reply=True,
                    disable_web_page_preview=True,
                )
