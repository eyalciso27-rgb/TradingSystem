"""Tests for the build metadata record (IMP-0003)."""

import re
import shutil
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_build_record_ties_versions_and_hashes(
    build_metadata: ModuleType, verify_baseline: ModuleType
) -> None:
    record = build_metadata.build_record(REPO_ROOT)
    assert record["record_version"] == "1"
    assert record["repository_version"] == "0.1.0"
    assert record["baseline"]["pack_version"] == "0.5"
    assert record["baseline"]["manifest_path"] == "docs/baseline-v0.5/BASELINE_MANIFEST.json"
    assert re.fullmatch(r"[0-9a-f]{64}", record["baseline"]["manifest_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", record["baseline"]["tree_hash"])
    assert record["baseline_verification"] == "PASS"
    assert record["baseline_problems"] == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")
def test_git_commit_sha_in_real_repository(build_metadata: ModuleType) -> None:
    sha = build_metadata.git_commit_sha(REPO_ROOT)
    assert re.fullmatch(r"[0-9a-f]{40}", sha)


def test_git_commit_is_unknown_outside_git(
    build_metadata: ModuleType, fake_repo: Path
) -> None:
    assert build_metadata.git_commit_sha(fake_repo) == "unknown"


def test_build_record_on_fake_repo_passes_verification(
    build_metadata: ModuleType, fake_repo: Path
) -> None:
    record = build_metadata.build_record(fake_repo)
    assert record["repository_version"] == "9.9.9"
    assert record["git_commit"] == "unknown"
    assert record["baseline_verification"] == "PASS"


def test_build_record_reports_fail_on_tampering(
    build_metadata: ModuleType, fake_repo: Path
) -> None:
    target = fake_repo / "docs" / "baseline-v0.5" / "POI_BACKLOG.yaml"
    target.write_text("tampered\n", encoding="utf-8")
    record = build_metadata.build_record(fake_repo)
    assert record["baseline_verification"] == "FAIL"
    assert record["baseline_problems"] != []


def test_build_record_is_deterministic(build_metadata: ModuleType) -> None:
    first = build_metadata.build_record(REPO_ROOT)
    second = build_metadata.build_record(REPO_ROOT)
    assert first == second
