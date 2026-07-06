"""Static secret loading (IMP-0302).

Authority: Technology_and_Deployment_Decisions_v0.6 ADR-011 (SOPS + age,
runtime file secrets — no heavy vault in the MVP), Integration_Specification
v0.6 section 20.7 (SOPS/age is for STATIC secrets only) and
Security_and_Operations_Specification_v0.6 sections 5-6 (no secrets in
images/env dumps/logs; Paper and Live use fully separate secret stores).

Model: SOPS/age decryption happens at deployment time and produces runtime
secret FILES under a per-environment root::

    <root>/<ENVIRONMENT>/<name...>

The loader only reads those runtime files — it never shells out to sops/age,
never reads secrets from environment variables (env dumps must stay clean)
and never stores secrets in the database. Versioned config keeps carrying an
opaque ``secret_ref`` only (IMP-0105).

Fail-closed: a missing required secret raises ``MissingSecretError`` naming
the reference (never a value) with no default and no fallback — including no
fallback between PAPER and LIVE, whose stores are separate by construction.

Leak-safety: values are wrapped in ``SecretValue`` whose ``repr``/``str`` are
masked; the raw value is obtainable only via ``reveal()`` and is not
JSON-serializable by accident.
"""

from pathlib import Path
from typing import Final

from trading_contracts.enums import Environment

_MASK: Final[str] = "SecretValue('***')"


class SecretError(Exception):
    """Base class for secret loading failures (never contains a value)."""


class MissingSecretError(SecretError):
    """A required secret reference has no runtime file. Startup must fail."""


class SecretPathError(SecretError):
    """The secret name is unsafe or escapes the environment's secret root."""


class SecretValue:
    """A secret wrapper that never leaks through repr/str/format/JSON."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the raw secret. The only intentional way out."""
        return self._value

    def __repr__(self) -> str:
        return _MASK

    def __str__(self) -> str:
        return _MASK

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and other._value == self._value

    def __hash__(self) -> int:
        return hash((SecretValue, self._value))


def _validate_name(name: str) -> list[str]:
    segments = name.split("/")
    for segment in segments:
        if (
            not segment
            or segment in {".", ".."}
            or "\\" in segment
            or ":" in segment
            or segment.startswith("~")
        ):
            raise SecretPathError(f"unsafe secret name: {name!r}")
    return segments


class FileSecretStore:
    """Reads decrypted runtime secret files for ONE environment.

    The store is bound to a single environment at construction; PAPER and
    LIVE stores are physically separate directories and there is no
    cross-environment lookup or fallback.
    """

    def __init__(self, root: Path, environment: Environment) -> None:
        self._environment = environment
        self._env_root = Path(root) / environment.value

    @property
    def environment(self) -> Environment:
        return self._environment

    def _path_for(self, name: str) -> Path:
        candidate = self._env_root.joinpath(*_validate_name(name))
        env_root = self._env_root.resolve()
        resolved = candidate.resolve()
        if env_root != resolved and env_root not in resolved.parents:
            raise SecretPathError(f"secret path escapes the {self._environment.value} store")
        return resolved

    def get(self, name: str) -> SecretValue:
        """Load one secret; fail closed if the runtime file is absent."""
        path = self._path_for(name)
        if not path.is_file():
            raise MissingSecretError(
                f"missing required secret {name!r} for environment "
                f"{self._environment.value} (no default, no fallback)"
            )
        return SecretValue(path.read_text(encoding="utf-8").strip())

    def require_all(self, names: list[str]) -> dict[str, SecretValue]:
        """Load every required secret or raise listing ALL missing refs.

        Intended for startup: any missing secret aborts before the
        application continues with partial configuration.
        """
        missing: list[str] = []
        loaded: dict[str, SecretValue] = {}
        for name in names:
            try:
                loaded[name] = self.get(name)
            except MissingSecretError:
                missing.append(name)
        if missing:
            raise MissingSecretError(
                f"missing required secrets for {self._environment.value}: {missing}"
            )
        return loaded
