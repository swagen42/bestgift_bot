from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    token: str
    owner_chat_id: int
    admin_user_ids: frozenset[int]
    owner_contact: str
    database_path: Path
    catalog_path: Path
    timezone: ZoneInfo
    log_level: int = logging.INFO
    max_attachments: int = 30


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token) or "replace_" in token:
        raise ValueError("Укажите настоящий BOT_TOKEN в .env или переменных окружения.")
    try:
        owner_chat_id = int(os.environ["OWNER_CHAT_ID"])
        admins = frozenset(
            int(value.strip())
            for value in os.getenv("ADMIN_USER_IDS", "").split(",")
            if value.strip()
        )
        max_attachments = int(os.getenv("MAX_ATTACHMENTS", "30"))
    except (KeyError, ValueError) as exc:
        raise ValueError("OWNER_CHAT_ID, ADMIN_USER_IDS и MAX_ATTACHMENTS должны быть числами.") from exc
    if not owner_chat_id or any(admin <= 0 for admin in admins):
        raise ValueError("OWNER_CHAT_ID не может быть 0, ADMIN_USER_IDS должны быть положительными.")
    if not admins:
        if owner_chat_id < 0:
            raise ValueError("Для группы заказов укажите ADMIN_USER_IDS.")
        admins = frozenset({owner_chat_id})
    if not 1 <= max_attachments <= 100:
        raise ValueError("MAX_ATTACHMENTS должен быть от 1 до 100.")
    contact = os.getenv("OWNER_CONTACT", "").strip().removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", contact) or contact == "your_username":
        raise ValueError("OWNER_CONTACT должен содержать Telegram username владельца.")
    try:
        timezone = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Неизвестный TIMEZONE. Например: Europe/Moscow или Asia/Bangkok.") from exc
    log_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if log_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL: DEBUG, INFO, WARNING, ERROR или CRITICAL.")

    def project_path(key: str, default: str) -> Path:
        path = Path(os.getenv(key, "").strip() or default).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    data_dir = project_path("DATA_DIR", "data")
    database_path = project_path("DATABASE_PATH", str(data_dir / "bestgift.sqlite3"))

    return Settings(
        token=token,
        owner_chat_id=owner_chat_id,
        admin_user_ids=admins,
        owner_contact=contact,
        database_path=database_path,
        catalog_path=project_path("CATALOG_PATH", "catalog.toml"),
        timezone=timezone,
        log_level=getattr(logging, log_name),
        max_attachments=max_attachments,
    )
