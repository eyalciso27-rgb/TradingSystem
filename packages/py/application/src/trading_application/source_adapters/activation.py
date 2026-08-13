"""Source activation readiness framework (Track C).

Takes the four merged adapters (Telegram user/TDLib, Telegram bot, news/filings,
TradingView) from "code implemented" to "the owner can activate without more
architecture work" — WITHOUT any real login here. Everything owner-specific is
loaded at run time from the environment's ``FileSecretStore`` (IMP-0302); the
machinery is proven with deterministic fakes only.

Shared primitives:

* ``SecretSpec`` — the fail-closed secret-key naming convention per connector.
* ``read_only_preflight`` — loads secrets (fail-closed if missing) and runs the
  source's READ-ONLY probe, producing a canonical ``IntegrationHealthStatus``.
* ``ActivationEvidence`` — the PoI evidence schema + sanitized JSON writer, with
  a hard rule: a **FAKE** client can only ever produce a ``NOT_EVIDENCE``
  verdict — a fixture can NEVER mark a PoI PASS. Only a REAL client with a
  HEALTHY probe yields PASS.
* ``CursorStore`` — durable checkpoint persistence with corruption handling and
  idempotent restart.
* ``assert_no_execution_surface`` — proves an adapter cannot reach a broker /
  execution / order surface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from trading_config.secrets import FileSecretStore, MissingSecretError, SecretValue
from trading_contracts.enums import Environment, IntegrationHealthStatus

from trading_application.source_adapters.framework import redact

ACTIVATION_VERSION = "source-activation-1.0.0"

#: Method/attribute names that would indicate an execution/order surface. An
#: adapter must expose NONE of these (read-only for trading input).
FORBIDDEN_EXECUTION_SURFACES: tuple[str, ...] = (
    "submit", "cancel", "execute", "place_order", "flatten", "authorize",
    "order_intent", "submit_authorization", "broker", "broker_port",
    "execution_agent", "send_order", "modify_order",
)


class ActivationError(Exception):
    """Fail-closed activation failure (missing secret, illegal verdict)."""


class ClientKind(StrEnum):
    """Whether a probe ran against a REAL provider or a deterministic FAKE."""

    REAL = "REAL"
    FAKE = "FAKE"


class PoIVerdict(StrEnum):
    """A PoI verdict. A FAKE probe can only ever be NOT_EVIDENCE."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVIDENCE = "NOT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class SecretSpec:
    """The required secret-key names for one connector (fail-closed)."""

    connector_id: str
    required_keys: tuple[str, ...]
    optional_keys: tuple[str, ...] = ()

    def load(self, store: FileSecretStore) -> dict[str, SecretValue]:
        """Load required secrets; fail closed listing any that are missing."""
        loaded: dict[str, SecretValue] = {}
        missing: list[str] = []
        for key in self.required_keys:
            try:
                loaded[key] = store.get(key)
            except MissingSecretError:
                missing.append(key)
        for key in self.optional_keys:
            try:
                loaded[key] = store.get(key)
            except MissingSecretError:
                continue
        if missing:
            raise ActivationError(
                f"{self.connector_id}: missing required secrets {missing} for"
                f" environment {store.environment.value} (no default, no fallback)"
            )
        return loaded


def secret_key(domain: str, connector_id: str, name: str) -> str:
    """Canonical secret-key naming: ``<domain>/<connector_id>/<name>``."""
    return f"{domain}/{connector_id}/{name}"


#: A read-only probe: given loaded secrets + the client kind, return a health
#: status and a short detail. It must NOT ingest trading data or mutate state.
PreflightProbe = Callable[[dict[str, SecretValue], ClientKind], tuple[IntegrationHealthStatus, str]]


@dataclass(frozen=True, slots=True)
class PreflightResult:
    connector_id: str
    client_kind: ClientKind
    status: IntegrationHealthStatus
    detail: str
    observed_at: datetime


def read_only_preflight(
    *,
    connector_id: str,
    spec: SecretSpec,
    store: FileSecretStore,
    probe: PreflightProbe,
    client_kind: ClientKind,
    observed_at: datetime,
) -> PreflightResult:
    """Load secrets fail-closed, then run the source's read-only probe."""
    if observed_at.tzinfo is None:
        raise ActivationError("observed_at must be timezone-aware")
    secrets = spec.load(store)  # fail-closed on any missing secret
    status, detail = probe(secrets, client_kind)
    # sanitize the detail against every secret value, defence in depth
    detail = redact(detail, [value.reveal() for value in secrets.values()])
    return PreflightResult(
        connector_id=connector_id,
        client_kind=client_kind,
        status=status,
        detail=detail,
        observed_at=observed_at,
    )


def _verdict_for(result: PreflightResult) -> PoIVerdict:
    # BINDING: a FAKE probe is never PoI evidence, whatever its status.
    if result.client_kind is ClientKind.FAKE:
        return PoIVerdict.NOT_EVIDENCE
    if result.status is IntegrationHealthStatus.HEALTHY:
        return PoIVerdict.PASS
    return PoIVerdict.FAIL


@dataclass(frozen=True, slots=True)
class ActivationEvidence:
    """Sanitized PoI evidence artifact for one activation preflight."""

    poi_id: str
    connector_id: str
    client_kind: ClientKind
    status: IntegrationHealthStatus
    verdict: PoIVerdict
    detail: str
    observed_at: datetime
    activation_version: str
    evidence_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "poi_id": self.poi_id,
            "connector_id": self.connector_id,
            "client_kind": self.client_kind.value,
            "status": self.status.value,
            "verdict": self.verdict.value,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat(),
            "activation_version": self.activation_version,
            "evidence_fingerprint": self.evidence_fingerprint,
        }

    def write(self, path: Path, *, secrets: Iterable[SecretValue] = ()) -> None:
        """Write the sanitized evidence JSON. Refuses if a secret would leak."""
        payload = json.dumps(self.to_dict(), sort_keys=True, indent=2)
        for secret in secrets:
            raw = secret.reveal()
            if raw and raw in payload:
                raise ActivationError("refusing to write evidence: secret value present")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")


def build_evidence(poi_id: str, result: PreflightResult) -> ActivationEvidence:
    """Build the PoI evidence artifact for a preflight result."""
    verdict = _verdict_for(result)
    material = {
        "poi_id": poi_id,
        "connector_id": result.connector_id,
        "client_kind": result.client_kind.value,
        "status": result.status.value,
        "verdict": verdict.value,
        "observed_at": result.observed_at.isoformat(),
        "activation_version": ACTIVATION_VERSION,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return ActivationEvidence(
        poi_id=poi_id,
        connector_id=result.connector_id,
        client_kind=result.client_kind,
        status=result.status,
        verdict=verdict,
        detail=result.detail,
        observed_at=result.observed_at,
        activation_version=ACTIVATION_VERSION,
        evidence_fingerprint=fingerprint,
    )


# -- durable checkpoint / resume ---------------------------------------------


@dataclass(frozen=True, slots=True)
class CursorStore:
    """A durable JSON cursor store with corruption handling (idempotent restart)."""

    path: Path

    def load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise ActivationError(f"corrupt cursor store {self.path}: {error}") from error
        if not isinstance(data, dict):
            raise ActivationError(f"corrupt cursor store {self.path}: not an object")
        return {str(k): str(v) for k, v in data.items()}

    def save(self, cursors: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(cursors, sort_keys=True, indent=2), encoding="utf-8")

    def advance(self, key: str, value: str) -> dict[str, str]:
        cursors = self.load()
        cursors[key] = value
        self.save(cursors)
        return cursors


# -- execution-surface proof -------------------------------------------------


def assert_no_execution_surface(adapter: object) -> None:
    """Fail closed if an adapter exposes any broker/execution/order surface."""
    for name in FORBIDDEN_EXECUTION_SURFACES:
        if hasattr(adapter, name):
            raise ActivationError(
                f"{type(adapter).__name__} exposes forbidden execution surface {name!r};"
                " external sources are read-only for trading input"
            )


def open_store(root: Path, environment: Environment) -> FileSecretStore:
    """Construct a FileSecretStore for one environment (helper for entrypoints)."""
    return FileSecretStore(root, environment)


__all__ = [
    "ACTIVATION_VERSION",
    "FORBIDDEN_EXECUTION_SURFACES",
    "ActivationError",
    "ActivationEvidence",
    "ClientKind",
    "CursorStore",
    "PoIVerdict",
    "PreflightProbe",
    "PreflightResult",
    "SecretSpec",
    "assert_no_execution_surface",
    "build_evidence",
    "open_store",
    "read_only_preflight",
    "secret_key",
]
