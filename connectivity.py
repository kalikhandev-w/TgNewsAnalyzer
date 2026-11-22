"""Сетевая устойчивость: повтор операции с переподключением.

Чистый модуль: НЕ импортирует PyQt/telethon. Клиент принимается параметром
(duck-typing), в тестах — мок. Тестируется через asyncio.run.
"""


async def with_reconnect(client, make_coro, attempts=3):
    """Выполнить make_coro() с реконнектом при сбое (до `attempts` попыток).

    make_coro — фабрика корутины: вызывается заново на каждой попытке
    (корутину нельзя awaited дважды). client — объект с is_connected()/connect()/disconnect().
    Возвращает результат make_coro() или бросает последнее исключение.
    """
    last = None
    for _ in range(attempts):
        try:
            if not client.is_connected():
                await client.connect()
            return await make_coro()
        except Exception as e:
            last = e
            try:
                await client.disconnect()
                await client.connect()
            except Exception:
                pass
    raise last
