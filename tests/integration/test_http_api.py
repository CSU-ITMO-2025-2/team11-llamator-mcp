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


def _create_run(
        http_client: HttpJsonClient, headers: dict[str, str], payload: dict[str, Any]
) -> LlamatorTestRunResponse:
    resp: ClientResponse = http_client.post_json("/v1/tests/runs", payload, headers=headers)
    assert resp.status == 200, f"create_run status={resp.status} body={resp.body!r}"
    return LlamatorTestRunResponse.model_validate(resp.json())


def _is_terminal_status(status: JobStatus) -> bool:
    return status in (JobStatus.SUCCEEDED, JobStatus.FAILED)


def _wait_job_terminal(
        http_client: HttpJsonClient,
        headers: dict[str, str],
        job_id: str,
        timeout_s: float,
        interval_s: float,
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
    while time.monotonic() < deadline:
        resp: ClientResponse = http_client.get(f"/v1/tests/runs/{job_id}", headers=headers)
        assert resp.status == 200, f"get_run status={resp.status} body={resp.body!r}"

        info: LlamatorJobInfo = LlamatorJobInfo.model_validate(resp.json())
        last_info = info
        if _is_terminal_status(info.status):
            return info

        time.sleep(float(interval_s))

    raise AssertionError(
        f"Job did not finish within timeout job_id={job_id} last_status={last_info.status if last_info is not None else None}")


def _artifact_download_path(job_id: str, rel_path: str) -> str:
    """
    Build a safe HTTP path for artifact download.

    :param job_id: Job identifier.
    :param rel_path: Relative artifact path as returned by artifacts/list endpoint.
    :return: Download endpoint path.
    """
    quoted: str = urllib.parse.quote(rel_path, safe="/")
    return f"/v1/tests/runs/{job_id}/artifacts/{quoted}"


def test_health(http_client: HttpJsonClient, http_headers: dict[str, str]) -> None:
    resp: ClientResponse = http_client.get("/v1/health", headers=http_headers)
    assert resp.status == 200
    payload: Any = resp.json()
    assert isinstance(payload, dict)
    assert payload.get("status") == "ok"


def test_create_run_and_get_status(
        http_client: HttpJsonClient,
        http_headers: dict[str, str],
        minimal_run_request_payload: dict[str, Any],
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload)
    assert created.job_id
    assert len(created.job_id) == 32
    assert created.status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED)

    resp: ClientResponse = http_client.get(f"/v1/tests/runs/{created.job_id}", headers=http_headers)
    assert resp.status == 200, f"get_run status={resp.status} body={resp.body!r}"
    info: LlamatorJobInfo = LlamatorJobInfo.model_validate(resp.json())

    assert info.job_id == created.job_id
    assert info.created_at <= info.updated_at

    req: dict[str, Any] = dict(info.request)
    tested_model: dict[str, Any] = dict(req["tested_model"])  # type: ignore[index]
    assert tested_model.get("kind") == "openai"
    assert tested_model.get("api_key_present") in (True, False)


def test_get_nonexistent_job_404(http_client: HttpJsonClient, http_headers: dict[str, str]) -> None:
    resp: ClientResponse = http_client.get("/v1/tests/runs/does-not-exist", headers=http_headers)
    assert resp.status == 404


def test_list_artifacts_schema(
        http_client: HttpJsonClient,
        http_headers: dict[str, str],
        minimal_run_request_payload: dict[str, Any],
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload)

    resp: ClientResponse = http_client.get(f"/v1/tests/runs/{created.job_id}/artifacts", headers=http_headers)
    assert resp.status == 200, f"list_artifacts status={resp.status} body={resp.body!r}"

    parsed: ArtifactsListResponse = ArtifactsListResponse.model_validate(resp.json())
    assert parsed.job_id == created.job_id

    for item in parsed.files:
        assert isinstance(item.get("path"), str)
        assert isinstance(item.get("size_bytes"), int)
        assert isinstance(item.get("mtime"), (int, float))


def test_download_artifact_rejects_path_traversal(
        http_client: HttpJsonClient,
        http_headers: dict[str, str],
        minimal_run_request_payload: dict[str, Any],
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload)

    resp: ClientResponse = http_client.get(
            f"/v1/tests/runs/{created.job_id}/artifacts/../secrets.txt",
            headers=http_headers,
    )
    assert resp.status == 400, f"expected 400, got {resp.status} body={resp.body!r}"


def test_create_run_validation_error_duplicate_param_names(
        http_client: HttpJsonClient, http_headers: dict[str, str]
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
    assert resp.status == 400, f"expected 400, got {resp.status} body={resp.body!r}"
    body: Any = resp.json()
    assert isinstance(body, dict)
    assert "Duplicate parameter name" in str(body.get("detail", ""))


def test_download_any_artifact_after_job_completion(
        http_client: HttpJsonClient,
        http_headers: dict[str, str],
        minimal_run_request_payload: dict[str, Any],
        it_config: IntegrationTestConfig,
        capsys: Any,
) -> None:
    created: LlamatorTestRunResponse = _create_run(http_client, http_headers, minimal_run_request_payload)

    final_info: LlamatorJobInfo = _wait_job_terminal(
            http_client=http_client,
            headers=http_headers,
            job_id=created.job_id,
            timeout_s=it_config.http_timeout_s,
            interval_s=0.5,
    )
    assert _is_terminal_status(final_info.status)

    resp_list: ClientResponse = http_client.get(f"/v1/tests/runs/{created.job_id}/artifacts", headers=http_headers)
    assert resp_list.status == 200, f"list_artifacts status={resp_list.status} body={resp_list.body!r}"

    parsed: ArtifactsListResponse = ArtifactsListResponse.model_validate(resp_list.json())
    assert parsed.job_id == created.job_id
    assert parsed.files, f"Expected at least one artifact file for job_id={created.job_id}"

    first_path: str = str(parsed.files[0].get("path"))
    assert first_path.strip(), f"Expected non-empty artifact path for job_id={created.job_id}"

    download_path: str = _artifact_download_path(created.job_id, first_path)
    resp_dl: ClientResponse = http_client.get_no_redirect(download_path, headers=http_headers)

    if resp_dl.status == 307:
        url: str | None = resp_dl.headers.get("location")
        assert url is not None and url.strip(), f"Expected Location header for 307 redirect job_id={created.job_id} path={first_path}"
        with capsys.disabled():
            print(f"artifact_download_url={url}")
        return

    assert resp_dl.status == 200, f"download_artifact status={resp_dl.status} body={resp_dl.body[:200]!r}"
    assert resp_dl.body, f"Expected non-empty artifact body job_id={created.job_id} path={first_path}"