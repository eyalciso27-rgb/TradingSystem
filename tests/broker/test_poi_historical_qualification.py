"""POI-IBKR historical qualification drills (fake transport; no ibapi/network).

Proves the read-only historical client + the owner qualification orchestration
end-to-end through an injected fake transport that replays the callback sequence
synchronously, and proves the request-lifecycle correctness (unique reqIds, no
cancel after normal completion, reqId-correlated errors, per-request timeout).
No IBKR SDK is imported and no socket is opened.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from trading_broker_ibkr.poi_historical_data_client import (
    HistoricalSink,
    IbkrHistoricalRawClient,
)
from trading_broker_ibkr.poi_historical_qualification import (
    QualificationEvidence,
    SymbolEvidence,
    _quote_capability,
    main,
    run_qualification,
)
from trading_broker_ibkr.poi_readonly_session import PoiSessionError

PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"


class FakeHistTransport:
    """Replays read-only historical callbacks synchronously, per reqId.

    Records the reqIds it was given and every cancel it observed, so tests can
    prove unique ids, no-cancel-after-completion, and per-request timeout.
    """

    def __init__(
        self,
        *,
        account_id: str = PAPER_ACCOUNT,
        rows: int = 10,
        head: str | None = "19800101-00:00:00",
        con_id: int = 265598,
        pacing_on: str | None = None,
        empty_for: tuple[str, ...] = (),
        matches: int = 1,
        hang_on: tuple[str, ...] = (),
        stray_error_for_previous: tuple[int, str] | None = None,
    ) -> None:
        self._account_id = account_id
        self._rows = rows
        self._head = head
        self._con_id = con_id
        self._pacing_on = pacing_on
        self._empty_for = set(empty_for)
        self._matches = matches
        self._hang_on = set(hang_on)
        self._stray = stray_error_for_previous
        self._sink: HistoricalSink | None = None
        self.started: tuple[str, int, int] | None = None
        self.stopped = False
        self.hist_req_ids: list[int] = []
        self.head_req_ids: list[int] = []
        self.cancels: list[tuple[str, int]] = []

    def start(self, host: str, port: int, client_id: int, sink: HistoricalSink) -> None:
        self.started = (host, port, client_id)
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
        self.cancels.append(("account", req_id))

    def request_head_timestamp(self, req_id, symbol, what_to_show, use_rth) -> None:  # type: ignore[no-untyped-def]
        assert self._sink is not None
        self.head_req_ids.append(req_id)
        if self._head is None:
            self._sink.on_error(req_id, 162, "historical data service error: no head timestamp")
        else:
            self._sink.on_head_timestamp(req_id, self._head)

    def cancel_head_timestamp(self, req_id: int) -> None:
        self.cancels.append(("head", req_id))

    def request_historical_data(self, req_id, symbol, what_to_show, bar_size, duration, use_rth):  # type: ignore[no-untyped-def]
        assert self._sink is not None
        # optionally deliver a LATE error for the PREVIOUS (now-retired) request,
        # before completing this one — must not contaminate this request.
        if self._stray is not None and self.hist_req_ids:
            self._sink.on_error(self.hist_req_ids[-1], *self._stray)
        self.hist_req_ids.append(req_id)
        if what_to_show in self._hang_on:
            return  # never completes -> client must time out and cancel THIS id
        if what_to_show == self._pacing_on:
            self._sink.on_error(req_id, 162, "pacing violation")
            return
        if what_to_show in self._empty_for:
            self._sink.on_historical_end(req_id)
            return
        for i in range(self._rows):
            day = 20200101 + i
            base = 100 + i
            self._sink.on_historical_bar(req_id, str(day), str(base), str(base + 1),
                                         str(base - 1), str(base), str(1000 + i))
        self._sink.on_historical_end(req_id)

    def cancel_historical_data(self, req_id: int) -> None:
        self.cancels.append(("hist", req_id))

    def request_contract_details(self, req_id, symbol) -> None:  # type: ignore[no-untyped-def]
        assert self._sink is not None
        if self._con_id == 0:
            self._sink.on_error(req_id, 200, "No security definition has been found")
            return
        self._sink.on_contract(req_id, self._con_id, f"{symbol} INC", "NASDAQ")
        self._sink.on_contract_end(req_id)

    def request_matching_symbols(self, req_id, pattern) -> None:  # type: ignore[no-untyped-def]
        assert self._sink is not None
        from trading_broker_ibkr.poi_historical_data_client import SymbolMatch

        matches = [
            SymbolMatch(pattern, 1000 + i, "STK", "NASDAQ", "") for i in range(self._matches)
        ]
        self._sink.on_symbol_samples(req_id, matches)


def _client(*, timeout: float = 2.0, **kw: object) -> IbkrHistoricalRawClient:
    transport = FakeHistTransport(**kw)  # type: ignore[arg-type]
    client = IbkrHistoricalRawClient(lambda: transport, timeout_seconds=timeout)
    client._transport_for_test = transport  # type: ignore[attr-defined]
    return client


# -- client-level --------------------------------------------------------------


def test_live_port_is_rejected() -> None:
    with pytest.raises(PoiSessionError, match="LIVE"):
        _client().open("127.0.0.1", 4001)


def test_nonlocal_host_is_rejected() -> None:
    with pytest.raises(PoiSessionError, match="only 127.0.0.1"):
        _client().open("10.0.0.5", 4002)


def test_historical_bars_schema_and_counts() -> None:
    c = _client(rows=5)
    c.open("127.0.0.1", 4002)
    series = c.historical("AAPL", "TRADES", duration="5 D")
    assert series.row_count == 5
    assert series.earliest_bar == "20200101" and series.latest_bar == "20200105"
    assert series.bars_sample[0].open and series.bars_sample[-1].close
    assert series.error is None
    c.close()


def test_head_timestamp_and_pacing_capture() -> None:
    c = _client(head="19800101-00:00:00", pacing_on="MIDPOINT")
    c.open("127.0.0.1", 4002)
    assert c.head_timestamp("AAPL", "TRADES").value == "19800101-00:00:00"
    mid = c.historical("AAPL", "MIDPOINT")
    assert mid.row_count == 0
    assert any("pacing" in n for n in mid.pacing_notes)
    c.close()


def test_contract_identity_and_matching() -> None:
    c = _client(con_id=265598, matches=2)
    c.open("127.0.0.1", 4002)
    ident = c.contract_identity("AAPL")
    assert ident.resolved and ident.con_id == 265598 and ident.primary_exchange == "NASDAQ"
    matches = c.matching_symbols("META")
    assert len(matches.matches) == 2
    c.close()


def test_no_order_surface_on_client() -> None:
    c = _client()
    for surface in ("place_order", "submit", "cancel_order", "reqGlobalCancel", "placeOrder"):
        assert not hasattr(c, surface)


# -- request-lifecycle correctness (the bug fix) ------------------------------


def test_sequential_historical_requests_use_unique_req_ids() -> None:
    c = _client(rows=3)
    transport: FakeHistTransport = c._transport_for_test  # type: ignore[attr-defined]
    c.open("127.0.0.1", 4002)
    results = [c.historical("AAPL", wts) for wts in ("TRADES", "BID", "ASK", "MIDPOINT")]
    ids = [r.request_id for r in results]
    assert len(set(ids)) == len(ids)  # every result carried a distinct reqId
    assert len(set(transport.hist_req_ids)) == len(transport.hist_req_ids)  # and on the wire
    c.close()


def test_completed_finite_request_is_not_cancelled() -> None:
    c = _client(rows=4)
    transport: FakeHistTransport = c._transport_for_test  # type: ignore[attr-defined]
    c.open("127.0.0.1", 4002)
    hist = c.historical("AAPL", "TRADES")
    c.head_timestamp("AAPL", "TRADES")
    # a completed one-shot historical/head request is NEVER cancelled afterwards
    assert ("hist", hist.request_id) not in transport.cancels
    assert not any(kind == "hist" for kind, _ in transport.cancels)
    assert not any(kind == "head" for kind, _ in transport.cancels)
    c.close()


def test_bid_ask_midpoint_complete_independently() -> None:
    c = _client(rows=6)
    c.open("127.0.0.1", 4002)
    got = {wts: c.historical("AAPL", wts) for wts in ("BID", "ASK", "MIDPOINT")}
    assert all(s.row_count == 6 and s.error is None for s in got.values())
    assert len({s.request_id for s in got.values()}) == 3  # independent reqIds
    c.close()


def test_late_error_for_old_reqid_does_not_contaminate_new_request() -> None:
    # a stray 322 for the PREVIOUS (retired) reqId arrives while the next runs
    c = _client(rows=3, stray_error_for_previous=(322, "Duplicate ticker ID for API query"))
    c.open("127.0.0.1", 4002)
    first = c.historical("AAPL", "TRADES")
    second = c.historical("AAPL", "BID")  # a stray error for `first` is delivered here
    assert first.error is None and second.error is None  # neither contaminated
    assert second.row_count == 3
    sink = c._sink
    assert sink is not None
    assert any("322" in note for note in sink.stray_errors)  # recorded as a stray note only
    c.close()


def test_timeout_cancels_only_its_own_request() -> None:
    c = _client(timeout=0.2, rows=3, hang_on=("TRADES",))
    transport: FakeHistTransport = c._transport_for_test  # type: ignore[attr-defined]
    c.open("127.0.0.1", 4002)
    ok = c.historical("AAPL", "BID")  # completes normally, not cancelled
    with pytest.raises(PoiSessionError, match="timed out"):
        c.historical("AAPL", "TRADES")  # hangs -> timeout -> cancels ONLY this id
    hist_cancels = [rid for kind, rid in transport.cancels if kind == "hist"]
    assert len(hist_cancels) == 1  # exactly one cancel...
    assert ok.request_id not in hist_cancels  # ...and NOT the completed request
    c.close()


# -- orchestration -------------------------------------------------------------


def _run(**kw: object) -> QualificationEvidence:
    transport = FakeHistTransport(**kw)  # type: ignore[arg-type]
    client = IbkrHistoricalRawClient(lambda: transport, timeout_seconds=2.0)
    return run_qualification(
        client, host="127.0.0.1", port=4002, symbols=["AAPL", "MSFT"], patterns=["META"],
        account_alias="paper-poi", now="2026-08-13T20:00:00+00:00",
    )


def test_qualification_passes_and_masks_account() -> None:
    ev = _run()
    assert ev.capabilities["connectivity_paper_du_gate"] == "PASS"
    assert ev.capabilities["daily_trades_rth_schema"] == "PASS"
    assert ev.capabilities["contract_identity_conid"] == "PASS"
    assert ev.capabilities["head_timestamp_coverage"] == "PASS"
    assert ev.capabilities["bid_ask_midpoint"] == "PASS"  # BID+ASK+MIDPOINT all return data
    dumped = str(ev.to_dict())
    assert PAPER_ACCOUNT not in dumped
    assert ev.masked_account_id.startswith("DU") and "*" in ev.masked_account_id
    assert ev.evidence_checksum.startswith("sha256:")
    assert ev.symbols[0].con_id == 265598
    # unique reqIds are recorded per whatToShow (lifecycle proof surfaced)
    req_ids = [ev.symbols[0].series[w]["request_id"] for w in ("TRADES", "BID", "ASK", "MIDPOINT")]
    assert len(set(req_ids)) == len(req_ids)


def test_qualification_fails_closed_on_non_paper_account() -> None:
    with pytest.raises(PoiSessionError, match="not a paper account"):
        _run(account_id=LIVE_ACCOUNT)


def test_active_run_is_not_proof_of_delisted_or_membership() -> None:
    ev = _run()
    joined = " ".join(ev.notes).lower()
    assert "delisted" in joined and "membership" in joined and "gics" in joined
    assert ev.active_name_price_qualification_sufficient is True
    assert ev.full_dataset_contract_ready is False
    assert "active-symbol" in joined
    assert "sufficient_for_frozen_strategy" not in str(ev.to_dict())


def test_checksum_is_deterministic() -> None:
    assert _run().evidence_checksum == _run().evidence_checksum


def test_cli_writes_sanitized_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_historical_qualification.build_historical_client",
        lambda: IbkrHistoricalRawClient(lambda: FakeHistTransport(), timeout_seconds=2.0),
    )
    out = tmp_path / "POI-IBKR-HIST.json"
    rc = main(["prog", "--host", "127.0.0.1", "--port", "4002", "--symbols", "AAPL",
               "--patterns", "META", "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert PAPER_ACCOUNT not in text
    assert "sha256:" in text
    assert "full_dataset_contract_ready" in text
    assert "sufficient_for_frozen_strategy" not in text
    printed = capsys.readouterr().out
    assert "daily_trades_rth_schema: PASS" in printed
    assert "full_dataset_contract_ready: False" in printed


# -- bid_ask_midpoint capability semantics ------------------------------------


def _sym(available: dict[str, int], errors: dict[str, str] | None = None) -> SymbolEvidence:
    errs = errors or {}
    series: dict[str, dict[str, object]] = {
        wts: {
            "row_count": available.get(wts, 0), "earliest": None, "latest": None,
            "schema_ok": available.get(wts, 0) > 0, "error": errs.get(wts),
        }
        for wts in ("BID", "ASK", "MIDPOINT")
    }
    return SymbolEvidence(
        symbol="X", con_id=1, long_name="", primary_exchange="", head_timestamp_trades="x",
        series=series, trades_schema_ok=True, pacing_notes=(),
    )


def _ev(*syms: SymbolEvidence) -> QualificationEvidence:
    return QualificationEvidence(symbols=list(syms))


def test_quote_only_midpoint_is_partial() -> None:
    assert _quote_capability(_ev(_sym({"MIDPOINT": 5}))) == "PARTIAL"


def test_quote_bid_ask_without_midpoint_is_partial() -> None:
    assert _quote_capability(_ev(_sym({"BID": 5, "ASK": 5}))) == "PARTIAL"


def test_quote_all_three_is_pass() -> None:
    assert _quote_capability(_ev(_sym({"BID": 5, "ASK": 5, "MIDPOINT": 5}))) == "PASS"


def test_quote_missing_entitlement_is_not_fail() -> None:
    ev = _ev(_sym({}, {"BID": "market data is not subscribed / no entitlement"}))
    assert _quote_capability(ev) == "PARTIAL"


def test_quote_genuine_request_failure_is_fail() -> None:
    ev = _ev(_sym({}, {"BID": "malformed historical response"}))
    assert _quote_capability(ev) == "FAIL"


def test_quote_all_three_on_one_symbol_passes_despite_other_failure() -> None:
    good = _sym({"BID": 5, "ASK": 5, "MIDPOINT": 5})
    bad = _sym({}, {"BID": "malformed historical response"})
    assert _quote_capability(_ev(good, bad)) == "PASS"
