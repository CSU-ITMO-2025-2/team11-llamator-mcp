from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from typing import Any

from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import LlamatorJobError
from llamator_mcp_server.domain.models import LlamatorJobInfo
from llamator_mcp_server.domain.models import LlamatorJobResult
from redis.asyncio import Redis


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_error_notice(error_type: str, message: str) -> str:
    msg: str = str(message)
    if msg:
        return f"{error_type}: {msg}"
    return f"{error_type}"


class JobStore:
    """
    Хранилище состояния заданий на базе Redis.

    :param redis: Redis-клиент.
    :param ttl_seconds: TTL ключей заданий.
    """

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis: Redis = redis
        self._ttl_seconds: int = ttl_seconds

    async def create(self, job_id: str, request_redacted: dict[str, object]) -> LlamatorJobInfo:
        """
        Создать запись задания.

        :param job_id: Идентификатор задания.
        :param request_redacted: Запрос с редактированными секретами.
        :return: Состояние задания.
        """
        now: datetime = _utcnow()
        info: LlamatorJobInfo = LlamatorJobInfo(
            job_id=job_id,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            request=request_redacted,
            result=None,
            error=None,
            error_notice=None,
        )
        await self._set(job_id, info)
        return info

    async def update_status(self, job_id: str, status: JobStatus) -> None:
        """
        Обновить статус задания.

        :param job_id: Идентификатор задания.
        :param status: Новый статус.
        :return: None
        :raises KeyError: Если задание не найдено.
        """
        info: LlamatorJobInfo = await self.get(job_id)
        updated: LlamatorJobInfo = info.model_copy(update={"status": status, "updated_at": _utcnow()})
        await self._set(job_id, updated)

    async def set_result(self, job_id: str, aggregated: dict[str, dict[str, int]]) -> None:
        """
        Сохранить результат выполнения задания.

        :param job_id: Идентификатор задания.
        :param aggregated: Агрегированные результаты.
        :return: None
        :raises KeyError: Если задание не найдено.
        """
        info: LlamatorJobInfo = await self.get(job_id)
        result: LlamatorJobResult = LlamatorJobResult(aggregated=aggregated, finished_at=_utcnow())
        updated: LlamatorJobInfo = info.model_copy(
            update={
                "status": JobStatus.SUCCEEDED,
                "updated_at": _utcnow(),
                "result": result,
                "error": None,
                "error_notice": None,
            }
        )
        await self._set(job_id, updated)

    async def set_error(self, job_id: str, error_type: str, message: str) -> None:
        """
        Сохранить ошибку выполнения задания.

        :param job_id: Идентификатор задания.
        :param error_type: Тип исключения.
        :param message: Сообщение.
        :return: None
        :raises KeyError: Если задание не найдено.
        """
        info: LlamatorJobInfo = await self.get(job_id)
        error: LlamatorJobError = LlamatorJobError(error_type=error_type, message=message, occurred_at=_utcnow())
        error_notice: str = _build_error_notice(error_type=error_type, message=message)
        updated: LlamatorJobInfo = info.model_copy(
            update={
                "status": JobStatus.FAILED,
                "updated_at": _utcnow(),
                "error": error,
                "error_notice": error_notice,
            }
        )
        await self._set(job_id, updated)

    async def get(self, job_id: str) -> LlamatorJobInfo:
        """
        Получить состояние задания.

        :param job_id: Идентификатор задания.
        :return: Состояние задания.
        :raises KeyError: Если задание не найдено.
        """
        key: str = self._key(job_id)
        raw: str | None = await self._redis.get(key)
        if raw is None:
            raise KeyError(f"Job not found: {job_id}")
        payload: dict[str, Any] = json.loads(raw)
        return LlamatorJobInfo.model_validate(payload)

    async def _set(self, job_id: str, info: LlamatorJobInfo) -> None:
        """
        Внутренний метод: сохранить состояние задания в Redis.
        """
        key: str = self._key(job_id)
        raw: str = info.model_dump_json()
        await self._redis.set(key, raw, ex=self._ttl_seconds)

    @staticmethod
    def _key(job_id: str) -> str:
        return f"llamator:job:{job_id}"
