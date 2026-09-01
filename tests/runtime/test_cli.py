"""Unit tests for the operator CLI wiring (no database required)."""

from __future__ import annotations

import json

import pytest
import trading_execution_agent.runtime.cli as cli
from trading_application.auth import Role
from trading_contracts.enums import HaltScope


def test_parser_exposes_the_operator_commands() -> None:
    parser = cli.build_parser()
    argv_by_command = {
        "halt": ["halt", "--reason", "x"],
        "resume": ["resume", "--reason", "x"],
        "status": ["status"],
        "run": ["run"],
    }
    for argv in argv_by_command.values():
        args = parser.parse_args(argv)
        assert callable(args.func)


def test_scope_parsing_accepts_canonical_and_rejects_unknown() -> None:
    assert cli._scope_from("GLOBAL_ENTRY_HALT") is HaltScope.GLOBAL_ENTRY_HALT
    assert cli._scope_from("symbol_halt") is HaltScope.SYMBOL_HALT
    with pytest.raises(ValueError):
        cli._scope_from("NOT_A_SCOPE")


def test_operator_principal_is_owner_with_mfa() -> None:
    principal = cli._operator_principal()
    assert Role.OWNER in principal.roles
    assert principal.mfa_verified is True


def test_run_is_refused_on_this_branch() -> None:
    args = cli.build_parser().parse_args(["run"])
    assert cli.cmd_run(args) == cli.EXIT_ERROR


def test_status_fails_closed_without_a_database(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    args = cli.build_parser().parse_args(["status"])
    assert cli.cmd_status(args) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    # No database -> database_ok is False, and reconciliation reads as blocked
    # (fail closed), never as clean.
    assert payload["database_ok"] is False
    assert payload["reconciliation_entry_blocked"] is True
    assert payload["authorization_mode"] == "DENYALL"
