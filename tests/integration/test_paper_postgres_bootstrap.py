"""What the SECOND real owner-host probe run exposed.

The probe stopped before opening IBKR, which is the correct behaviour, and in
stopping it revealed a deployment gap that no test could have caught because it
lives entirely in the seam between this repository and a real host:

* ``$SECRETS_ROOT/PAPER/`` existed and held exactly one thing --
  ``ibkr/paper-account-id``;
* ``PAPER_POSTGRES_USER`` and ``PAPER_POSTGRES_PASSWORD`` were ``NOT_SET``;
* there were zero PostgreSQL containers, zero ``trading-*`` networks, zero
  PostgreSQL volumes and zero compose projects on the host.

So a correct, complete, freshly provisioned owner host could not run the probe,
and the documentation's answer -- "supply them from the PAPER secret store" --
described secret material that no command in the repository ever created.

These tests cover the five secret states, and the two properties that make the
fix safe rather than merely convenient: the bootstrap never rotates, and no path
here can destroy an existing database.

Nothing here connects to anything, starts a container, or touches a volume.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from trading_config.paper_postgres import (
    ALL_REFS,
    DATABASE_REF,
    DEFAULT_DATABASE,
    DEFAULT_USERNAME,
    PASSWORD_REF,
    USERNAME_REF,
    PaperPostgresError,
    bootstrap,
    load,
    present_refs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "run_execution_probe_paper.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap_paper_postgres.py"
LOADER_SCRIPT = REPO_ROOT / "scripts" / "load_paper_postgres_env.py"

#: The account secret the real owner host already had. Present in every state,
#: because the point is that the PostgreSQL set joins it in the SAME store.
ACCOUNT_SECRET = "ibkr/paper-account-id"


def _code_lines(text: str) -> str:
    """Executable lines only. A comment explaining what is forbidden is not
    an occurrence of the forbidden thing."""
    return chr(10).join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _without_string_literals(text: str) -> str:
    """Shell code with the contents of quoted strings removed.

    Naming a command inside a message the owner reads is not running it, and
    ``_code_lines`` alone cannot tell the difference -- a multi-line failure
    message looks exactly like a command on its own line.
    """
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for character in text:
        if escaped:
            escaped = False
            continue
        if character == chr(92):
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        out.append(character)
    return "".join(out)


def _write(root: Path, ref: str, value: str) -> Path:
    path = root / "PAPER" / Path(*ref.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return path


def _owner_host_store(tmp_path: Path) -> Path:
    """State A, exactly as the owner host was found: the account id and nothing
    else."""
    root = tmp_path / "secrets"
    _write(root, ACCOUNT_SECRET, "DU1234567")
    return root


def _run(script: Path, root: Path | None, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run one of the two CLIs in a scrubbed environment.

    Scrubbed matters: the whole point of state E is a fresh SSH shell with no
    ``PAPER_POSTGRES_*`` in it, and a test that inherited the developer's
    environment would prove nothing.
    """
    env = dict(os.environ)
    for name in (
        "PAPER_POSTGRES_USER",
        "PAPER_POSTGRES_PASSWORD",
        "PAPER_POSTGRES_DB",
        "SECRETS_ROOT",
    ):
        env.pop(name, None)
    if root is not None:
        env["SECRETS_ROOT"] = str(root)
    env.update(overrides)
    return subprocess.run(  # noqa: S603 - a repository script, no external input
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
    )


# == state A: nothing in the store ==========================================


def test_state_a_bootstrap_creates_the_complete_set(tmp_path: Path) -> None:
    """The owner-host state. One command makes the store usable."""
    root = _owner_host_store(tmp_path)
    assert present_refs(root) == ()

    result = bootstrap(root)

    assert result.created == ALL_REFS
    assert result.already_present == ()
    assert set(present_refs(root)) == set(ALL_REFS)
    credentials = load(root)
    assert credentials.username == DEFAULT_USERNAME
    assert credentials.database == DEFAULT_DATABASE
    assert credentials.password.reveal()


def test_state_a_leaves_the_existing_account_secret_untouched(tmp_path: Path) -> None:
    """The same store, not a second one -- and the account id is not disturbed."""
    root = _owner_host_store(tmp_path)
    account = root / "PAPER" / "ibkr" / "paper-account-id"
    before = account.read_bytes()

    bootstrap(root)

    assert account.read_bytes() == before
    assert (root / "PAPER" / "postgres").is_dir(), "the set lives under the same root"


def test_the_generated_password_is_strong_and_unique(tmp_path: Path) -> None:
    """Not a default, not derived from anything, not the same twice."""
    first = _owner_host_store(tmp_path / "a")
    second = _owner_host_store(tmp_path / "b")
    bootstrap(first)
    bootstrap(second)

    one = load(first).password.reveal()
    other = load(second).password.reveal()

    assert one != other
    assert len(one) >= 40, "32 URL-safe bytes render as ~43 characters"
    assert one not in {DEFAULT_USERNAME, DEFAULT_DATABASE, "postgres", "trading"}


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    """Atomic creation: the rename target is the only thing that remains."""
    root = _owner_host_store(tmp_path)
    bootstrap(root)

    leftovers = [
        path.name
        for path in (root / "PAPER" / "postgres").iterdir()
        if path.name.startswith(".") or path.name.endswith(".tmp")
    ]

    assert leftovers == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_the_secret_files_are_owner_readable_only(tmp_path: Path) -> None:
    root = _owner_host_store(tmp_path)
    bootstrap(root)

    for ref in ALL_REFS:
        path = root / "PAPER" / Path(*ref.split("/"))
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO), f"{ref} is mode {mode:o}"
    directory = stat.S_IMODE((root / "PAPER" / "postgres").stat().st_mode)
    assert not directory & (stat.S_IRWXG | stat.S_IRWXO)


# == state B: a complete set already exists =================================


def test_state_b_is_idempotent_and_never_rotates(tmp_path: Path) -> None:
    """The single most important property here.

    ``POSTGRES_PASSWORD`` is applied by the PostgreSQL image only to an EMPTY
    data directory. A bootstrap that regenerated the password on a rerun would
    leave the secret store and an initialised volume permanently disagreeing --
    unfixable without the old password, and "fixable" only by destroying the
    data. So a rerun must be a genuine no-op, byte for byte.
    """
    root = _owner_host_store(tmp_path)
    bootstrap(root)
    before = {ref: (root / "PAPER" / Path(*ref.split("/"))).read_bytes() for ref in ALL_REFS}

    result = bootstrap(root)

    assert result.created == ()
    assert result.already_present == ALL_REFS
    assert result.rotated is False
    after = {ref: (root / "PAPER" / Path(*ref.split("/"))).read_bytes() for ref in ALL_REFS}
    assert after == before


def test_state_b_preserves_credentials_someone_else_chose(tmp_path: Path) -> None:
    """An operator-supplied set is kept exactly, not normalised to our policy."""
    root = _owner_host_store(tmp_path)
    _write(root, USERNAME_REF, "existing_role")
    _write(root, PASSWORD_REF, "an-existing-password")
    _write(root, DATABASE_REF, "existing_db")

    result = bootstrap(root)

    assert result.created == ()
    credentials = load(root)
    assert credentials.username == "existing_role"
    assert credentials.database == "existing_db"
    assert credentials.password.reveal() == "an-existing-password"


# == state C: a partial set =================================================


@pytest.mark.parametrize(
    "present",
    [
        pytest.param((USERNAME_REF,), id="username-only"),
        pytest.param((PASSWORD_REF,), id="password-only"),
        pytest.param((USERNAME_REF, DATABASE_REF), id="no-password"),
        pytest.param((USERNAME_REF, PASSWORD_REF), id="no-database"),
    ],
)
def test_state_c_partial_is_refused_and_nothing_is_written(
    tmp_path: Path, present: tuple[str, ...]
) -> None:
    """Completing a partial set would pair a new value with an old one, and the
    database would reject the pair -- after the probe had been told it was fine."""
    root = _owner_host_store(tmp_path)
    for ref in present:
        _write(root, ref, "chosen-earlier")
    before = sorted(path.name for path in (root / "PAPER" / "postgres").iterdir())

    with pytest.raises(PaperPostgresError, match="PARTIAL"):
        bootstrap(root)

    after = sorted(path.name for path in (root / "PAPER" / "postgres").iterdir())
    assert after == before, "a refused bootstrap must write nothing"


def test_state_c_partial_also_fails_the_ordinary_load(tmp_path: Path) -> None:
    root = _owner_host_store(tmp_path)
    _write(root, USERNAME_REF, "chosen-earlier")

    with pytest.raises(PaperPostgresError):
        load(root)


def test_an_empty_secret_file_is_not_mistaken_for_a_value(tmp_path: Path) -> None:
    """A truncated write is a partial set, not a blank password."""
    root = _owner_host_store(tmp_path)
    bootstrap(root)
    (root / "PAPER" / Path(*PASSWORD_REF.split("/"))).write_text("\n", encoding="utf-8")

    assert PASSWORD_REF not in present_refs(root)
    with pytest.raises(PaperPostgresError):
        load(root)


# == state D: nothing in the store, and no store either =====================


def test_state_d_a_missing_store_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PaperPostgresError, match="does not exist"):
        bootstrap(tmp_path / "not-here")


def test_a_load_without_the_set_names_the_bootstrap_not_a_value(
    tmp_path: Path,
) -> None:
    """The failure has to tell the owner what to run. It must not tell them to
    invent a password."""
    root = _owner_host_store(tmp_path)

    with pytest.raises(PaperPostgresError) as raised:
        load(root)

    message = str(raised.value)
    assert "bootstrap_paper_postgres.sh" in message
    assert PASSWORD_REF in message


# == state E: a fresh SSH shell with no PAPER_POSTGRES_* ====================


def test_state_e_a_fresh_shell_resolves_the_credentials_from_the_store(
    tmp_path: Path,
) -> None:
    """THE regression this round closes.

    No ``PAPER_POSTGRES_*`` anywhere in the environment -- exactly a new SSH
    session -- and the credentials still resolve, from the canonical store.
    """
    root = _owner_host_store(tmp_path)
    bootstrap(root)

    result = _run(LOADER_SCRIPT, root)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == DEFAULT_USERNAME
    assert lines[1] == DEFAULT_DATABASE
    assert lines[2] == load(root).password.reveal()
    assert len(lines) == 3


def test_state_e_without_the_set_fails_closed_and_points_at_the_bootstrap(
    tmp_path: Path,
) -> None:
    root = _owner_host_store(tmp_path)

    result = _run(LOADER_SCRIPT, root)

    assert result.returncode == 2
    assert "PAPER_POSTGRES_LOAD_FAILED" in result.stderr
    assert "bootstrap_paper_postgres.sh" in result.stderr


def test_an_environment_override_wins_and_is_all_or_nothing(tmp_path: Path) -> None:
    """The documented exceptional path, and its one guard rail.

    Taking a username from the environment and a password from the store would
    pair two values nobody chose together.
    """
    root = _owner_host_store(tmp_path)
    bootstrap(root)

    both = _run(
        LOADER_SCRIPT,
        root,
        PAPER_POSTGRES_USER="override_role",
        PAPER_POSTGRES_PASSWORD="override-password",
    )
    assert both.returncode == 0, both.stderr
    assert both.stdout.splitlines()[0] == "override_role"
    assert both.stdout.splitlines()[2] == "override-password"

    half = _run(LOADER_SCRIPT, root, PAPER_POSTGRES_USER="override_role")
    assert half.returncode == 2
    assert "together or not at all" in half.stderr


def test_a_multi_line_credential_is_refused_rather_than_desynchronising(
    tmp_path: Path,
) -> None:
    """The wrapper reads three lines. A value containing a line break would push
    a fragment of the password into the position the username is read from."""
    root = _owner_host_store(tmp_path)

    result = _run(
        LOADER_SCRIPT,
        root,
        PAPER_POSTGRES_USER="role",
        PAPER_POSTGRES_PASSWORD="first" + chr(10) + "second",
    )

    assert result.returncode == 2
    assert "line break" in result.stderr
    assert "first" not in result.stdout


# == the bootstrap CLI never discloses a password ===========================


def test_the_bootstrap_cli_never_prints_the_password(tmp_path: Path) -> None:
    root = _owner_host_store(tmp_path)

    created = _run(BOOTSTRAP_SCRIPT, root)
    rerun = _run(BOOTSTRAP_SCRIPT, root)

    assert created.returncode == 0, created.stderr
    assert rerun.returncode == 0, rerun.stderr
    password = load(root).password.reveal()
    for result in (created, rerun):
        combined = result.stdout + result.stderr
        assert password not in combined
        assert "postgresql" not in combined, "no DSN may appear either"
    assert "BOOTSTRAP_OK" in created.stdout
    assert "BOOTSTRAP_UNCHANGED" in rerun.stdout


@pytest.mark.parametrize(
    "script", [BOOTSTRAP_SCRIPT, LOADER_SCRIPT], ids=["bootstrap", "loader"]
)
def test_neither_cli_ever_emits_a_traceback(tmp_path: Path, script: Path) -> None:
    """A store root that exists but holds an unreadable tree still has to fail
    as a code and a message, never as a stack trace.

    The whole point of the previous two rounds was that owner-visible output is
    written by us, not by whatever library happened to raise.
    """
    root = _owner_host_store(tmp_path)
    # A directory where a secret file must be: the store rejects it, and that
    # rejection must not arrive as an unhandled exception.
    (root / "PAPER" / "postgres" / "paper-password").mkdir(parents=True)

    result = _run(script, root)

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "FAILED" in result.stderr


def test_the_bootstrap_cli_requires_the_canonical_root(tmp_path: Path) -> None:
    del tmp_path
    result = _run(BOOTSTRAP_SCRIPT, None)

    assert result.returncode == 2
    assert "SECRETS_ROOT" in result.stderr


def test_the_partial_refusal_reaches_the_owner_without_a_value(
    tmp_path: Path,
) -> None:
    root = _owner_host_store(tmp_path)
    _write(root, PASSWORD_REF, "a-password-chosen-earlier")

    result = _run(BOOTSTRAP_SCRIPT, root)

    assert result.returncode == 2
    assert "PARTIAL" in result.stderr
    assert "a-password-chosen-earlier" not in result.stderr


# == nothing may destroy an existing database ===============================


@pytest.mark.parametrize(
    "destructive",
    [
        pytest.param("down -v", id="compose-down-volumes"),
        pytest.param("--volumes", id="volumes-flag"),
        pytest.param("volume rm", id="volume-rm"),
        pytest.param("volume prune", id="volume-prune"),
        pytest.param("system prune", id="system-prune"),
    ],
)
def test_no_owner_facing_script_can_remove_the_database(destructive: str) -> None:
    """The paper volume holds the durable execution history.

    An authentication mismatch is a configuration problem. Repairing it by
    deleting the data would destroy the audit record to fix a password.
    """
    for script in (WRAPPER, BOOTSTRAP_SCRIPT, LOADER_SCRIPT):
        code = _code_lines(script.read_text(encoding="utf-8"))
        assert destructive not in code, f"{script.name} contains {destructive!r}"


def test_the_wrapper_cleanup_removes_the_publish_and_nothing_else() -> None:
    """Teardown re-applies the paper stack without the probe override. It brings
    the container UP without the published port; it never brings it down."""
    code = _code_lines(WRAPPER.read_text(encoding="utf-8"))
    cleanup = code.split("cleanup() {", 1)[1].split("trap cleanup EXIT", 1)[0]

    assert "up -d --no-deps postgres" in cleanup
    assert "down" not in cleanup
    assert "rm" not in cleanup


def test_an_authentication_failure_asks_for_reconciliation_not_deletion() -> None:
    """The message the owner will actually read when an existing volume and the
    secret set disagree."""
    text = WRAPPER.read_text(encoding="utf-8")

    assert "Reconcile" in text
    assert "Do NOT delete the volume" in text
    assert "zero IBKR connections were opened" in text


# == the wrapper loads the secrets itself ===================================


def test_the_wrapper_loads_the_credentials_before_it_starts_anything() -> None:
    """Order matters: a missing credential must fail before a container exists."""
    code = _code_lines(WRAPPER.read_text(encoding="utf-8"))

    load_at = code.index("load_paper_postgres_env.py")
    compose_at = code.index('docker compose "${COMPOSE_FILES[@]}" \\')
    probe_at = code.index("python -m trading_execution_agent probe")

    assert load_at < compose_at < probe_at


def test_the_wrapper_exports_into_its_own_process_only() -> None:
    """The owner exports nothing. The wrapper exports for its own children."""
    code = _code_lines(WRAPPER.read_text(encoding="utf-8"))

    assert "export PAPER_POSTGRES_USER=" in code
    assert "export PAPER_POSTGRES_PASSWORD=" in code
    assert "export PAPER_POSTGRES_DB=" in code
    # And it never echoes what it loaded.
    assert "set -x" not in code
    assert "$PAPER_POSTGRES_PASSWORD" not in code.replace(
        'export PAPER_POSTGRES_PASSWORD="${PAPER_CREDENTIAL_LINES[2]}"', ""
    )


def test_the_probe_wrapper_never_bootstraps_credentials() -> None:
    """Bootstrapping is explicit and owner-invoked. A probe run that could
    generate credentials could silently orphan an initialised volume."""
    text = WRAPPER.read_text(encoding="utf-8")

    # It may NAME the command -- it has to, in the failure message the owner
    # reads -- but nothing outside a string literal may reference it, so there
    # is no construct that could execute it.
    executable = _without_string_literals(_code_lines(text))

    assert "bootstrap_paper_postgres" not in executable
    assert "bash scripts/bootstrap_paper_postgres.sh" in text


def test_the_wrapper_still_publishes_only_on_loopback() -> None:
    """Preserved from the previous round: never 0.0.0.0, never left open."""
    code = _code_lines(WRAPPER.read_text(encoding="utf-8"))
    override = _code_lines(
        (REPO_ROOT / "deploy" / "compose.paper-probe.yaml").read_text(encoding="utf-8")
    )

    assert "0.0.0.0" not in code
    assert "0.0.0.0" not in override
    assert "127.0.0.1:" in override
    assert "trap cleanup EXIT" in code


def test_the_first_run_path_creates_the_database_without_asking(tmp_path: Path) -> None:
    """No container, no network, no volume: `compose up` makes all three, and
    PostgreSQL initialises the role from the credentials just loaded."""
    del tmp_path
    code = _code_lines(WRAPPER.read_text(encoding="utf-8"))

    assert "ps -aq postgres" in code, "first run is detected, not assumed"
    assert "first run: creating the paper database" in code
    # It is created by the ordinary paper stack definition, not by a bespoke
    # container, so isolation and persistence come from the reviewed compose
    # files rather than from this script.
    assert "-f deploy/compose.paper.yaml" in code


# == the wrapper's own bash, executed ========================================


def _credential_block() -> str:
    """The wrapper's real credential-loading lines, sliced out of the file.

    Sliced rather than retyped: a test that reimplements the code it is checking
    proves only that the reimplementation works. The one substitution replaces
    the ``uv`` invocation with a shell function, which bash resolves ahead of
    any PATH lookup, so the surrounding mapfile/count/export logic runs byte for
    byte as it does on the owner host.
    """
    text = WRAPPER.read_text(encoding="utf-8")
    start = text.index('PAPER_CREDENTIALS="$(')
    end = text.index('log "   database')
    return text[start:end]


def _run_bash(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "fragment.sh"
    script.write_text(body, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - a local bash fragment, no input
        ["bash", str(script)], capture_output=True, text=True, check=False
    )


def test_the_wrapper_block_exports_exactly_what_the_loader_emitted(
    tmp_path: Path,
) -> None:
    """Including a password full of characters that break naive parsing."""
    awkward = "p@ss:w/rd#1 %&$`"
    out = tmp_path / "exported.txt"
    # The stub emits the awkward password from a single-quoted variable, so the
    # stub itself does not have to quote it inline.
    body = (
        "set -Eeuo pipefail\n"
        'fail() { echo "ERROR: $*" >&2; exit 2; }\n'
        "log() { :; }\n"
        f"STUB_PASSWORD={chr(39)}{awkward}{chr(39)}\n"
        "uv() { printf '%s\\n' 'role_x' 'db_x' \"$STUB_PASSWORD\"; }\n"
        + _credential_block()
        + f'printf "%s\\n" "$PAPER_POSTGRES_USER" "$PAPER_POSTGRES_DB"'
        f' "$PAPER_POSTGRES_PASSWORD" > "{out.as_posix()}"\n'
    )

    result = _run_bash(tmp_path, body)

    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["role_x", "db_x", awkward]


def test_the_wrapper_block_fails_closed_when_the_loader_refuses(
    tmp_path: Path,
) -> None:
    """No credentials means nothing is started and the owner is told what to run."""
    marker = tmp_path / "went-further"
    body = (
        "set -Eeuo pipefail\n"
        'fail() { echo "ERROR: $*" >&2; exit 2; }\n'
        "log() { :; }\n"
        'uv() { echo "PAPER_POSTGRES_LOAD_FAILED: missing" >&2; return 2; }\n'
        + _credential_block()
        + f'touch "{marker.as_posix()}"\n'
    )

    result = _run_bash(tmp_path, body)

    assert result.returncode == 2
    assert not marker.exists(), "execution continued past a missing credential"
    assert "bootstrap_paper_postgres.sh" in result.stderr


def test_the_wrapper_block_rejects_a_short_read(tmp_path: Path) -> None:
    """Two lines where three are expected must not silently export a username
    into the password's place."""
    marker = tmp_path / "went-further"
    body = (
        "set -Eeuo pipefail\n"
        'fail() { echo "ERROR: $*" >&2; exit 2; }\n'
        "log() { :; }\n"
        "uv() { printf '%s\\n' 'role_x' 'db_x'; }\n"
        + _credential_block()
        + f'touch "{marker.as_posix()}"\n'
    )

    result = _run_bash(tmp_path, body)

    assert result.returncode == 2
    assert not marker.exists()
    assert "incomplete" in result.stderr


# == the documentation states the real flow =================================


def test_the_deployment_documentation_states_the_first_run_flow() -> None:
    readme = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

    assert "bash scripts/bootstrap_paper_postgres.sh" in readme
    assert "bash scripts/run_execution_probe_paper.sh" in readme


def test_the_documentation_never_asks_the_owner_for_the_password() -> None:
    """The owner must never be told to type, export or invent it.

    The previous documentation did exactly that -- "supply PAPER_POSTGRES_* from
    the PAPER secret store" -- for a value the store did not contain and no
    command created.
    """
    readme = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

    for forbidden in (
        "export PAPER_POSTGRES_PASSWORD=",
        "export PAPER_POSTGRES_USER=",
    ):
        assert forbidden not in readme, f"the README tells the owner to {forbidden}"
