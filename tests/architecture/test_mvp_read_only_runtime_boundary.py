"""Structural guardrail for the physically evaluate-only M7 runtime."""

from __future__ import annotations

from pathlib import Path

from scripts.check_architecture import READ_ONLY_RUNTIME_PATH, scan_repository

ROOT = Path(__file__).resolve().parents[2]


def test_canonical_runtime_has_no_read_only_boundary_violation() -> None:
    violations = [
        violation
        for violation in scan_repository(ROOT)
        if violation.path == READ_ONLY_RUNTIME_PATH
    ]
    assert violations == []


def test_runtime_source_has_no_automated_or_broker_authority_surface() -> None:
    source = (ROOT / READ_ONLY_RUNTIME_PATH).read_text(encoding="utf-8")
    for forbidden in (
        "submit_order(",
        "SubmitAuthorization(",
        "ExecutablePlan(",
        "PAPER_AUTO",
        "Environment.LIVE",
        ".evaluate(",
    ):
        assert forbidden not in source
    assert source.count(".evaluate_read_only(") == 1
