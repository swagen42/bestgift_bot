from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = {"new": "Новый", "in_progress": "В работе", "completed": "Завершён", "cancelled": "Отменён"}
STEPS = {"product", "variant", "name", "contact", "comment", "materials", "review"}
SCHEMA_VERSION = 1
SCHEMA_COLUMNS = {
    "drafts": "id user_id chat_id step revision active data_json created_at updated_at",
    "draft_attachments": "id draft_id source_chat_id source_message_id kind file_id file_unique_id file_name caption",
    "orders": (
        "id draft_id user_id chat_id username telegram_name customer_name contact product_key product_title "
        "variant_key variant_title price_rub comment created_at status status_updated_at status_updated_by "
        "owner_chat_id owner_message_id next_attachment delivered client_notified attempts retry_at last_error"
    ),
    "order_attachments": "id order_id source_chat_id source_message_id kind file_id file_unique_id file_name caption",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def order_number(order_id: int) -> str:
    return f"BG-{order_id:06d}"


class StaleDraft(ValueError):
    """Устаревший экран или уже завершенный черновик."""


class Store:
    """Короткие синхронные транзакции; Telegram API вызывается вне транзакций."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        try:
            self._initialize()
        except Exception:
            self.connection.close()
            raise

    def _initialize(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"База создана более новой версией бота (схема {version}). Обновите приложение; база не изменена."
            )
        tables = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables or version:
            # Не пытаемся молча использовать чужую или неполную старую схему.
            for table, columns in SCHEMA_COLUMNS.items():
                actual = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
                if not set(columns.split()) <= actual:
                    raise ValueError(
                        "Несовместимая схема SQLite. Сделайте резервную копию и выполните миграцию базы."
                    )
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                step TEXT NOT NULL DEFAULT 'product',
                revision INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                data_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_draft
                ON drafts(user_id) WHERE active = 1;
            CREATE TABLE IF NOT EXISTS draft_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT NOT NULL REFERENCES drafts(id),
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                file_name TEXT,
                caption TEXT,
                UNIQUE(draft_id, source_chat_id, source_message_id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                username TEXT,
                telegram_name TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                contact TEXT,
                product_key TEXT NOT NULL,
                product_title TEXT NOT NULL,
                variant_key TEXT NOT NULL,
                variant_title TEXT NOT NULL,
                price_rub INTEGER NOT NULL CHECK(price_rub > 0),
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
                    CHECK(status IN ('new', 'in_progress', 'completed', 'cancelled')),
                status_updated_at TEXT NOT NULL,
                status_updated_by INTEGER,
                owner_chat_id INTEGER NOT NULL,
                owner_message_id INTEGER,
                next_attachment INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0,
                client_notified INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS order_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                file_name TEXT,
                caption TEXT
            );
            CREATE INDEX IF NOT EXISTS pending_notifications
                ON orders(delivered, client_notified, retry_at);
            PRAGMA user_version = 1;
        """)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def _write_transaction(self):
        # Получаем блокировку до чтения: проверка состояния и запись должны быть атомарны.
        self.connection.execute("BEGIN IMMEDIATE")
        with self.connection:
            yield

    @staticmethod
    def _draft(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        draft = dict(row)
        draft["data"] = json.loads(draft.pop("data_json"))
        return draft

    def active_draft(self, user_id: int) -> dict | None:
        return self._draft(self.connection.execute(
            "SELECT * FROM drafts WHERE user_id = ? AND active = 1", (user_id,)
        ).fetchone())

    def draft(self, draft_id: str) -> dict | None:
        return self._draft(self.connection.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone())

    def start_draft(self, user_id: int, chat_id: int) -> dict:
        with self._write_transaction():
            existing = self.active_draft(user_id)
            if existing:
                return existing
            draft_id = uuid.uuid4().hex[:12]
            now = utcnow()
            self.connection.execute(
                "INSERT INTO drafts(id, user_id, chat_id, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (draft_id, user_id, chat_id, now, now),
            )
        return self.draft(draft_id)

    def update_draft(self, draft: dict, *, step: str | None = None, **data: Any) -> dict:
        if step is not None and step not in STEPS:
            raise ValueError("Неизвестный шаг")
        merged = {**draft["data"], **data}
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE drafts SET step = ?, data_json = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND user_id = ? AND revision = ? AND active = 1""",
                (step or draft["step"], json.dumps(merged, ensure_ascii=False), utcnow(),
                 draft["id"], draft["user_id"], draft["revision"]),
            )
            if not cursor.rowcount:
                raise StaleDraft("Черновик изменился")
        return self.draft(draft["id"])

    def cancel_draft(self, user_id: int) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE drafts SET active = 0, updated_at = ? WHERE user_id = ? AND active = 1",
                (utcnow(), user_id),
            )
        return bool(cursor.rowcount)

    def attachments(self, draft_id: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM draft_attachments WHERE draft_id = ? ORDER BY id", (draft_id,)
        )]

    def add_attachment(self, draft: dict, attachment: dict, limit: int) -> str:
        with self._write_transaction():
            current = self.draft(draft["id"])
            if (not current or not current["active"] or current["step"] != "materials"
                    or current["user_id"] != draft["user_id"] or current["revision"] != draft["revision"]):
                raise StaleDraft("Шаг приема материалов завершен")
            duplicate = self.connection.execute(
                "SELECT 1 FROM draft_attachments WHERE draft_id = ? AND source_chat_id = ? AND source_message_id = ?",
                (draft["id"], attachment["source_chat_id"], attachment["source_message_id"]),
            ).fetchone()
            if duplicate:
                return "duplicate"
            if len(self.attachments(draft["id"])) >= limit:
                return "limit"
            self.connection.execute(
                """INSERT INTO draft_attachments
                   (draft_id, source_chat_id, source_message_id, kind, file_id, file_unique_id, file_name, caption)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (draft["id"], *(attachment.get(key) for key in (
                    "source_chat_id", "source_message_id", "kind", "file_id", "file_unique_id", "file_name", "caption"
                ))),
            )
            # Добавление материалов не меняет revision: кнопка «Готово» остается рабочей для альбомов.
            self.connection.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (utcnow(), draft["id"]))
        return "added"

    def clear_attachments(self, draft: dict) -> None:
        with self._write_transaction():
            current = self.draft(draft["id"])
            if (not current or not current["active"] or current["step"] != "materials"
                    or current["user_id"] != draft["user_id"] or current["revision"] != draft["revision"]):
                raise StaleDraft("Шаг приема материалов изменился")
            self.connection.execute("DELETE FROM draft_attachments WHERE draft_id = ?", (draft["id"],))

    def confirm(self, draft: dict, *, username: str | None, telegram_name: str, owner_chat_id: int) -> tuple[dict, bool]:
        """Снимок цены, материалов и запись заказа создаются одной транзакцией."""
        with self._write_transaction():
            existing = self.connection.execute("SELECT * FROM orders WHERE draft_id = ?", (draft["id"],)).fetchone()
            if existing:
                if existing["user_id"] != draft["user_id"]:
                    raise StaleDraft("Черновик принадлежит другому пользователю")
                return dict(existing), False
            current = self.draft(draft["id"])
            if (not current or not current["active"] or current["step"] != "review"
                    or current["revision"] != draft["revision"] or current["user_id"] != draft["user_id"]):
                raise StaleDraft("Подтверждение устарело")
            data = current["data"]
            required = ("customer_name", "product_key", "product_title", "variant_key", "variant_title", "price_rub")
            if any(not data.get(key) for key in required) or "comment" not in data:
                raise ValueError("Не все данные заказа заполнены")
            now = utcnow()
            cursor = self.connection.execute(
                """INSERT INTO orders (
                    draft_id, user_id, chat_id, username, telegram_name, customer_name, contact,
                    product_key, product_title, variant_key, variant_title, price_rub, comment,
                    created_at, status_updated_at, owner_chat_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (current["id"], current["user_id"], current["chat_id"], username, telegram_name,
                 data["customer_name"], data.get("contact"), data["product_key"], data["product_title"],
                 data["variant_key"], data["variant_title"], data["price_rub"], data["comment"], now, now, owner_chat_id),
            )
            order_id = cursor.lastrowid
            self.connection.execute(
                """INSERT INTO order_attachments
                   (order_id, source_chat_id, source_message_id, kind, file_id, file_unique_id, file_name, caption)
                   SELECT ?, source_chat_id, source_message_id, kind, file_id, file_unique_id, file_name, caption
                   FROM draft_attachments WHERE draft_id = ? ORDER BY id""",
                (order_id, current["id"]),
            )
            self.connection.execute(
                "UPDATE drafts SET active = 0, updated_at = ? WHERE id = ?", (now, current["id"])
            )
        return self.order(order_id), True

    def order(self, order_id: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def order_for_draft(self, draft_id: str, user_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM orders WHERE draft_id = ? AND user_id = ?", (draft_id, user_id)
        ).fetchone()
        return dict(row) if row else None

    def order_attachments(self, order_id: int) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM order_attachments WHERE order_id = ? ORDER BY id", (order_id,)
        )]

    def set_status(self, order_id: int, status: str, admin_id: int) -> dict:
        if status not in STATUSES:
            raise ValueError("Неизвестный статус")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE orders SET status = ?, status_updated_at = ?, status_updated_by = ? WHERE id = ?",
                (status, utcnow(), admin_id, order_id),
            )
            if not cursor.rowcount:
                raise ValueError("Заказ не найден")
        return self.order(order_id)

    def next_delivery(self) -> dict | None:
        row = self.connection.execute(
            """SELECT * FROM orders WHERE (delivered = 0 OR client_notified = 0)
               AND retry_at <= ? ORDER BY id LIMIT 1""", (time.time(),)
        ).fetchone()
        return dict(row) if row else None

    def delivery_progress(self, order_id: int, **changes: Any) -> None:
        allowed = {"owner_message_id", "next_attachment", "delivered", "client_notified", "attempts", "retry_at", "last_error"}
        if not changes or not changes.keys() <= allowed:
            raise ValueError("Неизвестное поле доставки")
        keys = list(changes)
        with self.connection:
            self.connection.execute(
                "UPDATE orders SET " + ", ".join(f"{key} = ?" for key in keys) + " WHERE id = ?",
                (*changes.values(), order_id),
            )
