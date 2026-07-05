"""Build metadata record (IMP-0003).

Produces a deterministic build record that ties the repository version and
Git commit to the specification baseline version and hashes, satisfying the
IMP-0003 Definition of Done ("Build records baseline hash and versions").

The record contains no timestamps, so identical inputs produce identical
output on Windows and Linux ARM64.

Usage:
    uv run python scripts/build_metadata.py
    uv run python scripts/build_metadata.py --output artifacts/generated/build_metadata.json
"""

import argparse
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

RECORD_VERSION = "1"


def _load_verify_baseline() -> ModuleType:
    script_path = Path(__file__).resolve().parent / "verify_baseline.py"
    spec = importlib.util.spec_from_file_location("verify_baseline", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_baseline"] = module
    spec.loader.exec_module(module)
    return module


def repository_version(repo_root: Path) -> str:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return "unknown"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version", "unknown")
    return str(version)


def git_commit_sha(repo_root: Path) -> str:
    """Return the HEAD commit SHA, or 'unknown' outside a Git repository."""
    if not (repo_root / ".git").exists():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unknown"


def build_record(repo_root: Path) -> dict[str, Any]:
    verify_baseline = _load_verify_baseline()
    root = verify_baseline.baseline_root(repo_root)
    manifest_path = root / verify_baseline.MANIFEST_NAME
    lock_path = root / verify_baseline.LOCK_NAME

    pack_version = "unknown"
    manifest_sha256 = "unknown"
    if manifest_path.is_file():
        manifest = verify_baseline.load_json(manifest_path)
        pack_version = str(manifest.get("pack_version", "unknown"))
        manifest_sha256 = verify_baseline.normalized_sha256(manifest_path)

    baseline_tree_hash = "unknown"
    if lock_path.is_file():
        lock = verify_baseline.load_json(lock_path)
        baseline_tree_hash = str(lock.get("tree_hash", "unknown"))

    problems = verify_baseline.verify(repo_root)
    return {
        "record_version": RECORD_VERSION,
        "repository_version": repository_version(repo_root),
        "git_commit": git_commit_sha(repo_root),
        "baseline": {
            "pack_version": pack_version,
            "manifest_path": f"{verify_baseline.BASELINE_DIR}/{verify_baseline.MANIFEST_NAME}",
            "manifest_sha256": manifest_sha256,
            "lock_path": f"{verify_baseline.BASELINE_DIR}/{verify_baseline.LOCK_NAME}",
            "tree_hash": baseline_tree_hash,
        },
        "baseline_verification": "PASS" if not problems else "FAIL",
        "baseline_problems": problems,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv[1:])

    record = build_record(args.repo_root)
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(payload, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    return 0 if record["baseline_verification"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
