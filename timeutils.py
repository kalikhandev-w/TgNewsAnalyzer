"""Чистые хелперы времени: всё считаем в алматинском времени GMT+5."""
from datetime import datetime, timezone, timedelta

KZ_TZ = timezone(timedelta(hours=5))


def to_kz(dt: datetime) -> datetime:
    """Перевести tz-aware datetime в GMT+5."""
    return dt.astimezone(KZ_TZ)


def kz_date(dt: datetime):
    """Календарная дата по алматинскому времени."""
    return to_kz(dt).date()
