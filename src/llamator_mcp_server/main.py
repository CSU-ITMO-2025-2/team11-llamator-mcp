from __future__ import annotations

from fastapi import FastAPI

from llamator_mcp_server.app_factory import create_app
from llamator_mcp_server.config.settings import settings

app: FastAPI = create_app()


def main() -> None:
    """
    Точка входа для запуска HTTP сервера.

    :return: None
    """
    import uvicorn

    uvicorn.run(
        "llamator_mcp_server.main:app",
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.uvicorn_log_level.lower(),
    )
