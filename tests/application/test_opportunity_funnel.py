"""Opportunity-funnel drills (IMP-1203). DoD: every candidate has a full
terminal/stage history — nothing disappears; conservation is enforced."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from trading_application.opportunity_funnel import (
    STAGE_ORDER,
    CandidateLifecycleStatus,
    FunnelError,
    FunnelStage,
    OpportunityFunnel,
    StageTransition,
    TransitionOutcome,
)
from trading_application.replay_runner import ReplayDataset, ReplayEvent, ReplayRunner

T0 = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)


def tr(
    transition_id: str,
    candidate_id: str,
    stage: FunnelStage,
    *,
    outcome: TransitionOutcome = TransitionOutcome.ADVANCED,
    minute: int = 0,
    reason: str = "",
    engine: str = "",
    version: str = "",
    run_id: str = "",
    split_id: str = "",
) -> StageTransition:
    return StageTransition(
        transition_id=transition_id,
        candidate_id=candidate_id,
        stage=stage,
        outcome=outcome,
        reason_code=reason,
        attributed_engine=engine,
        attributed_version=version,
        as_of=T0 + timedelta(minutes=minute),
        run_id=run_id,
        split_id=split_id,
    )


def advance(funnel: OpportunityFunnel, candidate: str, *, upto: int, start_minute: int = 0) -> None:
    """Advance a candidate through canonical stages [0..upto]."""
    for index in range(upto + 1):
        funnel.record(
            tr(f"{candidate}-t{index}", candidate, STAGE_ORDER[index], minute=start_minute + index)
        )


def reject(
    funnel: OpportunityFunnel, candidate: str, *, at_index: int, minute: int
) -> None:
    funnel.record(
        tr(
            f"{candidate}-rej",
            candidate,
            STAGE_ORDER[at_index],
            outcome=TransitionOutcome.REJECTED,
            minute=minute,
            reason="SCORE_BELOW_THRESHOLD",
            engine="fusion-engine",
            version="1.2.0",
        )
    )


def build_populated() -> OpportunityFunnel:
    funnel = OpportunityFunnel()
    advance(funnel, "c-approved", upto=7)  # reaches APPROVED
    advance(funnel, "c-rejected", upto=2)  # OBSERVED..FUSED
    reject(funnel, "c-rejected", at_index=3, minute=10)  # rejected at DECIDED
    advance(funnel, "c-active", upto=1)  # still pending at ASSESSED
    return funnel


# -- DoD: histories, terminal, pending, conservation --------------------------------


def test_every_candidate_has_history() -> None:
    report = build_populated().report()
    assert report.total_candidates == 3
    assert {h.candidate_id for h in report.histories} == {
        "c-approved",
        "c-rejected",
        "c-active",
    }
    for history in report.histories:
        assert len(history.transitions) >= 2


def test_terminal_candidate_has_exactly_one_terminal_outcome() -> None:
    funnel = build_populated()
    history = funnel.candidate_history("c-rejected")
    assert history.status is CandidateLifecycleStatus.TERMINAL
    terminal = [t for t in history.transitions if t.outcome is TransitionOutcome.REJECTED]
    assert len(terminal) == 1
    assert history.terminal_outcome is TransitionOutcome.REJECTED


def test_active_candidate_is_explicitly_pending_never_hidden() -> None:
    report = build_populated().report()
    active = next(h for h in report.histories if h.candidate_id == "c-active")
    assert active.status is CandidateLifecycleStatus.ACTIVE
    assert active.terminal_outcome is None
    assert report.active_candidates == 1


def test_conservation_total_equals_active_plus_terminal() -> None:
    report = build_populated().report()
    terminal = (
        report.terminal_success
        + report.terminal_rejected
        + report.terminal_vetoed
        + report.terminal_expired
        + report.terminal_stale
    )
    assert report.total_candidates == report.active_candidates + terminal
    assert report.terminal_success == 1  # c-approved
    assert report.terminal_rejected == 1  # c-rejected


# -- stage counts / conversion -------------------------------------------------------


def test_stage_counts_are_correct() -> None:
    report = build_populated().report()
    assert report.stage_counts["OBSERVED"] == 3
    assert report.stage_counts["ASSESSED"] == 3
    assert report.stage_counts["FUSED"] == 2  # approved + rejected
    assert report.stage_counts["DECIDED"] == 2  # rejected AT decided still reached it
    assert report.stage_counts["RISK_CHECKED"] == 1
    assert report.stage_counts["APPROVED"] == 1


def test_conversion_has_explicit_denominator_and_is_deterministic() -> None:
    first = build_populated().report()
    second = build_populated().report()
    assert first.conversion == second.conversion
    assert first.conversion["FUSED->DECIDED"] == "1.0000 (2/2)"
    assert first.conversion["DECIDED->RISK_CHECKED"] == "0.5000 (1/2)"


# -- rejection attribution ------------------------------------------------------------


def test_rejection_attribution_includes_stage_reason_engine_version() -> None:
    report = build_populated().report()
    assert len(report.rejection_attribution) == 1
    entry = report.rejection_attribution[0]
    assert entry.stage is FunnelStage.DECIDED
    assert entry.reason_code == "SCORE_BELOW_THRESHOLD"
    assert entry.engine == "fusion-engine"
    assert entry.version == "1.2.0"
    assert entry.count == 1


def test_rejection_without_reason_or_version_is_refused() -> None:
    with pytest.raises(FunnelError, match="reason_code"):
        tr(
            "x", "c", FunnelStage.DECIDED,
            outcome=TransitionOutcome.REJECTED, engine="e", version="1",
        )
    with pytest.raises(FunnelError, match="attribution"):
        tr("x", "c", FunnelStage.DECIDED, outcome=TransitionOutcome.VETOED, reason="R")


# -- ordering enforcement ---------------------------------------------------------------


def test_candidate_must_start_at_observed() -> None:
    funnel = OpportunityFunnel()
    with pytest.raises(FunnelError, match="must start at OBSERVED"):
        funnel.record(tr("t1", "c1", FunnelStage.FUSED))


def test_stage_skipping_rejected() -> None:
    funnel = OpportunityFunnel()
    funnel.record(tr("t1", "c1", FunnelStage.OBSERVED))
    with pytest.raises(FunnelError, match="skips"):
        funnel.record(tr("t2", "c1", FunnelStage.FUSED, minute=1))


def test_stage_regression_rejected() -> None:
    funnel = OpportunityFunnel()
    advance(funnel, "c1", upto=2)
    with pytest.raises(FunnelError, match="regresses"):
        funnel.record(tr("t-back", "c1", FunnelStage.ASSESSED, minute=9))


def test_as_of_regression_rejected() -> None:
    funnel = OpportunityFunnel()
    funnel.record(tr("t1", "c1", FunnelStage.OBSERVED, minute=5))
    with pytest.raises(FunnelError, match="backwards"):
        funnel.record(tr("t2", "c1", FunnelStage.ASSESSED, minute=3))


def test_transition_after_terminal_rejected() -> None:
    funnel = OpportunityFunnel()
    advance(funnel, "c1", upto=1)
    reject(funnel, "c1", at_index=2, minute=5)
    with pytest.raises(FunnelError, match="terminal"):
        funnel.record(tr("t-more", "c1", FunnelStage.DECIDED, minute=6))


# -- dedup / conflict ---------------------------------------------------------------------


def test_identical_duplicate_is_idempotent() -> None:
    funnel = OpportunityFunnel()
    transition = tr("t1", "c1", FunnelStage.OBSERVED)
    funnel.record(transition)
    funnel.record(transition)  # no error, no double count
    assert len(funnel.candidate_history("c1").transitions) == 1


def test_conflicting_duplicate_id_rejected() -> None:
    funnel = OpportunityFunnel()
    funnel.record(tr("t1", "c1", FunnelStage.OBSERVED))
    with pytest.raises(FunnelError, match="different content"):
        funnel.record(tr("t1", "c2", FunnelStage.OBSERVED))


# -- group isolation ------------------------------------------------------------------------


def test_different_run_or_split_never_mixes() -> None:
    funnel = OpportunityFunnel(run_id="run-A", split_id="s1")
    funnel.record(tr("t1", "c1", FunnelStage.OBSERVED, run_id="run-A", split_id="s1"))
    with pytest.raises(FunnelError, match="fail closed"):
        funnel.record(tr("t2", "c1", FunnelStage.ASSESSED, minute=1, run_id="run-B", split_id="s1"))
    with pytest.raises(FunnelError, match="fail closed"):
        funnel.record(tr("t3", "c1", FunnelStage.ASSESSED, minute=1, run_id="run-A", split_id="s2"))


# -- determinism through ReplayRunner ---------------------------------------------------------


def stage_event(event_id: str, candidate: str, stage: FunnelStage, minute: int) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    return ReplayEvent(
        event_id=event_id,
        sequence=0,
        kind="STAGE",
        payload={
            "candidate_id": candidate,
            "stage": stage.value,
            "outcome": "ADVANCED",
            "reason_code": "",
            "engine": "",
            "version": "",
        },
        observed_at=at,
        available_at=at,
    )


RUNNER_EVENTS = [
    stage_event("e1", "c1", FunnelStage.OBSERVED, 0),
    stage_event("e2", "c1", FunnelStage.ASSESSED, 1),
    stage_event("e3", "c1", FunnelStage.FUSED, 2),
]
RUNNER_VERSIONS = {"funnel": "1.0.0"}


def run_via_runner(events: list[ReplayEvent]) -> tuple[str, object]:
    funnel = OpportunityFunnel()
    runner = ReplayRunner(funnel.consume)
    result = runner.run(
        ReplayDataset(
            dataset_id="fn-1", events=tuple(events), versions=RUNNER_VERSIONS, seed="seed:1"
        )
    )
    return result.result_hash, funnel.report()


def test_repeat_and_shuffle_through_runner_identical() -> None:
    hash_a, report_a = run_via_runner(list(RUNNER_EVENTS))
    hash_b, report_b = run_via_runner(list(reversed(RUNNER_EVENTS)))
    assert hash_a == hash_b
    assert report_a == report_b


# -- hygiene ------------------------------------------------------------------------------------


def test_naive_datetime_rejected() -> None:
    with pytest.raises(FunnelError, match="timezone-aware"):
        StageTransition(
            transition_id="t",
            candidate_id="c",
            stage=FunnelStage.OBSERVED,
            outcome=TransitionOutcome.ADVANCED,
            reason_code="",
            attributed_engine="",
            attributed_version="",
            as_of=datetime(2026, 6, 1, 14, 30),  # noqa: DTZ001
        )


def test_input_transitions_not_mutated() -> None:
    transition = tr("t1", "c1", FunnelStage.OBSERVED)
    before = transition.canonical()
    funnel = OpportunityFunnel()
    funnel.record(transition)
    funnel.report()
    assert transition.canonical() == before


def test_no_registry_broker_network_or_persistence_imports() -> None:
    import trading_application.opportunity_funnel as module

    assert module.__file__ is not None
    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    for fragment in ("datetime.now", "time.time", "utcnow", "random.", "uuid4"):
        assert fragment not in source, fragment
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "model_registry", "trading_broker", "ib" + "api", "httpx", "requests",
            "socket", "trading_persistence", "trading_event_bus",
        ):
            assert forbidden not in line, line
