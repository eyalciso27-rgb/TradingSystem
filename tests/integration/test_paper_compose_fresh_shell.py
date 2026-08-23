"""The fresh-shell Compose contract, exercised against real Docker Compose.

The previous round proved the probe wrapper could resolve PostgreSQL credentials
from the canonical store with no ``PAPER_POSTGRES_*`` in the environment, and
that was true. It was also not sufficient, and CI could not see why.

``deploy/compose.yaml`` requires the GENERIC names::

    POSTGRES_USER: ${POSTGRES_USER:?POSTGRES_USER must be provided; ...}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be provided; ...}

Compose interpolates each file **independently, before merging**. So

    docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml ...

must interpolate the base file first, and ``compose.paper.yaml`` overriding
those keys with ``PAPER_POSTGRES_*`` cannot rescue a base file that already
failed. A wrapper that exported only the ``PAPER_*`` family therefore still
broke on a real fresh owner shell -- at the first ``docker compose`` call,
before anything started.

CI could not catch it because the compose validation step supplied
``POSTGRES_USER=ci-syntax-check`` and ``POSTGRES_PASSWORD=ci-syntax-check``
itself. It proved Paper compose parses when handed test credentials, which is
not the owner runtime contract.

These tests use REAL ``docker compose``. They do not restate compose semantics
in a fake, because a reimplementation of the thing under test proves only that
the reimplementation works -- which is exactly how the original defect survived.

``config`` parses; it starts nothing, pulls nothing and needs no network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from trading_config.paper_postgres import bootstrap

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_CHECK = REPO_ROOT / "scripts" / "check_paper_compose_config.sh"
PAPER_ENV_LIB = REPO_ROOT / "scripts" / "lib" / "paper_env.sh"
WRAPPER = REPO_ROOT / "scripts" / "run_execution_probe_paper.sh"

#: Everything a fresh owner SSH session does NOT have. Both families.
CREDENTIAL_VARIABLES = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "PAPER_POSTGRES_USER",
    "PAPER_POSTGRES_PASSWORD",
    "PAPER_POSTGRES_DB",
)


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(  # noqa: S603, S607 - availability probe
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


requires_compose = pytest.mark.skipif(
    os.name != "posix" or not _docker_compose_available(),
    reason="needs POSIX and a working `docker compose`; runs in CI",
)


def _fresh_shell_environment(secrets_root: Path) -> dict[str, str]:
    """A real fresh owner shell: SECRETS_ROOT, and no credentials at all."""
    env = dict(os.environ)
    for name in CREDENTIAL_VARIABLES:
        env.pop(name, None)
    env["SECRETS_ROOT"] = str(secrets_root)
    return env


@pytest.fixture
def canonical_store(tmp_path: Path) -> Path:
    """A synthetic canonical store holding a complete PAPER PostgreSQL set.

    Built with the real bootstrap, so the test cannot pass against secret
    material shaped differently from what the owner will actually have.
    """
    root = tmp_path / "secrets"
    account = root / "PAPER" / "ibkr" / "paper-account-id"
    account.parent.mkdir(parents=True)
    account.write_text("DU1234567\n", encoding="utf-8")
    bootstrap(root)
    return root


def _bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a local bash fragment, no external input
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
    )


# == the contract ============================================================


@requires_compose
def test_paper_compose_interpolates_from_secrets_root_alone(
    canonical_store: Path,
) -> None:
    """THE fresh-shell contract, against real Compose.

    Only SECRETS_ROOT is set. Neither credential family is present. The
    canonical loader must make ``docker compose config`` succeed.
    """
    result = _bash(
        f"bash {CONFIG_CHECK.as_posix()}", _fresh_shell_environment(canonical_store)
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "PAPER_COMPOSE_CONFIG_OK" in result.stdout


@requires_compose
def test_without_the_loader_the_same_shell_cannot_interpolate_the_base_file(
    canonical_store: Path,
) -> None:
    """The control, and the root cause stated as an executable fact.

    Identical environment, real Compose, but going straight to the documented
    command instead of through the loader. It must FAIL, naming the generic
    variable -- which is precisely what a real owner shell hit and what the old
    CI step hid by supplying that variable itself.

    This is what makes the test above non-vacuous: it is the loader, not the
    ambient environment, that satisfies Compose.
    """
    result = _bash(
        "docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml config -q",
        _fresh_shell_environment(canonical_store),
    )

    assert result.returncode != 0, "the base file interpolated without credentials"
    # Which of the two required variables Compose names first is not
    # guaranteed -- it reported POSTGRES_USER on one run and POSTGRES_PASSWORD
    # on the next -- so asserting one name was a flake of this test's own
    # making. What must hold is that the failure is an interpolation failure
    # for a GENERIC name, raised by the BASE file. The base file's own `:?`
    # message identifies it: compose.paper.yaml's says "PAPER credentials come
    # from the PAPER secret store" instead.
    assert re.search(
        r"required variable POSTGRES_(USER|PASSWORD) is missing a value",
        result.stderr,
    ), result.stderr
    assert "no default credentials" in result.stderr, result.stderr


@requires_compose
def test_the_probe_stack_interpolates_from_secrets_root_alone(
    canonical_store: Path,
) -> None:
    """The probe adds the loopback-only publish override. Same contract."""
    result = _bash(
        "source scripts/lib/paper_env.sh && load_paper_environment && "
        "docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml"
        " -f deploy/compose.paper-probe.yaml config -q",
        _fresh_shell_environment(canonical_store),
    )

    assert result.returncode == 0, f"stderr={result.stderr}"


# == the cleanup path sees the same environment ==============================


def _join_continuations(body: str) -> list[str]:
    """Logical shell lines: a trailing backslash continues onto the next line.

    Scanning physical lines was a real defect in an earlier version of this
    file. The cleanup command is written as

        if ! docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \\
             up -d --no-deps postgres >/dev/null 2>&1; then

    so a physical-line scan captured only ``docker compose -f … -f …`` with no
    subcommand at all. That string exits 0 printing help, and the substitution
    of ``up -d --no-deps postgres`` then matched nothing -- so the "cleanup
    interpolates" test passed without ever asking Compose to interpolate.
    """
    logical: list[str] = []
    pending = ""
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.endswith(chr(92)):
            pending += stripped[:-1].rstrip() + " "
            continue
        logical.append((pending + stripped).strip())
        pending = ""
    if pending:
        logical.append(pending.strip())
    return logical


def _cleanup_compose_command() -> str:
    """The COMPLETE compose invocation the wrapper's EXIT trap actually runs.

    Read out of the wrapper rather than retyped, so the test follows the code
    if the teardown changes.
    """
    text = WRAPPER.read_text(encoding="utf-8")
    cleanup = text.split("cleanup() {", 1)[1].split("trap cleanup EXIT", 1)[0]
    for line in _join_continuations(cleanup):
        if line.startswith("if ! docker compose"):
            command = line.removeprefix("if ! ")
            for suffix in (">/dev/null 2>&1; then", "; then"):
                if command.endswith(suffix):
                    command = command[: -len(suffix)]
                    break
            return command.strip()
    raise AssertionError("no compose invocation found in cleanup()")


def test_the_extracted_cleanup_command_is_the_whole_command() -> None:
    """Guards the helper itself, which is what silently broke before.

    Without this, a helper that returns a truncated command makes every test
    built on it vacuous while still reporting green.
    """
    command = _cleanup_compose_command()

    assert command.startswith("docker compose ")
    assert "up -d --no-deps postgres" in command, (
        "the extraction lost the continuation line; the cleanup proof would be vacuous"
    )
    assert "-f deploy/compose.yaml" in command
    assert "-f deploy/compose.paper.yaml" in command
    assert ">/dev/null" not in command
    assert "; then" not in command


@requires_compose
def test_the_cleanup_invocation_also_interpolates_in_the_same_shell(
    canonical_store: Path,
) -> None:
    """Item 6: teardown must not fail where setup succeeded.

    The trap runs after a failure, in the same process, so it inherits whatever
    the loader exported -- but only because the loader uses ``export`` rather
    than plain assignment. Validate the trap's real file set under the fresh
    shell, substituting ``config -q`` for ``up -d`` so nothing is started.
    """
    command = _cleanup_compose_command()
    assert "compose.paper-probe.yaml" not in command, (
        "cleanup must re-apply the stack WITHOUT the probe publish"
    )
    parsed = command.replace("up -d --no-deps postgres", "config -q")
    assert parsed != command, "the substitution matched nothing; the proof is vacuous"
    assert parsed.endswith("config -q")

    result = _bash(
        f"source scripts/lib/paper_env.sh && load_paper_environment && {parsed}",
        _fresh_shell_environment(canonical_store),
    )

    assert result.returncode == 0, f"stderr={result.stderr}"


@requires_compose
def test_cleanup_still_interpolates_after_a_failure_mid_run(
    canonical_store: Path,
) -> None:
    """The realistic shape: load, arm the trap, fail, let the trap fire.

    Proves the exported environment survives to teardown, and that teardown
    neither publishes a port nor removes a volume.
    """
    extracted = _cleanup_compose_command()
    command = extracted.replace("up -d --no-deps postgres", "config -q")
    assert command != extracted, "the substitution matched nothing; the proof is vacuous"
    script = (
        "set -Eeuo pipefail\n"
        "source scripts/lib/paper_env.sh\n"
        "load_paper_environment\n"
        "PROBE_PUBLISH_STARTED=1\n"
        "cleanup() {\n"
        '  if [ "$PROBE_PUBLISH_STARTED" -eq 1 ]; then\n'
        f"    {command} || echo CLEANUP_COMPOSE_FAILED >&2\n"
        "    echo CLEANUP_RAN >&2\n"
        "  fi\n"
        "}\n"
        "trap cleanup EXIT\n"
        "echo 'simulated failure after the temporary publish' >&2\n"
        "exit 9\n"
    )

    result = _bash(script, _fresh_shell_environment(canonical_store))

    assert result.returncode == 9
    assert "CLEANUP_RAN" in result.stderr
    assert "CLEANUP_COMPOSE_FAILED" not in result.stderr, result.stderr


# == properties that hold without Docker =====================================


def test_the_loader_exports_rather_than_assigns() -> None:
    """Teardown inherits the environment only because these are exports.

    A plain assignment would be visible to the setup commands and to the trap
    alike within one shell, but not to `docker compose` as a child process.
    """
    text = PAPER_ENV_LIB.read_text(encoding="utf-8")

    for name in CREDENTIAL_VARIABLES:
        assert f"export {name}=" in text, f"{name} is not exported by the loader"


def test_no_compose_file_gained_a_default_credential() -> None:
    """The fix must not weaken the no-default-credentials policy.

    Satisfying Compose by giving the base file a default would have made the
    symptom disappear and left a stack that starts with credentials nobody
    chose. The `:?` stays; the loader supplies the values.
    """
    base = (REPO_ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")

    assert "${POSTGRES_USER:?" in base
    assert "${POSTGRES_PASSWORD:?" in base
    assert "${POSTGRES_USER:-" not in base
    assert "${POSTGRES_PASSWORD:-" not in base
    paper = (REPO_ROOT / "deploy" / "compose.paper.yaml").read_text(encoding="utf-8")
    assert "${PAPER_POSTGRES_USER:?" in paper
    assert "${PAPER_POSTGRES_PASSWORD:?" in paper


def test_no_dotenv_file_is_committed() -> None:
    """Credentials must not arrive via a checked-in .env either."""
    for candidate in (REPO_ROOT / ".env", REPO_ROOT / "deploy" / ".env"):
        assert not candidate.exists(), f"{candidate} must not be committed"
