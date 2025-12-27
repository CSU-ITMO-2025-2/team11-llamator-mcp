# team11-llamator-mcp/tests/integration/test_mcp_api.py
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import McpJsonRpcClient
from tests.conftest import McpSession
from tests.conftest import ResponseReporter


def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
    return {str(t.get("name", "")) for t in tools if isinstance(t.get("name"), str)}


def _extract_structured(result: dict[str, Any]) -> dict[str, Any] | None:
    val: Any = result.get("structuredContent")
    if isinstance(val, dict):
        return val
    return None


def _extract_text_json_from_content(result: dict[str, Any]) -> dict[str, Any] | None:
    content: Any = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text: Any = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _payload_for_tool_schema(tool: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    input_schema: Any = tool.get("inputSchema")
    if not isinstance(input_schema, dict):
        return payload
    props: Any = input_schema.get("properties")
    if isinstance(props, dict) and "req" in props and isinstance(props["req"], dict):
        return {"req": payload}
    return payload


def _assert_start_testing_result_schema(payload: Any) -> None:
    assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"

    # Empty aggregated results are valid (e.g. unreachable tested model / no executed tests).
    if not payload:
        return

    for k, v in payload.items():
        assert isinstance(k, str), f"Expected str key, got {type(k)}"
        assert isinstance(v, dict), f"Expected dict value, got {type(v)}"
        for k2, v2 in v.items():
            assert isinstance(k2, str), f"Expected str inner key, got {type(k2)}"
            assert isinstance(v2, int), f"Expected int inner value, got {type(v2)}"


def _assert_mcp_run_result_schema(payload: Any) -> None:
    """
    Assert MCP tool run result schema.

    Expected payload shape:
    - job_id: str (uuid4 hex, 32 chars)
    - artifacts_download_url: str | None (present for S3 backend)
    - aggregated: dict[str, dict[str, int]]
    """
    assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
    assert payload, "Expected non-empty MCP tool result dict."

    job_id: Any = payload.get("job_id")
    assert isinstance(job_id, str), f"Expected job_id to be str, got {type(job_id)}"
    assert len(job_id) == 32, f"Expected job_id length 32, got {len(job_id)}"

    artifacts_url: Any = payload.get("artifacts_download_url")
    if artifacts_url is not None:
        assert isinstance(artifacts_url, str), f"Expected artifacts_download_url to be str, got {type(artifacts_url)}"
        assert artifacts_url.strip(), "Expected non-empty artifacts_download_url when provided."

    err_val: Any = payload.get("error")
    if err_val is not None:
        if isinstance(err_val, str):
            assert err_val.strip(), "Expected non-empty error string when provided."
            return
        if isinstance(err_val, dict):
            assert err_val, "Expected non-empty error object when provided."
            return

    aggregated: Any = payload.get("aggregated")
    _assert_start_testing_result_schema(aggregated)


def test_mcp_tools_list_contains_llamator_tools(mcp_client: McpJsonRpcClient, mcp_session: McpSession) -> None:
    tools: list[dict[str, Any]] = mcp_client.list_tools(mcp_session)
    names: set[str] = _tool_names(tools)

    assert "create_llamator_run" in names
    assert "get_llamator_run" in names


def test_mcp_create_run_returns_start_testing_result(
    mcp_client: McpJsonRpcClient,
    mcp_session: McpSession,
    minimal_run_request_payload: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    reporter: ResponseReporter,
) -> None:
    tools: list[dict[str, Any]] = mcp_client.list_tools(mcp_session)
    tool_map: dict[str, dict[str, Any]] = {str(t.get("name")): t for t in tools if isinstance(t.get("name"), str)}

    create_tool: dict[str, Any] = tool_map["create_llamator_run"]
    create_args: dict[str, Any] = _payload_for_tool_schema(create_tool, minimal_run_request_payload)

    created_result: dict[str, Any] = mcp_client.call_tool(mcp_session, "create_llamator_run", arguments=create_args)

    created_struct: dict[str, Any] | None = _extract_structured(created_result)
    created_fallback: dict[str, Any] | None = _extract_text_json_from_content(created_result)

    assert (
        created_struct is not None or created_fallback is not None
    ), f"Tool result does not contain structuredContent or JSON text content: {created_result!r}"

    created_payload: dict[str, Any] = created_struct or created_fallback or {}

    _assert_mcp_run_result_schema(created_payload)

    with capsys.disabled():
        reporter.message(json.dumps(created_payload, ensure_ascii=False, indent=2, sort_keys=True))
