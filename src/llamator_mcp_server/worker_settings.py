from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings
from pydantic import TypeAdapter

from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.config.settings import settings
from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import OpenAIClientConfig
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


_CLIENT_CONFIG_ADAPTER: TypeAdapter[Any] = TypeAdapter(OpenAIClientConfig)
_START_TESTING_RESULT_ADAPTER: TypeAdapter[Any] = TypeAdapter(dict[str, dict[str, int]])


def _validate_client_config(val: Any) -> OpenAIClientConfig:
    if not isinstance(val, dict):
        raise ValueError("ClientConfig payload must be an object.")
    parsed: Any = _CLIENT_CONFIG_ADAPTER.validate_python(val)
    return parsed  # type: ignore[return-value]


def _validate_start_testing_result(val: Any) -> dict[str, dict[str, int]]:
    if not isinstance(val, dict):
        raise ValueError("start_testing result must be an object.")
    parsed: Any = _START_TESTING_RESULT_ADAPTER.validate_python(val)
    return parsed  # type: ignore[return-value]


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
    settings_obj: Settings = ctx["settings"]

    job_id: str = str(payload["job_id"])

    await store.update_status(job_id, JobStatus.RUNNING)
    logger.info(f"Worker started job_id={job_id}")

    try:
        attack_model: OpenAIClientConfig = _validate_client_config(payload["attack_model"])
        tested_model: OpenAIClientConfig = _validate_client_config(payload["tested_model"])
        judge_model: OpenAIClientConfig = _validate_client_config(payload["judge_model"])
        plan: TestPlan = TestPlan.model_validate(payload["plan"])
        run_config: dict[str, Any] = dict(payload["run_config"])

        artifacts_root: Path = Path(str(run_config["artifacts_path"]))

        resolved: ResolvedRun = ResolvedRun(
                job_id=job_id,
                attack_model=attack_model,
                tested_model=tested_model,
                judge_model=judge_model,
                plan=plan,
                run_config=run_config,
                artifacts_root=artifacts_root,
        )

        runner: LlamatorRunner = LlamatorRunner(logger=logger)

        aggregated_raw: Any = await asyncio.to_thread(runner.run, resolved)
        aggregated: dict[str, dict[str, int]] = _validate_start_testing_result(aggregated_raw)

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

    settings: Settings = settings
    redis_settings: RedisSettings = parse_redis_settings(settings.redis_dsn)
    functions = [run_llamator_job]
    on_startup = startup
    on_shutdown = shutdown
    job_timeout = settings.run_timeout_seconds
    allow_abort_jobs = True