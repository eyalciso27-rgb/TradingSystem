"""Authentication + RBAC enforcement tests (IMP-0301).

Done criterion: unauthorized approval/policy/emergency actions are denied and
audited. All enforcement is server-side (authorize/require), deny by default.
"""

import pytest
from trading_application.auth import (
    AuditSink,
    AuthAction,
    AuthorizationError,
    InMemoryAuditSink,
    Principal,
    Role,
    authorize,
    require,
)


def _principal(
    *roles: Role, mfa: bool = True, grants: frozenset[AuthAction] = frozenset()
) -> Principal:
    return Principal(
        principal_id="user-1", roles=frozenset(roles), mfa_verified=mfa, explicit_grants=grants
    )


@pytest.fixture()
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


# --- Deny by default ---

def test_principal_without_role_is_denied_everything(audit: InMemoryAuditSink) -> None:
    nobody = Principal(principal_id="anon")
    for action in AuthAction:
        assert not authorize(nobody, action, audit).allowed
    assert all(not d.allowed for d in audit.decisions)


def test_service_identity_gets_no_dashboard_actions(audit: InMemoryAuditSink) -> None:
    service = _principal(Role.SERVICE_IDENTITY)
    for action in AuthAction:
        assert not authorize(service, action, audit).allowed


# --- Matrix positives ---

def test_owner_matrix_permissions(audit: InMemoryAuditSink) -> None:
    owner = _principal(Role.OWNER)
    for action in (
        AuthAction.VIEW_DASHBOARD,
        AuthAction.APPROVE_PAPER_MANUAL,
        AuthAction.PROMOTE_STRATEGY_POLICY,
        AuthAction.ACTIVATE_KILL_SWITCH,
        AuthAction.CHANGE_SECRETS,
    ):
        assert authorize(owner, action, audit).allowed, action


def test_trader_can_approve_paper_but_not_promote_policy(audit: InMemoryAuditSink) -> None:
    trader = _principal(Role.TRADER_APPROVER)
    assert authorize(trader, AuthAction.APPROVE_PAPER_MANUAL, audit).allowed
    assert not authorize(trader, AuthAction.PROMOTE_STRATEGY_POLICY, audit).allowed


def test_ops_admin_can_kill_switch_but_not_approve_trades(audit: InMemoryAuditSink) -> None:
    ops = _principal(Role.OPERATIONS_ADMIN)
    assert authorize(ops, AuthAction.ACTIVATE_KILL_SWITCH, audit).allowed
    assert not authorize(ops, AuthAction.APPROVE_PAPER_MANUAL, audit).allowed


# --- Matrix negatives / no escalation ---

def test_read_only_cannot_change_config_or_approve(audit: InMemoryAuditSink) -> None:
    viewer = _principal(Role.READ_ONLY)
    assert authorize(viewer, AuthAction.VIEW_DASHBOARD, audit).allowed
    for action in (
        AuthAction.PROMOTE_STRATEGY_POLICY,
        AuthAction.APPROVE_PAPER_MANUAL,
        AuthAction.CHANGE_SECRETS,
        AuthAction.ACTIVATE_KILL_SWITCH,
        AuthAction.CORRECT_ENTITY_LABEL,
    ):
        assert not authorize(viewer, action, audit).allowed, action


def test_reviewer_cannot_approve_or_execute(audit: InMemoryAuditSink) -> None:
    reviewer = _principal(Role.REVIEWER)
    assert authorize(reviewer, AuthAction.CORRECT_ENTITY_LABEL, audit).allowed
    assert not authorize(reviewer, AuthAction.APPROVE_PAPER_MANUAL, audit).allowed


def test_explicit_grant_is_scoped_to_its_action_only(audit: InMemoryAuditSink) -> None:
    # A kill-switch grant must not escalate into secrets or policy promotion.
    trader = _principal(
        Role.TRADER_APPROVER, grants=frozenset({AuthAction.ACTIVATE_KILL_SWITCH})
    )
    assert authorize(trader, AuthAction.ACTIVATE_KILL_SWITCH, audit).allowed
    assert not authorize(trader, AuthAction.CHANGE_SECRETS, audit).allowed
    assert not authorize(trader, AuthAction.PROMOTE_STRATEGY_POLICY, audit).allowed


def test_explicit_grant_requires_eligible_role(audit: InMemoryAuditSink) -> None:
    # The same grant on an ineligible role (READ_ONLY) stays denied.
    viewer = _principal(Role.READ_ONLY, grants=frozenset({AuthAction.ACTIVATE_KILL_SWITCH}))
    assert not authorize(viewer, AuthAction.ACTIVATE_KILL_SWITCH, audit).allowed


# --- PAPER vs LIVE separation ---

def test_paper_approver_cannot_approve_live(audit: InMemoryAuditSink) -> None:
    trader = _principal(Role.TRADER_APPROVER)
    assert authorize(trader, AuthAction.APPROVE_PAPER_MANUAL, audit).allowed
    assert not authorize(trader, AuthAction.APPROVE_LIVE_MANUAL, audit).allowed


def test_live_is_blocked_by_default_even_for_owner(audit: InMemoryAuditSink) -> None:
    owner = _principal(Role.OWNER)
    decision = authorize(owner, AuthAction.APPROVE_LIVE_MANUAL, audit)
    assert not decision.allowed
    assert "baseline" in decision.reason  # blocked by live_enabled=false, not by role


def test_live_grant_does_not_bypass_baseline_block(audit: InMemoryAuditSink) -> None:
    trader = _principal(
        Role.TRADER_APPROVER, grants=frozenset({AuthAction.APPROVE_LIVE_MANUAL})
    )
    assert not authorize(trader, AuthAction.APPROVE_LIVE_MANUAL, audit).allowed


# --- MFA hooks (Security spec 4.1) ---

def test_privileged_roles_require_mfa_for_privileged_actions(
    audit: InMemoryAuditSink,
) -> None:
    owner_no_mfa = _principal(Role.OWNER, mfa=False)
    assert authorize(owner_no_mfa, AuthAction.VIEW_DASHBOARD, audit).allowed
    decision = authorize(owner_no_mfa, AuthAction.APPROVE_PAPER_MANUAL, audit)
    assert not decision.allowed
    assert "MFA" in decision.reason


def test_read_only_does_not_need_mfa_to_view(audit: InMemoryAuditSink) -> None:
    viewer = _principal(Role.READ_ONLY, mfa=False)
    assert authorize(viewer, AuthAction.VIEW_DASHBOARD, audit).allowed


# --- Audit + server-side enforcement ---

def test_every_denial_is_audited_with_reason(audit: InMemoryAuditSink) -> None:
    reviewer = _principal(Role.REVIEWER)
    authorize(reviewer, AuthAction.APPROVE_PAPER_MANUAL, audit)  # approval
    authorize(reviewer, AuthAction.PROMOTE_STRATEGY_POLICY, audit)  # policy
    authorize(reviewer, AuthAction.ACTIVATE_KILL_SWITCH, audit)  # emergency
    denials = [d for d in audit.decisions if not d.allowed]
    assert len(denials) == 3
    assert all(d.reason for d in denials)
    assert {d.action for d in denials} == {
        AuthAction.APPROVE_PAPER_MANUAL,
        AuthAction.PROMOTE_STRATEGY_POLICY,
        AuthAction.ACTIVATE_KILL_SWITCH,
    }


def test_require_raises_server_side_after_auditing(audit: InMemoryAuditSink) -> None:
    viewer = _principal(Role.READ_ONLY)
    with pytest.raises(AuthorizationError):
        require(viewer, AuthAction.CHANGE_SECRETS, audit)
    assert audit.decisions and not audit.decisions[-1].allowed


def test_custom_audit_sink_protocol(audit: InMemoryAuditSink) -> None:
    sink: AuditSink = audit  # structural typing: any sink can persist decisions
    require(_principal(Role.OWNER), AuthAction.VIEW_DASHBOARD, sink)
    assert audit.decisions[-1].allowed
