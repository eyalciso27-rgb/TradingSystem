"""Fail-closed point-in-time qualification of corporate-action records.

Authority: CLAUDE.md ("Preserve point-in-time semantics and prevent
look-ahead in replay/backtesting"), Data_Model_and_Retention_Specification_v0.6
sections 24.1 (``corporate_actions``: type, effective_at, source, status) and
24.2 (point-in-time fields: observed_at, ingested_at, effective_at,
available_at, superseded_at, as_of), System_Functional_Specification_v0.7
section 40.6 (Split/Reverse Split/Symbol Change/Delisting/Merger create a
CorporateActionEvent), ``docs/status/PIT_DATASET_QUALIFICATION.md`` section 3
(Revised R8 — corporate actions require effective-dated PIT records because a
split re-statement changes historical price levels), and
``docs/decisions/OWNER-DECISION-A-WP2-PIT-DATA-2026-09-01.md`` ("Delisted /
corporate-action evidence" and "Current V1 PIT reference requirement":
effective-dated corporate actions are mandatory; every external PIT artifact
must retain provenance and SHA-256 evidence; do not fabricate historical
values).

This module is purely OFFLINE and FIXTURE-ONLY: it validates a JSON fixture
describing corporate-action records and their downstream "adjusted history"
usages. It never performs a network call, never invents/infers a corporate
action, never mutates a position or price, and never claims empirical or
integration evidence — it only proves that a *given* fixture is internally
PIT-consistent, or fails closed with a precise reason.

Invariants enforced (fail closed, no silent defaults):

* Every record carries a stable Instrument Master identifier, a recognised
  ``CorporateActionType``, all four point-in-time timestamps (``effective_at``,
  ``observed_at``, ``available_at``, ``ingested_at``), non-empty source
  provenance, and a real 64-hex ``artifact_sha256`` (re-derived from
  ``artifact_path`` bytes when supplied — never trusted from the label alone).
  "Stable Instrument Master identifier" is not inferred from spelling: the
  Data Model (24.1) and the actual schema
  (``trading_persistence.models``: ``instruments.id`` is a UUID primary key,
  and every ``instrument_id`` foreign key — including ``corporate_actions`` —
  references that UUID) fix the identity space as a UUID, never a ticker,
  venue-qualified symbol, or any other provider-facing spelling. A value that
  does not parse as a UUID is rejected outright, so no ticker-shaped string in
  any casing or venue-suffixed form can pass by accident.
* ``observed_at <= available_at <= ingested_at`` — a record cannot become
  usable before it was observed, and cannot be ingested before it was usable.
* ``available_at`` must not be later than the fixture's decision ``as_of``:
  a record the fixture claims is part of the point-in-time picture at a given
  decision must actually have been available by then.
* Correction lineage (``corrects_record_id``) must reference a real record for
  the SAME instrument, must not cycle, must not branch (a corrected record may
  have at most one correcting record — two records correcting the same
  original would leave an ambiguous, unresolvable effective lineage), the
  correction must become available strictly after the record it corrects, and
  the corrected record must carry a ``superseded_at`` that exactly matches the
  correction's ``available_at`` (the same binding the offline dataset-manifest
  foundation uses for amendments).
* An ``adjusted_history_uses`` entry (a claim that some series was built from
  provider-adjusted prices) must name an existing, same-instrument, split-type
  corporate-action record that was itself available by the decision ``as_of``
  — adjusted history can never be used without its qualifying corporate
  action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_contracts.enums import CorporateActionType

VALIDATOR_VERSION = "corporate-action-pit-validation-1.0.0"

#: Exact CorporateActionType values — no other string is ever accepted.
ALLOWED_ACTION_TYPES = frozenset(member.value for member in CorporateActionType)

#: Only a share-count restatement actually changes historical price levels;
#: a symbol change / delisting / merger does not qualify "adjusted" pricing.
ADJUSTMENT_QUALIFYING_ACTION_TYPES = frozenset(
    {CorporateActionType.SPLIT.value, CorporateActionType.REVERSE_SPLIT.value}
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Provenance that characterises adjustment BEHAVIOUR (a price divergence) is
#: explicitly NOT event-level corporate-action evidence
#: (``trading_broker_ibkr.poi_corporate_actions_qualification``). Accepting it
#: as the source of a CorporateActionEvent would let inferred, non-official
#: "evidence" masquerade as an official effective-dated record.
FORBIDDEN_INFERRED_PROVENANCE = frozenset(
    {
        "ibkr-adjusted-last",
        "adjusted-price-divergence",
        "price-adjustment-inference",
        "adjusted-bars-divergence",
    }
)


class CorporateActionPitError(ValueError):
    """A fail-closed corporate-action point-in-time validation failure."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorporateActionPitError(message)


def _expect_dict(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorporateActionPitError(f"{where}: object is required")
    return value


def _expect_list(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise CorporateActionPitError(f"{where}: list is required")
    return value


def _expect_nonempty_str(value: object, where: str) -> str:
    _require(isinstance(value, str) and value.strip() != "", f"{where}: non-empty string required")
    assert isinstance(value, str)
    return value.strip()


def parse_iso8601(value: object, where: str) -> datetime:
    """Parse a real, timezone-aware ISO-8601 instant, normalized to UTC.

    Fails closed on anything else: missing/null (a required PIT timestamp
    must be recorded, never implied), the wrong type, malformed text, and a
    naive timestamp (its ordering would depend on a later, unspecified
    timezone interpretation, which is not a fixed point on the timeline).
    """
    if not isinstance(value, str) or not value.strip():
        raise CorporateActionPitError(f"{where}: missing timestamp (non-empty ISO-8601 required)")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CorporateActionPitError(
            f"{where}: {value!r} is not a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CorporateActionPitError(
            f"{where}: {value!r} must be timezone-aware (no naive clocks)"
        )
    return parsed.astimezone(UTC)


def validate_stable_instrument_id(instrument_id: str, where: str) -> str:
    """Require the documented Instrument Master identity: a UUID, not a spelling.

    ``trading_persistence.models``: ``instruments.id`` is a UUID primary key
    and every ``instrument_id`` foreign key (including on ``corporate_actions``
    itself) references that UUID — never a ticker, never a venue-qualified
    symbol. Rejecting "looks like a ticker" spellings one at a time is not
    stability; requiring the actual identity type is. Anything that does not
    parse as a UUID is rejected, regardless of casing, punctuation, or venue
    suffix.
    """
    try:
        parsed = uuid.UUID(instrument_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CorporateActionPitError(
            f"{where}: instrument_id {instrument_id!r} is not a stable Instrument "
            "Master UUID (a ticker, venue-qualified symbol, or any other "
            "provider-facing spelling is never accepted; fail closed)"
        ) from exc
    return str(parsed)


def _validate_artifact_sha256(record: dict[str, Any], where: str) -> str:
    checksum = record.get("artifact_sha256")
    _require(
        isinstance(checksum, str) and SHA256_RE.match(checksum) is not None,
        f"{where}: artifact_sha256 must be a real 64-hex sha256, not {checksum!r}",
    )
    assert isinstance(checksum, str)
    artifact_path = record.get("artifact_path")
    if artifact_path is not None:
        # Never trust a checksum label alone when real bytes are named:
        # re-derive the digest from the artifact itself.
        _require(
            isinstance(artifact_path, str) and artifact_path.strip() != "",
            f"{where}: artifact_path, when present, must be a non-empty string",
        )
        assert isinstance(artifact_path, str)
        path = Path(artifact_path)
        _require(path.is_absolute(), f"{where}: artifact_path must be an absolute path")
        _require(path.is_file(), f"{where}: artifact_path {artifact_path!r} does not exist")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _require(
            digest == checksum.lower(),
            f"{where}: artifact_sha256 does not match the recomputed digest of artifact_path bytes",
        )
    return checksum.lower()


class CorporateActionPitRecord:
    """A parsed, validated corporate-action PIT record (fixture-only)."""

    __slots__ = (
        "record_id",
        "instrument_id",
        "action_type",
        "effective_at",
        "observed_at",
        "available_at",
        "ingested_at",
        "source",
        "artifact_sha256",
        "corrects_record_id",
        "superseded_at",
    )

    def __init__(
        self,
        *,
        record_id: str,
        instrument_id: str,
        action_type: str,
        effective_at: datetime,
        observed_at: datetime,
        available_at: datetime,
        ingested_at: datetime,
        source: str,
        artifact_sha256: str,
        corrects_record_id: str | None,
        superseded_at: datetime | None,
    ) -> None:
        self.record_id = record_id
        self.instrument_id = instrument_id
        self.action_type = action_type
        self.effective_at = effective_at
        self.observed_at = observed_at
        self.available_at = available_at
        self.ingested_at = ingested_at
        self.source = source
        self.artifact_sha256 = artifact_sha256
        self.corrects_record_id = corrects_record_id
        self.superseded_at = superseded_at


def validate_record(raw: object, index: int) -> CorporateActionPitRecord:
    """Validate one ``corporate_actions[i]`` entry; fail closed on any gap."""
    where = f"corporate_actions[{index}]"
    record = _expect_dict(raw, where)
    record_id = _expect_nonempty_str(record.get("record_id"), f"{where}.record_id")
    where = f"corporate_actions[{index}] ({record_id})"

    instrument_id = _expect_nonempty_str(record.get("instrument_id"), f"{where}.instrument_id")
    instrument_id = validate_stable_instrument_id(instrument_id, f"{where}.instrument_id")

    action_type = record.get("action_type")
    _require(
        isinstance(action_type, str) and action_type in ALLOWED_ACTION_TYPES,
        f"{where}.action_type: {action_type!r} is not a recognised CorporateActionType",
    )
    assert isinstance(action_type, str)

    effective_at = parse_iso8601(record.get("effective_at"), f"{where}.effective_at")
    observed_at = parse_iso8601(record.get("observed_at"), f"{where}.observed_at")
    available_at = parse_iso8601(record.get("available_at"), f"{where}.available_at")
    ingested_at = parse_iso8601(record.get("ingested_at"), f"{where}.ingested_at")
    _require(
        observed_at <= available_at,
        f"{where}: available_at must not precede observed_at (no look-ahead)",
    )
    _require(
        available_at <= ingested_at,
        f"{where}: ingested_at must not precede available_at (no look-ahead)",
    )

    source = _expect_nonempty_str(record.get("source"), f"{where}.source")
    _require(
        source not in FORBIDDEN_INFERRED_PROVENANCE,
        f"{where}.source: {source!r} is adjustment-BEHAVIOUR inference, not event-level "
        "corporate-action evidence; it must never be used as a record's provenance",
    )

    artifact_sha256 = _validate_artifact_sha256(record, where)

    corrects_record_id = record.get("corrects_record_id")
    if corrects_record_id is not None:
        corrects_record_id = _expect_nonempty_str(corrects_record_id, f"{where}.corrects_record_id")

    superseded_at_raw = record.get("superseded_at")
    superseded_at = (
        None
        if superseded_at_raw is None
        else parse_iso8601(superseded_at_raw, f"{where}.superseded_at")
    )
    if superseded_at is not None:
        _require(
            superseded_at > available_at,
            f"{where}: superseded_at must be after this record's own available_at",
        )

    return CorporateActionPitRecord(
        record_id=record_id,
        instrument_id=instrument_id,
        action_type=action_type,
        effective_at=effective_at,
        observed_at=observed_at,
        available_at=available_at,
        ingested_at=ingested_at,
        source=source,
        artifact_sha256=artifact_sha256,
        corrects_record_id=corrects_record_id,
        superseded_at=superseded_at,
    )


def validate_pit_gate(
    record: CorporateActionPitRecord, decision_as_of: datetime, where: str
) -> None:
    """Reject a record that could not legally have been known at ``as_of``."""
    _require(
        record.available_at <= decision_as_of,
        f"{where}: available_at {record.available_at.isoformat()} is later than "
        f"decision as_of {decision_as_of.isoformat()} (look-ahead)",
    )


def validate_correction_lineage(records: dict[str, CorporateActionPitRecord]) -> None:
    """Reject dangling references, cross-instrument corrections, cycles, and
    branching (more than one record correcting the same original).

    A point-in-time consumer needs exactly one effective correction chain per
    record. If two records both name the same ``corrects_record_id``, each
    may pass every *direct-edge* check in isolation (same instrument, later
    available_at, matching superseded_at) yet leave two incompatible
    successors for the same original — an ambiguity that must never be
    silently resolved by pick-one-and-hope. Reject it outright, before the
    direct-edge checks even run.
    """
    correctors_by_target: dict[str, list[str]] = {}
    for record_id, record in records.items():
        if record.corrects_record_id is not None:
            correctors_by_target.setdefault(record.corrects_record_id, []).append(record_id)
    for target_id, correcting_ids in correctors_by_target.items():
        if len(correcting_ids) > 1:
            raise CorporateActionPitError(
                f"corporate_actions[{target_id}]: invalid correction lineage — "
                f"record {target_id!r} is corrected by more than one record "
                f"{sorted(correcting_ids)!r}; correction lineage must be a single, "
                "unambiguous chain (fail closed)"
            )

    for record_id, record in records.items():
        where = f"corporate_actions[{record_id}]"
        if record.corrects_record_id is None:
            continue

        # Cycle detection (a self-correction is a cycle of length one).
        seen = {record_id}
        cursor: str | None = record.corrects_record_id
        while cursor is not None:
            if cursor in seen:
                raise CorporateActionPitError(
                    f"{where}: invalid correction lineage — corrects_record_id cycle "
                    f"detected at {cursor!r}"
                )
            seen.add(cursor)
            target = records.get(cursor)
            _require(
                target is not None,
                f"{where}: invalid correction lineage — corrects unknown record {cursor!r}",
            )
            assert target is not None
            cursor = target.corrects_record_id

        target = records[record.corrects_record_id]
        _require(
            target.instrument_id == record.instrument_id,
            f"{where}: invalid correction lineage — corrects a record for a different "
            f"instrument ({target.instrument_id!r} != {record.instrument_id!r})",
        )
        _require(
            record.available_at > target.available_at,
            f"{where}: invalid correction lineage — correction available_at must be "
            "strictly after the corrected record's available_at",
        )
        _require(
            target.superseded_at is not None,
            f"{where}: invalid correction lineage — corrected record "
            f"{target.record_id!r} has no superseded_at",
        )
        assert target.superseded_at is not None
        _require(
            target.superseded_at == record.available_at,
            f"{where}: invalid correction lineage — corrected record "
            f"{target.record_id!r} superseded_at must equal this correction's available_at",
        )


def validate_adjusted_history_use(
    raw: object, index: int, records: dict[str, CorporateActionPitRecord], decision_as_of: datetime
) -> None:
    """Reject any use of provider-adjusted history without its qualifying CA."""
    where = f"adjusted_history_uses[{index}]"
    use = _expect_dict(raw, where)
    instrument_id = _expect_nonempty_str(use.get("instrument_id"), f"{where}.instrument_id")
    instrument_id = validate_stable_instrument_id(instrument_id, f"{where}.instrument_id")
    series_id = _expect_nonempty_str(use.get("series_id"), f"{where}.series_id")
    where = f"adjusted_history_uses[{index}] ({series_id})"

    qualifying_id = use.get("qualifying_corporate_action_record_id")
    _require(
        isinstance(qualifying_id, str) and qualifying_id.strip() != "",
        f"{where}: adjusted history used without a qualifying corporate-action record",
    )
    assert isinstance(qualifying_id, str)

    qualifying = records.get(qualifying_id)
    _require(
        qualifying is not None,
        f"{where}: qualifying_corporate_action_record_id {qualifying_id!r} does not "
        "reference any known corporate-action record",
    )
    assert qualifying is not None
    _require(
        qualifying.instrument_id == instrument_id,
        f"{where}: qualifying corporate-action record belongs to a different "
        f"instrument ({qualifying.instrument_id!r} != {instrument_id!r})",
    )
    _require(
        qualifying.action_type in ADJUSTMENT_QUALIFYING_ACTION_TYPES,
        f"{where}: qualifying corporate-action record has action_type "
        f"{qualifying.action_type!r}, which does not justify adjusted price history",
    )
    validate_pit_gate(qualifying, decision_as_of, f"{where}: qualifying record")


def load_fixture(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    return _expect_dict(payload, "fixture")


def validate_corporate_action_pit(
    data: dict[str, Any], *, decision_as_of_override: str | None = None
) -> dict[str, Any]:
    """Validate a complete fixture; raise ``CorporateActionPitError`` on any gap.

    Returns a small summary dict on success (never a claim of empirical or
    integration evidence — this proves internal PIT consistency of the given
    fixture only).
    """
    data = _expect_dict(data, "fixture")
    schema_version = _expect_nonempty_str(
        data.get("schema_version"), "fixture.schema_version"
    )
    if decision_as_of_override is not None:
        decision_as_of = parse_iso8601(decision_as_of_override, "--as-of")
    else:
        decision_as_of = parse_iso8601(data.get("decision_as_of"), "fixture.decision_as_of")

    raw_records = _expect_list(data.get("corporate_actions"), "fixture.corporate_actions")
    records: dict[str, CorporateActionPitRecord] = {}
    for index, raw in enumerate(raw_records):
        record = validate_record(raw, index)
        _require(
            record.record_id not in records,
            f"corporate_actions[{index}]: duplicate record_id {record.record_id!r}",
        )
        records[record.record_id] = record

    for record_id, record in records.items():
        validate_pit_gate(record, decision_as_of, f"corporate_actions[{record_id}]")

    validate_correction_lineage(records)

    raw_uses = _expect_list(
        data.get("adjusted_history_uses", []), "fixture.adjusted_history_uses"
    )
    for index, raw_use in enumerate(raw_uses):
        validate_adjusted_history_use(raw_use, index, records, decision_as_of)

    return {
        "schema_version": schema_version,
        "decision_as_of": decision_as_of.isoformat(),
        "corporate_actions_validated": len(records),
        "adjusted_history_uses_validated": len(raw_uses),
        "validator_version": VALIDATOR_VERSION,
    }


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help=(
            "path to a JSON fixture of corporate-action PIT records "
            "(fixture-only; nothing is fetched)"
        ),
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="override the fixture's decision_as_of with an explicit ISO-8601 instant",
    )
    args = parser.parse_args(argv[1:])

    try:
        data = load_fixture(args.fixture)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"CORPORATE_ACTION_PIT_FIXTURE_LOAD_FAILED: {exc}")
        return 2

    try:
        summary = validate_corporate_action_pit(data, decision_as_of_override=args.as_of)
    except CorporateActionPitError as exc:
        print(f"CORPORATE_ACTION_PIT_VALIDATION_FAILED: {exc}")
        return 1

    print("CORPORATE_ACTION_PIT_VALIDATION_OK")
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
