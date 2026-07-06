"""Deterministic, fail-closed versioned config loader (IMP-0105).

Design (Decision_Contract_v0.8 section 7, Data_Model section 15):

- Versioned config files are immutable: config/<dir>/<config_id>/<version>.json.
- The active version is selected by a small pointer, separate from the config
  content, per environment: config/active/<ENVIRONMENT>/<TYPE>/<config_id>.json.
- Rollback is a change of the pointer's `version` to an existing version — no
  build, no code change, no regeneration.
- There is NO silent fallback: a missing/invalid pointer, a missing target
  version, a schema error, an environment mismatch, a broken reference or a
  hash mismatch each raises. Startup is expected to fail on any of these.

Pure standard library + Pydantic; JSON only (safe parser, no YAML tags or
anchors); atomic pointer writes via os.replace (Windows and Linux ARM64).
"""

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError
from trading_contracts.enums import Environment

from trading_config.models import (
    CONFIG_DIR_BY_TYPE,
    ActivePointer,
    VersionedConfig,
    _ConfigBase,
)

_CONFIG_ADAPTER: Final[TypeAdapter[VersionedConfig]] = TypeAdapter(VersionedConfig)
_POINTER_ADAPTER: Final[TypeAdapter[ActivePointer]] = TypeAdapter(ActivePointer)

ACTIVE_DIR: Final[str] = "active"


class ConfigError(Exception):
    """Base class for every config-loading failure."""


class PathSecurityError(ConfigError):
    """A config identifier or version is unsafe or escapes the config root."""


class ConfigNotFoundError(ConfigError):
    """A requested versioned config file does not exist."""


class ActivePointerError(ConfigError):
    """An active pointer is missing, malformed or inconsistent."""


class ConfigValidationError(ConfigError):
    """A config file is malformed or fails schema/reference validation."""


class EnvironmentMismatchError(ConfigError):
    """A pointer's environment is not permitted by the target config."""


class ConfigIntegrityError(ConfigError):
    """A pointer's content hash does not match the target config file."""


def _validate_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."}:
        raise PathSecurityError(f"unsafe {label}: {value!r}")
    if "/" in value or "\\" in value or "\x00" in value or ":" in value:
        raise PathSecurityError(f"unsafe {label}: {value!r}")
    if os.path.isabs(value) or value.startswith("~"):
        raise PathSecurityError(f"unsafe {label}: {value!r}")
    return value


def _contained(root: Path, candidate: Path) -> Path:
    """Return the resolved path, ensuring it stays within ``root``."""
    root_resolved = root.resolve()
    resolved = candidate.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise PathSecurityError(f"path escapes config root: {candidate}")
    return resolved


class ConfigLoader:
    """Loads and validates versioned configs and their active pointers."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _version_path(self, config_type: str, config_id: str, version: str) -> Path:
        directory = CONFIG_DIR_BY_TYPE[config_type]
        _validate_segment(config_id, "config_id")
        _validate_segment(version, "version")
        candidate = self._root / directory / config_id / f"{version}.json"
        return _contained(self._root, candidate)

    def _pointer_path(self, config_type: str, config_id: str, environment: Environment) -> Path:
        _validate_segment(config_id, "config_id")
        candidate = (
            self._root / ACTIVE_DIR / environment.value / config_type / f"{config_id}.json"
        )
        return _contained(self._root, candidate)

    def load_config_version(
        self, config_type: str, config_id: str, version: str
    ) -> _ConfigBase:
        """Load one immutable versioned config; never falls back."""
        if config_type not in CONFIG_DIR_BY_TYPE:
            raise ConfigValidationError(f"unknown config_type: {config_type!r}")
        path = self._version_path(config_type, config_id, version)
        if not path.is_file():
            raise ConfigNotFoundError(
                f"{config_type} {config_id} version {version} not found at {path}"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConfigValidationError(f"malformed config JSON at {path}: {error.msg}") from error
        try:
            config = _CONFIG_ADAPTER.validate_python(raw)
        except ValidationError as error:
            raise ConfigValidationError(f"invalid config at {path}: {error.errors()}") from error
        if config.config_type != config_type:
            raise ConfigValidationError(
                f"config_type mismatch at {path}: file says "
                f"{config.config_type}, path says {config_type}"
            )
        if config.config_id != config_id or config.version != version:
            raise ConfigValidationError(
                f"identity mismatch at {path}: file is {config.config_id}/{config.version}"
            )
        return config

    def resolve_pointer(
        self, config_type: str, config_id: str, environment: Environment
    ) -> ActivePointer:
        """Resolve the active pointer for one config in one environment."""
        if config_type not in CONFIG_DIR_BY_TYPE:
            raise ActivePointerError(f"unknown config_type: {config_type!r}")
        path = self._pointer_path(config_type, config_id, environment)
        if not path.is_file():
            raise ActivePointerError(
                f"missing active pointer for {config_type} {config_id} in {environment.value}"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ActivePointerError(f"malformed pointer at {path}: {error.msg}") from error
        try:
            pointer = _POINTER_ADAPTER.validate_python(raw)
        except ValidationError as error:
            raise ActivePointerError(f"invalid pointer at {path}: {error.errors()}") from error
        if pointer.config_type != config_type:
            raise ActivePointerError(
                f"pointer config_type mismatch at {path}: {pointer.config_type} != {config_type}"
            )
        if pointer.config_id != config_id or pointer.environment != environment:
            raise ActivePointerError(f"pointer identity mismatch at {path}")
        return pointer

    def load_active(
        self, config_type: str, config_id: str, environment: Environment
    ) -> _ConfigBase:
        """Resolve the pointer and load the active version, fully validated."""
        pointer = self.resolve_pointer(config_type, config_id, environment)
        config = self.load_config_version(config_type, config_id, pointer.version)
        if environment not in config.resolved_environments:
            raise EnvironmentMismatchError(
                f"{config_type} {config_id} v{pointer.version} does not permit "
                f"environment {environment.value} (Paper/Live isolation)"
            )
        if pointer.content_hash is not None:
            actual = content_hash(self._version_path(config_type, config_id, pointer.version))
            if actual != pointer.content_hash:
                raise ConfigIntegrityError(
                    f"hash mismatch for {config_type} {config_id} v{pointer.version}"
                )
        return config

    def iter_pointer_files(self, environment: Environment) -> list[tuple[str, str]]:
        """List (config_type, config_id) with a pointer in one environment."""
        found: list[tuple[str, str]] = []
        env_dir = self._root / ACTIVE_DIR / environment.value
        if not env_dir.is_dir():
            return found
        for type_dir in sorted(env_dir.iterdir()):
            if type_dir.is_dir() and type_dir.name in CONFIG_DIR_BY_TYPE:
                for pointer_file in sorted(type_dir.glob("*.json")):
                    found.append((type_dir.name, pointer_file.stem))
        return found

    def load_all_active(self, environment: Environment) -> dict[tuple[str, str], _ConfigBase]:
        """Load every active config for one environment; raise on any failure."""
        result: dict[tuple[str, str], _ConfigBase] = {}
        for config_type, config_id in self.iter_pointer_files(environment):
            result[(config_type, config_id)] = self.load_active(
                config_type, config_id, environment
            )
        return result

    def validate_active(
        self, environments: Iterable[Environment] | None = None
    ) -> list[str]:
        """Return a list of startup problems; empty means all active configs load."""
        problems: list[str] = []
        targets = list(environments) if environments is not None else list(Environment)
        for environment in targets:
            for config_type, config_id in self.iter_pointer_files(environment):
                try:
                    self.load_active(config_type, config_id, environment)
                except ConfigError as error:
                    problems.append(
                        f"{environment.value}/{config_type}/{config_id}: "
                        f"{type(error).__name__}: {error}"
                    )
        return problems


def content_hash(path: Path) -> str:
    """Deterministic sha256 reference of a config file (CRLF normalized)."""
    import hashlib

    data = path.read_bytes().replace(b"\r\n", b"\n")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_active_pointer(root: Path, pointer: ActivePointer) -> Path:
    """Atomically write (or repoint) an active pointer.

    Used by rollback and promotion: write to a temp file in the same directory
    then os.replace, so a reader never observes a partial pointer file. Works
    identically on Windows and Linux.
    """
    _validate_segment(pointer.config_id, "config_id")
    directory = root / ACTIVE_DIR / pointer.environment.value / pointer.config_type
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{pointer.config_id}.json"
    payload = pointer.model_dump_json(indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=directory, delete=False, suffix=".tmp"
    )
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)
    return target
