"""Schema preparation sits between the database and the broker, and gates it.

Ordering is the whole point. The wrapper had grown a careful preflight --
reachability, then authentication -- and a fresh cluster passes both while
holding no application tables. So the gate had to be added at the same place the
others live: after the database is proven, before a single IBKR socket.

These tests need no database and no Docker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "run_execution_probe_paper.sh"
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "prepare_paper_database.py"


def _code_lines(text: str) -> str:
    """Executable lines only. A comment naming a thing is not that thing."""
    return chr(10).join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _python_code(source: str) -> str:
    """Python with comments AND docstrings removed.

    Prose explaining why something is forbidden is not an occurrence of it.
    Stripping only ``#`` comments was not enough here: these modules document
    their own prohibitions in docstrings, so the checks below matched the
    explanation rather than any code.
    """
    import ast

    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            first = body[0]
            assert first.end_lineno is not None
            docstrings.update(range(first.lineno, first.end_lineno + 1))
    kept = [
        line
        for number, line in enumerate(source.splitlines(), start=1)
        if number not in docstrings
    ]
    return _code_lines(chr(10).join(kept))


def _wrapper_code() -> str:
    return _code_lines(WRAPPER.read_text(encoding="utf-8"))


# The DB/env lifecycle (auth, DATABASE_URL, schema prep) now lives in the ONE
# shared bootstrap library both owner wrappers source, so those steps are
# asserted there; the wrapper only orders the bootstrap before the probe.
BOOTSTRAP = REPO_ROOT / "scripts" / "lib" / "paper_probe_bootstrap.sh"


def _bootstrap_code() -> str:
    return _code_lines(BOOTSTRAP.read_text(encoding="utf-8"))


def test_schema_preparation_runs_after_the_database_is_proven() -> None:
    """Migrating before authenticating would report the wrong failure."""
    lib = _bootstrap_code()

    auth_at = lib.index("check_paper_probe_database.py")
    prepare_at = lib.index("prepare_paper_database.py")
    assert auth_at < prepare_at
    # And the wrapper runs the probe only after the bootstrap returns.
    wrapper = _wrapper_code()
    assert wrapper.index("paper_probe_open_db") < wrapper.index(
        "python -m trading_execution_agent probe"
    )


def test_schema_preparation_uses_the_url_the_probe_will_use() -> None:
    """The same DATABASE_URL, already built and exported, not a second one."""
    lib = _bootstrap_code()

    url_at = lib.index("build_paper_database_url.py")
    export_at = lib.index("export DATABASE_URL")
    prepare_at = lib.index("prepare_paper_database.py")

    assert url_at < export_at < prepare_at
    # And the preparation step does not construct its own.
    prepare_line = next(
        line for line in lib.splitlines() if "prepare_paper_database.py" in line
    )
    assert "PAPER_POSTGRES" not in prepare_line
    assert "postgresql" not in prepare_line


def test_only_upgrade_head_is_ever_offered() -> None:
    """No downgrade, no reset, no parallel schema mechanism."""
    from trading_persistence import schema_preparation

    source = Path(schema_preparation.__file__).read_text(encoding="utf-8")
    executable = _python_code(source)

    assert "command.upgrade" in executable
    for forbidden in (
        "command.downgrade",
        "command.stamp",
        "create_all",
        "drop_all",
        "DROP TABLE",
        "DROP DATABASE",
        "TRUNCATE",
    ):
        assert forbidden not in executable, f"{forbidden} must not be reachable"


def test_the_wrapper_never_resets_the_database_on_a_schema_failure() -> None:
    """A migration failure is reported, never "fixed" by destroying data."""
    wrapper_text = WRAPPER.read_text(encoding="utf-8")
    lib_text = BOOTSTRAP.read_text(encoding="utf-8")
    executable = _code_lines(wrapper_text) + "\n" + _code_lines(lib_text)

    for destructive in ("down -v", "--volumes", "volume rm", "volume prune"):
        assert destructive not in executable
    # The reassurance lives beside the schema step, now in the shared library.
    assert "Nothing was reset and no data was removed" in lib_text


# == the gate actually stops the probe =======================================


def _run_bash(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "fragment.sh"
    script.write_text(body, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - a local bash fragment, no input
        ["bash", str(script)], capture_output=True, text=True, check=False
    )


def _gate_fragment() -> str:
    """The wrapper's real bootstrap gate, sliced out of the file.

    The DB/schema preparation is one shared function now, so the wrapper's gate
    is ``paper_probe_open_db ... || fail ...`` before the probe. Sliced rather
    than retyped, and asserted non-empty, so the control flow under test is the
    shipped one; the fake ``paper_probe_open_db`` stands in for the whole
    bootstrap (whose OWN failure-closed behaviour is proven in the library
    tests).
    """
    text = WRAPPER.read_text(encoding="utf-8")
    start = text.index("paper_probe_open_db \"$REPO_ROOT\"")
    end = text.index("log \"-- running the probe")
    fragment = text[start:end]
    assert "|| fail" in fragment, "the gate must fail, not warn"
    return fragment


def test_a_failing_bootstrap_invokes_zero_probes(tmp_path: Path) -> None:
    """The requirement: no schema prep, no probe, no IBKR session."""
    marker = tmp_path / "probe-was-invoked"
    body = (
        "set -Eeuo pipefail\n"
        'fail() { echo "ERROR: $*" >&2; exit 2; }\n'
        "log() { :; }\n"
        'REPO_ROOT="."\n'
        # The bootstrap fails closed (its own reasons are library-tested).
        "paper_probe_open_db() { return 1; }\n"
        + _gate_fragment()
        + f'touch "{marker.as_posix()}"\n'
    )

    result = _run_bash(tmp_path, body)

    assert result.returncode == 2
    assert not marker.exists(), "the execution probe was invoked anyway"
    assert "zero IBKR connections were opened" in result.stderr


def test_a_passing_bootstrap_allows_the_probe(tmp_path: Path) -> None:
    """The control: identical control flow, the bootstrap succeeds."""
    marker = tmp_path / "probe-was-invoked"
    body = (
        "set -Eeuo pipefail\n"
        'fail() { echo "ERROR: $*" >&2; exit 2; }\n'
        "log() { :; }\n"
        'REPO_ROOT="."\n'
        "paper_probe_open_db() { return 0; }\n"
        + _gate_fragment()
        + f'touch "{marker.as_posix()}"\n'
    )

    result = _run_bash(tmp_path, body)

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "the probe should run once the bootstrap succeeds"


# == the CLI is secret-safe ==================================================


def test_the_preparation_cli_reports_a_code_and_a_type_only() -> None:
    """No DSN, no password, no traceback in the normal owner path."""
    source = PREPARE_SCRIPT.read_text(encoding="utf-8")
    executable = _python_code(source)

    assert "type(error).__name__" in executable
    assert "{error}" not in executable, "raw exception text must never be written"
    assert "traceback" not in executable.lower()


def test_the_preparation_cli_fails_closed_without_a_url(tmp_path: Path) -> None:
    """No DATABASE_URL means a stable code, not a stack trace."""
    import os
    import sys

    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    result = subprocess.run(  # noqa: S603 - a repository script
        [sys.executable, str(PREPARE_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 2
    assert "PAPER_DB_SCHEMA_NO_URL" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_broken_url_reports_a_type_without_the_dsn(tmp_path: Path) -> None:
    """Driver text embeds the DSN, so only the exception type may surface."""
    import os
    import sys

    env = dict(os.environ)
    secret = "hunter2-not-a-real-password"
    env["DATABASE_URL"] = (
        f"postgresql+psycopg://trading_paper:{secret}@127.0.0.1:1/trading_paper"
    )
    result = subprocess.run(  # noqa: S603 - a repository script
        [sys.executable, str(PREPARE_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "PAPER_DB_SCHEMA_FAILED" in result.stderr
    assert secret not in combined
    assert "postgresql+psycopg://" not in combined
    assert "Traceback" not in combined


@pytest.mark.parametrize(
    "attribute", ["previous_revision", "revision", "upgraded", "first_run"]
)
def test_the_result_carries_revisions_not_credentials(attribute: str) -> None:
    from trading_persistence.schema_preparation import PreparationResult

    result = PreparationResult(previous_revision=None, revision="abc123", upgraded=True)

    assert hasattr(result, attribute)
    assert "url" not in repr(result).lower()
    assert "password" not in repr(result).lower()
