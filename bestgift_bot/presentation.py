from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .catalog import money
from .storage import STATUSES, order_number

ORDER_BUTTON = "Оформить заказ"
PRICE_BUTTON = "Прайс"
CONTACT_BUTTON = "Связаться с мастером"
MAIN_MENU = ReplyKeyboardMarkup(
    [[ORDER_BUTTON], [PRICE_BUTTON, CONTACT_BUTTON]], resize_keyboard=True
)
KINDS = {
    "photo": "Фото", "animation": "Анимация", "video": "Видео", "audio": "Аудио",
    "voice": "Голосовое сообщение", "video_note": "Видеосообщение",
    "sticker": "Стикер", "document": "Файл",
}


def split_text(text: str, limit: int = 4096) -> list[str]:
    """Разбить обычный текст по лимиту Telegram, не разрывая Unicode символы."""
    if limit < 2:
        raise ValueError("Лимит должен вмещать хотя бы один Unicode символ (2 единицы UTF-16).")
    chunks = []
    current = []
    size = 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > limit:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(char)
        size += units
    if current:
        chunks.append("".join(current))
    return chunks


def draft_keyboard(draft: dict, rows: list[list[tuple[str, str]]], *, back: bool = True) -> InlineKeyboardMarkup:
    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"d:{draft['id']}:{draft['revision']}:{action}")

    buttons = [[button(label, action) for label, action in row] for row in rows]
    navigation = []
    if back:
        navigation.append(button("← Назад", "back"))
    navigation.append(button("Отмена", "cancel"))
    buttons.append(navigation)
    return InlineKeyboardMarkup(buttons)


def attachment_summary(attachments: list[dict]) -> str:
    if not attachments:
        return "Материалы: не приложены"
    lines = [f"Материалы: {len(attachments)} шт."]
    for index, item in enumerate(attachments[:5], 1):
        label = (item.get("file_name") or KINDS.get(item["kind"], "Файл")).replace("\n", " ")
        lines.append(f"{index}. {label[:32]}")
    if len(attachments) > 5:
        lines.append(f"…и ещё {len(attachments) - 5} шт. Все материалы будут переданы мастеру.")
    return "\n".join(lines)


def summary(data: dict, attachments: list[dict]) -> str:
    return "\n".join([
        f"Товар: {data['product_title']}",
        f"Вариант: {data['variant_title']}",
        f"Цена: {money(data['price_rub'])}",
        f"Имя: {data['customer_name']}",
        f"Контакт: {data.get('contact') or 'Telegram-профиль'}",
        f"Комментарий: {data.get('comment') or 'Без комментария'}",
        "", attachment_summary(attachments),
    ])


def owner_text(order: dict, attachments: list[dict], timezone: ZoneInfo) -> str:
    created = datetime.fromisoformat(order["created_at"]).astimezone(timezone)
    if order["username"]:
        profile = f"@{order['username']} — https://t.me/{order['username']}"
    else:
        profile = f"tg://user?id={order['user_id']} (доступность зависит от настроек Telegram)"
    return "\n".join([
        f"Заказ {order_number(order['id'])}",
        f"Дата: {created:%d.%m.%Y %H:%M:%S} ({timezone.key})",
        f"Статус: {STATUSES[order['status']]}",
        "", f"Telegram: {order['telegram_name']}",
        f"Telegram ID: {order['user_id']}", f"Профиль: {profile}",
        "", summary(order, attachments),
    ])


def status_keyboard(order: dict) -> InlineKeyboardMarkup:
    def status_button(key: str) -> InlineKeyboardButton:
        label = STATUSES[key]
        if key == order["status"]:
            label = "✓ " + label
        return InlineKeyboardButton(label, callback_data=f"s:{order['id']}:{key}")

    rows = []
    if order["username"]:
        rows.append([InlineKeyboardButton("Открыть профиль клиента", url=f"https://t.me/{order['username']}")])
    rows.extend([
        [status_button("new"), status_button("in_progress")],
        [status_button("completed"), status_button("cancelled")],
    ])
    return InlineKeyboardMarkup(rows)
