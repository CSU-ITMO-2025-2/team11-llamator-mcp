from __future__ import annotations

import time
import urllib.parse
from typing import Any

from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import LlamatorJobInfo
from llamator_mcp_server.domain.models import LlamatorTestRunResponse

from tests.conftest import ArtifactsListResponse
from tests.conftest import ClientResponse
from tests.conftest import HttpJsonClient
from tests.conftest import IntegrationTestConfig
from tests.conftest import ResponseReporter


def _create_run(
    http_client: HttpJsonClient,
    headers: dict[str, str],
    payload: dict[str, Any],
    reporter: ResponseReporter,
) -> LlamatorTestRunResponse:
    resp: ClientResponse = http_client.post_json("/v1/tests/runs", payload, headers=headers)
    reporter.http_call("http.create_run", "POST", "/v1/tests/runs", resp)
    assert resp.status == 200, f"create_run status={resp.status} body={resp.body!r}"

    parsed: Any = resp.json()
    reporter.http_json("http.create_run.response", parsed)
    return LlamatorTestRunResponse.model_validate(parsed)


def _is_terminal_status(status: JobStatus) -> bool:
    return status in (JobStatus.SUCCEEDED, JobStatus.FAILED)


def _wait_job_terminal(
    http_client: HttpJsonClient,
    headers: dict[str, str],
    job_id: str,
    timeout_s: float,
    interval_s: float,
    reporter: ResponseReporter,
) -> LlamatorJobInfo:
    """
    Wait until a job transitions into a terminal state.

    :param http_client: HTTP client.
    :param headers: Request headers.
    :param job_id: Job identifier.
    :param timeout_s: Wait timeout in seconds.
    :param interval_s: Poll interval in seconds.
    :return: Final LlamatorJobInfo state.
    :raises AssertionError: If the job does not finish within timeout.
    """
    deadline: float = time.monotonic() + float(timeout_s)

    last_info: LlamatorJobInfo | None = None
    last_logged_status: str | None = None

    while time.monotonic() < deadline:
        path: str = f"/v1/tests/runs/{job_id}"
        resp: ClientResponse = http_client.get(path, headers=headers)
        assert resp.status == 200, f"get_run status={resp.status} body={resp.body!r}"

        payload_any: Any = resp.json()
        assert isinstance(payload_any, dict)
        info: LlamatorJobInfo = LlamatorJobInfo.model_validate(payload_any)
        last_info = info

        status_str: str = str(info.status.value if hasattr(info.status, "value") else info.status)
        updated_at_str: str | None = None
        try:
            updated_at_str = str(payload_any.get("updated_at"))
        except Exception:
            updated_at_str = None

        if last_logged_status != status_str:
            reporter.poll_status_line("http.get_run.poll", job_id=job_id, status=status_str, updated_at=updated_at_str)
            last_logged_status = status_str

        if _is_terminal_status(info.status):
            reporter.final_job_result("http.get_run.final", payload_any)
            return info

        time.sleep(float(interval_s))

    raise AssertionError(
        f"Job did not finish within timeout job_id={job_id} last_status={last_info.status if last_info is not None else None}"
    )


def _artifact_download_path(job_id: str, rel_path: str) -> str:
    """
    Build a safe HTTP path for artifact download.

    :param job_id: Job identifier.
    :param rel_path: Relative artifact path as returned by artifacts/list endpoint.
    :return: Download endpoint path.
    """
    quoted: str = urllib.parse.quote(rel_path, safe="/")
    return f"/v1/tests/runs/{job_id}/artifacts/{quoted}"


def test_health(http_client: HttpJsonClient, http_headers: dict[str, str], reporter: ResponseReporter) -> None:
    resp: ClientResponse = http_client.get("/v1/health", headers=http_headers)
    reporter.http_call("http.health", "GET", "/v1/health", resp)
    assert resp.status == 200
    payload: Any = resp.json()
    reporter.http_json("http.health.response", payload)
    assert isinstance(payload, dict)
    assert payload.get("status") == "ok"


def test_create_run_and_get_status(
    http_client: HttpJsonClient,
    http_headers: dict[str, str],
    minimal_run_request_payload: dict[str, Any],
    reporter: ResponseReporter,
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload, reporter)
    assert created.job_id
    assert len(created.job_id) == 32
    assert created.status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED)

    resp: ClientResponse = http_client.get(f"/v1/tests/runs/{created.job_id}", headers=http_headers)
    reporter.http_call("http.get_run", "GET", f"/v1/tests/runs/{created.job_id}", resp)
    assert resp.status == 200, f"get_run status={resp.status} body={resp.body!r}"
    payload: Any = resp.json()
    reporter.http_json("http.get_run.response", payload)
    info: LlamatorJobInfo = LlamatorJobInfo.model_validate(payload)

    assert info.job_id == created.job_id
    assert info.created_at <= info.updated_at

    req: dict[str, Any] = dict(info.request)
    tested_model: dict[str, Any] = dict(req["tested_model"])  # type: ignore[index]
    assert tested_model.get("kind") == "openai"
    assert tested_model.get("api_key_present") in (True, False)


def test_get_nonexistent_job_404(
    http_client: HttpJsonClient, http_headers: dict[str, str], reporter: ResponseReporter
) -> None:
    resp: ClientResponse = http_client.get("/v1/tests/runs/does-not-exist", headers=http_headers)
    reporter.http_call("http.get_run.404", "GET", "/v1/tests/runs/does-not-exist", resp)
    assert resp.status == 404


def test_list_artifacts_schema(
    http_client: HttpJsonClient,
    http_headers: dict[str, str],
    minimal_run_request_payload: dict[str, Any],
    reporter: ResponseReporter,
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload, reporter)

    path: str = f"/v1/tests/runs/{created.job_id}/artifacts"
    resp: ClientResponse = http_client.get(path, headers=http_headers)
    reporter.http_call("http.list_artifacts", "GET", path, resp)
    assert resp.status == 200, f"list_artifacts status={resp.status} body={resp.body!r}"

    parsed_any: Any = resp.json()
    reporter.http_json("http.list_artifacts.response", parsed_any)

    parsed: ArtifactsListResponse = ArtifactsListResponse.model_validate(parsed_any)
    assert parsed.job_id == created.job_id

    for item in parsed.files:
        assert isinstance(item.get("path"), str)
        assert isinstance(item.get("size_bytes"), int)
        assert isinstance(item.get("mtime"), (int, float))


def test_download_artifact_rejects_path_traversal(
    http_client: HttpJsonClient,
    http_headers: dict[str, str],
    minimal_run_request_payload: dict[str, Any],
    reporter: ResponseReporter,
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload, reporter)

    path: str = f"/v1/tests/runs/{created.job_id}/artifacts/../secrets.txt"
    resp: ClientResponse = http_client.get(path, headers=http_headers)
    reporter.http_call("http.download_artifact.path_traversal", "GET", path, resp)
    assert resp.status == 400, f"expected 400, got {resp.status} body={resp.body!r}"


def test_create_run_validation_error_duplicate_param_names(
    http_client: HttpJsonClient,
    http_headers: dict[str, str],
    reporter: ResponseReporter,
) -> None:
    payload: dict[str, Any] = {
        "tested_model": {"kind": "openai", "base_url": "http://localhost:9999/v1", "model": "dummy"},
        "plan": {
            "basic_tests": [
                {
                    "code_name": "some_test",
                    "params": [
                        {"name": "dup", "value": 1},
                        {"name": "dup", "value": 2},
                    ],
                }
            ]
        },
    }

    resp: ClientResponse = http_client.post_json("/v1/tests/runs", payload, headers=http_headers)
    reporter.http_call("http.create_run.validation_error", "POST", "/v1/tests/runs", resp)
    assert resp.status == 400, f"expected 400, got {resp.status} body={resp.body!r}"
    body: Any = resp.json()
    reporter.http_json("http.create_run.validation_error.response", body)
    assert isinstance(body, dict)
    assert "Duplicate parameter name" in str(body.get("detail", ""))


def test_download_any_artifact_after_job_completion(
    http_client: HttpJsonClient,
    http_headers: dict[str, str],
    minimal_run_request_payload: dict[str, Any],
    it_config: IntegrationTestConfig,
    capsys: Any,
    reporter: ResponseReporter,
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload, reporter)

    final_info: LlamatorJobInfo = _wait_job_terminal(
        http_client=http_client,
        headers=http_headers,
        job_id=created.job_id,
        timeout_s=it_config.http_timeout_s,
        interval_s=0.5,
        reporter=reporter,
    )
    assert _is_terminal_status(final_info.status)

    if final_info.status == JobStatus.FAILED:
        with capsys.disabled():
            reporter.section(f"JOB FAILED job_id={final_info.job_id}")
            reporter.message(f"job_failed_error={final_info.error}")

    list_path: str = f"/v1/tests/runs/{created.job_id}/artifacts"
    resp_list: ClientResponse = http_client.get(list_path, headers=http_headers)
    reporter.http_call("http.list_artifacts.after_completion", "GET", list_path, resp_list)
    assert resp_list.status == 200, f"list_artifacts status={resp_list.status} body={resp_list.body!r}"

    parsed_any: Any = resp_list.json()
    reporter.http_json("http.list_artifacts.after_completion.response", parsed_any)

    parsed: ArtifactsListResponse = ArtifactsListResponse.model_validate(parsed_any)
    assert parsed.job_id == created.job_id

    if not parsed.files:
        with capsys.disabled():
            reporter.section(f"NO ARTIFACTS job_id={created.job_id}")
        return

    first_path: str = str(parsed.files[0].get("path"))
    if not first_path.strip():
        with capsys.disabled():
            reporter.section(f"EMPTY ARTIFACT PATH job_id={created.job_id}")
            reporter.http_json("first_item", parsed.files[0])
        return

    download_path: str = _artifact_download_path(created.job_id, first_path)
    resp_dl: ClientResponse = http_client.get_no_redirect(download_path, headers=http_headers)

    if resp_dl.status == 307:
        reporter.http_redirect_location("http.download_artifact.after_completion", "GET", download_path, resp_dl)
        url: str | None = resp_dl.headers.get("location")
        assert (
            url is not None and url.strip()
        ), f"Expected Location header for 307 redirect job_id={created.job_id} path={first_path}"
        with capsys.disabled():
            reporter.section("ARTIFACT DOWNLOAD URL")
            reporter.message(f"artifact_download_url={url}")
        return

    reporter.http_call("http.download_artifact.after_completion", "GET", download_path, resp_dl)
    assert resp_dl.status == 200, f"download_artifact status={resp_dl.status} body={resp_dl.body[:200]!r}"
    assert resp_dl.body, f"Expected non-empty artifact body job_id={created.job_id} path={first_path}"
