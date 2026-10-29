from __future__ import annotations

import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bestgift_bot.__main__ import RedactingFormatter, main
from bestgift_bot.catalog import Catalog
from bestgift_bot.config import PROJECT_ROOT, load_settings
from bestgift_bot.presentation import owner_text, status_keyboard


class Configuration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = {
            # OpenSSL на Windows требует системные переменные при первой инициализации.
            **{key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}},
            "BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz123456789",
            "OWNER_CHAT_ID": "777",
            "OWNER_CONTACT": "@craft_master",
            "DATABASE_PATH": str(Path(self.temp.name) / "check.sqlite3"),
            "CATALOG_PATH": str(PROJECT_ROOT / "catalog.toml"),
            "TIMEZONE": "Asia/Bangkok",
        }
        dotenv = patch("bestgift_bot.config.load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)

    def test_personal_chat_default_admin_and_paths(self):
        with patch.dict("os.environ", self.environment, clear=True):
            settings = load_settings()
        self.assertEqual(settings.admin_user_ids, frozenset({777}))
        self.assertEqual(settings.owner_contact, "craft_master")
        self.assertEqual(settings.timezone.key, "Asia/Bangkok")
        self.assertEqual(settings.database_path, Path(self.temp.name) / "check.sqlite3")

    def test_group_requires_explicit_admins(self):
        self.environment["OWNER_CHAT_ID"] = "-100123456"
        with patch.dict("os.environ", self.environment, clear=True):
            with self.assertRaisesRegex(ValueError, "ADMIN_USER_IDS"):
                load_settings()
        self.environment["ADMIN_USER_IDS"] = "777, 888"
        with patch.dict("os.environ", self.environment, clear=True):
            self.assertEqual(load_settings().admin_user_ids, frozenset({777, 888}))

    def test_placeholder_secret_and_invalid_settings_are_rejected(self):
        for key, value in [
            ("BOT_TOKEN", "123456789:replace_with_your_bot_token"),
            ("OWNER_CHAT_ID", "0"),
            ("OWNER_CHAT_ID", "not_a_number"),
            ("OWNER_CONTACT", "@your_username"),
            ("TIMEZONE", "Invalid/Timezone"),
            ("MAX_ATTACHMENTS", "0"),
        ]:
            with self.subTest(key=key, value=value):
                with patch.dict("os.environ", {**self.environment, key: value}, clear=True):
                    with self.assertRaises(ValueError):
                        load_settings()

    def test_check_command_initializes_database_without_telegram(self):
        with patch.dict("os.environ", self.environment, clear=True), patch("sys.argv", ["bestgift_bot", "--check"]):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                main()
        self.assertIn("Telegram API", output.getvalue())
        self.assertTrue(Path(self.environment["DATABASE_PATH"]).exists())

    def test_invalid_catalog_price_is_rejected(self):
        path = Path(self.temp.name) / "bad.toml"
        path.write_text('[products.test]\ntitle="Test"\n[products.test.variants.a]\ntitle="A"\nprice=-1\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "цен"):
            Catalog.load(path)

    def test_token_is_redacted_including_exception_traceback(self):
        token = self.environment["BOT_TOKEN"]
        formatter = RedactingFormatter(token)
        try:
            raise RuntimeError(f"https://api.telegram.org/bot{token}/getMe")
        except RuntimeError as error:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Failure %s", (token,),
                                       (type(error), error, error.__traceback__))
        formatted = formatter.format(record)
        self.assertNotIn(token, formatted)
        self.assertIn("<BOT_TOKEN>", formatted)

    def test_maximum_accepted_fields_fit_telegram_message_limit(self):
        emoji = "\U0001f600"
        order = dict(
            id=1, created_at="2026-10-26T17:00:00+00:00", status="new", username="a" * 32,
            user_id=1234567890123, telegram_name=emoji * 129, customer_name=emoji * 80,
            contact=emoji * 160, product_title=emoji * 100, variant_title=emoji * 100,
            price_rub=10000000, comment=emoji * 1000,
        )
        attachments = [dict(kind="document", file_name=emoji * 100) for _ in range(100)]
        text = owner_text(order, attachments, ZoneInfo("Asia/Bangkok"))
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)

    def test_no_username_does_not_add_privacy_restricted_button(self):
        keyboard = status_keyboard(dict(id=1, username=None, user_id=111, status="new"))
        self.assertFalse(any(button.url for row in keyboard.inline_keyboard for button in row))


if __name__ == "__main__":
    unittest.main()
