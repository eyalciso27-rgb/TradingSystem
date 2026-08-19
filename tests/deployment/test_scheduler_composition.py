"""The canonical PAPER startup must start the scheduler. No flag to remember.

"A scheduler executable exists" is not the requirement. The requirement is that
normal operation runs it, so the owner never has to remember the monthly
refresh. That is a property of the deployment composition, so it is tested here
rather than assumed.

An earlier version put the scheduler behind a Compose profile and documented
`--profile apps`. That satisfied the letter and missed the point: an operator
who runs the ordinary paper command gets no scheduler and no monthly refresh,
and nothing tells them. These tests exist so that cannot come back.

The test/CI base stack staying infrastructure-only IS intentional, and is
asserted too — a CI stack must not start a service that reaches the network.

Static: the compose files are parsed, never executed. No Docker required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"

BASE = DEPLOY / "compose.yaml"
PAPER = DEPLOY / "compose.paper.yaml"
DEV = DEPLOY / "compose.dev.yaml"
DOCKERFILE = DEPLOY / "Dockerfile.scheduler"
README = DEPLOY / "README.md"

#: The canonical paper startup, exactly as deploy/README.md documents it.
CANONICAL_PAPER_STARTUP = (
    "docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml up -d"
)

UNIVERSE_MOUNT = "/data/sp500-current"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} is not a compose mapping"
    return loaded


def _services(path: Path) -> dict[str, Any]:
    services = _load(path).get("services", {})
    assert isinstance(services, dict)
    return services


def _merged(*paths: Path) -> dict[str, Any]:
    """Compose's own override semantics for the part that matters here.

    Later files add services and override keys of services already present,
    which is all these assertions depend on.
    """
    merged: dict[str, Any] = {}
    for path in paths:
        for name, definition in _services(path).items():
            merged.setdefault(name, {}).update(definition or {})
    return merged


# == the invariant: canonical paper startup starts the scheduler ==============


def test_canonical_paper_startup_includes_the_scheduler() -> None:
    merged = _merged(BASE, PAPER)
    assert "scheduler" in merged, (
        "the canonical paper stack must include the scheduler; without it the"
        " monthly refresh silently never happens"
    )


def test_the_scheduler_needs_no_profile_in_paper() -> None:
    """A profile is a flag to remember, which is the thing being removed."""
    scheduler = _merged(BASE, PAPER)["scheduler"]
    assert "profiles" not in scheduler, (
        "a profiled service does not start on a normal `up`; the owner would"
        " have to remember --profile, which is exactly the dependency this"
        " deployment removes"
    )


def test_the_documented_paper_command_is_the_one_that_starts_it() -> None:
    """The docs and the composition must not disagree.

    The README previously carried two 'normal' paper commands, one of which
    silently omitted the scheduler.
    """
    readme = README.read_text(encoding="utf-8")
    assert CANONICAL_PAPER_STARTUP in readme
    assert "--profile" not in readme, "no startup path may depend on a profile flag"


def test_the_dev_stack_also_starts_it() -> None:
    assert "scheduler" in _merged(BASE, DEV)


# == what the scheduler service must actually be ==============================


def test_the_scheduler_runs_the_canonical_entry_point() -> None:
    """It must run `python -m trading_scheduler`, not something adjacent."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert 'CMD ["uv", "run", "python", "-m", "trading_scheduler"]' in dockerfile

    scheduler = _merged(BASE, PAPER)["scheduler"]
    build = scheduler["build"]
    assert build["dockerfile"] == "deploy/Dockerfile.scheduler"
    # No `command:` override, so the image's canonical entry point is what runs.
    assert "command" not in scheduler


def test_the_scheduler_returns_after_a_restart() -> None:
    """An automatic refresh that stops at the first reboot is not automatic."""
    for overlay in (PAPER, DEV):
        scheduler = _merged(BASE, overlay)["scheduler"]
        assert scheduler["restart"] == "unless-stopped", overlay.name


def test_the_snapshot_history_is_on_a_persistent_volume() -> None:
    """Container-local state would lose the last-known-good list on restart."""
    for overlay, expected in ((PAPER, "universe-data-paper"), (DEV, "universe-data-dev")):
        scheduler = _merged(BASE, overlay)["scheduler"]
        assert scheduler["volumes"] == [f"{expected}:{UNIVERSE_MOUNT}"], overlay.name
        declared = _load(overlay).get("volumes", {})
        assert expected in declared, f"{expected} must be a declared volume"


def test_the_universe_root_points_at_that_volume() -> None:
    scheduler = _merged(BASE, PAPER)["scheduler"]
    assert scheduler["environment"]["TRADING_UNIVERSE_ROOT"] == UNIVERSE_MOUNT


def test_paper_and_dev_never_share_universe_history() -> None:
    """Environment isolation covers the universe store like everything else."""
    paper = set(_load(PAPER).get("volumes", {}))
    dev = set(_load(DEV).get("volumes", {}))
    assert paper.isdisjoint(dev)


def test_the_paper_scheduler_is_pinned_to_paper() -> None:
    scheduler = _merged(BASE, PAPER)["scheduler"]
    assert scheduler["environment"]["TRADING_MODE"] == "PAPER"


def test_shutdown_is_given_time_to_be_graceful() -> None:
    scheduler = _merged(BASE, PAPER)["scheduler"]
    assert scheduler["stop_grace_period"] == "30s"
    assert "STOPSIGNAL SIGTERM" in DOCKERFILE.read_text(encoding="utf-8")


# == the test/CI stack stays infrastructure-only ==============================


def test_the_base_stack_starts_no_application_services() -> None:
    """Intentional: an ephemeral CI stack must not start a network-reaching job."""
    assert set(_services(BASE)) == {"postgres", "nats"}


@pytest.mark.parametrize("path", [BASE, PAPER, DEV])
def test_no_compose_file_uses_profiles_at_all(path: Path) -> None:
    for name, definition in _services(path).items():
        assert "profiles" not in (definition or {}), f"{path.name}:{name}"
