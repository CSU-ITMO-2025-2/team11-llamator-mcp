from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from datetime import timezone
from typing import Any

from arq.connections import RedisSettings
from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import ClientConfig
from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import TestPlan
from llamator_mcp_server.infra.job_store import JobStore
from llamator_mcp_server.infra.llamator_runner import LlamatorRunner
from llamator_mcp_server.infra.llamator_runner import ResolvedRun
from llamator_mcp_server.infra.redis import create_redis_client
from llamator_mcp_server.infra.redis import parse_redis_settings
from llamator_mcp_server.utils.logging import LOGGER_NAME
from llamator_mcp_server.utils.logging import configure_logging


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_env_optional_str(name: str) -> str:
    return os.environ.get(name, "").strip()


def _get_env_int(name: str) -> int:
    raw: str = os.environ.get(name, "").strip()
    if not raw:
        raise ValueError(f"Missing required env var: {name}")
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"Invalid int for {name}: {raw}") from e


def _load_settings_with_defaults() -> Settings:
    """
    Загрузить настройки worker-а, подставляя значения по умолчанию на уровне точки входа.

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

    data: dict[str, object] = {}

    data["redis_dsn"] = _get_env_optional_str(f"{prefix}REDIS_DSN") or str(defaults["redis_dsn"])
    data["artifacts_root"] = _get_env_optional_str(f"{prefix}ARTIFACTS_ROOT") or str(defaults["artifacts_root"])
    data["api_key"] = _get_env_optional_str(f"{prefix}API_KEY") or str(defaults["api_key"])
    data["log_level"] = _get_env_optional_str(f"{prefix}LOG_LEVEL") or str(defaults["log_level"])

    data["aux_openai_base_url"] = _get_env_optional_str(f"{prefix}AUX_OPENAI_BASE_URL") or str(
            defaults["aux_openai_base_url"]
    )
    data["aux_openai_model"] = _get_env_optional_str(f"{prefix}AUX_OPENAI_MODEL") or str(defaults["aux_openai_model"])
    data["aux_openai_api_key"] = _get_env_optional_str(f"{prefix}AUX_OPENAI_API_KEY") or str(
            defaults["aux_openai_api_key"]
    )

    if _get_env_optional_str(f"{prefix}JOB_TTL_SECONDS"):
        data["job_ttl_seconds"] = _get_env_int(f"{prefix}JOB_TTL_SECONDS")
    else:
        data["job_ttl_seconds"] = defaults["job_ttl_seconds"]

    if _get_env_optional_str(f"{prefix}RUN_TIMEOUT_SECONDS"):
        data["run_timeout_seconds"] = _get_env_int(f"{prefix}RUN_TIMEOUT_SECONDS")
    else:
        data["run_timeout_seconds"] = defaults["run_timeout_seconds"]

    data["report_language"] = _get_env_optional_str(f"{prefix}REPORT_LANGUAGE") or str(defaults["report_language"])

    data["http_host"] = _get_env_optional_str(f"{prefix}HTTP_HOST") or str(defaults["http_host"])
    if _get_env_optional_str(f"{prefix}HTTP_PORT"):
        data["http_port"] = _get_env_int(f"{prefix}HTTP_PORT")
    else:
        data["http_port"] = defaults["http_port"]

    data["mcp_mount_path"] = _get_env_optional_str(f"{prefix}MCP_MOUNT_PATH") or str(defaults["mcp_mount_path"])
    data["mcp_streamable_http_path"] = _get_env_optional_str(f"{prefix}MCP_STREAMABLE_HTTP_PATH") or str(
            defaults["mcp_streamable_http_path"]
    )

    data["uvicorn_log_level"] = _get_env_optional_str(f"{prefix}UVICORN_LOG_LEVEL") or str(
            defaults["uvicorn_log_level"])

    return Settings(**data)


async def run_llamator_job(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """
    ARQ задача: выполнить LLAMATOR тестирование.

    :param ctx: Контекст worker-а.
    :param payload: Полезная нагрузка (job_id и конфигурации).
    :return: Результат (агрегированные метрики).
    :raises Exception: Пробрасывает исключение для обработки worker-ом.
    """
    logger: logging.Logger = ctx["logger"]
    store: JobStore = ctx["store"]
    settings: Settings = ctx["settings"]

    job_id: str = str(payload["job_id"])

    await store.update_status(job_id, JobStatus.RUNNING)
    logger.info(f"Worker started job_id={job_id}")

    attack_model: ClientConfig = ClientConfig.model_validate(payload["attack_model"])
    tested_model: ClientConfig = ClientConfig.model_validate(payload["tested_model"])
    judge_model: ClientConfig | None = (
        ClientConfig.model_validate(payload["judge_model"]) if payload.get("judge_model") is not None else None
    )
    plan: TestPlan = TestPlan.model_validate(payload["plan"])
    run_config: dict[str, Any] = dict(payload["run_config"])

    resolved: ResolvedRun = ResolvedRun(
            job_id=job_id,
            attack_model=attack_model,
            tested_model=tested_model,
            judge_model=judge_model,
            plan=plan,
            run_config=run_config,
            artifacts_root=settings.artifacts_root / job_id,
    )

    runner: LlamatorRunner = LlamatorRunner(logger=logger)

    try:
        aggregated: dict[str, dict[str, int]] = await asyncio.to_thread(runner.run, resolved)
        await store.set_result(job_id, aggregated)
        logger.info(f"Worker finished job_id={job_id} status=succeeded")
        return {"job_id": job_id, "aggregated": aggregated, "finished_at": _utcnow().isoformat()}
    except Exception as exc:
        err_type: str = type(exc).__name__
        msg: str = str(exc)
        await store.set_error(job_id, error_type=err_type, message=msg)
        logger.error(f"Worker finished job_id={job_id} status=failed error_type={err_type} message={msg}")
        raise


async def startup(ctx: dict[str, Any]) -> None:
    """
    Startup-хук ARQ worker-а.

    :param ctx: Контекст worker-а.
    :return: None
    """
    settings: Settings = _load_settings_with_defaults()
    configure_logging(settings.log_level)
    logger: logging.Logger = logging.getLogger(LOGGER_NAME)

    redis = create_redis_client(settings.redis_dsn)
    await redis.ping()

    ctx["settings"] = settings
    ctx["logger"] = logger
    ctx["redis_client"] = redis
    ctx["store"] = JobStore(redis=redis, ttl_seconds=settings.job_ttl_seconds)

    logger.info("ARQ worker startup completed")


async def shutdown(ctx: dict[str, Any]) -> None:
    """
    Shutdown-хук ARQ worker-а.

    :param ctx: Контекст worker-а.
    :return: None
    """
    redis = ctx.get("redis_client")
    if redis is not None:
        await redis.aclose()


class WorkerSettings:
    """
    Настройки ARQ worker-а.

    Используется CLI командой: ``arq llamator_mcp_server.worker_settings.WorkerSettings``.
    """
    settings: Settings = _load_settings_with_defaults()
    redis_settings: RedisSettings = parse_redis_settings(settings.redis_dsn)
    functions = [run_llamator_job]
    on_startup = startup
    on_shutdown = shutdown
    job_timeout = settings.run_timeout_seconds
    allow_abort_jobs = True