"""Tests for the durable halt source: conversion + fail-closed (A4)."""

from __future__ import annotations

import pytest
from trading_application.emergency import EntryContext
from trading_contracts.enums import Environment, HaltScope
from trading_execution_agent.runtime import halts as halts_module
from trading_execution_agent.runtime.halts import (
    DurableHaltSource,
    HaltStoreUnavailableError,
)
from trading_persistence.emergency import ActiveHalt

ENGINE = object()  # opaque: read_active_halts is what touches the database


def test_active_halts_converts_to_halt_records(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read(_engine: object, _env: Environment) -> list[ActiveHalt]:
        return [
            ActiveHalt(
                scope=HaltScope.GLOBAL_ENTRY_HALT,
                environment=Environment.PAPER,
                target_ref=None,
                reason_code="OPERATOR_KILL_SWITCH",
                keep_protection=True,
            )
        ]

    monkeypatch.setattr(halts_module, "read_active_halts", fake_read)
    source = DurableHaltSource(ENGINE, Environment.PAPER)
    halts = source.active_halts()
    assert len(halts) == 1
    assert halts[0].scope is HaltScope.GLOBAL_ENTRY_HALT
    assert source.entry_halt_active(EntryContext(environment=Environment.PAPER)) is True


def test_no_halts_means_entry_not_halted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        halts_module, "read_active_halts", lambda _engine, _env: []
    )
    source = DurableHaltSource(ENGINE, Environment.PAPER)
    assert source.active_halts() == ()
    assert source.entry_halt_active(EntryContext(environment=Environment.PAPER)) is False


def test_store_failure_raises_on_active_halts(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_engine: object, _env: Environment) -> list[ActiveHalt]:
        raise RuntimeError("database down")

    monkeypatch.setattr(halts_module, "read_active_halts", boom)
    source = DurableHaltSource(ENGINE, Environment.PAPER)
    with pytest.raises(HaltStoreUnavailableError):
        source.active_halts()


def test_store_failure_fails_closed_on_entry_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A runtime that cannot prove there is NO halt must treat the entry as
    # halted. entry_halt_active swallows the store error into a blocked verdict.
    def boom(_engine: object, _env: Environment) -> list[ActiveHalt]:
        raise RuntimeError("database down")

    monkeypatch.setattr(halts_module, "read_active_halts", boom)
    source = DurableHaltSource(ENGINE, Environment.PAPER)
    assert source.entry_halt_active(EntryContext(environment=Environment.PAPER)) is True
