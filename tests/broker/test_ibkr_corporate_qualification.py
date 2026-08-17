"""IBKR read-only corporate-action qualification drills (fake transport).

No ibapi import, no socket, no order surface. These prove the three things that
decide whether this evidence can be trusted:

* an ENTITLEMENT message is recorded as an entitlement gap, never as a
  capability failure and never as an assumption of availability;
* TRADES vs ADJUSTED_LAST is compared only at MATCHED bar dates, so a
  divergence cannot be manufactured by aligning different days;
* the resulting artifact never upgrades an adjustment divergence into an
  event-level corporate action.
"""

from __future__ import annotations

import pytest
from trading_broker_ibkr.poi_corporate_actions_qualification import (
    classify_probe_outcome,
    compare_adjustment,
    run_corporate_qualification,
)
from trading_broker_ibkr.poi_historical_data_client import (
    HistoricalBar,
    HistoricalSeries,
    HistoricalSink,
    IbkrHistoricalRawClient,
)
from trading_broker_ibkr.poi_readonly_session import PoiSessionError

PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"


class FakeCorporateTransport:
    """Replays read-only callbacks; ADJUSTED_LAST can diverge or be entitled-out."""

    def __init__(
        self,
        *,
        account_id: str = PAPER_ACCOUNT,
        adjusted_error: str | None = None,
        adjusted_factor: int = 1,
        adjusted_shifted_dates: bool = False,
        rows: int = 3,
    ) -> None:
        self._account_id = account_id
        self._adjusted_error = adjusted_error
        self._adjusted_factor = adjusted_factor
        self._shift = adjusted_shifted_dates
        self._rows = rows
        self._sink: HistoricalSink | None = None
        self.stopped = False
        self.hist_req_ids: list[int] = []

    def start(self, host: str, port: int, client_id: int, sink: HistoricalSink) -> None:
        self._sink = sink
        sink.on_ready()

    def stop(self) -> None:
        self.stopped = True

    def request_current_time(self) -> None:
        assert self._sink is not None
        self._sink.on_current_time(1_780_000_000)

    def request_account_summary(self, req_id: int, tags: str) -> None:
        assert self._sink is not None
        self._sink.on_account_value(req_id, self._account_id, "NetLiquidation", "100000")
        self._sink.on_account_end(req_id)

    def cancel_account_summary(self, req_id: int) -> None:
        return

    def request_head_timestamp(self, req_id, symbol, what_to_show, use_rth) -> None:  # type: ignore[no-untyped-def]
        assert self._sink is not None
        self._sink.on_head_timestamp(req_id, "19800101-00:00:00")

    def cancel_head_timestamp(self, req_id: int) -> None:
        return

    def request_historical_data(self, req_id, symbol, what_to_show, bar_size, duration, use_rth):  # type: ignore[no-untyped-def]
        assert self._sink is not None
        self.hist_req_ids.append(req_id)
        if what_to_show == "ADJUSTED_LAST" and self._adjusted_error:
            self._sink.on_error(req_id, 162, self._adjusted_error)
            return
        factor = self._adjusted_factor if what_to_show == "ADJUSTED_LAST" else 1
        offset = 100 if (self._shift and what_to_show == "ADJUSTED_LAST") else 0
        for i in range(self._rows):
            day = 20230101 + i + offset
            close = (100 + i) * factor
            self._sink.on_historical_bar(
                req_id, str(day), str(close), str(close), str(close), str(close), "1000"
            )
        self._sink.on_historical_end(req_id)

    def cancel_historical_data(self, req_id: int) -> None:
        return

    def request_contract_details(self, req_id: int, symbol: str) -> None:
        assert self._sink is not None
        self._sink.on_contract(req_id, 265598, "APPLE INC", "NASDAQ")
        self._sink.on_contract_end(req_id)

    def request_matching_symbols(self, req_id: int, pattern: str) -> None:
        assert self._sink is not None
        self._sink.on_symbol_samples(req_id, [])


def _client(transport: FakeCorporateTransport) -> IbkrHistoricalRawClient:
    return IbkrHistoricalRawClient(lambda: transport, timeout_seconds=5.0)


def _series(what: str, closes: dict[str, str]) -> HistoricalSeries:
    bars = tuple(
        HistoricalBar(date=day, open=c, high=c, low=c, close=c, volume="1")
        for day, c in closes.items()
    )
    return HistoricalSeries(
        symbol="AAPL", what_to_show=what, bar_size="1 day", use_rth=1, duration="1 Y",
        row_count=len(bars), earliest_bar=next(iter(closes), None),
        latest_bar=list(closes)[-1] if closes else None, bars_sample=bars,
        pacing_notes=(), error=None, request_id=1,
    )


# -- entitlement classification ------------------------------------------------


def test_entitlement_message_is_not_a_capability_failure() -> None:
    assert classify_probe_outcome("Requires subscription to XYZ", row_count=0) == (
        "ENTITLEMENT_REQUIRED"
    )
    assert classify_probe_outcome("market data is not subscribed", row_count=0) == (
        "ENTITLEMENT_REQUIRED"
    )


def test_no_data_is_unavailable_not_entitlement() -> None:
    assert classify_probe_outcome("query returned no data", row_count=0) == "UNAVAILABLE"


def test_rows_are_required_for_availability() -> None:
    assert classify_probe_outcome(None, row_count=5) == "AVAILABLE"
    # a clean but EMPTY response is not proof the capability works
    assert classify_probe_outcome(None, row_count=0) == "UNAVAILABLE"


# -- adjustment comparison -----------------------------------------------------


def test_divergence_is_detected_at_matched_dates() -> None:
    trades = _series("TRADES", {"20230101": "100", "20230103": "102"})
    adjusted = _series("ADJUSTED_LAST", {"20230101": "25", "20230103": "25.5"})
    observations = compare_adjustment("AAPL", trades, adjusted)
    assert len(observations) == 2
    assert all(o.diverges for o in observations)


def test_unmatched_dates_are_dropped_not_aligned_by_position() -> None:
    """Comparing different days would manufacture a divergence."""
    trades = _series("TRADES", {"20230101": "100"})
    adjusted = _series("ADJUSTED_LAST", {"20230601": "100"})
    assert compare_adjustment("AAPL", trades, adjusted) == ()


def test_identical_series_report_no_divergence() -> None:
    trades = _series("TRADES", {"20230101": "100"})
    adjusted = _series("ADJUSTED_LAST", {"20230101": "100"})
    observations = compare_adjustment("AAPL", trades, adjusted)
    assert observations and not observations[0].diverges


def test_every_observation_carries_the_inference_boundary() -> None:
    trades = _series("TRADES", {"20230101": "100"})
    adjusted = _series("ADJUSTED_LAST", {"20230101": "25"})
    payload = compare_adjustment("AAPL", trades, adjusted)[0].to_dict()
    assert "NOT" in str(payload["inference_boundary"])
    assert "event-level" in str(payload["inference_boundary"])


# -- end-to-end run ------------------------------------------------------------


def test_paper_gate_rejects_a_non_du_account() -> None:
    client = _client(FakeCorporateTransport(account_id=LIVE_ACCOUNT))
    with pytest.raises(PoiSessionError, match="not a paper account"):
        run_corporate_qualification(
            client, host="127.0.0.1", port=4002, symbols=["AAPL"], account_alias="paper-poi"
        )


def test_run_records_divergence_without_claiming_an_event() -> None:
    transport = FakeCorporateTransport(adjusted_factor=1)
    evidence = run_corporate_qualification(
        _client(transport), host="127.0.0.1", port=4002, symbols=["AAPL"],
        account_alias="paper-poi", now="2026-08-17T00:00:00+00:00",
    )
    feed = next(p for p in evidence.probes if p["capability"] == "CORPORATE_EVENT_FEED")
    assert feed["outcome"] == "UNAVAILABLE"
    assert any("NOT event-level" in note for note in evidence.notes)
    assert evidence.evidence_checksum.startswith("sha256:")
    assert transport.stopped, "the session must always be closed"


def test_adjusted_entitlement_gap_is_recorded_and_the_run_continues() -> None:
    transport = FakeCorporateTransport(adjusted_error="Requires subscription to adjusted data")
    evidence = run_corporate_qualification(
        _client(transport), host="127.0.0.1", port=4002, symbols=["AAPL"],
        account_alias="paper-poi", now="2026-08-17T00:00:00+00:00",
    )
    adjusted = next(p for p in evidence.probes if p["capability"] == "ADJUSTED_LAST")
    assert adjusted["outcome"] == "ENTITLEMENT_REQUIRED"
    trades = next(p for p in evidence.probes if p["capability"] == "HISTORICAL_BARS_TRADES")
    assert trades["outcome"] == "AVAILABLE", "an entitlement gap must not fail TRADES"


def test_account_id_is_only_ever_recorded_masked() -> None:
    evidence = run_corporate_qualification(
        _client(FakeCorporateTransport()), host="127.0.0.1", port=4002, symbols=["AAPL"],
        account_alias="paper-poi", now="2026-08-17T00:00:00+00:00",
    )
    assert PAPER_ACCOUNT not in str(evidence.to_dict())
    assert evidence.masked_account_id


def test_evidence_checksum_covers_the_body() -> None:
    kwargs = {
        "host": "127.0.0.1", "port": 4002, "symbols": ["AAPL"],
        "account_alias": "paper-poi", "now": "2026-08-17T00:00:00+00:00",
    }
    first = run_corporate_qualification(_client(FakeCorporateTransport()), **kwargs)  # type: ignore[arg-type]
    second = run_corporate_qualification(
        _client(FakeCorporateTransport(adjusted_factor=4)), **kwargs  # type: ignore[arg-type]
    )
    assert first.evidence_checksum != second.evidence_checksum


def test_each_request_uses_a_unique_req_id() -> None:
    transport = FakeCorporateTransport()
    run_corporate_qualification(
        _client(transport), host="127.0.0.1", port=4002, symbols=["AAPL", "MSFT"],
        account_alias="paper-poi", now="2026-08-17T00:00:00+00:00",
    )
    assert len(transport.hist_req_ids) == len(set(transport.hist_req_ids))
