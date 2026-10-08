"""Unit tests for the canonical contract enums."""

from enum import StrEnum

import pytest
from trading_contracts.enums import (
    CANONICAL_ENUMS,
    OPERATING_MODE_TO_ENVIRONMENT,
    Environment,
    OperatingMode,
)


@pytest.mark.parametrize("enum_type", CANONICAL_ENUMS, ids=lambda enum_type: enum_type.__name__)
def test_canonical_enums_round_trip_their_declared_values(
    enum_type: type[StrEnum],
) -> None:
    assert enum_type
    assert len(enum_type) == len({member.value for member in enum_type})

    for member in enum_type:
        assert isinstance(member.value, str)
        assert str(member) == member.value
        assert enum_type(member.value) is member


@pytest.mark.parametrize("enum_type", CANONICAL_ENUMS, ids=lambda enum_type: enum_type.__name__)
def test_canonical_enums_reject_unknown_and_empty_values(enum_type: type[StrEnum]) -> None:
    with pytest.raises(ValueError):
        enum_type("NOT_A_CANONICAL_VALUE")
    with pytest.raises(ValueError):
        enum_type("")


def test_every_operating_mode_has_an_explicit_environment_mapping() -> None:
    assert set(OPERATING_MODE_TO_ENVIRONMENT) == set(OperatingMode)
    assert set(OPERATING_MODE_TO_ENVIRONMENT.values()) == set(Environment)
    assert OPERATING_MODE_TO_ENVIRONMENT[OperatingMode.REPLAY] is Environment.RESEARCH
    assert OPERATING_MODE_TO_ENVIRONMENT[OperatingMode.PAPER_AUTO] is Environment.PAPER
    assert OPERATING_MODE_TO_ENVIRONMENT[OperatingMode.LIVE_LIMITED_AUTO] is Environment.LIVE
