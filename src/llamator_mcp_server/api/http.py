import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Security
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from fastapi.security.api_key import APIKeyHeader
from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import ArtifactFileInfo
from llamator_mcp_server.domain.models import ArtifactsListResponse
from llamator_mcp_server.domain.models import HealthResponse
from llamator_mcp_server.domain.models import LlamatorJobInfo
from llamator_mcp_server.domain.models import LlamatorTestRunRequest
from llamator_mcp_server.domain.models import LlamatorTestRunResponse
from llamator_mcp_server.domain.services import TestRunService
from llamator_mcp_server.domain.services import validate_test_specs
from llamator_mcp_server.infra.artifacts_storage import ArtifactsStorage
from llamator_mcp_server.infra.artifacts_storage import ArtifactsStorageError
from llamator_mcp_server.infra.job_store import JobStore
from redis.asyncio import Redis
from starlette.responses import Response

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


_API_KEY_SCHEME: APIKeyHeader = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="McpApiKey",
)


class _ApiKeyDependency:
    """
    FastAPI dependency enforcing X-API-Key authentication.

    This dependency is intended to be attached to a protected router, while
    public routes (e.g. healthchecks) are mounted on a separate router without it.

    :param settings: Application settings.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings: Settings = settings

    async def __call__(self, x_api_key: str | None = Security(_API_KEY_SCHEME)) -> None:
        await require_api_key(settings=self._settings, x_api_key=x_api_key)


def build_router(
    settings: Settings,
    redis: Redis,
    arq: Any,
    logger: logging.Logger,
    artifacts: ArtifactsStorage,
) -> APIRouter:
    """
    Построить HTTP роутер API.

    :param settings: Настройки приложения.
    :param redis: Redis-клиент.
    :param arq: ARQ pool.
    :param logger: Логгер.
    :return: Роутер FastAPI.
    """
    root_router: APIRouter = APIRouter()

    public_router: APIRouter = APIRouter()
    api_key_dep: _ApiKeyDependency = _ApiKeyDependency(settings=settings)
    protected_router: APIRouter = APIRouter(dependencies=[Depends(api_key_dep)])

    store: JobStore = JobStore(redis=redis, ttl_seconds=settings.job_ttl_seconds)
    service: TestRunService = TestRunService(arq=arq, store=store, settings=settings, logger=logger)

    @public_router.get("/v1/health", response_model=HealthResponse)
    @public_router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """
        Проверка здоровья сервиса.

        :return: Статус сервера.
        """
        return HealthResponse(status="ok")

    @protected_router.post("/v1/tests/runs", response_model=LlamatorTestRunResponse)
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

    @protected_router.get("/v1/tests/runs/{job_id}", response_model=LlamatorJobInfo)
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

    @protected_router.get("/v1/tests/runs/{job_id}/artifacts", response_model=ArtifactsListResponse)
    async def list_artifacts(job_id: str) -> ArtifactsListResponse:
        """
        Получить список файлов артефактов по заданию.

        :param job_id: Идентификатор задания.
        :return: Список файлов (путь, размер, mtime).
        :raises HTTPException: Если задание не найдено или backend артефактов недоступен.
        """
        try:
            await store.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found")

        try:
            files: list[dict[str, Any]] = await artifacts.list_files(job_id)
        except ArtifactsStorageError as e:
            logger.error(f"Artifacts list failed job_id={job_id} error={type(e).__name__}: {e}")
            raise HTTPException(status_code=502, detail="Artifacts backend error")

        for f in files:
            f.pop("full_key", None)

        parsed_files: list[ArtifactFileInfo] = [ArtifactFileInfo.model_validate(f) for f in files]
        return ArtifactsListResponse(job_id=job_id, files=parsed_files)

    @protected_router.get(
        "/v1/tests/runs/{job_id}/artifacts/{path:path}",
        response_model=None,
        responses={
            200: {"description": "Artifact file content."},
            307: {"description": "Temporary redirect to presigned URL (S3 backend)."},
            400: {"description": "Invalid path."},
            404: {"description": "Job or file not found."},
            502: {"description": "Artifacts backend error."},
        },
    )
    async def download_artifact(job_id: str, path: str) -> Response:
        """
        Скачать конкретный файл артефакта.

        :param job_id: Идентификатор задания.
        :param path: Относительный путь файла внутри артефактов задания.
        :return: RedirectResponse (307) при S3 backend или FileResponse (200) при локальном backend.
        :raises HTTPException: Если задание/файл не найден, путь небезопасен или backend артефактов недоступен.
        :raises RuntimeError: Если backend вернул некорректную цель загрузки.
        """
        try:
            await store.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found")

        try:
            target = await artifacts.resolve_download(job_id=job_id, rel_path=path)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid path")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found")
        except ArtifactsStorageError as e:
            logger.error(f"Artifacts download resolve failed job_id={job_id} path={path} error={type(e).__name__}: {e}")
            raise HTTPException(status_code=502, detail="Artifacts backend error")

        if target.redirect_url is not None:
            return RedirectResponse(url=target.redirect_url, status_code=307)

        if target.local_path is None:
            raise RuntimeError("Artifacts backend returned an empty download target.")

        return FileResponse(path=str(target.local_path), filename=target.local_path.name)

    root_router.include_router(public_router)
    root_router.include_router(protected_router)
    return root_router
