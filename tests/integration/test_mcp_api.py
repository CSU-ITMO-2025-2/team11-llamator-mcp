# llamator-mcp-server/tests/integration/test_mcp_api.py
from __future__ import annotations

import json
from typing import Any

from llamator_mcp_server.domain.models import JobStatus
from llamator_mcp_server.domain.models import LlamatorJobInfo

from tests.conftest import McpJsonRpcClient
from tests.conftest import McpSession


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


def test_mcp_tools_list_contains_llamator_tools(mcp_client: McpJsonRpcClient, mcp_session: McpSession) -> None:
    tools: list[dict[str, Any]] = mcp_client.list_tools(mcp_session)
    names: set[str] = _tool_names(tools)

    assert "create_llamator_run" in names
    assert "get_llamator_run" in names


def test_mcp_create_and_get_run(
        mcp_client: McpJsonRpcClient,
        mcp_session: McpSession,
        minimal_run_request_payload: dict[str, Any],
) -> None:
    tools: list[dict[str, Any]] = mcp_client.list_tools(mcp_session)
    tool_map: dict[str, dict[str, Any]] = {str(t.get("name")): t for t in tools if isinstance(t.get("name"), str)}

    create_tool: dict[str, Any] = tool_map["create_llamator_run"]
    get_tool: dict[str, Any] = tool_map["get_llamator_run"]

    create_args: dict[str, Any] = _payload_for_tool_schema(create_tool, minimal_run_request_payload)
    created_result: dict[str, Any] = mcp_client.call_tool(mcp_session, "create_llamator_run", arguments=create_args)

    created_struct: dict[str, Any] | None = _extract_structured(created_result)
    created_fallback: dict[str, Any] | None = _extract_text_json_from_content(created_result)
    created_payload: dict[str, Any] = created_struct or created_fallback or {}
    created: LlamatorJobInfo = LlamatorJobInfo.model_validate(created_payload)

    assert created.job_id
    assert len(created.job_id) == 32
    assert created.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)
    assert created.created_at <= created.updated_at

    if created.status == JobStatus.SUCCEEDED:
        assert created.result is not None
        assert created.error is None
    if created.status == JobStatus.FAILED:
        assert created.error is not None

    get_args: dict[str, Any] = _payload_for_tool_schema(get_tool, {"job_id": created.job_id})
    got_result: dict[str, Any] = mcp_client.call_tool(mcp_session, "get_llamator_run", arguments=get_args)

    got_struct: dict[str, Any] | None = _extract_structured(got_result)
    got_fallback: dict[str, Any] | None = _extract_text_json_from_content(got_result)
    got_payload: dict[str, Any] = got_struct or got_fallback or {}
    info: LlamatorJobInfo = LlamatorJobInfo.model_validate(got_payload)

    assert info.job_id == created.job_id
    assert info.created_at <= info.updated_at
    assert info.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)