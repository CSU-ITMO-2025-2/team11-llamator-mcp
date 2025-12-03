from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from arq.connections import RedisSettings
from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class RedisConfig:
    """
    Нормализованная конфигурация Redis.

    :param dsn: DSN Redis (redis://host:port/db).
    """

    dsn: str


def parse_redis_settings(dsn: str) -> RedisSettings:
    """
    Преобразовать DSN Redis в RedisSettings для ARQ.

    :param dsn: Строка DSN.
    :return: RedisSettings для ARQ.
    :raises ValueError: Если DSN некорректен.
    """
    parsed = urlparse(dsn)
    if parsed.scheme not in ("redis", "rediss"):
        raise ValueError("Only redis:// and rediss:// schemes are supported.")
    if not parsed.hostname:
        raise ValueError("Redis DSN must include hostname.")

    database: int = 0
    if parsed.path and parsed.path != "/":
        raw_db: str = parsed.path.lstrip("/")
        if not raw_db.isdigit():
            raise ValueError("Redis DSN database must be an integer.")
        database = int(raw_db)

    return RedisSettings(
        host=parsed.hostname,
        port=parsed.port or 6379,
        database=database,
        password=parsed.password,
        username=parsed.username,
        ssl=(parsed.scheme == "rediss"),
    )


def create_redis_client(dsn: str) -> Redis:
    """
    Создать asyncio Redis-клиент.

    :param dsn: DSN Redis.
    :return: Redis клиент.
    """
    return Redis.from_url(dsn, decode_responses=True)
