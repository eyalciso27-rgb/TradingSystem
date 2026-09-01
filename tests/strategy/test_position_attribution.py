"""Adversarial tests for conId-based position attribution (mission section 6).

Exact ``con_id`` identity outranks ticker equality; an unknown or unattributable
position blocks a new entry and is never treated as zero exposure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from trading_application.position_attribution import (
    BrokerHeldPosition,
    PositionAttribution,
    PositionAttributionError,
    SystemPositionRecord,
    classify_positions,
)

AS_OF = datetime(2025, 6, 1, 20, 0, tzinfo=UTC)


def _held(con_id: int, symbol: str, notional: str = "10000") -> BrokerHeldPosition:
    return BrokerHeldPosition(
        con_id=con_id, symbol=symbol, quantity=Decimal(100),
        notional=Decimal(notional), observed_at=AS_OF,
    )


def _record(con_id: int, symbol: str, **kw: object) -> SystemPositionRecord:
    return SystemPositionRecord(
        con_id=con_id, symbol=symbol,
        strategy_id=str(kw.get("strategy_id", "baseline-trend-breakout-v1")),
        source_id=str(kw.get("source_id", "scanner")),
        sector=str(kw.get("sector", "TECH")),
        cluster=str(kw.get("cluster", "SEMI")),
        open_risk_amount=Decimal(str(kw.get("open_risk_amount", "250"))),
        notional=Decimal(str(kw.get("notional", "10000"))),
    )


def test_clean_attribution_permits_new_entry() -> None:
    result = classify_positions([_held(111, "AAA")], [_record(111, "AAA")], as_of=AS_OF)
    assert not result.new_entry_blocked
    assert result.block_reason_codes == ()
    assert len(result.attributed_exposures) == 1
    exposure = result.attributed_exposures[0]
    assert exposure.con_id == 111
    assert exposure.sector == "TECH"  # taken from the system record, not invented


def test_empty_portfolio_is_clean() -> None:
    result = classify_positions([], [], as_of=AS_OF)
    assert not result.new_entry_blocked
    assert result.attributed_exposures == ()


def test_unattributable_position_blocks_new_entry() -> None:
    # A broker position with no matching system record: unknown exposure.
    result = classify_positions([_held(222, "BBB")], [], as_of=AS_OF)
    assert result.new_entry_blocked
    assert any(r.startswith("POSITION_UNATTRIBUTABLE") for r in result.block_reason_codes)
    assert result.attributed_exposures == ()  # never a zero-exposure entry
    assert result.classified[0].attribution is PositionAttribution.UNATTRIBUTABLE


def test_conid_outranks_ticker_equality() -> None:
    # Same ticker, DIFFERENT con_id => not the system's position => block.
    result = classify_positions([_held(999, "AAA")], [_record(111, "AAA")], as_of=AS_OF)
    assert result.new_entry_blocked
    codes = result.block_reason_codes
    # The held 999 is unattributable and the recorded 111 is missing at broker.
    assert any(r.startswith("POSITION_UNATTRIBUTABLE_CONID_999") for r in codes)
    assert any(r.startswith("POSITION_MISSING_AT_BROKER_CONID_111") for r in codes)


def test_conid_symbol_mismatch_fails_closed() -> None:
    # Exact con_id matches but the symbols disagree: a corrupt identity.
    result = classify_positions([_held(111, "AAA")], [_record(111, "ZZZ")], as_of=AS_OF)
    assert result.new_entry_blocked
    assert any(r.startswith("CONID_SYMBOL_MISMATCH") for r in result.block_reason_codes)
    assert result.classified[0].attribution is PositionAttribution.CONID_SYMBOL_MISMATCH


def test_system_record_missing_at_broker_blocks() -> None:
    # The system believes it holds a position the broker does not report.
    result = classify_positions([], [_record(111, "AAA")], as_of=AS_OF)
    assert result.new_entry_blocked
    assert any(r.startswith("POSITION_MISSING_AT_BROKER") for r in result.block_reason_codes)


def test_one_unknown_among_attributed_still_blocks() -> None:
    held = [_held(111, "AAA"), _held(222, "BBB")]
    records = [_record(111, "AAA")]  # 222 unattributable
    result = classify_positions(held, records, as_of=AS_OF)
    assert result.new_entry_blocked
    # The known position stays a known exposure; the UNKNOWN one is never listed
    # as a zero-exposure entry — but its mere presence blocks the new entry.
    attributed_conids = {exposure.con_id for exposure in result.attributed_exposures}
    assert attributed_conids == {111}
    assert 222 not in attributed_conids


def test_duplicate_conids_fail_closed() -> None:
    with pytest.raises(PositionAttributionError):
        classify_positions([_held(111, "AAA"), _held(111, "AAA")], [], as_of=AS_OF)
    with pytest.raises(PositionAttributionError):
        classify_positions([], [_record(111, "AAA"), _record(111, "AAA")], as_of=AS_OF)


def test_lookahead_position_is_rejected() -> None:
    future = datetime(2025, 6, 2, tzinfo=UTC)
    late = BrokerHeldPosition(
        con_id=111, symbol="AAA", quantity=Decimal(1),
        notional=Decimal(1), observed_at=future,
    )
    with pytest.raises(PositionAttributionError):
        classify_positions([late], [_record(111, "AAA")], as_of=AS_OF)
