"""The PoI status registry stays consistent with the frozen backlog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKLOG = REPO_ROOT / "docs" / "baseline-v0.5" / "POI_BACKLOG.yaml"
STATUS = REPO_ROOT / "docs" / "poi" / "STATUS.yaml"

CANONICAL_STATES = {"PASS", "FAIL", "BLOCKED"}


def _items(path: Path) -> list[dict[str, Any]]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = loaded["items"]
    return items


def test_registry_ids_exist_in_frozen_backlog() -> None:
    backlog_ids = {item["id"] for item in _items(BACKLOG)}
    for item in _items(STATUS):
        assert item["id"] in backlog_ids, item["id"]


def test_registry_states_are_canonical_and_explained() -> None:
    for item in _items(STATUS):
        assert item["status"] in CANONICAL_STATES, item["id"]
        # every non-PASS item must say what blocks it; every item has evidence
        if item["status"] == "BLOCKED":
            assert item.get("blocked_on"), f"{item['id']} lacks blocked_on"
        assert item.get("evidence"), f"{item['id']} lacks evidence pointer"
        evidence = REPO_ROOT / "docs" / "poi" / str(item["evidence"])
        assert evidence.is_file(), f"{item['id']} evidence file missing: {evidence}"


def test_priorities_match_backlog() -> None:
    backlog = {item["id"]: item["priority"] for item in _items(BACKLOG)}
    for item in _items(STATUS):
        assert item["priority"] == backlog[item["id"]], item["id"]


def test_no_secret_material_in_registry_or_evidence() -> None:
    corpus = [STATUS.read_text(encoding="utf-8")]
    for evidence in (REPO_ROOT / "docs" / "poi" / "evidence").glob("*.md"):
        corpus.append(evidence.read_text(encoding="utf-8"))
    for text in corpus:
        lowered = text.lower()
        for marker in ("api_hash=", "bot_token=", "password=", "bearer ey", "akia"):
            assert marker not in lowered
