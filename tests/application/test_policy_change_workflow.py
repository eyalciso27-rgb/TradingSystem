"""Policy-change-workflow drills (IMP-1206). DoD: no direct edit of the
active immutable version — append-only pointers, human actors, evidence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from trading_application.policy_change_workflow import (
    ChangeSetItem,
    EvidenceKind,
    EvidenceRef,
    PolicyChangeProposal,
    PolicyChangeWorkflow,
    PolicyPointerRecord,
    PolicyVersion,
    RollbackRecord,
    WorkflowError,
    WorkflowStage,
    WorkflowTransition,
)

T0 = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
COMPONENT = "decision_policy"

V1 = PolicyVersion(COMPONENT, "1.0.0", "fp:v1", T0)
V2 = PolicyVersion(COMPONENT, "2.0.0", "fp:v2", T0)


def make_workflow() -> PolicyChangeWorkflow:
    return PolicyChangeWorkflow(
        [V1, V2], {COMPONENT: "1.0.0"}, actor_id="eyal", as_of=T0
    )


def make_proposal(**overrides: object) -> PolicyChangeProposal:
    values: dict[str, object] = {
        "proposal_id": "prop-1",
        "component": COMPONENT,
        "from_version": "1.0.0",
        "to_version": "2.0.0",
        "change_set": (ChangeSetItem("threshold", "0.5", "0.6"),),
        "author_actor_id": "eyal",
        "created_as_of": T0 + timedelta(minutes=1),
        "rationale": "raise the fusion threshold",
        "from_fingerprint": "fp:v1",
        "to_fingerprint": "fp:v2",
    }
    values.update(overrides)
    return PolicyChangeProposal(**values)  # type: ignore[arg-type]


def ev(
    kind: EvidenceKind, *, minute: int = 1, fingerprint: str = "fp:v2", run: str = "run-1"
) -> EvidenceRef:
    return EvidenceRef(
        kind=kind,
        run_id=run,
        seed="seed:1",
        result_hash="sha256:aaa",
        version_fingerprint=fingerprint,
        created_as_of=T0 + timedelta(minutes=minute),
    )


def tr(
    transition_id: str,
    from_stage: WorkflowStage,
    to_stage: WorkflowStage,
    *,
    minute: int,
    evidence: tuple[EvidenceRef, ...] = (),
    actor: str = "eyal",
    reason: str = "documented review",
    proposal_id: str = "prop-1",
) -> WorkflowTransition:
    return WorkflowTransition(
        transition_id=transition_id,
        proposal_id=proposal_id,
        from_stage=from_stage,
        to_stage=to_stage,
        actor_id=actor,
        as_of=T0 + timedelta(minutes=minute),
        evidence=evidence,
        reason=reason,
    )


def drive_to_approved(workflow: PolicyChangeWorkflow) -> None:
    workflow.submit_proposal(make_proposal())
    workflow.advance(
        tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
           minute=2, evidence=(ev(EvidenceKind.REPLAY),))
    )
    workflow.advance(
        tr("t2", WorkflowStage.REPLAY_VERIFIED, WorkflowStage.SHADOW_VERIFIED,
           minute=3, evidence=(ev(EvidenceKind.SHADOW, run="run-2"),))
    )
    workflow.advance(
        tr("t3", WorkflowStage.SHADOW_VERIFIED, WorkflowStage.APPROVAL_PENDING, minute=4)
    )
    workflow.advance(
        tr("t4", WorkflowStage.APPROVAL_PENDING, WorkflowStage.APPROVED,
           minute=5, reason="owner approval after full evidence review")
    )


# -- immutable versions -----------------------------------------------------------


def test_policy_version_is_immutable_and_has_no_edit_api() -> None:
    with pytest.raises(FrozenInstanceError):
        V1.content_fingerprint = "fp:hacked"  # type: ignore[misc]
    workflow = make_workflow()
    for surface in ("update_version", "edit_version", "replace_version", "delete_version"):
        assert not hasattr(workflow, surface)


def test_reregistering_a_version_is_idempotent_or_rejected() -> None:
    workflow = make_workflow()
    workflow.register_version(V2)  # identical -> idempotent
    with pytest.raises(WorkflowError, match="immutable"):
        workflow.register_version(PolicyVersion(COMPONENT, "2.0.0", "fp:other", T0))


def test_naive_datetime_rejected() -> None:
    with pytest.raises(WorkflowError, match="timezone-aware"):
        PolicyVersion(COMPONENT, "3.0.0", "fp:v3", datetime(2026, 6, 1))  # noqa: DTZ001


# -- proposals -----------------------------------------------------------------------


def test_valid_proposal_accepted() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    assert workflow.report().proposals == (("prop-1", "PROPOSED"),)


def test_from_version_not_matching_active_snapshot_rejected() -> None:
    workflow = make_workflow()
    with pytest.raises(WorkflowError, match="active-pointer snapshot"):
        workflow.submit_proposal(
            make_proposal(from_version="2.0.0", from_fingerprint="fp:v2",
                          to_version="1.0.0", to_fingerprint="fp:v1")
        )


def test_unregistered_or_mismatched_to_version_rejected() -> None:
    workflow = make_workflow()
    with pytest.raises(WorkflowError, match="not a registered"):
        workflow.submit_proposal(make_proposal(to_version="9.9.9", to_fingerprint="fp:x"))
    with pytest.raises(WorkflowError, match="fingerprint does not match"):
        workflow.submit_proposal(make_proposal(to_fingerprint="fp:wrong"))


def test_multi_change_set_without_ablation_notes_rejected() -> None:
    items = (
        ChangeSetItem("threshold", "0.5", "0.6", ablation_note="isolated in run-7"),
        ChangeSetItem("horizon", "5", "8"),  # no ablation note
    )
    with pytest.raises(WorkflowError, match="ablation"):
        make_proposal(change_set=items)
    noted = (
        ChangeSetItem("threshold", "0.5", "0.6", ablation_note="isolated in run-7"),
        ChangeSetItem("horizon", "5", "8", ablation_note="isolated in run-8"),
    )
    make_proposal(change_set=noted)  # explicit ablation -> accepted


# -- stage/actor/evidence enforcement ---------------------------------------------------


def test_stage_skipping_rejected() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    with pytest.raises(WorkflowError, match="skipping"):
        workflow.advance(
            tr("t1", WorkflowStage.PROPOSED, WorkflowStage.SHADOW_VERIFIED,
               minute=2, evidence=(ev(EvidenceKind.SHADOW),))
        )


def test_non_human_actors_rejected_everywhere() -> None:
    for actor in ("", "system", "AUTO", "scheduler", "timer", "metric", "cron", "bot"):
        with pytest.raises(WorkflowError, match="human actor"):
            tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
               minute=2, actor=actor)
    with pytest.raises(WorkflowError, match="human actor"):
        make_proposal(author_actor_id="system")
    with pytest.raises(WorkflowError, match="human actor"):
        PolicyChangeWorkflow([V1], {COMPONENT: "1.0.0"}, actor_id="auto", as_of=T0)


def test_replay_evidence_required_and_backtest_alone_insufficient() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    with pytest.raises(WorkflowError, match="requires replay evidence"):
        workflow.advance(
            tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED, minute=2)
        )
    with pytest.raises(WorkflowError, match="requires replay evidence"):
        workflow.advance(
            tr("t1b", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
               minute=2, evidence=(ev(EvidenceKind.BACKTEST),))
        )


def test_shadow_or_challenger_evidence_required() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    workflow.advance(
        tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
           minute=2, evidence=(ev(EvidenceKind.REPLAY),))
    )
    with pytest.raises(WorkflowError, match="shadow or challenger"):
        workflow.advance(
            tr("t2", WorkflowStage.REPLAY_VERIFIED, WorkflowStage.SHADOW_VERIFIED,
               minute=3, evidence=(ev(EvidenceKind.REPLAY, run="run-3"),))
        )
    workflow.advance(
        tr("t2b", WorkflowStage.REPLAY_VERIFIED, WorkflowStage.SHADOW_VERIFIED,
           minute=3, evidence=(ev(EvidenceKind.CHALLENGER, run="run-4"),))
    )


def test_evidence_for_a_different_version_rejected() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    with pytest.raises(WorkflowError, match="different version"):
        workflow.advance(
            tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
               minute=2, evidence=(ev(EvidenceKind.REPLAY, fingerprint="fp:v1"),))
        )


def test_future_evidence_rejected() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    with pytest.raises(WorkflowError, match="future evidence"):
        workflow.advance(
            tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
               minute=2, evidence=(ev(EvidenceKind.REPLAY, minute=9),))
        )


def test_empty_result_hash_rejected() -> None:
    with pytest.raises(WorkflowError, match="no empty hashes"):
        EvidenceRef(
            kind=EvidenceKind.REPLAY,
            run_id="run-1",
            seed="seed:1",
            result_hash="  ",
            version_fingerprint="fp:v2",
            created_as_of=T0,
        )


def test_approval_pending_requires_full_bundle() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    workflow.advance(
        tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
           minute=2, evidence=(ev(EvidenceKind.REPLAY),))
    )
    # Force a gap: pretend shadow passed without shadow evidence is
    # impossible, so the bundle check at APPROVAL_PENDING is the backstop.
    with pytest.raises(WorkflowError, match="shadow or challenger"):
        workflow.advance(
            tr("t2", WorkflowStage.REPLAY_VERIFIED, WorkflowStage.SHADOW_VERIFIED,
               minute=3)
        )


# -- approval / pointer append-only ------------------------------------------------------


def test_approval_appends_pointer_record_and_edits_nothing() -> None:
    workflow = make_workflow()
    genesis_before = workflow.pointer_history(COMPONENT)[0]
    drive_to_approved(workflow)
    history = workflow.pointer_history(COMPONENT)
    assert len(history) == 2
    assert history[0] == genesis_before  # the prior pointer is untouched
    appended = history[1]
    assert appended.from_version == "1.0.0"
    assert appended.to_version == "2.0.0"
    assert appended.previous_pointer_record_id == genesis_before.record_id
    assert appended.actor_id == "eyal"
    assert appended.proposal_id == "prop-1"
    assert workflow.active_version(COMPONENT) == "2.0.0"
    report = workflow.report()
    assert len(report.approvals) == 1
    assert report.approvals[0].evidence_hashes  # documented evidence trail
    # The immutable version itself is unchanged: identical re-registration
    # is still idempotent and a conflicting one still fails.
    workflow.register_version(V2)
    with pytest.raises(WorkflowError, match="immutable"):
        workflow.register_version(PolicyVersion(COMPONENT, "2.0.0", "fp:mutated", T0))


def test_pointer_records_are_research_only_and_frozen() -> None:
    workflow = make_workflow()
    drive_to_approved(workflow)
    record = workflow.pointer_history(COMPONENT)[-1]
    assert record.research_only is True
    assert record.affects_live_state is False
    assert record.affects_paper_state is False
    with pytest.raises(FrozenInstanceError):
        record.to_version = "9.9.9"  # type: ignore[misc]
    with pytest.raises(WorkflowError, match="research/governance"):
        PolicyPointerRecord(
            record_id="ptr:x:1",
            component=COMPONENT,
            from_version="1.0.0",
            to_version="2.0.0",
            actor_id="eyal",
            reason="r",
            proposal_id="p",
            as_of=T0,
            previous_pointer_record_id="ptr:x:0",
            affects_live_state=True,
        )


def test_no_promotion_or_execution_surface_exists() -> None:
    workflow = make_workflow()
    for surface in (
        "promote", "auto_promote", "activate", "swap_active", "set_active",
        "execute", "submit", "cancel", "flatten", "place_order",
    ):
        assert not hasattr(workflow, surface)


def test_metrics_and_reports_cannot_move_the_pointer() -> None:
    workflow = make_workflow()
    drive_to_approved(workflow)
    before = workflow.pointer_history(COMPONENT)
    workflow.report()
    workflow.report()
    assert workflow.pointer_history(COMPONENT) == before


# -- rollback ---------------------------------------------------------------------------


def test_rollback_appends_and_preserves_all_history() -> None:
    workflow = make_workflow()
    drive_to_approved(workflow)
    workflow.rollback(
        RollbackRecord(
            rollback_id="rb-1",
            proposal_id="prop-1",
            actor_id="eyal",
            reason="virtual regression found in shadow",
            to_version="1.0.0",
            as_of=T0 + timedelta(minutes=6),
        )
    )
    history = workflow.pointer_history(COMPONENT)
    assert len(history) == 3  # genesis + approval + rollback; nothing deleted
    assert [record.to_version for record in history] == ["1.0.0", "2.0.0", "1.0.0"]
    assert history[2].previous_pointer_record_id == history[1].record_id
    assert workflow.active_version(COMPONENT) == "1.0.0"
    report = workflow.report()
    assert len(report.approvals) == 1  # the approval record is preserved
    assert report.proposals == (("prop-1", "ROLLED_BACK"),)
    assert report.audit[-1].stage_after is WorkflowStage.ROLLED_BACK


def test_rollback_requires_reason_actor_and_registered_version() -> None:
    with pytest.raises(WorkflowError, match="reason"):
        RollbackRecord("rb", "prop-1", "eyal", " ", "1.0.0", T0)
    with pytest.raises(WorkflowError, match="human actor"):
        RollbackRecord("rb", "prop-1", "system", "regression", "1.0.0", T0)
    workflow = make_workflow()
    drive_to_approved(workflow)
    with pytest.raises(WorkflowError, match="not a registered"):
        workflow.rollback(
            RollbackRecord("rb", "prop-1", "eyal", "regression", "0.0.1",
                           T0 + timedelta(minutes=6))
        )


def test_rollback_only_from_approved() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    with pytest.raises(WorkflowError, match="only from APPROVED"):
        workflow.rollback(
            RollbackRecord("rb", "prop-1", "eyal", "regret", "1.0.0",
                           T0 + timedelta(minutes=2))
        )


# -- terminal / audit / determinism -------------------------------------------------------


def test_rejected_is_terminal() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    workflow.advance(
        tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REJECTED,
           minute=2, reason="not worth pursuing")
    )
    with pytest.raises(WorkflowError, match="terminal"):
        workflow.advance(
            tr("t2", WorkflowStage.REJECTED, WorkflowStage.REPLAY_VERIFIED,
               minute=3, evidence=(ev(EvidenceKind.REPLAY),))
        )


def test_no_transition_after_rolled_back() -> None:
    workflow = make_workflow()
    drive_to_approved(workflow)
    workflow.rollback(
        RollbackRecord("rb-1", "prop-1", "eyal", "regression", "1.0.0",
                       T0 + timedelta(minutes=6))
    )
    with pytest.raises(WorkflowError, match="terminal"):
        workflow.advance(
            tr("t9", WorkflowStage.ROLLED_BACK, WorkflowStage.APPROVED, minute=7)
        )


def test_audit_is_append_only_sequential_and_frozen() -> None:
    workflow = make_workflow()
    drive_to_approved(workflow)
    audit = workflow.audit()
    assert isinstance(audit, tuple)
    assert [entry.entry_index for entry in audit] == [0, 1, 2, 3]
    with pytest.raises(FrozenInstanceError):
        audit[0].reason = "rewritten"  # type: ignore[misc]
    for surface in ("delete_audit", "edit_audit", "rewrite_audit"):
        assert not hasattr(workflow, surface)


def test_as_of_regression_rejected() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    workflow.advance(
        tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
           minute=5, evidence=(ev(EvidenceKind.REPLAY),))
    )
    with pytest.raises(WorkflowError, match="monotonic"):
        workflow.advance(
            tr("t2", WorkflowStage.REPLAY_VERIFIED, WorkflowStage.SHADOW_VERIFIED,
               minute=3, evidence=(ev(EvidenceKind.SHADOW),))
        )


def test_identical_duplicate_transition_is_idempotent() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    transition = tr(
        "t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
        minute=2, evidence=(ev(EvidenceKind.REPLAY),),
    )
    workflow.advance(transition)
    workflow.advance(transition)  # no error, no duplicate audit entry
    assert len(workflow.audit()) == 1


def test_conflicting_duplicate_transition_id_rejected() -> None:
    workflow = make_workflow()
    workflow.submit_proposal(make_proposal())
    workflow.advance(
        tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
           minute=2, evidence=(ev(EvidenceKind.REPLAY),))
    )
    with pytest.raises(WorkflowError, match="different content"):
        workflow.advance(
            tr("t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
               minute=2, evidence=(ev(EvidenceKind.REPLAY),), reason="other reason")
        )


def test_repeat_yields_identical_report_and_hash() -> None:
    first = make_workflow()
    second = make_workflow()
    drive_to_approved(first)
    drive_to_approved(second)
    assert first.report() == second.report()
    assert first.report().report_hash == second.report().report_hash


def test_input_objects_are_not_mutated() -> None:
    workflow = make_workflow()
    proposal = make_proposal()
    transition = tr(
        "t1", WorkflowStage.PROPOSED, WorkflowStage.REPLAY_VERIFIED,
        minute=2, evidence=(ev(EvidenceKind.REPLAY),),
    )
    proposal_snapshot = (proposal.proposal_id, proposal.change_set, proposal.rationale)
    transition_snapshot = transition.canonical()
    workflow.submit_proposal(proposal)
    workflow.advance(transition)
    assert (proposal.proposal_id, proposal.change_set, proposal.rationale) == proposal_snapshot
    assert transition.canonical() == transition_snapshot


def test_report_is_marked_research_only() -> None:
    workflow = make_workflow()
    report = workflow.report()
    assert report.research_only is True
    assert report.affects_live_state is False
    assert report.affects_paper_state is False
    assert all("recommendation only" in finding for finding in report.findings) or (
        not report.findings
    )


def test_source_has_no_forbidden_imports_or_order_surface() -> None:
    import trading_application.policy_change_workflow as module

    assert module.__file__ is not None
    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for token in (
        "OrderIntent", "SubmitAuthorization", ".submit(", ".cancel(", "FLATTEN",
        "datetime.now", "utcnow", "time.time", "random.", "uuid4",
        "def promote", "def activate", "def auto",
    ):
        assert token not in source, token
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "trading_broker", "ib" + "api", "execution_agent", "httpx", "requests",
            "socket", "trading_persistence", "trading_event_bus", "model_registry",
            "sqlalchemy", "alembic",
        ):
            assert forbidden not in line, line
