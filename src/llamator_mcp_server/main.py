from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from arq import create_pool
from arq.connections import ArqRedis
from fastapi import FastAPI
from llamator_mcp_server.api.http import build_router
from llamator_mcp_server.api.mcp_server import build_mcp
from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.infra.redis import create_redis_client
from llamator_mcp_server.infra.redis import parse_redis_settings
from llamator_mcp_server.utils.logging import LOGGER_NAME
from llamator_mcp_server.utils.logging import configure_logging
from redis.asyncio import Redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Lifespan-хук FastAPI для инициализации соединений.

    :param app: Приложение FastAPI.
    :return: Контекстный менеджер.
    """
    settings: Settings = Settings()
    configure_logging(settings.log_level)
    logger: logging.Logger = logging.getLogger(LOGGER_NAME)

    redis: Redis = create_redis_client(settings.redis_dsn)
    await redis.ping()

    arq_pool: ArqRedis = await create_pool(parse_redis_settings(settings.redis_dsn))

    app.state.settings = settings
    app.state.redis = redis
    app.state.arq = arq_pool
    app.state.logger = logger

    yield

    await arq_pool.close()
    await redis.aclose()


app: FastAPI = FastAPI(title="llamator-mcp-server", version="0.1.0", lifespan=lifespan)

_settings = Settings()
configure_logging(_settings.log_level)
_logger: logging.Logger = logging.getLogger(LOGGER_NAME)

_redis: Redis = create_redis_client(_settings.redis_dsn)
_arq: ArqRedis


@app.on_event("startup")
async def _startup_bindings() -> None:
    """
    Привязать зависимости после старта, когда доступен lifespan state.

    :return: None
    """
    global _arq  # noqa: PLW0603
    _arq = app.state.arq  # type: ignore[attr-defined]

    router = build_router(settings=app.state.settings, redis=app.state.redis, arq=app.state.arq,
                          logger=app.state.logger)
    app.include_router(router)

    mcp = build_mcp(settings=app.state.settings, redis=app.state.redis, arq=app.state.arq, logger=app.state.logger)
    app.mount("/mcp", mcp.streamable_http_app())


def main() -> None:
    """
    Точка входа для запуска HTTP сервера.

    :return: None
    """
    import uvicorn

    uvicorn.run("llamator_mcp_server.main:app", host="0.0.0.0", port=8000, log_level="info")