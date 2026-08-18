"""The REAL free current-universe refresh path (offline).

The adapter is exercised through recorded payloads: CI never touches the
network. What is proven here is the safety behaviour around a refresh — a bad
fetch must never destroy a good list — and that a CURRENT list is never
presented as point-in-time history.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_sources import SourceAdapterError
from trading_market_data_core.sp500_current_universe import (
    FREE_SOURCE_ID,
    REFRESH_INTERVAL_DAYS,
    WIKIPEDIA_API_URL,
    Constituent,
    ConstituentSnapshot,
    UniverseStore,
    parse_wikitext_constituents,
    refresh_universe,
)
from trading_market_data_core.sp500_universe_cli import main as universe_main
from trading_market_data_core.trading_readiness import DEFAULT_FRESHNESS, TradingMode

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

WIKITEXT: dict[str, object] = {
    "parse": {
        "wikitext": (
            '{| class="wikitable"\n'
            "! Symbol !! Security !! GICS Sector !! GICS Sub-Industry !! CIK !! Date added\n"
            "|-\n"
            "| AAPL\n| Apple Inc.\n| Information Technology\n"
            "| Technology Hardware\n| 0000320193\n| 1982-11-30\n"
            "|-\n"
            "| MSFT\n| Microsoft\n| Information Technology\n"
            "| Systems Software\n| 0000789019\n| 1994-06-01\n"
            "|}"
        )
    }
}


def _payload(data: object) -> bytes:
    return json.dumps(data).encode("utf-8")


# -- the source adapter --------------------------------------------------------


def test_the_source_is_https_and_a_stable_api_endpoint() -> None:
    """A documented API beats scraping a rendered page that changes shape."""
    assert WIKIPEDIA_API_URL.startswith("https://")
    assert "action=parse" in WIKIPEDIA_API_URL
    assert "format=json" in WIKIPEDIA_API_URL


def test_wikitext_parses_into_a_current_snapshot() -> None:
    snapshot = parse_wikitext_constituents(
        _payload(WIKITEXT), as_of=date(2026, 8, 1), fetched_at=NOW
    )
    assert snapshot.count == 2
    assert snapshot.tickers() == {"AAPL", "MSFT"}
    apple = next(c for c in snapshot.constituents if c.ticker == "AAPL")
    assert apple.security_id == "sec-cik:0000320193", "identity is CIK, never the ticker"
    assert apple.gics_sector == "Information Technology"
    assert apple.date_added == date(1982, 11, 30)


def test_the_snapshot_is_marked_secondary_and_not_history() -> None:
    snapshot = parse_wikitext_constituents(
        _payload(WIKITEXT), as_of=date(2026, 8, 1), fetched_at=NOW
    )
    assert snapshot.source_authority == "PUBLIC_SECONDARY"
    assert snapshot.is_point_in_time_history is False
    assert snapshot.source_id == FREE_SOURCE_ID
    assert any("NOT point-in-time history" in note for note in snapshot.notes)


def test_a_header_row_is_not_a_constituent() -> None:
    snapshot = parse_wikitext_constituents(
        _payload(WIKITEXT), as_of=date(2026, 8, 1), fetched_at=NOW
    )
    assert "SYMBOL" not in snapshot.tickers()


def test_a_non_json_payload_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="did not return JSON"):
        parse_wikitext_constituents(
            b"<html>error page</html>", as_of=date(2026, 8, 1), fetched_at=NOW
        )


def test_a_payload_without_a_table_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="no wikitext table"):
        parse_wikitext_constituents(
            _payload({"parse": {"wikitext": "no table here"}}),
            as_of=date(2026, 8, 1), fetched_at=NOW,
        )


def test_an_unexpected_shape_fails_closed() -> None:
    with pytest.raises(SourceAdapterError):
        parse_wikitext_constituents(
            _payload({"error": "nope"}), as_of=date(2026, 8, 1), fetched_at=NOW
        )


# -- refresh safety ------------------------------------------------------------


def _good(count: int = 500, as_of: date = date(2026, 8, 1)) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_id="SP500", as_of=as_of, fetched_at=NOW, source_id=FREE_SOURCE_ID,
        source_authority="PUBLIC_SECONDARY",
        constituents=tuple(
            Constituent(
                ticker=f"T{index:03d}", name=f"Co {index}",
                security_id=f"sec-cik:{index:010d}",
            )
            for index in range(count)
        ),
    )


def test_a_parsed_but_implausible_snapshot_never_replaces_a_good_list(
    tmp_path: Path,
) -> None:
    """Two constituents is a parse accident, not an index."""
    store = UniverseStore(tmp_path)
    store.accept(_good())
    thin = parse_wikitext_constituents(
        _payload(WIKITEXT), as_of=date(2026, 8, 15), fetched_at=NOW
    )
    current, _, findings = refresh_universe(
        store, thin, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER]
    )
    assert current is not None and current.count == 500
    assert any("refresh rejected" in finding for finding in findings)


def test_a_failed_fetch_retains_the_last_known_good(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path)
    store.accept(_good())
    current, status, findings = refresh_universe(
        store, None, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER]
    )
    assert current is not None and current.count == 500
    assert status.value == "CURRENT_LIST_VALID"
    assert any("keeping the last-known-good" in finding for finding in findings)


def test_monthly_snapshots_accumulate_as_forward_history(tmp_path: Path) -> None:
    """Collection builds OUR history going forward - never backwards."""
    store = UniverseStore(tmp_path)
    store.accept(_good(as_of=date(2026, 6, 1)))
    store.accept(_good(as_of=date(2026, 7, 1)))
    store.accept(_good(as_of=date(2026, 8, 1)))
    assert len(store.snapshots()) == 3
    latest = store.latest()
    assert latest is not None and latest.as_of == date(2026, 8, 1)
    assert latest.is_point_in_time_history is False


def test_refresh_is_not_due_within_the_monthly_interval(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path)
    store.accept(_good(as_of=date(2026, 8, 15)))
    assert not store.due_for_refresh(now=NOW)
    assert REFRESH_INTERVAL_DAYS == 30


def test_refresh_is_due_after_the_interval(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path)
    store.accept(_good(as_of=date(2026, 6, 1)))
    assert store.due_for_refresh(now=NOW)


# -- the owner CLI -------------------------------------------------------------


def test_status_reports_unavailable_without_a_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = universe_main(["prog", "status", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 2
    assert "CURRENT_LIST_UNAVAILABLE" in out
    assert "point-in-time" in out


def test_status_reports_the_snapshot_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    UniverseStore(tmp_path).accept(_good())
    code = universe_main(["prog", "status", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    for expected in (
        "source", "constituent count", "snapshot age", "checksum",
        "last-known-good", "CURRENT_LIST_VALID",
    ):
        assert expected in out


def test_refresh_does_not_refetch_when_not_due(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No network is touched here: the CLI must short-circuit on freshness."""
    UniverseStore(tmp_path).accept(_good(as_of=date(2026, 8, 15)))
    code = universe_main(["prog", "refresh", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "refresh not due" in out


def test_status_is_stricter_for_live_than_paper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    UniverseStore(tmp_path).accept(_good(as_of=date(2026, 6, 25)))
    paper = universe_main(["prog", "status", "--root", str(tmp_path), "--mode", "PAPER"])
    capsys.readouterr()
    live = universe_main(["prog", "status", "--root", str(tmp_path), "--mode", "LIVE"])
    capsys.readouterr()
    assert paper == 1, "stale but usable for paper"
    assert live == 2, "unavailable for live"
