from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Final
from typing import Literal
from typing import Mapping

import pytest
from icecream import ic
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

DEFAULT_ENV_FILE_NAME: Final[str] = ".env.test"
ENV_PREFIX: Final[str] = "LLAMATOR_MCP_TEST_"
SERVICE_ENV_PREFIX: Final[str] = "LLAMATOR_MCP_"


class ArtifactsListResponse(BaseModel):
    """
    Integration DTO: response schema for artifacts/list endpoint.

    :param job_id: Job identifier.
    :param files: Artifact files metadata list.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    files: list[dict[str, Any]] = Field(default_factory=list)


class RunRequestEnvConfig(BaseModel):
    """
    Integration tests configuration for LlamatorTestRunRequest payload.

    :param tested_kind: Client kind for tested model.
    :param tested_base_url: OpenAI-compatible base_url, e.g. http://host:port/v1
    :param tested_model: Model id.
    :param tested_api_key: Optional api key.
    :param preset_name: Test preset name.
    :param num_threads: Number of threads.
    :param enable_reports: Enable LLAMATOR reports generation (docx/xlsx/csv).
    """

    model_config = ConfigDict(frozen=True)

    tested_kind: Literal["openai"]
    tested_base_url: str = Field(min_length=1, max_length=2000)
    tested_model: str = Field(min_length=1, max_length=300)
    tested_api_key: str | None = Field(default=None, min_length=1, max_length=500)

    preset_name: str = Field(min_length=1, max_length=200)
    num_threads: int = Field(ge=1, le=256)

    enable_reports: bool

    @field_validator("tested_base_url", "tested_model", "preset_name")
    @classmethod
    def _strip_non_empty(cls, v: str) -> str:
        val: str = v.strip()
        if not val:
            raise ValueError("Value must be non-empty.")
        return val

    @field_validator("tested_api_key")
    @classmethod
    def _strip_optional(cls, v: str | None) -> str | None:
        if v is None:
            return None
        val: str = v.strip()
        return val or None


@dataclass(frozen=True, slots=True)
class IntegrationTestConfig:
    """
    Integration tests configuration.

    :param base_url: Base URL of the running API server.
    :param mcp_path: Path where MCP ASGI app is mounted.
    :param api_key: Optional API key for protected HTTP/MCP routes.
    :param http_timeout_s: Per-request timeout in seconds.
    :param ready_timeout_s: Healthcheck wait timeout.
    :param ready_interval_s: Healthcheck poll interval.
    :param mcp_protocol_version: MCP protocol version header value.
    """

    base_url: str
    mcp_path: str
    api_key: str | None
    http_timeout_s: float
    ready_timeout_s: float
    ready_interval_s: float
    mcp_protocol_version: str

    @property
    def mcp_endpoint(self) -> str:
        """
        Build absolute MCP endpoint URL.

        :return: MCP endpoint URL.
        """
        return _join_url(self.base_url, self.mcp_path)


@dataclass(frozen=True, slots=True)
class ClientResponse:
    """
    HTTP client response container.

    :param status: HTTP status code.
    :param headers: Response headers (lowercased keys).
    :param body: Raw response body bytes.
    """

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        """
        Parse response body as JSON.

        :return: Parsed JSON.
        :raises ValueError: If body is not valid JSON.
        """
        if not self.body:
            raise ValueError("Response body is empty.")
        return json.loads(self.body.decode("utf-8"))


class ResponseReporter:
    """
    Centralized structured output for integration tests using icecream.

    This class standardizes logging across tests and keeps printing logic
    out of the test bodies.
    """

    def __init__(self) -> None:
        ic.configureOutput(includeContext=True, argToStringFunction=self._arg_to_str)

    @staticmethod
    def _arg_to_str(arg: Any) -> str:
        """
        Convert icecream arguments to strings without truncation.

        :param arg: Any value.
        :return: String representation.
        """
        if isinstance(arg, str):
            return arg
        if isinstance(arg, bytes):
            try:
                return arg.decode("utf-8")
            except UnicodeDecodeError:
                return repr(arg)
        return repr(arg)

    @staticmethod
    def _json_pretty(payload: Any) -> str:
        """
        Pretty JSON dump without truncation.

        :param payload: JSON-serializable structure.
        :return: Pretty string.
        """
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def section(self, title: str) -> None:
        """
        Print a logical section header.

        :param title: Section title.
        :return: None.
        """
        ic(f"=== {title} ===")

    def http_call(self, label: str, method: str, path: str, resp: ClientResponse) -> None:
        """
        Print a one-line HTTP call summary.

        :param label: Logical label (test step).
        :param method: HTTP method.
        :param path: Request path.
        :param resp: Response.
        :return: None.
        """
        ic(f"[HTTP] {label} {method.upper()} {path} -> {resp.status}")

    def http_json(self, title: str, payload: Any) -> None:
        """
        Print a JSON payload in a dedicated block.

        :param title: Block title.
        :param payload: JSON payload.
        :return: None.
        """
        ic(f"{title}:\n{self._json_pretty(payload)}")

    def http_redirect_location(self, label: str, method: str, path: str, resp: ClientResponse) -> None:
        """
        Print redirect location for 3xx responses.

        :param label: Logical label.
        :param method: HTTP method.
        :param path: Request path.
        :param resp: Response.
        :return: None.
        """
        location: str | None = resp.headers.get("location")
        ic(f"[HTTP] {label} {method.upper()} {path} -> {resp.status} location={location}")

    def poll_status_line(self, label: str, job_id: str, status: str, updated_at: str | None) -> None:
        """
        Print a compact poll status line.

        :param label: Logical label.
        :param job_id: Job id.
        :param status: Job status.
        :param updated_at: Optional updated_at string.
        :return: None.
        """
        suffix: str = f" updated_at={updated_at}" if updated_at else ""
        ic(f"[POLL] {label} job_id={job_id} status={status}{suffix}")

    def final_job_result(self, label: str, job_payload: dict[str, Any]) -> None:
        """
        Print the final server response and the extracted result block.

        :param label: Logical label.
        :param job_payload: Final job JSON response.
        :return: None.
        """
        job_id: Any = job_payload.get("job_id")
        status: Any = job_payload.get("status")
        updated_at: Any = job_payload.get("updated_at")

        self.section(f"FINAL {label} job_id={job_id} status={status} updated_at={updated_at}")

        result_val: Any = job_payload.get("result")
        error_val: Any = job_payload.get("error")
        error_notice_val: Any = job_payload.get("error_notice")

        self.http_json("server.response", job_payload)

        if result_val is not None:
            self.http_json("server.result", result_val)

        if error_val is not None:
            self.http_json("server.error", error_val)

        if error_notice_val is not None:
            self.http_json("server.error_notice", error_notice_val)

    def message(self, msg: str) -> None:
        """
        Log a message line.

        :param msg: Message string.
        :return: None.
        """
        ic(msg)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    urllib handler that disables automatic redirect following.

    This is used by integration tests to capture 307 Location header
    for S3 presigned downloads without downloading the file.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:  # noqa: ANN401
        return None


class HttpJsonClient:
    """
    Minimal JSON-over-HTTP client for integration tests.

    :param base_url: Base URL, e.g. http://localhost:8000
    :param timeout_s: Per-request timeout in seconds.
    """

    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._base_url: str = _normalize_base_url(base_url)
        self._timeout_s: float = float(timeout_s)

        if self._timeout_s <= 0.0:
            raise ValueError("http_timeout_s must be > 0.")

    def get(self, path: str, headers: Mapping[str, str]) -> ClientResponse:
        """
        Send GET request.

        :param path: Path part, e.g. /v1/health
        :param headers: Request headers.
        :return: ClientResponse.
        """
        url: str = _join_url(self._base_url, path)
        req: urllib.request.Request = urllib.request.Request(url=url, method="GET", headers=dict(headers))
        return self._send(req, follow_redirects=True)

    def get_no_redirect(self, path: str, headers: Mapping[str, str]) -> ClientResponse:
        """
        Send GET request without following redirects.

        Useful for endpoints that return 307 redirect to a presigned URL (S3 backend).

        :param path: Path part, e.g. /v1/tests/runs/{job_id}/artifacts/{path}
        :param headers: Request headers.
        :return: ClientResponse.
        """
        url: str = _join_url(self._base_url, path)
        req: urllib.request.Request = urllib.request.Request(url=url, method="GET", headers=dict(headers))
        return self._send(req, follow_redirects=False)

    def post_json(self, path: str, payload: Any, headers: Mapping[str, str]) -> ClientResponse:
        """
        Send POST request with JSON body.

        :param path: Path part, e.g. /v1/tests/runs
        :param payload: JSON-serializable payload.
        :param headers: Request headers.
        :return: ClientResponse.
        """
        url: str = _join_url(self._base_url, path)
        data: bytes = json.dumps(payload).encode("utf-8")
        req_headers: dict[str, str] = dict(headers)
        req_headers["Content-Type"] = "application/json"
        req: urllib.request.Request = urllib.request.Request(url=url, data=data, method="POST", headers=req_headers)
        return self._send(req, follow_redirects=True)

    def post_raw(self, url: str, payload_json: dict[str, Any], headers: Mapping[str, str]) -> ClientResponse:
        """
        Send POST request with JSON body to an absolute URL.

        :param url: Absolute URL.
        :param payload_json: JSON-RPC message payload.
        :param headers: Request headers.
        :return: ClientResponse.
        """
        data: bytes = json.dumps(payload_json).encode("utf-8")
        req_headers: dict[str, str] = dict(headers)
        req_headers["Content-Type"] = "application/json"
        req: urllib.request.Request = urllib.request.Request(url=url, data=data, method="POST", headers=req_headers)
        return self._send(req, follow_redirects=True)

    def _send(self, req: urllib.request.Request, follow_redirects: bool) -> ClientResponse:
        """
        Execute request and return response.

        :param req: Prepared urllib request.
        :param follow_redirects: Whether to follow 3xx redirects.
        :return: ClientResponse.
        """
        try:
            if follow_redirects:
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    status: int = int(resp.status)
                    headers: dict[str, str] = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                    body: bytes = resp.read()
                    return ClientResponse(status=status, headers=headers, body=body)

            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(req, timeout=self._timeout_s) as resp:
                status2: int = int(resp.status)
                headers2: dict[str, str] = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                body2: bytes = resp.read()
                return ClientResponse(status=status2, headers=headers2, body=body2)

        except urllib.error.HTTPError as e:
            status_err: int = int(e.code)
            headers_err: dict[str, str] = {str(k).lower(): str(v) for k, v in e.headers.items()}
            body_err: bytes = e.read() if e.fp is not None else b""
            return ClientResponse(status=status_err, headers=headers_err, body=body_err)


@dataclass(frozen=True, slots=True)
class McpSession:
    """
    MCP session state for Streamable HTTP transport.

    :param endpoint_url: Absolute MCP endpoint URL.
    :param session_id: Optional session id assigned by the server.
    """

    endpoint_url: str
    session_id: str | None


class McpJsonRpcClient:
    """
    Minimal MCP JSON-RPC client over Streamable HTTP.

    :param http: Underlying HTTP client.
    :param cfg: Integration test configuration.
    """

    def __init__(self, http: HttpJsonClient, cfg: IntegrationTestConfig, reporter: ResponseReporter) -> None:
        self._http: HttpJsonClient = http
        self._cfg: IntegrationTestConfig = cfg
        self._reporter: ResponseReporter = reporter

    def initialize(self) -> McpSession:
        """
        Perform MCP initialization handshake.

        :return: McpSession containing session id if provided.
        :raises AssertionError: If initialization fails.
        """
        init_msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": self._cfg.mcp_protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "pytest-integration", "version": "1.0.0"},
            },
        }

        headers: dict[str, str] = self._mcp_headers(session_id=None)
        resp: ClientResponse = self._http.post_raw(self._cfg.mcp_endpoint, init_msg, headers=headers)
        self._reporter.http_call("mcp.initialize", "POST", self._cfg.mcp_path, resp)
        assert resp.status == 200, f"initialize status={resp.status} body={resp.body!r}"

        payload: dict[str, Any] = _expect_json_obj(resp)
        assert payload.get("jsonrpc") == "2.0"
        assert payload.get("id") == 1
        assert isinstance(payload.get("result"), dict)

        session_id: str | None = resp.headers.get("mcp-session-id")
        init_notif: dict[str, Any] = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        headers2: dict[str, str] = self._mcp_headers(session_id=session_id)
        resp2: ClientResponse = self._http.post_raw(self._cfg.mcp_endpoint, init_notif, headers=headers2)
        self._reporter.http_call("mcp.notifications.initialized", "POST", self._cfg.mcp_path, resp2)
        assert resp2.status in (202, 200), f"initialized status={resp2.status} body={resp2.body!r}"

        return McpSession(endpoint_url=self._cfg.mcp_endpoint, session_id=session_id)

    def list_tools(self, session: McpSession) -> list[dict[str, Any]]:
        """
        List available MCP tools.

        :param session: Active MCP session.
        :return: Tools list (dicts).
        :raises AssertionError: If the response is invalid.
        """
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        headers: dict[str, str] = self._mcp_headers(session_id=session.session_id)
        resp: ClientResponse = self._http.post_raw(session.endpoint_url, msg, headers=headers)
        self._reporter.http_call("mcp.tools.list", "POST", self._cfg.mcp_path, resp)
        assert resp.status == 200, f"tools/list status={resp.status} body={resp.body!r}"

        payload: dict[str, Any] = _expect_json_obj(resp)
        result: dict[str, Any] = _expect_dict(payload.get("result"))
        tools_val: Any = result.get("tools")
        assert isinstance(tools_val, list)
        return [t for t in tools_val if isinstance(t, dict)]

    def call_tool(self, session: McpSession, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Call an MCP tool.

        :param session: Active MCP session.
        :param tool_name: Tool name.
        :param arguments: Tool arguments.
        :return: Tool call result.
        :raises AssertionError: If the call fails or response is invalid.
        """
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        headers: dict[str, str] = self._mcp_headers(session_id=session.session_id)
        resp: ClientResponse = self._http.post_raw(session.endpoint_url, msg, headers=headers)
        self._reporter.http_call(f"mcp.tools.call.{tool_name}", "POST", self._cfg.mcp_path, resp)
        assert resp.status == 200, f"tools/call status={resp.status} body={resp.body!r}"

        payload: dict[str, Any] = _expect_json_obj(resp)
        assert payload.get("jsonrpc") == "2.0"
        assert payload.get("id") == 3

        result: dict[str, Any] = _expect_dict(payload.get("result"))
        return result

    def _mcp_headers(self, session_id: str | None) -> dict[str, str]:
        """
        Build MCP request headers.

        :param session_id: Optional Mcp-Session-Id value.
        :return: Headers dict.
        """
        origin: str = _origin_from_base_url(self._cfg.base_url)
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._cfg.mcp_protocol_version,
            "Origin": origin,
        }
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        if self._cfg.api_key is not None:
            headers["X-API-Key"] = self._cfg.api_key
        return headers


def _normalize_base_url(base_url: str) -> str:
    base: str = base_url.strip()
    if not base:
        raise ValueError("base_url must be non-empty.")
    return base.rstrip("/")


def _normalize_mcp_mount_path(mount_path: str) -> str:
    """
    Normalize MCP mount path for Streamable HTTP endpoint usage.

    Ensures leading slash and enforces trailing slash to avoid 307 redirects
    when calling POST endpoints on Starlette/FastAPI mounted apps.

    :param mount_path: Mount path value (e.g. /mcp, mcp, /mcp/).
    :return: Normalized mount path ending with '/' (e.g. /mcp/).
    :raises ValueError: If mount_path is empty.
    """
    raw: str = str(mount_path).strip()
    if not raw:
        raise ValueError("mcp_path must be non-empty.")

    if not raw.startswith("/"):
        raw = f"/{raw}"

    if raw == "/":
        return "/"

    normalized: str = raw.rstrip("/")
    return f"{normalized}/"


def _join_url(base_url: str, path: str) -> str:
    base: str = _normalize_base_url(base_url)
    p: str = path.strip()
    if not p:
        return base
    if not p.startswith("/"):
        return f"{base}/{p}"
    return f"{base}{p}"


def _origin_from_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(_normalize_base_url(base_url))
    origin: str = f"{parsed.scheme}://{parsed.netloc}"
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("base_url must include URL scheme and host.")
    return origin


def _parse_env_file(env_path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    raw: str = env_path.read_text(encoding="utf-8")

    for line in raw.splitlines():
        s: str = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, value = s.split("=", 1)
        k: str = key.strip()
        v: str = value.strip()

        if len(v) >= 2 and ((v[0] == v[-1]) and v[0] in ("'", '"')):
            v = v[1:-1]

        if k:
            data[k] = v

    return data


def _get_env_required(key: str) -> str:
    val: str = os.environ.get(key, "").strip()
    if not val:
        raise ValueError(f"Missing required env var: {key}")
    return val


def _get_env_optional(key: str) -> str | None:
    val: str = os.environ.get(key, "").strip()
    return val or None


def _get_env_float(key: str) -> float:
    raw: str = _get_env_required(key)
    try:
        val: float = float(raw)
    except ValueError as e:
        raise ValueError(f"Invalid float for {key}: {raw}") from e
    if val <= 0.0:
        raise ValueError(f"{key} must be > 0.")
    return val


def _get_env_int(key: str, *, min_value: int, max_value: int) -> int:
    raw: str = _get_env_required(key)
    try:
        val: int = int(raw)
    except ValueError as e:
        raise ValueError(f"Invalid int for {key}: {raw}") from e
    if val < min_value or val > max_value:
        raise ValueError(f"{key} must be in [{min_value}, {max_value}].")
    return val


def _get_env_bool(key: str) -> bool:
    raw: str = _get_env_required(key).strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(f"Invalid bool for {key}: {raw}")


def _load_env_from_tests_file() -> None:
    tests_root: Path = Path(__file__).resolve().parent
    env_file: Path = tests_root / DEFAULT_ENV_FILE_NAME
    if not env_file.exists():
        raise ValueError(f"Env file not found: {env_file}")
    parsed: dict[str, str] = _parse_env_file(env_file)
    for k, v in parsed.items():
        os.environ.setdefault(k, v)


def _service_http_port() -> int:
    raw: str = os.environ.get(f"{SERVICE_ENV_PREFIX}HTTP_PORT", "").strip()
    if not raw:
        return 8000
    try:
        port: int = int(raw)
    except ValueError as e:
        raise ValueError(f"Invalid int for {SERVICE_ENV_PREFIX}HTTP_PORT: {raw}") from e
    if port < 1 or port > 65535:
        raise ValueError(f"{SERVICE_ENV_PREFIX}HTTP_PORT must be in [1, 65535].")
    return port


def _service_mcp_mount_path() -> str:
    raw: str = os.environ.get(f"{SERVICE_ENV_PREFIX}MCP_MOUNT_PATH", "").strip()
    return _normalize_mcp_mount_path(raw or "/mcp")


def _service_api_key() -> str | None:
    raw: str = os.environ.get(f"{SERVICE_ENV_PREFIX}API_KEY", "").strip()
    return raw or None


def _load_test_config() -> IntegrationTestConfig:
    _load_env_from_tests_file()

    base_url_override: str | None = _get_env_optional(f"{ENV_PREFIX}BASE_URL")

    if base_url_override is not None:
        base_url: str = _normalize_base_url(base_url_override)
    else:
        port: int = _service_http_port()
        base_url = _normalize_base_url(f"http://localhost:{port}")

    mcp_path: str = _service_mcp_mount_path()
    api_key: str | None = _get_env_optional(f"{ENV_PREFIX}API_KEY") or _service_api_key()

    http_timeout_s: float = _get_env_float(f"{ENV_PREFIX}HTTP_TIMEOUT_S")
    ready_timeout_s: float = _get_env_float(f"{ENV_PREFIX}READY_TIMEOUT_S")
    ready_interval_s: float = _get_env_float(f"{ENV_PREFIX}READY_INTERVAL_S")

    mcp_protocol_version: str = _get_env_required(f"{ENV_PREFIX}MCP_PROTOCOL_VERSION")

    return IntegrationTestConfig(
        base_url=base_url,
        mcp_path=mcp_path,
        api_key=api_key,
        http_timeout_s=http_timeout_s,
        ready_timeout_s=ready_timeout_s,
        ready_interval_s=ready_interval_s,
        mcp_protocol_version=mcp_protocol_version,
    )


def _load_run_request_env_config() -> RunRequestEnvConfig:
    _load_env_from_tests_file()

    tested_kind: str = _get_env_required(f"{ENV_PREFIX}TESTED_KIND")
    tested_base_url: str = _get_env_required(f"{ENV_PREFIX}TESTED_BASE_URL")
    tested_model: str = _get_env_required(f"{ENV_PREFIX}TESTED_MODEL")
    tested_api_key: str | None = _get_env_optional(f"{ENV_PREFIX}TESTED_API_KEY")

    preset_name: str = _get_env_required(f"{ENV_PREFIX}PRESET_NAME")
    num_threads: int = _get_env_int(f"{ENV_PREFIX}NUM_THREADS", min_value=1, max_value=256)

    enable_reports: bool = _get_env_bool(f"{ENV_PREFIX}ENABLE_REPORTS")

    return RunRequestEnvConfig(
        tested_kind=tested_kind,  # type: ignore[arg-type]
        tested_base_url=tested_base_url,
        tested_model=tested_model,
        tested_api_key=tested_api_key,
        preset_name=preset_name,
        num_threads=num_threads,
        enable_reports=enable_reports,
    )


def _wait_until_ready(http: HttpJsonClient, cfg: IntegrationTestConfig) -> None:
    deadline: float = time.monotonic() + cfg.ready_timeout_s
    headers: dict[str, str] = _http_headers(cfg)

    last_status: int | None = None
    last_body: bytes | None = None

    while time.monotonic() < deadline:
        resp: ClientResponse = http.get("/v1/health", headers=headers)
        last_status = resp.status
        last_body = resp.body
        if resp.status == 200:
            try:
                payload: Any = resp.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return
        time.sleep(cfg.ready_interval_s)

    raise AssertionError(f"Server is not ready: last_status={last_status} last_body={last_body!r}")


def _http_headers(cfg: IntegrationTestConfig) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if cfg.api_key is not None:
        headers["X-API-Key"] = cfg.api_key
    return headers


def _expect_json_obj(resp: ClientResponse) -> dict[str, Any]:
    payload: Any = resp.json()
    assert isinstance(payload, dict), f"Expected JSON object, got {type(payload)}"
    return payload


def _expect_dict(val: Any) -> dict[str, Any]:
    if not isinstance(val, dict):
        raise AssertionError(f"Expected dict, got {type(val)}")
    return val


@pytest.fixture(scope="session")
def it_config() -> IntegrationTestConfig:
    """
    Provide integration test configuration loaded from tests/.env.test.

    :return: IntegrationTestConfig.
    """
    return _load_test_config()


@pytest.fixture(scope="session")
def run_request_env_config() -> RunRequestEnvConfig:
    """
    Provide request-payload configuration loaded from tests/.env.test.

    :return: RunRequestEnvConfig.
    """
    return _load_run_request_env_config()


@pytest.fixture(scope="session")
def http_client(it_config: IntegrationTestConfig) -> HttpJsonClient:
    """
    Provide shared HTTP client.

    :param it_config: Integration configuration.
    :return: HttpJsonClient instance.
    """
    return HttpJsonClient(base_url=it_config.base_url, timeout_s=it_config.http_timeout_s)


@pytest.fixture(scope="session")
def reporter() -> ResponseReporter:
    """
    Provide a shared response reporter using icecream.

    :return: ResponseReporter.
    """
    return ResponseReporter()


@pytest.fixture(scope="session", autouse=True)
def _ensure_server_ready(http_client: HttpJsonClient, it_config: IntegrationTestConfig) -> None:
    """
    Ensure server is reachable before running tests.

    :param http_client: HTTP client.
    :param it_config: Integration configuration.
    :return: None.
    """
    _wait_until_ready(http_client, it_config)


@pytest.fixture()
def http_headers(it_config: IntegrationTestConfig) -> dict[str, str]:
    """
    Default headers for HTTP API calls.

    :param it_config: IntegrationTestConfig.
    :return: Headers dict.
    """
    return _http_headers(it_config)


@pytest.fixture()
def mcp_client(
    http_client: HttpJsonClient,
    it_config: IntegrationTestConfig,
    reporter: ResponseReporter,
) -> McpJsonRpcClient:
    """
    Provide MCP JSON-RPC client.

    :param http_client: HTTP client.
    :param it_config: Integration configuration.
    :return: McpJsonRpcClient instance.
    """
    return McpJsonRpcClient(http=http_client, cfg=it_config, reporter=reporter)


@pytest.fixture()
def mcp_session(mcp_client: McpJsonRpcClient) -> McpSession:
    """
    Provide initialized MCP session.

    :param mcp_client: MCP JSON-RPC client.
    :return: McpSession.
    """
    return mcp_client.initialize()


@pytest.fixture()
def minimal_run_request_payload(run_request_env_config: RunRequestEnvConfig) -> dict[str, Any]:
    """
    Build a minimal valid LlamatorTestRunRequest payload from env config.

    :param run_request_env_config: RunRequestEnvConfig.
    :return: Request payload as dict.
    """
    tested_model: dict[str, Any] = {
        "kind": run_request_env_config.tested_kind,
        "base_url": run_request_env_config.tested_base_url,
        "model": run_request_env_config.tested_model,
    }
    if run_request_env_config.tested_api_key is not None:
        tested_model["api_key"] = run_request_env_config.tested_api_key

    return {
        "tested_model": tested_model,
        "run_config": {
            "enable_reports": run_request_env_config.enable_reports,
        },
        "plan": {
            "preset_name": run_request_env_config.preset_name,
            "num_threads": run_request_env_config.num_threads,
        },
    }
