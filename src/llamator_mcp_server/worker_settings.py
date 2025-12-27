from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
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


def _is_empty_aggregated_result(aggregated: dict[str, dict[str, int]]) -> bool:
    return not aggregated


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    """
    Immutable typed view over ARQ worker context.

    This class centralizes access to required dependencies and prevents
    scattered string-key lookups across the code.
    """

    settings: Settings
    logger: logging.Logger
    store: JobStore
    artifacts: ArtifactsStorage
    artifacts_backend_resolved: str

    @classmethod
    def from_ctx(cls, ctx: dict[str, Any]) -> "_ExecutionContext":
        return cls(
            settings=ctx["settings"],
            logger=ctx["logger"],
            store=ctx["store"],
            artifacts=ctx["artifacts_storage"],
            artifacts_backend_resolved=ctx["artifacts_backend_resolved"],
        )


@dataclass(frozen=True, slots=True)
class _RunInputs:
    """
    Validated job inputs required to run LLAMATOR.
    """

    job_id: str
    attack_model: OpenAIClientConfig
    tested_model: OpenAIClientConfig
    judge_model: OpenAIClientConfig
    plan: TestPlan
    run_config: dict[str, Any]
    artifacts_root: Path

    @classmethod
    def from_payload(cls, job_id: str, payload: dict[str, Any]) -> "_RunInputs":
        attack_model: OpenAIClientConfig = _validate_client_config(payload["attack_model"])
        tested_model: OpenAIClientConfig = _validate_client_config(payload["tested_model"])
        judge_model: OpenAIClientConfig = _validate_client_config(payload["judge_model"])
        plan: TestPlan = TestPlan.model_validate(payload["plan"])
        run_config: dict[str, Any] = dict(payload["run_config"])
        artifacts_root: Path = Path(str(run_config["artifacts_path"])).resolve(strict=False)

        return cls(
            job_id=job_id,
            attack_model=attack_model,
            tested_model=tested_model,
            judge_model=judge_model,
            plan=plan,
            run_config=run_config,
            artifacts_root=artifacts_root,
        )

    def to_resolved_run(self) -> ResolvedRun:
        return ResolvedRun(
            job_id=self.job_id,
            attack_model=self.attack_model,
            tested_model=self.tested_model,
            judge_model=self.judge_model,
            plan=self.plan,
            run_config=self.run_config,
            artifacts_root=self.artifacts_root,
        )


class _ArtifactsLifecycle:
    """
    Job artifacts lifecycle operations: upload to backend and local cleanup.

    Local cleanup is only performed for S3 backend because local backend may require
    artifacts to remain accessible via filesystem.
    """

    def __init__(
        self,
        logger: logging.Logger,
        artifacts: ArtifactsStorage,
        job_id: str,
        local_root: Path,
        resolved_backend: str,
    ) -> None:
        self._logger: logging.Logger = logger
        self._artifacts: ArtifactsStorage = artifacts
        self._job_id: str = job_id
        self._local_root: Path = local_root
        self._resolved_backend: str = resolved_backend

    async def upload(self, job_status: str) -> bool:
        """
        Upload job artifacts to the configured backend.

        :param job_status: Job status marker for logs (e.g. "succeeded", "failed").
        :return: True if upload succeeded; otherwise False.
        """
        try:
            self._logger.info(
                f"Worker job_id={self._job_id} status=artifacts_uploading job_status={job_status} path={self._local_root}"
            )
            await self._artifacts.upload_job_artifacts(job_id=self._job_id, local_root=self._local_root)
            self._logger.info(
                f"Worker job_id={self._job_id} status=artifacts_uploaded job_status={job_status} path={self._local_root}"
            )
            return True
        except Exception as exc:
            self._logger.error(
                f"Worker job_id={self._job_id} status=artifacts_upload_failed job_status={job_status} "
                f"error={type(exc).__name__}: {exc}"
            )
            return False

    async def cleanup_local(self, uploaded: bool) -> None:
        """
        Remove local artifacts directory after a successful upload.

        :param uploaded: Indicates whether upload succeeded.
        :return: None
        """
        if self._resolved_backend != "s3":
            return
        if not uploaded:
            return

        try:
            await asyncio.to_thread(shutil.rmtree, self._local_root)
            self._logger.info(f"Worker job_id={self._job_id} status=artifacts_local_cleaned path={self._local_root}")
        except FileNotFoundError:
            return
        except Exception as exc:
            self._logger.warning(
                f"Worker job_id={self._job_id} status=artifacts_local_cleanup_failed "
                f"error={type(exc).__name__}: {exc}"
            )


class _JobExecutor:
    """
    Orchestrates job execution, artifacts handling, and job state persistence.
    """

    def __init__(self, exec_ctx: _ExecutionContext) -> None:
        self._logger: logging.Logger = exec_ctx.logger
        self._store: JobStore = exec_ctx.store
        self._artifacts: ArtifactsStorage = exec_ctx.artifacts
        self._resolved_backend: str = exec_ctx.artifacts_backend_resolved

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id: str = str(payload["job_id"])

        await self._store.update_status(job_id, JobStatus.RUNNING)
        self._logger.info(f"Worker started job_id={job_id} status=running")

        lifecycle: _ArtifactsLifecycle | None = None
        uploaded: bool = False

        try:
            inputs: _RunInputs = _RunInputs.from_payload(job_id=job_id, payload=payload)
            lifecycle = _ArtifactsLifecycle(
                logger=self._logger,
                artifacts=self._artifacts,
                job_id=job_id,
                local_root=inputs.artifacts_root,
                resolved_backend=self._resolved_backend,
            )

            resolved: ResolvedRun = inputs.to_resolved_run()

            runner: LlamatorRunner = LlamatorRunner(logger=self._logger)
            aggregated_raw: Any = await asyncio.to_thread(runner.run, resolved)
            aggregated: dict[str, dict[str, int]] = _validate_start_testing_result(aggregated_raw)

            if _is_empty_aggregated_result(aggregated):
                err_type: str = "EmptyAggregatedResultError"
                err_msg: str = "No tests were executed; aggregated results are empty."

                uploaded = await lifecycle.upload(job_status="failed")
                await self._store.set_error(job_id, err_type, err_msg)
                self._logger.error(f"Worker finished job_id={job_id} status=failed error={err_type}: {err_msg}")

                return {"job_id": job_id, "aggregated": {}, "finished_at": _utcnow().isoformat()}

            uploaded = await lifecycle.upload(job_status="succeeded")

            await self._store.set_result(job_id, aggregated)
            self._logger.info(f"Worker finished job_id={job_id} status=succeeded")
            return {"job_id": job_id, "aggregated": aggregated, "finished_at": _utcnow().isoformat()}
        except Exception as exc:
            if lifecycle is not None:
                uploaded = await lifecycle.upload(job_status="failed")

            await self._store.set_error(job_id, type(exc).__name__, str(exc))
            self._logger.exception(f"Worker failed job_id={job_id} status=failed error={type(exc).__name__}: {exc}")
            raise
        finally:
            if lifecycle is not None:
                await lifecycle.cleanup_local(uploaded=uploaded)


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
    ctx["artifacts_backend_resolved"] = resolved_backend

    logger.info("ARQ worker startup completed status=ready")


async def worker_shutdown(ctx: dict[str, Any]) -> None:
    logger = ctx.get("logger")

    redis = ctx.get("redis_client")
    if redis is not None:
        await redis.aclose()

    if logger is not None:
        logger.info("ARQ worker shutdown completed status=stopped")


async def run_llamator_job(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """
    ARQ задача: выполнить LLAMATOR тестирование.

    :param ctx: Контекст worker-а.
    :param payload: Полезная нагрузка (job_id и конфигурации).
    :return: Результат (агрегированные метрики).
    :raises Exception: Пробрасывает исключение для обработки worker-ом.
    """
    exec_ctx: _ExecutionContext = _ExecutionContext.from_ctx(ctx)
    executor: _JobExecutor = _JobExecutor(exec_ctx=exec_ctx)
    return await executor.execute(payload)


class WorkerSettings:
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    functions = [run_llamator_job]
    redis_settings: RedisSettings = parse_redis_settings(settings.redis_dsn)
    job_timeout: int = settings.run_timeout_seconds
    max_tries: int = 1
