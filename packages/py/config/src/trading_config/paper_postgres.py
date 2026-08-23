"""Canonical PAPER PostgreSQL credentials, in the canonical secret store.

The second real owner-host run found the gap: ``compose.paper.yaml`` requires
``PAPER_POSTGRES_USER`` / ``PAPER_POSTGRES_PASSWORD`` "from the PAPER secret
store", the owner host has a PAPER secret store, and the only thing in it is
``ibkr/paper-account-id``. There was no PostgreSQL secret material and no
deployed database to recover any from, so the documented deployment contract
could not be satisfied by anyone following the documentation.

This closes that with one bootstrap, under the SAME ``SECRETS_ROOT`` tree. No
second secret store, no new location, no hard-coded owner path.

Two operations, deliberately separate:

* :func:`bootstrap` is explicit and owner-invoked. It creates a complete set
  when none exists and does nothing at all when one does -- it never rotates,
  because rotating a password out from under an initialised PostgreSQL volume
  produces exactly the authentication mismatch that cannot be repaired without
  either the old password or destroying the data.
* :func:`load` is what ordinary runs use. It reads a complete set or fails
  closed.

A PARTIAL set is refused by both. Half a credential is not a credential, and
guessing the missing half is how a probe ends up authenticating as something
nobody chose.
"""

from __future__ import annotations

import os
import secrets as secrets_module
import stat
from dataclasses import dataclass
from pathlib import Path

from trading_contracts.enums import Environment

from trading_config.secrets import (
    FileSecretStore,
    MissingSecretError,
    SecretError,
    SecretValue,
)

#: The canonical references, resolved under ``$SECRETS_ROOT/PAPER/``.
USERNAME_REF = "postgres/paper-username"
PASSWORD_REF = "postgres/paper-password"
DATABASE_REF = "postgres/paper-database"

ALL_REFS: tuple[str, ...] = (USERNAME_REF, PASSWORD_REF, DATABASE_REF)

#: One policy, stated once. The role and database names are not secrets, but
#: they live beside the password so the whole set moves together and a rerun
#: cannot silently disagree with the database it already created.
DEFAULT_USERNAME = "trading_paper"
DEFAULT_DATABASE = "trading_paper"

#: 32 URL-safe bytes: ~256 bits. Long enough that nobody is tempted to memorise
#: it, and no character class that any consumer has to special-case.
PASSWORD_BYTES = 32

#: Owner-readable only. The directory too: a 0600 file inside a world-readable
#: directory still leaks its name and its existence.
_FILE_MODE = 0o600
_DIR_MODE = 0o700


class PaperPostgresError(SecretError):
    """The PAPER PostgreSQL secret set is unusable. Always fail closed."""


@dataclass(frozen=True, slots=True)
class PaperPostgresCredentials:
    """One complete, consistent set. The password never renders."""

    username: str
    password: SecretValue
    database: str


def _paper_root(secrets_root: Path) -> Path:
    return Path(secrets_root) / Environment.PAPER.value


def _path_for(secrets_root: Path, ref: str) -> Path:
    return _paper_root(secrets_root).joinpath(*ref.split("/"))


def present_refs(secrets_root: Path) -> tuple[str, ...]:
    """Which of the canonical references currently exist and are non-empty."""
    found = []
    for ref in ALL_REFS:
        path = _path_for(secrets_root, ref)
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            found.append(ref)
    return tuple(found)


def load(secrets_root: Path) -> PaperPostgresCredentials:
    """The complete set, or a refusal naming what is missing.

    Never a default password, never a partial set. The message names the
    references, never a value.
    """
    store = FileSecretStore(Path(secrets_root), Environment.PAPER)
    try:
        username = store.get(USERNAME_REF).reveal().strip()
        password = store.get(PASSWORD_REF)
        database = store.get(DATABASE_REF).reveal().strip()
    except MissingSecretError as error:
        missing = [ref for ref in ALL_REFS if ref not in present_refs(secrets_root)]
        raise PaperPostgresError(
            "the PAPER PostgreSQL credentials are not in the secret store"
            f" (missing {missing}). Run the one-time bootstrap:"
            " bash scripts/bootstrap_paper_postgres.sh"
        ) from error
    if not username or not database or not password.reveal().strip():
        raise PaperPostgresError(
            "the PAPER PostgreSQL secret set is incomplete or empty; refusing"
            " to guess. Inspect the store and re-run the bootstrap only after"
            " deciding what the correct values are."
        )
    return PaperPostgresCredentials(
        username=username, password=password, database=database
    )


def _write_secret(path: Path, value: str) -> None:
    """Create one secret file atomically, owner-readable only.

    Written to a temporary name in the same directory with restrictive mode and
    then renamed, so a reader never observes a half-written secret and the file
    is never briefly world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict(path.parent, _DIR_MODE)
    temporary = path.parent / f".{path.name}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    _restrict(path, _FILE_MODE)


def _restrict(path: Path, mode: int) -> None:
    """Best effort: POSIX modes do not exist on every platform we develop on."""
    try:
        os.chmod(path, mode)
    except (NotImplementedError, OSError):  # pragma: no cover - platform dependent
        pass


def is_owner_only(path: Path) -> bool:
    """Does this path deny group and other? False on platforms without modes."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:  # pragma: no cover - platform dependent
        return False
    return not mode & (stat.S_IRWXG | stat.S_IRWXO)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """What the bootstrap did. Deliberately carries no secret value."""

    created: tuple[str, ...]
    already_present: tuple[str, ...]

    @property
    def rotated(self) -> bool:
        """Always False. Stated as a property so a test can assert on it."""
        return False


def bootstrap(secrets_root: Path) -> BootstrapResult:
    """Create the PAPER PostgreSQL secret set once. Never rotate.

    Idempotent by design: a rerun on a complete set changes nothing and reports
    that. That is not a convenience -- the PostgreSQL image only applies
    ``POSTGRES_PASSWORD`` to an EMPTY data directory, so silently generating a
    new password would leave the environment and an already-initialised volume
    permanently disagreeing, repairable only with the old password or by
    destroying the database.

    A PARTIAL set is refused without writing anything. Filling in the gap would
    mean pairing a fresh password with a username some earlier run chose, and
    the database would reject the pair.
    """
    root = Path(secrets_root)
    if not root.exists():
        raise PaperPostgresError(
            f"the secret store root does not exist: {root}. SECRETS_ROOT must"
            " point at the canonical store."
        )
    present = present_refs(root)
    if present and len(present) != len(ALL_REFS):
        missing = [ref for ref in ALL_REFS if ref not in present]
        raise PaperPostgresError(
            "the PAPER PostgreSQL secret set is PARTIAL"
            f" (present {list(present)}, missing {missing}). Refusing to write:"
            " completing it would pair a new value with values an earlier run"
            " chose, and an existing database would reject the pair. Decide"
            " what the correct values are, then place or remove them together."
        )
    if len(present) == len(ALL_REFS):
        return BootstrapResult(created=(), already_present=ALL_REFS)

    # Nothing present: create the complete set.
    _write_secret(_path_for(root, USERNAME_REF), DEFAULT_USERNAME)
    _write_secret(_path_for(root, DATABASE_REF), DEFAULT_DATABASE)
    _write_secret(
        _path_for(root, PASSWORD_REF), secrets_module.token_urlsafe(PASSWORD_BYTES)
    )
    return BootstrapResult(created=ALL_REFS, already_present=())


__all__ = [
    "ALL_REFS",
    "DATABASE_REF",
    "DEFAULT_DATABASE",
    "DEFAULT_USERNAME",
    "PASSWORD_BYTES",
    "PASSWORD_REF",
    "USERNAME_REF",
    "BootstrapResult",
    "PaperPostgresCredentials",
    "PaperPostgresError",
    "bootstrap",
    "is_owner_only",
    "load",
    "present_refs",
]
