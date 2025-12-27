from __future__ import annotations

from fastapi import HTTPException
from llamator_mcp_server.config.settings import Settings


async def require_api_key(settings: Settings, x_api_key: str | None) -> None:
    """
    Проверить доступ по API-ключу.

    Если в настройках не задан ``api_key``, проверка отключена.

    :param settings: Настройки приложения.
    :param x_api_key: Значение заголовка ``X-API-Key``.
    :return: None
    :raises HTTPException: Если ключ задан и не совпадает.
    """
    expected: str | None = settings.api_key
    if expected is None or expected == "":
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
