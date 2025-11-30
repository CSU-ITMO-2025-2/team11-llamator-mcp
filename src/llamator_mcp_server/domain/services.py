from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from arq.connections import ArqRedis
from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import BasicTestSpec
from llamator_mcp_server.domain.models import ClientConfig
from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import LangChainClientConfig
from llamator_mcp_server.domain.models import LlamatorRunConfig
from llamator_mcp_server.domain.models import LlamatorTestRunRequest
from llamator_mcp_server.domain.models import OpenAIClientConfig
from llamator_mcp_server.domain.models import TestParameter
from llamator_mcp_server.infra.job_store import JobStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _redact_client(cfg: ClientConfig) -> dict[str, Any]:
    if isinstance(cfg, OpenAIClientConfig):
        return {
            "kind": "openai",
            "base_url": str(cfg.base_url),
            "model": cfg.model,
            "temperature": cfg.temperature,
            "system_prompts": list(cfg.system_prompts) if cfg.system_prompts is not None else None,
            "model_description": cfg.model_description,
            "api_key_present": bool(cfg.api_key),
        }
    if isinstance(cfg, LangChainClientConfig):
        return {
            "kind": "langchain",
            "backend": cfg.backend,
            "system_prompts": list(cfg.system_prompts) if cfg.system_prompts is not None else None,
            "model_description": cfg.model_description,
            "init_params": [{"name": p.name, "value": "<redacted>"} for p in cfg.init_params],
        }
    return {"kind": "unknown"}


def _redact_request(req: LlamatorTestRunRequest, attack: ClientConfig, judge: ClientConfig | None) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "preset_name": req.plan.preset_name,
        "num_threads": req.plan.num_threads,
        "basic_tests": [
            {"code_name": t.code_name, "params": [{"name": p.name, "value": p.value} for p in t.params]}
            for t in (req.plan.basic_tests or ())
        ],
        "custom_tests": [
            {"import_path": t.import_path, "params": [{"name": p.name, "value": p.value} for p in t.params]}
            for t in (req.plan.custom_tests or ())
        ],
    }
    return {
        "tested_model": _redact_client(req.tested_model),
        "attack_model": _redact_client(attack),
        "judge_model": _redact_client(judge) if judge is not None else None,
        "run_config": (req.run_config.model_dump() if req.run_config is not None else None),
        "plan": plan,
    }


def _ensure_safe_relative_artifacts_path(relative_path: str) -> str:
    p = Path(relative_path)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("artifacts_path must be a safe relative path.")
    return str(p.as_posix())


def _build_default_aux_client(settings: Settings) -> OpenAIClientConfig:
    return OpenAIClientConfig(
            api_key=settings.aux_openai_api_key,
            base_url=settings.aux_openai_base_url,
            model=settings.aux_openai_model,
            temperature=0.1,
            system_prompts=None,
            model_description="Auxiliary LLM for LLAMATOR (attack/judge default).",
    )


def _merge_run_config(
        settings: Settings,
        job_id: str,
        user_cfg: LlamatorRunConfig | None,
) -> dict[str, Any]:
    effective: dict[str, Any] = {}
    artifacts_rel: str = user_cfg.artifacts_path if (
                user_cfg is not None and user_cfg.artifacts_path is not None) else job_id
    artifacts_rel = _ensure_safe_relative_artifacts_path(artifacts_rel)

    enable_logging: bool = True if user_cfg is None or user_cfg.enable_logging is None else bool(
        user_cfg.enable_logging)
    enable_reports: bool = False if user_cfg is None or user_cfg.enable_reports is None else bool(
        user_cfg.enable_reports)
    debug_level: int = 1 if user_cfg is None or user_cfg.debug_level is None else int(user_cfg.debug_level)
    report_language: str = settings.report_language if user_cfg is None or user_cfg.report_language is None else user_cfg.report_language

    effective["enable_logging"] = enable_logging
    effective["enable_reports"] = enable_reports
    effective["debug_level"] = debug_level
    effective["report_language"] = report_language
    effective["artifacts_path"] = str((settings.artifacts_root / artifacts_rel).resolve())
    return effective


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """
    Результат постановки задания в очередь.

    :param job_id: Идентификатор задания.
    :param created_at: Время создания.
    :param status: Статус.
    """

    job_id: str
    created_at: datetime
    status: JobStatus


class TestRunService:
    """
    Сервис постановки LLAMATOR тестов в очередь.

    :param arq: ARQ Redis pool для enqueue_job.
    :param store: Хранилище статусов заданий.
    :param settings: Настройки приложения.
    :param logger: Логгер.
    """

    def __init__(self, arq: ArqRedis, store: JobStore, settings: Settings, logger: logging.Logger) -> None:
        self._arq: ArqRedis = arq
        self._store: JobStore = store
        self._settings: Settings = settings
        self._logger: logging.Logger = logger

    async def submit(self, req: LlamatorTestRunRequest) -> SubmitResult:
        """
        Поставить тестирование в очередь.

        :param req: Запрос на запуск.
        :return: Результат постановки.
        :raises ValueError: При некорректных входных данных.
        """
        job_id: str = uuid.uuid4().hex

        attack: ClientConfig = req.attack_model if req.attack_model is not None else _build_default_aux_client(
            self._settings)
        judge: ClientConfig | None = req.judge_model if req.judge_model is not None else _build_default_aux_client(
            self._settings)
        run_config: dict[str, Any] = _merge_run_config(self._settings, job_id, req.run_config)

        request_redacted: dict[str, Any] = _redact_request(req, attack=attack, judge=judge)
        await self._store.create(job_id=job_id, request_redacted=request_redacted)

        payload: dict[str, Any] = {
            "job_id": job_id,
            "created_at": _utcnow().isoformat(),
            "attack_model": attack.model_dump(mode="json"),
            "tested_model": req.tested_model.model_dump(mode="json"),
            "judge_model": judge.model_dump(mode="json") if judge is not None else None,
            "plan": req.plan.model_dump(mode="json"),
            "run_config": run_config,
        }

        await self._arq.enqueue_job("run_llamator_job", payload, _job_id=job_id)
        self._logger.info(f"Enqueued LLAMATOR job_id={job_id}")
        return SubmitResult(job_id=job_id, created_at=_utcnow(), status=JobStatus.QUEUED)


def validate_unique_param_names(params: tuple[TestParameter, ...]) -> None:
    """
    Проверить уникальность имён параметров.

    :param params: Кортеж параметров.
    :return: None
    :raises ValueError: Если имена повторяются.
    """
    names: set[str] = set()
    for p in params:
        if p.name in names:
            raise ValueError(f"Duplicate parameter name: {p.name}")
        names.add(p.name)


def validate_test_specs(basic_tests: tuple[BasicTestSpec, ...] | None) -> None:
    """
    Базовая валидация списка тестов.

    :param basic_tests: Список тестов.
    :return: None
    :raises ValueError: Если параметры некорректны.
    """
    if basic_tests is None:
        return
    for t in basic_tests:
        validate_unique_param_names(t.params)