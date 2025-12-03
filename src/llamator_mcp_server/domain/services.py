from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from arq.connections import ArqRedis

from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.domain.models import (
    BasicTestSpec,
    CustomTestSpec,
    JobStatus,
    LlamatorRunConfig,
    LlamatorTestRunRequest,
    OpenAIClientConfig,
    TestParameter,
)
from llamator_mcp_server.infra.job_store import JobStore


def _utcnow() -> datetime:
    """
    Получить текущий момент времени в UTC.
    """
    return datetime.now(timezone.utc)


def _redact_client(cfg: OpenAIClientConfig) -> dict[str, Any]:
    """
    Отфильтровать чувствительные данные клиента LLM для хранения/вывода.

    Заменяет секретные поля на маркеры или признаки их наличия.
    """
    return {
        "kind": "openai",
        "base_url": str(cfg.base_url),
        "model": cfg.model,
        "temperature": cfg.temperature,
        "system_prompts": list(cfg.system_prompts) if cfg.system_prompts is not None else None,
        "model_description": cfg.model_description,
        "api_key_present": bool(cfg.api_key),
    }


def _redact_request(
    req: LlamatorTestRunRequest,
    attack: OpenAIClientConfig,
    judge: OpenAIClientConfig,
) -> dict[str, Any]:
    """
    Отфильтровать конфиденциальные данные в запросе тестирования перед сохранением.

    Возвращает словарь с информацией о тестируемой модели, моделях-атакере/судье, конфигурации запуска и плане тестирования.
    """
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
        "judge_model": _redact_client(judge),
        "run_config": (req.run_config.model_dump() if req.run_config is not None else None),
        "plan": plan,
    }


def _build_attack_client(settings: Settings) -> OpenAIClientConfig:
    """
    Build an attack model configuration from environment-backed settings.

    :param settings: Application settings.
    :return: OpenAIClientConfig for attack LLM.
    :raises ValueError: If settings are invalid.
    """
    api_key_val: str | None = settings.attack_openai_api_key or None
    return OpenAIClientConfig(
        api_key=api_key_val,
        base_url=settings.attack_openai_base_url,
        model=settings.attack_openai_model,
        temperature=settings.attack_openai_temperature,
        system_prompts=settings.attack_openai_system_prompts,
        model_description=None,
    )


def _build_judge_client(settings: Settings) -> OpenAIClientConfig:
    """
    Build a judge model configuration from environment-backed settings.

    :param settings: Application settings.
    :return: OpenAIClientConfig for judge LLM.
    :raises ValueError: If settings are invalid.
    """
    api_key_val: str | None = settings.judge_openai_api_key or None
    return OpenAIClientConfig(
        api_key=api_key_val,
        base_url=settings.judge_openai_base_url,
        model=settings.judge_openai_model,
        temperature=settings.judge_openai_temperature,
        system_prompts=settings.judge_openai_system_prompts,
        model_description=None,
    )


def _resolve_artifacts_dir(settings: Settings, job_id: str, user_cfg: LlamatorRunConfig | None) -> Path:
    base: Path = (settings.artifacts_root / job_id).resolve(strict=False)

    if user_cfg is None or user_cfg.artifacts_path is None:
        return base

    rel: PurePosixPath = PurePosixPath(user_cfg.artifacts_path)
    candidate: Path = (base / Path(*rel.parts)).resolve(strict=False)

    if base not in candidate.parents and candidate != base:
        raise ValueError("artifacts_path escaped job artifacts root.")

    return candidate


def _merge_run_config(
    settings: Settings,
    job_id: str,
    user_cfg: LlamatorRunConfig | None,
) -> dict[str, Any]:
    """
    Объединить конфигурацию запуска от пользователя с настройками по умолчанию.

    Формирует полную конфигурацию запуска LLAMATOR.
    """
    effective: dict[str, Any] = {}

    enable_logging: bool = (
        True if user_cfg is None or user_cfg.enable_logging is None else bool(user_cfg.enable_logging)
    )
    enable_reports: bool = (
        False if user_cfg is None or user_cfg.enable_reports is None else bool(user_cfg.enable_reports)
    )
    debug_level: int = 1 if user_cfg is None or user_cfg.debug_level is None else int(user_cfg.debug_level)
    report_language: str = (
        settings.report_language if user_cfg is None or user_cfg.report_language is None else user_cfg.report_language
    )

    effective["enable_logging"] = enable_logging
    effective["enable_reports"] = enable_reports
    effective["debug_level"] = debug_level
    effective["report_language"] = report_language

    artifacts_dir: Path = _resolve_artifacts_dir(settings=settings, job_id=job_id, user_cfg=user_cfg)
    effective["artifacts_path"] = str(artifacts_dir)

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

        attack: OpenAIClientConfig = _build_attack_client(self._settings)
        judge: OpenAIClientConfig = _build_judge_client(self._settings)
        run_config: dict[str, Any] = _merge_run_config(self._settings, job_id, req.run_config)

        request_redacted: dict[str, Any] = _redact_request(req, attack=attack, judge=judge)
        await self._store.create(job_id=job_id, request_redacted=request_redacted)

        payload: dict[str, Any] = {
            "job_id": job_id,
            "created_at": _utcnow().isoformat(),
            "attack_model": attack.model_dump(mode="json"),
            "tested_model": req.tested_model.model_dump(mode="json"),
            "judge_model": judge.model_dump(mode="json"),
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


def validate_test_specs(
    basic_tests: tuple[BasicTestSpec, ...] | None,
    custom_tests: tuple[CustomTestSpec, ...] | None,
) -> None:
    """
    Базовая валидация списков тестов.

    :param basic_tests: Список базовых тестов (может быть None).
    :param custom_tests: Список пользовательских тестов (может быть None).
    :return: None
    :raises ValueError: Если параметры тестов некорректны (например, повторяются имена параметров).
    """
    if basic_tests is not None:
        for t in basic_tests:
            validate_unique_param_names(t.params)
    if custom_tests is not None:
        for t in custom_tests:
            validate_unique_param_names(t.params)
