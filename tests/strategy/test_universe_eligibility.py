"""Unit tests for the universe/liquidity/price/spread eligibility gate (B3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from trading_application.baseline_strategy_config import baseline_universe_eligibility_policy
from trading_application.universe_eligibility import (
    UniverseEligibility,
    UniverseEligibilityError,
    UniverseFacts,
    evaluate_universe_eligibility,
)

AS_OF = datetime(2025, 6, 2, 14, 0, tzinfo=UTC)


def _facts(**overrides: object) -> UniverseFacts:
    base: dict[str, object] = dict(
        symbol="AAA",
        is_sp500_member=True,
        membership_is_point_in_time=True,
        gics_sector="Information Technology",
        adv20_usd=Decimal("100000000"),
        last_price=Decimal("50"),
        spread_bps=Decimal("5"),
        observed_at=AS_OF,
    )
    base.update(overrides)
    return UniverseFacts(**base)  # type: ignore[arg-type]


def _eval(facts: UniverseFacts) -> UniverseEligibility:
    return evaluate_universe_eligibility(
        facts, baseline_universe_eligibility_policy(), as_of=AS_OF
    )


def test_all_valid_facts_are_eligible() -> None:
    result = _eval(_facts())
    assert result.eligible
    assert result.gics_sector == "Information Technology"
    assert result.reason_codes == ()


def test_non_member_blocks() -> None:
    result = _eval(_facts(is_sp500_member=False))
    assert not result.eligible
    assert "NOT_SP500_MEMBER" in result.reason_codes
    assert result.gics_sector is None


def test_membership_unknown_blocks() -> None:
    assert "SP500_MEMBERSHIP_UNKNOWN" in _eval(_facts(is_sp500_member=None)).reason_codes


def test_non_pit_membership_blocks() -> None:
    assert "MEMBERSHIP_NOT_POINT_IN_TIME" in _eval(
        _facts(membership_is_point_in_time=False)
    ).reason_codes


def test_sector_unknown_blocks() -> None:
    assert "GICS_SECTOR_UNKNOWN" in _eval(_facts(gics_sector=None)).reason_codes


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("adv20_usd", Decimal("49999999"), "ADV20_BELOW_MIN"),
        ("adv20_usd", None, "ADV20_UNKNOWN"),
        ("last_price", Decimal("9.99"), "PRICE_BELOW_MIN"),
        ("last_price", None, "PRICE_UNKNOWN"),
        ("spread_bps", Decimal("10.01"), "SPREAD_TOO_WIDE"),
        ("spread_bps", None, "SPREAD_UNKNOWN"),
    ],
)
def test_threshold_and_unknown_blocks(field: str, value: object, code: str) -> None:
    result = _eval(_facts(**{field: value}))
    assert not result.eligible
    assert code in result.reason_codes


def test_boundary_values_are_eligible() -> None:
    # Exactly at the frozen thresholds is eligible (>=/<=).
    result = _eval(
        _facts(adv20_usd=Decimal("50000000"), last_price=Decimal("10"), spread_bps=Decimal("10"))
    )
    assert result.eligible


def test_lookahead_facts_rejected() -> None:
    future = datetime(2025, 6, 3, tzinfo=UTC)
    with pytest.raises(UniverseEligibilityError):
        _eval(_facts(observed_at=future))
