"""Canonical identities for the objects an execution is authorized against.

Two producers were naming the same things differently, and nothing noticed
because nothing compared them. ``FinalPreSubmitCheck`` emitted
``decision_ref = decision_hash`` while ``RiskEngine`` emitted
``decision_ref = "decision:{id}@{hash}"``; the pre-submit check's ``policy_ref``
named the PRE-SUBMIT policy while the risk result's named the RISK policy. A
consumer comparing them therefore rejected a perfectly valid authorization, and
the only reason the tests passed is that they built the pre-submit result by
hand with references chosen to match.

So the identities live here, once, and both producers use them. That is the
whole point: an identity that two components compute differently is not an
identity.

Two properties matter beyond consistency:

* **``risk_ref`` must identify the exact risk result.** It used to be
  ``"risk:APPROVE"``, which every approving evaluation in the system shares —
  so an authorization for one decision's risk result was indistinguishable from
  an authorization for another's. It is now a hash over the result's own
  content.
* **The pre-submit policy and the risk policy are different facts.** They are
  named separately and must never be compared to each other.

Every identity is a hash over canonical JSON of the object's own fields, so two
objects that differ anywhere produce different identities and equal objects
always produce the same one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields as dataclass_fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_application.decision_engine import EngineDecision
    from trading_application.execution_plan import ExecutablePlan
    from trading_application.risk_engine import RiskPolicy, RiskResult, TradeRiskRequest

IDENTITY_VERSION = "execution-identity-1.0.0"


#: Any frozen record whose fields are the thing being identified. Kept as a
#: loose alias rather than a Protocol because ``__dataclass_fields__`` is a
#: class variable and no structural type expresses that cleanly; the functions
#: below only ever read fields.
_Dataclassish = Any


def _digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _content(obj: _Dataclassish, *, exclude: frozenset[str] = frozenset()) -> str:
    """Hash every field of a frozen record, so any difference shows."""
    return _digest(
        {
            field.name: str(getattr(obj, field.name))
            for field in dataclass_fields(obj)
            if field.name not in exclude
        }
    )


def decision_identity(decision: EngineDecision) -> str:
    """The canonical decision reference.

    Carries both the id and the hash: the id says WHICH decision and the hash
    says which VERSION of it, and a reference with only one of those cannot
    detect a decision that was re-made with different content.
    """
    result = decision.result
    return f"decision:{result.decision_id}@{result.decision_hash}"


def risk_policy_identity(policy: RiskPolicy) -> str:
    """The risk policy, by version AND content.

    Two policies sharing a version and differing in a limit are two policies,
    and an audit that reports limits which were never enforced is worse than no
    audit at all.
    """
    return f"risk-policy:{policy.policy_version}@sha256:{_content(policy)}"


def risk_request_identity(request: TradeRiskRequest) -> str:
    """THE exact risk question that was asked.

    A risk RESULT is a set of output numbers, and two materially different
    questions can produce identical outputs: same quantity, same prices, same
    approved risk, but AAPL instead of MSFT, or a different sector, source or
    cluster where no cap happened to bind. Hashing only the result therefore
    gave both evaluations the SAME identity, so an authorization derived from
    one could be presented for the other and every reference would line up.

    This hashes the request itself -- symbol, sector, cluster, source,
    strategy, environment, requested quantity, entry and stop -- so the
    identity says which question was asked, not merely what came back.
    """
    return f"risk-request:{request.symbol}@sha256:{_content(request)}"


def risk_result_identity(result: RiskResult) -> str:
    """THE exact risk evaluation, not merely its outcome.

    ``"risk:APPROVE"`` is shared by every approving evaluation in the system, so
    an authorization carrying it could be presented against a different
    decision's risk result and look correct. This is content-addressed, so it
    identifies one evaluation and no other.

    The result carries ``request_ref``, so this hash covers the QUESTION as
    well as the answer: two evaluations whose output numbers coincide but whose
    requests differ can no longer share an identity.
    """
    return f"risk-result:sha256:{_content(result)}"


def pre_submit_policy_identity(policy: _Dataclassish) -> str:
    """The PRE-SUBMIT policy. Deliberately its own namespace.

    Never comparable to a risk policy identity: they govern different checks,
    and conflating them is what made a valid authorization look invalid.
    """
    version = str(getattr(policy, "policy_version", "unknown"))
    return f"pre-submit-policy:{version}@sha256:{_content(policy)}"


def portfolio_policy_identity(policy: _Dataclassish) -> str:
    """The PORTFOLIO policy, by version AND content.

    A third distinct namespace. Risk, portfolio and pre-submit policies govern
    different checks, and a version string alone would let two materially
    different policies share a name -- so an audit could report caps that were
    never applied.
    """
    version = str(getattr(policy, "policy_version", "unknown"))
    return f"portfolio-policy:{version}@sha256:{_content(policy)}"


def allocation_identity(allocation: _Dataclassish) -> str:
    """THE exact CandidateAllocation, content-addressed.

    Not ``allocation:{candidate}@{action}``: many allocations for one candidate
    can share an action, so that could never prove WHICH allocation an
    authorization was sized against.
    """
    candidate = str(getattr(allocation, "candidate_id", "unknown"))
    return f"allocation:{candidate}@sha256:{_content(allocation)}"


def plan_identity(plan: ExecutablePlan) -> str:
    """THE exact order, and the exact evaluation that permitted it.

    This is the seal. Everything that could change the order -- symbol, side,
    quantity, order type, time in force, entry and stop prices, environment and
    account -- is inside the hash, together with the decision, risk request,
    risk result and allocation refs that authorized it.

    So there is no order fact a later component can vary on its own: an agent
    that builds a different order builds a different identity, and the
    comparison is equality, never a prefix.
    """
    return f"plan:{plan.symbol}@sha256:{_content(plan)}"


def authorization_identity(authorization: _Dataclassish) -> str:
    """One minted SubmitAuthorization, by its own content.

    Content-addressed over every field, which includes ``plan_ref`` -- so this
    reference identifies the exact authorization AND says which plan it
    authorized. The agent used to build ``submit-auth:{candidate}@{timestamp}``
    by hand for the broker receipt and the audit lineage: that proved neither,
    and every authorization minted for one candidate within the same second
    shared it. There is deliberately ONE authorization identity format, and
    this is it.
    """
    candidate = str(getattr(authorization, "candidate_id", "unknown"))
    return f"submit-auth:{candidate}@sha256:{_content(authorization)}"


__all__ = [
    "IDENTITY_VERSION",
    "allocation_identity",
    "authorization_identity",
    "decision_identity",
    "portfolio_policy_identity",
    "plan_identity",
    "pre_submit_policy_identity",
    "risk_policy_identity",
    "risk_request_identity",
    "risk_result_identity",
]
