"""Canonical enum value tests (IMP-0101).

The expected values below are hardcoded on purpose: they are an independent
witness of the authoritative baseline documents, so an accidental edit of
trading_contracts.enums cannot silently pass.
"""

import json
import re
from enum import StrEnum
from pathlib import Path

import pytest
from trading_contracts.enums import (
    CANONICAL_ENUMS,
    DecisionOutcome,
    Environment,
    OperatingMode,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "packages" / "schemas" / "json" / "enums"
TS_FILE = REPO_ROOT / "packages" / "ts" / "contracts-generated" / "src" / "enums.ts"

EXPECTED_VALUES: dict[str, list[str]] = {
    # Decision_Contract_v0.8 section 12.1
    "SourceEntryType": [
        "BUY_LIMIT_MAX",
        "BUY_STOP_TRIGGER",
        "BUY_ZONE",
        "ENTER_IMMEDIATELY",
        "BUY_PULLBACK",
        "CONDITIONAL_ENTRY",
        "UNKNOWN",
    ],
    "DecisionOutcome": [
        "REJECT",
        "WATCH",
        "SHADOW",
        "APPROVE",
        "NEEDS_REVIEW",
        "EXPIRE",
        "INVALID",
    ],
    # Decision_Contract_v0.8 section 12.5
    "OrderState": [
        "CREATED",
        "VALIDATED",
        "QUEUED",
        "SUBMITTING",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_PENDING",
        "CANCELLED",
        "CANCELLED_BY_POLICY",
        "REJECTED",
        "EXPIRED",
        "UNKNOWN",
    ],
    "OrderType": ["LIMIT", "STOP", "STOP_LIMIT", "MARKET"],
    "OrderRole": ["ENTRY", "PROTECTIVE_STOP", "TAKE_PROFIT", "REDUCE", "EXIT"],
    "PositionStatus": [
        "OPENING",
        "OPEN",
        "REDUCING",
        "CLOSING",
        "CLOSED",
        "RECONCILIATION_REQUIRED",
    ],
    # Integration_Specification_v0.6 section 2.3
    "IntegrationHealthStatus": [
        "INITIALIZING",
        "HEALTHY",
        "DEGRADED",
        "AUTH_REQUIRED",
        "RATE_LIMITED",
        "CIRCUIT_OPEN",
        "PAUSED",
        "DOWN",
    ],
    # Integration_Specification_v0.6 section 2.4
    "SourceType": [
        "WHATSAPP",
        "DISCORD",
        "TELEGRAM",
        "INVESTINGPRO",
        "NEWS",
        "FILING",
        "MARKET",
    ],
    # Data_Model_and_Retention_Specification_v0.6 (fusion_evidence.role)
    "EvidenceRole": ["REQUIRED", "OPTIONAL", "VETO", "CONTEXT"],
    # Data_Model_and_Retention_Specification_v0.6 sections 30.1/30.2
    "TelegramAccessMode": ["BOT_API", "USER_ACCOUNT_TDLIB"],
    # Data_Model_and_Retention_Specification_v0.6 section 31
    "SocialAccessMode": [
        "WHATSAPP_USER",
        "DISCORD_USER",
        "TELEGRAM_USER_TDLIB",
        "TELEGRAM_BOT",
    ],
    # MVP_Scope_v0.6 section 4
    "OperatingMode": [
        "RESEARCH",
        "REPLAY",
        "SHADOW",
        "PAPER_MANUAL",
        "PAPER_AUTO",
        "LIVE_MANUAL",
        "LIVE_LIMITED_AUTO",
    ],
    # Data_Model_and_Retention_Specification_v0.6 section 4
    "Environment": ["RESEARCH", "PAPER", "LIVE"],
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def test_every_canonical_enum_is_covered() -> None:
    assert sorted(cls.__name__ for cls in CANONICAL_ENUMS) == sorted(EXPECTED_VALUES)


@pytest.mark.parametrize("enum_class", CANONICAL_ENUMS, ids=lambda c: c.__name__)
def test_enum_values_match_baseline_documents(enum_class: type[StrEnum]) -> None:
    assert [member.value for member in enum_class] == EXPECTED_VALUES[enum_class.__name__]


@pytest.mark.parametrize("enum_class", CANONICAL_ENUMS, ids=lambda c: c.__name__)
def test_no_duplicate_values(enum_class: type[StrEnum]) -> None:
    values = [member.value for member in enum_class]
    assert len(values) == len(set(values))


@pytest.mark.parametrize("enum_class", CANONICAL_ENUMS, ids=lambda c: c.__name__)
def test_serialization_round_trip(enum_class: type[StrEnum]) -> None:
    for member in enum_class:
        assert str(member) == member.value
        assert enum_class(member.value) is member


@pytest.mark.parametrize("enum_class", CANONICAL_ENUMS, ids=lambda c: c.__name__)
def test_unknown_value_fails_without_fallback(enum_class: type[StrEnum]) -> None:
    with pytest.raises(ValueError):
        enum_class("NOT_A_CANONICAL_VALUE")


@pytest.mark.parametrize("enum_class", CANONICAL_ENUMS, ids=lambda c: c.__name__)
def test_json_schema_matches_python(enum_class: type[StrEnum]) -> None:
    schema_path = SCHEMA_DIR / f"{_snake(enum_class.__name__)}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["type"] == "string"
    assert schema["title"] == enum_class.__name__
    assert schema["enum"] == [member.value for member in enum_class]


@pytest.mark.parametrize("enum_class", CANONICAL_ENUMS, ids=lambda c: c.__name__)
def test_json_schema_accepts_valid_and_rejects_invalid(enum_class: type[StrEnum]) -> None:
    schema_path = SCHEMA_DIR / f"{_snake(enum_class.__name__)}.schema.json"
    allowed = json.loads(schema_path.read_text(encoding="utf-8"))["enum"]
    for member in enum_class:
        assert member.value in allowed
    assert "NOT_A_CANONICAL_VALUE" not in allowed
    assert None not in allowed


@pytest.mark.parametrize("enum_class", CANONICAL_ENUMS, ids=lambda c: c.__name__)
def test_typescript_matches_python(enum_class: type[StrEnum]) -> None:
    text = TS_FILE.read_text(encoding="utf-8")
    block = re.search(
        rf"export const {enum_class.__name__} = \{{\n(.*?)\n\}} as const;",
        text,
        re.DOTALL,
    )
    assert block is not None, f"no TS block for {enum_class.__name__}"
    ts_values = re.findall(r':\s*"([A-Z0-9_]+)",', block.group(1))
    assert ts_values == [member.value for member in enum_class]


def test_generated_files_carry_do_not_edit_header() -> None:
    assert "GENERATED FILE — DO NOT EDIT" in TS_FILE.read_text(encoding="utf-8")


def test_operating_modes_have_no_live_auto_and_include_replay() -> None:
    values = {member.value for member in OperatingMode}
    assert "LIVE_AUTO" not in values
    assert "REPLAY" in values


def test_environment_axis_is_separate_from_operating_mode() -> None:
    assert {member.value for member in Environment} == {"RESEARCH", "PAPER", "LIVE"}
    assert {member.value for member in Environment} != {
        member.value for member in OperatingMode
    }


def test_decision_outcome_has_no_default_and_no_live_semantics() -> None:
    with pytest.raises(ValueError):
        DecisionOutcome("")
