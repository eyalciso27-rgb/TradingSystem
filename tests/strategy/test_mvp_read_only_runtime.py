"""Fake-tested bounded runtime and deterministic read-only audit behavior."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.mvp_policy_config import MvpPolicyConfig
from trading_application.mvp_read_only_runtime import (
    MvpInputEvidence,
    MvpReadOnlyInputSnapshot,
    MvpReadOnlyRuntime,
    MvpReadOnlyRuntimeError,
    canonical_run_payload,
)
from trading_application.mvp_trading_policy import (
    MvpAccountSnapshot,
    MvpCandidateFacts,
    MvpExecutionInputs,
    MvpResult,
    MvpTradingPolicy,
)
from trading_application.universe_eligibility import UniverseFacts
from trading_contracts.enums import MarketSessionStatus
from trading_domain.calendar import TradingCalendar
from trading_market_data_core.types import BarSeries


class _MemoryAudit:
    def __init__(self) -> None:
        self.records: dict[str, Mapping[str, object]] = {}
        self.appends = 0

    def put_if_absent(
        self, identity: str, record: Mapping[str, object]
    ) -> Mapping[str, object]:
        existing = self.records.get(identity)
        if existing is not None:
            return existing
        self.records[identity] = record
        self.appends += 1
        return record

    def load(self, identity: str) -> Mapping[str, object] | None:
        return self.records.get(identity)


MakeRun = Callable[
    [BarSeries], tuple[MvpReadOnlyRuntime, MvpReadOnlyInputSnapshot, _MemoryAudit]
]


@pytest.fixture
def make_read_only_run(
    mvp_config: MvpPolicyConfig,
    instrument_id: UUID,
    candidate_facts: MvpCandidateFacts,
    universe_facts: Callable[..., UniverseFacts],
    account_ok: Callable[..., MvpAccountSnapshot],
    execution_inputs: Callable[..., MvpExecutionInputs],
    next_rth_as_of: Callable[[BarSeries], datetime],
    test_calendar: TradingCalendar,
) -> MakeRun:
    def _make(series: BarSeries) -> tuple[
        MvpReadOnlyRuntime, MvpReadOnlyInputSnapshot, _MemoryAudit
    ]:
        policy = MvpTradingPolicy(mvp_config)
        as_of = next_rth_as_of(series)
        evaluation = policy._strategy.evaluate(  # noqa: SLF001 - fixture setup
            instrument_id,
            bars=series,
            as_of=as_of,
            session=MarketSessionStatus.REGULAR,
        )
        entry_price = (
            evaluation.signal.planned_entry_price
            if evaluation.signal is not None
            else Decimal("100")
        )
        snapshot = MvpReadOnlyInputSnapshot(
            instrument_id=instrument_id,
            bars=series,
            candidate=candidate_facts,
            universe=universe_facts(observed_at=series.bars[-1].observed_at),
            account=account_ok(as_of),
            held_positions=(),
            system_records=(),
            execution=execution_inputs(entry_price=entry_price, as_of=as_of),
            as_of=as_of,
            calendar=test_calendar,
            input_evidence=(
                MvpInputEvidence(
                    input_name="universe",
                    quality_status="FIXTURE_NOT_PIT_EVIDENCE",
                    evidence_ref="fixture:universe-v1",
                ),
            ),
        )
        store = _MemoryAudit()
        return MvpReadOnlyRuntime(policy, store), snapshot, store

    return _make


def test_runtime_persists_complete_sealed_lineage(
    make_read_only_run: MakeRun, triggered_series: BarSeries
) -> None:
    runtime, snapshot, store = make_read_only_run(triggered_series)
    run = runtime.run(task_id="M7", run_id="run-1", snapshot=snapshot)

    assert run.outcome.result is MvpResult.READ_ONLY_INTENT
    seal = run.read_only_seal
    assert seal is not None and seal is run.outcome.read_only_seal
    record = run.audit_record
    assert record["result"] == "READ_ONLY_INTENT"
    assert record["terminal_stage"] == "READ_ONLY_INTENT"
    assert record["strategy_id"] == "baseline-trend-breakout-v1"
    assert str(record["strategy_fingerprint"]).startswith("sha256:")
    assert record["decision_ref"] == seal.decision_ref
    assert record["risk_ref"] == seal.risk_ref
    assert record["allocation_ref"] == seal.allocation_ref
    assert record["read_only"] is True
    assert record["seal_ref"] == seal.seal_ref
    assert record["input_evidence"] == [
        {
            "input_name": "universe",
            "quality_status": "FIXTURE_NOT_PIT_EVIDENCE",
            "evidence_ref": "fixture:universe-v1",
        }
    ]
    assert store.appends == 1


def test_exact_retry_is_a_no_op_and_conflicting_inputs_fail_closed(
    make_read_only_run: MakeRun, triggered_series: BarSeries
) -> None:
    runtime, snapshot, store = make_read_only_run(triggered_series)
    first = runtime.run(task_id="M7", run_id="same", snapshot=snapshot)
    second = runtime.run(task_id="M7", run_id="same", snapshot=snapshot)

    assert canonical_run_payload(first.audit_record) == canonical_run_payload(
        second.audit_record
    )
    assert store.appends == 1

    conflicting = replace(
        snapshot,
        input_evidence=(MvpInputEvidence("universe", "DIFFERENT_PROVENANCE"),),
    )
    with pytest.raises(MvpReadOnlyRuntimeError, match="conflicting evidence"):
        runtime.run(task_id="M7", run_id="same", snapshot=conflicting)
    assert store.appends == 1


def test_replay_loads_without_evaluation(
    make_read_only_run: MakeRun,
    triggered_series: BarSeries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, snapshot, _ = make_read_only_run(triggered_series)
    expected = runtime.run(
        task_id="M7", run_id="replay", snapshot=snapshot
    ).audit_record

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("replay must not evaluate or reach execution")

    monkeypatch.setattr(MvpTradingPolicy, "evaluate_read_only", _forbidden)
    assert runtime.replay(task_id="M7", run_id="replay") == expected


def test_replay_compares_stored_seal_with_canonical_lineage(
    make_read_only_run: MakeRun, triggered_series: BarSeries
) -> None:
    runtime, snapshot, store = make_read_only_run(triggered_series)
    runtime.run(task_id="M7", run_id="tampered", snapshot=snapshot)
    identity = next(iter(store.records))
    changed = dict(store.records[identity])
    changed["seal_ref"] = "mvp-read-only-lineage:sha256:" + "0" * 64
    store.records[identity] = changed

    with pytest.raises(
        MvpReadOnlyRuntimeError, match="does not match canonical lineage"
    ):
        runtime.replay(task_id="M7", run_id="tampered")


def test_no_trade_is_distinct_and_never_fabricates_a_seal(
    make_read_only_run: MakeRun,
    build_series: Callable[[list[tuple[float, float]]], BarSeries],
    base_spec: list[tuple[float, float]],
) -> None:
    base_spec[170] = (165.5, 2.2)
    runtime, snapshot, _ = make_read_only_run(build_series(base_spec))
    run = runtime.run(task_id="M7", run_id="no-trade", snapshot=snapshot)

    assert run.outcome.result is MvpResult.NO_TRADE
    assert run.read_only_seal is None
    assert run.audit_record["result"] == "NO_TRADE"
    assert run.audit_record["read_only"] is True
    assert run.audit_record["seal_ref"] is None
    assert run.audit_record["risk_ref"] is None
    assert run.audit_record["allocation_ref"] is None


def test_fail_closed_no_new_entry_is_distinct_and_unsealed(
    make_read_only_run: MakeRun, triggered_series: BarSeries
) -> None:
    runtime, snapshot, _ = make_read_only_run(triggered_series)
    unavailable = replace(
        snapshot, universe=replace(snapshot.universe, adv20_usd=None)
    )
    run = runtime.run(task_id="M7", run_id="blocked", snapshot=unavailable)

    assert run.outcome.result is MvpResult.NO_NEW_ENTRY
    assert run.audit_record["result"] == "NO_NEW_ENTRY"
    reason_codes = run.audit_record["reason_codes"]
    assert isinstance(reason_codes, list)
    assert "ADV20_UNKNOWN" in reason_codes
    assert run.audit_record["read_only"] is True
    assert run.audit_record["seal_ref"] is None


def test_pre_submit_refusal_preserves_quantity_without_a_seal(
    make_read_only_run: MakeRun, triggered_series: BarSeries
) -> None:
    runtime, snapshot, _ = make_read_only_run(triggered_series)
    refused = replace(
        snapshot,
        execution=replace(snapshot.execution, broker_healthy=False),
    )
    run = runtime.run(
        task_id="M7", run_id="pre-submit-refused", snapshot=refused
    )

    assert run.outcome.result is MvpResult.NO_NEW_ENTRY
    assert run.outcome.pre_submit_evaluation is not None
    assert run.audit_record["evaluated_quantity"] is not None
    assert run.audit_record["read_only"] is True
    assert run.audit_record["seal_ref"] is None


def test_runtime_cannot_enter_authorizing_or_executable_paths(
    make_read_only_run: MakeRun,
    triggered_series: BarSeries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trading_application.execution_plan as plan_module
    import trading_application.pre_submit_check as pre_submit_module
    from trading_broker_core.fake import FakeBroker

    class _Forbidden:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("read-only runtime reached a forbidden constructor")

    def _forbidden_check(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "read-only runtime reached authorization-consuming pre-submit"
        )

    monkeypatch.setattr(FakeBroker, "submit_order", _forbidden_check)
    monkeypatch.setattr(pre_submit_module, "SubmitAuthorization", _Forbidden)
    monkeypatch.setattr(plan_module, "ExecutablePlan", _Forbidden)
    monkeypatch.setattr(
        pre_submit_module.FinalPreSubmitCheck, "check", _forbidden_check
    )
    runtime, snapshot, _ = make_read_only_run(triggered_series)

    run = runtime.run(task_id="M7", run_id="safe", snapshot=snapshot)
    assert run.outcome.result is MvpResult.READ_ONLY_INTENT
    assert run.outcome.executable_plan is None
