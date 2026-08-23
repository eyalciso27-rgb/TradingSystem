"""One-time, owner-invoked bootstrap of the PAPER PostgreSQL credentials.

    bash scripts/bootstrap_paper_postgres.sh

Creates the canonical secret set under ``$SECRETS_ROOT/PAPER/postgres/`` if and
only if none of it exists yet. Reruns are safe and change nothing.

Nothing here prints a password, and nothing here rotates one. See
``trading_config.paper_postgres`` for why rotation is the dangerous operation
rather than the safe one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from trading_config.paper_postgres import (
    ALL_REFS,
    DATABASE_REF,
    USERNAME_REF,
    BootstrapResult,
    PaperPostgresError,
    bootstrap,
    is_owner_only,
    load,
)

ENV_SECRETS_ROOT = "SECRETS_ROOT"

OK = "PAPER_POSTGRES_BOOTSTRAP_OK"
UNCHANGED = "PAPER_POSTGRES_BOOTSTRAP_UNCHANGED"
FAILED = "PAPER_POSTGRES_BOOTSTRAP_FAILED"


def _describe(result: BootstrapResult, secrets_root: Path) -> list[str]:
    """Human-readable outcome. Names references and paths; never values."""
    lines: list[str] = []
    if result.created:
        lines.append(f"{OK}: created the PAPER PostgreSQL credentials")
        for ref in result.created:
            lines.append(f"  created  $SECRETS_ROOT/PAPER/{ref}")
    else:
        lines.append(f"{UNCHANGED}: the PAPER PostgreSQL credentials already exist")
        for ref in result.already_present:
            lines.append(f"  kept     $SECRETS_ROOT/PAPER/{ref}")
        lines.append("  nothing was rotated, overwritten or removed.")

    # The username and database are policy, not secrets, so echoing them helps
    # the owner confirm the deployment matches. The password never appears.
    credentials = load(secrets_root)
    lines.append(f"  role     {credentials.username}   (ref {USERNAME_REF})")
    lines.append(f"  database {credentials.database}   (ref {DATABASE_REF})")
    lines.append("  password stored, never displayed and never logged.")
    return lines


def _permission_warnings(secrets_root: Path) -> list[str]:
    """Report any credential file readable beyond its owner.

    Only on POSIX. Windows does not implement the group/other bits at all --
    ``os.chmod`` there toggles the read-only flag and leaves the reported mode
    at 0o666 -- so warning on it would be advice the owner cannot act on, about
    a platform the deployment does not use.
    """
    if os.name != "posix":
        return []
    warnings = []
    for ref in ALL_REFS:
        path = secrets_root / "PAPER" / Path(*ref.split("/"))
        if path.exists() and not is_owner_only(path):
            warnings.append(f"WARNING: {ref} is readable beyond its owner; chmod 600 it")
    return warnings


def main(argv: list[str] | None = None) -> int:
    del argv
    raw_root = os.environ.get(ENV_SECRETS_ROOT, "").strip()
    if not raw_root:
        sys.stderr.write(
            f"{FAILED}: {ENV_SECRETS_ROOT} must point at the canonical secret"
            " store, the same one that holds ibkr/paper-account-id.\n"
        )
        return 2
    secrets_root = Path(raw_root)
    try:
        result = bootstrap(secrets_root)
    except PaperPostgresError as error:
        # PaperPostgresError messages are constructed from reference names and
        # paths only -- never from a secret value -- so this is safe to print.
        sys.stderr.write(f"{FAILED}: {error}\n")
        return 2
    for line in _describe(result, secrets_root):
        sys.stdout.write(line + "\n")
    for warning in _permission_warnings(secrets_root):
        sys.stderr.write(warning + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
