"""Unit tests for the canonical contract enums."""

import pytest
from trading_contracts.enums import (
    CANONICAL_ENUMS,
    OPERATING_MODE_TO_ENVIRONMENT,
    Environment,
    OperatingMode,
    OrderState,
)


def test_canonical_enum_values_are_unique_and_round_trip() -> None:
    for enum_type in CANONICAL_ENUMS:
        values = [member.value for member in enum_type]

        assert values
        assert len(values) == len(set(values))
        assert all(
            enum_type(value) is member
            for value, member in zip(values, enum_type, strict=True)
        )


def test_string_enums_serialize_as_their_canonical_value() -> None:
    assert OrderState.PARTIALLY_FILLED == "PARTIALLY_FILLED"
    assert str(OrderState.PARTIALLY_FILLED) == "PARTIALLY_FILLED"


def test_enum_rejects_unknown_and_wrong_case_values() -> None:
    with pytest.raises(ValueError):
        OrderState("partial_fill")

    with pytest.raises(ValueError):
        OrderState("UNKNOWN_STATE")


def test_every_operating_mode_maps_to_its_environment() -> None:
    assert set(OPERATING_MODE_TO_ENVIRONMENT) == set(OperatingMode)
    assert OPERATING_MODE_TO_ENVIRONMENT[OperatingMode.REPLAY] is Environment.RESEARCH
    assert OPERATING_MODE_TO_ENVIRONMENT[OperatingMode.PAPER_AUTO] is Environment.PAPER
    assert OPERATING_MODE_TO_ENVIRONMENT[OperatingMode.LIVE_MANUAL] is Environment.LIVE
