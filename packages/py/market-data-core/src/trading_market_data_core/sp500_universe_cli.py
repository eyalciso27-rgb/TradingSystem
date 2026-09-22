"""Current S&P 500 universe: check freshness, refresh if due (owner command).

ADR-0017. This is the CURRENT operational universe only. It is never point-in-
time history, and historical PIT membership is never substituted for it.

Refresh is monthly by policy, so this does not refetch on every trade: a fresh
snapshot is used as-is, and a refresh is attempted only when one is due. A
failed or implausible refresh leaves the last-known-good snapshot in place.

Usage:
    uv run --no-sync python -m trading_market_data_core.sp500_universe_cli status
    uv run --no-sync python -m trading_market_data_core.sp500_universe_cli refresh
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1_sources import SourceAdapterError
from trading_market_data_core.sp500_current_universe import (
    FREE_SOURCE_ID,
    REFRESH_INTERVAL_DAYS,
    ConstituentSnapshot,
    RawConstituentRefresh,
    UniverseStore,
    classify_freshness,
    fetch_current_constituents_with_raw,
    refresh_universe,
)
from trading_market_data_core.trading_readiness import (
    DEFAULT_FRESHNESS,
    TradingMode,
    UniverseListStatus,
)

DEFAULT_ROOT = Path("docs/poi/evidence/sp500-current")

EXIT_VALID = 0
EXIT_STALE = 1
EXIT_UNAVAILABLE = 2


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _report(
    store: UniverseStore, snapshot: ConstituentSnapshot | None, *, mode: TradingMode,
    now: datetime, findings: tuple[str, ...] = (),
) -> int:
    policy = DEFAULT_FRESHNESS[mode]
    status = classify_freshness(snapshot, now=now, policy=policy)
    print(f"mode                : {mode.value}")
    print(f"status              : {status.value}")
    if snapshot is None:
        print("snapshot            : none stored yet")
        print(f"refresh interval    : {REFRESH_INTERVAL_DAYS} days")
        print(f"source              : {FREE_SOURCE_ID} (PUBLIC_SECONDARY)")
        for finding in findings:
            print(f"  finding: {finding}")
        print(
            "NOTE: this is the CURRENT operational universe only; it is never"
            " point-in-time history."
        )
        return EXIT_UNAVAILABLE
    print(f"source              : {snapshot.source_id} ({snapshot.source_authority})")
    print(f"constituent count   : {snapshot.count}")
    print(f"as_of               : {snapshot.as_of.isoformat()}")
    print(f"snapshot age (days) : {snapshot.age_days(now)}")
    print(f"fetched_at          : {snapshot.fetched_at.isoformat()}")
    print(f"checksum            : {snapshot.checksum}")
    print(f"last-known-good     : {store.root}")
    print(f"point_in_time_history: {snapshot.is_point_in_time_history} (always False here)")
    print(f"gics_mode           : {snapshot.gics_mode} (optional context)")
    for finding in findings:
        print(f"  finding: {finding}")
    if status is UniverseListStatus.CURRENT_LIST_VALID:
        return EXIT_VALID
    return EXIT_STALE if status is UniverseListStatus.CURRENT_LIST_STALE else EXIT_UNAVAILABLE


def _parse_now(value: str) -> datetime:
    """Parse an ISO-8601 instant for --now; naive input is treated as UTC.

    Invalid input raises and argparse turns it into a deterministic usage error.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "refresh"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--mode", choices=("PAPER", "LIVE"), default="PAPER")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--now",
        type=_parse_now,
        default=None,
        help=(
            "Evaluate freshness as of this ISO-8601 UTC instant (default: current "
            "time). For deterministic tests and 'as-of' owner checks; does not "
            "alter stored data."
        ),
    )
    _force_utf8_stdout()
    args = parser.parse_args(argv[1:])

    now: datetime = args.now if args.now is not None else datetime.now(tz=UTC)
    mode = TradingMode(args.mode)
    store = UniverseStore(args.root)

    if args.command == "status":
        return _report(store, store.latest(), mode=mode, now=now)

    if not args.force and not store.due_for_refresh(now=now):
        print("refresh not due; using the existing snapshot")
        return _report(store, store.latest(), mode=mode, now=now)

    candidate: ConstituentSnapshot | RawConstituentRefresh | None = None
    findings: list[str] = []
    try:
        candidate = fetch_current_constituents_with_raw(now=now)
    except SourceAdapterError as error:
        findings.append(f"fetch failed: {error}")
    current, _, refresh_findings = refresh_universe(
        store, candidate, now=now, policy=DEFAULT_FRESHNESS[mode]
    )
    return _report(
        store, current, mode=mode, now=now, findings=tuple(findings) + refresh_findings
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
