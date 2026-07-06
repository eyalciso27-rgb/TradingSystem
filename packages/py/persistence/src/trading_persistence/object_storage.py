"""Object storage port + filesystem adapter (IMP-0205).

Content-addressed storage for raw evidence, attachments and artifacts.
Authority: Technology_and_Deployment_Decisions_v0.6 ADR-008 (Filesystem
Content-Addressed Adapter in the MVP; a future OCI/S3 adapter replaces it
behind the same interface) and Data_Model_and_Retention_Specification_v0.6
(raw_objects: URI/MIME/size/hash/pinning; retention R4 — pinned objects are
kept, unpinned large attachments expire).

Design:

- The reference IS the content hash (``sha256:<hex>``), matching the
  raw-object references used by the contracts (IMP-0103) and raw_objects.
- ``put`` is idempotent: storing the same bytes twice yields the same
  reference and one stored object.
- ``get`` re-hashes the bytes read and fails closed on any mismatch — a
  corrupted or tampered object is never returned silently.
- Pinning is owned by the database (raw_objects.pinned); the adapter's
  ``purge_unpinned`` receives the pinned reference set and never deletes a
  pinned or too-recent object.

No credentials, no cloud provider, pure stdlib — identical behaviour on
Windows x64 and Linux ARM64 (pathlib paths, binary I/O).
"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

SHA256_REF_PREFIX: Final[str] = "sha256:"


class ObjectStorageError(Exception):
    """Base class for object storage failures."""


class ObjectNotFoundError(ObjectStorageError):
    """The requested reference does not exist in the store."""


class ObjectIntegrityError(ObjectStorageError):
    """Stored bytes do not match the content-addressed reference."""


class InvalidReferenceError(ObjectStorageError):
    """The reference is not a valid sha256 content reference."""


@dataclass(frozen=True)
class StoredObject:
    """Result of storing an object: its content reference and size."""

    ref: str
    size_bytes: int


def content_ref(data: bytes) -> str:
    """The canonical content-addressed reference of ``data``."""
    return SHA256_REF_PREFIX + hashlib.sha256(data).hexdigest()


def _validate_ref(ref: str) -> str:
    digest = ref.removeprefix(SHA256_REF_PREFIX)
    if ref == digest or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise InvalidReferenceError(f"not a sha256 content reference: {ref!r}")
    return digest


class ObjectStoragePort(Protocol):
    """Provider-neutral object storage port (ADR-008)."""

    def put(self, data: bytes) -> StoredObject: ...

    def get(self, ref: str) -> bytes: ...

    def exists(self, ref: str) -> bool: ...

    def purge_unpinned(self, older_than_seconds: float, pinned: frozenset[str]) -> list[str]: ...


class FilesystemObjectStorage:
    """Content-addressed filesystem adapter (the MVP implementation)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        return self._root / digest[:2] / digest

    def put(self, data: bytes) -> StoredObject:
        ref = content_ref(data)
        path = self._path_for(_validate_ref(ref))
        if not path.is_file():  # idempotent: same content -> same object
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return StoredObject(ref=ref, size_bytes=len(data))

    def get(self, ref: str) -> bytes:
        digest = _validate_ref(ref)
        path = self._path_for(digest)
        if not path.is_file():
            raise ObjectNotFoundError(ref)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ObjectIntegrityError(f"stored bytes do not match {ref}")
        return data

    def exists(self, ref: str) -> bool:
        return self._path_for(_validate_ref(ref)).is_file()

    def purge_unpinned(self, older_than_seconds: float, pinned: frozenset[str]) -> list[str]:
        """Delete unpinned objects older than the cut-off; return purged refs.

        Pinned references are never deleted (retention rule R1/R4); recent
        objects are kept regardless.
        """
        cutoff = time.time() - older_than_seconds
        purged: list[str] = []
        for path in sorted(self._root.glob("*/*")):
            if not path.is_file() or path.suffix == ".tmp":
                continue
            ref = SHA256_REF_PREFIX + path.name
            if ref in pinned or path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            purged.append(ref)
        return purged
