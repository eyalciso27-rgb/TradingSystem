"""Positive and negative tests for the versioned config loader (IMP-0105).

Done criteria: invalid config fails startup; rollback changes the pointer
without a build.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from trading_config.loader import (
    ActivePointerError,
    ConfigIntegrityError,
    ConfigLoader,
    ConfigNotFoundError,
    ConfigValidationError,
    EnvironmentMismatchError,
    PathSecurityError,
    write_active_pointer,
)
from trading_config.models import (
    ActivePointer,
    PolicyConfig,
    ProviderConfig,
    SourceConfig,
    StrategyConfig,
)
from trading_contracts.enums import Environment, SocialAccessMode, SourceType

NOW = "2026-07-05T00:00:00Z"
WriteVersion = Callable[[str, str, str, dict[str, Any]], Path]
PointTo = Callable[..., ActivePointer]
Policy = Callable[..., dict[str, Any]]


def _strategy(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "config_type": "STRATEGY",
        "config_id": "breakout.v1",
        "version": "0.1.0",
        "schema_version": "1.0.0",
        "environments": ["SHADOW", "PAPER_MANUAL"],
        "enabled": True,
        "owner": "STRATEGY",
        "status": "ACTIVE",
        "valid_from": NOW,
        "strategy_id": "breakout",
        "strategy_version": "1.0.0",
        "allowed_operating_modes": ["PAPER_MANUAL"],
        "assessment_requirement_refs": ["technical.breakout"],
    }
    base.update(over)
    return base


def _provider(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "config_type": "PROVIDER",
        "config_id": "marketdata.primary",
        "version": "0.1.0",
        "schema_version": "1.0.0",
        "environments": ["PAPER_MANUAL"],
        "enabled": True,
        "owner": "PLATFORM",
        "status": "ACTIVE",
        "valid_from": NOW,
        "provider_id": "primary",
        "provider_kind": "MARKET_DATA",
        "secret_ref": "sops://age/marketdata",
    }
    base.update(over)
    return base


def _source(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "config_type": "SOURCE",
        "config_id": "telegram.main",
        "version": "0.1.0",
        "schema_version": "1.0.0",
        "environments": ["PAPER_MANUAL"],
        "enabled": True,
        "owner": "PLATFORM",
        "status": "ACTIVE",
        "valid_from": NOW,
        "source_id": "telegram-main",
        "source_type": SourceType.TELEGRAM.value,
        "access_mode": SocialAccessMode.TELEGRAM_USER_TDLIB.value,
        "secret_ref": "sops://age/telegram",
    }
    base.update(over)
    return base


# --- Positive ---

def test_load_active_policy(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    config = loader.load_active("POLICY", "risk.core", Environment.PAPER)
    assert isinstance(config, PolicyConfig)
    assert config.version == "0.1.0"
    assert config.policy_kind == "RISK"


def test_load_active_strategy(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo
) -> None:
    write_version("STRATEGY", "breakout.v1", "0.1.0", _strategy())
    point_to("STRATEGY", "breakout.v1", "0.1.0", "PAPER")
    config = loader.load_active("STRATEGY", "breakout.v1", Environment.PAPER)
    assert isinstance(config, StrategyConfig)
    assert config.strategy_id == "breakout"


def test_load_active_provider_and_source(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo
) -> None:
    write_version("PROVIDER", "marketdata.primary", "0.1.0", _provider())
    point_to("PROVIDER", "marketdata.primary", "0.1.0", "PAPER")
    provider = loader.load_active("PROVIDER", "marketdata.primary", Environment.PAPER)
    assert isinstance(provider, ProviderConfig)

    write_version("SOURCE", "telegram.main", "0.1.0", _source())
    point_to("SOURCE", "telegram.main", "0.1.0", "PAPER")
    source = loader.load_active("SOURCE", "telegram.main", Environment.PAPER)
    assert isinstance(source, SourceConfig)
    assert source.source_type is SourceType.TELEGRAM


def test_resolve_pointer_exposes_active_version(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.2.0", policy_payload(version="0.2.0"))
    point_to("POLICY", "risk.core", "0.2.0", "PAPER")
    pointer = loader.resolve_pointer("POLICY", "risk.core", Environment.PAPER)
    assert pointer.version == "0.2.0"


def test_load_all_active_and_validate(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    write_version("STRATEGY", "breakout.v1", "0.1.0", _strategy())
    point_to("STRATEGY", "breakout.v1", "0.1.0", "PAPER")
    active = loader.load_all_active(Environment.PAPER)
    assert set(active) == {("POLICY", "risk.core"), ("STRATEGY", "breakout.v1")}
    assert loader.validate_active([Environment.PAPER]) == []


def test_load_is_deterministic_and_immutable(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    first = loader.load_active("POLICY", "risk.core", Environment.PAPER)
    second = loader.load_active("POLICY", "risk.core", Environment.PAPER)
    assert first == second
    with pytest.raises(ValidationError):
        first.enabled = False


def test_integrity_hash_accepted_when_matching(
    loader: ConfigLoader,
    write_version: WriteVersion,
    point_to: PointTo,
    policy_payload: Policy,
    hash_of: Callable[[Path], str],
) -> None:
    path = write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    point_to("POLICY", "risk.core", "0.1.0", "PAPER", content_hash_value=hash_of(path))
    assert loader.load_active("POLICY", "risk.core", Environment.PAPER).version == "0.1.0"


def test_paper_and_live_use_separate_pointers(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload(environments=["PAPER_MANUAL"]))
    write_version(
        "POLICY",
        "risk.core",
        "0.2.0",
        policy_payload(version="0.2.0", environments=["LIVE_MANUAL"]),
    )
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    point_to("POLICY", "risk.core", "0.2.0", "LIVE")
    assert loader.load_active("POLICY", "risk.core", Environment.PAPER).version == "0.1.0"
    assert loader.load_active("POLICY", "risk.core", Environment.LIVE).version == "0.2.0"


# --- Rollback (done criterion) ---

def test_rollback_changes_pointer_without_rebuild(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload(version="0.1.0"))
    write_version("POLICY", "risk.core", "0.2.0", policy_payload(version="0.2.0"))
    point_to("POLICY", "risk.core", "0.2.0", "PAPER")
    assert loader.load_active("POLICY", "risk.core", Environment.PAPER).version == "0.2.0"

    # Rollback: repoint to the previous, existing version. No code/build change.
    rolled_back = ActivePointer.model_validate(
        {
            "config_type": "POLICY",
            "config_id": "risk.core",
            "version": "0.1.0",
            "environment": "PAPER",
            "activated_at": NOW,
            "activated_by": "owner",
            "reason": "rollback",
        }
    )
    write_active_pointer(loader.root, rolled_back)
    assert loader.load_active("POLICY", "risk.core", Environment.PAPER).version == "0.1.0"


# --- Negative ---

def test_missing_config_file_fails(
    loader: ConfigLoader, point_to: PointTo
) -> None:
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    with pytest.raises(ConfigNotFoundError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_missing_pointer_fails(loader: ConfigLoader) -> None:
    with pytest.raises(ActivePointerError):
        loader.resolve_pointer("POLICY", "risk.core", Environment.PAPER)


def test_pointer_to_missing_version_fails_without_fallback(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    point_to("POLICY", "risk.core", "9.9.9", "PAPER")  # points at a non-existent version
    with pytest.raises(ConfigNotFoundError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_malformed_json_fails(
    loader: ConfigLoader, config_root: Path, point_to: PointTo
) -> None:
    path = config_root / "policies" / "risk.core" / "0.1.0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json ", encoding="utf-8")
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    with pytest.raises(ConfigValidationError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_unknown_field_fails(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload(surprise="x"))
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    with pytest.raises(ConfigValidationError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_missing_required_field_fails(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    payload = policy_payload()
    del payload["owner"]
    write_version("POLICY", "risk.core", "0.1.0", payload)
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    with pytest.raises(ConfigValidationError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_invalid_version_format_fails(
    loader: ConfigLoader, config_root: Path, point_to: PointTo, policy_payload: Policy
) -> None:
    path = config_root / "policies" / "risk.core" / "0.1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"x": 1}', encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        loader.load_config_version("POLICY", "risk.core", "0.1")  # not SemVer -> parse fails


def test_invalid_enum_fails(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload(environments=["NOT_A_MODE"]))
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    with pytest.raises(ConfigValidationError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_pointer_wrong_config_type_fails(
    loader: ConfigLoader, config_root: Path, write_version: WriteVersion, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    # A pointer file whose declared config_type disagrees with its location.
    path = config_root / "active" / "PAPER" / "POLICY" / "risk.core.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"config_type": "STRATEGY", "config_id": "risk.core", "version": "0.1.0",'
        ' "environment": "PAPER", "activated_at": "2026-07-05T00:00:00Z",'
        ' "activated_by": "o", "reason": "r"}',
        encoding="utf-8",
    )
    with pytest.raises(ActivePointerError):
        loader.resolve_pointer("POLICY", "risk.core", Environment.PAPER)


def test_environment_mismatch_paper_pointer_live_only_config(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload(environments=["LIVE_MANUAL"]))
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    with pytest.raises(EnvironmentMismatchError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_environment_mismatch_live_pointer_paper_only_config(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload(environments=["PAPER_MANUAL"]))
    point_to("POLICY", "risk.core", "0.1.0", "LIVE")
    with pytest.raises(EnvironmentMismatchError):
        loader.load_active("POLICY", "risk.core", Environment.LIVE)


def test_hash_mismatch_fails(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    point_to("POLICY", "risk.core", "0.1.0", "PAPER", content_hash_value="sha256:" + "0" * 64)
    with pytest.raises(ConfigIntegrityError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


@pytest.mark.parametrize("bad_id", ["..", "../evil", "a/b", "a\\b", "/abs", "with:colon"])
def test_path_traversal_rejected(loader: ConfigLoader, bad_id: str) -> None:
    with pytest.raises(PathSecurityError):
        loader.load_config_version("POLICY", bad_id, "0.1.0")


@pytest.mark.parametrize("bad_version", ["..", "../0.1.0", "0.1.0/x"])
def test_path_traversal_in_version_rejected(loader: ConfigLoader, bad_version: str) -> None:
    with pytest.raises(PathSecurityError):
        loader.load_config_version("POLICY", "risk.core", bad_version)


@pytest.mark.parametrize(
    "secret_field", ["password", "token", "cookie", "private_key", "api_key", "session"]
)
def test_inline_secret_fields_rejected(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, secret_field: str
) -> None:
    payload = _provider()
    payload[secret_field] = "super-secret-value"
    write_version("PROVIDER", "marketdata.primary", "0.1.0", payload)
    point_to("PROVIDER", "marketdata.primary", "0.1.0", "PAPER")
    with pytest.raises(ConfigValidationError):
        loader.load_active("PROVIDER", "marketdata.primary", Environment.PAPER)


def test_secret_ref_with_inline_secret_value_rejected(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo
) -> None:
    payload = _provider(secret_ref="-----BEGIN PRIVATE KEY-----")
    write_version("PROVIDER", "marketdata.primary", "0.1.0", payload)
    point_to("PROVIDER", "marketdata.primary", "0.1.0", "PAPER")
    with pytest.raises(ConfigValidationError):
        loader.load_active("PROVIDER", "marketdata.primary", Environment.PAPER)


def test_no_fallback_when_active_version_invalid(
    loader: ConfigLoader, write_version: WriteVersion, point_to: PointTo, policy_payload: Policy
) -> None:
    # A good previous version exists, but the active one is broken: no fallback.
    write_version("POLICY", "risk.core", "0.1.0", policy_payload(version="0.1.0"))
    write_version("POLICY", "risk.core", "0.2.0", policy_payload(version="0.2.0", owner=""))
    point_to("POLICY", "risk.core", "0.2.0", "PAPER")
    with pytest.raises(ConfigValidationError):
        loader.load_active("POLICY", "risk.core", Environment.PAPER)


def test_validate_active_reports_problem(
    loader: ConfigLoader, point_to: PointTo
) -> None:
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")  # pointer with no config file
    problems = loader.validate_active([Environment.PAPER])
    assert len(problems) == 1
    assert "ConfigNotFoundError" in problems[0]


def test_atomic_pointer_write_leaves_no_partial(
    loader: ConfigLoader, config_root: Path, point_to: PointTo, write_version: WriteVersion,
    policy_payload: Policy,
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    point_to("POLICY", "risk.core", "0.1.0", "PAPER")
    active_dir = config_root / "active" / "PAPER" / "POLICY"
    # Exactly one committed pointer file, no leftover temp files.
    files = sorted(p.name for p in active_dir.iterdir())
    assert files == ["risk.core.json"]
