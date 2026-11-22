"""Встроенные ключи приложения (dev-аккаунт), с лёгкой обфускацией base64.

Реальные значения инжектятся ПРИ СБОРКЕ в _embedded_creds.py (gitignored).
Для разработки — fallback на config.py. base64 — НЕ защита, лишь чтобы ключи
не лежали голым текстом в бинаре.
"""
import base64


def _decode(blob: str) -> str:
    return base64.b64decode(blob.encode()).decode()


def get_api_credentials() -> tuple[int, str]:
    """Вернуть (api_id, api_hash). Порядок: _embedded_creds -> config.py."""
    try:
        from _embedded_creds import API_ID_B64, API_HASH_B64
        return int(_decode(API_ID_B64)), _decode(API_HASH_B64)
    except ImportError:
        pass
    try:
        import config
        return int(config.API_ID), config.API_HASH
    except Exception as e:
        raise RuntimeError(
            "Не найдены встроенные ключи (_embedded_creds.py) и нет config.py для разработки"
        ) from e
