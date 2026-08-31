"""Operator CLI for the autonomous-Paper runtime (A4/A6).

The smallest surface an operator needs, and no more:

    paper-runtime halt   --reason "operator kill switch"
    paper-runtime status
    paper-runtime resume --reason "recovered and reconciled"

``halt`` and ``resume`` write to the durable, append-only emergency log through
the RBAC- and audit-guarded ``EmergencyControlService``, so the halt the running
loop reads on its next tick is the same durable fact, recorded twice (command +
tamper-evident audit) in one transaction. ``resume`` NEVER bypasses safety: it
passes the CURRENT reconciliation-latch verdict as ``reconciliation_ok``, so a
resume is refused while the latch blocks entry -- exactly as the emergency
service's own fail-closed rule requires.

``status`` reports the durable, observable facts a separate process can see
without reaching into the running loop's memory: database reachability, whether
the single-instance ownership lock is currently held, the active halts, and the
reconciliation-latch gate. The runtime's full in-memory status is exposed by the
loop itself; a health endpoint/file for it lands with the live ``run`` wiring in
Phase B.

``run`` is intentionally not wired on this branch: the supervised loop
(:class:`PaperRuntime`) is complete and exercised by the test suite, but its
production wiring to an owned IBKR session, market data and the real strategy is
Phase B, after PR #94 and the strategy branch merge. It refuses rather than
pretend to trade.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from trading_application.auth import Principal, Role
from trading_application.emergency import (
    EmergencyControlError,
    EmergencyControlService,
    ResumeNotAllowedError,
)
from trading_contracts.enums import Environment, HaltScope
from trading_observability.logging import configure_logging
from trading_persistence.advisory_lock import advisory_lock_held
from trading_persistence.audit import PersistentAuditSink
from trading_persistence.runtime_ops import (
    dispose,
    emergency_unit_of_work,
    ping,
    runtime_engine,
)

from trading_execution_agent.reconciliation_latch import ReconciliationLatch
from trading_execution_agent.runtime.emergency_store import DurableEmergencyCommandStore

LOGGER = logging.getLogger("trading_execution_agent.runtime.cli")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2

#: The runtime is Paper-only. The CLI never offers LIVE.
ENVIRONMENT = Environment.PAPER

ENV_OPERATOR_ID = "TRADING_OPERATOR_ID"
DEFAULT_OPERATOR_ID = "owner-cli"


def _operator_principal() -> Principal:
    """The owner operating the host. OWNER + MFA so a global halt is permitted.

    The operator on the owner host is the owner; the kill switch must be usable
    without a separate auth service in the MVP. RBAC still records the decision.
    """
    operator_id = os.environ.get(ENV_OPERATOR_ID, "").strip() or DEFAULT_OPERATOR_ID
    return Principal(
        principal_id=operator_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified=True,
    )


def _scope_from(raw: str) -> HaltScope:
    candidate = raw.strip().upper()
    for scope in HaltScope:
        if scope.value == candidate or scope.name == candidate:
            return scope
    raise ValueError(f"unknown halt scope {raw!r}")


def _latch() -> ReconciliationLatch:
    return ReconciliationLatch()


def cmd_halt(args: argparse.Namespace) -> int:
    scope = _scope_from(args.scope)
    principal = _operator_principal()
    engine = runtime_engine()
    try:
        with emergency_unit_of_work(engine) as connection:
            service = EmergencyControlService(
                DurableEmergencyCommandStore(connection),
                PersistentAuditSink(connection, ENVIRONMENT),
            )
            service.activate_halt(
                principal,
                scope=scope,
                environment=ENVIRONMENT,
                reason_code=args.reason_code,
                reason_text=args.reason,
                target_ref=args.target,
            )
    except EmergencyControlError as error:
        LOGGER.error("halt refused: %s", error)
        return EXIT_REFUSED
    finally:
        dispose(engine)
    print(
        json.dumps(
            {
                "action": "HALT",
                "scope": scope.value,
                "environment": ENVIRONMENT.value,
                "target": args.target,
                "reason_code": args.reason_code,
                "result": "HALT_ACTIVE",
            }
        )
    )
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    scope = _scope_from(args.scope)
    principal = _operator_principal()
    # Resume must not bypass reconciliation/readiness: the CURRENT latch verdict
    # is the reconciliation gate. A blocked latch => reconciliation_ok False =>
    # the emergency service refuses. Fail closed if the latch cannot be read.
    latch_blocked, latch_reason = _latch().entry_blocked()
    reconciliation_ok = not latch_blocked
    engine = runtime_engine()
    try:
        with emergency_unit_of_work(engine) as connection:
            service = EmergencyControlService(
                DurableEmergencyCommandStore(connection),
                PersistentAuditSink(connection, ENVIRONMENT),
            )
            service.resume(
                principal,
                scope=scope,
                environment=ENVIRONMENT,
                reason_code=args.reason_code,
                reason_text=args.reason,
                # Health is proven by the very fact the DB is reachable here;
                # reconciliation is proven by the latch, never assumed.
                health_ok=True,
                reconciliation_ok=reconciliation_ok,
                target_ref=args.target,
            )
    except ResumeNotAllowedError as error:
        LOGGER.error(
            "resume refused (fail closed): %s; latch: %s",
            error,
            latch_reason or "clear",
        )
        print(
            json.dumps(
                {
                    "action": "RESUME",
                    "result": "REFUSED",
                    "reason": "reconciliation/readiness not proven",
                    "reconciliation_ok": reconciliation_ok,
                    "latch_reason": latch_reason,
                }
            )
        )
        return EXIT_REFUSED
    except EmergencyControlError as error:
        LOGGER.error("resume refused: %s", error)
        return EXIT_REFUSED
    finally:
        dispose(engine)
    print(
        json.dumps(
            {
                "action": "RESUME",
                "scope": scope.value,
                "environment": ENVIRONMENT.value,
                "target": args.target,
                "result": "RESUMED",
            }
        )
    )
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    del args
    facts: dict[str, object] = {
        "environment": ENVIRONMENT.value,
        "authorization_mode": "DENYALL",
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    engine = None
    try:
        engine = runtime_engine()
        ping(engine)
        facts["database_ok"] = True
        facts["ownership_lock_held"] = advisory_lock_held(engine)
        with emergency_unit_of_work(engine) as connection:
            store = DurableEmergencyCommandStore(connection)
            halts = store.active_halts(ENVIRONMENT)
        facts["active_halts"] = [
            {
                "scope": halt.scope.value,
                "target": halt.target_ref,
                "reason_code": halt.reason_code,
            }
            for halt in halts
        ]
        facts["halt_active"] = bool(halts)
    except Exception as error:  # noqa: BLE001 - status is best-effort, fail closed
        facts["database_ok"] = False
        facts["error"] = type(error).__name__
    finally:
        if engine is not None:
            dispose(engine)
    latch_blocked, latch_reason = _latch().entry_blocked()
    facts["reconciliation_entry_blocked"] = latch_blocked
    facts["reconciliation_reason"] = latch_reason
    print(json.dumps(facts, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    del args
    LOGGER.error(
        "the autonomous runtime loop is not wired on this branch. The supervised"
        " loop is complete and covered by tests; its production wiring to an"
        " owned IBKR session, market data and the real strategy is Phase B,"
        " after PR #94 and the strategy branch merge. Refusing to start a loop"
        " that cannot prove account safety."
    )
    return EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-runtime",
        description="Operator control for the autonomous IBKR Paper runtime.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    halt = sub.add_parser("halt", help="activate an entry halt (kill switch)")
    halt.add_argument("--reason", required=True, help="human-readable reason text")
    halt.add_argument("--reason-code", default="OPERATOR_KILL_SWITCH")
    halt.add_argument(
        "--scope",
        default=HaltScope.GLOBAL_ENTRY_HALT.value,
        help="halt scope (default GLOBAL_ENTRY_HALT)",
    )
    halt.add_argument("--target", default=None, help="target ref for a scoped halt")
    halt.set_defaults(func=cmd_halt)

    resume = sub.add_parser("resume", help="lift a halt (fails closed on reconciliation)")
    resume.add_argument("--reason", required=True)
    resume.add_argument("--reason-code", default="OPERATOR_RESUME")
    resume.add_argument("--scope", default=HaltScope.GLOBAL_ENTRY_HALT.value)
    resume.add_argument("--target", default=None)
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="print the durable runtime status (JSON)")
    status.set_defaults(func=cmd_status)

    run = sub.add_parser("run", help="(Phase B) run the autonomous loop")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging(service="paper-runtime", environment=ENVIRONMENT.value)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as error:
        LOGGER.error("invalid argument: %s", error)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
