"""POI-IBKR-READONLY harness — read-only infrastructure check, manual run only.

Purpose (PoI plan v2): after the OWNER has manually installed the official
IBKR Linux ARM64 IB Gateway, logged in to the PAPER environment in the
Gateway UI (with 2FA) and enabled the Gateway's Read-Only API setting, this
script verifies — read-only — that the API answers on localhost, that the
account is a PAPER account, and that account snapshot + positions can be
read. It then prints a PASS/FAIL checklist for
``docs/poi/evidence/POI-IBKR-READONLY.md``.

Hard safety rules (ADR-0014; PoI plan v2):

- READ-ONLY: the only session methods this harness will ever call are in
  ``ALLOWED_METHODS``. The ``ReadOnlyGuard`` wrapper refuses every other
  attribute, so no order can be placed, cancelled or modified even if the
  underlying client supports it.
- PAPER ONLY: the port must be exactly ``PAPER_API_PORT`` (the Gateway
  paper API port); the live API port is rejected. The account id must start
  with the paper prefix ``DU`` — anything else fails closed and disconnects
  immediately.
- LOCALHOST ONLY: the host must be 127.0.0.1/localhost. There is no
  override flag at all in the PoI stage.
- NO SECRETS: the harness never reads credentials from anywhere (no env
  access, no prompts) — login happens only in the Gateway UI, by the owner.
  Account ids are printed masked; nothing sensitive is written anywhere.
- NO SDK COUPLING: no broker SDK is imported here (guardrails allow the
  IBKR SDK only inside the adapter package). The concrete session module is
  supplied at run time via ``--session-module`` and loaded lazily; it must
  expose ``create_session()`` returning an object with the allowlisted
  methods. Tests inject a fake session instead.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

HARNESS_VERSION = "poi-ibkr-readonly-1.0.0"

ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
PAPER_ACCOUNT_PREFIX = "DU"

#: The ONLY session methods the harness may call. Everything else — order
#: placement, cancellation, modification, option exercise, anything — is
#: refused by ReadOnlyGuard before it can reach the broker.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {"connect", "req_current_time", "req_account_summary", "req_positions", "disconnect"}
)


class PoiFailure(Exception):
    """Fail-closed PoI failure; carries the checklist reason."""


class ReadOnlyViolation(PoiFailure):
    """A non-allowlisted (potentially mutating) method was requested."""


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Read-only account snapshot fields (values printed sanitized only)."""

    account_id: str
    net_liquidation: str
    total_cash: str
    buying_power: str


class ReadOnlySession(Protocol):
    """Duck-typed broker session; the real one is chosen at PoI run time."""

    def connect(self, host: str, port: int) -> None: ...

    def req_current_time(self) -> str: ...

    def req_account_summary(self) -> AccountSummary: ...

    def req_positions(self) -> Sequence[object]: ...

    def disconnect(self) -> None: ...


class ReadOnlyGuard:
    """Hard allowlist proxy: only ``ALLOWED_METHODS`` pass through."""

    def __init__(self, session: object) -> None:
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name: str) -> object:
        if name not in ALLOWED_METHODS:
            raise ReadOnlyViolation(
                f"method {name!r} is not in the read-only allowlist; refusing"
            )
        return getattr(object.__getattribute__(self, "_session"), name)

    def __setattr__(self, name: str, value: object) -> None:
        raise ReadOnlyViolation("the read-only session proxy is immutable")


def mask_account_id(account_id: str) -> str:
    """``DU1234567`` -> ``DU*****67``; never expose the full id."""
    if len(account_id) <= 4:
        return account_id[:2] + "**"
    return account_id[:2] + "*" * (len(account_id) - 4) + account_id[-2:]


def validate_endpoint(host: str, port: int) -> None:
    """Fail closed on anything that is not localhost + the paper API port."""
    if host not in ALLOWED_HOSTS:
        raise PoiFailure(
            f"host {host!r} refused: only 127.0.0.1/localhost is allowed in this PoI"
        )
    if port != PAPER_API_PORT:
        raise PoiFailure(
            f"port {port} refused: only the paper API port {PAPER_API_PORT} is allowed"
            " (the live API port is never allowed)"
        )


@dataclass(slots=True)
class CheckOutcome:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class PoiReport:
    checks: list[CheckOutcome] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(CheckOutcome(name, passed, detail))

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def render(self) -> str:
        lines = [f"POI-IBKR-READONLY harness {HARNESS_VERSION}"]
        for check in self.checks:
            status = "PASS" if check.passed else "FAIL"
            lines.append(f"  [{status}] {check.name}: {check.detail}")
        lines.append(f"RESULT: {'PASS' if self.passed else 'FAIL'}")
        lines.append("(read-only allowlist enforced; no order API was ever callable)")
        return "\n".join(lines)


def run_poi(
    session_factory: Callable[[], object],
    *,
    host: str = "127.0.0.1",
    port: int = PAPER_API_PORT,
    position_limit_note: str = "count only",
) -> PoiReport:
    """Execute the read-only checklist; fail closed on every error."""
    report = PoiReport()
    validate_endpoint(host, port)
    report.record("endpoint", True, f"{host}:{port} (localhost + paper API port only)")

    guarded: ReadOnlySession = ReadOnlyGuard(session_factory())  # type: ignore[assignment]
    connected = False
    try:
        guarded.connect(host, port)
        connected = True
        report.record("connect", True, "API socket connected")

        current_time = guarded.req_current_time()
        if not str(current_time).strip():
            raise PoiFailure("empty current-time response")
        report.record("api_health", True, "req_current_time answered")

        summary = guarded.req_account_summary()
        account_id = summary.account_id.strip()
        if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
            raise PoiFailure(
                f"account {mask_account_id(account_id)} is not a paper account"
                f" (expected prefix {PAPER_ACCOUNT_PREFIX}); disconnecting immediately"
            )
        report.record("paper_account", True, f"account {mask_account_id(account_id)}")
        report.record(
            "account_snapshot",
            all(
                str(value).strip()
                for value in (summary.net_liquidation, summary.total_cash, summary.buying_power)
            ),
            "net_liquidation/total_cash/buying_power present (values withheld from output)",
        )

        positions = guarded.req_positions()
        report.record(
            "positions_readonly", True, f"{len(positions)} position(s), {position_limit_note}"
        )
    except PoiFailure as failure:
        report.record("fail_closed", False, str(failure))
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        report.record(
            "fail_closed", False, f"unknown error, failing closed: {type(error).__name__}"
        )
    finally:
        if connected:
            try:
                guarded.disconnect()
                report.record("disconnect", True, "clean disconnect")
            except Exception:  # noqa: BLE001
                report.record("disconnect", False, "disconnect failed")
    return report


def _load_session_factory(module_name: str) -> Callable[[], object]:
    """Late, isolated import of the run-time session module (never at top)."""
    import importlib

    module = importlib.import_module(module_name)
    factory = getattr(module, "create_session", None)
    if factory is None or not callable(factory):
        raise PoiFailure(f"session module {module_name!r} must expose create_session()")
    return factory  # type: ignore[no-any-return]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "POI-IBKR-READONLY read-only harness. Manual run only, AFTER the owner"
            " has installed IB Gateway (official installer), logged in to PAPER in"
            " the Gateway UI and enabled Read-Only API. Never places orders."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1/localhost only")
    parser.add_argument("--port", type=int, default=PAPER_API_PORT, help="paper API port only")
    parser.add_argument(
        "--session-module",
        required=True,
        help=(
            "python module exposing create_session(); chosen at PoI run time so no"
            " broker SDK is imported by the harness itself"
        ),
    )
    args = parser.parse_args(argv)

    try:
        factory = _load_session_factory(args.session_module)
    except (PoiFailure, ImportError) as error:
        print(f"FAIL: {error}")
        return 2
    report = run_poi(factory, host=args.host, port=args.port)
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
