from __future__ import annotations

import logging
from typing import Final


class _SuppressMcpClosedResourceErrorFilter(logging.Filter):
    """
    Filter out noisy anyio.ClosedResourceError tracebacks emitted by MCP Streamable HTTP.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("mcp.server.streamable_http"):
            return True

        if record.exc_info is None:
            return True

        exc: BaseException | None = record.exc_info[1]
        if exc is None:
            return True

        return type(exc).__name__ != "ClosedResourceError"


def configure_logging(level: str) -> None:
    """
    Настроить логирование приложения.

    :param level: Уровень логирования (например, ``INFO``).
    :return: None
    """
    root: logging.Logger = logging.getLogger()
    lvl: str = level.upper()

    if root.handlers:
        root.setLevel(lvl)
        for h in root.handlers:
            if not any(isinstance(f, _SuppressMcpClosedResourceErrorFilter) for f in h.filters):
                h.addFilter(_SuppressMcpClosedResourceErrorFilter())
        return

    formatter: logging.Formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler: logging.StreamHandler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(_SuppressMcpClosedResourceErrorFilter())

    root.setLevel(lvl)
    root.addHandler(handler)


LOGGER_NAME: Final[str] = "llamator_mcp_server"
