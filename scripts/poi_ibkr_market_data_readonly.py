"""POI-IBKR market-data read-only harness — a POI-MD-001 pre-check.

Purpose: after the OWNER has manually installed the official IBKR Linux
ARM64 IB Gateway, logged in to the PAPER environment (2FA) and enabled the
Gateway's Read-Only API, this script verifies — read-only — WHAT market
data actually arrives for one liquid symbol (e.g. AAPL) on the DU/Paper
account: contract resolution, the received IBKR ``marketDataType``,
bid/ask/last and their sizes, a timestamp, any entitlement/permission
error, and an EXPLICIT canonical freshness classification. It then prints
a PASS/FAIL checklist for the POI-MD-001 evidence file.

This is a market-data pre-check for POI-MD-001; it does NOT make
POI-MD-001 (provider benchmark: primary/fallback, IBKR vs external) PASS,
and it is unrelated to POI-IBKR-001 order execution.

Hard safety rules (ADR-0014; PoI plan v2):

- READ-ONLY: the only session methods this harness may ever call are in
  ``ALLOWED_METHODS`` (connect, current time, account summary for the DU
  gate, contract resolution, market-data request, disconnect). The
  ``ReadOnlyGuard`` refuses every other attribute, so no order can be
  placed, cancelled or globally cancelled even if the underlying client
  exposes it. There is deliberately no order/market-data-mutation method
  anywhere.
- PAPER ONLY: the port must be exactly ``PAPER_API_PORT`` (4002); the live
  API port is rejected. The account id must start with ``DU`` — anything
  else fails closed and disconnects immediately.
- LOCALHOST ONLY: the host must be 127.0.0.1/localhost. No override flag
  exists.
- NO SECRETS: the harness never reads credentials (no env access, no
  prompts) — login happens only in the Gateway UI, by the owner. The
  account id is printed masked; account values are never printed. Public
  market quotes (bid/ask/last of a public symbol) are shown — they are
  public market data, not account or personal data.
- NO AUTOMATIC RETRY: each request is issued exactly once; on any error
  the harness fails closed and disconnects.
- NO SDK COUPLING: no broker SDK is imported here (guardrails allow the
  IBKR SDK only inside the adapter package). The concrete session module
  is supplied at run time via ``--session-module`` and loaded lazily; it
  must expose ``create_session()``. Tests inject a fake session instead.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol

HARNESS_VERSION = "poi-ibkr-market-data-readonly-1.0.0"

ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
PAPER_ACCOUNT_PREFIX = "DU"

#: The ONLY session methods the harness may call. There is no order or
#: cancel method here at all; ReadOnlyGuard refuses anything else.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "connect",
        "req_current_time",
        "req_account_summary",
        "resolve_contract",
        "req_market_data",
        "disconnect",
    }
)

#: IBKR ``marketDataType`` codes (reqMarketDataType / tickReqParams).
IBKR_MARKET_DATA_TYPES: dict[int, str] = {
    1: "REALTIME",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED_FROZEN",
}

#: Canonical freshness classes (Integration spec 7.3): REALTIME,
#: NEAR_REALTIME, DELAYED, STALE, UNKNOWN (UNKNOWN => fail closed). A
#: frozen snapshot is a real-time value that is no longer updating, so it
#: is treated as STALE for a new decision; delayed-frozen likewise.
FRESHNESS_BY_IBKR_TYPE: dict[int, str] = {
    1: "REALTIME",
    2: "STALE",
    3: "DELAYED",
    4: "STALE",
}

#: Explicit market-data modes -> the ``reqMarketDataType`` value requested.
#: ``realtime`` (default) asks for live data; ``delayed`` is an EXPLICIT
#: development/research mode that asks for delayed data and can never yield a
#: POI-MD-001 PASS. There is never a silent fallback between them.
MARKET_DATA_MODES: dict[str, int] = {"realtime": 1, "delayed": 3}

#: In delayed mode, only these IBKR types are accepted, with these labels.
DELAYED_MODE_CLASSES: dict[int, str] = {3: "DELAYED", 4: "DELAYED_FROZEN"}

#: IBKR "displaying delayed data" notices. In delayed mode these are EXPECTED
#: and documented explicitly; they do not block a valid delayed quote. Any
#: other entitlement/permission code is treated as blocking.
DELAYED_DATA_NOTICE_CODES: frozenset[str] = frozenset({"10167", "10168", "10169", "10197"})


class PoiFailure(Exception):
    """Fail-closed PoI failure; carries the checklist reason."""


class ReadOnlyViolation(PoiFailure):
    """A non-allowlisted (potentially mutating) method was requested."""


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Only the account id is used (to gate DU); values are never printed."""

    account_id: str


@dataclass(frozen=True, slots=True)
class ContractResolution:
    """Result of resolving one symbol to a broker contract (read-only).

    ``con_id_present`` records only that a contract id came back — the id
    itself is never stored or printed (no broker-native id leakage).
    """

    symbol: str
    resolved: bool
    con_id_present: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MarketDataQuote:
    """One read-only market-data snapshot for a symbol.

    ``received_at`` is captured by the session at tick time (ISO-8601);
    the harness performs no wall-clock reads itself.
    """

    symbol: str
    market_data_type: int | None
    bid: str | None
    ask: str | None
    last: str | None
    bid_size: str | None
    ask_size: str | None
    last_size: str | None
    received_at: str | None
    entitlement_errors: tuple[str, ...] = ()


class ReadOnlySession(Protocol):
    """Duck-typed broker session; the real one is chosen at PoI run time."""

    def connect(self, host: str, port: int) -> None: ...

    def req_current_time(self) -> str: ...

    def req_account_summary(self) -> AccountSummary: ...

    def resolve_contract(self, symbol: str) -> ContractResolution: ...

    def req_market_data(
        self, symbol: str, requested_market_data_type: int = 1
    ) -> MarketDataQuote: ...

    def disconnect(self) -> None: ...


class ReadOnlyGuard:
    """Hard allowlist proxy: only ``ALLOWED_METHODS`` pass through."""

    def __init__(self, session: object) -> None:
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name: str) -> object:
        if name not in ALLOWED_METHODS:
            raise ReadOnlyViolation(
                f"method {name!r} is not in the read-only allowlist; refusing"
            )
        return getattr(object.__getattribute__(self, "_session"), name)

    def __setattr__(self, name: str, value: object) -> None:
        raise ReadOnlyViolation("the read-only session proxy is immutable")


def mask_account_id(account_id: str) -> str:
    """``DU1234567`` -> ``DU*****67``; never expose the full id."""
    if len(account_id) <= 4:
        return account_id[:2] + "**"
    return account_id[:2] + "*" * (len(account_id) - 4) + account_id[-2:]


def validate_endpoint(host: str, port: int) -> None:
    """Fail closed on anything that is not localhost + the paper API port."""
    if host not in ALLOWED_HOSTS:
        raise PoiFailure(
            f"host {host!r} refused: only 127.0.0.1/localhost is allowed in this PoI"
        )
    if port != PAPER_API_PORT:
        raise PoiFailure(
            f"port {port} refused: only the paper API port {PAPER_API_PORT} is allowed"
            " (the live API port is never allowed)"
        )


def market_data_type_label(market_data_type: int | None) -> str:
    """Human label for an IBKR ``marketDataType`` (UNKNOWN if unrecognized)."""
    if market_data_type is None:
        return "UNKNOWN"
    return IBKR_MARKET_DATA_TYPES.get(market_data_type, "UNKNOWN")


def classify_freshness(market_data_type: int | None) -> str:
    """Map an IBKR ``marketDataType`` to a canonical freshness class.

    Anything unrecognized or missing is UNKNOWN (fail closed).
    """
    if market_data_type is None:
        return "UNKNOWN"
    return FRESHNESS_BY_IBKR_TYPE.get(market_data_type, "UNKNOWN")


@dataclass(slots=True)
class CheckOutcome:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class PoiReport:
    mode: str = "realtime"
    execution_eligible: bool = False
    checks: list[CheckOutcome] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(CheckOutcome(name, passed, detail))

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def result_label(self) -> str:
        """PASS / FAIL, or the development-only PASS_DEVELOPMENT_DELAYED.

        A delayed-mode success is DELIBERATELY never a plain "PASS": it is
        research/development only and is never a POI-MD-001 PASS.
        """
        if not self.passed:
            return "FAIL"
        return "PASS_DEVELOPMENT_DELAYED" if self.mode == "delayed" else "PASS"

    def render(self) -> str:
        lines = [f"POI-IBKR market-data read-only harness {HARNESS_VERSION}"]
        lines.append(f"MODE: {self.mode}")
        for check in self.checks:
            status = "PASS" if check.passed else "FAIL"
            lines.append(f"  [{status}] {check.name}: {check.detail}")
        lines.append(f"RESULT: {self.result_label}")
        lines.append(
            f"EXECUTION_ELIGIBLE: {'true' if self.execution_eligible else 'false'}"
        )
        if self.mode == "delayed":
            lines.append("WARNING: DELAYED DATA - RESEARCH/DEVELOPMENT ONLY.")
            lines.append(
                "NOTE: PASS_DEVELOPMENT_DELAYED is development-only; it is NOT a"
                " POI-MD-001 PASS and never authorizes execution."
            )
        lines.append(
            "(read-only allowlist enforced; no order/cancel API was ever callable;"
            " account id masked, account values withheld; execution stays gated by"
            " POI-IBKR-001 regardless of data freshness)"
        )
        return "\n".join(lines)


def _has_value(value: str | None) -> bool:
    return value is not None and str(value).strip() != ""


def _valid_price(value: str | None) -> bool:
    """A usable quote price: a finite number strictly greater than 0.

    IBKR uses -1 (and sometimes other non-positive sentinels) to mean "no
    value"; None/blank/non-numeric/NaN/Infinity/<=0 are all invalid.
    """
    if not _has_value(value):
        return False
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and number > 0


def _valid_size(value: str | None) -> bool:
    """A size is valid when absent, or a finite number >= 0.

    A size of 0 is valid but does NOT prove a usable quote; a negative or
    non-finite size is invalid.
    """
    if value is None or str(value).strip() == "":
        return True  # sizes are not part of the PASS gate; absence is tolerated
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and number >= 0


def _error_code(entitlement_error: str) -> str:
    """The leading numeric code of an entitlement error string ("354: …")."""
    return entitlement_error.split(":", 1)[0].strip()


def run_market_data_poi(
    session_factory: Callable[[], object],
    *,
    symbol: str = "AAPL",
    host: str = "127.0.0.1",
    port: int = PAPER_API_PORT,
    mode: str = "realtime",
    require_realtime: bool = True,
) -> PoiReport:
    """Execute the read-only market-data checklist; fail closed on any error.

    ``mode`` is ``realtime`` (default) or ``delayed``:

    - ``realtime`` requests ``reqMarketDataType(1)``. With ``require_realtime``
      (the default) the run FAILS unless the canonical freshness is REALTIME —
      a delayed downgrade is never silently accepted. ``require_realtime=False``
      (``--off-hours``) tolerates a FROZEN off-hours snapshot but STILL fails
      on a delayed downgrade (no silent fallback).
    - ``delayed`` requests ``reqMarketDataType(3)`` EXPLICITLY for development/
      research. It accepts only IBKR types 3 (DELAYED) or 4 (DELAYED_FROZEN),
      is always ``execution_eligible=false``, and its success is reported as
      PASS_DEVELOPMENT_DELAYED — never a POI-MD-001 PASS.
    """
    report = PoiReport(mode=mode if mode in MARKET_DATA_MODES else "realtime")
    if mode not in MARKET_DATA_MODES:
        report.record(
            "market_data_mode",
            False,
            f"unknown mode {mode!r}; use one of {sorted(MARKET_DATA_MODES)}",
        )
        return report
    requested_type = MARKET_DATA_MODES[mode]
    validate_endpoint(host, port)
    report.record("endpoint", True, f"{host}:{port} (localhost + paper API port only)")

    guarded: ReadOnlySession = ReadOnlyGuard(session_factory())  # type: ignore[assignment]
    connected = False
    try:
        guarded.connect(host, port)
        connected = True
        report.record("connect", True, "API socket connected")

        current_time = guarded.req_current_time()
        if not str(current_time).strip():
            raise PoiFailure("empty current-time response")
        report.record("api_health", True, "req_current_time answered")

        summary = guarded.req_account_summary()
        account_id = summary.account_id.strip()
        if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
            raise PoiFailure(
                f"account {mask_account_id(account_id)} is not a paper account"
                f" (expected prefix {PAPER_ACCOUNT_PREFIX}); disconnecting immediately"
            )
        report.record("paper_account", True, f"account {mask_account_id(account_id)}")

        resolution = guarded.resolve_contract(symbol)
        if not resolution.resolved or not resolution.con_id_present:
            raise PoiFailure(
                f"contract for {symbol!r} did not resolve"
                f"{': ' + resolution.detail if resolution.detail else ''}"
            )
        report.record(
            "contract_resolution",
            True,
            f"{symbol} resolved (broker contract id present, not shown)",
        )

        quote = guarded.req_market_data(
            symbol, requested_market_data_type=requested_type
        )

        # -- entitlement / permission errors ------------------------------
        # In delayed mode a "displaying delayed data" notice (e.g. 10167) is
        # EXPECTED and documented; it never blocks a valid delayed quote. Any
        # other code is blocking. In realtime mode every entitlement error is
        # blocking (a realtime run must not silently accept a delayed feed).
        if mode == "delayed":
            blocking = [
                error
                for error in quote.entitlement_errors
                if _error_code(error) not in DELAYED_DATA_NOTICE_CODES
            ]
            notices = [
                error
                for error in quote.entitlement_errors
                if _error_code(error) in DELAYED_DATA_NOTICE_CODES
            ]
            report.record(
                "entitlement",
                not blocking,
                (
                    "no blocking entitlement error"
                    if not blocking
                    else f"blocking entitlement error(s): {'; '.join(blocking)}"
                ),
            )
            if notices:
                report.record(
                    "delayed_data_notice",
                    True,
                    "IBKR reports it is DISPLAYING DELAYED DATA"
                    f" ({'; '.join(notices)}); expected in delayed mode,"
                    " non-blocking for a valid quote",
                )
        else:
            report.record(
                "entitlement",
                not quote.entitlement_errors,
                (
                    "no entitlement/permission error"
                    if not quote.entitlement_errors
                    else f"entitlement/permission error(s): {'; '.join(quote.entitlement_errors)}"
                ),
            )

        # -- market data type identified ----------------------------------
        label = market_data_type_label(quote.market_data_type)
        report.record(
            "market_data_type",
            label != "UNKNOWN",
            f"marketDataType={quote.market_data_type} ({label})",
        )

        # -- bid/ask/last VALID (finite and > 0; -1/negative/NaN => FAIL) --
        fields_present = all(_valid_price(v) for v in (quote.bid, quote.ask, quote.last))
        report.record(
            "quote_fields",
            fields_present,
            (
                f"bid={quote.bid} ask={quote.ask} last={quote.last}"
                if fields_present
                else "invalid/missing price(s) (must be finite and > 0;"
                f" -1/negative/NaN rejected): bid={quote.bid} ask={quote.ask}"
                f" last={quote.last}"
            ),
        )
        report.record(
            "quote_sizes",
            all(_valid_size(v) for v in (quote.bid_size, quote.ask_size, quote.last_size)),
            f"bid_size={quote.bid_size} ask_size={quote.ask_size}"
            f" last_size={quote.last_size} (must be >= 0; a size of 0 does not"
            " prove a usable quote)",
        )

        # -- timestamp present (LOCAL capture time, not an exchange stamp) --
        exchange_timestamp = getattr(quote, "exchange_timestamp", None)
        report.record(
            "timestamp",
            _has_value(quote.received_at),
            f"received_at={quote.received_at} (local capture time, NOT an"
            f" exchange timestamp); exchange_timestamp={exchange_timestamp}",
        )

        # -- explicit classification per mode (never a silent fallback) ---
        mdt = quote.market_data_type
        delayed_label = DELAYED_MODE_CLASSES.get(mdt) if mdt is not None else None
        is_delayed_type = delayed_label is not None
        if mode == "delayed":
            # Development/research mode: accept ONLY delayed (3) or
            # delayed-frozen (4). Realtime (or anything else) received while
            # DELAYED was requested is reported explicitly, never reclassified.
            report.record(
                "delayed_classification",
                is_delayed_type,
                (
                    f"delayed-mode class={delayed_label} (marketDataType={mdt})"
                    if is_delayed_type
                    else f"requested DELAYED (type 3) but received marketDataType={mdt}"
                    f" ({label}); reported explicitly, not reclassified"
                ),
            )
            report.execution_eligible = False
        else:
            freshness = classify_freshness(mdt)
            report.record(
                "freshness_classification",
                freshness != "UNKNOWN",
                f"canonical freshness={freshness} (from IBKR {label})",
            )
            if require_realtime:
                report.record(
                    "realtime_expectation",
                    freshness == "REALTIME",
                    (
                        "REALTIME as expected"
                        if freshness == "REALTIME"
                        else f"expected REALTIME but classified {freshness}"
                        " (run during market hours, or pass --off-hours to only observe)"
                    ),
                )
            elif is_delayed_type:
                # Off-hours tolerates a frozen snapshot but NEVER a delayed
                # downgrade — that would be a silent realtime->delayed fallback.
                report.record(
                    "realtime_expectation",
                    False,
                    f"requested REALTIME but received DELAYED ({label});"
                    " no silent fallback to delayed",
                )
            else:
                report.record(
                    "realtime_expectation",
                    True,
                    f"off-hours observe mode: recorded {freshness} without requiring"
                    " REALTIME",
                )
            report.execution_eligible = (
                require_realtime
                and freshness == "REALTIME"
                and not quote.entitlement_errors
                and fields_present
            )
    except PoiFailure as failure:
        report.record("fail_closed", False, str(failure))
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        report.record(
            "fail_closed", False, f"unknown error, failing closed: {type(error).__name__}"
        )
    finally:
        if connected:
            try:
                guarded.disconnect()
                report.record("disconnect", True, "clean disconnect")
            except Exception:  # noqa: BLE001
                report.record("disconnect", False, "disconnect failed")
    return report


def _load_session_factory(module_name: str) -> Callable[[], object]:
    """Late, isolated import of the run-time session module (never at top)."""
    import importlib

    module = importlib.import_module(module_name)
    factory = getattr(module, "create_session", None)
    if factory is None or not callable(factory):
        raise PoiFailure(f"session module {module_name!r} must expose create_session()")
    return factory  # type: ignore[no-any-return]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "POI-IBKR market-data read-only harness (POI-MD-001 pre-check)."
            " Manual run only, AFTER the owner has installed IB Gateway"
            " (official installer), logged in to PAPER in the Gateway UI and"
            " enabled Read-Only API. Never places or cancels orders."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1/localhost only")
    parser.add_argument("--port", type=int, default=PAPER_API_PORT, help="paper API port only")
    parser.add_argument("--symbol", default="AAPL", help="one liquid symbol to probe")
    parser.add_argument(
        "--market-data-mode",
        choices=sorted(MARKET_DATA_MODES),
        default="realtime",
        help=(
            "realtime (default) requests live data and fails on a delayed"
            " downgrade; delayed EXPLICITLY requests delayed data for"
            " development/research (never a POI-MD-001 PASS, never executable)"
        ),
    )
    parser.add_argument(
        "--off-hours",
        action="store_true",
        help=(
            "realtime mode only: tolerate a FROZEN off-hours snapshot without"
            " failing (still fails on a delayed downgrade)"
        ),
    )
    parser.add_argument(
        "--session-module",
        required=True,
        help=(
            "python module exposing create_session(); chosen at PoI run time so no"
            " broker SDK is imported by the harness itself"
        ),
    )
    args = parser.parse_args(argv)

    try:
        factory = _load_session_factory(args.session_module)
    except (PoiFailure, ImportError) as error:
        print(f"FAIL: {error}")
        return 2
    report = run_market_data_poi(
        factory,
        symbol=args.symbol,
        host=args.host,
        port=args.port,
        mode=args.market_data_mode,
        require_realtime=not args.off_hours,
    )
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
