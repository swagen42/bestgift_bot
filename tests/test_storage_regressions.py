from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bestgift_bot.storage import StaleDraft, Store


class StorageRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orders.sqlite3"

    def open_store(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        return store

    @staticmethod
    def attachment():
        return {
            "source_chat_id": 111, "source_message_id": 1, "kind": "photo",
            "file_id": "file-1", "file_unique_id": "unique-1",
        }

    @staticmethod
    def review_draft(store):
        draft = store.start_draft(111, 111)
        draft = store.update_draft(
            draft, step="materials", customer_name="Анна", product_key="magazine",
            product_title="Журнал", variant_key="p12", variant_title="12 страниц",
            price_rub=2000, comment="Подарок",
        )
        store.add_attachment(draft, StorageRegressions.attachment(), 30)
        return store.update_draft(draft, step="review")

    def test_newer_schema_is_rejected_without_changing_database(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                "CREATE TABLE future_data(value TEXT); INSERT INTO future_data VALUES ('keep');"
                "PRAGMA user_version = 2;"
            )
        with self.assertRaisesRegex(ValueError, "новой версией"):
            Store(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT value FROM future_data").fetchone()[0], "keep")
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall(), [("future_data",)])

    def test_incomplete_schema_is_rejected_before_creating_other_tables(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                "CREATE TABLE drafts(id TEXT); INSERT INTO drafts VALUES ('keep'); PRAGMA user_version = 1;"
            )
        with self.assertRaisesRegex(ValueError, "Несовместимая схема"):
            Store(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT id FROM drafts").fetchall(), [("keep",)])
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall(), [("drafts",)])

    def test_stale_material_screen_cannot_clear_or_add_attachments(self):
        store = self.open_store()
        old = store.start_draft(111, 111)
        old = store.update_draft(old, step="materials")
        store.add_attachment(old, self.attachment(), 30)
        current = store.update_draft(old, step="review")
        current = store.update_draft(current, step="materials")
        with self.assertRaises(StaleDraft):
            store.clear_attachments(old)
        with self.assertRaises(StaleDraft):
            store.add_attachment(old, {**self.attachment(), "source_message_id": 2}, 30)
        self.assertEqual(len(store.attachments(old["id"])), 1)
        store.clear_attachments(current)
        self.assertEqual(store.attachments(current["id"]), [])

    def test_failed_snapshot_rolls_back_order_and_keeps_draft(self):
        store = self.open_store()
        draft = self.review_draft(store)
        store.connection.executescript(
            "CREATE TRIGGER fail_snapshot BEFORE INSERT ON order_attachments "
            "BEGIN SELECT RAISE(ABORT, 'simulated disk write failure'); END;"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            store.confirm(draft, username=None, telegram_name="Анна", owner_chat_id=777)
        self.assertIsNotNone(store.active_draft(111))
        self.assertIsNone(store.order_for_draft(draft["id"], 111))
        self.assertEqual(len(store.attachments(draft["id"])), 1)
        store.connection.execute("DROP TRIGGER fail_snapshot")
        order, created = store.confirm(draft, username=None, telegram_name="Анна", owner_chat_id=777)
        self.assertTrue(created)
        self.assertEqual(len(store.order_attachments(order["id"])), 1)
        self.assertIsNone(store.active_draft(111))

    def test_confirm_cannot_return_another_users_existing_order(self):
        store = self.open_store()
        draft = self.review_draft(store)
        order, _ = store.confirm(draft, username=None, telegram_name="Анна", owner_chat_id=777)
        with self.assertRaises(StaleDraft):
            store.confirm({**draft, "user_id": 222}, username=None, telegram_name="Другой", owner_chat_id=777)
        repeated, created = store.confirm(draft, username=None, telegram_name="Анна", owner_chat_id=777)
        self.assertFalse(created)
        self.assertEqual(repeated["id"], order["id"])


if __name__ == "__main__":
    unittest.main()
