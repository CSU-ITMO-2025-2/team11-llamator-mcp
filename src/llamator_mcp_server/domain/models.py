from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import HttpUrl
from pydantic import field_validator
from pydantic import model_validator


class JobStatus(str, Enum):
    """
    Статус выполнения задания.

    :cvar QUEUED: Задание поставлено в очередь.
    :cvar RUNNING: Задание выполняется worker-ом.
    :cvar SUCCEEDED: Задание завершилось успешно.
    :cvar FAILED: Задание завершилось ошибкой.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ClientKind(str, Enum):
    """
    Тип клиента для взаимодействия с LLM.

    :cvar OPENAI: OpenAI-совместимый API.
    """

    OPENAI = "openai"


class TestParameter(BaseModel):
    """
    Параметр теста.

    :param name: Имя параметра.
    :param value: Значение параметра (JSON-совместимое).
    :raises ValueError: Если имя пустое.
    """

    model_config = ConfigDict(frozen=True)
    name: str = Field(min_length=1, max_length=200)
    value: object

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        name: str = v.strip()
        if not name:
            raise ValueError("Parameter name must be non-empty.")
        return name


class BasicTestSpec(BaseModel):
    """
    Описание базового теста LLAMATOR.

    :param code_name: Кодовое имя атаки.
    :param params: Параметры атаки.
    :raises ValueError: Если code_name пустое.
    """

    model_config = ConfigDict(frozen=True)
    code_name: str = Field(min_length=1, max_length=200)
    params: tuple[TestParameter, ...] = Field(default_factory=tuple)

    @field_validator("code_name")
    @classmethod
    def _validate_code_name(cls, v: str) -> str:
        code_name: str = v.strip()
        if not code_name:
            raise ValueError("Test code_name must be non-empty.")
        return code_name


class CustomTestSpec(BaseModel):
    """
    Описание пользовательского теста (класс, доступный в окружении worker-а).

    import_path должен указывать на импортируемый класс-наследник ``llamator.attack_provider.test_base.TestBase``.

    :param import_path: Полный путь импорта класса (например, ``llamator.attacks.some.TestClass``).
    :param params: Параметры теста.
    :raises ValueError: Если import_path пустой или не соответствует политике импортов.
    """

    model_config = ConfigDict(frozen=True)
    import_path: str = Field(min_length=1, max_length=500)
    params: tuple[TestParameter, ...] = Field(default_factory=tuple)

    @field_validator("import_path")
    @classmethod
    def _validate_import_path(cls, v: str) -> str:
        path: str = v.strip()
        if not path:
            raise ValueError("Custom test import_path must be non-empty.")
        allowed_prefixes: tuple[str, ...] = ("llamator.", "llamator_mcp_server.")
        if not any(path.startswith(prefix) for prefix in allowed_prefixes):
            raise ValueError("Custom test import_path is not allowed by import policy.")
        return path


class LlamatorRunConfig(BaseModel):
    """
    Конфигурация запуска LLAMATOR (параметр ``config`` функции start_testing).

    :param enable_logging: Включить логирование LLAMATOR.
    :param enable_reports: Включить генерацию отчётов (xlsx/docx).
    :param artifacts_path: Относительный путь внутри корня артефактов сервера.
    :param debug_level: Уровень логирования LLAMATOR (0=WARNING, 1=INFO, 2=DEBUG).
    :param report_language: Язык отчёта (en/ru).
    :raises ValueError: При некорректных значениях.
    """

    model_config = ConfigDict(frozen=True)
    enable_logging: bool | None = None
    enable_reports: bool | None = None
    artifacts_path: str | None = None
    debug_level: int | None = None
    report_language: Literal["en", "ru"] | None = None

    @field_validator("artifacts_path")
    @classmethod
    def _validate_artifacts_path(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized: PurePosixPath = PurePosixPath(v)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("artifacts_path must be a safe relative path.")
        return str(normalized)

    @field_validator("debug_level")
    @classmethod
    def _validate_debug_level(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v not in (0, 1, 2):
            raise ValueError("debug_level must be one of: 0, 1, 2.")
        return v


class OpenAIClientConfig(BaseModel):
    """
    Конфигурация OpenAI-совместимого клиента для LLAMATOR.

    :param kind: Тип клиента (``openai``).
    :param api_key: Ключ доступа.
    :param base_url: Базовый URL OpenAI-совместимого API (например, ``http://host:port/v1``).
    :param model: Идентификатор модели.
    :param temperature: Температура.
    :param system_prompts: Системные промпты.
    :param model_description: Описание модели.
    :raises ValueError: При некорректных значениях.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal[ClientKind.OPENAI] = Field(default=ClientKind.OPENAI)
    api_key: str | None = Field(default=None, min_length=1)
    base_url: HttpUrl
    model: str = Field(min_length=1, max_length=300)
    temperature: float | None = None
    system_prompts: tuple[str, ...] | None = None
    model_description: str | None = None

    @field_validator("temperature")
    @classmethod
    def _validate_temperature(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v < 0.0 or v > 2.0:
            raise ValueError("temperature must be in [0.0, 2.0].")
        return v

    @field_validator("system_prompts")
    @classmethod
    def _validate_system_prompts(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if v is None:
            return None
        cleaned: list[str] = [p.strip() for p in v if p.strip()]
        if not cleaned:
            return None
        return tuple(cleaned)


class TestPlan(BaseModel):
    """
    План тестирования LLAMATOR.

    :param preset_name: Имя встроенного набора тестов LLAMATOR (например, ``all``, ``rus``, ``owasp:llm01``).
    :param num_threads: Число потоков для параллельного тестирования.
    :param basic_tests: Явный список базовых тестов.
    :param custom_tests: Явный список пользовательских тестов (по import_path).
    :raises ValueError: При некорректной комбинации параметров.
    """

    model_config = ConfigDict(frozen=True)
    preset_name: str | None = None
    num_threads: int | None = None
    basic_tests: tuple[BasicTestSpec, ...] | None = None
    custom_tests: tuple[CustomTestSpec, ...] | None = None

    @model_validator(mode="after")
    def _validate_plan(self) -> TestPlan:
        if self.num_threads is not None and self.num_threads < 1:
            raise ValueError("num_threads must be >= 1.")
        if self.preset_name is not None and not self.preset_name.strip():
            raise ValueError("preset_name must be non-empty when provided.")
        return self


class LlamatorTestRunRequest(BaseModel):
    """
    Запрос на запуск тестирования определённого LLM endpoint-а через LLAMATOR.

    :param tested_model: Конфигурация тестируемой модели.
    :param run_config: Конфигурация LLAMATOR запуска (опционально).
    :param plan: План тестирования.
    :raises ValueError: При некорректных данных.
    """

    model_config = ConfigDict(frozen=True)
    tested_model: OpenAIClientConfig
    run_config: LlamatorRunConfig | None = None
    plan: TestPlan


class LlamatorTestRunResponse(BaseModel):
    """
    Ответ на запрос создания задания.

    :param job_id: Идентификатор задания.
    :param status: Текущий статус.
    :param created_at: Время создания.
    """

    model_config = ConfigDict(frozen=True)
    job_id: str
    status: JobStatus
    created_at: datetime


class LlamatorJobError(BaseModel):
    """
    Ошибка выполнения задания.

    :param error_type: Тип исключения.
    :param message: Сообщение.
    :param occurred_at: Время фиксации ошибки.
    """

    model_config = ConfigDict(frozen=True)
    error_type: str
    message: str
    occurred_at: datetime


class LlamatorJobResult(BaseModel):
    """
    Результат выполнения LLAMATOR.

    :param aggregated: Агрегированные результаты по атакам.
    :param finished_at: Время завершения.
    """

    model_config = ConfigDict(frozen=True)
    aggregated: dict[str, dict[str, int]]
    finished_at: datetime


class LlamatorJobInfo(BaseModel):
    """
    Состояние задания.

    :param job_id: Идентификатор задания.
    :param status: Статус.
    :param created_at: Время создания.
    :param updated_at: Время последнего обновления.
    :param request: Запрос (с редактированными секретами).
    :param result: Результат (если есть).
    :param error: Ошибка (если есть).
    :param error_notice: Notification message about execution error (if any).
    """

    model_config = ConfigDict(frozen=True)
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    request: dict[str, object]
    result: LlamatorJobResult | None = None
    error: LlamatorJobError | None = None
    error_notice: str | None = None


class ArtifactFileInfo(BaseModel):
    """
    Artifact file metadata.

    :param path: Relative artifact path inside the job artifacts root.
    :param size_bytes: File size in bytes.
    :param mtime: File modification time as a Unix timestamp in seconds.
    """

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1, max_length=4000, description="Relative path inside job artifacts root.")
    size_bytes: int = Field(ge=0, description="File size in bytes.")
    mtime: float = Field(description="Unix timestamp in seconds.")


class ArtifactsListResponse(BaseModel):
    """
    Artifacts list endpoint response.

    :param job_id: Job identifier.
    :param files: Artifact files metadata list.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(min_length=1, max_length=200, description="Job identifier.")
    files: list[ArtifactFileInfo] = Field(default_factory=list, description="Artifact files metadata list.")


class HealthResponse(BaseModel):
    """
    Healthcheck response.

    :param status: Service status.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = Field(description="Service status.")
