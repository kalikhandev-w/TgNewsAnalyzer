"""Чистая логика онбординга: нормализация телефона и self-check доступа.

НЕ импортирует PyQt. Telethon-клиент принимается параметром (duck-typing),
в тестах подменяется моком. Async-функции тестируются через asyncio.run.
"""


def normalize_phone(raw: str) -> str:
    """Привести номер к виду +<цифры>. Убирает всё, кроме цифр; добавляет ведущий +.
    Возвращает "" если цифр нет."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return "+" + digits if digits else ""


async def run_self_check(client, target_channel, competitor_channels):
    """Проверить готовность: авторизация, доступ к целевому каналу и конкурентам.

    client — объект с async-методами is_user_authorized() и get_entity(name).
    Возвращает dict {authorized, target_ok, channels:{name:bool}, problems:[str]}.
    """
    result = {"authorized": False, "target_ok": False, "channels": {}, "problems": []}
    try:
        result["authorized"] = bool(await client.is_user_authorized())
    except Exception as e:
        result["problems"].append(f"Не удалось проверить авторизацию: {e}")
        return result
    if not result["authorized"]:
        result["problems"].append("Не авторизован в Telegram")
        return result
    try:
        await client.get_entity(target_channel)
        result["target_ok"] = True
    except Exception as e:
        result["problems"].append(f"Нет доступа к каналу {target_channel}: {e}")
    for name in competitor_channels:
        try:
            await client.get_entity(name)
            result["channels"][name] = True
        except Exception:
            result["channels"][name] = False
            result["problems"].append(f"Нет доступа к каналу {name}")
    return result
