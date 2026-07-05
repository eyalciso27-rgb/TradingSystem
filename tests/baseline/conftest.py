"""Shared fixtures for baseline verification tests (IMP-0003)."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    script_path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def verify_baseline() -> ModuleType:
    return _load_script("verify_baseline")


@pytest.fixture(scope="session")
def build_metadata() -> ModuleType:
    return _load_script("build_metadata")


@pytest.fixture()
def fake_repo(tmp_path: Path, verify_baseline: ModuleType) -> Path:
    """A minimal repository whose baseline verifies cleanly."""
    repo_root = tmp_path / "repo"
    baseline_dir = repo_root / "docs" / "baseline-v0.5"
    baseline_dir.mkdir(parents=True)

    manifest = {
        "pack_version": "0.5",
        "status": "CONSISTENT_FOR_IMPLEMENTATION",
        "deliverables": [],
        "live_enabled": False,
        "paper_auto_enabled": False,
    }
    (baseline_dir / "BASELINE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (baseline_dir / "CONTRACT_AUTHORITY_MAP.json").write_text(
        json.dumps({"pack_version": "0.5", "documents": []}), encoding="utf-8"
    )
    for required in verify_baseline.REQUIRED_FILES:
        file_path = baseline_dir / required
        if not file_path.exists():
            file_path.write_text(f"placeholder for {required}\n", encoding="utf-8")

    (repo_root / "pyproject.toml").write_text(
        '[project]\nname = "fake"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    verify_baseline.write_lock(repo_root)
    return repo_root
