from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from typing import Callable

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


def _get_env_str(name: str) -> str:
    raw: str = os.environ.get(name, "")
    val: str = raw.strip()
    if not val:
        raise ValueError(f"Missing required env var: {name}")
    return val


def _get_env_optional_str(name: str) -> str:
    return os.environ.get(name, "").strip()


def _get_env_int(name: str) -> int:
    raw: str = _get_env_str(name)
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"Invalid int for {name}: {raw}") from e


def _load_settings_with_defaults() -> Settings:
    """
    Загрузить настройки приложения, подставляя значения по умолчанию на уровне точки входа.

    :return: Настройки приложения.
    :raises ValueError: При некорректных значениях окружения.
    """
    prefix: str = "LLAMATOR_MCP_"

    defaults: dict[str, object] = {
        "redis_dsn": "redis://redis:6379/0",
        "artifacts_root": "/data/artifacts",
        "api_key": "",
        "log_level": "INFO",
        "aux_openai_base_url": "http://tgi:80/v1",
        "aux_openai_model": "tgi",
        "aux_openai_api_key": "dummy",
        "job_ttl_seconds": 7 * 24 * 60 * 60,
        "run_timeout_seconds": 60 * 60,
        "report_language": "en",
        "http_host": "0.0.0.0",
        "http_port": 8000,
        "mcp_mount_path": "/mcp",
        "mcp_streamable_http_path": "/",
        "uvicorn_log_level": "info",
    }

    provided: dict[str, object] = {}

    if _get_env_optional_str(f"{prefix}REDIS_DSN"):
        provided["redis_dsn"] = _get_env_str(f"{prefix}REDIS_DSN")
    else:
        provided["redis_dsn"] = defaults["redis_dsn"]

    if _get_env_optional_str(f"{prefix}ARTIFACTS_ROOT"):
        provided["artifacts_root"] = _get_env_str(f"{prefix}ARTIFACTS_ROOT")
    else:
        provided["artifacts_root"] = defaults["artifacts_root"]

    provided["api_key"] = _get_env_optional_str(f"{prefix}API_KEY") or str(defaults["api_key"])

    if _get_env_optional_str(f"{prefix}LOG_LEVEL"):
        provided["log_level"] = _get_env_str(f"{prefix}LOG_LEVEL")
    else:
        provided["log_level"] = defaults["log_level"]

    if _get_env_optional_str(f"{prefix}AUX_OPENAI_BASE_URL"):
        provided["aux_openai_base_url"] = _get_env_str(f"{prefix}AUX_OPENAI_BASE_URL")
    else:
        provided["aux_openai_base_url"] = defaults["aux_openai_base_url"]

    if _get_env_optional_str(f"{prefix}AUX_OPENAI_MODEL"):
        provided["aux_openai_model"] = _get_env_str(f"{prefix}AUX_OPENAI_MODEL")
    else:
        provided["aux_openai_model"] = defaults["aux_openai_model"]

    provided["aux_openai_api_key"] = _get_env_optional_str(f"{prefix}AUX_OPENAI_API_KEY") or str(
            defaults["aux_openai_api_key"]
    )

    if _get_env_optional_str(f"{prefix}JOB_TTL_SECONDS"):
        provided["job_ttl_seconds"] = _get_env_int(f"{prefix}JOB_TTL_SECONDS")
    else:
        provided["job_ttl_seconds"] = defaults["job_ttl_seconds"]

    if _get_env_optional_str(f"{prefix}RUN_TIMEOUT_SECONDS"):
        provided["run_timeout_seconds"] = _get_env_int(f"{prefix}RUN_TIMEOUT_SECONDS")
    else:
        provided["run_timeout_seconds"] = defaults["run_timeout_seconds"]

    if _get_env_optional_str(f"{prefix}REPORT_LANGUAGE"):
        provided["report_language"] = _get_env_str(f"{prefix}REPORT_LANGUAGE")
    else:
        provided["report_language"] = defaults["report_language"]

    if _get_env_optional_str(f"{prefix}HTTP_HOST"):
        provided["http_host"] = _get_env_str(f"{prefix}HTTP_HOST")
    else:
        provided["http_host"] = defaults["http_host"]

    if _get_env_optional_str(f"{prefix}HTTP_PORT"):
        provided["http_port"] = _get_env_int(f"{prefix}HTTP_PORT")
    else:
        provided["http_port"] = defaults["http_port"]

    if _get_env_optional_str(f"{prefix}MCP_MOUNT_PATH"):
        provided["mcp_mount_path"] = _get_env_str(f"{prefix}MCP_MOUNT_PATH")
    else:
        provided["mcp_mount_path"] = defaults["mcp_mount_path"]

    if _get_env_optional_str(f"{prefix}MCP_STREAMABLE_HTTP_PATH"):
        provided["mcp_streamable_http_path"] = _get_env_str(f"{prefix}MCP_STREAMABLE_HTTP_PATH")
    else:
        provided["mcp_streamable_http_path"] = defaults["mcp_streamable_http_path"]

    if _get_env_optional_str(f"{prefix}UVICORN_LOG_LEVEL"):
        provided["uvicorn_log_level"] = _get_env_str(f"{prefix}UVICORN_LOG_LEVEL")
    else:
        provided["uvicorn_log_level"] = defaults["uvicorn_log_level"]

    return Settings(**provided)


class _ApiKeyAsgiWrapper:
    """
    ASGI-обёртка для защиты приложений по заголовку X-API-Key.

    Если ключ пустой, проверка отключена.

    :param app: Внутреннее ASGI приложение.
    :param api_key: Ожидаемое значение ключа.
    """

    def __init__(self, app: Callable, api_key: str) -> None:
        self._app: Callable = app
        self._api_key: str = api_key

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        expected: str = self._api_key
        if not expected:
            await self._app(scope, receive, send)
            return

        method: str = str(scope.get("method", "GET")).upper()
        if method == "OPTIONS":
            await self._app(scope, receive, send)
            return

        header_map: dict[str, str] = {}
        for k, v in scope.get("headers", []) or []:
            if isinstance(k, (bytes, bytearray)) and isinstance(v, (bytes, bytearray)):
                header_map[k.decode("latin-1").lower()] = v.decode("latin-1")

        if header_map.get("x-api-key") != expected:
            body: bytes = b'{"detail":"Unauthorized"}'
            await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())],
                    }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self._app(scope, receive, send)


def create_app() -> FastAPI:
    """
    Создать приложение FastAPI.

    :return: Экземпляр FastAPI.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings: Settings = _load_settings_with_defaults()
        configure_logging(settings.log_level)
        logger: logging.Logger = logging.getLogger(LOGGER_NAME)

        redis: Redis = create_redis_client(settings.redis_dsn)
        await redis.ping()

        arq_pool: ArqRedis = await create_pool(parse_redis_settings(settings.redis_dsn))

        app.state.settings = settings
        app.state.redis = redis
        app.state.arq = arq_pool
        app.state.logger = logger

        router = build_router(settings=settings, redis=redis, arq=arq_pool, logger=logger)
        app.include_router(router)

        mcp = build_mcp(settings=settings, redis=redis, arq=arq_pool, logger=logger)
        mcp_app = _ApiKeyAsgiWrapper(mcp.streamable_http_app(), api_key=settings.api_key)
        app.mount(settings.mcp_mount_path, mcp_app)

        yield

        await arq_pool.close()
        await redis.aclose()

    return FastAPI(title="llamator-mcp-server", version="0.1.0", lifespan=lifespan)


app: FastAPI = create_app()


def main() -> None:
    """
    Точка входа для запуска HTTP сервера.

    :return: None
    """
    import uvicorn

    settings: Settings = _load_settings_with_defaults()
    uvicorn.run(
            "llamator_mcp_server.main:app",
            host=settings.http_host,
            port=settings.http_port,
            log_level=settings.uvicorn_log_level.lower(),
    )