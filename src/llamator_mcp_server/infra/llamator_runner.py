from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import llamator
from llamator_mcp_server.domain.models import OpenAIClientConfig, TestParameter, TestPlan


@dataclass(frozen=True, slots=True)
class ResolvedRun:
    """
    Полностью разрешённая конфигурация запуска worker-а.

    :param job_id: Идентификатор задания.
    :param attack_model: Конфигурация модели-атакера.
    :param tested_model: Конфигурация тестируемой модели.
    :param judge_model: Конфигурация модели-судьи (может быть None).
    :param plan: План тестирования.
    :param run_config: Конфигурация LLAMATOR.
    :param artifacts_root: Корень артефактов для данного job_id.
    """

    job_id: str
    attack_model: OpenAIClientConfig
    tested_model: OpenAIClientConfig
    judge_model: OpenAIClientConfig
    plan: TestPlan
    run_config: dict[str, Any]
    artifacts_root: Path


def _params_to_dict(params: tuple[TestParameter, ...]) -> dict[str, Any]:
    return {p.name: p.value for p in params}


def _build_client(cfg: OpenAIClientConfig) -> Any:
    api_key: str = cfg.api_key or ""
    temperature: float = cfg.temperature if cfg.temperature is not None else 0.1
    return llamator.ClientOpenAI(
        api_key=api_key,
        base_url=str(cfg.base_url),
        model=cfg.model,
        temperature=temperature,
        system_prompts=list(cfg.system_prompts) if cfg.system_prompts is not None else None,
        model_description=cfg.model_description,
    )


def _resolve_basic_tests(plan: TestPlan) -> list[tuple[str, dict[str, Any]]] | None:
    tests: list[tuple[str, dict[str, Any]]] = []

    if plan.preset_name is not None:
        preset_tests: list[tuple[str, dict[str, Any]]] = llamator.get_test_preset(plan.preset_name.strip())
        tests.extend(preset_tests)

    if plan.basic_tests is not None:
        for spec in plan.basic_tests:
            tests.append((spec.code_name, _params_to_dict(spec.params)))

    return tests or None


def _import_custom_test(import_path: str) -> type:
    module_name, _, class_name = import_path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError("import_path must be a fully-qualified path to a class.")
    module = importlib.import_module(module_name)
    obj = getattr(module, class_name, None)
    if obj is None or not inspect.isclass(obj):
        raise ValueError("import_path must point to an importable class.")
    return obj


def _resolve_custom_tests(plan: TestPlan) -> list[tuple[type, dict[str, Any]]] | None:
    if plan.custom_tests is None:
        return None

    tests: list[tuple[type, dict[str, Any]]] = []
    for spec in plan.custom_tests:
        test_cls: type = _import_custom_test(spec.import_path)
        tests.append((test_cls, _params_to_dict(spec.params)))
    return tests or None


class LlamatorRunner:
    """
    Исполнитель тестов LLAMATOR (вызывается worker-ом).

    :param logger: Логгер.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger: logging.Logger = logger

    def run(self, resolved: ResolvedRun) -> dict[str, dict[str, int]]:
        """
        Выполнить тестирование LLAMATOR.

        :param resolved: Полностью разрешённая конфигурация.
        :return: Агрегированные результаты LLAMATOR.
        :raises Exception: Любая ошибка LLAMATOR/клиента, не обработанная ниже.
        """
        resolved.artifacts_root.mkdir(parents=True, exist_ok=True)

        attack_model = _build_client(resolved.attack_model)
        tested_model = _build_client(resolved.tested_model)
        judge_model = _build_client(resolved.judge_model)

        basic_tests = _resolve_basic_tests(resolved.plan)
        custom_tests = _resolve_custom_tests(resolved.plan)

        num_threads: int | None = resolved.plan.num_threads
        self._logger.info(f"Starting LLAMATOR run for job_id={resolved.job_id}")

        return llamator.start_testing(
            attack_model=attack_model,
            tested_model=tested_model,
            config=resolved.run_config,
            judge_model=judge_model,
            num_threads=num_threads,
            basic_tests=basic_tests,
            custom_tests=custom_tests,
        )
