"""Baseline manifest and hash verification (IMP-0003).

The authoritative specification baseline lives in docs/baseline-v0.5/.
BASELINE_MANIFEST.json is the canonical manifest (its format is unchanged);
BASELINE_LOCK.json is the derived hash lock that pins every baseline file.
Any change to a baseline document without an explicit lock update fails
verification.

Hashing normalizes CRLF to LF before computing SHA-256 so results are
identical on Windows (core.autocrlf=true checkouts) and Linux ARM64.
The lock file contains no timestamps and is fully deterministic.

Usage:
    uv run python scripts/verify_baseline.py            # verify, exit 0/1
    uv run python scripts/verify_baseline.py --update   # regenerate the lock
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

BASELINE_DIR = "docs/baseline-v0.5"
MANIFEST_NAME = "BASELINE_MANIFEST.json"
LOCK_NAME = "BASELINE_LOCK.json"
AUTHORITY_MAP_NAME = "CONTRACT_AUTHORITY_MAP.json"
CANONICAL_PACK_VERSION = "0.5"
HASH_ALGORITHM = "sha256"
NORMALIZATION = "crlf-to-lf"

REQUIRED_FILES: tuple[str, ...] = (
    MANIFEST_NAME,
    AUTHORITY_MAP_NAME,
    "Master_Architecture_Baseline_v0.3_HE.md",
    "Baseline_Consistency_Review_v0.1_HE.md",
    "Claude_Code_Implementation_Plan_v0.1_HE.md",
    "Proof_of_Integration_Backlog_v0.1_HE.md",
    "Traceability_Matrix_v0.3_HE.md",
    "Specification_Pack_Index_v0.5_HE.md",
    "CLAUDE_CODE_TASKS.yaml",
    "POI_BACKLOG.yaml",
)


def baseline_root(repo_root: Path) -> Path:
    return repo_root / Path(BASELINE_DIR)


def normalized_sha256(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def collect_file_entries(repo_root: Path) -> list[dict[str, str]]:
    """Hash every baseline file except the lock itself, sorted by path."""
    root = baseline_root(repo_root)
    entries: list[dict[str, str]] = []
    for file_path in sorted(root.rglob("*")):
        if file_path.is_file() and file_path.name != LOCK_NAME:
            entries.append(
                {
                    "path": file_path.relative_to(root).as_posix(),
                    "sha256": normalized_sha256(file_path),
                }
            )
    return entries


def tree_hash(entries: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_lock(repo_root: Path) -> dict[str, Any]:
    manifest = load_json(baseline_root(repo_root) / MANIFEST_NAME)
    entries = collect_file_entries(repo_root)
    return {
        "pack_version": manifest["pack_version"],
        "manifest": MANIFEST_NAME,
        "algorithm": HASH_ALGORITHM,
        "normalization": NORMALIZATION,
        "tree_hash": tree_hash(entries),
        "files": entries,
    }


def write_lock(repo_root: Path) -> Path:
    lock_path = baseline_root(repo_root) / LOCK_NAME
    payload = json.dumps(build_lock(repo_root), ensure_ascii=False, indent=2) + "\n"
    with lock_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    return lock_path


def _is_unsafe(rel_path: str) -> bool:
    if "\\" in rel_path or rel_path.startswith("/") or ":" in rel_path:
        return True
    parts = PurePosixPath(rel_path).parts
    return ".." in parts or rel_path == ""


def verify(repo_root: Path) -> list[str]:
    """Return a list of problems; an empty list means the baseline verifies."""
    problems: list[str] = []
    root = baseline_root(repo_root)

    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"missing-file: {BASELINE_DIR}/{MANIFEST_NAME}"]
    try:
        manifest = load_json(manifest_path)
    except json.JSONDecodeError as error:
        return [f"invalid-json: {MANIFEST_NAME}: {error.msg}"]

    if manifest.get("pack_version") != CANONICAL_PACK_VERSION:
        problems.append(
            f"pack-version: manifest pack_version {manifest.get('pack_version')!r} "
            f"!= canonical {CANONICAL_PACK_VERSION!r}"
        )
    if manifest.get("live_enabled") is not False:
        problems.append("manifest-policy: live_enabled must be false")
    if manifest.get("paper_auto_enabled") is not False:
        problems.append("manifest-policy: paper_auto_enabled must be false")

    lock_path = root / LOCK_NAME
    if not lock_path.is_file():
        problems.append(f"missing-file: {BASELINE_DIR}/{LOCK_NAME} (run --update once)")
        return problems
    try:
        lock = load_json(lock_path)
    except json.JSONDecodeError as error:
        problems.append(f"invalid-json: {LOCK_NAME}: {error.msg}")
        return problems

    if lock.get("pack_version") != manifest.get("pack_version"):
        problems.append(
            f"pack-version: lock pack_version {lock.get('pack_version')!r} "
            f"!= manifest {manifest.get('pack_version')!r}"
        )

    entries = lock.get("files", [])
    seen: set[str] = set()
    for entry in entries:
        rel_path = str(entry.get("path", ""))
        if _is_unsafe(rel_path):
            problems.append(f"unsafe-path: {rel_path!r} escapes or is not baseline-relative")
            continue
        if rel_path in seen:
            problems.append(f"duplicate-path: {rel_path}")
            continue
        seen.add(rel_path)
        file_path = root / PurePosixPath(rel_path)
        if not file_path.is_file():
            problems.append(f"missing-file: {BASELINE_DIR}/{rel_path}")
            continue
        actual = normalized_sha256(file_path)
        if actual != entry.get("sha256"):
            problems.append(
                f"hash-mismatch: {rel_path} changed without a lock update "
                f"(expected {entry.get('sha256')}, actual {actual})"
            )

    recomputed = tree_hash(
        [{"path": str(e.get("path", "")), "sha256": str(e.get("sha256", ""))} for e in entries]
    )
    if recomputed != lock.get("tree_hash"):
        problems.append("tree-hash: lock tree_hash does not match its file entries")

    on_disk = {entry["path"] for entry in collect_file_entries(repo_root)}
    for rel_path in sorted(on_disk - seen):
        problems.append(f"undocumented-file: {BASELINE_DIR}/{rel_path} is not in the lock")

    for required in REQUIRED_FILES:
        if required not in seen:
            problems.append(f"missing-required: {required} is not covered by the lock")

    authority_path = root / AUTHORITY_MAP_NAME
    if authority_path.is_file():
        try:
            authority = load_json(authority_path)
            for document in authority.get("documents", []):
                doc_file = str(document.get("file", ""))
                if doc_file not in seen:
                    problems.append(
                        f"authority-map: referenced file {doc_file!r} is not in the lock"
                    )
        except json.JSONDecodeError as error:
            problems.append(f"invalid-json: {AUTHORITY_MAP_NAME}: {error.msg}")

    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate the lock from the current baseline (explicit, audited action)",
    )
    args = parser.parse_args(argv[1:])

    if args.update:
        lock_path = write_lock(args.repo_root)
        print(f"UPDATED: {lock_path}")

    problems = verify(args.repo_root)
    for problem in problems:
        print(problem)
    if problems:
        print(f"FAIL: {len(problems)} baseline verification problem(s).")
        return 1
    print("OK: baseline verification passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
