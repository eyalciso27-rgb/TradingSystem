"""Owner-safety of the protected-bracket proof wrapper (ADR-0019).

The wrapper is the ONLY documented owner-host way to run the protected-bracket
proof, so it must be owner-safe from a fresh SSH shell that holds only
SECRETS_ROOT, must refuse Live and a wrong arming phrase before anything opens,
must never run system Python, must always clean up the temporary publish, and
must never delete the durable Paper database.

Static assertions read the wrapper and the shared bootstrap library (the DB/env
lifecycle lives there, shared with the read-only probe, so it cannot drift).
Two behavioural tests execute the real wrapper's early gates -- which fail before
any Docker/uv/socket -- to prove Live and a wrong token are refused.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "run_protected_bracket_proof_paper.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "lib" / "paper_probe_bootstrap.sh"
ARM_PHRASE = "yes-place-a-real-paper-bracket"


def _code_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _wrapper_code() -> str:
    return _code_lines(WRAPPER.read_text(encoding="utf-8"))


def _both_code() -> str:
    return _wrapper_code() + "\n" + _code_lines(BOOTSTRAP.read_text(encoding="utf-8"))


def test_the_proof_wrapper_exists_and_is_executable() -> None:
    assert WRAPPER.is_file()
    raw = WRAPPER.read_text(encoding="utf-8")
    assert raw.startswith("#!/usr/bin/env bash")
    assert "set -Eeuo pipefail" in _wrapper_code()


def test_it_never_invokes_system_python() -> None:
    """Every Python invocation goes through `uv run --no-sync python`."""
    code = _both_code()
    for line in code.splitlines():
        # The only pythons allowed are `uv run --no-sync python ...`.
        if "python" in line and "uv run --no-sync python" not in line:
            # A comment/echo mentioning python is fine only if it is not a call.
            assert not line.lstrip().startswith(("python", "python3")), line
            assert " python " not in f" {line} ".replace(
                "uv run --no-sync python", "uv-python"
            ), line
    assert "uv run --no-sync python" in code
    # Never a bare interpreter.
    assert "\npython " not in "\n" + code
    assert "python3 " not in code


def test_a_fresh_shell_needs_only_secrets_root() -> None:
    """No DATABASE_URL / generic POSTGRES_* demanded from the caller's env."""
    code = _both_code()
    # The credentials are loaded from the store, never required via `:?`.
    for forbidden in (
        "DATABASE_URL:?",
        "PAPER_POSTGRES_USER:?",
        "PAPER_POSTGRES_PASSWORD:?",
        "POSTGRES_PASSWORD:?",
    ):
        assert forbidden not in code, forbidden
    assert "SECRETS_ROOT" in code
    assert "load_paper_environment" in code
    assert "paper_probe_bootstrap.sh" in _wrapper_code()


def test_the_live_port_cannot_be_selected() -> None:
    # Paper-only by construction: the gate REQUIRES 4002, so every other port
    # (the Live 4001 included) is refused. Proven behaviourally below too.
    code = _wrapper_code()
    assert '[ "$PORT" = "4002" ]' in code


def test_a_dry_run_passes_no_execute_and_an_armed_run_passes_the_exact_phrase() -> None:
    code = _wrapper_code()
    # The token is a whole phrase, and --execute is added ONLY when the arg
    # equals it exactly. A dry run (empty arg) yields an empty EXECUTE_ARGS.
    assert 'ARM_PHRASE="yes-place-a-real-paper-bracket"' in code
    assert 'EXECUTE_ARGS=(--execute "$ARM_PHRASE")' in code
    assert '[ "$ARM_ARG" = "$ARM_PHRASE" ]' in code
    # No quantity override: the Python proof hard-codes qty 1.
    assert "--quantity" not in code


def test_cleanup_is_trapped_on_every_exit() -> None:
    assert "trap 'paper_probe_close_db' EXIT" in _wrapper_code()


def test_the_dsn_is_never_printed_and_the_shell_is_never_traced() -> None:
    code = _both_code()
    assert "set -x" not in code
    for leak in ("echo $DATABASE_URL", 'echo "$DATABASE_URL"', "log $DATABASE_URL"):
        assert leak not in code, leak


def test_the_durable_database_is_never_destroyed() -> None:
    code = _both_code()
    for destructive in ("down -v", " down ", "volume rm", "volume prune", "rm -rf", "--reset"):
        assert destructive not in code, destructive


def test_production_authorization_and_paper_auto_are_untouched() -> None:
    # The wrapper only ever passes --execute (scoped proof authority); it never
    # enables Paper Auto or Live, and it names neither as a mode.
    code = _wrapper_code()
    assert "PAPER_AUTO" not in code
    assert "--mode live" not in code  # never a Live path
    # The only submission lever is --execute (the scoped proof authority).
    assert "DenyAll" not in code  # it does not touch production authorization


# -- behavioural: the early gates refuse before anything opens ---------------


def _run(
    args: list[str], env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(  # noqa: S603 - local wrapper, fixed args, no shell
        ["bash", str(WRAPPER), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
    )


def test_a_live_port_is_refused_before_any_bootstrap() -> None:
    result = _run(["AAPL"], {"TRADING_IBKR_PORT": "4001"})
    assert result.returncode == 2, result.stderr
    assert "Paper port 4002 only" in result.stderr
    # Nothing was prepared: no publish, no python, no proof.
    assert "opening a temporary loopback" not in result.stderr


def test_a_wrong_arming_token_is_refused_before_any_bootstrap() -> None:
    result = _run(["AAPL", "not-the-phrase"])
    assert result.returncode == 2, result.stderr
    assert "arming phrase" in result.stderr
    assert "opening a temporary loopback" not in result.stderr
