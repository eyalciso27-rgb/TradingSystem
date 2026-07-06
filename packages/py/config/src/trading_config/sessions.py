"""Encrypted runtime session store (IMP-0303).

Authority: Integration_Specification_v0.6 section 20.7 (dynamic sessions live
in an Encrypted Runtime Session Store; a connector may access ONLY its own
session namespace; AUTH_REQUIRED is a real state, not an empty feed),
Security_and_Operations_Specification_v0.6 sections 5-6 (session material is
never stored in domain tables or logs; Paper and Live stores are separate;
SESSION_CORRUPTION means isolate and re-login, never an uncontrolled copy)
and Data_Model_and_Retention_Specification_v0.6 section 30.3 (domain tables
hold only an opaque session_ref).

Model: one encrypted blob per (environment, connector). The plaintext session
material (e.g. a TDLib session database) exists only in memory; on disk it is
a Fernet (AES-CBC + HMAC) token, so no raw token/cookie/password/private key
is ever persisted in plaintext. Visible metadata is non-sensitive only
(connector id, environment, timestamps, status).

Fail-closed: a missing, corrupted (wrong key / tampered) or expired session
raises a dedicated error — the caller must go through re-authentication
(AUTH_REQUIRED), never a fallback. Revocation and rotation are per-connector:
revoking one connector's session cannot affect any other (the DoD).

This is NOT a secret store: static secrets (api_id/api_hash/bot tokens) stay
in trading_config.secrets; the session encryption key itself is supplied as a
SecretValue from that store.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from trading_contracts.enums import Environment

from trading_config.secrets import SecretValue

_SUFFIX: Final[str] = ".session"


class SessionError(Exception):
    """Base class; never contains session material."""


class MissingSessionError(SessionError):
    """No stored session for this connector: re-authentication is required."""


class CorruptSessionError(SessionError):
    """The stored blob failed authentication/decryption: isolate and re-login."""


class ExpiredSessionError(SessionError):
    """The stored session is past its expiry: re-authentication is required."""


class SessionPathError(SessionError):
    """The connector id is unsafe as a session namespace."""


@dataclass(frozen=True)
class SessionMetadata:
    """Non-sensitive session metadata (safe to log)."""

    connector_id: str
    environment: Environment
    created_at: datetime
    expires_at: datetime | None
    rotations: int


def _validate_connector_id(connector_id: str) -> str:
    if (
        not connector_id
        or connector_id in {".", ".."}
        or any(ch in connector_id for ch in "/\\:")
        or connector_id.startswith("~")
    ):
        raise SessionPathError(f"unsafe connector id: {connector_id!r}")
    return connector_id


class RuntimeSessionStore:
    """Per-connector encrypted session blobs for ONE environment."""

    def __init__(self, root: Path, environment: Environment, key: SecretValue) -> None:
        self._environment = environment
        self._dir = Path(root) / environment.value
        try:
            self._fernet = Fernet(key.reveal().encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise CorruptSessionError("invalid session encryption key") from error

    def _path_for(self, connector_id: str) -> Path:
        return self._dir / f"{_validate_connector_id(connector_id)}{_SUFFIX}"

    def save(
        self,
        connector_id: str,
        session_blob: bytes,
        *,
        now: datetime,
        expires_at: datetime | None = None,
        rotations: int = 0,
    ) -> SessionMetadata:
        """Encrypt and store one connector's session; atomic replace."""
        if now.tzinfo is None or (expires_at is not None and expires_at.tzinfo is None):
            raise ValueError("timestamps must be timezone-aware")
        record = {
            "connector_id": connector_id,
            "environment": self._environment.value,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "rotations": rotations,
            "blob_hex": session_blob.hex(),
        }
        token = self._fernet.encrypt(json.dumps(record).encode("utf-8"))
        path = self._path_for(connector_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(token)
        tmp.replace(path)
        return SessionMetadata(
            connector_id=connector_id,
            environment=self._environment,
            created_at=now,
            expires_at=expires_at,
            rotations=rotations,
        )

    def load(self, connector_id: str, *, now: datetime) -> tuple[SessionMetadata, bytes]:
        """Decrypt one connector's session; fail closed on any problem."""
        path = self._path_for(connector_id)
        if not path.is_file():
            raise MissingSessionError(
                f"no session for connector {connector_id!r} in "
                f"{self._environment.value}; re-authentication required"
            )
        try:
            payload = self._fernet.decrypt(path.read_bytes())
            record = json.loads(payload.decode("utf-8"))
        except (InvalidToken, ValueError) as error:
            raise CorruptSessionError(
                f"session for connector {connector_id!r} failed decryption; "
                "isolate and re-authenticate"
            ) from error
        if record["environment"] != self._environment.value:
            raise CorruptSessionError("session belongs to a different environment")
        expires_raw = record["expires_at"]
        expires_at = datetime.fromisoformat(expires_raw) if expires_raw else None
        if expires_at is not None and now >= expires_at:
            raise ExpiredSessionError(
                f"session for connector {connector_id!r} expired; "
                "re-authentication required"
            )
        metadata = SessionMetadata(
            connector_id=record["connector_id"],
            environment=self._environment,
            created_at=datetime.fromisoformat(record["created_at"]),
            expires_at=expires_at,
            rotations=int(record["rotations"]),
        )
        return metadata, bytes.fromhex(record["blob_hex"])

    def rotate(
        self,
        connector_id: str,
        new_blob: bytes,
        *,
        now: datetime,
        expires_at: datetime | None = None,
    ) -> SessionMetadata:
        """Replace one connector's session after re-authentication."""
        rotations = 0
        try:
            metadata, _ = self.load(connector_id, now=now)
            rotations = metadata.rotations + 1
        except SessionError:
            rotations = 1  # rotating a broken/expired session is a re-auth
        return self.save(
            connector_id, new_blob, now=now, expires_at=expires_at, rotations=rotations
        )

    def revoke(self, connector_id: str) -> bool:
        """Delete ONE connector's session; every other session is untouched."""
        path = self._path_for(connector_id)
        if path.is_file():
            path.unlink()
            return True
        return False

    def list_connectors(self) -> list[str]:
        """Connector ids with a stored session in this environment."""
        if not self._dir.is_dir():
            return []
        return sorted(p.name.removesuffix(_SUFFIX) for p in self._dir.glob(f"*{_SUFFIX}"))


def utc_now() -> datetime:
    return datetime.now(UTC)
