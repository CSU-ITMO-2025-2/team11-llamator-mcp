from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings
from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.config.settings import settings
from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import OpenAIClientConfig
from llamator_mcp_server.domain.models import TestPlan
from llamator_mcp_server.infra.artifacts_storage import ArtifactsStorage
from llamator_mcp_server.infra.artifacts_storage import S3ArtifactsStorage
from llamator_mcp_server.infra.artifacts_storage import create_artifacts_storage
from llamator_mcp_server.infra.job_store import JobStore
from llamator_mcp_server.infra.llamator_runner import LlamatorRunner
from llamator_mcp_server.infra.llamator_runner import ResolvedRun
from llamator_mcp_server.infra.redis import create_redis_client
from llamator_mcp_server.infra.redis import parse_redis_settings
from llamator_mcp_server.utils.logging import LOGGER_NAME
from llamator_mcp_server.utils.logging import configure_logging
from pydantic import TypeAdapter


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


async def worker_startup(ctx: dict[str, Any]) -> None:
    configure_logging(settings.log_level)
    logger: logging.Logger = logging.getLogger(LOGGER_NAME)

    redis = create_redis_client(settings.redis_dsn)
    await redis.ping()

    artifacts: ArtifactsStorage = create_artifacts_storage(
            settings=settings,
            presign_expires_seconds=15 * 60,
            list_max_keys=1000,
    )

    resolved_backend: str = "local"
    if isinstance(artifacts, S3ArtifactsStorage):
        resolved_backend = "s3"

    s3_configured: bool = all(
            [
                settings.s3_endpoint_url,
                settings.s3_bucket,
                settings.s3_access_key_id,
                settings.s3_secret_access_key,
            ]
    )
    logger.info(
            f"Artifacts backend initialized configured={settings.artifacts_backend} "
            f"resolved={resolved_backend} s3_configured={s3_configured}"
    )

    ctx["settings"] = settings
    ctx["logger"] = logger
    ctx["redis_client"] = redis
    ctx["store"] = JobStore(redis=redis, ttl_seconds=settings.job_ttl_seconds)
    ctx["artifacts_storage"] = artifacts

    logger.info(f"ARQ worker startup completed status=ready")


async def worker_shutdown(ctx: dict[str, Any]) -> None:
    logger = ctx.get("logger")

    redis = ctx.get("redis_client")
    if redis is not None:
        await redis.aclose()

    if logger is not None:
        logger.info(f"ARQ worker shutdown completed status=stopped")


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
    artifacts: ArtifactsStorage = ctx["artifacts_storage"]
    settings_obj: Settings = ctx["settings"]
    job_id: str = str(payload["job_id"])

    await store.update_status(job_id, JobStatus.RUNNING)
    logger.info(f"Worker started job_id={job_id} status=running")

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

        upload_root: Path = (settings_obj.artifacts_root / job_id).resolve(strict=False)
        logger.info(f"Worker uploading artifacts job_id={job_id} path={upload_root}")
        await artifacts.upload_job_artifacts(job_id=job_id, local_root=upload_root)
        logger.info(f"Worker uploaded artifacts job_id={job_id}")

        await store.set_result(job_id, aggregated)
        logger.info(f"Worker finished job_id={job_id} status=succeeded")
        return {"job_id": job_id, "aggregated": aggregated, "finished_at": _utcnow().isoformat()}
    except Exception as exc:
        await store.set_error(job_id, type(exc).__name__, str(exc))
        logger.exception(f"Worker failed job_id={job_id} status=failed error={type(exc).__name__}: {exc}")
        raise


class WorkerSettings:
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    functions = [run_llamator_job]
    redis_settings: RedisSettings = parse_redis_settings(settings.redis_dsn)
    job_timeout: int = settings.run_timeout_seconds