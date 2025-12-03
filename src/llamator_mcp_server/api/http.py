from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from redis.asyncio import Redis

from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import LlamatorJobInfo, LlamatorTestRunRequest, LlamatorTestRunResponse
from llamator_mcp_server.domain.services import TestRunService, validate_test_specs
from llamator_mcp_server.infra.job_store import JobStore

from .security import require_api_key


def _safe_join(root: Path, *parts: str) -> Path:
    """
    Безопасно соединить корневой путь с относительным.

    Генерирует абсолютный путь и проверяет, что он лежит внутри корня.
    """
    candidate: Path = (root.joinpath(*parts)).resolve(strict=False)
    root_resolved: Path = root.resolve(strict=False)
    if root_resolved not in candidate.parents and candidate != root_resolved:
        raise ValueError("Unsafe path.")
    return candidate


def _list_files(root: Path) -> list[dict[str, Any]]:
    """
    Получить список всех файлов в заданной корневой директории.

    Возвращает информацию о каждом файле: относительный путь, размер, время изменения.
    """
    results: list[dict[str, Any]] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p: Path = Path(dirpath) / name
            try:
                rel: str = str(p.relative_to(root))
            except ValueError:
                continue
            st = p.stat()
            results.append({"path": rel, "size_bytes": st.st_size, "mtime": st.st_mtime})
    results.sort(key=lambda x: x["path"])
    return results


def build_router(
    settings: Settings,
    redis: Redis,
    arq: Any,
    logger: logging.Logger,
) -> APIRouter:
    """
    Построить HTTP роутер API.

    :param settings: Настройки приложения.
    :param redis: Redis-клиент.
    :param arq: ARQ pool.
    :param logger: Логгер.
    :return: Роутер FastAPI.
    """

    async def _require_api_key_dep(request: Request) -> None:
        await require_api_key(settings=settings, x_api_key=request.headers.get("x-api-key"))

    router: APIRouter = APIRouter(dependencies=[Depends(_require_api_key_dep)])

    store: JobStore = JobStore(redis=redis, ttl_seconds=settings.job_ttl_seconds)
    service: TestRunService = TestRunService(arq=arq, store=store, settings=settings, logger=logger)

    @router.post("/v1/tests/runs", response_model=LlamatorTestRunResponse)
    async def create_run(req: LlamatorTestRunRequest) -> LlamatorTestRunResponse:
        """
        Создать задание на тестирование.

        :param req: Запрос запуска.
        :return: Ответ с job_id.
        :raises HTTPException: При ошибке валидации входных данных.
        """
        try:
            validate_test_specs(req.plan.basic_tests, req.plan.custom_tests)
        except ValueError as e:
            logger.warning(f"Validation error in create_run: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        result = await service.submit(req)
        return LlamatorTestRunResponse(job_id=result.job_id, status=result.status, created_at=result.created_at)

    @router.get("/v1/tests/runs/{job_id}", response_model=LlamatorJobInfo)
    async def get_run(job_id: str) -> LlamatorJobInfo:
        """
        Получить состояние задания.

        :param job_id: Идентификатор задания.
        :return: Состояние задания.
        :raises HTTPException: Если задание не найдено.
        """
        try:
            return await store.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found")

    @router.get("/v1/tests/runs/{job_id}/artifacts")
    async def list_artifacts(job_id: str) -> dict[str, Any]:
        """
        Получить список файлов артефактов по заданию.

        :param job_id: Идентификатор задания.
        :return: Список файлов (путь, размер, mtime).
        :raises HTTPException: Если задание не найдено.
        """
        try:
            await store.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found")

        root: Path = settings.artifacts_root / job_id
        if not root.exists():
            return {"job_id": job_id, "files": []}
        return {"job_id": job_id, "files": _list_files(root)}

    @router.get("/v1/tests/runs/{job_id}/artifacts/{path:path}")
    async def download_artifact(job_id: str, path: str) -> FileResponse:
        """
        Скачать конкретный файл артефакта.

        :param job_id: Идентификатор задания.
        :param path: Относительный путь файла внутри артефактов задания.
        :return: Ответ с файлом (FileResponse).
        :raises HTTPException: Если файл не найден или путь небезопасен.
        """
        try:
            await store.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found")

        artifacts_root: Path = settings.artifacts_root / job_id
        try:
            file_path: Path = _safe_join(artifacts_root, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid path")

        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(path=str(file_path), filename=file_path.name)

    @router.get("/v1/health")
    async def health() -> dict[str, str]:
        """
        Проверка здоровья сервиса.

        :return: Статус сервера.
        """
        return {"status": "ok"}

    return router
