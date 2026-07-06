"""Fixtures for the versioned config loader tests (IMP-0105).

Every scenario runs on a temporary config root; no production config is
committed or required.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from trading_config.loader import ConfigLoader, content_hash, write_active_pointer
from trading_config.models import ActivePointer

NOW = "2026-07-05T00:00:00Z"


def _policy(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "config_type": "POLICY",
        "config_id": "risk.core",
        "version": "0.1.0",
        "schema_version": "1.0.0",
        "environments": ["PAPER_MANUAL", "PAPER_AUTO"],
        "enabled": True,
        "owner": "RISK_POLICY",
        "status": "ACTIVE",
        "valid_from": NOW,
        "policy_kind": "RISK",
        "parameter_refs": ["risk.per_trade_pct"],
    }
    base.update(over)
    return base


@pytest.fixture()
def config_root(tmp_path: Path) -> Path:
    return tmp_path / "config"


@pytest.fixture()
def write_version(config_root: Path) -> Callable[..., Path]:
    def _write(config_type: str, config_id: str, version: str, payload: dict[str, Any]) -> Path:
        directory = {
            "POLICY": "policies",
            "STRATEGY": "strategies",
            "PROVIDER": "providers",
            "SOURCE": "sources",
        }[config_type]
        path = config_root / directory / config_id / f"{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    return _write


@pytest.fixture()
def point_to(config_root: Path) -> Callable[..., ActivePointer]:
    def _point(
        config_type: str,
        config_id: str,
        version: str,
        environment: str,
        *,
        content_hash_value: str | None = None,
    ) -> ActivePointer:
        pointer = ActivePointer.model_validate(
            {
                "config_type": config_type,
                "config_id": config_id,
                "version": version,
                "environment": environment,
                "activated_at": NOW,
                "activated_by": "owner",
                "reason": "test activation",
                "content_hash": content_hash_value,
            }
        )
        write_active_pointer(config_root, pointer)
        return pointer

    return _point


@pytest.fixture()
def loader(config_root: Path) -> ConfigLoader:
    config_root.mkdir(parents=True, exist_ok=True)
    return ConfigLoader(config_root)


@pytest.fixture()
def policy_payload() -> Callable[..., dict[str, Any]]:
    return _policy


@pytest.fixture()
def hash_of() -> Callable[[Path], str]:
    return content_hash
