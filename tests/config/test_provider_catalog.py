"""Provider/runtime catalog consistency (IMP-0503).

Every provider assumption is either FROZEN (with decision + authority) or
PENDING_POI pointing at a real, still-unresolved PoI. When a blocking PoI
turns PASS, the corresponding test fails — forcing the catalog to be frozen
instead of silently drifting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "config" / "assumptions" / "provider-runtime-catalog.yaml"
POI_STATUS = REPO_ROOT / "docs" / "poi" / "STATUS.yaml"
POI_BACKLOG = REPO_ROOT / "docs" / "baseline-v0.5" / "POI_BACKLOG.yaml"

SECTIONS = ("runtime", "connectors", "market_data", "brokers", "ai", "notifications")


def _catalog() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    return loaded


def _entries() -> list[dict[str, Any]]:
    catalog = _catalog()
    entries: list[dict[str, Any]] = []
    for section in SECTIONS:
        assert section in catalog, f"missing catalog section: {section}"
        entries.extend(catalog[section])
    return entries


def _poi_statuses() -> dict[str, str]:
    loaded: dict[str, Any] = yaml.safe_load(POI_STATUS.read_text(encoding="utf-8"))
    return {item["id"]: item["status"] for item in loaded["items"]}


def test_every_entry_is_frozen_or_explicitly_pending() -> None:
    for entry in _entries():
        assert entry.get("status") in ("FROZEN", "PENDING_POI"), entry.get("id")
        if entry["status"] == "FROZEN":
            assert entry.get("decision"), f"{entry['id']}: FROZEN needs a decision"
            assert entry.get("authority"), f"{entry['id']}: FROZEN needs an authority ref"
        else:
            assert entry.get("blocking_poi"), f"{entry['id']}: PENDING needs blocking_poi"
            assert entry.get("note"), f"{entry['id']}: PENDING needs a note"


def test_blocking_pois_exist_and_are_still_unresolved() -> None:
    backlog_ids = {
        item["id"]
        for item in yaml.safe_load(POI_BACKLOG.read_text(encoding="utf-8"))["items"]
    }
    statuses = _poi_statuses()
    for entry in _entries():
        if entry["status"] != "PENDING_POI":
            continue
        poi = entry["blocking_poi"]
        assert poi in backlog_ids, f"{entry['id']}: unknown PoI {poi}"
        # A PASS PoI may no longer hold a provider slot open.
        assert statuses.get(poi) != "PASS", (
            f"{entry['id']}: blocking PoI {poi} already PASSED — freeze this entry"
        )


def test_no_duplicate_ids() -> None:
    ids = [entry["id"] for entry in _entries()]
    assert len(ids) == len(set(ids))


def test_social_user_paths_never_frozen_onto_bots() -> None:
    """Binding clarification: bots never substitute personal user accounts."""
    by_id = {entry["id"]: entry for entry in _entries()}
    for connector in ("whatsapp-user", "discord-user", "telegram-user-tdlib"):
        assert connector in by_id, connector
        text = yaml.safe_dump(by_id[connector])
        assert "fallback" not in text.lower() or "never" in text.lower()
    bot = by_id["telegram-bot"]
    assert "supplemental" in bot["decision"].lower()
    assert "never a substitute" in bot["decision"].lower()


def test_no_secret_material_in_catalog() -> None:
    lowered = CATALOG.read_text(encoding="utf-8").lower()
    for marker in ("password=", "token=", "api_hash=", "bearer ey", "akia"):
        assert marker not in lowered
