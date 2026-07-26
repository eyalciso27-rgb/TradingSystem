"""Schema validation for the Phase-13 deferred owner-run manifest.

Guards docs/operations/PHASE_13_DEFERRED_OWNER_RUNS.yaml so it can never drift
into an unsafe or incomplete state. The manifest describes owner-run commands
that will be executed LATER on the owner host; this test rejects, at CI time,
any record that is missing required fields or that would be unsafe to run:

- missing command_template or evidence_path_template;
- an invalid blocker value;
- any Live API port (4001) or LIVE environment;
- a command that does not use `uv run --no-sync`;
- an execute command (mutating broker state) without an explicit confirmation
  placeholder;
- an invented fixed market price (prices must be `<...>` placeholders);
- any Phase-14 / Phase-15 command.

This test connects to nothing and runs no command; it only reads the YAML.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs" / "operations" / "PHASE_13_DEFERRED_OWNER_RUNS.yaml"
LEDGER_MD = REPO_ROOT / "docs" / "operations" / "PHASE_13_DEFERRED_OWNER_RUNS.md"

REQUIRED_FIELDS = frozenset(
    {
        "id",
        "task_id",
        "poi_id",
        "blocker",
        "requires_market_open",
        "requires_owner_host",
        "requires_broker_connection",
        "mutates_broker_state",
        "command_template",
        "evidence_path_template",
        "prerequisites",
        "pass_criteria",
        "abort_criteria",
        "current_status",
    }
)

ALLOWED_BLOCKERS = frozenset(
    {
        "MARKET_CLOSED",
        "OWNER_HOST_REQUIRED",
        "IBKR_PAPER_EXECUTION_REQUIRED",
        "IB_GATEWAY_RESTART_REQUIRED",
        "MARKET_DATA_WINDOW_REQUIRED",
        "EXTERNAL_PROVIDER_DEFERRED",
        "EXTERNAL_INFRASTRUCTURE_NOT_IMPLEMENTED",
    }
)

#: price-bearing flags whose value must be a `<...>` placeholder, never a number
PRICE_FLAGS = ("--limit-price", "--reference-price")


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = manifest["records"]
    return recs


def _tokens(command: str) -> list[str]:
    return command.split()


def test_manifest_loads_and_has_records(records: list[dict[str, Any]]) -> None:
    assert len(records) >= 14  # the fourteen mandated deferred items, at least
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids)), "record ids must be unique"


def test_allowed_blockers_match_the_declared_set(manifest: dict[str, Any]) -> None:
    assert frozenset(manifest["allowed_blockers"]) == ALLOWED_BLOCKERS


def test_every_record_has_all_required_fields(records: list[dict[str, Any]]) -> None:
    for record in records:
        missing = REQUIRED_FIELDS - set(record)
        assert not missing, f"{record.get('id')} missing fields: {sorted(missing)}"


def test_blocker_values_are_valid(records: list[dict[str, Any]]) -> None:
    for record in records:
        assert record["blocker"] in ALLOWED_BLOCKERS, f"{record['id']}: bad blocker"


def test_commands_and_evidence_paths_are_non_empty(records: list[dict[str, Any]]) -> None:
    for record in records:
        assert record["command_template"].strip(), f"{record['id']}: empty command"
        assert record["evidence_path_template"].strip(), f"{record['id']}: empty evidence path"


def test_no_live_port_or_live_environment(records: list[dict[str, Any]]) -> None:
    for record in records:
        blob = f"{record['command_template']} {record['evidence_path_template']}"
        assert "4001" not in blob, f"{record['id']}: Live API port 4001 present"
        assert "LIVE" not in blob.upper().replace("LIVELINESS", ""), f"{record['id']}: LIVE present"
        # any explicit port must be the paper port
        for i, tok in enumerate(_tokens(record["command_template"])):
            if tok == "--port":
                assert _tokens(record["command_template"])[i + 1] == "4002"


def test_every_command_uses_uv_run_no_sync(records: list[dict[str, Any]]) -> None:
    for record in records:
        assert "uv run --no-sync" in record["command_template"], (
            f"{record['id']}: command must use `uv run --no-sync` (host must not prune ibapi)"
        )
        assert "uv run python" not in record["command_template"], (
            f"{record['id']}: plain `uv run python` is forbidden on the host"
        )


def test_mutating_execute_commands_carry_a_confirmation_placeholder(
    records: list[dict[str, Any]],
) -> None:
    for record in records:
        if not record["mutates_broker_state"]:
            continue
        cmd = record["command_template"]
        # cleanup is the one mutating scenario that cancels via the port with no
        # --execute/--confirm; every other mutating command must be an execute.
        if "--scenario cleanup" in cmd:
            continue
        assert "--execute" in cmd, f"{record['id']}: mutating command must use --execute"
        assert "--confirm <" in cmd, (
            f"{record['id']}: execute command needs an explicit <TOKEN> placeholder"
        )


def test_prices_are_placeholders_never_invented_numbers(records: list[dict[str, Any]]) -> None:
    for record in records:
        toks = _tokens(record["command_template"])
        for i, tok in enumerate(toks):
            if tok in PRICE_FLAGS:
                value = toks[i + 1]
                assert value.startswith("<") and value.endswith(">"), (
                    f"{record['id']}: {tok} must be a <placeholder>, not {value!r}"
                )
                assert not re.fullmatch(r"[0-9.]+", value), (
                    f"{record['id']}: {tok} must not be a fixed numeric market price"
                )


def test_no_phase_14_or_15_commands(records: list[dict[str, Any]]) -> None:
    for record in records:
        blob = " ".join(
            str(record[k]) for k in ("id", "task_id", "command_template", "current_status")
        ).upper()
        for banned in ("PHASE-14", "PHASE-15", "PHASE_14", "PHASE_15", "IMP-14", "IMP-15"):
            assert banned not in blob, f"{record['id']}: Phase 14/15 reference present ({banned})"


def test_execute_records_require_owner_confirmation(records: list[dict[str, Any]]) -> None:
    for record in records:
        if "--execute" in record["command_template"]:
            assert record["owner_confirmation_required"] is True, (
                f"{record['id']}: an execute command must require owner confirmation"
            )


def test_markdown_ledger_references_every_record_id(records: list[dict[str, Any]]) -> None:
    """The human ledger must mirror the manifest: every record id appears."""
    ledger = LEDGER_MD.read_text(encoding="utf-8")
    for record in records:
        assert record["id"] in ledger, f"{record['id']} missing from the Markdown ledger"


def test_the_fourteen_mandated_items_are_present(records: list[dict[str, Any]]) -> None:
    tasks = {r["id"]: r for r in records}
    # spot-check the mandated coverage by intent, keyed on stable id fragments
    needed = [
        "cancel-path-dry-run",
        "cancel-path-execute",
        "fill-path-dry-run",
        "fill-path-execute",
        "protection",
        "gateway-restart",
        "reconnect",
        "reconciliation",
        "cleanup",
        "market-data",
        "host-chaos",
        "ibc-watchdog",
        "deep-probe",
        "external-provider",
    ]
    joined = " ".join(tasks)
    for fragment in needed:
        assert fragment in joined, f"mandated deferred item missing: {fragment}"
