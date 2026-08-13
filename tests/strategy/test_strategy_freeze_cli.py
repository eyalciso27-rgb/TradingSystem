"""Track A / A1 — strategy-freeze CLI: validate / fingerprint / freeze-readiness."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from trading_application.strategy_freeze import MANDATORY_OWNER_PARAMETERS
from trading_application.strategy_freeze_cli import main

SID = "baseline-trend-breakout-v1"


def _draft_doc(*, param_keys: list[str], with_manifest: bool = True) -> dict[str, object]:
    doc: dict[str, object] = {
        "strategy_id": SID,
        "version_id": "v1",
        "status": "DRAFT",
        "parameters": [
            {
                "key": key,
                "value": "owner-decided",
                "source": "owner:test",
                "supplied_at": "2026-08-13T12:00:00+00:00",
            }
            for key in param_keys
        ],
    }
    if with_manifest:
        doc["fusion_manifest"] = {
            "version": "v1",
            "entries": [
                {
                    "assessment_key": "technical.breakout",
                    "role": "REQUIRED",
                    "required_status": "TRIGGERED",
                }
            ],
        }
    return doc


def _write(tmp_path: Path, doc: dict[str, object]) -> Path:
    path = tmp_path / "strategy.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_validate_ok_on_structural_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, _draft_doc(param_keys=list(MANDATORY_OWNER_PARAMETERS[:2])))
    assert main(["prog", "validate", str(path)]) == 0
    assert "OK" in capsys.readouterr().out


def test_freeze_readiness_lists_missing_keys_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, _draft_doc(param_keys=list(MANDATORY_OWNER_PARAMETERS[:3])))
    assert main(["prog", "freeze-readiness", str(path)]) == 1
    out = capsys.readouterr().out
    assert "NOT_READY" in out
    # a missing mandatory key is named, never filled
    assert MANDATORY_OWNER_PARAMETERS[-1] in out


def test_freeze_readiness_ready_when_all_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, _draft_doc(param_keys=list(MANDATORY_OWNER_PARAMETERS)))
    assert main(["prog", "freeze-readiness", str(path)]) == 0
    assert "READY" in capsys.readouterr().out


def test_fingerprint_is_stable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, _draft_doc(param_keys=list(MANDATORY_OWNER_PARAMETERS)))
    assert main(["prog", "fingerprint", str(path)]) == 0
    first = capsys.readouterr().out.strip()
    assert main(["prog", "fingerprint", str(path)]) == 0
    assert capsys.readouterr().out.strip() == first
    assert first.startswith("sha256:")


def test_invalid_file_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("status: NONSENSE\nstrategy_id: x\nversion_id: v1\n", encoding="utf-8")
    assert main(["prog", "validate", str(path)]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_missing_file_returns_usage_error(tmp_path: Path) -> None:
    assert main(["prog", "validate", str(tmp_path / "nope.yaml")]) == 2
