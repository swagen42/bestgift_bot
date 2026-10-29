from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .catalog import Catalog, money
from .config import Settings
from .presentation import (
    CONTACT_BUTTON, KINDS, MAIN_MENU, ORDER_BUTTON, PRICE_BUTTON,
    draft_keyboard, split_text, owner_text, status_keyboard, summary,
)
from .storage import STATUSES, StaleDraft, Store, order_number

logger = logging.getLogger(__name__)


def clean_text(value: str) -> str:
    return "".join(char for char in value.strip() if char >= " " or char == "\n")


def attachment_from_message(message) -> dict | None:
    for kind in KINDS:
        media = getattr(message, kind, None)
        if media:
            if kind == "photo":
                media = media[-1]
            return {
                "source_chat_id": message.chat_id,
                "source_message_id": message.message_id,
                "kind": kind,
                "file_id": media.file_id,
                "file_unique_id": media.file_unique_id,
                "file_name": getattr(media, "file_name", None),
                "caption": message.caption,
            }
    return None


class BotHandlers:
    def __init__(self, store: Store, catalog: Catalog, settings: Settings):
        self.store = store
        self.catalog = catalog
        self.settings = settings

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("order", self.order))
        app.add_handler(CommandHandler("cancel", self.cancel))
        app.add_handler(CommandHandler("price", self.price))
        app.add_handler(CommandHandler("contact", self.contact))
        app.add_handler(CommandHandler("id", self.identify))
        app.add_handler(CallbackQueryHandler(self.status_callback, pattern=r"^s:"))
        app.add_handler(CallbackQueryHandler(self.draft_callback, pattern=r"^d:"))
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.COMMAND, self.unknown_command))
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE, self.message))

    @staticmethod
    async def private(update: Update) -> bool:
        if update.effective_chat and update.effective_chat.type == "private":
            return True
        if update.effective_message:
            await update.effective_message.reply_text("Для оформления заказа откройте личный чат с ботом и нажмите /start.")
        return False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.private(update):
            return
        await update.effective_message.reply_text(
            "Здравствуйте! Здесь можно заказать персональный журнал или коллаж. "
            "Вы выберете вариант, добавите пожелания и материалы, затем проверите заявку. "
            "Детали, оплату и доставку вы обсудите с мастером лично.",
            reply_markup=MAIN_MENU,
        )
        draft = self.store.active_draft(update.effective_user.id)
        if draft:
            await update.effective_message.reply_text("У вас есть незавершённая заявка. Продолжим с сохранённого шага.")
            await self.render(update, draft)

    async def order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.private(update):
            return
        draft = self.store.start_draft(update.effective_user.id, update.effective_chat.id)
        await self.render(update, draft)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.private(update):
            return
        cancelled = self.store.cancel_draft(update.effective_user.id)
        await update.effective_message.reply_text(
            "Оформление отменено. Новую заявку можно начать из меню." if cancelled else "Сейчас нет незавершённой заявки.",
            reply_markup=MAIN_MENU,
        )

    async def price(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.private(update):
            for chunk in split_text(self.catalog.price_list()):
                await update.effective_message.reply_text(chunk, reply_markup=MAIN_MENU)

    async def contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.private(update):
            await update.effective_message.reply_text(
                f"Напишите мастеру: @{self.settings.owner_contact}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Написать мастеру", url=f"https://t.me/{self.settings.owner_contact}")
                ]]),
            )

    async def identify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            f"ID этого чата: {update.effective_chat.id}\nВаш Telegram ID: {update.effective_user.id}"
        )

    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "Доступны /start, /order, /price, /contact, /cancel и /id. Данные заявки сохранены."
        )

    def refresh_quote(self, draft: dict) -> tuple[dict, bool]:
        data = draft["data"]
        product = self.catalog.products.get(data.get("product_key"))
        variant = product.variants.get(data.get("variant_key")) if product else None
        if not product or not variant:
            return self.store.update_draft(draft, step="product"), True
        current = {
            "product_title": product.title,
            "variant_title": variant.title,
            "price_rub": variant.price,
        }
        if any(data.get(key) != value for key, value in current.items()):
            return self.store.update_draft(draft, **current), True
        return draft, False

    async def render(self, update: Update, draft: dict) -> None:
        message = update.effective_message
        step = draft["step"]
        data = draft["data"]
        if step == "product":
            rows = [[(p.title, f"product_{p.key}")] for p in self.catalog.products.values()]
            text = "Что хотите заказать?"
        elif step == "variant":
            product = self.catalog.products.get(data.get("product_key"))
            if not product:
                draft = self.store.update_draft(draft, step="product")
                await self.render(update, draft)
                return
            rows = [[(f"{v.title} — {money(v.price)}", f"variant_{v.key}")] for v in product.variants.values()]
            text = f"{product.title}. Выберите вариант:"
        elif step == "name":
            text = f"Вы выбрали: {data['product_title']}, {data['variant_title']} — {money(data['price_rub'])}.\n\nКак вас зовут? Отправьте имя (2–80 символов)."
            rows = []
            if data.get("customer_name"):
                text += f"\nСохранённое имя: {data['customer_name']}"
                rows = [[("Оставить имя", "keep")]]
        elif step == "contact":
            text = (
                "У вас нет Telegram username. Чтобы мастер смог связаться с вами, "
                "отправьте контакт Telegram, телефон или другой способ связи (до 160 символов). "
                "Можно пропустить, но ссылка на профиль по ID может быть недоступна из-за настроек Telegram."
            )
            rows = [[("Пропустить", "skip")]]
            if data.get("contact"):
                text += f"\nСохранённый контакт: {data['contact']}"
                rows.insert(0, [("Оставить контакт", "keep")])
        elif step == "comment":
            text = "Какие у вас пожелания к заказу? Отправьте комментарий одним сообщением (до 1000 символов) или пропустите."
            rows = [[("Без комментария", "skip")]]
            if data.get("comment"):
                text += f"\n\nСохранённый комментарий: {data['comment']}"
                rows.insert(0, [("Оставить комментарий", "keep")])
        elif step == "materials":
            count = len(self.store.attachments(draft["id"]))
            text = (
                f"Приложить материалы к заказу? Можно отправить фото, видео, документы, аудио, "
                f"голосовые сообщения, анимации, видеосообщения и стикеры.\n"
                f"Добавлено: {count} из {self.settings.max_attachments}. "
                "Дождитесь подтверждения приёма всех файлов, затем нажмите «Готово»."
            )
            rows = [[("Готово" if count else "Пропустить материалы", "done")]]
            if count:
                rows.append([("Удалить все материалы", "clear")])
        elif step == "review":
            draft, changed = self.refresh_quote(draft)
            if draft["step"] != "review":
                await message.reply_text("Выбранный вариант больше недоступен. Выберите товар заново; остальные данные сохранены.")
                await self.render(update, draft)
                return
            text = "Проверьте заявку перед отправкой:\n\n" + summary(draft["data"], self.store.attachments(draft["id"]))
            if changed:
                text = "Каталог изменился. Проверьте актуальные параметры и цену.\n\n" + text
            text += "\n\nНажимая «Отправить заявку», вы передаёте указанные данные и материалы мастеру для оформления заказа."
            rows = [
                [("Отправить заявку", "confirm")],
                [("Изменить товар", "edit_product"), ("Изменить имя", "edit_name")],
                [("Изменить комментарий", "edit_comment"), ("Изменить материалы", "edit_materials")],
                [("Изменить контакт", "edit_contact")],
            ]
        else:
            raise ValueError("Неизвестный шаг черновика")
        await message.reply_text(text, reply_markup=draft_keyboard(draft, rows, back=step != "product"))

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        text = clean_text(message.text or "")
        menu_handlers = {ORDER_BUTTON: self.order, PRICE_BUTTON: self.price, CONTACT_BUTTON: self.contact}
        if text in menu_handlers:
            await menu_handlers[text](update, context)
            return
        draft = self.store.active_draft(update.effective_user.id)
        if not draft:
            await message.reply_text("Чтобы начать оформление, нажмите «Оформить заказ».", reply_markup=MAIN_MENU)
            return
        step = draft["step"]
        if step == "name":
            if not message.text or not 2 <= len(text) <= 80 or "\n" in text:
                await message.reply_text("Отправьте имя текстом в одну строку: от 2 до 80 символов.")
                return
            next_step = "comment" if update.effective_user.username else "contact"
            draft = self.store.update_draft(draft, step=next_step, customer_name=text)
        elif step == "contact":
            if message.contact:
                if message.contact.user_id and message.contact.user_id != update.effective_user.id:
                    await message.reply_text("Отправьте свой контакт, укажите способ связи текстом или нажмите «Пропустить».")
                    return
                text = message.contact.phone_number
            if not 3 <= len(text) <= 160 or "\n" in text:
                await message.reply_text("Отправьте контакт или текст в одну строку: 3–160 символов. Либо нажмите «Пропустить».")
                return
            draft = self.store.update_draft(draft, step="comment", contact=text)
        elif step == "comment":
            if not message.text or not text or len(text) > 1000:
                await message.reply_text("Нужен текстовый комментарий до 1000 символов. Файлы можно будет добавить на следующем шаге.")
                return
            draft = self.store.update_draft(draft, step="materials", comment=text)
        elif step == "materials":
            attachment = attachment_from_message(message)
            if not attachment:
                await message.reply_text("Отправьте фото или файл. Когда закончите, нажмите «Готово». Пожелания можно исправить на экране проверки.")
                return
            result = self.store.add_attachment(draft, attachment, self.settings.max_attachments)
            if result == "duplicate":
                return
            if result == "limit":
                await message.reply_text(f"Достигнут лимит: {self.settings.max_attachments} материалов. Нажмите «Готово» или удалите материалы.")
                return
            # Одно подтверждение на элемент альбома; не отправляем новый полный экран каждый раз.
            count = len(self.store.attachments(draft["id"]))
            await message.reply_text(
                f"Материал принят. Добавлено: {count}. Можно отправить ещё или нажать «Готово».",
                reply_markup=draft_keyboard(draft, [[("Готово", "done"), ("Удалить все", "clear")]]),
            )
            return
        else:
            await message.reply_text("Выберите действие кнопкой под сообщением. /start восстановит текущий шаг.")
            return
        await self.render(update, draft)

    async def draft_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not update.effective_chat or update.effective_chat.type != "private":
            await self.answer_callback(query, "Оформление доступно в личном чате с ботом.", show_alert=True)
            return
        if not query.message or not query.message.is_accessible:
            await self.answer_callback(query, "Этот экран недоступен. Откройте /start, чтобы продолжить заявку.", show_alert=True)
            return
        try:
            _, draft_id, revision_text, action = query.data.split(":", 3)
            revision = int(revision_text)
        except (ValueError, AttributeError):
            await self.answer_callback(query, "Некорректная кнопка. Откройте /start.")
            return
        draft = self.store.active_draft(update.effective_user.id)
        if not draft or draft["id"] != draft_id:
            order = self.store.order_for_draft(draft_id, update.effective_user.id)
            if order:
                await self.answer_callback(query, f"Заявка {order_number(order['id'])} уже принята. Повторная отправка не нужна.", show_alert=True)
            else:
                await self.answer_callback(query, "Это оформление уже завершено или отменено. Откройте /start.")
            return
        if draft["revision"] != revision:
            await self.answer_callback(query, "Это предыдущий экран. Используйте кнопки последнего сообщения.")
            await self.render(update, draft)
            return
        await self.answer_callback(query)
        step = draft["step"]
        try:
            if action == "cancel":
                await self.cancel(update, context)
                return
            if action == "back":
                previous = {
                    "variant": "product", "name": "variant", "contact": "name",
                    "comment": "contact" if not update.effective_user.username else "name",
                    "materials": "comment", "review": "materials",
                }.get(step)
                if previous:
                    draft = self.store.update_draft(draft, step=previous)
            elif step == "product" and action.startswith("product_"):
                key = action.removeprefix("product_")
                product = self.catalog.products.get(key)
                if not product:
                    await self.render(update, draft)
                    return
                draft = self.store.update_draft(
                    draft, step="variant", product_key=key, product_title=product.title,
                    variant_key=None, variant_title=None, price_rub=None,
                )
            elif step == "variant" and action.startswith("variant_"):
                product = self.catalog.products.get(draft["data"].get("product_key"))
                variant = product.variants.get(action.removeprefix("variant_")) if product else None
                if not variant:
                    await self.render(update, draft)
                    return
                draft = self.store.update_draft(
                    draft, step="name", variant_key=variant.key, variant_title=variant.title, price_rub=variant.price
                )
            elif action == "keep" and step in {"name", "contact", "comment"}:
                field = {"name": "customer_name", "contact": "contact", "comment": "comment"}[step]
                if draft["data"].get(field):
                    following = {
                        "name": "comment" if update.effective_user.username else "contact",
                        "contact": "comment", "comment": "materials",
                    }[step]
                    draft = self.store.update_draft(draft, step=following)
            elif action == "skip" and step in {"contact", "comment"}:
                field = "contact" if step == "contact" else "comment"
                draft = self.store.update_draft(draft, step="comment" if step == "contact" else "materials", **{field: ""})
            elif step == "materials" and action == "clear":
                self.store.clear_attachments(draft)
                draft = self.store.update_draft(draft)
            elif step == "materials" and action == "done":
                draft = self.store.update_draft(draft, step="review")
            elif step == "review" and action.startswith("edit_"):
                target = action.removeprefix("edit_")
                if target in {"product", "name", "contact", "comment", "materials"}:
                    draft = self.store.update_draft(draft, step=target)
            elif step == "review" and action == "confirm":
                draft, changed = self.refresh_quote(draft)
                if changed:
                    await update.effective_message.reply_text("Каталог изменился. Проверьте цену и подтвердите актуальную заявку.")
                    await self.render(update, draft)
                    return
                order, _ = self.store.confirm(
                    draft, username=update.effective_user.username,
                    telegram_name=update.effective_user.full_name, owner_chat_id=self.settings.owner_chat_id,
                )
                logger.info("Сохранен заказ %s", order_number(order["id"]))
                await update.effective_message.reply_text(
                    f"Заявка {order_number(order['id'])} сохранена. Передаю её мастеру. "
                    "Когда все материалы будут доставлены, пришлю подтверждение. Повторять отправку не нужно.",
                    reply_markup=MAIN_MENU,
                )
                return
            await self.render(update, draft)
        except StaleDraft:
            await update.effective_message.reply_text("Заявка уже изменилась. Нажмите /start, чтобы открыть текущий шаг.")

    async def status_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if update.effective_user.id not in self.settings.admin_user_ids:
            await self.answer_callback(query, "Менять статус может только владелец или администратор.", show_alert=True)
            return
        if not update.effective_chat or not query.message or not query.message.is_accessible:
            await self.answer_callback(query, "Сообщение недоступно. Откройте актуальное уведомление о заказе.", show_alert=True)
            return
        try:
            _, number, status = query.data.split(":", 2)
            order = self.store.order(int(number))
        except (ValueError, AttributeError):
            order = None
            status = ""
        if not order or status not in STATUSES or update.effective_chat.id != order["owner_chat_id"]:
            await self.answer_callback(query, "Заказ не найден в этом чате.", show_alert=True)
            return
        order = self.store.set_status(order["id"], status, update.effective_user.id)
        await self.answer_callback(query, f"Статус: {STATUSES[status]}")
        try:
            await query.edit_message_text(
                owner_text(order, self.store.order_attachments(order["id"]), self.settings.timezone),
                reply_markup=status_keyboard(order), disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Статус сохранен, но экран заказа %s не обновлен: %s", order["id"], type(exc).__name__)

    @staticmethod
    async def answer_callback(query, text: str | None = None, *, show_alert: bool = False) -> None:
        # Telegram хранит callback недолго. После простоя ответ на него может
        # истечь, но само действие по актуальной revision всё ещё допустимо.
        try:
            await query.answer(text, show_alert=show_alert)
        except BadRequest as exc:
            error = str(exc).lower()
            if "query is too old" not in error and "query id is invalid" not in error:
                raise
            logger.info("Ответ на устаревшее нажатие уже недоступен")

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        # Не журналируем Update: в нем находятся имена, контакты и материалы клиента.
        logger.error("Ошибка обработчика: %s", type(error).__name__, exc_info=error)
        if isinstance(update, Update) and update.effective_message and update.effective_chat.type == "private":
            try:
                await update.effective_message.reply_text(
                    "Не удалось выполнить действие. Сохранённые данные доступны через /start. "
                    "Если заявка уже подтверждена, повторно отправлять её не нужно."
                )
            except TelegramError:
                logger.warning("Не удалось сообщить клиенту об ошибке Telegram API")
