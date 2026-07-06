"""Tests for the startup validation entry point (IMP-0105)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trading_config import validate_active
from trading_config.loader import write_active_pointer
from trading_config.models import ActivePointer

NOW = "2026-07-05T00:00:00Z"
WriteVersion = Callable[[str, str, str, dict[str, Any]], Path]
Policy = Callable[..., dict[str, Any]]


def test_cli_ok_on_valid_active_config(
    config_root: Path, write_version: WriteVersion, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    write_active_pointer(
        config_root,
        ActivePointer.model_validate(
            {
                "config_type": "POLICY",
                "config_id": "risk.core",
                "version": "0.1.0",
                "environment": "PAPER",
                "activated_at": NOW,
                "activated_by": "owner",
                "reason": "test",
            }
        ),
    )
    exit_code = validate_active.main(["prog", "--root", str(config_root)])
    assert exit_code == 0


def test_cli_fails_on_invalid_active_config(
    config_root: Path, policy_payload: Policy
) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    write_active_pointer(
        config_root,
        ActivePointer.model_validate(
            {
                "config_type": "POLICY",
                "config_id": "risk.core",
                "version": "0.1.0",
                "environment": "PAPER",
                "activated_at": NOW,
                "activated_by": "owner",
                "reason": "test",
            }
        ),
    )
    # Pointer exists but the config version file does not -> non-zero exit.
    exit_code = validate_active.main(["prog", "--root", str(config_root)])
    assert exit_code == 1


def test_cli_missing_root(tmp_path: Path) -> None:
    exit_code = validate_active.main(["prog", "--root", str(tmp_path / "nope")])
    assert exit_code == 2


def test_cli_ok_on_empty_active(config_root: Path) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    exit_code = validate_active.main(["prog", "--root", str(config_root)])
    assert exit_code == 0


def test_malformed_pointer_file_reported(
    config_root: Path, write_version: WriteVersion, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    pointer_file = config_root / "active" / "PAPER" / "POLICY" / "risk.core.json"
    pointer_file.parent.mkdir(parents=True, exist_ok=True)
    pointer_file.write_text("{ broken", encoding="utf-8")
    exit_code = validate_active.main(["prog", "--root", str(config_root), "--environment", "PAPER"])
    assert exit_code == 1


def test_pointer_file_content_is_valid_json(
    config_root: Path, write_version: WriteVersion, policy_payload: Policy
) -> None:
    write_version("POLICY", "risk.core", "0.1.0", policy_payload())
    path = write_active_pointer(
        config_root,
        ActivePointer.model_validate(
            {
                "config_type": "POLICY",
                "config_id": "risk.core",
                "version": "0.1.0",
                "environment": "PAPER",
                "activated_at": NOW,
                "activated_by": "owner",
                "reason": "test",
            }
        ),
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "0.1.0"
    assert "value" not in data  # pointer never carries config content
