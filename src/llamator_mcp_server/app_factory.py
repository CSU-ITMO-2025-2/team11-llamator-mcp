from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

from arq import create_pool
from arq.connections import ArqRedis
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from redis.asyncio import Redis

from llamator_mcp_server.api.asgi_wrappers import _ApiKeyAsgiWrapper, _McpSseToJsonWrapper
from llamator_mcp_server.api.http import build_router
from llamator_mcp_server.api.mcp_server import build_mcp
from llamator_mcp_server.config.settings import settings
from llamator_mcp_server.infra.redis import create_redis_client, parse_redis_settings
from llamator_mcp_server.utils.logging import LOGGER_NAME, configure_logging


async def _close_arq_pool(arq_pool: ArqRedis) -> None:
    await arq_pool.close()


async def _close_redis_client(redis: Redis) -> None:
    await redis.aclose()


def create_app() -> FastAPI:
    """
    Создать приложение FastAPI.

    :return: Экземпляр FastAPI.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            configure_logging(settings.log_level)
            logger: logging.Logger = logging.getLogger(LOGGER_NAME)

            redis: Redis = create_redis_client(settings.redis_dsn)
            await redis.ping()
            stack.push_async_callback(_close_redis_client, redis)

            arq_pool: ArqRedis = await create_pool(parse_redis_settings(settings.redis_dsn))
            stack.push_async_callback(_close_arq_pool, arq_pool)

            app.state.settings = settings
            app.state.redis = redis
            app.state.arq = arq_pool
            app.state.logger = logger

            router = build_router(settings=settings, redis=redis, arq=arq_pool, logger=logger)
            app.include_router(router)

            mcp = build_mcp(settings=settings, redis=redis, arq=arq_pool, logger=logger)
            raw_mcp_app = mcp.streamable_http_app()
            await stack.enter_async_context(mcp.session_manager.run())

            mcp_app = _ApiKeyAsgiWrapper(raw_mcp_app, api_key=settings.api_key)
            mcp_app = _McpSseToJsonWrapper(mcp_app, max_body_bytes=1024 * 1024)
            app.mount(settings.mcp_mount_path, mcp_app)

            yield

    app = FastAPI(title="llamator-mcp-server", version="0.1.0", lifespan=lifespan)

    # Prometheus metrics at /metrics
    Instrumentator().instrument(app).expose(app)

    return app
