"""Bind the MVP baseline configuration to the immutable freeze record.

The strategy evaluator invents no parameter: the fingerprint it carries must be
the frozen fingerprint, its fusion-manifest entries must be the owner-approved
frozen entries, and the numeric execution params must equal the owner-attributed
``config/strategies/baseline-trend-breakout-v1.execution-params.yaml``. Any drift
here fails closed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from trading_application.baseline_strategy import BASE_SELECTION_LARGEST
from trading_application.baseline_strategy_config import (
    BASELINE_BASE_SELECTION_APPROVAL_REF,
    BASELINE_FROZEN_FINGERPRINT,
    baseline_trend_breakout_v1_manifest,
    baseline_trend_breakout_v1_policy,
    baseline_universe_eligibility_policy,
)
from trading_application.strategy_freeze_cli import load_freeze_record

REPO_ROOT = Path(__file__).resolve().parents[2]
FROZEN_MANIFEST = REPO_ROOT / "docs/strategies/baseline-trend-breakout-v1.yaml"
EXEC_PARAMS = REPO_ROOT / "config/strategies/baseline-trend-breakout-v1.execution-params.yaml"


def test_policy_fingerprint_equals_the_frozen_record() -> None:
    record = load_freeze_record(FROZEN_MANIFEST)
    policy = baseline_trend_breakout_v1_policy()
    assert policy.frozen_fingerprint == BASELINE_FROZEN_FINGERPRINT
    assert policy.frozen_fingerprint == record.fingerprint()


def test_manifest_entries_equal_the_frozen_fusion_manifest() -> None:
    record = load_freeze_record(FROZEN_MANIFEST)
    frozen = record.fusion_manifest
    assert frozen is not None
    mine = baseline_trend_breakout_v1_manifest()
    assert mine.strategy_id == frozen.strategy_id
    # Same owner-approved structural content (only the version LABEL differs:
    # the freeze label "v1" vs the canonical semver strategy_version).
    assert mine.entries == frozen.entries


def test_execution_params_yaml_matches_the_policy() -> None:
    data = yaml.safe_load(EXEC_PARAMS.read_text(encoding="utf-8"))
    params = data["parameters"]
    policy = baseline_trend_breakout_v1_policy()
    assert data["frozen_fingerprint"] == policy.frozen_fingerprint
    assert params["sma_trend_period"]["value"] == policy.sma_trend_period
    assert params["sma_trend_rising_lookback"]["value"] == policy.sma_trend_rising_lookback
    assert params["base_min_bars"]["value"] == policy.base_min_bars
    assert params["base_max_bars"]["value"] == policy.base_max_bars
    assert params["range_window"]["value"] == policy.range_window
    assert params["atr_short_period"]["value"] == policy.atr_short_period
    assert params["atr_long_period"]["value"] == policy.atr_long_period
    assert Decimal(params["entry_atr_multiple"]["value"]) == policy.entry_atr_multiple
    assert Decimal(params["stop_atr_multiple"]["value"]) == policy.stop_atr_multiple
    assert Decimal(params["max_risk_atr_multiple"]["value"]) == policy.max_risk_atr_multiple


def test_every_execution_param_cites_a_frozen_owner_key() -> None:
    record = load_freeze_record(FROZEN_MANIFEST)
    owner_keys = record.parameter_keys()
    data = yaml.safe_load(EXEC_PARAMS.read_text(encoding="utf-8"))
    for name, spec in data["parameters"].items():
        assert spec["source_key"] in owner_keys, f"{name} cites unknown owner key"


def test_base_selection_is_owner_ratified_and_explicit() -> None:
    policy = baseline_trend_breakout_v1_policy()
    assert policy.base_selection_rule == BASE_SELECTION_LARGEST
    data = yaml.safe_load(EXEC_PARAMS.read_text(encoding="utf-8"))
    assert data["base_selection"]["rule"] == BASE_SELECTION_LARGEST
    assert data["base_selection"]["approval_ref"] == BASELINE_BASE_SELECTION_APPROVAL_REF
    # The clarification cites a real frozen owner key and does NOT change the
    # frozen fingerprint.
    record = load_freeze_record(FROZEN_MANIFEST)
    assert data["base_selection"]["source_key"] in record.parameter_keys()
    assert record.fingerprint() == BASELINE_FROZEN_FINGERPRINT


def test_universe_eligibility_policy_matches_frozen_thresholds() -> None:
    policy = baseline_universe_eligibility_policy()
    data = yaml.safe_load(EXEC_PARAMS.read_text(encoding="utf-8"))["universe"]
    assert policy.min_adv20_usd == Decimal(data["min_adv20_usd"]["value"])
    assert policy.min_price == Decimal(data["min_price"]["value"])
    assert policy.max_spread_bps == Decimal(data["max_spread_bps"]["value"])
    assert policy.require_point_in_time_membership is True
    # Owner provenance for every universe threshold.
    record = load_freeze_record(FROZEN_MANIFEST)
    for spec in data.values():
        assert spec["source_key"] in record.parameter_keys()
