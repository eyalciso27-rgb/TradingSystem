"""Positive and negative tests for baseline verification (IMP-0003).

Negative scenarios run only on temporary fixture repositories; the canonical
baseline documents are never modified.
"""

import json
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _baseline_dir(repo_root: Path) -> Path:
    return repo_root / "docs" / "baseline-v0.5"


def _edit_lock(verify_baseline: ModuleType, repo_root: Path, key: str, value: object) -> None:
    lock_path = _baseline_dir(repo_root) / verify_baseline.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock[key] = value
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")


def test_valid_fake_baseline_passes(verify_baseline: ModuleType, fake_repo: Path) -> None:
    assert verify_baseline.verify(fake_repo) == []


def test_tampered_document_fails(verify_baseline: ModuleType, fake_repo: Path) -> None:
    target = _baseline_dir(fake_repo) / "CLAUDE_CODE_TASKS.yaml"
    target.write_text("tampered without lock update\n", encoding="utf-8")
    problems = verify_baseline.verify(fake_repo)
    assert any(problem.startswith("hash-mismatch:") for problem in problems)


def test_missing_document_fails(verify_baseline: ModuleType, fake_repo: Path) -> None:
    (_baseline_dir(fake_repo) / "POI_BACKLOG.yaml").unlink()
    problems = verify_baseline.verify(fake_repo)
    assert any(problem.startswith("missing-file:") for problem in problems)


def test_undocumented_extra_file_fails(verify_baseline: ModuleType, fake_repo: Path) -> None:
    (_baseline_dir(fake_repo) / "sneaky_new_doc.md").write_text("new\n", encoding="utf-8")
    problems = verify_baseline.verify(fake_repo)
    assert any(problem.startswith("undocumented-file:") for problem in problems)


def test_path_escaping_baseline_root_fails(
    verify_baseline: ModuleType, fake_repo: Path
) -> None:
    lock_path = _baseline_dir(fake_repo) / verify_baseline.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["files"].append({"path": "../../evil.md", "sha256": "0" * 64})
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    problems = verify_baseline.verify(fake_repo)
    assert any(problem.startswith("unsafe-path:") for problem in problems)


def test_duplicate_lock_entry_fails(verify_baseline: ModuleType, fake_repo: Path) -> None:
    lock_path = _baseline_dir(fake_repo) / verify_baseline.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["files"].append(dict(lock["files"][0]))
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    problems = verify_baseline.verify(fake_repo)
    assert any(problem.startswith("duplicate-path:") for problem in problems)


def test_wrong_pack_version_fails(verify_baseline: ModuleType, fake_repo: Path) -> None:
    manifest_path = _baseline_dir(fake_repo) / verify_baseline.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pack_version"] = "0.6"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    problems = verify_baseline.verify(fake_repo)
    assert any(problem.startswith("pack-version:") for problem in problems)


def test_lock_tree_hash_mismatch_fails(verify_baseline: ModuleType, fake_repo: Path) -> None:
    _edit_lock(verify_baseline, fake_repo, "tree_hash", "0" * 64)
    problems = verify_baseline.verify(fake_repo)
    assert any(problem.startswith("tree-hash:") for problem in problems)


def test_real_baseline_verifies(verify_baseline: ModuleType) -> None:
    problems = verify_baseline.verify(REPO_ROOT)
    assert problems == [], "\n".join(problems)


def test_verification_does_not_modify_documents(verify_baseline: ModuleType) -> None:
    before = verify_baseline.collect_file_entries(REPO_ROOT)
    verify_baseline.verify(REPO_ROOT)
    after = verify_baseline.collect_file_entries(REPO_ROOT)
    assert before == after
