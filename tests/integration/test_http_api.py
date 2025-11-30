from __future__ import annotations

from typing import Any

from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import LlamatorJobInfo
from llamator_mcp_server.domain.models import LlamatorTestRunResponse
from tests.conftest import ArtifactsListResponse
from tests.conftest import ClientResponse
from tests.conftest import HttpJsonClient


def _create_run(http_client: HttpJsonClient, headers: dict[str, str],
                payload: dict[str, Any]) -> LlamatorTestRunResponse:
    resp: ClientResponse = http_client.post_json("/v1/tests/runs", payload, headers=headers)
    assert resp.status == 200, f"create_run status={resp.status} body={resp.body!r}"
    return LlamatorTestRunResponse.model_validate(resp.json())


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


def test_create_run_validation_error_duplicate_param_names(http_client: HttpJsonClient,
                                                           http_headers: dict[str, str]) -> None:
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