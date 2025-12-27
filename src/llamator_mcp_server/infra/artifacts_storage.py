from __future__ import annotations

import asyncio
import os
import tempfile
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from typing import Final
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen

from llamator_mcp_server.config.settings import Settings
from llamator_mcp_server.infra.s3_presign import S3PresignConfig
from llamator_mcp_server.infra.s3_presign import S3Presigner

_UPLOAD_CHUNK_SIZE_BYTES: int = 1024 * 1024
ARTIFACTS_ARCHIVE_NAME: Final[str] = "artifacts.zip"


class ArtifactsStorageError(RuntimeError):
    """
    Artifacts storage operation error.

    This exception is raised for backend communication/parsing errors
    (e.g. S3 request failures or invalid XML responses).
    """


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
    if str(normalized) in ("", "."):
        raise ValueError("Invalid path.")
    return str(normalized)


def _collect_files_for_zip(root: Path) -> list[tuple[Path, str]]:
    """
    Collect files under root for ZIP packaging.

    :param root: Root directory to walk.
    :return: Sorted list of (absolute_path, archive_relative_posix_path).
    """
    out: list[tuple[Path, str]] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p: Path = Path(dirpath) / name
            if not p.is_file():
                continue
            rel: str = str(p.relative_to(root))
            rel_posix: str = str(PurePosixPath(Path(rel).as_posix()))
            out.append((p, rel_posix))
    out.sort(key=lambda x: x[1])
    return out


def _build_zip_archive(root: Path, out_file: Path) -> None:
    """
    Build a ZIP archive from all files under root.

    :param root: Root directory to archive.
    :param out_file: Output ZIP file path.
    :return: None.
    """
    files: list[tuple[Path, str]] = _collect_files_for_zip(root)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_file, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path, arc_name in files:
            zf.write(file_path, arcname=arc_name)


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
        if not all(
            [
                settings.s3_endpoint_url,
                settings.s3_bucket,
                settings.s3_access_key_id,
                settings.s3_secret_access_key,
            ]
        ):
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

            try:
                xml_bytes: bytes = await asyncio.to_thread(self._http_get_bytes, url)
                batch, next_token, is_truncated = self._parse_list_objects_v2(xml_bytes, prefix)
            except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, ValueError) as e:
                raise ArtifactsStorageError(f"S3 list_objects_v2 failed prefix={prefix!r}") from e

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

        try:
            exists: bool = await self._object_exists(key)
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, ValueError) as e:
            raise ArtifactsStorageError(f"S3 resolve_download failed key={key!r}") from e

        if not exists:
            raise FileNotFoundError("File not found")

        url: str = self._presigner.presign_get_object(
            bucket=self._settings.s3_bucket,
            key=key,
            expires_seconds=self._presign_expires_seconds,
        )
        return ArtifactDownloadTarget(local_path=None, redirect_url=url)

    async def upload_job_artifacts(self, job_id: str, local_root: Path) -> None:
        if not local_root.exists():
            return

        root: Path = local_root.resolve(strict=False)

        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="llamator-artifacts-", suffix=".zip")
            os.close(fd)
            tmp_path = Path(tmp_name)

            await asyncio.to_thread(_build_zip_archive, root, tmp_path)

            key: str = self._object_key(job_id, ARTIFACTS_ARCHIVE_NAME)
            url: str = self._presigner.presign_put_object(
                bucket=self._settings.s3_bucket,
                key=key,
                expires_seconds=self._presign_expires_seconds,
            )
            await asyncio.to_thread(self._http_put_file, url, tmp_path)
        except Exception as e:
            raise ArtifactsStorageError(f"S3 upload_job_artifacts failed job_id={job_id!r}") from e
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

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
            conn.putrequest("PUT", target, skip_host=True, skip_accept_encoding=True)
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

            rel_path: str = full_key[len(prefix) :]
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
