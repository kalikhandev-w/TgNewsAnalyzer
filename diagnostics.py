"""Логирование в файл + сбор отчёта для разработчика (без секретов).

Отчёт НЕ содержит телефон/код/api_hash/содержимое сессии — только факт наличия
сессии и техданные среды. Помогает чинить проблемы у получателя без удалённого доступа.
"""
import os
import sys
import platform

import app_paths

APP_VERSION = "1.1"


class Tee:
    """Пишет в несколько потоков сразу (консоль + файл лога)."""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def enable_file_logging() -> str:
    """Дублировать stdout/stderr в logs/app.log. Возвращает путь к логу."""
    app_paths.ensure_dirs()
    log_path = str(app_paths.logs_dir() / "app.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = Tee(sys.__stdout__, log_file)
    sys.stderr = Tee(sys.__stderr__, log_file)
    return log_path


def _log_tail(max_lines: int = 80) -> str:
    log_path = app_paths.logs_dir() / "app.log"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-max_lines:])
    except Exception:
        return "(лог недоступен)"


def collect_report() -> str:
    """Собрать отчёт о среде и состоянии (без секретов)."""
    try:
        import model_loader
        model_mb = model_loader.dir_size_bytes(app_paths.models_dir()) / (1024 * 1024)
    except Exception:
        model_mb = 0.0
    session_exists = os.path.exists(str(app_paths.session_file()) + ".session")
    info = {
        "app_version": APP_VERSION,
        "os": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "data_dir": str(app_paths.base_dir()),
        "model_mb": model_mb,
        "has_session": session_exists,
        "log_tail": _log_tail(),
    }
    return format_report(info)


def format_report(info: dict) -> str:
    return "\n".join([
        "=== News Analyzer — отчёт для разработчика ===",
        f"Версия приложения: {info.get('app_version', '?')}",
        f"ОС: {info.get('os', '?')}",
        f"Архитектура: {info.get('arch', '?')}",
        f"Python: {info.get('python', '?')}",
        f"Каталог данных: {info.get('data_dir', '?')}",
        f"Модель на диске: {info.get('model_mb', 0):.0f} МБ",
        f"Сессия сохранена: {info.get('has_session', '?')}",
        "",
        "--- Последние строки лога ---",
        info.get("log_tail", ""),
    ])


def save_report(dest_path: str) -> None:
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(collect_report())
