"""A-WP2 — fail-closed corporate-action point-in-time qualification.

Authority: CLAUDE.md ("Preserve point-in-time semantics and prevent
look-ahead in replay/backtesting"), Data_Model_and_Retention_Specification_v0.6
sections 24.1/24.2, ``docs/status/PIT_DATASET_QUALIFICATION.md`` section 3
(corporate actions require effective-dated PIT records with correction
lineage), and ``docs/decisions/OWNER-DECISION-A-WP2-PIT-DATA-2026-09-01.md``
("Delisted / corporate-action evidence" and "Current V1 PIT reference
requirement").

``scripts/validate_corporate_action_pit.py`` is purely OFFLINE and
FIXTURE-ONLY. Every test here builds its own in-memory JSON-shaped fixture; no
test reads a real corporate-action feed, fetches anything from the network, or
claims empirical/integration evidence. The script lives in ``scripts/`` (not a
package) and is imported here the same way the other script proofs do it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from validate_corporate_action_pit import (  # noqa: E402
    CorporateActionPitError,
    load_fixture,
    main,
    validate_corporate_action_pit,
)

EFFECTIVE = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)  # ex-date
ANNOUNCED = EFFECTIVE - timedelta(days=32)  # observed_at: announced ahead of ex-date
AVAILABLE = ANNOUNCED + timedelta(minutes=5)
INGESTED = AVAILABLE + timedelta(minutes=1)
DECISION_AS_OF = AVAILABLE + timedelta(days=1)

VALID_SHA = "a" * 64
Capsys = pytest.CaptureFixture[str]


def ca_record(
    record_id: str = "ca-001",
    *,
    instrument_id: str = "INST:AAPL:0001",
    action_type: str = "SPLIT",
    effective_at: datetime = EFFECTIVE,
    observed_at: datetime = ANNOUNCED,
    available_at: datetime = AVAILABLE,
    ingested_at: datetime = INGESTED,
    source: str = "sec-edgar-form8k",
    artifact_sha256: str = VALID_SHA,
    corrects_record_id: str | None = None,
    superseded_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "record_id": record_id,
        "instrument_id": instrument_id,
        "action_type": action_type,
        "effective_at": effective_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "available_at": available_at.isoformat(),
        "ingested_at": ingested_at.isoformat(),
        "source": source,
        "artifact_sha256": artifact_sha256,
    }
    if corrects_record_id is not None:
        record["corrects_record_id"] = corrects_record_id
    if superseded_at is not None:
        record["superseded_at"] = superseded_at.isoformat()
    if extra:
        record.update(extra)
    return record


def fixture(
    *,
    records: list[dict[str, Any]] | None = None,
    uses: list[dict[str, Any]] | None = None,
    decision_as_of: datetime = DECISION_AS_OF,
) -> dict[str, Any]:
    return {
        "schema_version": "corporate-action-pit-fixture-1.0.0",
        "decision_as_of": decision_as_of.isoformat(),
        "corporate_actions": records if records is not None else [ca_record()],
        "adjusted_history_uses": uses if uses is not None else [],
    }


def use(
    *,
    instrument_id: str = "INST:AAPL:0001",
    series_id: str = "AAPL:close:1d:adjusted",
    qualifying_corporate_action_record_id: str | None = "ca-001",
) -> dict[str, Any]:
    entry: dict[str, Any] = {"instrument_id": instrument_id, "series_id": series_id}
    if qualifying_corporate_action_record_id is not None:
        entry["qualifying_corporate_action_record_id"] = qualifying_corporate_action_record_id
    return entry


# -- happy path ---------------------------------------------------------------


def test_valid_fixture_passes_and_reports_counts() -> None:
    data = fixture(uses=[use()])
    summary = validate_corporate_action_pit(data)
    assert summary["corporate_actions_validated"] == 1
    assert summary["adjusted_history_uses_validated"] == 1
    assert summary["decision_as_of"] == DECISION_AS_OF.isoformat()


def test_valid_fixture_with_no_uses_passes() -> None:
    validate_corporate_action_pit(fixture())  # must not raise


def test_as_of_override_can_reject_a_fixture_that_otherwise_passes() -> None:
    data = fixture()
    with pytest.raises(CorporateActionPitError, match="look-ahead"):
        validate_corporate_action_pit(
            data, decision_as_of_override=(AVAILABLE - timedelta(seconds=1)).isoformat()
        )


def test_as_of_override_can_accept_at_exact_availability_boundary() -> None:
    validate_corporate_action_pit(fixture(), decision_as_of_override=AVAILABLE.isoformat())


# -- missing / malformed timestamps -------------------------------------------


@pytest.mark.parametrize(
    "field", ["effective_at", "observed_at", "available_at", "ingested_at"]
)
def test_missing_timestamp_is_rejected(field: str) -> None:
    record = ca_record()
    del record[field]
    with pytest.raises(CorporateActionPitError, match="missing timestamp"):
        validate_corporate_action_pit(fixture(records=[record]))


@pytest.mark.parametrize(
    "field", ["effective_at", "observed_at", "available_at", "ingested_at"]
)
def test_null_timestamp_is_rejected(field: str) -> None:
    record = ca_record()
    record[field] = None
    with pytest.raises(CorporateActionPitError, match="missing timestamp"):
        validate_corporate_action_pit(fixture(records=[record]))


@pytest.mark.parametrize(
    "field", ["effective_at", "observed_at", "available_at", "ingested_at"]
)
def test_naive_timestamp_is_rejected(field: str) -> None:
    record = ca_record()
    record[field] = "2026-08-31T13:00:00"  # no offset
    with pytest.raises(CorporateActionPitError, match="timezone-aware"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_missing_decision_as_of_is_rejected() -> None:
    data = fixture()
    del data["decision_as_of"]
    with pytest.raises(CorporateActionPitError, match="missing timestamp"):
        validate_corporate_action_pit(data)


def test_available_before_observed_is_rejected() -> None:
    record = ca_record(observed_at=AVAILABLE, available_at=ANNOUNCED)
    with pytest.raises(CorporateActionPitError, match="available_at must not precede observed_at"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_ingested_before_available_is_rejected() -> None:
    record = ca_record(ingested_at=AVAILABLE - timedelta(seconds=1))
    with pytest.raises(CorporateActionPitError, match="ingested_at must not precede available_at"):
        validate_corporate_action_pit(fixture(records=[record]))


# -- available_at later than decision as_of -----------------------------------


def test_available_at_later_than_decision_as_of_is_rejected() -> None:
    record = ca_record(available_at=DECISION_AS_OF + timedelta(days=1))
    with pytest.raises(CorporateActionPitError, match="look-ahead"):
        validate_corporate_action_pit(fixture(records=[record], decision_as_of=DECISION_AS_OF))


def test_available_at_exactly_at_decision_as_of_is_accepted() -> None:
    record = ca_record(available_at=DECISION_AS_OF, ingested_at=DECISION_AS_OF)
    validate_corporate_action_pit(fixture(records=[record], decision_as_of=DECISION_AS_OF))


# -- instrument identifier, action type, source provenance --------------------


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "X", "GOOGL"])
def test_bare_ticker_instrument_id_is_rejected(ticker: str) -> None:
    record = ca_record(instrument_id=ticker)
    with pytest.raises(CorporateActionPitError, match="raw ticker symbol"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_missing_instrument_id_is_rejected() -> None:
    record = ca_record()
    del record["instrument_id"]
    with pytest.raises(CorporateActionPitError, match="non-empty string required"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_unrecognised_action_type_is_rejected() -> None:
    record = ca_record(action_type="STOCK_BUYBACK")
    with pytest.raises(CorporateActionPitError, match="not a recognised CorporateActionType"):
        validate_corporate_action_pit(fixture(records=[record]))


@pytest.mark.parametrize(
    "action_type", ["SPLIT", "REVERSE_SPLIT", "SYMBOL_CHANGE", "DELISTING", "MERGER"]
)
def test_every_real_action_type_is_accepted(action_type: str) -> None:
    record = ca_record(action_type=action_type)
    validate_corporate_action_pit(fixture(records=[record]))


def test_missing_source_is_rejected() -> None:
    record = ca_record()
    del record["source"]
    with pytest.raises(CorporateActionPitError, match="non-empty string required"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_inferred_adjustment_provenance_is_rejected_as_source() -> None:
    record = ca_record(source="ibkr-adjusted-last")
    with pytest.raises(CorporateActionPitError, match="not event-level corporate-action evidence"):
        validate_corporate_action_pit(fixture(records=[record]))


# -- artifact SHA-256 ----------------------------------------------------------


def test_missing_artifact_sha256_is_rejected() -> None:
    record = ca_record()
    del record["artifact_sha256"]
    with pytest.raises(CorporateActionPitError, match="real 64-hex sha256"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_malformed_artifact_sha256_is_rejected() -> None:
    record = ca_record(artifact_sha256="not-a-hash")
    with pytest.raises(CorporateActionPitError, match="real 64-hex sha256"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_artifact_sha256_is_rebound_to_real_bytes_when_path_given(tmp_path: Path) -> None:
    artifact = tmp_path / "form8k.htm"
    artifact.write_bytes(b"<html>8-K split announcement</html>")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    record = ca_record(artifact_sha256=digest, extra={"artifact_path": str(artifact)})
    validate_corporate_action_pit(fixture(records=[record]))  # must not raise


def test_artifact_sha256_mismatch_against_real_bytes_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "form8k.htm"
    artifact.write_bytes(b"<html>8-K split announcement</html>")
    record = ca_record(artifact_sha256=VALID_SHA, extra={"artifact_path": str(artifact)})
    with pytest.raises(CorporateActionPitError, match="does not match the recomputed digest"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_artifact_path_must_be_absolute(tmp_path: Path) -> None:
    record = ca_record(extra={"artifact_path": "relative/form8k.htm"})
    with pytest.raises(CorporateActionPitError, match="must be an absolute path"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_artifact_path_must_exist(tmp_path: Path) -> None:
    record = ca_record(extra={"artifact_path": str(tmp_path / "missing.htm")})
    with pytest.raises(CorporateActionPitError, match="does not exist"):
        validate_corporate_action_pit(fixture(records=[record]))


# -- duplicates -----------------------------------------------------------------


def test_duplicate_record_id_is_rejected() -> None:
    with pytest.raises(CorporateActionPitError, match="duplicate record_id"):
        validate_corporate_action_pit(fixture(records=[ca_record(), ca_record()]))


# -- correction lineage ---------------------------------------------------------


def _corrected_pair() -> list[dict[str, Any]]:
    """A clean original + correction pair, valid by construction."""
    correction_available = AVAILABLE + timedelta(days=2)
    original = ca_record(
        "ca-001",
        available_at=AVAILABLE,
        ingested_at=AVAILABLE,
        superseded_at=correction_available,
    )
    correction = ca_record(
        "ca-002",
        observed_at=ANNOUNCED + timedelta(days=1),
        available_at=correction_available,
        ingested_at=correction_available,
        corrects_record_id="ca-001",
    )
    return [original, correction]


def test_valid_correction_lineage_is_accepted() -> None:
    validate_corporate_action_pit(
        fixture(records=_corrected_pair(), decision_as_of=DECISION_AS_OF + timedelta(days=5))
    )


def test_correction_referencing_unknown_record_is_rejected() -> None:
    records = _corrected_pair()
    records[1]["corrects_record_id"] = "ca-does-not-exist"
    with pytest.raises(CorporateActionPitError, match="corrects unknown record"):
        validate_corporate_action_pit(
            fixture(records=records, decision_as_of=DECISION_AS_OF + timedelta(days=5))
        )


def test_self_correcting_record_is_rejected_as_a_cycle() -> None:
    record = ca_record(
        "ca-001", corrects_record_id="ca-001", superseded_at=AVAILABLE + timedelta(days=1)
    )
    with pytest.raises(CorporateActionPitError, match="cycle"):
        validate_corporate_action_pit(fixture(records=[record]))


def test_two_record_correction_cycle_is_rejected() -> None:
    later = AVAILABLE + timedelta(days=2)
    a = ca_record("ca-a", corrects_record_id="ca-b", available_at=later, ingested_at=later,
                  superseded_at=later + timedelta(days=1))
    b = ca_record("ca-b", corrects_record_id="ca-a", available_at=AVAILABLE, ingested_at=AVAILABLE,
                  superseded_at=later)
    with pytest.raises(CorporateActionPitError, match="cycle"):
        validate_corporate_action_pit(
            fixture(records=[a, b], decision_as_of=DECISION_AS_OF + timedelta(days=10))
        )


def test_correction_across_different_instruments_is_rejected() -> None:
    records = _corrected_pair()
    records[1]["instrument_id"] = "INST:MSFT:0002"
    with pytest.raises(CorporateActionPitError, match="different instrument"):
        validate_corporate_action_pit(
            fixture(records=records, decision_as_of=DECISION_AS_OF + timedelta(days=5))
        )


def test_correction_not_after_corrected_record_is_rejected() -> None:
    # Both records share the same available_at: the correction is not
    # STRICTLY after the record it corrects, so lineage must fail closed
    # before superseded_at is even considered.
    original = ca_record(
        "ca-001", observed_at=ANNOUNCED, available_at=AVAILABLE, ingested_at=AVAILABLE
    )
    correction = ca_record(
        "ca-002",
        observed_at=ANNOUNCED,
        available_at=AVAILABLE,
        ingested_at=AVAILABLE,
        corrects_record_id="ca-001",
    )
    with pytest.raises(CorporateActionPitError, match="strictly after"):
        validate_corporate_action_pit(fixture(records=[original, correction]))


def test_corrected_record_without_superseded_at_is_rejected() -> None:
    records = _corrected_pair()
    del records[0]["superseded_at"]
    with pytest.raises(CorporateActionPitError, match="has no superseded_at"):
        validate_corporate_action_pit(
            fixture(records=records, decision_as_of=DECISION_AS_OF + timedelta(days=5))
        )


def test_corrected_record_superseded_at_must_equal_correction_available_at() -> None:
    records = _corrected_pair()
    original_superseded = datetime.fromisoformat(records[0]["superseded_at"])
    records[0]["superseded_at"] = (original_superseded + timedelta(hours=1)).isoformat()
    with pytest.raises(CorporateActionPitError, match="must equal this correction's available_at"):
        validate_corporate_action_pit(
            fixture(records=records, decision_as_of=DECISION_AS_OF + timedelta(days=5))
        )


def test_superseded_at_before_own_available_at_is_rejected() -> None:
    record = ca_record(
        available_at=AVAILABLE,
        ingested_at=AVAILABLE,
        superseded_at=AVAILABLE - timedelta(days=1),
    )
    with pytest.raises(CorporateActionPitError, match="superseded_at must be after"):
        validate_corporate_action_pit(fixture(records=[record]))


# -- adjusted history uses without a qualifying corporate action --------------


def test_adjusted_history_use_without_qualifying_id_is_rejected() -> None:
    data = fixture(uses=[use(qualifying_corporate_action_record_id=None)])
    with pytest.raises(
        CorporateActionPitError, match="without a qualifying corporate-action record"
    ):
        validate_corporate_action_pit(data)


def test_adjusted_history_use_with_blank_qualifying_id_is_rejected() -> None:
    entry = use()
    entry["qualifying_corporate_action_record_id"] = "   "
    with pytest.raises(
        CorporateActionPitError, match="without a qualifying corporate-action record"
    ):
        validate_corporate_action_pit(fixture(uses=[entry]))


def test_adjusted_history_use_referencing_unknown_record_is_rejected() -> None:
    data = fixture(uses=[use(qualifying_corporate_action_record_id="ca-ghost")])
    with pytest.raises(CorporateActionPitError, match="does not reference any known"):
        validate_corporate_action_pit(data)


def test_adjusted_history_use_for_a_different_instrument_is_rejected() -> None:
    data = fixture(uses=[use(instrument_id="INST:MSFT:0002")])
    with pytest.raises(CorporateActionPitError, match="belongs to a different"):
        validate_corporate_action_pit(data)


def test_adjusted_history_use_qualified_by_non_split_action_is_rejected() -> None:
    record = ca_record(action_type="SYMBOL_CHANGE")
    data = fixture(records=[record], uses=[use()])
    with pytest.raises(CorporateActionPitError, match="does not justify adjusted price history"):
        validate_corporate_action_pit(data)


def test_adjusted_history_use_qualified_by_reverse_split_is_accepted() -> None:
    record = ca_record(action_type="REVERSE_SPLIT")
    data = fixture(records=[record], uses=[use()])
    validate_corporate_action_pit(data)  # must not raise


def test_adjusted_history_use_qualified_by_not_yet_available_record_is_rejected() -> None:
    # The qualifying record itself is fine in isolation, but the use's
    # decision_as_of predates it: the top-level PIT gate already blocks the
    # record, and the reason is unambiguous.
    record = ca_record(available_at=AVAILABLE, ingested_at=AVAILABLE)
    data = fixture(records=[record], uses=[use()], decision_as_of=AVAILABLE - timedelta(seconds=1))
    with pytest.raises(CorporateActionPitError, match="look-ahead"):
        validate_corporate_action_pit(data)


# -- structural fixture errors --------------------------------------------------


def test_missing_schema_version_is_rejected() -> None:
    data = fixture()
    del data["schema_version"]
    with pytest.raises(CorporateActionPitError, match="non-empty string required"):
        validate_corporate_action_pit(data)


def test_corporate_actions_must_be_a_list() -> None:
    data = fixture()
    data["corporate_actions"] = {"not": "a list"}
    with pytest.raises(CorporateActionPitError, match="list is required"):
        validate_corporate_action_pit(data)


def test_adjusted_history_uses_must_be_a_list() -> None:
    data = fixture()
    data["adjusted_history_uses"] = "nope"
    with pytest.raises(CorporateActionPitError, match="list is required"):
        validate_corporate_action_pit(data)


def test_top_level_payload_must_be_an_object() -> None:
    with pytest.raises(CorporateActionPitError, match="object is required"):
        validate_corporate_action_pit([])  # type: ignore[arg-type]


# -- fixture never claims real fetch / integration evidence --------------------


def test_module_docstring_never_claims_integration_evidence() -> None:
    import validate_corporate_action_pit as module

    doc = module.__doc__ or ""
    for forbidden in ("EMPIRICALLY_VERIFIED", "REAL_DATASET_BUILT", "LIVE_READY", "PAPER_AUTO"):
        assert forbidden not in doc


# -- load_fixture / CLI ----------------------------------------------------------


def test_load_fixture_reads_real_json_file(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture(uses=[use()])), encoding="utf-8")
    data = load_fixture(path)
    summary = validate_corporate_action_pit(data)
    assert summary["corporate_actions_validated"] == 1


def test_cli_reports_success_and_prints_summary(tmp_path: Path, capsys: Capsys) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture(uses=[use()])), encoding="utf-8")
    exit_code = main(["prog", "--fixture", str(path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "CORPORATE_ACTION_PIT_VALIDATION_OK" in out
    assert "corporate_actions_validated: 1" in out


def test_cli_fails_closed_on_invalid_fixture(tmp_path: Path, capsys: Capsys) -> None:
    bad = fixture(records=[ca_record(instrument_id="AAPL")])
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    exit_code = main(["prog", "--fixture", str(path)])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "CORPORATE_ACTION_PIT_VALIDATION_FAILED" in out


def test_cli_fails_closed_on_missing_fixture_file(tmp_path: Path, capsys: Capsys) -> None:
    exit_code = main(["prog", "--fixture", str(tmp_path / "nope.json")])
    assert exit_code == 2
    out = capsys.readouterr().out
    assert "CORPORATE_ACTION_PIT_FIXTURE_LOAD_FAILED" in out


def test_cli_fails_closed_on_malformed_json(tmp_path: Path, capsys: Capsys) -> None:
    path = tmp_path / "fixture.json"
    path.write_text("{ not json", encoding="utf-8")
    exit_code = main(["prog", "--fixture", str(path)])
    assert exit_code == 2
    out = capsys.readouterr().out
    assert "CORPORATE_ACTION_PIT_FIXTURE_LOAD_FAILED" in out


def test_cli_as_of_override_flows_through(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture()), encoding="utf-8")
    exit_code = main(
        ["prog", "--fixture", str(path), "--as-of", (AVAILABLE - timedelta(seconds=1)).isoformat()]
    )
    assert exit_code == 1


def test_cli_help_exits_zero(capsys: Capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["prog", "--help"])
    assert excinfo.value.code == 0
    assert "--fixture" in capsys.readouterr().out


# -- deep-copy tamper safety: the builder returns fresh dicts each time -------


def test_fixture_helper_does_not_share_mutable_state_between_calls() -> None:
    a = fixture()
    b = fixture()
    a["corporate_actions"][0]["source"] = "mutated"
    assert b["corporate_actions"][0]["source"] == "sec-edgar-form8k"
    _ = copy.deepcopy(a)  # smoke: fixtures remain deep-copyable
