"""Point-in-time Dataset V1 contract.

The contract is deliberately provider-neutral. It models the information a
research/backtest dataset must carry to avoid survivorship and look-ahead bias;
it does not claim that any external source has supplied those fields.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

#: The end of an ``as_of`` calendar day in UTC — the default knowledge cutoff for
#: a daily backtest deciding on that date (it may use any fact KNOWN by then).
_UTC_MAX = datetime.max.replace(tzinfo=UTC)


def end_of_day_utc(as_of: date) -> datetime:
    return datetime.combine(as_of, time.max, tzinfo=UTC)


class CapabilityState(StrEnum):
    """Evidence state for one Dataset V1 capability."""

    EMPIRICALLY_VERIFIED = "EMPIRICALLY_VERIFIED"
    CONTRACT_READY = "CONTRACT_READY"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"


class CorporateActionType(StrEnum):
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SPECIAL_DIVIDEND = "SPECIAL_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    MERGER = "MERGER"
    ACQUISITION = "ACQUISITION"
    SPINOFF = "SPINOFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    EXCHANGE_CHANGE = "EXCHANGE_CHANGE"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


#: Action types whose ``ratio`` is mandatory and must be > 0.
RATIO_ACTIONS = frozenset({CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT})

#: Action types whose ``cash_amount`` is mandatory and must be >= 0.
CASH_ACTIONS = frozenset(
    {CorporateActionType.CASH_DIVIDEND, CorporateActionType.SPECIAL_DIVIDEND}
)

#: Action types that terminate the predecessor into a successor identity.
IDENTITY_TRANSITION_ACTIONS = frozenset(
    {CorporateActionType.MERGER, CorporateActionType.ACQUISITION}
)


class DelistingReason(StrEnum):
    """Why a listing ended. ``UNKNOWN`` is explicit — never inferred."""

    ACQUIRED = "ACQUIRED"
    MERGED = "MERGED"
    BANKRUPTCY = "BANKRUPTCY"
    LIQUIDATION = "LIQUIDATION"
    NON_COMPLIANCE = "NON_COMPLIANCE"
    GOING_PRIVATE = "GOING_PRIVATE"
    EXCHANGE_TRANSFER = "EXCHANGE_TRANSFER"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class SecurityLifecycleState(StrEnum):
    """What a backtest may conclude about a security on a given date.

    ``UNKNOWN`` is a first-class answer: missing price data is NEVER silently
    interpreted as a delisting, and a security with no listing information is not
    assumed to have existed.
    """

    NOT_YET_EXISTED = "NOT_YET_EXISTED"
    ACTIVE = "ACTIVE"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


class PriceAdjustmentStatus(StrEnum):
    """How a price series relates to the corporate-action ledger.

    The Dataset Builder must know this to avoid applying split/dividend
    adjustments twice. ``UNKNOWN`` is fail-closed: an unlabelled series may not
    be adjusted, because double adjustment cannot be ruled out.
    """

    RAW_UNADJUSTED = "RAW_UNADJUSTED"
    PROVIDER_ADJUSTED = "PROVIDER_ADJUSTED"
    INTERNALLY_ADJUSTED = "INTERNALLY_ADJUSTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    observed_at: datetime
    source_record_id: str | None = None

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.source.strip():
            errors.append("provenance.source must be non-empty")
        if self.observed_at.tzinfo is None:
            errors.append("provenance.observed_at must be timezone-aware")
        return tuple(errors)


@dataclass(frozen=True, slots=True)
class EffectiveInterval:
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be >= valid_from")

    def contains(self, as_of: date) -> bool:
        return self.valid_from <= as_of and (
            self.valid_to is None or as_of <= self.valid_to
        )


@dataclass(frozen=True, slots=True)
class SecurityIdentity:
    security_id: str
    symbol: str
    primary_exchange: str
    currency: str = "USD"
    ibkr_con_id: int | None = None


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """A bitemporal index-membership assertion.

    Two time axes are carried: EFFECTIVE validity (``interval`` — when the
    security genuinely was a member) and KNOWLEDGE/transaction time
    (``known_at`` — when this assertion first became knowable). A later
    correction supersedes an earlier assertion via ``supersedes`` and, because
    it carries a later ``known_at``, is invisible to any as-of query whose
    knowledge cutoff precedes it — restatements never leak backwards.
    """

    record_id: str
    index_id: str
    security_id: str
    interval: EffectiveInterval
    known_at: datetime
    provenance: Provenance
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("MembershipRecord.known_at must be timezone-aware")

    @property
    def bitemporal_key(self) -> tuple[str, ...]:
        return (self.index_id, self.security_id)


@dataclass(frozen=True, slots=True)
class GicsRecord:
    """A bitemporal as-of GICS classification assertion (see MembershipRecord)."""

    record_id: str
    security_id: str
    sector: str
    industry_group: str | None
    industry: str | None
    sub_industry: str | None
    interval: EffectiveInterval
    known_at: datetime
    provenance: Provenance
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("GicsRecord.known_at must be timezone-aware")

    @property
    def bitemporal_key(self) -> tuple[str, ...]:
        return (self.security_id,)


@dataclass(frozen=True, slots=True)
class SymbolHistoryRecord:
    """A ticker an identity traded under during an interval (bitemporal).

    Symbols are an ATTRIBUTE of a security, never its identity: a rename adds a
    new symbol interval under the SAME ``security_id``.
    """

    record_id: str
    security_id: str
    symbol: str
    interval: EffectiveInterval
    known_at: datetime
    provenance: Provenance
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("SymbolHistoryRecord.known_at must be timezone-aware")

    @property
    def bitemporal_key(self) -> tuple[str, ...]:
        return (self.security_id,)


@dataclass(frozen=True, slots=True)
class DelistingRecord:
    """A bitemporal listing-lifecycle assertion for one security.

    A delisted security stays a first-class historical entity: the record is
    never removed, and the security remains resolvable by ``security_id``.
    ``last_trading_date`` is distinguished from ``delisted_on`` where the source
    can tell them apart; both are optional facts, never inferred.
    """

    record_id: str
    security_id: str
    delisted_on: date
    known_at: datetime
    provenance: Provenance
    reason: DelistingReason = DelistingReason.UNKNOWN
    listing_date: date | None = None
    last_trading_date: date | None = None
    venue: str | None = None
    successor_security_id: str | None = None
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("DelistingRecord.known_at must be timezone-aware")

    @property
    def bitemporal_key(self) -> tuple[str, ...]:
        return (self.security_id,)

    @property
    def interval(self) -> EffectiveInterval:
        """Listing lifetime as an effective interval (for overlap checking)."""
        return EffectiveInterval(self.listing_date or date.min, self.delisted_on)


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    """One bitemporal, event-level corporate action.

    Monetary values and ratios are ``Decimal`` (market-data-core convention).
    Optional date fields (ex/record/payment) are recorded only when the source
    supplies them — absent fields are never invented.
    """

    record_id: str
    security_id: str
    action_type: CorporateActionType
    effective_on: date
    known_at: datetime
    provenance: Provenance
    ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None
    ex_date: date | None = None
    record_date: date | None = None
    payment_date: date | None = None
    predecessor_security_id: str | None = None
    successor_security_id: str | None = None
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("CorporateActionRecord.known_at must be timezone-aware")

    @property
    def bitemporal_key(self) -> tuple[str, ...]:
        # Two different action types on one day are distinct facts, so the key
        # includes the type: only same-type same-day duplicates are conflicts.
        return (self.security_id, self.action_type.value)

    @property
    def interval(self) -> EffectiveInterval:
        """A point event, modelled as a single-day interval for overlap checks."""
        return EffectiveInterval(self.effective_on, self.effective_on)


@dataclass(frozen=True, slots=True)
class RetractionRecord:
    """A tombstone: an earlier assertion was determined never to have been valid.

    This is NOT a zero-length interval hack — the retracted record keeps its
    immutable identity and stays in the dataset, but from ``known_at`` onward it
    is no longer treated as valid. A backtest whose knowledge cutoff precedes
    ``known_at`` still sees exactly what was believed then. Reusable across
    membership, GICS, symbol history, delistings and corporate actions.
    """

    record_id: str
    target_record_id: str
    known_at: datetime
    provenance: Provenance
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("RetractionRecord.known_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DailyPriceRecord:
    """A daily price observation with EXPLICIT adjustment provenance.

    ``adjustment_status`` tells the Dataset Builder whether the series is raw,
    provider-adjusted or internally adjusted, so corporate-action adjustments are
    never applied twice. It defaults to ``UNKNOWN`` (fail-closed): an unlabelled
    series may not be adjusted.
    """

    security_id: str
    trading_date: date
    close: Decimal
    provenance: Provenance
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    adjusted_close: Decimal | None = None
    ibkr_con_id: int | None = None
    adjustment_status: PriceAdjustmentStatus = PriceAdjustmentStatus.UNKNOWN
    #: Fingerprint of the corporate-action ledger already applied to this series
    #: (only meaningful when internally adjusted); prevents re-application.
    adjusted_with_ledger: str | None = None


class _Bitemporal(Protocol):
    """Structural type for a bitemporal reference assertion (read-only)."""

    @property
    def record_id(self) -> str: ...

    @property
    def known_at(self) -> datetime: ...

    @property
    def supersedes(self) -> str | None: ...

    @property
    def bitemporal_key(self) -> tuple[str, ...]: ...


def resolve_visible[B: _Bitemporal](
    records: Sequence[B],
    known_as_of: datetime,
    retractions: Sequence[RetractionRecord] = (),
) -> tuple[B, ...]:
    """The current assertions known by ``known_as_of``.

    Resolution order is deterministic and defined:

    1. only assertions with ``known_at <= known_as_of`` are visible (a
       correction reported LATER than the cutoff does not exist yet, so a past
       decision can never see a future restatement);
    2. any assertion targeted by a retraction visible at the cutoff is dropped —
       it was determined never to have been valid;
    3. any assertion superseded by a SURVIVING visible correction is dropped.

    Because retraction is applied BEFORE supersession, retracting a correction
    restores the record that correction had superseded — the intuitive result,
    and the reason the two mechanisms compose unambiguously.
    """
    if known_as_of.tzinfo is None:
        raise ValueError("known_as_of must be timezone-aware")
    for retraction in retractions:
        if retraction.known_at.tzinfo is None:
            raise ValueError("retraction.known_at must be timezone-aware")
    visible = [record for record in records if record.known_at <= known_as_of]
    retracted = {
        retraction.target_record_id
        for retraction in retractions
        if retraction.known_at <= known_as_of
    }
    surviving = [record for record in visible if record.record_id not in retracted]
    superseded = {
        record.supersedes for record in surviving if record.supersedes is not None
    }
    return tuple(record for record in surviving if record.record_id not in superseded)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> None:
        if self.errors:
            raise ValueError("Dataset V1 validation failed: " + "; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class DatasetV1:
    securities: tuple[SecurityIdentity, ...] = ()
    memberships: tuple[MembershipRecord, ...] = ()
    gics: tuple[GicsRecord, ...] = ()
    delistings: tuple[DelistingRecord, ...] = ()
    corporate_actions: tuple[CorporateActionRecord, ...] = ()
    prices: tuple[DailyPriceRecord, ...] = ()
    symbol_history: tuple[SymbolHistoryRecord, ...] = ()
    retractions: tuple[RetractionRecord, ...] = ()

    def members_as_of(
        self, index_id: str, as_of: date, *, known_as_of: datetime | None = None
    ) -> frozenset[str]:
        """Members of ``index_id`` effective on ``as_of``, per knowledge available
        by ``known_as_of`` (default: end of the ``as_of`` day, UTC).

        Enforces BOTH axes: event-time (the interval must contain ``as_of``) and
        knowledge-time (the assertion must have been known by ``known_as_of``),
        so a later restatement cannot change a historical answer.
        """
        cutoff = known_as_of if known_as_of is not None else end_of_day_utc(as_of)
        return frozenset(
            row.security_id
            for row in resolve_visible(self.memberships, cutoff, self.retractions)
            if row.index_id == index_id and row.interval.contains(as_of)
        )

    def gics_as_of(
        self, security_id: str, as_of: date, *, known_as_of: datetime | None = None
    ) -> GicsRecord | None:
        """As-of GICS classification, enforcing event-time AND knowledge-time."""
        cutoff = known_as_of if known_as_of is not None else end_of_day_utc(as_of)
        matches = [
            row
            for row in resolve_visible(self.gics, cutoff, self.retractions)
            if row.security_id == security_id and row.interval.contains(as_of)
        ]
        if len(matches) > 1:
            raise ValueError(
                f"conflicting GICS classifications for {security_id} on {as_of}"
            )
        return matches[0] if matches else None

    # -- lifecycle / delisting -------------------------------------------------

    def delisting_as_of(
        self, security_id: str, *, known_as_of: datetime
    ) -> DelistingRecord | None:
        """The current delisting assertion for a security at a knowledge cutoff."""
        matches = [
            row
            for row in resolve_visible(self.delistings, known_as_of, self.retractions)
            if row.security_id == security_id
        ]
        if len(matches) > 1:
            raise ValueError(f"conflicting delisting assertions for {security_id}")
        return matches[0] if matches else None

    def is_delisted_as_of(
        self, security_id: str, as_of: date, *, known_as_of: datetime | None = None
    ) -> bool:
        """True only if a delisting KNOWN by the cutoff is effective on ``as_of``."""
        cutoff = known_as_of if known_as_of is not None else end_of_day_utc(as_of)
        record = self.delisting_as_of(security_id, known_as_of=cutoff)
        return record is not None and record.delisted_on <= as_of

    def lifecycle_state_as_of(
        self, security_id: str, as_of: date, *, known_as_of: datetime | None = None
    ) -> SecurityLifecycleState:
        """Distinguish not-yet-existed / active / delisted / unknown.

        Missing price data is NEVER interpreted as a delisting: without listing
        information the answer is ``UNKNOWN``, not ``NOT_YET_EXISTED``.
        """
        if not any(identity.security_id == security_id for identity in self.securities):
            return SecurityLifecycleState.UNKNOWN
        cutoff = known_as_of if known_as_of is not None else end_of_day_utc(as_of)
        record = self.delisting_as_of(security_id, known_as_of=cutoff)
        if record is None:
            return SecurityLifecycleState.UNKNOWN  # no lifecycle facts known yet
        if record.listing_date is not None and as_of < record.listing_date:
            return SecurityLifecycleState.NOT_YET_EXISTED
        if as_of >= record.delisted_on:
            return SecurityLifecycleState.DELISTED
        return SecurityLifecycleState.ACTIVE

    # -- symbol history (attribute, never identity) ---------------------------

    def symbol_as_of(
        self, security_id: str, as_of: date, *, known_as_of: datetime | None = None
    ) -> str | None:
        cutoff = known_as_of if known_as_of is not None else end_of_day_utc(as_of)
        matches = [
            row
            for row in resolve_visible(self.symbol_history, cutoff, self.retractions)
            if row.security_id == security_id and row.interval.contains(as_of)
        ]
        if len(matches) > 1:
            raise ValueError(f"conflicting symbol history for {security_id} on {as_of}")
        return matches[0].symbol if matches else None

    def resolve_security_by_symbol_as_of(
        self, symbol: str, as_of: date, *, known_as_of: datetime | None = None
    ) -> frozenset[str]:
        """Historical symbol -> security_id(s). Never uses today's symbol map."""
        cutoff = known_as_of if known_as_of is not None else end_of_day_utc(as_of)
        return frozenset(
            row.security_id
            for row in resolve_visible(self.symbol_history, cutoff, self.retractions)
            if row.symbol == symbol and row.interval.contains(as_of)
        )

    # -- corporate actions -----------------------------------------------------

    def corporate_actions_for(
        self,
        security_id: str,
        *,
        known_as_of: datetime,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CorporateActionRecord, ...]:
        """Events for a security in an effective-date range, at a knowledge cutoff."""
        events = [
            row
            for row in resolve_visible(
                self.corporate_actions, known_as_of, self.retractions
            )
            if row.security_id == security_id
            and (start is None or row.effective_on >= start)
            and (end is None or row.effective_on <= end)
        ]
        return tuple(
            sorted(events, key=lambda row: (row.effective_on, row.action_type.value))
        )

    def successors_of(
        self, security_id: str, *, known_as_of: datetime
    ) -> frozenset[str]:
        """Successor identities asserted by visible identity-transition events.

        Lineage is EXPOSED, never auto-applied: the Dataset Builder decides
        whether to stitch, so price histories are not blindly concatenated.
        """
        return frozenset(
            row.successor_security_id
            for row in resolve_visible(
                self.corporate_actions, known_as_of, self.retractions
            )
            if row.security_id == security_id and row.successor_security_id is not None
        )

    def validate(self) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        identities = {identity.security_id: identity for identity in self.securities}
        if len(identities) != len(self.securities):
            errors.append("duplicate security_id in securities")
        for identity in self.securities:
            if not identity.security_id.strip():
                errors.append("security identity has empty security_id")
            if not identity.symbol.strip():
                errors.append(f"security {identity.security_id} has empty symbol")
            if not identity.primary_exchange.strip():
                errors.append(f"security {identity.security_id} has empty primary_exchange")

        def require_identity(kind: str, security_id: str) -> None:
            if security_id not in identities:
                errors.append(f"{kind} references unknown security_id={security_id}")

        provenance_rows: list[tuple[str, str, Provenance]] = []
        for membership in self.memberships:
            require_identity("membership", membership.security_id)
            provenance_rows.append(
                ("membership", membership.security_id, membership.provenance)
            )
        for gics_row in self.gics:
            require_identity("gics", gics_row.security_id)
            if not gics_row.sector.strip():
                errors.append(f"gics sector missing for {gics_row.security_id}")
            provenance_rows.append(("gics", gics_row.security_id, gics_row.provenance))
        for symbol_row in self.symbol_history:
            require_identity("symbol_history", symbol_row.security_id)
            if not symbol_row.symbol.strip():
                errors.append(f"symbol_history has empty symbol for {symbol_row.security_id}")
            provenance_rows.append(
                ("symbol_history", symbol_row.security_id, symbol_row.provenance)
            )
        for delisting in self.delistings:
            require_identity("delisting", delisting.security_id)
            if delisting.successor_security_id is not None:
                require_identity("delisting successor", delisting.successor_security_id)
                if delisting.successor_security_id == delisting.security_id:
                    errors.append(
                        f"delisting {delisting.record_id}: successor cannot be itself"
                    )
            if (
                delisting.listing_date is not None
                and delisting.listing_date > delisting.delisted_on
            ):
                errors.append(
                    f"delisting {delisting.record_id}: listing_date after delisted_on"
                )
            if (
                delisting.last_trading_date is not None
                and delisting.last_trading_date > delisting.delisted_on
            ):
                errors.append(
                    f"delisting {delisting.record_id}: last_trading_date after delisted_on"
                )
            provenance_rows.append(
                ("delisting", delisting.security_id, delisting.provenance)
            )
        for action in self.corporate_actions:
            require_identity("corporate_action", action.security_id)
            for label, related in (
                ("successor", action.successor_security_id),
                ("predecessor", action.predecessor_security_id),
            ):
                if related is None:
                    continue
                require_identity(f"corporate_action {label}", related)
                if related == action.security_id:
                    errors.append(
                        f"corporate_action {action.record_id}: {label} cannot be itself"
                    )
            if action.action_type in RATIO_ACTIONS and (
                action.ratio is None or action.ratio <= 0
            ):
                errors.append(
                    f"{action.action_type.value} ratio must be > 0 for {action.security_id}"
                )
            if action.action_type in CASH_ACTIONS:
                if action.cash_amount is None or action.cash_amount < 0:
                    errors.append(
                        f"{action.action_type.value} cash_amount must be >= 0 for"
                        f" {action.security_id}"
                    )
                elif not (action.currency or "").strip():
                    errors.append(
                        f"{action.action_type.value} requires a currency for"
                        f" {action.security_id}"
                    )
            if (
                action.action_type in IDENTITY_TRANSITION_ACTIONS
                and action.successor_security_id is None
            ):
                errors.append(
                    f"{action.action_type.value} {action.record_id} must name a successor"
                )
            ordered_dates = [
                (name, value)
                for name, value in (
                    ("ex_date", action.ex_date),
                    ("record_date", action.record_date),
                    ("payment_date", action.payment_date),
                )
                if value is not None
            ]
            for (earlier_name, earlier), (later_name, later) in zip(
                ordered_dates, ordered_dates[1:], strict=False
            ):
                if later < earlier:
                    errors.append(
                        f"corporate_action {action.record_id}: {later_name} before"
                        f" {earlier_name}"
                    )
            provenance_rows.append(("corporate_action", action.security_id, action.provenance))
        seen_prices: set[tuple[str, date]] = set()
        for price in self.prices:
            require_identity("price", price.security_id)
            key = (price.security_id, price.trading_date)
            if key in seen_prices:
                errors.append(
                    f"duplicate price for security_id={price.security_id} date={price.trading_date}"
                )
            seen_prices.add(key)
            if price.close <= 0:
                errors.append(f"price close must be > 0 for {price.security_id}")
            if price.volume is not None and price.volume < 0:
                errors.append(f"price volume must be >= 0 for {price.security_id}")
            # Double-adjustment protection: a RAW series cannot carry an adjusted
            # value, and an internally adjusted series must name the ledger it was
            # adjusted with so the same actions are never applied twice.
            if (
                price.adjustment_status is PriceAdjustmentStatus.RAW_UNADJUSTED
                and price.adjusted_close is not None
            ):
                errors.append(
                    f"price for {price.security_id} is RAW_UNADJUSTED but carries an"
                    " adjusted_close; adjustment provenance is contradictory"
                )
            if price.adjustment_status is PriceAdjustmentStatus.INTERNALLY_ADJUSTED and not (
                price.adjusted_with_ledger or ""
            ).strip():
                errors.append(
                    f"price for {price.security_id} is INTERNALLY_ADJUSTED but names no"
                    " adjusted_with_ledger fingerprint (double adjustment cannot be ruled out)"
                )
            if (
                price.adjustment_status is not PriceAdjustmentStatus.INTERNALLY_ADJUSTED
                and price.adjusted_with_ledger is not None
            ):
                errors.append(
                    f"price for {price.security_id} names an adjusted_with_ledger but is"
                    f" {price.adjustment_status.value}"
                )
            provenance_rows.append(("price", price.security_id, price.provenance))

        errors.extend(self._retraction_errors())
        errors.extend(self._identity_graph_errors())

        for kind, security_id, provenance in provenance_rows:
            for error in provenance.validate():
                errors.append(f"{kind} {security_id}: {error}")

        errors.extend(_bitemporal_errors("membership", self.memberships, self.retractions))
        errors.extend(_bitemporal_errors("gics", self.gics, self.retractions))
        errors.extend(
            _bitemporal_errors("symbol_history", self.symbol_history, self.retractions)
        )
        errors.extend(_bitemporal_errors("delisting", self.delistings, self.retractions))
        errors.extend(
            _bitemporal_errors("corporate_action", self.corporate_actions, self.retractions)
        )

        membership_ids = {membership.security_id for membership in self.memberships}
        delisted_ids = {delisting.security_id for delisting in self.delistings}
        missing_delisted_history = membership_ids.intersection(identities) - delisted_ids
        if membership_ids and missing_delisted_history:
            warnings.append(
                "delisting coverage is not proven for all historical members; "
                "absence of a delisting row is not evidence that a security survived"
            )
        return ValidationReport(tuple(errors), tuple(warnings))

    # -- retraction / identity-graph integrity --------------------------------

    def _revisionable_ids(self) -> set[str]:
        return {
            record.record_id
            for group in (
                self.memberships, self.gics, self.symbol_history,
                self.delistings, self.corporate_actions,
            )
            for record in group
        }

    def _retraction_errors(self) -> list[str]:
        """Retraction chains must be well-formed and acyclic."""
        errors: list[str] = []
        targets = self._revisionable_ids()
        retraction_ids: set[str] = set()
        seen_targets: set[str] = set()
        for retraction in self.retractions:
            if retraction.record_id in retraction_ids:
                errors.append(f"duplicate retraction record_id={retraction.record_id}")
            retraction_ids.add(retraction.record_id)
            for error in retraction.provenance.validate():
                errors.append(f"retraction {retraction.record_id}: {error}")
            if retraction.target_record_id == retraction.record_id:
                errors.append(
                    f"retraction {retraction.record_id} targets itself (cycle)"
                )
            if retraction.target_record_id in seen_targets:
                errors.append(
                    f"duplicate retraction of record {retraction.target_record_id}"
                )
            seen_targets.add(retraction.target_record_id)
        for retraction in self.retractions:
            if retraction.target_record_id in retraction_ids:
                # Retracting a retraction is not modelled: it would make
                # resolution order ambiguous. Fail closed rather than guess.
                errors.append(
                    f"retraction {retraction.record_id} targets another retraction"
                    f" {retraction.target_record_id}; unsupported (fail closed)"
                )
            elif retraction.target_record_id not in targets:
                errors.append(
                    f"retraction {retraction.record_id} targets unknown record"
                    f" {retraction.target_record_id}"
                )
        return errors

    def _identity_graph_errors(self) -> list[str]:
        """Successor/predecessor transitions must be consistent and acyclic."""
        errors: list[str] = []
        successors: dict[str, set[str]] = {}
        # judged over current survivors: a retracted or superseded event is
        # not a contradiction.
        for action in resolve_visible(self.corporate_actions, _UTC_MAX, self.retractions):
            if action.successor_security_id is None:
                continue
            # A merger/acquisition terminates the predecessor: two DIFFERENT
            # terminal successors for one security is contradictory. A spin-off
            # legitimately produces several successors, so only terminal
            # transitions are constrained.
            if action.action_type in IDENTITY_TRANSITION_ACTIONS:
                successors.setdefault(action.security_id, set()).add(
                    action.successor_security_id
                )
        for security_id, targets in successors.items():
            if len(targets) > 1:
                errors.append(
                    f"contradictory successor mapping for {security_id}: {sorted(targets)}"
                )
        # cycle detection over the terminal-transition graph
        edges = {source: next(iter(targets)) for source, targets in successors.items()
                 if len(targets) == 1}
        for start in edges:
            seen = {start}
            node = start
            while node in edges:
                node = edges[node]
                if node in seen:
                    errors.append(
                        f"identity transition cycle involving {start}"
                    )
                    break
                seen.add(node)
        return errors


class _BitemporalReference(Protocol):
    """A bitemporal reference record carrying an effective interval (read-only)."""

    @property
    def record_id(self) -> str: ...

    @property
    def known_at(self) -> datetime: ...

    @property
    def supersedes(self) -> str | None: ...

    @property
    def interval(self) -> EffectiveInterval: ...

    @property
    def bitemporal_key(self) -> tuple[str, ...]: ...


def _bitemporal_errors[BR: _BitemporalReference](
    kind: str, records: Sequence[BR], retractions: Sequence[RetractionRecord] = ()
) -> list[str]:
    """Validate a bitemporal reference collection.

    Checks duplicate ``record_id``, tz-aware ``known_at``, supersession integrity
    (target exists, same key, and a correction is never reported with an EARLIER
    ``known_at``), and — over the CURRENT survivors at full knowledge — that no
    two effective intervals for the same key overlap. Disjoint intervals (a real
    re-addition) are allowed; a supersession removes the corrected record, so a
    restatement is not double-counted; two overlapping, non-superseding records
    are a genuine duplicate/conflict and fail closed.
    """
    errors: list[str] = []
    by_id: dict[str, BR] = {}
    for record in records:
        if record.known_at.tzinfo is None:
            errors.append(f"{kind} {record.record_id}: known_at must be timezone-aware")
        if record.record_id in by_id:
            errors.append(f"duplicate {kind} record_id={record.record_id}")
        by_id[record.record_id] = record
    for record in records:
        if record.supersedes is None:
            continue
        target = by_id.get(record.supersedes)
        if target is None:
            errors.append(
                f"{kind} {record.record_id} supersedes unknown record {record.supersedes}"
            )
        elif target.bitemporal_key != record.bitemporal_key:
            errors.append(
                f"{kind} {record.record_id} supersedes a record with a different key"
            )
        elif record.known_at < target.known_at:
            errors.append(
                f"{kind} {record.record_id} supersedes with an earlier known_at"
            )
    # Overlaps are judged over the CURRENT survivors at full knowledge, with
    # retractions applied — a retracted assertion is not a conflict.
    survivors = resolve_visible(records, _UTC_MAX, retractions)
    intervals: list[tuple[tuple[str, ...], EffectiveInterval]] = []
    for survivor in survivors:
        try:
            intervals.append((survivor.bitemporal_key, survivor.interval))
        except ValueError as error:
            # A record whose derived interval is impossible (e.g. a listing date
            # after its delisting date) is reported, never raised out of
            # validation — validate() must return findings, not explode.
            errors.append(f"{kind} {survivor.record_id}: invalid effective interval ({error})")
    errors.extend(_overlap_errors(intervals))
    return errors


def _overlap_errors(
    rows: Iterable[tuple[tuple[str, ...], EffectiveInterval]],
) -> list[str]:
    grouped: dict[tuple[str, ...], list[EffectiveInterval]] = {}
    for group_key, interval in rows:
        grouped.setdefault(group_key, []).append(interval)
    errors: list[str] = []
    for group_key, intervals in grouped.items():
        ordered = sorted(intervals, key=lambda value: value.valid_from)
        previous: EffectiveInterval | None = None
        for current in ordered:
            if previous is not None and (
                previous.valid_to is None or current.valid_from <= previous.valid_to
            ):
                errors.append(f"overlapping effective intervals for {group_key}")
                break
            previous = current
    return errors


def _recompute_evidence_checksum(payload: Mapping[str, Any]) -> str:
    """Recompute the ``sha256:`` evidence checksum over the body.

    Mirrors ``poi_historical_qualification`` exactly: the checksum covers every
    field EXCEPT ``evidence_checksum`` itself, canonically serialized with sorted
    keys, tight separators and ASCII encoding. Kept here (rather than importing
    the broker package) so market-data-core has no broker dependency.
    """
    # The typing ENVELOPE (artifact_type, schema_version, producer,
    # capability_scope, normalized_checksum, run_id) is authenticated separately
    # by `normalized_checksum`. Excluding it here means migrating a legacy
    # artifact adds typing WITHOUT invalidating the digest that authenticates
    # the recorded observations.
    envelope = {
        "evidence_checksum", "artifact_type", "schema_version", "producer",
        "capability_scope", "normalized_checksum", "run_id",
    }
    body = {key: value for key, value in payload.items() if key not in envelope}
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("ascii")).hexdigest()


IBKR_HISTORICAL_CAPABILITIES = frozenset(
    {
        "connectivity_paper_du_gate",
        "daily_trades_rth_schema",
        "contract_identity_conid",
        "head_timestamp_coverage",
        "bid_ask_midpoint",
        "symbol_change_discovery",
    }
)

DATASET_V1_REQUIRED_CAPABILITIES = frozenset(
    {
        *IBKR_HISTORICAL_CAPABILITIES,
        "pit_sp500_membership",
        "asof_gics",
        "delisted_security_history",
        "corporate_actions",
    }
)


@dataclass(frozen=True, slots=True)
class DatasetCapabilityMatrix:
    states: Mapping[str, CapabilityState] = field(default_factory=dict)
    evidence_checksum: str | None = None

    @classmethod
    def from_ibkr_historical_evidence(cls, path: str | Path) -> DatasetCapabilityMatrix:
        payload = json.loads(Path(path).read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("historical evidence must be a JSON object")
        if payload.get("poi_id") != "POI-IBKR-HIST":
            raise ValueError("unexpected historical evidence poi_id")
        declared = payload.get("evidence_checksum")
        if not isinstance(declared, str) or not declared.startswith("sha256:"):
            raise ValueError("historical evidence is missing evidence_checksum")
        # The checksum must be VERIFIED against the body, not merely present:
        # a corrupt or tampered evidence file (e.g. a flipped capability) whose
        # checksum was not regenerated is rejected fail-closed. This is what makes
        # the checksum evidence rather than decoration.
        recomputed = _recompute_evidence_checksum(payload)
        if recomputed != declared:
            raise ValueError(
                "historical evidence checksum mismatch (tampered or corrupt): "
                f"declared {declared}, recomputed {recomputed}"
            )
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict):
            raise ValueError("historical evidence capabilities must be an object")
        missing = IBKR_HISTORICAL_CAPABILITIES - capabilities.keys()
        if missing:
            raise ValueError(f"historical evidence missing capabilities: {sorted(missing)}")
        not_pass = sorted(
            name for name in IBKR_HISTORICAL_CAPABILITIES if capabilities.get(name) != "PASS"
        )
        if not_pass:
            raise ValueError(f"historical capabilities not PASS: {not_pass}")
        if payload.get("active_name_price_qualification_sufficient") is not True:
            raise ValueError("active-name qualification is not sufficient")
        states: dict[str, CapabilityState] = {
            name: CapabilityState.EMPIRICALLY_VERIFIED
            for name in IBKR_HISTORICAL_CAPABILITIES
        }
        for name in DATASET_V1_REQUIRED_CAPABILITIES - IBKR_HISTORICAL_CAPABILITIES:
            states[name] = CapabilityState.BLOCKED_EXTERNAL
        return cls(states=states, evidence_checksum=declared)

    @property
    def full_dataset_contract_ready(self) -> bool:
        return all(
            self.states.get(name) is CapabilityState.EMPIRICALLY_VERIFIED
            for name in DATASET_V1_REQUIRED_CAPABILITIES
        )

    def assert_full_dataset_contract_ready(self) -> None:
        if self.full_dataset_contract_ready:
            return
        unresolved = sorted(
            name
            for name in DATASET_V1_REQUIRED_CAPABILITIES
            if self.states.get(name) is not CapabilityState.EMPIRICALLY_VERIFIED
        )
        raise ValueError(
            "Dataset V1 is not empirically ready; unresolved: " + ", ".join(unresolved)
        )
