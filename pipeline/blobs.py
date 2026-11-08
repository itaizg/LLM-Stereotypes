"""Content-addressed blob storage for raw files, MT responses, and snapshots.

Raw files are stored once under their SHA-256 and never changed (FR-009), so a
stored hash is a durable promise about content. The same two-method layout
(``raw/<sha256>`` and ``snapshots/<version>/...``) works on a local directory
and on S3, behind one protocol, so stage logic never imports an SDK
(constitution Principle V, dependency inversion) and tests need no cloud
account (FR-027).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

DEFAULT_BLOB_ROOT = "./.pipeline-data"

_SHA256 = re.compile(r"[0-9a-f]{64}")


class BlobCorruptionError(Exception):
    """A stored file's bytes do not hash to the name it is stored under."""


class BlobExistsError(FileExistsError):
    """A named object already exists and overwriting was not requested."""


class BlobStore(Protocol):
    """The only storage surface the stages use (research R5)."""

    def put(self, data: bytes) -> str:
        """Store raw bytes under their SHA-256 and return it. Never overwrites."""
        ...

    def get(self, sha256: str) -> bytes: ...

    def exists(self, sha256: str) -> bool: ...

    def put_named(self, path: str, data: bytes, *, overwrite: bool = False) -> None:
        """Store a named object. Raises ``BlobExistsError`` unless ``overwrite``."""
        ...

    def get_named(self, path: str) -> bytes: ...

    def exists_named(self, path: str) -> bool: ...


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_sha(sha256: str) -> str:
    if not _SHA256.fullmatch(sha256):
        raise ValueError(f"not a SHA-256 hex digest: {sha256!r}")
    return sha256


def _check_name(path: str) -> PurePosixPath:
    """Reject names that could escape the blob root."""
    name = PurePosixPath(path)
    if not path or name.is_absolute() or ".." in name.parts:
        raise ValueError(f"unsafe blob name: {path!r}")
    return name


class LocalBlobStore:
    """A blob store on a local directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _raw_path(self, sha256: str) -> Path:
        return self._root / "raw" / _check_sha(sha256)

    def _verified(self, path: Path, sha256: str) -> None:
        if _sha256(path.read_bytes()) != sha256:
            raise BlobCorruptionError(f"stored bytes do not match name {sha256}")

    def _publish(self, target: Path, data: bytes, *, overwrite: bool) -> bool:
        """Write ``data`` to ``target`` atomically; return False if it existed.

        The bytes go to a temporary file in the same directory, are fsynced, and
        are then linked (or, when overwriting, renamed) to the final name. A
        crash therefore leaves at most a stray temporary file, never a partial
        file at the final name.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(tmp, target)
                return True
            try:
                os.link(tmp, target)
            except FileExistsError:
                return False
            return True
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def put(self, data: bytes) -> str:
        sha256 = _sha256(data)
        target = self._raw_path(sha256)
        if target.exists():
            self._verified(target, sha256)
            return sha256
        if not self._publish(target, data, overwrite=False):
            self._verified(target, sha256)  # Lost a race to an identical writer.
        return sha256

    def get(self, sha256: str) -> bytes:
        path = self._raw_path(sha256)
        data = path.read_bytes()
        if _sha256(data) != sha256:
            raise BlobCorruptionError(f"stored bytes do not match name {sha256}")
        return data

    def exists(self, sha256: str) -> bool:
        return self._raw_path(sha256).exists()

    def put_named(self, path: str, data: bytes, *, overwrite: bool = False) -> None:
        target = self._root / _check_name(path)
        if not self._publish(target, data, overwrite=overwrite):
            raise BlobExistsError(path)

    def get_named(self, path: str) -> bytes:
        return (self._root / _check_name(path)).read_bytes()

    def exists_named(self, path: str) -> bool:
        return (self._root / _check_name(path)).exists()


class S3BlobStore:
    """A blob store on S3, using conditional writes to forbid overwrites.

    The boto3 client is passed in (a third-party trust boundary, hence ``Any``)
    so tests can drive it with ``botocore.stub.Stubber`` and no network.
    """

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _key(self, name: str) -> str:
        return self._prefix + name

    def _put_if_absent(self, key: str, data: bytes) -> bool:
        """Return True if written, False if the key already existed."""
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, IfNoneMatch="*"
            )
        except self._client.exceptions.ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "412"}:
                return False
            raise
        return True

    def _read(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def put(self, data: bytes) -> str:
        sha256 = _sha256(data)
        key = self._key(f"raw/{sha256}")
        if not self._put_if_absent(key, data) and _sha256(self._read(key)) != sha256:
            raise BlobCorruptionError(f"stored bytes do not match name {sha256}")
        return sha256

    def get(self, sha256: str) -> bytes:
        data = self._read(self._key(f"raw/{_check_sha(sha256)}"))
        if _sha256(data) != sha256:
            raise BlobCorruptionError(f"stored bytes do not match name {sha256}")
        return data

    def exists(self, sha256: str) -> bool:
        return self._head(self._key(f"raw/{_check_sha(sha256)}"))

    def _head(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                return False
            raise
        return True

    def put_named(self, path: str, data: bytes, *, overwrite: bool = False) -> None:
        key = self._key(str(_check_name(path)))
        if overwrite:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        elif not self._put_if_absent(key, data):
            raise BlobExistsError(path)

    def get_named(self, path: str) -> bytes:
        return self._read(self._key(str(_check_name(path))))

    def exists_named(self, path: str) -> bool:
        return self._head(self._key(str(_check_name(path))))


def open_blob_store(root: str | None = None) -> BlobStore:
    """Open the store named by ``root`` or ``PIPELINE_BLOB_ROOT``.

    ``s3://bucket/prefix`` selects S3 (credentials come from the standard boto3
    chain, never from this code); anything else is a local directory.
    """
    location = root or os.environ.get("PIPELINE_BLOB_ROOT") or DEFAULT_BLOB_ROOT
    if location.startswith("s3://"):
        import boto3  # Imported here so local runs never touch the SDK.

        bucket, _, prefix = location.removeprefix("s3://").partition("/")
        return S3BlobStore(boto3.client("s3"), bucket, prefix)
    return LocalBlobStore(Path(location))
