from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def build_openapi_schema(app: FastAPI, scheme_name: str, api_key_header_name: str) -> dict[str, Any]:
    """
    Build and cache an OpenAPI schema with an API key security scheme.

    This helper is compatible with FastAPI automatic security generation:
    protected endpoints that declare Security(APIKeyHeader(..., scheme_name=scheme_name))
    will reference the scheme and expose Swagger UI "Authorize".

    The function does not enforce global security requirements to avoid
    applying auth to public endpoints (e.g. health, metrics).

    :param app: FastAPI application instance.
    :param scheme_name: OpenAPI security scheme name (e.g. "McpApiKey").
    :param api_key_header_name: Header name (e.g. "X-API-Key").
    :return: OpenAPI schema dict.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema

    schema: dict[str, Any] = get_openapi(
        title=str(app.title),
        version=str(app.version),
        routes=app.routes,
        description=app.description,
    )

    components: dict[str, Any] = schema.setdefault("components", {})
    security_schemes: dict[str, Any] = components.setdefault("securitySchemes", {})

    security_schemes.setdefault(
        scheme_name,
        {
            "type": "apiKey",
            "in": "header",
            "name": api_key_header_name,
        },
    )

    app.openapi_schema = schema
    return schema
