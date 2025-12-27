from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Final
from urllib.parse import quote
from urllib.parse import urlparse


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _aws_quote(val: str) -> str:
    return quote(val, safe="-_.~")


def _aws_quote_path(path: str) -> str:
    return quote(path, safe="/-_.~")


@dataclass(frozen=True, slots=True)
class S3PresignConfig:
    """
    S3 presign configuration.

    :param endpoint_url: Base endpoint URL (e.g. https://s3.regru.cloud).
    :param access_key_id: Access key id.
    :param secret_access_key: Secret access key.
    :param region: AWS region name (S3-compatible vendors typically accept "us-east-1").
    """

    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    region: str


class S3Presigner:
    """
    AWS Signature V4 presigner for S3-compatible storages.

    This presigner uses query-string authentication (presigned URLs) and does not rely
    on external dependencies.
    """

    _ALGO: Final[str] = "AWS4-HMAC-SHA256"
    _SERVICE: Final[str] = "s3"
    _SIGNED_HEADERS: Final[str] = "host"

    def __init__(self, cfg: S3PresignConfig) -> None:
        self._cfg: S3PresignConfig = cfg
        parsed = urlparse(cfg.endpoint_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("S3 endpoint_url must start with http:// or https://.")
        if not parsed.netloc:
            raise ValueError("S3 endpoint_url must include a host.")
        self._endpoint_parsed = parsed

    def presign_get_object(self, bucket: str, key: str, expires_seconds: int) -> str:
        return self._presign_url(
            method="GET",
            canonical_uri=f"/{bucket}/{key.lstrip('/')}",
            query_params={},
            expires_seconds=expires_seconds,
        )

    def presign_put_object(self, bucket: str, key: str, expires_seconds: int) -> str:
        return self._presign_url(
            method="PUT",
            canonical_uri=f"/{bucket}/{key.lstrip('/')}",
            query_params={},
            expires_seconds=expires_seconds,
        )

    def presign_list_objects_v2(
        self,
        bucket: str,
        prefix: str,
        continuation_token: str | None,
        max_keys: int,
        expires_seconds: int,
    ) -> str:
        qp: dict[str, str] = {
            "list-type": "2",
            "prefix": prefix,
            "max-keys": str(max_keys),
        }
        if continuation_token is not None:
            qp["continuation-token"] = continuation_token
        return self._presign_url(
            method="GET",
            canonical_uri=f"/{bucket}",
            query_params=qp,
            expires_seconds=expires_seconds,
        )

    def _presign_url(
        self,
        method: str,
        canonical_uri: str,
        query_params: dict[str, str],
        expires_seconds: int,
    ) -> str:
        if expires_seconds < 1:
            raise ValueError("expires_seconds must be >= 1.")

        now: datetime = _utcnow()
        amz_date: str = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp: str = now.strftime("%Y%m%d")

        host: str = self._endpoint_parsed.netloc
        credential_scope: str = f"{date_stamp}/{self._cfg.region}/{self._SERVICE}/aws4_request"
        credential: str = f"{self._cfg.access_key_id}/{credential_scope}"

        presign_params: dict[str, str] = {
            "X-Amz-Algorithm": self._ALGO,
            "X-Amz-Credential": credential,
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires_seconds),
            "X-Amz-SignedHeaders": self._SIGNED_HEADERS,
        }
        merged_qp: dict[str, str] = dict(query_params)
        merged_qp.update(presign_params)

        canonical_query: str = self._canonical_querystring(merged_qp)
        canonical_headers: str = f"host:{host}\n"
        payload_hash: str = "UNSIGNED-PAYLOAD"

        canonical_request: str = (
            f"{method}\n"
            f"{_aws_quote_path(canonical_uri)}\n"
            f"{canonical_query}\n"
            f"{canonical_headers}\n"
            f"{self._SIGNED_HEADERS}\n"
            f"{payload_hash}"
        )

        string_to_sign: str = (
            f"{self._ALGO}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{_sha256_hex(canonical_request.encode('utf-8'))}"
        )

        signing_key: bytes = self._get_signing_key(self._cfg.secret_access_key, date_stamp, self._cfg.region)
        signature: str = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        final_query: str = f"{canonical_query}&X-Amz-Signature={signature}"

        base: str = f"{self._endpoint_parsed.scheme}://{host}"
        return f"{base}{_aws_quote_path(canonical_uri)}?{final_query}"

    @staticmethod
    def _get_signing_key(secret_access_key: str, date_stamp: str, region: str) -> bytes:
        k_date: bytes = _hmac_sha256(("AWS4" + secret_access_key).encode("utf-8"), date_stamp)
        k_region: bytes = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
        k_service: bytes = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()

    @staticmethod
    def _canonical_querystring(params: dict[str, str]) -> str:
        encoded: list[tuple[str, str]] = [(_aws_quote(k), _aws_quote(v)) for (k, v) in params.items()]
        encoded.sort()
        return "&".join(f"{k}={v}" for (k, v) in encoded)
