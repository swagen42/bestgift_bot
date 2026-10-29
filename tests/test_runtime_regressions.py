from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import NetworkError

from bestgift_bot.__main__ import DatabaseLock, main, run_bot
from bestgift_bot.catalog import Catalog
from bestgift_bot.config import PROJECT_ROOT, load_settings


class RuntimeRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = {
            **{key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}},
            "BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz123456789",
            "OWNER_CHAT_ID": "777",
            "OWNER_CONTACT": "craft_master",
            "DATABASE_PATH": str(self.root / "orders.sqlite3"),
            "CATALOG_PATH": str(PROJECT_ROOT / "catalog.toml"),
            "TIMEZONE": "Asia/Bangkok",
        }
        dotenv = patch("bestgift_bot.config.load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)

    def settings(self, **environment):
        with patch.dict(os.environ, {**self.environment, **environment}, clear=True):
            return load_settings()

    def test_data_dir_is_used_when_database_path_is_unset_or_blank(self):
        environment = {**self.environment, "DATA_DIR": str(self.root / "persistent")}
        environment.pop("DATABASE_PATH")
        for database_path in (None, "", "   "):
            with self.subTest(database_path=database_path):
                values = dict(environment)
                if database_path is not None:
                    values["DATABASE_PATH"] = database_path
                with patch.dict(os.environ, values, clear=True):
                    self.assertEqual(load_settings().database_path, self.root / "persistent" / "bestgift.sqlite3")

    def test_explicit_database_path_takes_precedence_over_data_dir(self):
        settings = self.settings(DATA_DIR=str(self.root / "persistent"))
        self.assertEqual(settings.database_path, self.root / "orders.sqlite3")

    def test_invalid_timezone_paths_are_configuration_errors(self):
        for value in ("", "/absolute", "../relative"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "TIMEZONE"):
                    self.settings(TIMEZONE=value)

    def test_corrupt_database_is_reported_without_traceback(self):
        (self.root / "orders.sqlite3").write_bytes(b"This is not a SQLite database")
        with patch.dict(os.environ, self.environment, clear=True), patch("sys.argv", ["bot", "--check"]):
            with patch("sys.stderr", new_callable=io.StringIO) as output:
                with self.assertRaises(SystemExit) as exit_status:
                    main()
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("Ошибка настройки базы", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_database_lock_rejects_second_process_and_releases_after_exception(self):
        path = self.root / "orders.sqlite3"
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with DatabaseLock(path):
                with self.assertRaisesRegex(ValueError, "другим экземпляром"):
                    with DatabaseLock(path):
                        self.fail("Second lock was granted")
                raise RuntimeError("interrupted")
        with DatabaseLock(path):
            pass

    def test_check_does_not_open_database_when_instance_is_running(self):
        with DatabaseLock(self.root / "orders.sqlite3"):
            with patch.dict(os.environ, self.environment, clear=True), patch("sys.argv", ["bot", "--check"]):
                with patch("bestgift_bot.__main__.Store") as store, patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as exit_status:
                        main()
        self.assertEqual(exit_status.exception.code, 2)
        store.assert_not_called()

    def test_malformed_catalog_tables_are_validation_errors(self):
        for source in (
            'products = "invalid"',
            '[products]\ntest = 1',
            '[products.test]\ntitle = "Test"\nvariants = [1, 2]',
            '[products.test]\ntitle = "Test"\n[products.test.variants]\na = true',
        ):
            with self.subTest(source=source):
                path = self.root / "bad.toml"
                path.write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    Catalog.load(path)

    def test_large_valid_catalog_can_be_loaded(self):
        source = '[products.test]\ntitle = "Test"\n'
        for index in range(50):
            source += f'[products.test.variants.v{index}]\ntitle = "' + "x" * 100 + '"\nprice = 10\n'
        path = self.root / "large.toml"
        path.write_text(source, encoding="utf-8")
        catalog = Catalog.load(path)
        self.assertEqual(len(catalog.products["test"].variants), 50)
        self.assertGreater(len(catalog.price_list()), 4096)

    def test_first_http_client_is_closed_when_second_cannot_be_created(self):
        first = MagicMock()
        first.shutdown = AsyncMock()
        with patch("bestgift_bot.__main__.create_request", side_effect=[first, ValueError("bad proxy")]):
            with patch("bestgift_bot.__main__.Notifier") as notifier, patch("bestgift_bot.__main__.BotHandlers"):
                notifier.return_value.stop = AsyncMock()
                with self.assertRaises(SystemExit) as exit_status:
                    run_bot(self.settings(), Catalog.load(PROJECT_ROOT / "catalog.toml"), MagicMock())
        self.assertEqual(exit_status.exception.code, 1)
        first.shutdown.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            asyncio.get_event_loop()

    def test_command_menu_network_error_does_not_prevent_startup(self):
        builder = MagicMock()
        builder.token.return_value = builder
        builder.request.return_value = builder
        builder.get_updates_request.return_value = builder
        builder.concurrent_updates.return_value = builder
        builder.post_init.return_value = builder
        builder.post_stop.return_value = builder
        app = builder.build.return_value
        app.bot.set_my_commands = AsyncMock(side_effect=NetworkError("temporary error"))

        def polling(**_kwargs):
            loop = asyncio.get_event_loop()
            loop.run_until_complete(builder.post_init.call_args.args[0](app))

        app.run_polling.side_effect = polling
        requests = [MagicMock(shutdown=AsyncMock()), MagicMock(shutdown=AsyncMock())]
        with patch("bestgift_bot.__main__.Application.builder", return_value=builder):
            with patch("bestgift_bot.__main__.create_request", side_effect=requests):
                with patch("bestgift_bot.__main__.Notifier") as notifier, patch("bestgift_bot.__main__.BotHandlers"):
                    notifier.return_value.stop = AsyncMock()
                    run_bot(self.settings(), Catalog.load(PROJECT_ROOT / "catalog.toml"), MagicMock())
        notifier.return_value.start.assert_called_once_with(app.bot)
        for request in requests:
            request.shutdown.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
