"""Write-once storage outside the database (design §4.3-§4.4).

Three kinds of fact must survive a database that is lost, restored or tampered
with: uploaded evidence bytes, published numbering ceilings and chain-head
anchors. They are written through one small interface so the provider can be
chosen later (OPEN-09) without touching a business command.

Only a local adapter exists today. It keeps each object in a folder, refuses to
overwrite a key with different bytes, and marks the file read-only. It is the
dev/CI stand-in for an object store with retention locks; it is **not** evidence
that production protection has been proven.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from django.conf import settings

from core.canonical import sha256_hex

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.:=-]{0,499}$")


class OffboxError(Exception):
    """Base for write-once storage failures."""


class OffboxUnavailable(OffboxError):
    """The store could not be reached or could not confirm a write."""


class WriteOnceConflict(OffboxError):
    """The key already holds different bytes; write-once storage never replaces."""


@dataclass(frozen=True)
class StoredObject:
    """What the store confirmed it holds."""

    key: str
    version: str
    sha256: str
    size: int


class WriteOnceStore(Protocol):
    def put(self, key: str, data: bytes) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def head(self, key: str) -> StoredObject | None: ...

    def list(self, prefix: str) -> list[StoredObject]: ...


def validate_key(key: str) -> str:
    if not _SAFE_KEY.match(key) or ".." in key.split("/") or "//" in key:
        raise OffboxError(f"unsafe object key {key!r}")
    return key


class LocalWriteOnceStore:
    """A folder that behaves like a write-once bucket."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def head(self, key: str) -> StoredObject | None:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:  # pragma: no cover - disk failure
            raise OffboxUnavailable(str(exc)) from exc
        digest = sha256_hex(data)
        return StoredObject(key=key, version=f"sha256:{digest}", sha256=digest, size=len(data))

    def put(self, key: str, data: bytes) -> StoredObject:
        path = self._path(key)
        digest = sha256_hex(data)
        existing = self.head(key)
        if existing is not None:
            if existing.sha256 != digest:
                raise WriteOnceConflict(f"{key} already holds different bytes")
            return existing
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".staging-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # `link` refuses an existing target, so two writers racing for
                    # one key cannot both believe they created it.
                    os.link(tmp_name, path)
                except FileExistsError:
                    pass
            finally:
                Path(tmp_name).unlink(missing_ok=True)
            os.chmod(path, 0o444)
        except OSError as exc:
            raise OffboxUnavailable(str(exc)) from exc
        confirmed = self.head(key)
        if confirmed is None or confirmed.sha256 != digest:
            raise WriteOnceConflict(f"{key} could not be confirmed with the written bytes")
        return confirmed

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise OffboxUnavailable(f"{key} is not in the store") from exc

    def list(self, prefix: str) -> list[StoredObject]:
        base = self.root / validate_key(prefix.rstrip("/")) if prefix else self.root
        if not base.exists():
            return []
        found: list[StoredObject] = []
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            if path.name.startswith(".staging-"):
                continue
            key = path.relative_to(self.root).as_posix()
            head = self.head(key)
            if head is not None:
                found.append(head)
        return found


def get_store() -> WriteOnceStore:
    """The deployment's configured write-once store."""
    root = getattr(settings, "KDPS_OFFBOX_ROOT", None)
    if not root:
        raise OffboxUnavailable("KDPS_OFFBOX_ROOT is not configured")
    return LocalWriteOnceStore(Path(root))
