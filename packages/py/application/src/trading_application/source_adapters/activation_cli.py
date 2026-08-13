"""Source activation CLI (Track C).

Offline owner tooling for the four merged adapters. Commands::

    list                          list activatable sources
    runbook <source>              print the exact owner-run steps + PASS/FAIL
    secret-keys <source>          print the required/optional secret keys
    check-secrets <source>        load secrets fail-closed; report OK or missing

The real read-only preflight (and the sanitized evidence artifact) run on the
owner's controlled host with the REAL provider client injected — this offline
CLI never constructs a network client and never logs in. ``check-secrets`` only
confirms the secret files exist; it never prints a secret value.

Usage:
    uv run python -m trading_application.source_adapters.activation_cli <command> [source] [opts]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trading_config.secrets import FileSecretStore
from trading_contracts.enums import Environment

from trading_application.source_adapters.activation import ActivationError
from trading_application.source_adapters.activation_sources import RUNBOOKS, SPECS


def _print_runbook(source: str) -> int:
    runbook = RUNBOOKS[source]
    print(f"# {source} activation ({runbook.connector_id}) -> {runbook.poi_id}")
    print("\nsecret keys:")
    for key in runbook.secret_keys or ("(none required)",):
        print(f"  - {key}")
    print("\nstates to exercise:")
    for state in runbook.states:
        print(f"  - {state}")
    print("\nowner commands:")
    for i, command in enumerate(runbook.owner_commands, 1):
        print(f"  {i}. {command}")
    print("\nPASS criteria:")
    for c in runbook.pass_criteria:
        print(f"  - {c}")
    print("\nFAIL criteria:")
    for c in runbook.fail_criteria:
        print(f"  - {c}")
    print(f"\nre-auth:  {runbook.reauth_procedure}")
    print(f"logout:   {runbook.logout_procedure}")
    print(f"rotation: {runbook.rotation_procedure}")
    return 0


def _check_secrets(source: str, environment: Environment, root: Path) -> int:
    spec = SPECS[source]
    store = FileSecretStore(root, environment)
    try:
        spec.load(store)
    except ActivationError as error:
        print(f"MISSING: {error}")
        return 1
    print(
        f"OK: all required secrets present for {source} ({spec.connector_id})"
        f" in {environment.value}"
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("list", "runbook", "secret-keys", "check-secrets")
    )
    parser.add_argument("source", nargs="?", choices=sorted(RUNBOOKS))
    parser.add_argument(
        "--environment", choices=[e.value for e in Environment], default=Environment.RESEARCH.value
    )
    parser.add_argument("--root", type=Path, default=Path("secrets"))
    args = parser.parse_args(argv[1:])

    if args.command == "list":
        for name, runbook in sorted(RUNBOOKS.items()):
            print(f"{name}: {runbook.connector_id} -> {runbook.poi_id}")
        return 0

    if args.source is None:
        print(f"command {args.command!r} requires a source (one of {sorted(RUNBOOKS)})")
        return 2

    if args.command == "runbook":
        return _print_runbook(args.source)

    if args.command == "secret-keys":
        spec = SPECS[args.source]
        for key in spec.required_keys:
            print(f"required: {key}")
        for key in spec.optional_keys:
            print(f"optional: {key}")
        return 0

    # check-secrets
    return _check_secrets(args.source, Environment(args.environment), args.root)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
