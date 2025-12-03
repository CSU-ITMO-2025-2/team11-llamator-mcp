# llamator-mcp-server/src/llamator_mcp_server/api/mcp_server.py
from __future__ import annotations

import asyncio
import logging
from typing import Final

from arq.connections import ArqRedis
from mcp.server.fastmcp import FastMCP
from redis.asyncio import Redis

from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import LlamatorJobInfo
from llamator_mcp_server.domain.models import LlamatorTestRunRequest
from llamator_mcp_server.domain.services import TestRunService
from llamator_mcp_server.domain.services import validate_test_specs
from llamator_mcp_server.infra.job_store import JobStore


def _is_terminal_status(status: JobStatus) -> bool:
    return status in (JobStatus.SUCCEEDED, JobStatus.FAILED)


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
    )

    store: JobStore = JobStore(redis=redis, ttl_seconds=settings.job_ttl_seconds)
    service: TestRunService = TestRunService(arq=arq, store=store, settings=settings, logger=logger)

    @mcp.tool()
    async def create_llamator_run(req: LlamatorTestRunRequest) -> dict[str, dict[str, int]]:
        """
        Создать задание на тестирование LLM endpoint-а через LLAMATOR и вернуть финальный результат.

        :param req: Запрос запуска.
        :return: Финальное состояние задания (SUCCEEDED/FAILED) с результатом или ошибкой.
        :raises ValueError: При некорректных данных.
        :raises TimeoutError: Если выполнение не завершилось за таймаут.
        :raises KeyError: Если задание не найдено (неожиданно для только что созданного).
        """
        validate_test_specs(req.plan.basic_tests, req.plan.custom_tests)
        submitted = await service.submit(req)
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
        Получить состояние задания LLAMATOR.

        :param job_id: Идентификатор задания.
        :return: Статус и результаты (если доступны).
        :raises KeyError: Если задание не найдено.
        """
        info: LlamatorJobInfo = await store.get(job_id)
        return _extract_aggregated_result(info)

    return mcp