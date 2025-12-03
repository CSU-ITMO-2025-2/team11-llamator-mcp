from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from arq.connections import ArqRedis
from mcp.server.fastmcp import FastMCP
from redis.asyncio import Redis

from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import JobStatus, LlamatorJobInfo, LlamatorTestRunRequest
from llamator_mcp_server.domain.services import TestRunService, validate_test_specs
from llamator_mcp_server.infra.job_store import JobStore


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


def _extract_aggregated_result(info: LlamatorJobInfo) -> dict[str, dict[str, int]]:
    if info.status == JobStatus.SUCCEEDED:
        if info.result is None:
            raise RuntimeError("Job succeeded but result is missing.")
        return dict(info.result.aggregated)

    if info.status == JobStatus.FAILED:
        if info.error is None:
            raise RuntimeError("Job failed but error is missing.")
        raise RuntimeError(f"Job failed: {info.error.error_type}: {info.error.message}")

    raise ValueError(f"Job not finished: {info.status.value}")


def build_mcp(
    settings: Settings,
    redis: Redis,
    arq: ArqRedis,
    logger: logging.Logger,
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
    async def create_llamator_run(req: LlamatorTestRunRequest) -> dict[str, dict[str, int]]:
        """
        Create a LLAMATOR job and return the aggregated result after completion.

        :param req: Run request.
        :return: Aggregated LLAMATOR results for a succeeded job.
        :raises ValueError: If the request is invalid or the job is not finished.
        :raises TimeoutError: If the job does not complete within the configured timeout.
        :raises KeyError: If the job cannot be found in the store.
        :raises RuntimeError: If the job failed or returned an inconsistent state.
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
        return _extract_aggregated_result(info)

    @mcp.tool()
    async def get_llamator_run(job_id: str) -> dict[str, dict[str, int]]:
        """
        Return aggregated LLAMATOR results for a finished job.

        :param job_id: Job identifier.
        :return: Aggregated LLAMATOR results for a succeeded job.
        :raises KeyError: If the job cannot be found in the store.
        :raises ValueError: If the job is not finished yet.
        :raises RuntimeError: If the job failed or returned an inconsistent state.
        """
        info: LlamatorJobInfo = await store.get(job_id)
        return _extract_aggregated_result(info)

    return mcp
