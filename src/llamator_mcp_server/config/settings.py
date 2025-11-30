from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """
    Настройки приложения.

    Параметры читаются из переменных окружения с префиксом ``LLAMATOR_MCP_``.

    :param redis_dsn: DSN для подключения к Redis.
    :param artifacts_root: Корневая директория для артефактов запусков LLAMATOR.
    :param api_key: Опциональный ключ доступа к HTTP/MCP API (если пусто — защита отключена).
    :param log_level: Уровень логирования Python logging.
    :param aux_openai_base_url: Базовый URL OpenAI-совместимого API для вспомогательной LLM (attack/judge по умолчанию).
    :param aux_openai_model: Идентификатор модели для вспомогательной LLM.
    :param aux_openai_api_key: Ключ доступа для вспомогательной LLM.
    :param job_ttl_seconds: TTL (сек) для метаданных и результатов задач в Redis.
    :param run_timeout_seconds: Таймаут (сек) ARQ-задачи. Используется worker-ом.
    :param report_language: Язык отчётов LLAMATOR по умолчанию.
    :raises ValueError: При некорректных параметрах окружения.
    """

    model_config = SettingsConfigDict(env_prefix="LLAMATOR_MCP_", env_file=".env", extra="ignore")

    redis_dsn: str = Field(default="redis://redis:6379/0")
    artifacts_root: Path = Field(default=Path("/data/artifacts"))

    api_key: str | None = Field(default=None)
    log_level: str = Field(default="INFO")

    aux_openai_base_url: str = Field(default="http://tgi:80/v1")
    aux_openai_model: str = Field(default="tgi")
    aux_openai_api_key: str = Field(default="dummy")

    job_ttl_seconds: int = Field(default=7 * 24 * 60 * 60)
    run_timeout_seconds: int = Field(default=60 * 60)

    report_language: Literal["en", "ru"] = Field(default="en")