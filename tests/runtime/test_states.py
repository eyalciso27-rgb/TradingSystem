"""Tests for the derived runtime state and fail-closed eligibility (A6)."""

from __future__ import annotations

from datetime import UTC, datetime

from trading_execution_agent.runtime.states import (
    AuthorizationMode,
    LifecyclePhase,
    RuntimeState,
    RuntimeStatus,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _status(**overrides: object) -> RuntimeStatus:
    base: dict[str, object] = {
        "phase": LifecyclePhase.ACTIVE,
        "authorization_mode": AuthorizationMode.DENYALL,
        "ownership_proven": True,
        "ibkr_connected": True,
        "market_data_connected": True,
        "reconciliation_clean": True,
        "halt_active": False,
        "unknown_positions": 0,
        "unknown_orders": 0,
        "last_tick_at": NOW,
        "last_reconciliation_at": NOW,
    }
    base.update(overrides)
    return RuntimeStatus(**base)  # type: ignore[arg-type]


def test_eligible_healthy_status_is_ready_denyall() -> None:
    status = _status()
    assert status.entry_eligible is True
    assert status.state is RuntimeState.READY_DENYALL


def test_phase_maps_to_state() -> None:
    assert _status(phase=LifecyclePhase.INIT).state is RuntimeState.STARTING
    assert _status(phase=LifecyclePhase.RECONCILING).state is RuntimeState.RECONCILING
    assert _status(phase=LifecyclePhase.STOPPING).state is RuntimeState.STOPPING
    assert _status(phase=LifecyclePhase.FAILED).state is RuntimeState.FAILED


def test_unproven_ownership_is_never_eligible() -> None:
    status = _status(ownership_proven=False)
    assert status.entry_eligible is False
    assert status.state is RuntimeState.BLOCKED


def test_unknown_reconciliation_is_never_eligible() -> None:
    assert _status(reconciliation_clean=None).entry_eligible is False
    assert _status(reconciliation_clean=False).entry_eligible is False


def test_uncertain_connection_is_never_eligible() -> None:
    # A connection whose state is uncertain (None) or down (False) blocks new
    # entries, for both the broker and the market-data session.
    assert _status(ibkr_connected=None).entry_eligible is False
    assert _status(ibkr_connected=False).entry_eligible is False
    assert _status(market_data_connected=None).entry_eligible is False
    assert _status(market_data_connected=False).entry_eligible is False


def test_halt_blocks_eligibility() -> None:
    assert _status(halt_active=True).entry_eligible is False


def test_unknown_counts_block_eligibility() -> None:
    assert _status(unknown_positions=None).entry_eligible is False
    assert _status(unknown_positions=1).entry_eligible is False
    assert _status(unknown_orders=None).entry_eligible is False
    assert _status(unknown_orders=3).entry_eligible is False


def test_running_is_unreachable_under_denyall() -> None:
    # Even a perfectly eligible status cannot reach RUNNING while DENYALL is the
    # only authorization mode -- the Phase A guarantee, made visible in the state.
    assert _status().state is RuntimeState.READY_DENYALL


def test_status_serializes_deterministically() -> None:
    payload = _status().as_dict()
    assert payload["state"] == "READY_DENYALL"
    assert payload["authorization_mode"] == "DENYALL"
    assert payload["entry_eligible"] is True
    assert payload["reconciliation_clean"] is True
