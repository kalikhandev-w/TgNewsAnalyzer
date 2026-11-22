"""Пути к пользовательским данным в ~/Library/Application Support/TgNewsAnalyzer/.

Чистый модуль: только stdlib, без PyQt/torch/telethon.
"""
from pathlib import Path

APP_NAME = "TgNewsAnalyzer"


def base_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_NAME


def session_dir() -> Path:
    return base_dir() / "session"


def models_dir() -> Path:
    return base_dir() / "models"


def logs_dir() -> Path:
    return base_dir() / "logs"


def settings_path() -> Path:
    return base_dir() / "settings.json"


def ensure_dirs() -> None:
    for d in (base_dir(), session_dir(), models_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


def session_file() -> str:
    """Путь к файлу сессии Telethon БЕЗ расширения .session (Telethon добавит сам)."""
    return str(session_dir() / "user")
