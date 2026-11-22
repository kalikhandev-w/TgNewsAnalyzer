"""Чистый парсинг Telegram-ссылок."""
import re

_MSG_ID = re.compile(r"https?://t\.me/[^/]+/(\d+)")
_CHANNEL = re.compile(r"https?://t\.me/([^/?#]+)")
_ALL_LINKS = re.compile(r"https?://t\.me/[^\s)]+")


def find_telegram_links(text: str):
    """Все t.me-ссылки в строке, в т.ч. когда перед ссылкой идёт заголовок
    ("Заголовок новости https://t.me/zakonkz/123"). Сохраняет порядок."""
    if not text:
        return []
    return [u.rstrip(".,;") for u in _ALL_LINKS.findall(text)]


def extract_message_id(link: str):
    """ID сообщения из ссылки t.me/<channel>/<id>. None, если не распознано."""
    m = _MSG_ID.search(link or "")
    return int(m.group(1)) if m else None


def extract_channel_name(link: str):
    """Имя канала из ссылки t.me/<channel>/... . None, если не распознано."""
    m = _CHANNEL.search(link or "")
    return m.group(1) if m else None
