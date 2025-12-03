from __future__ import annotations

import json
from typing import Any, Callable


class _ApiKeyAsgiWrapper:
    """
    ASGI-обёртка для защиты приложений по заголовку X-API-Key.

    Если ключ пустой, проверка отключена.

    :param app: Внутреннее ASGI приложение.
    :param api_key: Ожидаемое значение ключа.
    """

    def __init__(self, app: Callable, api_key: str) -> None:
        self._app: Callable = app
        self._api_key: str = api_key

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        expected: str = self._api_key
        if not expected:
            await self._app(scope, receive, send)
            return

        method: str = str(scope.get("method", "GET")).upper()
        if method == "OPTIONS":
            await self._app(scope, receive, send)
            return

        header_map: dict[str, str] = {}
        for k, v in scope.get("headers", []) or []:
            if isinstance(k, (bytes, bytearray)) and isinstance(v, (bytes, bytearray)):
                header_map[k.decode("latin-1").lower()] = v.decode("latin-1")

        if header_map.get("x-api-key") != expected:
            body: bytes = b'{"detail":"Unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self._app(scope, receive, send)


def _header_value(headers: list[tuple[bytes, bytes]], key_lower: bytes) -> bytes | None:
    """
    Extract the first header value by lowercased name.

    :param headers: ASGI headers list (key/value bytes tuples).
    :param key_lower: Header name in lowercase bytes.
    :return: Header value or None.
    """
    for k, v in headers:
        if k.lower() == key_lower:
            return v
    return None


def _remove_headers(headers: list[tuple[bytes, bytes]], keys_lower: set[bytes]) -> list[tuple[bytes, bytes]]:
    """
    Remove all headers with names present in keys_lower.

    :param headers: ASGI headers list.
    :param keys_lower: A set of lowercased header names.
    :return: Filtered headers list.
    """
    return [(k, v) for (k, v) in headers if k.lower() not in keys_lower]


def _try_extract_json_from_sse(body: bytes) -> bytes | None:
    """
    Try extracting a JSON payload from an SSE body.

    The function searches for one or more SSE events and returns the first valid JSON
    DTO found in "data:" blocks.

    :param body: Raw SSE body bytes.
    :return: Raw JSON bytes if extraction succeeds; otherwise None.
    """
    if not body:
        return None

    data_lines: list[bytes] = []
    for raw_line in body.splitlines():
        line: bytes = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
        if not line:
            if data_lines:
                candidate: bytes = b"\n".join(data_lines).strip()
                try:
                    json.loads(candidate.decode("utf-8"))
                    return candidate
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data_lines.clear()
            continue

        if line.startswith(b"data:"):
            payload: bytes = line[5:]
            if payload.startswith(b" "):
                payload = payload[1:]
            data_lines.append(payload)

    if data_lines:
        candidate2: bytes = b"\n".join(data_lines).strip()
        try:
            json.loads(candidate2.decode("utf-8"))
            return candidate2
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    return None


class _McpSseToJsonWrapper:
    """
    ASGI wrapper converting single-message SSE responses to application/json for POST requests.

    This wrapper addresses clients that expect raw JSON-RPC responses over POST while the upstream
    handler returns SSE with "event: message" + "data: <json>" payload.

    :param app: Inner ASGI application.
    :param max_body_bytes: Max buffered body size in bytes before falling back to passthrough.
    """

    def __init__(self, app: Callable, max_body_bytes: int) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be >= 1.")
        self._app: Callable = app
        self._max_body_bytes: int = int(max_body_bytes)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method: str = str(scope.get("method", "GET")).upper()
        if method != "POST":
            await self._app(scope, receive, send)
            return

        captured_start: dict[str, Any] | None = None
        captured_body_msgs: list[dict[str, Any]] = []
        captured_bytes: int = 0
        passthrough: bool = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal captured_start, captured_body_msgs, captured_bytes, passthrough

            msg_type: str = str(message.get("type", ""))

            if passthrough:
                await send(message)
                return

            if msg_type == "http.response.start":
                captured_start = dict(message)
                return

            if msg_type == "http.response.body":
                if captured_start is None:
                    raise RuntimeError("ASGI protocol violation: response body sent before response start.")

                body_part: Any = message.get("body", b"")
                if not isinstance(body_part, (bytes, bytearray)):
                    raise RuntimeError("ASGI protocol violation: response body must be bytes.")

                captured_bytes += len(body_part)
                if captured_bytes > self._max_body_bytes:
                    passthrough = True
                    await send(captured_start)
                    for m in captured_body_msgs:
                        await send(m)
                    await send(message)
                    return

                captured_body_msgs.append(dict(message))
                return

            await send(message)

        await self._app(scope, receive, send_wrapper)

        if passthrough:
            return

        if captured_start is None:
            return

        headers: list[tuple[bytes, bytes]] = list(captured_start.get("headers", []))

        content_type_val: bytes | None = _header_value(headers, b"content-type")
        content_type: str = content_type_val.decode("latin-1").lower() if content_type_val is not None else ""

        if "text/event-stream" not in content_type:
            await send(captured_start)
            for m in captured_body_msgs:
                await send(m)
            return

        combined: bytes = b"".join(bytes(m.get("body", b"")) for m in captured_body_msgs)

        extracted: bytes | None = _try_extract_json_from_sse(combined)
        if extracted is None:
            await send(captured_start)
            for m in captured_body_msgs:
                await send(m)
            return

        new_headers: list[tuple[bytes, bytes]] = _remove_headers(headers, {b"content-type", b"content-length"})
        new_headers.append((b"content-type", b"application/json"))
        new_headers.append((b"content-length", str(len(extracted)).encode("ascii")))

        await send(
            {
                "type": "http.response.start",
                "status": int(captured_start.get("status", 200)),
                "headers": new_headers,
            }
        )
        await send({"type": "http.response.body", "body": extracted, "more_body": False})
