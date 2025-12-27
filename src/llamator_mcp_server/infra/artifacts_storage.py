from __future__ import annotations

import asyncio
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen

from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.infra.s3_presign import S3PresignConfig
from llamator_mcp_server.infra.s3_presign import S3Presigner

_UPLOAD_CHUNK_SIZE_BYTES: int = 1024 * 1024
_MAX_PARALLEL_UPLOADS: int = 4


@dataclass(frozen=True, slots=True)
class ArtifactDownloadTarget:
    """
    Resolved download target for artifact downloads.

    :param local_path: Local file path (when using local backend).
    :param redirect_url: Presigned URL (when using S3 backend).
    """
    local_path: Path | None
    redirect_url: str | None


class ArtifactsStorage:
    """
    Artifacts storage interface.
    """

    async def list_files(self, job_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def resolve_download(self, job_id: str, rel_path: str) -> ArtifactDownloadTarget:
        raise NotImplementedError

    async def upload_job_artifacts(self, job_id: str, local_root: Path) -> None:
        raise NotImplementedError


def _safe_posix_relpath(path: str) -> str:
    normalized: PurePosixPath = PurePosixPath(path)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("Invalid path.")
    return str(normalized)


class LocalArtifactsStorage(ArtifactsStorage):
    """
    Local filesystem artifacts storage.
    """

    def __init__(self, root: Path) -> None:
        self._root: Path = root

    def _list_files_sync(self, job_id: str) -> list[dict[str, Any]]:
        root: Path = (self._root / job_id).resolve(strict=False)
        if not root.exists():
            return []

        results: list[dict[str, Any]] = []
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                p: Path = Path(dirpath) / name
                try:
                    rel: str = str(p.relative_to(root))
                except ValueError:
                    continue
                st = p.stat()
                results.append({"path": rel, "size_bytes": st.st_size, "mtime": st.st_mtime})

        results.sort(key=lambda x: x["path"])
        return results

    async def list_files(self, job_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_files_sync, job_id)

    async def resolve_download(self, job_id: str, rel_path: str) -> ArtifactDownloadTarget:
        rel: str = _safe_posix_relpath(rel_path)
        root: Path = (self._root / job_id).resolve(strict=False)
        candidate: Path = (root / Path(*PurePosixPath(rel).parts)).resolve(strict=False)
        if root not in candidate.parents and candidate != root:
            raise FileNotFoundError("File not found")
        if not candidate.is_file():
            raise FileNotFoundError("File not found")
        return ArtifactDownloadTarget(local_path=candidate, redirect_url=None)

    async def upload_job_artifacts(self, job_id: str, local_root: Path) -> None:
        return


class S3ArtifactsStorage(ArtifactsStorage):
    """
    S3-compatible artifacts storage using presigned URLs.
    """

    def __init__(
            self,
            settings: Settings,
            presign_expires_seconds: int,
            list_max_keys: int,
    ) -> None:
        if presign_expires_seconds < 1:
            raise ValueError("presign_expires_seconds must be >= 1.")
        if list_max_keys < 1:
            raise ValueError("list_max_keys must be >= 1.")
        if not all([settings.s3_endpoint_url, settings.s3_bucket, settings.s3_access_key_id,
                    settings.s3_secret_access_key]):
            raise ValueError("S3 settings are not fully configured.")

        self._settings: Settings = settings
        self._presign_expires_seconds: int = presign_expires_seconds
        self._list_max_keys: int = list_max_keys

        self._presigner: S3Presigner = S3Presigner(
                S3PresignConfig(
                        endpoint_url=settings.s3_endpoint_url,
                        access_key_id=settings.s3_access_key_id,
                        secret_access_key=settings.s3_secret_access_key,
                        region=settings.s3_region or "us-east-1",
                )
        )

    async def list_files(self, job_id: str) -> list[dict[str, Any]]:
        prefix: str = self._job_prefix(job_id)
        all_items: list[dict[str, Any]] = []

        continuation: str | None = None
        while True:
            url: str = self._presigner.presign_list_objects_v2(
                    bucket=self._settings.s3_bucket,
                    prefix=prefix,
                    continuation_token=continuation,
                    max_keys=self._list_max_keys,
                    expires_seconds=self._presign_expires_seconds,
            )
            xml_bytes: bytes = await asyncio.to_thread(self._http_get_bytes, url)
            batch, next_token, is_truncated = self._parse_list_objects_v2(xml_bytes, prefix)
            all_items.extend(batch)
            if not is_truncated:
                break
            continuation = next_token
            if not continuation:
                break

        for item in all_items:
            item.pop("full_key", None)

        all_items.sort(key=lambda x: x.get("path", ""))
        return all_items

    async def resolve_download(self, job_id: str, rel_path: str) -> ArtifactDownloadTarget:
        key: str = self._object_key(job_id, rel_path)
        exists: bool = await self._object_exists(key)
        if not exists:
            raise FileNotFoundError("File not found")

        url: str = self._presigner.presign_get_object(
                bucket=self._settings.s3_bucket,
                key=key,
                expires_seconds=self._presign_expires_seconds,
        )
        return ArtifactDownloadTarget(local_path=None, redirect_url=url)

    @staticmethod
    def _collect_upload_files(root: Path) -> list[tuple[Path, str]]:
        out: list[tuple[Path, str]] = []
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                p: Path = Path(dirpath) / name
                if not p.is_file():
                    continue
                rel: str = str(p.relative_to(root))
                rel_posix: str = str(PurePosixPath(Path(rel).as_posix()))
                out.append((p, rel_posix))
        return out

    async def upload_job_artifacts(self, job_id: str, local_root: Path) -> None:
        if not local_root.exists():
            return

        root: Path = local_root.resolve(strict=False)
        files: list[tuple[Path, str]] = await asyncio.to_thread(self._collect_upload_files, root)
        if not files:
            return

        sem: asyncio.Semaphore = asyncio.Semaphore(_MAX_PARALLEL_UPLOADS)

        async def _upload_one(file_path: Path, rel_posix: str) -> None:
            key: str = self._object_key(job_id, rel_posix)
            url: str = self._presigner.presign_put_object(
                    bucket=self._settings.s3_bucket,
                    key=key,
                    expires_seconds=self._presign_expires_seconds,
            )
            await sem.acquire()
            try:
                try:
                    await asyncio.to_thread(self._http_put_file, url, file_path)
                except Exception as e:
                    raise RuntimeError(f"Upload failed key={key} file={file_path}") from e
            finally:
                sem.release()

        await asyncio.gather(*(_upload_one(p, rel) for (p, rel) in files))

    def _job_prefix(self, job_id: str) -> str:
        base: str = (self._settings.s3_key_prefix or "").strip().strip("/")
        if base:
            return f"{base}/{job_id}/"
        return f"{job_id}/"

    def _object_key(self, job_id: str, rel_path: str) -> str:
        rel: str = _safe_posix_relpath(rel_path)
        return f"{self._job_prefix(job_id)}{rel}"

    async def _object_exists(self, key: str) -> bool:
        url: str = self._presigner.presign_list_objects_v2(
                bucket=self._settings.s3_bucket,
                prefix=key,
                continuation_token=None,
                max_keys=1,
                expires_seconds=self._presign_expires_seconds,
        )
        xml_bytes: bytes = await asyncio.to_thread(self._http_get_bytes, url)
        batch, _, _ = self._parse_list_objects_v2(xml_bytes, "")
        return any(item.get("full_key") == key for item in batch)

    @staticmethod
    def _http_get_bytes(url: str) -> bytes:
        req = Request(url=url, method="GET")
        with urlopen(req, timeout=30) as resp:
            return resp.read()

    def _http_put_file(self, url: str, file_path: Path) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Invalid presigned URL scheme.")

        target = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        size: int = file_path.stat().st_size

        import http.client

        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=60)
        try:
            conn.putrequest("PUT", target)
            conn.putheader("Host", parsed.netloc)
            conn.putheader("Content-Length", str(size))
            conn.endheaders()

            with file_path.open("rb") as f:
                while True:
                    chunk = f.read(_UPLOAD_CHUNK_SIZE_BYTES)
                    if not chunk:
                        break
                    conn.send(chunk)

            resp = conn.getresponse()
            body = resp.read()
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"S3 PUT failed status={resp.status} body={body[:200]!r}")
        finally:
            conn.close()

    @staticmethod
    def _parse_list_objects_v2(xml_bytes: bytes, prefix: str) -> tuple[list[dict[str, Any]], str | None, bool]:
        root = ET.fromstring(xml_bytes)

        def _text(tag: str) -> str | None:
            el = root.find(f".//{{*}}{tag}")
            if el is None or el.text is None:
                return None
            return el.text

        is_truncated_val = _text("IsTruncated")
        is_truncated: bool = (is_truncated_val or "").lower() == "true"
        next_token: str | None = _text("NextContinuationToken") if is_truncated else None

        out: list[dict[str, Any]] = []
        for c in root.findall(".//{*}Contents"):
            key_el = c.find("{*}Key")
            size_el = c.find("{*}Size")
            lm_el = c.find("{*}LastModified")
            if key_el is None or key_el.text is None:
                continue
            full_key: str = key_el.text
            if not full_key.startswith(prefix):
                continue

            rel_path: str = full_key[len(prefix):]
            size_bytes: int = int(size_el.text) if size_el is not None and size_el.text is not None else 0
            mtime: float = 0.0
            if lm_el is not None and lm_el.text is not None:
                mtime = _parse_s3_time(lm_el.text).timestamp()

            out.append({"path": rel_path, "size_bytes": size_bytes, "mtime": mtime, "full_key": full_key})

        return out, next_token, is_truncated


def _parse_s3_time(val: str) -> datetime:
    s: str = val.strip()
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _s3_is_configured(settings: Settings) -> bool:
    return all(
            [
                settings.s3_endpoint_url,
                settings.s3_bucket,
                settings.s3_access_key_id,
                settings.s3_secret_access_key,
            ]
    )


def create_artifacts_storage(
        settings: Settings,
        presign_expires_seconds: int,
        list_max_keys: int,
) -> ArtifactsStorage:
    """
    Create artifacts storage based on configuration.

    :param settings: The application settings object.
    :param presign_expires_seconds: Presigned URL TTL for S3.
    :param list_max_keys: Max keys per ListObjectsV2 page.
    :return: ArtifactsStorage implementation.
    :raises ValueError: If configuration is invalid.
    """
    backend: str = settings.artifacts_backend.strip().lower()
    if backend not in ("local", "s3", "auto"):
        raise ValueError("artifacts_backend must be one of: local, s3, auto.")

    s3_configured: bool = _s3_is_configured(settings)

    if backend == "local":
        return LocalArtifactsStorage(root=settings.artifacts_root)

    if backend == "s3":
        if not s3_configured:
            raise ValueError("S3 backend selected but S3 settings are not fully configured.")
        return S3ArtifactsStorage(
                settings=settings,
                presign_expires_seconds=presign_expires_seconds,
                list_max_keys=list_max_keys,
        )

    if s3_configured:
        return S3ArtifactsStorage(
                settings=settings,
                presign_expires_seconds=presign_expires_seconds,
                list_max_keys=list_max_keys,
        )

    return LocalArtifactsStorage(root=settings.artifacts_root)