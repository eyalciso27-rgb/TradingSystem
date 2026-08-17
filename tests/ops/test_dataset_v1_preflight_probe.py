"""Behavioural regression for the Dataset V1 runner's TCP preflight
(scripts/run_dataset_v1_real_qualification.sh).

Owner-host bug being fixed: the preflight was gated on ``command -v nc``, so a
host WITHOUT the ``nc`` binary was reported as ``GATEWAY_UNREACHABLE`` even
while ``ss -ltnp`` proved IB Gateway was listening on 4002. A missing tool is a
tooling gap, never evidence about the port.

These tests source the runner in a POSIX shell (main() is guarded, so nothing
runs) and exercise the pure probe functions against a real loopback socket:

    tcp_probe <host> <port>            -> 0 open | 1 closed | 2 no probe available
    ibkr_preflight_status <host> <port> -> the status token the owner sees

No IB Gateway, no broker connection and no order surface is involved: the
listener is a throwaway loopback socket owned by the test.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = (REPO_ROOT / "scripts" / "run_dataset_v1_real_qualification.sh").as_posix()

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="sources the runner in a POSIX shell",
)


BASH = shutil.which("bash") or "bash"


def _bash(body: str, *, path: str | None = None) -> str:
    """Source the runner (main() is guarded) and run ``body``.

    ``path`` replaces the child's PATH so a probe's binaries are genuinely
    invisible rather than merely mocked. bash is invoked by ABSOLUTE path so
    that even an empty PATH still reaches the shell.
    """
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    script = f'set -uo pipefail\nsource "{RUNNER}"\n{body}\n'
    result = subprocess.run(  # noqa: S603
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
        env=env,
    )
    lines = [line for line in result.stdout.replace("\x00", "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


@contextlib.contextmanager
def _listening_port() -> Iterator[int]:
    """A real listening loopback socket, closed on exit."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(64)  # unaccepted probes must not starve the queue
        yield int(server.getsockname()[1])


def _closed_port() -> int:
    """A port that was bound and released: nothing is listening there now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


#: A PATH with nothing on it at all — the harshest possible "nc is absent" case.
#: bash's /dev/tcp is a builtin, so a correct probe still works here.
NO_TOOLS_PATH = ""


# -- item 1: a reachable port is detected WITHOUT nc --------------------------


@POSIX_ONLY
def test_listening_port_is_reachable_without_nc() -> None:
    with _listening_port() as port:
        status = _bash(f"ibkr_preflight_status 127.0.0.1 {port}", path=NO_TOOLS_PATH)
    assert status == "GATEWAY_REACHABLE"


@POSIX_ONLY
def test_tcp_probe_returns_zero_for_a_listening_port() -> None:
    with _listening_port() as port:
        assert _bash(f"tcp_probe 127.0.0.1 {port}; echo rc=$?") == "rc=0"


# -- item 2: a genuinely closed port is UNREACHABLE ---------------------------


@POSIX_ONLY
def test_closed_port_is_reported_unreachable() -> None:
    status = _bash(f"ibkr_preflight_status 127.0.0.1 {_closed_port()}")
    assert status == "GATEWAY_UNREACHABLE"


@POSIX_ONLY
def test_closed_port_is_unreachable_without_nc_too() -> None:
    status = _bash(f"ibkr_preflight_status 127.0.0.1 {_closed_port()}", path=NO_TOOLS_PATH)
    assert status == "GATEWAY_UNREACHABLE"


# -- item 3: the live port stays hard-blocked ---------------------------------


@POSIX_ONLY
def test_live_port_4001_is_rejected_and_never_probed() -> None:
    # The live API port must be refused BEFORE any connection is attempted.
    assert _bash("ibkr_preflight_status 127.0.0.1 4001") == "LIVE_PORT_REJECTED"


@POSIX_ONLY
def test_live_port_rejection_is_a_nonzero_preflight_failure() -> None:
    assert _bash("ibkr_preflight_status 127.0.0.1 4001 >/dev/null; echo rc=$?") == "rc=2"


@POSIX_ONLY
def test_live_port_is_rejected_before_any_connection_is_attempted() -> None:
    # A stub that would announce itself if it were ever reached. Port 4001 is
    # never bound by this test: the block must not depend on reachability.
    body = (
        "tcp_probe() { echo PROBE_WAS_ATTEMPTED; return 0; }\n"
        "ibkr_preflight_status 127.0.0.1 4001"
    )
    assert _bash(body) == "LIVE_PORT_REJECTED"


# -- item 4: a missing tool is NEVER a false unreachable ----------------------


@POSIX_ONLY
def test_absent_nc_alone_never_produces_unreachable() -> None:
    """The exact owner-host bug: nc absent, Gateway listening."""
    with _listening_port() as port:
        without_nc = _bash(f"ibkr_preflight_status 127.0.0.1 {port}", path=NO_TOOLS_PATH)
        with_everything = _bash(f"ibkr_preflight_status 127.0.0.1 {port}")
    assert without_nc == "GATEWAY_REACHABLE"
    assert without_nc == with_everything, "nc's presence must not change the verdict"


@POSIX_ONLY
def test_no_usable_probe_reports_tooling_gap_not_unreachable() -> None:
    """With every probe stubbed unavailable, the runner blames tooling, not the port."""
    body = (
        "_probe_bash() { return 2; }\n"
        "_probe_python() { return 2; }\n"
        "_probe_nc() { return 2; }\n"
        "ibkr_preflight_status 127.0.0.1 4002"
    )
    assert _bash(body) == "PREFLIGHT_TOOL_UNAVAILABLE"


@POSIX_ONLY
def test_unavailable_probe_defers_to_the_next_one() -> None:
    """An unavailable probe (rc=2) must not short-circuit the chain.

    Stubbed rather than host-dependent: the point is the chain's control flow,
    not which probe happens to exist on the runner.
    """
    body = (
        "_probe_bash() { return 2; }\n"
        "_probe_python() { return 0; }\n"
        "ibkr_preflight_status 127.0.0.1 4002"
    )
    assert _bash(body) == "GATEWAY_REACHABLE"


@POSIX_ONLY
def test_a_closed_verdict_from_the_first_available_probe_is_final() -> None:
    """rc=1 is a real answer: it must short-circuit, not fall through."""
    body = (
        "_probe_bash() { return 1; }\n"
        "_probe_python() { echo SHOULD_NOT_RUN; return 0; }\n"
        "ibkr_preflight_status 127.0.0.1 4002"
    )
    assert _bash(body) == "GATEWAY_UNREACHABLE"


@POSIX_ONLY
def test_probe_helpers_report_unavailable_rather_than_closed() -> None:
    """A probe whose binary is missing returns 2 (unavailable), not 1 (closed).

    This is the precise inversion of the owner-host bug: rc=1 here would mean
    "port closed" and would have produced the false GATEWAY_UNREACHABLE.
    """
    with _listening_port() as port:
        assert _bash(f"_probe_nc 127.0.0.1 {port} 3; echo rc=$?", path=NO_TOOLS_PATH) == "rc=2"


@POSIX_ONLY
def test_a_broken_interpreter_shim_is_unavailable_not_closed(tmp_path: Path) -> None:
    """A shim that exits with neither 0 nor 1 never ran our probe program.

    Real case: the Windows Store ``python3`` stub prints an advert and exits 49.
    Reading that as "port closed" is the same defect as the nc gate.
    """
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text("#!/bin/sh\necho 'not really python' >&2\nexit 49\n", encoding="utf-8")
    shim.chmod(0o755)
    with _listening_port() as port:
        rc = _bash(f"_probe_python 127.0.0.1 {port} 3; echo rc=$?", path=str(shim_dir))
    assert rc == "rc=2", "a broken interpreter must report UNAVAILABLE, not closed"


@POSIX_ONLY
def test_a_broken_interpreter_does_not_make_an_open_port_unreachable(tmp_path: Path) -> None:
    """End to end: broken python shim + no nc + open port must stay REACHABLE."""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text("#!/bin/sh\nexit 49\n", encoding="utf-8")
    shim.chmod(0o755)
    with _listening_port() as port:
        status = _bash(f"ibkr_preflight_status 127.0.0.1 {port}", path=str(shim_dir))
    assert status == "GATEWAY_REACHABLE"


# -- the runner stays sourceable and side-effect free -------------------------


@POSIX_ONLY
def test_sourcing_the_runner_executes_nothing() -> None:
    assert _bash("echo sourced-clean") == "sourced-clean"


@POSIX_ONLY
def test_sourcing_does_not_impose_errexit_on_the_caller() -> None:
    """`set -e` at file scope would abort the caller on a legitimate rc=1 probe.

    Without this, `tcp_probe <closed port>; echo rc=$?` kills the calling shell
    before it can report the result.
    """
    assert _bash(f"tcp_probe 127.0.0.1 {_closed_port()}; echo rc=$?") == "rc=1"


@POSIX_ONLY
def test_sourcing_does_not_require_any_binary_on_path() -> None:
    """Resolving the repo root at file scope needed `dirname`; with an empty
    PATH that failed at source time. Sourcing must be self-contained."""
    assert _bash("echo sourced-clean", path=NO_TOOLS_PATH) == "sourced-clean"


def test_runner_does_not_gate_the_probe_on_nc_being_installed() -> None:
    """Guards the regression at the source level, on every platform."""
    text = Path(RUNNER).read_text(encoding="utf-8")
    assert "command -v nc >/dev/null 2>&1 && nc -z" not in text, (
        "a missing nc must never be classified as an unreachable Gateway"
    )
    assert "/dev/tcp/" in text, "expected a dependency-free TCP probe"


def test_runner_does_not_claim_sec_ingestion() -> None:
    """The SEC line reports CONFIGURATION only; it must not imply a real run."""
    text = Path(RUNNER).read_text(encoding="utf-8")
    assert "configuration only" in text
    assert "not exercised" in text
