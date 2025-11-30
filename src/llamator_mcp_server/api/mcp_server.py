from __future__ import annotations

import logging

from arq.connections import ArqRedis
from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import LlamatorJobInfo
from llamator_mcp_server.domain.models import LlamatorTestRunRequest
from llamator_mcp_server.domain.models import LlamatorTestRunResponse
from llamator_mcp_server.domain.services import TestRunService
from llamator_mcp_server.domain.services import validate_test_specs
from llamator_mcp_server.infra.job_store import JobStore
from mcp.server.fastmcp import FastMCP
from redis.asyncio import Redis


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
    mcp: FastMCP = FastMCP(name="llamator-mcp-server", stateless_http=True, streamable_http_path="/")

    store: JobStore = JobStore(redis=redis, ttl_seconds=settings.job_ttl_seconds)
    service: TestRunService = TestRunService(arq=arq, store=store, settings=settings, logger=logger)

    @mcp.tool()
    async def create_llamator_run(req: LlamatorTestRunRequest) -> LlamatorTestRunResponse:
        """
        Создать задание на тестирование LLM endpoint-а через LLAMATOR.

        :param req: Запрос запуска.
        :return: Ответ с job_id.
        :raises ValueError: При некорректных данных.
        """
        validate_test_specs(req.plan.basic_tests, req.plan.custom_tests)
        result = await service.submit(req)
        return LlamatorTestRunResponse(job_id=result.job_id, status=result.status, created_at=result.created_at)

    @mcp.tool()
    async def get_llamator_run(job_id: str) -> LlamatorJobInfo:
        """
        Получить состояние задания LLAMATOR.

        :param job_id: Идентификатор задания.
        :return: Статус и результаты (если доступны).
        :raises KeyError: Если задание не найдено.
        """
        return await store.get(job_id)

    return mcp