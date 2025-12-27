# ![LLAMATOR](assets/LLAMATOR.svg)

MCP server for LLAMATOR: automate LLM red teaming workflows

[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC_BY--NC--SA_4.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)
[![GitHub Repo stars](https://img.shields.io/github/stars/LLAMATOR-Core/llamator-mcp-server)](https://github.com/LLAMATOR-Core/llamator-mcp-server/stargazers)
[![Chat](https://img.shields.io/badge/chat-gray.svg?logo=telegram)](https://t.me/llamator)

## Contents

- [What this server does](#what-this-server-does)
- [Architecture](#architecture)
- [Quick start (Docker Compose)](#quick-start-docker-compose)
- [Configuration (env)](#configuration-env)
- [HTTP API](#http-api)
- [MCP API (Streamable HTTP)](#mcp-api-streamable-http)
- [Request model](#request-model)
- [Artifact storage](#artifact-storage)
- [Metrics](#metrics)
- [Repository tests](#repository-tests)
- [License 📜](#license-)

## What this server does

This service wraps **LLAMATOR** into two programmatic interfaces:

- **HTTP API**: queue a test run, poll status, browse/download artifacts.
- **MCP server (Streamable HTTP)**: expose LLAMATOR as MCP tools for agent workflows.

It uses **Redis** to store job state and **ARQ** workers to execute LLAMATOR runs.

Main usage flow:

1. Submit a run request (tested model + test plan).
2. Worker runs LLAMATOR and aggregates results.
3. Poll job status and optionally download artifacts (logs/reports/etc.).

## Architecture

Components:

- **API container** (FastAPI + mounted MCP ASGI app):
    - Validates input (`validate_test_specs`).
    - Enqueues ARQ job (`run_llamator_job`).
    - Serves HTTP endpoints under `/v1/...`.
    - Exposes Swagger UI with persisted auth (`swagger_ui_parameters={"persistAuthorization": True}`).
    - Mounts MCP app under `LLAMATOR_MCP_MCP_MOUNT_PATH` (default: `/mcp`).
    - Protects:
        - HTTP API via FastAPI dependency (`X-API-Key` header).
        - MCP app via an ASGI wrapper (`X-API-Key` header).
    - Converts **single-message SSE** responses to `application/json` for MCP POST requests when upstream returns
      `text/event-stream` (buffered up to 1 MiB; otherwise passthrough).
- **Worker container** (ARQ):
    - Resolves test plan (presets + explicit test specs).
    - Runs LLAMATOR (`llamator.start_testing`) in a thread.
    - Persists `queued → running → succeeded/failed` state into Redis.
    - Handles artifacts lifecycle:
        - local backend: leaves artifacts on the shared volume
        - S3 backend: archives artifacts to `artifacts.zip`, uploads to S3 (presigned PUT), then cleans local directory
- **Redis**:
    - Stores job metadata, redacted request, results, errors (TTL-based).

Security:

- Optional API key for both HTTP and MCP routes via header `X-API-Key`.
- If `LLAMATOR_MCP_API_KEY` is empty, authentication is disabled.

## Quick start (Docker Compose)

Minimal run:

```bash
docker compose up --build
```

The compose stack typically includes:

- `redis` (ports: `6379:6379`)
- `api` (ports: `${LLAMATOR_MCP_HTTP_PORT:-8000}:${LLAMATOR_MCP_HTTP_PORT:-8000}`)
- `worker`

Expected external dependency:

- An **OpenAI-compatible** endpoint for attack/judge/tested models (e.g. LM Studio, vLLM, etc.), configured via env.

## Configuration (env)

All service settings are read from environment variables prefixed with `LLAMATOR_MCP_`.

### Redis

- `LLAMATOR_MCP_REDIS_DSN` (default: `redis://redis:6379/0`)  
  Redis DSN used by API and worker.

### Artifacts storage

- `LLAMATOR_MCP_ARTIFACTS_ROOT` (default: `/data/artifacts`)  
  Root directory where job artifacts are stored (one subdir per `job_id`).

### Artifacts backend

- `LLAMATOR_MCP_ARTIFACTS_BACKEND` (default: `auto`, allowed: `local|s3|auto`)  
  Artifacts backend selection:
    - `local`: serve artifacts from filesystem (`LLAMATOR_MCP_ARTIFACTS_ROOT`)
    - `s3`: store artifacts in S3-compatible storage (requires S3 env vars)
    - `auto`: use S3 if fully configured; otherwise fall back to local

### S3-compatible storage (when backend is `s3` or `auto`)

Required:

- `LLAMATOR_MCP_S3_ENDPOINT_URL` (e.g. `https://s3.example.com`)
- `LLAMATOR_MCP_S3_BUCKET`
- `LLAMATOR_MCP_S3_ACCESS_KEY_ID`
- `LLAMATOR_MCP_S3_SECRET_ACCESS_KEY`

Optional:

- `LLAMATOR_MCP_S3_REGION` (defaults to `us-east-1` internally if empty)
- `LLAMATOR_MCP_S3_KEY_PREFIX` (default: empty)  
  Key prefix under the bucket (e.g. `llamator-mcp`).

Behavior:

- Worker zips job artifacts into `artifacts.zip` and uploads it to S3 via a presigned PUT URL.
- HTTP downloads for S3 backend return **307 redirect** to a presigned GET URL (no direct file streaming from the API
  container).
- MCP tool output may include a presigned `artifacts_download_url` for `artifacts.zip` (if the archive exists).

### API security

- `LLAMATOR_MCP_API_KEY` (default: empty)  
  If set, requests must include header `X-API-Key: <value>` for protected routes.

Public routes:

- `GET /v1/health`, `GET /health`
- `GET /metrics`

Protected routes (require API key when enabled):

- `POST /v1/tests/runs`
- `GET /v1/tests/runs/{job_id}`
- `GET /v1/tests/runs/{job_id}/artifacts`
- `GET /v1/tests/runs/{job_id}/artifacts/{path}`
- MCP Streamable HTTP endpoint under `LLAMATOR_MCP_MCP_MOUNT_PATH`

### Attack model (OpenAI-compatible)

- `LLAMATOR_MCP_ATTACK_OPENAI_BASE_URL` (default: `http://localhost:1234/v1`)
- `LLAMATOR_MCP_ATTACK_OPENAI_MODEL` (default: `model-identifier`)
- `LLAMATOR_MCP_ATTACK_OPENAI_API_KEY` (default: `lm-studio`)
- `LLAMATOR_MCP_ATTACK_OPENAI_TEMPERATURE` (default: `0.5`)
- `LLAMATOR_MCP_ATTACK_OPENAI_SYSTEM_PROMPTS` (default: built-in prompt)

`*_SYSTEM_PROMPTS` accepts:

- JSON array (preferred), e.g. `["Prompt 1", "Prompt 2"]`
- or a newline-separated string

### Judge model (OpenAI-compatible)

- `LLAMATOR_MCP_JUDGE_OPENAI_BASE_URL` (default: `http://localhost:1234/v1`)
- `LLAMATOR_MCP_JUDGE_OPENAI_MODEL` (default: `model-identifier`)
- `LLAMATOR_MCP_JUDGE_OPENAI_API_KEY` (default: `lm-studio`)
- `LLAMATOR_MCP_JUDGE_OPENAI_TEMPERATURE` (default: `0.1`)
- `LLAMATOR_MCP_JUDGE_OPENAI_SYSTEM_PROMPTS` (default: built-in prompt)

### Job execution

- `LLAMATOR_MCP_JOB_TTL_SECONDS` (default: `604800`)  
  TTL for job keys in Redis.
- `LLAMATOR_MCP_RUN_TIMEOUT_SECONDS` (default: `3600`)  
  ARQ per-job timeout (worker side) and MCP `create_llamator_run` await timeout.
- `LLAMATOR_MCP_REPORT_LANGUAGE` (default: `en`, allowed: `en|ru`)  
  Default report language used in merged run config.

### Logging

- `LLAMATOR_MCP_LOG_LEVEL` (default: `INFO`)  
  Python logging level for app and worker.
- `LLAMATOR_MCP_UVICORN_LOG_LEVEL` (default: `info`)  
  Uvicorn log level for HTTP entrypoint.

### HTTP server

- `LLAMATOR_MCP_HTTP_HOST` (default: `0.0.0.0`)
- `LLAMATOR_MCP_HTTP_PORT` (default: `8000`)

### MCP mounting

- `LLAMATOR_MCP_MCP_MOUNT_PATH` (default: `/mcp`)  
  Path where the MCP ASGI app is mounted in FastAPI.
- `LLAMATOR_MCP_MCP_STREAMABLE_HTTP_PATH` (default: `/`)  
  Streamable HTTP path exposed by the MCP app (inside mount).

Effective MCP endpoint URL:

- `http://<host>:<port><LLAMATOR_MCP_MCP_MOUNT_PATH><LLAMATOR_MCP_MCP_STREAMABLE_HTTP_PATH>`

With defaults: `http://localhost:8000/mcp/`.

## HTTP API

All routes below (except health/metrics) are protected by `X-API-Key` if `LLAMATOR_MCP_API_KEY` is set.

### Health

- `GET /v1/health` → `{"status":"ok"}`
- `GET /health` → `{"status":"ok"}`

### Create a test run

- `POST /v1/tests/runs` → `LlamatorTestRunResponse`

Request body: `LlamatorTestRunRequest` (see [Request model](#request-model)).

Response (`200`):

```json
{
  "job_id": "<32-hex>",
  "status": "queued",
  "created_at": "2025-01-01T00:00:00Z"
}
```

Errors:

- `400` on validation failure (e.g. duplicate parameter names).
- `401` if API key is required and invalid/missing.

### Get run status

- `GET /v1/tests/runs/{job_id}` → `LlamatorJobInfo`

Contains:

- `status`: `queued | running | succeeded | failed`
- timestamps
- `request`: **redacted** request snapshot (no API keys; only `api_key_present: true|false`)
- optional `result` or `error`
- optional `error_notice` (a compact server-generated error string)

Errors:

- `404` if job does not exist.

### List artifacts

- `GET /v1/tests/runs/{job_id}/artifacts` → `ArtifactsListResponse`

Response:

```json
{
  "job_id": "<job_id>",
  "files": [
    {"path":"logs/run.log","size_bytes":1234,"mtime":1735689600.0}
  ]
}
```

Notes:

- If the job exists but has no artifacts yet: `files: []`.
- For S3 backend this lists object metadata under the job prefix (path/size/mtime).

Errors:

- `404` if job does not exist.
- `502` if artifacts backend is unavailable.

### Download artifact

- `GET /v1/tests/runs/{job_id}/artifacts/{path}` → file download

Behavior depends on artifacts backend:

- **local backend**: returns `200` with file content (`FileResponse`)
- **S3 backend**: returns `307` redirect to a presigned URL (`RedirectResponse`)

The server enforces safe relative paths (`..` escapes are rejected).

Errors:

- `400` invalid path
- `404` missing job or missing file
- `502` artifacts backend error

## MCP API (Streamable HTTP)

The MCP server is mounted under `LLAMATOR_MCP_MCP_MOUNT_PATH` and uses **Streamable HTTP** transport with:

- `stateless_http=True`
- `streamable_http_path=LLAMATOR_MCP_MCP_STREAMABLE_HTTP_PATH`
- `json_response=True`

Tools exposed:

- `create_llamator_run`
    - Input: `LlamatorTestRunRequest`
    - Behavior: submit job and **await completion** (within `LLAMATOR_MCP_RUN_TIMEOUT_SECONDS`)
    - Output: aggregated result dict + optional artifacts URL (S3 backend)
- `get_llamator_run`
    - Input: `job_id: str`
    - Behavior: fetch existing job and return aggregated result **only if finished**
    - Output: aggregated result dict + optional artifacts URL (S3 backend)

Tool output schema:

```json
{
  "job_id": "<job_id>",
  "aggregated": {
    "<attack_code_name>": {
      "<metric_name>": 123
    }
  },
  "artifacts_download_url": "https://presigned-url.example/...",
  "error_notice": "SomeError: some message"
}
```

`artifacts_download_url` is:

- a presigned URL to `artifacts.zip` for S3 backend (if the archive exists)
- `null` for local backend or if the archive is not available

`error_notice` is:

- `null` if the job succeeded without errors
- a compact error string for failed jobs (either stored by worker or built from the job error payload)

Headers commonly used by clients in this repo (integration tests):

- `Accept: application/json, text/event-stream`
- `MCP-Protocol-Version: <version>`
- `Origin: <scheme>://<host>`
- optionally `Mcp-Session-Id: <id>` after initialization
- optionally `X-API-Key: <key>` if auth is enabled

Transport detail:

- Some MCP handlers may respond with `text/event-stream` for POST. The server includes an ASGI wrapper that converts
  **single-message SSE** responses (`data: <json>`) into `application/json` for POST requests (when possible)
  to support clients that expect raw JSON responses.

## Request model

### `tested_model` (required)

OpenAI-compatible client config:

- `kind`: `"openai"` (required)
- `base_url`: OpenAI-compatible base URL (required), e.g. `http://host:port/v1`
- `model`: model identifier (required)
- `api_key`: optional
- `temperature`: optional, `[0.0, 2.0]`
- `system_prompts`: optional (tuple/list in JSON)
- `model_description`: optional

### `plan` (required)

Test plan can be defined via presets and/or explicit test lists:

- `preset_name`: optional preset name supported by LLAMATOR (e.g. `all`, `rus`, `owasp:llm01`)
- `num_threads`: optional, `>= 1`
- `basic_tests`: optional list of:
    - `code_name`: str
    - `params`: optional list of `{name, value}` (parameter names must be unique per test)
- `custom_tests`: optional list of:
    - `import_path`: fully-qualified class (import policy: must start with `llamator.` or `llamator_mcp_server.`)
    - `params`: optional list of `{name, value}` (names unique per test)

### `run_config` (optional)

User overrides for LLAMATOR run config:

- `enable_logging`: bool
- `enable_reports`: bool
- `artifacts_path`: safe relative path inside the job artifacts directory
- `debug_level`: int in `{0,1,2}`
- `report_language`: `"en" | "ru"`

The server merges user config with defaults and stores the resolved `artifacts_path` as an absolute path under:
`<LLAMATOR_MCP_ARTIFACTS_ROOT>/<job_id>/...`.

### Minimal example request (HTTP)

```json
{
  "tested_model": {
    "kind": "openai",
    "base_url": "http://host.docker.internal:1234/v1",
    "model": "model-identifier",
    "api_key": "lm-studio"
  },
  "plan": {
    "preset_name": "owasp:llm07",
    "num_threads": 1
  }
}
```

## Artifact storage

By default, each job writes artifacts into:

- `<LLAMATOR_MCP_ARTIFACTS_ROOT>/<job_id>/`

If `run_config.artifacts_path` is provided, it must be a **safe relative path** and is resolved inside the job root.

The HTTP API exposes:

- listing metadata for all files under job root (or job prefix for S3)
- downloading a single file by relative path (validated server-side)

S3 backend specifics:

- Worker creates an archive `artifacts.zip` and uploads it via presigned PUT.
- The API resolves downloads via presigned GET and returns 307 redirects.
- For local filesystem downloads, the API streams files directly.

## Metrics

Prometheus metrics are exposed on:

- `GET /metrics`

## Repository tests

The repository ships **integration tests** that exercise the running server.

Location:

- `tests/integration/test_http_api.py`
- `tests/integration/test_mcp_api.py`

What they verify:

### HTTP API tests

- `GET /v1/health` returns `{"status":"ok"}`
- `POST /v1/tests/runs` creates a job and returns `job_id` + initial status
- `GET /v1/tests/runs/{job_id}` returns `LlamatorJobInfo` and redacts secrets (`api_key_present`)
- `GET /v1/tests/runs/does-not-exist` returns `404`
- `GET /v1/tests/runs/{job_id}/artifacts` returns a schema-compatible listing
- `GET /v1/tests/runs/{job_id}/artifacts/../secrets.txt` is rejected (`400`)
- duplicate parameter names in test params cause `400` validation error
- after job completion, attempts to download the first listed artifact:
    - expects `307` + `Location` for S3 backend
    - expects `200` + non-empty body for local backend

### MCP API tests

- MCP `tools/list` contains `create_llamator_run` and `get_llamator_run`
- `create_llamator_run` returns a dict with:
    - `job_id` (32-char hex)
    - `aggregated` (may be empty if no tests executed)
    - optional `artifacts_download_url`
- MCP tool result may be returned in either:
    - `structuredContent`
    - or as JSON in `content[].text` (fallback parsing)

Test configuration:

- `tests/.env.test` defines the integration defaults:
    - timeouts and polling intervals
    - MCP protocol version header value
    - minimal request payload defaults (preset name, num threads, tested model base_url/model/api_key)

Optional override:

- `LLAMATOR_MCP_TEST_BASE_URL` allows running tests against an already deployed service.

## License 📜

This project is licensed under the terms of the **Creative Commons Attribution-NonCommercial-ShareAlike 4.0
International** license. See the [LICENSE](LICENSE) file for details.

[![Creative Commons License](https://i.creativecommons.org/l/by-nc-sa/4.0/88x31.png)](https://creativecommons.org/licenses/by-nc-sa/4.0/)