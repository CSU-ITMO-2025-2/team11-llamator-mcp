from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """
    Настройки приложения.

    Параметры читаются из переменных окружения с префиксом ``LLAMATOR_MCP_``.

    :param redis_dsn: DSN для подключения к Redis.
    :param artifacts_root: Корневая директория для артефактов запусков LLAMATOR.
    :param api_key: Ключ доступа к HTTP/MCP API (пусто — защита отключена).
    :param log_level: Уровень логирования Python logging.
    :param aux_openai_base_url: Базовый URL OpenAI-совместимого API для вспомогательной LLM (attack/judge по умолчанию).
    :param aux_openai_model: Идентификатор модели для вспомогательной LLM.
    :param aux_openai_api_key: Ключ доступа для вспомогательной LLM.
    :param job_ttl_seconds: TTL (сек) для метаданных и результатов задач в Redis.
    :param run_timeout_seconds: Таймаут (сек) ARQ-задачи. Используется worker-ом.
    :param report_language: Язык отчётов LLAMATOR по умолчанию.
    :param http_host: Адрес bind для HTTP сервера (uvicorn).
    :param http_port: Порт HTTP сервера (uvicorn).
    :param mcp_mount_path: Path, по которому MCP ASGI app монтируется в FastAPI.
    :param mcp_streamable_http_path: Path streamable HTTP внутри MCP ASGI app.
    :param uvicorn_log_level: Уровень логирования uvicorn.
    :raises ValueError: При некорректных параметрах окружения.
    """

    model_config = SettingsConfigDict(env_prefix="LLAMATOR_MCP_", env_file=".env", extra="ignore")

    redis_dsn: str = Field(min_length=1, max_length=2000)
    artifacts_root: Path

    api_key: str = Field(max_length=500)
    log_level: str = Field(min_length=1, max_length=50)

    aux_openai_base_url: str = Field(min_length=1, max_length=2000)
    aux_openai_model: str = Field(min_length=1, max_length=300)
    aux_openai_api_key: str = Field(max_length=1000)

    job_ttl_seconds: int = Field(ge=1)
    run_timeout_seconds: int = Field(ge=1)

    report_language: Literal["en", "ru"]

    http_host: str = Field(min_length=1, max_length=255)
    http_port: int = Field(ge=1, le=65535)

    mcp_mount_path: str = Field(min_length=1, max_length=200)
    mcp_streamable_http_path: str = Field(min_length=1, max_length=200)

    uvicorn_log_level: str = Field(min_length=1, max_length=50)

    @field_validator(
            "redis_dsn",
            "log_level",
            "aux_openai_base_url",
            "aux_openai_model",
            "http_host",
            "uvicorn_log_level",
    )
    @classmethod
    def _strip_required(cls, v: str) -> str:
        val: str = v.strip()
        if not val:
            raise ValueError("Value must be non-empty.")
        return val

    @field_validator("api_key", "aux_openai_api_key")
    @classmethod
    def _strip_optional_secret(cls, v: str) -> str:
        return v.strip()

    @field_validator("mcp_mount_path", "mcp_streamable_http_path")
    @classmethod
    def _validate_url_path(cls, v: str) -> str:
        raw: str = v.strip()
        if not raw:
            raise ValueError("Path must be non-empty.")

        normalized: PurePosixPath = PurePosixPath(raw)
        if normalized.is_absolute() is False:
            normalized = PurePosixPath(f"/{raw.lstrip('/')}")

        if ".." in normalized.parts:
            raise ValueError("Path must not contain '..' segments.")

        if str(normalized) != "/":
            return str(normalized).rstrip("/")
        return "/"