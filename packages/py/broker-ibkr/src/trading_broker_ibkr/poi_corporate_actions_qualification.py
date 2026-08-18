"""IBKR read-only corporate-action qualification (owner host, paper only).

What this proves and — just as importantly — what it does not:

* ``TRADES`` vs ``ADJUSTED_LAST`` over the same window characterises the
  provider's ADJUSTMENT BEHAVIOUR, so the Dataset V1 builder can avoid double
  adjustment. A divergence means an adjustment was applied. It is **NOT**
  event-level corporate-action evidence: it carries no event type, no ratio and
  no ex-date, and this module never upgrades it into one.
* Contract identity (``conId``) and symbol matching support the identity
  crosswalk.
* An event-level corporate-action feed is **probed, never assumed**. The
  read-only historical transport exposes no such endpoint; if the account has no
  entitlement, that is recorded as ``ENTITLEMENT_REQUIRED``/``UNAVAILABLE`` with
  the broker's own message, and qualification continues with SEC.

Safety: read-only verbs only. No order, cancel or account mutation exists in
this path; the paper DU gate is enforced before any request; the live API port
is rejected by the shared endpoint validation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1_ibkr_artifacts import (
    IbkrArtifactError,
    load_and_verify_ibkr_evidence,
)

from trading_broker_ibkr.paper_adapter import PAPER_ACCOUNT_PREFIX, mask_account_id
from trading_broker_ibkr.poi_historical_data_client import (
    HistoricalSeries,
    IbkrHistoricalRawClient,
    build_historical_client,
)
from trading_broker_ibkr.poi_readonly_session import PoiSessionError

CORPORATE_QUALIFICATION_VERSION = "poi-ibkr-corporate-qualification-1.0.0"

#: Broker messages that mean "you are not entitled", not "the request failed".
_ENTITLEMENT_MARKERS = (
    "entitle", "not subscribed", "requires subscription", "no permission",
    "market data is not subscribed", "subscription",
)
#: Broker messages that mean "nothing to return", which is not a failure either.
_NO_DATA_MARKERS = ("no data", "no historical", "query returned no data")


def classify_probe_outcome(error: str | None, *, row_count: int) -> str:
    """Map a broker response to an honest probe outcome.

    An entitlement message is NEVER reported as a capability failure, and a
    successful-but-empty response is not reported as availability.
    """
    if error:
        lowered = error.lower()
        if any(marker in lowered for marker in _ENTITLEMENT_MARKERS):
            return "ENTITLEMENT_REQUIRED"
        if any(marker in lowered for marker in _NO_DATA_MARKERS):
            return "UNAVAILABLE"
        return "UNAVAILABLE"
    return "AVAILABLE" if row_count > 0 else "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AdjustmentObservation:
    """One TRADES vs ADJUSTED_LAST comparison at a matched bar date."""

    symbol: str
    bar_date: str
    trades_close: str
    adjusted_last_close: str
    diverges: bool
    #: Recorded in the artifact so this can never be read as an event ledger.
    inference_boundary: str = (
        "adjusted-price divergence indicates provider adjustment only; it is NOT"
        " event-level corporate-action evidence"
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol, "bar_date": self.bar_date,
            "trades_close": self.trades_close,
            "adjusted_last_close": self.adjusted_last_close,
            "diverges": self.diverges,
            "inference_boundary": self.inference_boundary,
        }


def compare_adjustment(symbol: str, trades: HistoricalSeries, adjusted: HistoricalSeries) -> tuple[
    AdjustmentObservation, ...
]:
    """Compare closes at MATCHED bar dates only.

    Comparing different dates would manufacture a divergence, so unmatched bars
    are dropped rather than aligned by position.
    """
    by_date = {bar.date: bar for bar in adjusted.bars_sample}
    observations: list[AdjustmentObservation] = []
    for bar in trades.bars_sample:
        counterpart = by_date.get(bar.date)
        if counterpart is None:
            continue
        observations.append(
            AdjustmentObservation(
                symbol=symbol, bar_date=bar.date, trades_close=bar.close,
                adjusted_last_close=counterpart.close,
                diverges=bar.close != counterpart.close,
            )
        )
    return tuple(observations)


@dataclass
class CorporateQualificationEvidence:
    """Sanitized, checksummed evidence of what the owner account really exposes."""

    account_alias: str
    observed_at: str
    masked_account_id: str = ""
    server_time: str = ""
    symbols: list[dict[str, object]] = field(default_factory=list)
    adjustment_observations: list[dict[str, object]] = field(default_factory=list)
    probes: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence_checksum: str = ""

    #: See the historical writer: literal strings mirroring ArtifactType.
    ARTIFACT_TYPE = "ibkr_corporate_evidence"
    ARTIFACT_SCHEMA_VERSION = "dataset-v1-ibkr-evidence-1.0.0"

    def typing_fields(self) -> dict[str, object]:
        return {
            "artifact_type": self.ARTIFACT_TYPE,
            "schema_version": self.ARTIFACT_SCHEMA_VERSION,
            "producer": CORPORATE_QUALIFICATION_VERSION,
            # This artifact qualifies NOTHING by itself: it contributes
            # adjustment behaviour and probe outcomes to corporate-actions.
            "capability_scope": [],
        }

    def body_for_checksum(self) -> dict[str, object]:
        return {
            "version": CORPORATE_QUALIFICATION_VERSION,
            "account_alias": self.account_alias,
            "masked_account_id": self.masked_account_id,
            "server_time": self.server_time,
            "symbols": self.symbols,
            "adjustment_observations": self.adjustment_observations,
            "probes": self.probes,
            "notes": self.notes,
            "observed_at": self.observed_at,
        }

    def to_dict(self) -> dict[str, object]:
        """The FINAL typed artifact, sealed with the outer envelope digest.

        See the historical writer: ``evidence_checksum`` authenticates the
        qualification body, ``normalized_checksum`` authenticates the complete
        typed envelope. They are never interchanged.
        """
        from trading_market_data_core.dataset_v1_ibkr_artifacts import (
            seal_typed_ibkr_artifact,
        )

        body = {**self.body_for_checksum(), **self.typing_fields()}
        body["evidence_checksum"] = self.evidence_checksum
        return seal_typed_ibkr_artifact(body)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def run_corporate_qualification(
    client: IbkrHistoricalRawClient,
    *,
    host: str,
    port: int,
    symbols: Sequence[str],
    account_alias: str,
    duration: str = "1 Y",
    now: str | None = None,
) -> CorporateQualificationEvidence:
    """Exercise the read-only IBKR environment for corporate-action support.

    Fail-closed: a non-DU account raises before any market-data request.
    """
    import hashlib

    evidence = CorporateQualificationEvidence(
        account_alias=account_alias, observed_at=now or _now_iso()
    )
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

        trades_outcomes: list[str] = []
        adjusted_outcomes: list[str] = []
        for symbol in symbols:
            identity = client.contract_identity(symbol)
            trades = client.historical(symbol, "TRADES", duration=duration, use_rth=1)
            adjusted = client.historical(symbol, "ADJUSTED_LAST", duration=duration, use_rth=1)
            trades_outcomes.append(
                classify_probe_outcome(trades.error, row_count=trades.row_count)
            )
            adjusted_outcomes.append(
                classify_probe_outcome(adjusted.error, row_count=adjusted.row_count)
            )
            observations = compare_adjustment(symbol, trades, adjusted)
            evidence.adjustment_observations.extend(o.to_dict() for o in observations)
            evidence.symbols.append(
                {
                    "symbol": symbol,
                    "con_id": identity.con_id,
                    "primary_exchange": identity.primary_exchange,
                    "trades_rows": trades.row_count,
                    "adjusted_last_rows": adjusted.row_count,
                    "trades_error": trades.error,
                    "adjusted_last_error": adjusted.error,
                    "trades_request_id": trades.request_id,
                    "adjusted_last_request_id": adjusted.request_id,
                }
            )

        def _best(outcomes: Sequence[str]) -> str:
            if "AVAILABLE" in outcomes:
                return "AVAILABLE"
            if "ENTITLEMENT_REQUIRED" in outcomes:
                return "ENTITLEMENT_REQUIRED"
            return "UNAVAILABLE" if outcomes else "NOT_TESTED"

        diverged = any(o["diverges"] for o in evidence.adjustment_observations)
        evidence.probes = [
            {
                "capability": "HISTORICAL_BARS_TRADES",
                "outcome": _best(trades_outcomes),
                "detail": f"{len(symbols)} symbol(s) requested",
            },
            {
                "capability": "ADJUSTED_LAST",
                "outcome": _best(adjusted_outcomes),
                "detail": (
                    "adjusted series returned"
                    if _best(adjusted_outcomes) == "AVAILABLE"
                    else "adjusted series not returned"
                ),
            },
            {
                "capability": "CONTRACT_IDENTITY_CONID",
                "outcome": (
                    "AVAILABLE"
                    if any(s.get("con_id") for s in evidence.symbols)
                    else "UNAVAILABLE"
                ),
                "detail": "conId resolved from contract details",
            },
            {
                # Probed, never assumed. The read-only historical transport has no
                # event-level endpoint; obtaining one would need an additional
                # IBKR entitlement AND new SDK surface, which is an owner decision.
                "capability": "CORPORATE_EVENT_FEED",
                "outcome": "UNAVAILABLE",
                "detail": (
                    "no event-level corporate-action endpoint is reachable through the"
                    " read-only historical API surface used here; an event feed (e.g."
                    " a fundamentals/WSH entitlement) is not part of this account's"
                    " read-only path. Recorded as observed, not assumed."
                ),
            },
        ]
        evidence.notes = [
            "TRADES vs ADJUSTED_LAST characterises ADJUSTMENT BEHAVIOUR only.",
            "A divergence is NOT event-level corporate-action evidence: no event"
            " type, ratio or ex-date is established by it.",
            f"divergence_observed={diverged}",
            "Event-level corporate actions in this package come from SEC official"
            " filings, not from price adjustment.",
        ]
    finally:
        client.close()

    payload = json.dumps(evidence.body_for_checksum(), sort_keys=True, separators=(",", ":"))
    evidence.evidence_checksum = "sha256:" + hashlib.sha256(payload.encode("ascii")).hexdigest()
    return evidence


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--symbols", default="AAPL,MSFT")
    parser.add_argument("--account-alias", default="paper-poi")
    parser.add_argument("--duration", default="1 Y")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv[1:])

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    client = build_historical_client()
    try:
        evidence = run_corporate_qualification(
            client, host=args.host, port=args.port, symbols=symbols,
            account_alias=args.account_alias, duration=args.duration,
        )
    except PoiSessionError as error:
        print(f"CORPORATE_QUALIFICATION_FAILED: {error}")
        return 1

    for probe in evidence.probes:
        print(f"{probe['capability']}: {probe['outcome']}")
    print(f"adjustment_observations: {len(evidence.adjustment_observations)}")
    print(f"evidence_checksum: {evidence.evidence_checksum}")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(evidence.to_dict(), indent=2), encoding="utf-8")
        # READ BACK and verify immediately: a writer must never emit an artifact
        # that the manifest binder would later reject. Failing here is loud and
        # local instead of surfacing as an unbindable artifact mid-run.
        try:
            verified = load_and_verify_ibkr_evidence(args.out)
        except IbkrArtifactError as error:
            print(f"ARTIFACT_SELF_CHECK_FAILED: {error}")
            return 1
        print(f"evidence written: {args.out}")
        print(f"artifact_type      : {verified.artifact_type.value}")
        print(f"normalized_checksum: {verified.normalized_checksum}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
