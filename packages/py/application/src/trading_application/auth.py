"""Authentication identity + RBAC enforcement (IMP-0301).

Authority: Security_and_Operations_Specification_v0.6 section 4 — the
canonical role table (OWNER, TRADER_APPROVER, OPERATIONS_ADMIN, REVIEWER,
READ_ONLY, SERVICE_IDENTITY), the basic permission matrix (4.2, including
"by explicit grant" cells) and the login requirements (4.1: MFA mandatory for
OWNER/TRADER_APPROVER/OPERATIONS_ADMIN; auth failures and permission events
are audited; service identities never use human passwords).

Separation of concerns:

- Authentication answers "who is this" and yields a ``Principal`` (identity,
  roles, MFA state). Token/session mechanics are NOT invented here — the
  encrypted runtime session store is IMP-0303; MFA itself is a hook: the
  principal carries ``mfa_verified`` and privileged roles cannot act without
  it.
- Authorization answers "may this principal perform this action", enforced
  server-side by ``authorize``/``require`` with **deny by default**: no role
  -> deny; an action not granted by the matrix -> deny; "by explicit grant"
  cells require both the eligible role and a per-principal grant (a grant can
  never escalate beyond its own action).

LIVE stays blocked by default: APPROVE_LIVE_MANUAL is denied for everyone —
including OWNER — while the baseline keeps live trading disabled
(trading_config.environments.LIVE_ENABLED == False).

Every denial (and every decision) is recorded through an ``AuditSink`` —
tamper-evident persistent audit arrives with IMP-0304.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from trading_config.environments import LIVE_ENABLED


class Role(StrEnum):
    """Canonical roles (Security spec section 4)."""

    OWNER = "OWNER"
    TRADER_APPROVER = "TRADER_APPROVER"
    OPERATIONS_ADMIN = "OPERATIONS_ADMIN"
    REVIEWER = "REVIEWER"
    READ_ONLY = "READ_ONLY"
    SERVICE_IDENTITY = "SERVICE_IDENTITY"


class AuthAction(StrEnum):
    """Canonical actions of the basic permission matrix (section 4.2)."""

    VIEW_DASHBOARD = "VIEW_DASHBOARD"
    APPROVE_PAPER_MANUAL = "APPROVE_PAPER_MANUAL"
    APPROVE_LIVE_MANUAL = "APPROVE_LIVE_MANUAL"
    PROMOTE_STRATEGY_POLICY = "PROMOTE_STRATEGY_POLICY"
    ACTIVATE_KILL_SWITCH = "ACTIVATE_KILL_SWITCH"
    CHANGE_SECRETS = "CHANGE_SECRETS"
    CORRECT_ENTITY_LABEL = "CORRECT_ENTITY_LABEL"


# The matrix's unconditional "yes" cells. Anything absent is denied.
ROLE_PERMISSIONS: Final[dict[Role, frozenset[AuthAction]]] = {
    Role.OWNER: frozenset(
        {
            AuthAction.VIEW_DASHBOARD,
            AuthAction.APPROVE_PAPER_MANUAL,
            AuthAction.APPROVE_LIVE_MANUAL,
            AuthAction.PROMOTE_STRATEGY_POLICY,
            AuthAction.ACTIVATE_KILL_SWITCH,
            AuthAction.CHANGE_SECRETS,
            AuthAction.CORRECT_ENTITY_LABEL,
        }
    ),
    Role.TRADER_APPROVER: frozenset(
        {
            AuthAction.VIEW_DASHBOARD,
            AuthAction.APPROVE_PAPER_MANUAL,
            AuthAction.CORRECT_ENTITY_LABEL,
        }
    ),
    Role.OPERATIONS_ADMIN: frozenset(
        {
            AuthAction.VIEW_DASHBOARD,
            AuthAction.ACTIVATE_KILL_SWITCH,
            AuthAction.CORRECT_ENTITY_LABEL,
        }
    ),
    Role.REVIEWER: frozenset(
        {AuthAction.VIEW_DASHBOARD, AuthAction.CORRECT_ENTITY_LABEL}
    ),
    Role.READ_ONLY: frozenset({AuthAction.VIEW_DASHBOARD}),
    # Machine identities get narrow per-service permissions, none of the
    # human dashboard actions by default (section 4).
    Role.SERVICE_IDENTITY: frozenset(),
}

# The matrix's "by explicit grant" cells: the role is eligible only when the
# principal also carries a per-principal grant for that exact action.
EXPLICIT_GRANT_ELIGIBLE: Final[dict[AuthAction, frozenset[Role]]] = {
    AuthAction.APPROVE_LIVE_MANUAL: frozenset({Role.TRADER_APPROVER}),
    AuthAction.ACTIVATE_KILL_SWITCH: frozenset({Role.TRADER_APPROVER}),
    AuthAction.CHANGE_SECRETS: frozenset({Role.OPERATIONS_ADMIN}),
}

# MFA is mandatory for these roles (section 4.1); without a verified MFA the
# principal may only view.
MFA_REQUIRED_ROLES: Final[frozenset[Role]] = frozenset(
    {Role.OWNER, Role.TRADER_APPROVER, Role.OPERATIONS_ADMIN}
)


@dataclass(frozen=True)
class Principal:
    """An authenticated identity (output of authentication, input to RBAC)."""

    principal_id: str
    roles: frozenset[Role] = frozenset()
    mfa_verified: bool = False
    explicit_grants: frozenset[AuthAction] = frozenset()


@dataclass(frozen=True)
class AuthorizationDecision:
    """One audited decision; ``action`` may be an RBAC action or another
    audited event name (e.g. quarantine transitions)."""

    allowed: bool
    principal_id: str
    action: AuthAction | str
    reason: str


class AuditSink(Protocol):
    """Receives every authorization decision (persistence: IMP-0304)."""

    def record(self, decision: AuthorizationDecision) -> None: ...


class InMemoryAuditSink:
    """Test/bootstrap sink collecting decisions in memory."""

    def __init__(self) -> None:
        self.decisions: list[AuthorizationDecision] = []

    def record(self, decision: AuthorizationDecision) -> None:
        self.decisions.append(decision)


class AuthorizationError(PermissionError):
    """The action was denied; the denial has been audited."""


class Authenticator(Protocol):
    """Authentication port: resolves a request context to a Principal.

    Concrete credential/session mechanics are defined by IMP-0302/0303;
    nothing here invents tokens, passwords or expiry rules.
    """

    def authenticate(self, credential_ref: str) -> Principal | None: ...


def authorize(
    principal: Principal, action: AuthAction, audit: AuditSink
) -> AuthorizationDecision:
    """Server-side RBAC decision, deny by default; every decision is audited."""

    def decide(allowed: bool, reason: str) -> AuthorizationDecision:
        decision = AuthorizationDecision(
            allowed=allowed,
            principal_id=principal.principal_id,
            action=action,
            reason=reason,
        )
        audit.record(decision)
        return decision

    if not principal.roles:
        return decide(False, "principal has no role")

    if action is AuthAction.APPROVE_LIVE_MANUAL and not LIVE_ENABLED:
        return decide(False, "live trading is disabled by baseline policy")

    granted_by_role = any(action in ROLE_PERMISSIONS[role] for role in principal.roles)
    granted_explicitly = action in principal.explicit_grants and bool(
        principal.roles & EXPLICIT_GRANT_ELIGIBLE.get(action, frozenset())
    )
    if not granted_by_role and not granted_explicitly:
        return decide(False, "no role or explicit grant permits this action")

    if (
        action is not AuthAction.VIEW_DASHBOARD
        and principal.roles & MFA_REQUIRED_ROLES
        and not principal.mfa_verified
    ):
        return decide(False, "MFA verification required for this role")

    return decide(True, "granted")


def require(principal: Principal, action: AuthAction, audit: AuditSink) -> None:
    """Enforce ``authorize``; raise (after auditing) when denied."""
    decision = authorize(principal, action, audit)
    if not decision.allowed:
        raise AuthorizationError(
            f"{principal.principal_id} may not {action.value}: {decision.reason}"
        )
