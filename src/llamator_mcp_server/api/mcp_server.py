from __future__ import annotations

import asyncio
import logging
from typing import Any
from typing import Final

from arq.connections import ArqRedis
from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import LlamatorJobInfo
from llamator_mcp_server.domain.models import LlamatorTestRunRequest
from llamator_mcp_server.domain.services import TestRunService
from llamator_mcp_server.domain.services import validate_test_specs
from llamator_mcp_server.infra.artifacts_storage import ARTIFACTS_ARCHIVE_NAME
from llamator_mcp_server.infra.artifacts_storage import ArtifactsStorage
from llamator_mcp_server.infra.artifacts_storage import ArtifactsStorageError
from llamator_mcp_server.infra.job_store import JobStore
from mcp.server.fastmcp import FastMCP
from redis.asyncio import Redis


def _is_terminal_status(status: JobStatus) -> bool:
    return status in (JobStatus.SUCCEEDED, JobStatus.FAILED)


def _safe_log_request(req: LlamatorTestRunRequest) -> dict[str, Any]:
    """
    Build a safe-to-log representation of LlamatorTestRunRequest.

    The function removes secrets (API keys) and keeps only a boolean marker
    indicating whether a key was provided.

    :param req: Incoming request model.
    :return: JSON-serializable safe payload for logs.
    """
    tested = req.tested_model
    tested_safe: dict[str, Any] = {
        "kind": "openai",
        "base_url": str(tested.base_url),
        "model": tested.model,
        "temperature": tested.temperature,
        "system_prompts": list(tested.system_prompts) if tested.system_prompts is not None else None,
        "model_description": tested.model_description,
        "api_key_present": bool(tested.api_key),
    }
    return {
        "tested_model": tested_safe,
        "run_config": req.run_config.model_dump(mode="json") if req.run_config is not None else None,
        "plan": req.plan.model_dump(mode="json"),
    }


async def _await_job_completion(
    store: JobStore,
    job_id: str,
    timeout_seconds: int,
) -> LlamatorJobInfo:
    """
    Дождаться завершения задания (SUCCEEDED/FAILED), опрашивая JobStore.

    :param store: Хранилище заданий.
    :param job_id: Идентификатор задания.
    :param timeout_seconds: Таймаут ожидания в секундах.
    :return: Финальное состояние задания.
    :raises TimeoutError: Если задание не завершилось за отведённое время.
    :raises KeyError: Если задание не найдено.
    """
    loop = asyncio.get_running_loop()
    deadline: float = loop.time() + float(timeout_seconds)
    poll_interval_s: Final[float] = 0.25

    while True:
        info: LlamatorJobInfo = await store.get(job_id)
        if _is_terminal_status(info.status):
            return info

        now: float = loop.time()
        if now >= deadline:
            raise TimeoutError(f"Job timeout: {job_id}")

        sleep_for: float = min(poll_interval_s, max(0.0, deadline - now))
        await asyncio.sleep(sleep_for)


def _build_error_notice(info: LlamatorJobInfo) -> str | None:
    err = info.error
    if err is None:
        return None
    if err.message:
        return f"{err.error_type}: {err.message}"
    return f"{err.error_type}"


def _extract_aggregated_or_empty(info: LlamatorJobInfo) -> dict[str, dict[str, int]]:
    if info.status == JobStatus.SUCCEEDED:
        if info.result is None:
            raise RuntimeError("Job succeeded but result is missing.")
        return dict(info.result.aggregated)

    if info.status == JobStatus.FAILED:
        return {}

    raise ValueError(f"Job not finished: {info.status.value}")


async def _try_get_artifacts_download_url(artifacts: ArtifactsStorage, job_id: str) -> str | None:
    """
    Resolve artifacts archive download URL for S3 backend.

    :param artifacts: Artifacts storage backend.
    :param job_id: Job identifier.
    :return: Presigned URL if available; otherwise None.
    """
    try:
        target = await artifacts.resolve_download(job_id=job_id, rel_path=ARTIFACTS_ARCHIVE_NAME)
    except (FileNotFoundError, ValueError):
        return None
    except ArtifactsStorageError:
        return None

    return target.redirect_url


def build_mcp(
    settings: Settings,
    redis: Redis,
    arq: ArqRedis,
    logger: logging.Logger,
    artifacts: ArtifactsStorage,
) -> FastMCP:
    """
    Построить MCP сервер с инструментами для запуска и мониторинга LLAMATOR.

    :param settings: Настройки приложения.
    :param redis: Redis-клиент.
    :param arq: ARQ pool.
    :param logger: Логгер.
    :return: Экземпляр FastMCP.
    """
    mcp: FastMCP = FastMCP(
        name="llamator-mcp-server",
        stateless_http=True,
        streamable_http_path=settings.mcp_streamable_http_path,
        json_response=True,
    )

    store: JobStore = JobStore(redis=redis, ttl_seconds=settings.job_ttl_seconds)
    service: TestRunService = TestRunService(arq=arq, store=store, settings=settings, logger=logger)

    @mcp.tool()
    async def create_llamator_run(req: LlamatorTestRunRequest) -> dict[str, Any]:
        """
        Create a LLAMATOR job and return the aggregated result after completion.

        :param req: Run request.
        :return: A dict with keys:
            - job_id: str
            - aggregated: dict[str, dict[str, int]]
            - artifacts_download_url: str | None
            - error_notice: str | None
        :raises ValueError: If the request is invalid or the job is not finished.
        :raises TimeoutError: If the job does not complete within the configured timeout.
        :raises KeyError: If the job cannot be found in the store.
        :raises RuntimeError: If the job returned an inconsistent state (e.g. succeeded but result is missing).
        """
        logger.info(f"Received MCP create_llamator_run parameters: {_safe_log_request(req)}")
        validate_test_specs(req.plan.basic_tests, req.plan.custom_tests)

        submitted = await service.submit(req)
        logger.info(f"Enqueued LLAMATOR job via MCP job_id={submitted.job_id}")

        logger.info(f"Awaiting LLAMATOR job completion job_id={submitted.job_id}")
        info: LlamatorJobInfo = await _await_job_completion(
            store=store,
            job_id=submitted.job_id,
            timeout_seconds=settings.run_timeout_seconds,
        )

        aggregated: dict[str, dict[str, int]] = _extract_aggregated_or_empty(info)
        artifacts_url: str | None = await _try_get_artifacts_download_url(artifacts=artifacts, job_id=submitted.job_id)
        error_notice: str | None = info.error_notice if info.error_notice is not None else _build_error_notice(info)

        return {
            "job_id": submitted.job_id,
            "aggregated": aggregated,
            "artifacts_download_url": artifacts_url,
            "error_notice": error_notice,
        }

    @mcp.tool()
    async def get_llamator_run(job_id: str) -> dict[str, Any]:
        """
        Return aggregated LLAMATOR results for a finished job.

        :param job_id: Job identifier.
        :return: A dict with keys:
            - job_id: str
            - aggregated: dict[str, dict[str, int]]
            - artifacts_download_url: str | None
            - error_notice: str | None
        :raises KeyError: If the job cannot be found in the store.
        :raises ValueError: If the job is not finished yet.
        :raises RuntimeError: If the job returned an inconsistent state (e.g. succeeded but result is missing).
        """
        info: LlamatorJobInfo = await store.get(job_id)
        aggregated: dict[str, dict[str, int]] = _extract_aggregated_or_empty(info)
        artifacts_url: str | None = await _try_get_artifacts_download_url(artifacts=artifacts, job_id=job_id)
        error_notice: str | None = info.error_notice if info.error_notice is not None else _build_error_notice(info)

        return {
            "job_id": job_id,
            "aggregated": aggregated,
            "artifacts_download_url": artifacts_url,
            "error_notice": error_notice,
        }

    return mcp
