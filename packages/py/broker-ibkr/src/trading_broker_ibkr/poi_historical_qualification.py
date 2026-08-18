"""Owner-run READ-ONLY IBKR historical qualification (POI-IBKR-HIST).

Smallest owner-authorized read-only run that EMPIRICALLY qualifies the owner's
actual IBKR access for the frozen strategy's dataset contract against the
existing PAPER Gateway. It performs NO order, cancel, modify, account-mutation,
or Gateway restart — only the read-only historical/reference requests in
``poi_historical_data_client`` — and writes a sanitized evidence artifact with
checksums.

It never logs in (the owner logs in via the Gateway UI), never reads secrets,
masks the account id, and records only a contract-level conId (not an account
id). A SMALL sample is used to prove schema/coverage — never a bulk S&P download.

Usage (on the owner's controlled host, Gateway logged into PAPER, Read-Only
API = ON, bound to 127.0.0.1:4002):

    uv run --no-sync python -m trading_broker_ibkr.poi_historical_qualification \
        --host 127.0.0.1 --port 4002 \
        --symbols AAPL,MSFT --patterns META \
        --account-alias paper-poi \
        --out docs/poi/evidence/POI-IBKR-HIST.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from trading_broker_ibkr.poi_historical_data_client import (
    HISTORICAL_CLIENT_VERSION,
    WHAT_TO_SHOW,
    IbkrHistoricalRawClient,
    build_historical_client,
)
from trading_broker_ibkr.poi_readonly_session import (
    PAPER_ACCOUNT_PREFIX,
    PoiSessionError,
    mask_account_id,
)

QUALIFICATION_VERSION = "poi-ibkr-hist-qualification-1.0.0"


@dataclass(frozen=True, slots=True)
class SymbolEvidence:
    symbol: str
    con_id: int | None
    long_name: str
    primary_exchange: str
    head_timestamp_trades: str | None
    series: dict[str, dict[str, object]]  # whatToShow -> {row_count, earliest, latest, error}
    trades_schema_ok: bool
    pacing_notes: tuple[str, ...]


@dataclass
class QualificationEvidence:
    poi_id: str = "POI-IBKR-HIST"
    qualification_version: str = QUALIFICATION_VERSION
    client_version: str = HISTORICAL_CLIENT_VERSION
    account_alias: str = ""
    masked_account_id: str = ""
    server_time: str = ""
    symbols: list[SymbolEvidence] = field(default_factory=list)
    matching: list[dict[str, object]] = field(default_factory=list)
    capabilities: dict[str, str] = field(default_factory=dict)  # capability -> PASS/FAIL/PARTIAL
    notes: list[str] = field(default_factory=list)
    # ACTIVE-name daily price qualification only. This is NOT full V1 dataset
    # readiness: PIT S&P 500 membership, as-of GICS, delisted history and
    # corporate actions remain unresolved, so the full contract stays not-ready.
    active_name_price_qualification_sufficient: bool = False
    full_dataset_contract_ready: bool = False  # always False here — reference-layer gaps remain
    observed_at: str = ""
    evidence_checksum: str = ""

    #: Machine-readable typing so the run manifest can BIND this artifact.
    #: The literal strings mirror ArtifactType in market-data-core; a test
    #: asserts they agree, which avoids a package dependency here.
    ARTIFACT_TYPE = "ibkr_historical_evidence"
    ARTIFACT_SCHEMA_VERSION = "dataset-v1-ibkr-evidence-1.0.0"
    CAPABILITY_SCOPE = (
        "bid_ask_midpoint",
        "connectivity_paper_du_gate",
        "contract_identity_conid",
        "daily_trades_rth_schema",
        "head_timestamp_coverage",
        "symbol_change_discovery",
    )

    def typing_fields(self) -> dict[str, object]:
        return {
            "artifact_type": self.ARTIFACT_TYPE,
            "schema_version": self.ARTIFACT_SCHEMA_VERSION,
            "producer": QUALIFICATION_VERSION,
            "capability_scope": list(self.CAPABILITY_SCOPE),
        }

    def body_for_checksum(self) -> dict[str, object]:
        return {
            "poi_id": self.poi_id,
            "qualification_version": self.qualification_version,
            "client_version": self.client_version,
            "account_alias": self.account_alias,
            "masked_account_id": self.masked_account_id,
            "server_time": self.server_time,
            "symbols": [
                {
                    "symbol": s.symbol, "con_id": s.con_id, "long_name": s.long_name,
                    "primary_exchange": s.primary_exchange,
                    "head_timestamp_trades": s.head_timestamp_trades,
                    "series": s.series, "trades_schema_ok": s.trades_schema_ok,
                    "pacing_notes": list(s.pacing_notes),
                }
                for s in self.symbols
            ],
            "matching": self.matching,
            "capabilities": self.capabilities,
            "notes": self.notes,
            "active_name_price_qualification_sufficient": (
                self.active_name_price_qualification_sufficient
            ),
            "full_dataset_contract_ready": self.full_dataset_contract_ready,
            "observed_at": self.observed_at,
        }

    def to_dict(self) -> dict[str, object]:
        # Typing is an ENVELOPE around the observation body. Keeping it out of
        # `evidence_checksum` means migrating a legacy artifact adds typing
        # WITHOUT invalidating the digest that authenticates the observations.
        body = {**self.body_for_checksum(), **self.typing_fields()}
        # `normalized_checksum` is the name every typed artifact uses for a
        # canonical-BODY digest; `evidence_checksum` is retained so existing
        # readers keep working.
        body["evidence_checksum"] = self.evidence_checksum
        body["normalized_checksum"] = self.evidence_checksum
        return body


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def run_qualification(
    client: IbkrHistoricalRawClient,
    *,
    host: str,
    port: int,
    symbols: Sequence[str],
    patterns: Sequence[str],
    account_alias: str,
    now: str | None = None,
    small_duration: str = "10 D",
) -> QualificationEvidence:
    """Run the read-only qualification and return sanitized evidence.

    Fail-closed: a non-DU account raises before any historical request. The
    account id is only ever recorded masked. conId is a contract id, not an
    account id, and is recorded as provenance.
    """
    evidence = QualificationEvidence(account_alias=account_alias, observed_at=now or _now_iso())
    client.open(host, port)
    try:
        evidence.server_time = client.current_time()
        summary = client.account_summary()
        account_id = summary.account_id.strip()
        if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
            raise PoiSessionError(
                f"account {mask_account_id(account_id)} is not a paper account"
                f" (expected {PAPER_ACCOUNT_PREFIX}); fail closed"
            )
        evidence.masked_account_id = mask_account_id(account_id)

        any_trades_ok = False
        any_con_id = False
        for symbol in symbols:
            identity = client.contract_identity(symbol)
            any_con_id = any_con_id or identity.con_id is not None
            head = client.head_timestamp(symbol, "TRADES")
            series: dict[str, dict[str, object]] = {}
            pacing: list[str] = []
            trades_ok = False
            for wts in WHAT_TO_SHOW:
                result = client.historical(
                    symbol, wts, duration=small_duration, use_rth=1
                )
                pacing.extend(result.pacing_notes)
                schema_ok = result.row_count > 0 and all(
                    b.open and b.high and b.low and b.close for b in result.bars_sample
                )
                if wts == "TRADES":
                    trades_ok = schema_ok and all(b.volume for b in result.bars_sample)
                series[wts] = {
                    "row_count": result.row_count,
                    "earliest": result.earliest_bar,
                    "latest": result.latest_bar,
                    "schema_ok": schema_ok,
                    "error": result.error,
                    "request_id": result.request_id,  # unique per request (lifecycle proof)
                }
            any_trades_ok = any_trades_ok or trades_ok
            evidence.symbols.append(
                SymbolEvidence(
                    symbol=symbol, con_id=identity.con_id, long_name=identity.long_name,
                    primary_exchange=identity.primary_exchange,
                    head_timestamp_trades=head.value, series=series,
                    trades_schema_ok=trades_ok, pacing_notes=tuple(pacing),
                )
            )

        for pattern in patterns:
            matches = client.matching_symbols(pattern)
            evidence.matching.append(
                {
                    "pattern": pattern,
                    "match_count": len(matches.matches),
                    "matches": [
                        {"symbol": m.symbol, "con_id": m.con_id, "sec_type": m.sec_type,
                         "primary_exchange": m.primary_exchange}
                        for m in matches.matches
                    ],
                    "error": matches.error,
                }
            )

        evidence.capabilities = {
            "connectivity_paper_du_gate": "PASS",
            "daily_trades_rth_schema": "PASS" if any_trades_ok else "FAIL",
            "contract_identity_conid": "PASS" if any_con_id else "FAIL",
            "head_timestamp_coverage": (
                "PASS" if any(s.head_timestamp_trades for s in evidence.symbols) else "FAIL"
            ),
            "bid_ask_midpoint": _quote_capability(evidence),
            "symbol_change_discovery": (
                "PASS" if any(m["match_count"] for m in evidence.matching) else "PARTIAL"
            ),
        }
        # This run proves ACTIVE-symbol coverage ONLY. It is NOT proof of
        # delisted history, PIT S&P 500 membership, or as-of GICS.
        evidence.notes = [
            "Active-symbol daily TRADES/RTH coverage is what this run can prove.",
            "Delisted-name history, PIT S&P 500 membership and as-of GICS are NOT"
            " tested here and remain external gaps.",
            "Adjustment semantics: compare TRADES vs ADJUSTED_LAST row values on the host.",
            "Commission SCHEDULE is not a read-only API endpoint; read it from IBKR"
            " Account Management (Reports) — this run only proves connectivity.",
        ]
        # ACTIVE-name price qualification only — never the full V1 dataset
        # contract (membership/GICS/delisted/corporate-actions remain gaps).
        evidence.active_name_price_qualification_sufficient = any_trades_ok and any_con_id
        evidence.full_dataset_contract_ready = False
    finally:
        client.close()

    payload = json.dumps(evidence.body_for_checksum(), sort_keys=True, separators=(",", ":"))
    evidence.evidence_checksum = "sha256:" + hashlib.sha256(payload.encode("ascii")).hexdigest()
    return evidence


#: An entitlement / no-data / pacing note is NOT a genuine request failure.
_SOFT_ERROR_MARKERS = (
    "entitle", "no data", "no historical", "requires subscription",
    "not subscribed", "pacing",
)


def _is_soft_error(error: object) -> bool:
    if not isinstance(error, str) or not error.strip():
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in _SOFT_ERROR_MARKERS)


def _quote_capability(evidence: QualificationEvidence) -> str:
    """PASS only if BID+ASK+MIDPOINT are ALL available for at least one symbol.

    PARTIAL if only a subset is available (or nothing is available but only
    because of missing entitlement / empty data). FAIL ONLY when a genuine
    request failure occurred and nothing was retrievable — a missing entitlement
    is never a FAIL.
    """
    families = ("BID", "ASK", "MIDPOINT")
    any_all_three = False
    any_subset = False
    genuine_failure = False
    for symbol in evidence.symbols:
        present: set[str] = set()
        for wts in families:
            entry = symbol.series.get(wts, {})
            row_count = entry.get("row_count", 0)
            if isinstance(row_count, int) and row_count > 0:
                present.add(wts)
                continue
            error = entry.get("error")
            if error and not _is_soft_error(error):
                genuine_failure = True
        if len(present) == len(families):
            any_all_three = True
        elif present:
            any_subset = True
    if any_all_three:
        return "PASS"
    if any_subset:
        return "PARTIAL"
    if genuine_failure:
        return "FAIL"
    return "PARTIAL"  # nothing available, but only entitlement/empty — not a failure


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--symbols", default="AAPL,MSFT")
    parser.add_argument("--patterns", default="META")
    parser.add_argument("--account-alias", default="paper-poi")
    parser.add_argument("--duration", default="10 D")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv[1:])

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    client = build_historical_client()
    try:
        evidence = run_qualification(
            client, host=args.host, port=args.port, symbols=symbols, patterns=patterns,
            account_alias=args.account_alias, small_duration=args.duration,
        )
    except PoiSessionError as error:
        print(f"QUALIFICATION_FAILED: {error}")
        return 1

    for capability, verdict in sorted(evidence.capabilities.items()):
        print(f"{capability}: {verdict}")
    print("active_name_price_qualification_sufficient: "
          f"{evidence.active_name_price_qualification_sufficient}")
    print(f"full_dataset_contract_ready: {evidence.full_dataset_contract_ready}")
    print(f"evidence_checksum: {evidence.evidence_checksum}")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(evidence.to_dict(), indent=2), encoding="utf-8")
        print(f"evidence written: {args.out}")
    fail = any(v == "FAIL" for v in evidence.capabilities.values())
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
