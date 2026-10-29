from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path

from telegram import BotCommand
from telegram.error import TelegramError
from telegram.ext import Application

from .catalog import Catalog
from .config import Settings, load_settings
from .handlers import BotHandlers
from .network import check_network_configuration, create_request
from .notifications import Notifier
from .storage import Store


class DatabaseLock:
    """Один процесс на файл БД; блокировка снимается ОС даже после аварии."""

    def __init__(self, database_path: Path):
        self.path = Path(str(database_path.resolve()) + ".lock")
        self.stream = None

    def __enter__(self) -> DatabaseLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ValueError("Эта база уже используется другим экземпляром бота или недоступна для блокировки.") from exc
        self.stream = stream
        return self

    def __exit__(self, *_args) -> None:
        if self.stream is not None:
            # Не удаляем файл: другой процесс мог уже открыть тот же inode.
            self.stream.close()
            self.stream = None


class RedactingFormatter(logging.Formatter):
    """Токен не попадает в журнал, в том числе в traceback HTTP-клиента."""

    def __init__(self, token: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record).replace(self.token, "<BOT_TOKEN>")
        return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<BOT_TOKEN>", text)


def main() -> None:
    parser = argparse.ArgumentParser(description="BestGift — Telegram-бот для приема заказов")
    parser.add_argument("--check", action="store_true", help="Проверить конфигурацию и базу без подключения к Telegram")
    args = parser.parse_args()
    try:
        settings = load_settings()
        catalog = Catalog.load(settings.catalog_path)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Ошибка настройки: {exc}\n")
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(settings.token))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    # HTTP-журналы содержат API URL, поэтому подавляем повседневные запросы.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    try:
        with DatabaseLock(settings.database_path):
            store = Store(settings.database_path)
            try:
                if args.check:
                    try:
                        asyncio.run(check_network_configuration())
                    except (ValueError, RuntimeError, ImportError, OSError):
                        parser.exit(2, "Ошибка настройки сетевого клиента. Проверьте системный прокси и установите зависимости из requirements.txt.\n")
                    print(f"Конфигурация корректна. Товаров: {len(catalog.products)}. SQLite и сетевой клиент готовы. Telegram API не вызывался.")
                else:
                    run_bot(settings, catalog, store)
            finally:
                store.close()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Ошибка настройки базы: {exc}\n")


def run_bot(settings: Settings, catalog: Catalog, store: Store) -> None:
    # Python 3.14 больше не создает event loop автоматически для синхронного run_polling.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    notifier = Notifier(store, settings)
    handlers = BotHandlers(store, catalog, settings)
    requests = []

    async def post_init(app: Application) -> None:
        try:
            await app.bot.set_my_commands([
                BotCommand("start", "Главное меню / продолжить заявку"),
                BotCommand("order", "Оформить заказ"),
                BotCommand("price", "Актуальный прайс"),
                BotCommand("contact", "Связаться с мастером"),
                BotCommand("cancel", "Отменить оформление"),
                BotCommand("id", "Узнать ID чата и пользователя"),
            ])
        except TelegramError:
            # Сбой необязательного меню не должен останавливать приём заказов.
            logging.getLogger(__name__).warning("Не удалось обновить меню команд; команды доступны вручную.")
        notifier.start(app.bot)

    async def post_stop(app: Application) -> None:
        await notifier.stop()

    try:
        requests.append(create_request())
        requests.append(create_request(polling=True))
        app = (Application.builder().token(settings.token)
               .request(requests[0]).get_updates_request(requests[1])
               .concurrent_updates(False).post_init(post_init).post_stop(post_stop).build())
        handlers.register(app)
        app.add_error_handler(handlers.error)
        logging.getLogger(__name__).info("Бот запущен. Ожидание заявок.")
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False, close_loop=False)
    except TelegramError:
        logging.getLogger(__name__).exception("Запуск Telegram API не удался. Проверьте токен, сеть и доступность чата.")
        raise SystemExit(1) from None
    except (ValueError, RuntimeError, ImportError, OSError) as exc:
        logging.getLogger(__name__).error(
            "Не удалось настроить сетевой клиент (%s). Проверьте системный прокси и зависимости requirements.txt.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None
    finally:
        if not loop.is_closed():
            try:
                loop.run_until_complete(notifier.stop())
            finally:
                try:
                    # run_polling закрывает клиенты сам; повторное закрытие также
                    # освобождает первый клиент при ошибке создания второго.
                    for request in requests:
                        loop.run_until_complete(request.shutdown())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)


if __name__ == "__main__":
    main()
