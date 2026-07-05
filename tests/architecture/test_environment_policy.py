"""Positive and negative tests for canonical environment naming policy."""

import pytest
from trading_config.environments import (
    CANONICAL_ENVIRONMENTS,
    CANONICAL_OPERATING_MODES,
    LIVE_ENABLED,
    PAPER_AUTO_ENABLED,
    EnvironmentPolicyError,
    ProhibitedOperatingModeError,
    UnknownEnvironmentError,
    UnknownOperatingModeError,
    environment_for_mode,
    validate_account_allowlists,
    validate_environment,
    validate_environment_scoped_name,
    validate_operating_mode,
)


def test_canonical_sets_match_baseline_documents() -> None:
    assert CANONICAL_ENVIRONMENTS == {"RESEARCH", "PAPER", "LIVE"}
    assert CANONICAL_OPERATING_MODES == {
        "RESEARCH",
        "REPLAY",
        "SHADOW",
        "PAPER_MANUAL",
        "PAPER_AUTO",
        "LIVE_MANUAL",
        "LIVE_LIMITED_AUTO",
    }


def test_baseline_enablement_flags_are_off() -> None:
    assert LIVE_ENABLED is False
    assert PAPER_AUTO_ENABLED is False


@pytest.mark.parametrize("environment", ["RESEARCH", "PAPER", "LIVE"])
def test_canonical_environment_is_accepted(environment: str) -> None:
    assert validate_environment(environment) == environment


@pytest.mark.parametrize("environment", ["", "paper", "PROD", "STAGING", "LIVE "])
def test_unknown_environment_fails_explicitly(environment: str) -> None:
    with pytest.raises(UnknownEnvironmentError):
        validate_environment(environment)


@pytest.mark.parametrize("mode", ["RESEARCH", "REPLAY", "SHADOW", "PAPER_MANUAL"])
def test_permitted_modes_are_accepted(mode: str) -> None:
    assert validate_operating_mode(mode) == mode


@pytest.mark.parametrize("mode", ["LIVE_MANUAL", "LIVE_LIMITED_AUTO"])
def test_live_modes_are_prohibited_by_default(mode: str) -> None:
    with pytest.raises(ProhibitedOperatingModeError):
        validate_operating_mode(mode)


@pytest.mark.parametrize("mode", ["LIVE_MANUAL", "LIVE_LIMITED_AUTO"])
def test_live_enablement_alone_is_not_enough(mode: str) -> None:
    with pytest.raises(ProhibitedOperatingModeError):
        validate_operating_mode(mode, live_enabled=True)
    with pytest.raises(ProhibitedOperatingModeError):
        validate_operating_mode(mode, live_account_allowlist=frozenset({"alias"}))


def test_paper_auto_is_prohibited_by_default() -> None:
    with pytest.raises(ProhibitedOperatingModeError):
        validate_operating_mode("PAPER_AUTO")


@pytest.mark.parametrize("mode", ["", "LIVE_AUTO", "PAPER", "paper_manual", "AUTO"])
def test_unknown_mode_fails_and_never_falls_back(mode: str) -> None:
    with pytest.raises(UnknownOperatingModeError):
        validate_operating_mode(mode)


def test_mode_maps_to_environment_axis() -> None:
    assert environment_for_mode("PAPER_MANUAL") == "PAPER"
    assert environment_for_mode("LIVE_MANUAL") == "LIVE"
    assert environment_for_mode("REPLAY") == "RESEARCH"


@pytest.mark.parametrize(
    ("name", "environment"),
    [
        ("paper.orders", "PAPER"),
        ("orders.paper.intent", "PAPER"),
        ("live.orders", "LIVE"),
        ("events.candidate.created", "RESEARCH"),
    ],
)
def test_environment_scoped_names_from_specs_pass(name: str, environment: str) -> None:
    assert validate_environment_scoped_name(name, environment) == name


@pytest.mark.parametrize(
    ("name", "environment"),
    [
        ("live.orders", "PAPER"),
        ("paper.orders", "LIVE"),
        ("orders.intent", "PAPER"),
        ("orders.paper.live.intent", "PAPER"),
        ("paper.orders", "RESEARCH"),
    ],
)
def test_environment_scoped_name_violations_fail(name: str, environment: str) -> None:
    with pytest.raises(EnvironmentPolicyError):
        validate_environment_scoped_name(name, environment)


def test_paper_and_live_allowlists_must_not_overlap() -> None:
    validate_account_allowlists({"paper-alias"}, {"live-alias"})
    with pytest.raises(EnvironmentPolicyError):
        validate_account_allowlists({"shared-alias"}, {"shared-alias"})
