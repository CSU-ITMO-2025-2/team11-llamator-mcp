from __future__ import annotations

from typing import Final

import logging


def configure_logging(level: str) -> None:
    """
    Настроить логирование приложения.

    :param level: Уровень логирования (например, ``INFO``).
    :return: None
    """
    root: logging.Logger = logging.getLogger()
    if root.handlers:
        return

    formatter: logging.Formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler: logging.StreamHandler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root.setLevel(level.upper())
    root.addHandler(handler)


LOGGER_NAME: Final[str] = "llamator_mcp_server"