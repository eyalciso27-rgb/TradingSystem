"""CURRENT S&P 500 universe: monthly refresh, freshness, last-known-good.

ADR-0017 separates the CURRENT constituent list from HISTORICAL point-in-time
membership. This module owns the current list only.

The distinction is load-bearing and enforced by type, not by convention: a
``ConstituentSnapshot`` describes the index **as of one date**, and nothing here
can be substituted for PIT membership. Collecting a snapshot each month does
build a genuine forward history from the day collection starts — that is a
by-product, and it is never presented as reconstructed past history.

Safety rules:

* a failed, empty or malformed refresh NEVER replaces a valid last-known-good
  snapshot;
* freshness is explicit — ``CURRENT_LIST_VALID`` / ``CURRENT_LIST_STALE`` /
  ``CURRENT_LIST_UNAVAILABLE`` — and a stale list is usable only when the
  configured policy for that mode allows it;
* the free constituent dataset is recorded as ``PUBLIC_SECONDARY`` and is never
  labelled official or point-in-time.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1_sources import SourceAdapterError, SourceAuthority
from trading_market_data_core.sp500_wikitable import (
    ConstituentRow,
    ParseDiagnostics,
    TableParseError,
    parse_html_table,
    parse_wikitext_table,
)
from trading_market_data_core.trading_readiness import (
    FreshnessPolicy,
    GicsMode,
    UniverseListStatus,
)

UNIVERSE_SCHEMA_VERSION = "sp500-current-universe-1.0.0"

#: Roughly monthly, per the owner decision. Not a hard schedule: freshness is
#: what matters, and it is evaluated against the configured policy.
REFRESH_INTERVAL_DAYS = 30


@dataclass(frozen=True, slots=True)
class Constituent:
    """One current index member. ``security_id`` is never the ticker alone."""

    ticker: str
    name: str
    security_id: str
    cik: str | None = None
    #: Sector context when the source carries it. OPTIONAL and, from a free
    #: source, CURRENT classification only - never point-in-time.
    gics_sector: str | None = None
    gics_sub_industry: str | None = None
    date_added: date | None = None
    #: True only when a STABLE identifier (CIK) backed the security_id. A
    #: ticker can change, so a ticker-derived id is operational routing only
    #: and is never promoted to stable identity.
    identity_resolved: bool = True

    @property
    def identity_kind(self) -> str:
        return "sec_cik" if self.identity_resolved else "ticker_operational_unresolved"

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker, "name": self.name, "security_id": self.security_id,
            "identity_resolved": self.identity_resolved,
            "identity_kind": self.identity_kind,
            "cik": self.cik, "gics_sector": self.gics_sector,
            "gics_sub_industry": self.gics_sub_industry,
            "date_added": self.date_added.isoformat() if self.date_added else None,
        }


@dataclass(frozen=True, slots=True)
class ConstituentSnapshot:
    """The index membership AS OF one date. NOT point-in-time history."""

    index_id: str
    as_of: date
    fetched_at: datetime
    source_id: str
    source_authority: str
    constituents: tuple[Constituent, ...]
    checksum: str = ""
    #: Stated on the artifact so no reader can mistake it for PIT evidence.
    is_point_in_time_history: bool = False
    gics_mode: str = GicsMode.FREE_RECONSTRUCTED.value
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def count(self) -> int:
        return len(self.constituents)

    def body(self) -> dict[str, object]:
        return {
            "schema_version": UNIVERSE_SCHEMA_VERSION,
            "index_id": self.index_id,
            "as_of": self.as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "source_id": self.source_id,
            "source_authority": self.source_authority,
            "constituent_count": self.count,
            "is_point_in_time_history": self.is_point_in_time_history,
            "gics_mode": self.gics_mode,
            "constituents": [c.to_dict() for c in sorted(self.constituents, key=_sort_key)],
            "notes": list(self.notes),
        }

    def compute_checksum(self) -> str:
        serialized = json.dumps(
            self.body(), sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
        )
        return "sha256:" + hashlib.sha256(serialized.encode("ascii")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        payload = self.body()
        payload["checksum"] = self.checksum or self.compute_checksum()
        return payload

    def age_days(self, now: datetime) -> int:
        return max(0, (now.date() - self.as_of).days)

    def tickers(self) -> frozenset[str]:
        return frozenset(c.ticker for c in self.constituents)

    def sector_of(self, ticker: str) -> str | None:
        """Optional sector context. UNKNOWN (None) is a valid, safe answer."""
        for constituent in self.constituents:
            if constituent.ticker == ticker:
                return constituent.gics_sector
        return None


@dataclass(frozen=True, slots=True)
class RawConstituentRefresh:
    """A parsed CURRENT snapshot plus the exact raw bytes it came from."""

    snapshot: ConstituentSnapshot
    raw_bytes: bytes


def _sort_key(constituent: Constituent) -> tuple[str, str]:
    return (constituent.ticker, constituent.security_id)


def classify_freshness(
    snapshot: ConstituentSnapshot | None, *, now: datetime, policy: FreshnessPolicy
) -> UniverseListStatus:
    """Explicit freshness. A stale list is never silently treated as valid."""
    if snapshot is None or snapshot.count == 0:
        return UniverseListStatus.CURRENT_LIST_UNAVAILABLE
    age = snapshot.age_days(now)
    if age <= policy.max_age_days:
        return UniverseListStatus.CURRENT_LIST_VALID
    if policy.allow_stale and age <= policy.max_age_days + policy.stale_grace_days:
        return UniverseListStatus.CURRENT_LIST_STALE
    return (
        UniverseListStatus.CURRENT_LIST_STALE
        if policy.allow_stale
        else UniverseListStatus.CURRENT_LIST_UNAVAILABLE
    )


#: A refresh must look like an index, not like a parse accident.
MIN_PLAUSIBLE_CONSTITUENTS = 400
MAX_PLAUSIBLE_CONSTITUENTS = 600


def validate_snapshot(snapshot: ConstituentSnapshot) -> tuple[str, ...]:
    """Findings that make a snapshot unfit to replace a known-good one."""
    findings: list[str] = []
    if snapshot.count == 0:
        findings.append("snapshot is empty")
    elif not (MIN_PLAUSIBLE_CONSTITUENTS <= snapshot.count <= MAX_PLAUSIBLE_CONSTITUENTS):
        findings.append(
            f"constituent count {snapshot.count} is outside the plausible range"
            f" {MIN_PLAUSIBLE_CONSTITUENTS}..{MAX_PLAUSIBLE_CONSTITUENTS}"
        )
    tickers = [c.ticker for c in snapshot.constituents]
    if len(set(tickers)) != len(tickers):
        findings.append("snapshot contains duplicate tickers")
    if any(not c.security_id.strip() for c in snapshot.constituents):
        findings.append("a constituent has no security_id at all")
    if snapshot.is_point_in_time_history:
        findings.append(
            "a CURRENT snapshot must never claim to be point-in-time history"
        )
    # Integrity: a candidate carrying a checksum that does not authenticate its
    # own body has been corrupted or tampered with somewhere between the source
    # and here. It must never replace a good list.
    if snapshot.checksum and snapshot.checksum != snapshot.compute_checksum():
        findings.append(
            "snapshot checksum does not authenticate its body (corrupt or tampered):"
            f" declared {snapshot.checksum}, recomputed {snapshot.compute_checksum()}"
        )
    return tuple(findings)


#: Greppable convention (mirrors ``DIAGNOSTIC_NOTE_PREFIX`` below) for binding a
#: snapshot to the raw source artifact it was parsed from. Because ``notes`` is
#: part of the checksummed body, once a snapshot is bound its normalized
#: checksum authenticates the raw binding too -- altering the recorded raw
#: checksum after the fact is exactly as detectable as altering a ticker.
RAW_CHECKSUM_NOTE_PREFIX = "raw source checksum: "


def raw_artifact_checksum(raw_bytes: bytes) -> str:
    """The sha256 of a raw source artifact's actual bytes."""
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def bind_raw_source(
    snapshot: ConstituentSnapshot, raw_checksum: str
) -> ConstituentSnapshot:
    """Bind a snapshot to the raw artifact it was parsed from and reseal it.

    This only records and authenticates the CLAIM; it does not verify the
    claim against real bytes. ``UniverseStore.accept_with_raw_artifact`` is
    what enforces that the claim matches the raw bytes actually supplied at
    archive time and durably retains them for later re-verification.
    """
    if not raw_checksum.startswith("sha256:"):
        raise SourceAdapterError("raw_checksum must be a sha256: digest")
    if recorded_raw_checksum(snapshot) is not None:
        raise SourceAdapterError(
            "snapshot already carries a raw source checksum binding; a snapshot"
            " is bound to exactly one raw artifact"
        )
    bound = replace(
        snapshot, notes=snapshot.notes + (f"{RAW_CHECKSUM_NOTE_PREFIX}{raw_checksum}",)
    )
    return replace(bound, checksum=bound.compute_checksum())


def recorded_raw_checksum(snapshot: ConstituentSnapshot) -> str | None:
    """The raw-artifact checksum a snapshot declares itself bound to, if any."""
    checksums = recorded_raw_checksums(snapshot)
    return checksums[0] if checksums else None


def recorded_raw_checksums(snapshot: ConstituentSnapshot) -> tuple[str, ...]:
    """All raw-artifact checksum claims a snapshot carries."""
    return tuple(
        note[len(RAW_CHECKSUM_NOTE_PREFIX) :]
        for note in snapshot.notes
        if note.startswith(RAW_CHECKSUM_NOTE_PREFIX)
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically -- fsync-then-rename-then-dir-fsync.

    A reader (this process or another) can only ever observe the COMPLETE
    previous content or the COMPLETE new content, never a partial write left
    by a crash, a full disk, or a killed process mid-write: the data is
    written to a temporary file in the SAME directory (so the final
    ``os.replace`` is an atomic rename on one filesystem, not a cross-device
    copy), flushed and ``fsync``ed before the rename, and then the parent
    directory is ``fsync``ed after the rename so the directory entry itself is
    durable on POSIX filesystems. The temporary file is removed if anything
    fails before the rename completes -- no visible partial artifact is ever
    left behind on failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _fsync_directory(path: Path) -> None:
    """Make a just-created/replaced directory entry durable on POSIX."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class UniverseStore:
    """Monthly snapshots on disk, with an explicit last-known-good.

    ``accept`` is the ONLY way a snapshot becomes current, and it refuses a
    snapshot that fails validation — so a failed or malformed refresh cannot
    quietly replace a good list.

    Archived entries are WRITE-ONCE: once a snapshot for a given
    ``(index_id, as_of)`` has been accepted, it is the permanent forward-history
    record for that date. A later call for the same date is accepted only when
    it is byte-identical (an idempotent retry after e.g. a crashed process); any
    other candidate for a date already on record is refused rather than
    silently rewriting archived history. A new observation belongs under a new
    ``as_of``, never as a correction of a past one.
    """

    def __init__(self, root: Path, *, allow_normalized_only: bool = False) -> None:
        self.root = root
        self.allow_normalized_only = allow_normalized_only

    def _path(self, snapshot: ConstituentSnapshot) -> Path:
        return self.root / f"{snapshot.index_id}-{snapshot.as_of.isoformat()}.json"

    def _raw_path(self, snapshot: ConstituentSnapshot) -> Path:
        # Deliberately NOT ``*.json`` -- it must never be picked up by
        # ``snapshots()``/``latest()``, which glob only normalized entries.
        return self.root / f"{snapshot.index_id}-{snapshot.as_of.isoformat()}.raw"

    def _lock_path(self, snapshot: ConstituentSnapshot) -> Path:
        return self.root / f".{snapshot.index_id}-{snapshot.as_of.isoformat()}.lock"

    @contextlib.contextmanager
    def _locked(self, snapshot: ConstituentSnapshot) -> Iterator[None]:
        """Serialize the whole read-check-write sequence for one
        ``(index_id, as_of)`` across processes AND threads.

        Without this, two concurrent writers (two refresh processes, a
        refresh racing a manual CLI run, or two threads) could each observe
        an absent archive entry in :meth:`_prepare_accept`, both pass
        validation, and then both call ``os.replace`` -- which OVERWRITES
        rather than creates exclusively. The second writer would silently
        win, either discarding the first writer's accepted candidate outright
        (both API calls report success, but only one survives on disk) or,
        worse, pairing a normalized entry from one candidate with the raw
        sidecar from another. That is exactly the write-once invariant this
        store exists to guarantee, and a check-then-act sequence alone cannot
        make it hold under concurrency.

        ``fcntl.flock`` on an exclusively-opened lock file blocks a second
        writer for the same date rather than failing it outright (so a
        legitimate identical retry or an honestly concurrent refresh simply
        waits its turn), and is released automatically if this process dies
        while holding it -- no stale-lock cleanup is required. Every
        accepting path re-runs :meth:`_prepare_accept` (a fresh read of
        current disk state) only AFTER acquiring this lock, so the writer
        that loses the race sees the winner's already-committed content and
        is refused (or, if byte-identical, treated as idempotent) exactly as
        the write-once contract promises.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(snapshot)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _prepare_accept(
        self, snapshot: ConstituentSnapshot
    ) -> tuple[ConstituentSnapshot | None, Path | None, bool, tuple[str, ...]]:
        """Validate and resolve write-once conflicts WITHOUT writing anything.

        Returns ``(sealed, path, already_written, findings)``:

        * ``findings`` non-empty -> refuse; nothing may be written;
        * ``already_written`` True -> an idempotent retry: ``path`` already
          holds byte-identical content and must NOT be written again;
        * otherwise the caller may proceed to atomically write ``sealed`` to
          ``path``.

        Read-only: this only ever reads the existing archive entry (if any)
        to check for a write-once conflict. Splitting validation from the
        actual write is what lets :meth:`accept_with_raw_artifact` confirm
        BOTH the raw and normalized sides are writable before committing
        either one.
        """
        findings = validate_snapshot(snapshot)
        if findings:
            return None, None, False, findings
        sealed = snapshot if snapshot.checksum else _with_checksum(snapshot)
        path = self._path(sealed)
        if path.is_file():
            try:
                existing = load_snapshot(path)
            except SourceAdapterError as error:
                return None, None, False, (
                    f"an archived snapshot already exists at {path} and is unreadable"
                    f" ({error}); refusing to overwrite an immutable archive entry",
                )
            if existing.checksum != sealed.checksum:
                return None, None, False, (
                    f"a snapshot for {sealed.index_id} as_of {sealed.as_of.isoformat()} is"
                    f" already archived immutably at {path}; refusing to overwrite it with"
                    " different content (archive entries are write-once; a new"
                    " observation needs a new as_of date, not a correction of a past one)",
                )
            # Byte-identical to what is already archived: an idempotent retry,
            # not a mutation. Nothing to write.
            return sealed, path, True, ()
        return sealed, path, False, ()

    def accept(self, snapshot: ConstituentSnapshot) -> tuple[Path | None, tuple[str, ...]]:
        """Persist a snapshot if it validates. Returns (path, findings).

        The write itself is atomic (:func:`_atomic_write_bytes`), and the
        whole check-then-write sequence is serialized per ``(index_id,
        as_of)`` (:meth:`_locked`): a reader never observes a partially
        written normalized entry, and a concurrent conflicting writer for the
        same date is refused rather than racing this one.
        """
        if not self.allow_normalized_only:
            return None, (
                "plain archive acceptance is disabled for this store; production"
                " monthly snapshots must use accept_with_raw_artifact so raw"
                " provenance is retained with the normalized entry",
            )
        with self._locked(snapshot):
            sealed, path, already_written, findings = self._prepare_accept(snapshot)
            if findings or sealed is None or path is None:
                return None, findings
            if not already_written:
                _atomic_write_bytes(
                    path, json.dumps(sealed.to_dict(), indent=2, sort_keys=True).encode("utf-8")
                )
            return path, ()

    def snapshots(self) -> tuple[Path, ...]:
        if not self.root.is_dir():
            return ()
        return tuple(
            path
            for path in sorted(self.root.glob("*.json"))
            # sidecar files (maintenance state) are not snapshots
            if not path.name.startswith("maintenance-")
        )

    def latest(self) -> ConstituentSnapshot | None:
        """The most recent FULLY VALID accepted snapshot — the last-known-good.

        For a raw-bound entry (see :meth:`accept_with_raw_artifact`), "fully
        valid" also requires its retained raw sidecar to still verify: a
        newer entry whose normalized body is intact but whose independent raw
        provenance has since been lost or tampered with is not eligible here,
        exactly as if its normalized body itself had failed to authenticate.
        Selection falls back to the newest entry that IS fully valid (which
        may carry no raw binding at all, or a raw binding that still verifies).
        """
        best: ConstituentSnapshot | None = None
        for path in self.snapshots():
            try:
                candidate = load_snapshot(path)
            except SourceAdapterError:
                continue
            raw_checksum = recorded_raw_checksum(candidate)
            if raw_checksum is None and not self.allow_normalized_only:
                continue
            if raw_checksum is not None and self.verify_raw_artifact(candidate):
                continue
            if best is None or candidate.as_of > best.as_of:
                best = candidate
        return best

    def due_for_refresh(self, *, now: datetime) -> bool:
        latest = self.latest()
        return latest is None or latest.age_days(now) >= REFRESH_INTERVAL_DAYS

    def accept_with_raw_artifact(
        self, snapshot: ConstituentSnapshot, raw_bytes: bytes
    ) -> tuple[Path | None, tuple[str, ...]]:
        """Archive a snapshot AND durably retain the raw artifact it was
        parsed from, enforcing the raw <-> normalized checksum binding.

        Fail-closed, and strictly stronger than :meth:`accept`:

        * the snapshot MUST already declare a raw-checksum binding (see
          :func:`bind_raw_source`) that matches the sha256 of ``raw_bytes``
          actually supplied here -- a snapshot with no binding, or one whose
          binding does not match the real bytes, is refused outright, and
          nothing is written;
        * BOTH sides are validated (raw match, normalized validation, AND
          each side's write-once conflict check) before either one is
          written, so a refusal never leaves a partial write behind;
        * the two writes are each individually atomic
          (:func:`_atomic_write_bytes`), and the RAW artifact is committed
          FIRST, the normalized entry SECOND. A crash between the two can
          leave at most an unreferenced raw sidecar with no normalized entry
          claiming it yet -- never the reverse (a normalized entry claiming a
          raw binding that is not durably backed by real bytes). Calling this
          method again with the SAME ``(snapshot, raw_bytes)`` after such a
          crash recovers idempotently: the already-written raw side is left
          alone and only the missing normalized side is written;
        * once both sides are durably archived, :meth:`latest` re-verifies
          the raw sidecar on every lookup, so tampering it later demotes the
          entry exactly as tampering the normalized body would;
        * the whole read-check-write sequence for both sides is serialized
          per ``(index_id, as_of)`` (:meth:`_locked`), so two concurrent
          writers racing DIFFERENT candidates for the same date can never
          both succeed or pair a normalized entry with the wrong raw sidecar
          -- exactly one wins, and the other is refused (or, if
          byte-identical, treated as idempotent) once it observes the
          winner's already-committed content.
        """
        recorded_checksums = recorded_raw_checksums(snapshot)
        if not recorded_checksums:
            return None, (
                "snapshot carries no raw source checksum binding (see"
                " bind_raw_source); refusing to archive without durable raw"
                " provenance",
            )
        if len(recorded_checksums) != 1:
            return None, (
                "snapshot carries multiple raw source checksum bindings;"
                " refusing to archive ambiguous raw provenance",
            )
        recorded = recorded_checksums[0]
        actual = raw_artifact_checksum(raw_bytes)
        if recorded != actual:
            return None, (
                "raw artifact checksum mismatch: snapshot declares a binding to"
                f" {recorded}, but the supplied raw bytes hash to {actual};"
                " refusing to archive",
            )

        with self._locked(snapshot):
            sealed, normalized_path, normalized_already_written, findings = (
                self._prepare_accept(snapshot)
            )
            if findings or sealed is None or normalized_path is None:
                return None, findings

            raw_path = self._raw_path(sealed)
            raw_already_written = raw_path.is_file()
            if raw_already_written and raw_path.read_bytes() != raw_bytes:
                return None, (
                    f"a raw artifact for {sealed.index_id} as_of"
                    f" {sealed.as_of.isoformat()} is already archived immutably at"
                    f" {raw_path}; refusing to overwrite it with different bytes",
                )

            # Commit order matters for crash-safety -- see the docstring: raw
            # first, normalized second, so a normalized entry is never visible
            # before the raw bytes it claims are durably on disk.
            if not raw_already_written:
                _atomic_write_bytes(raw_path, raw_bytes)
            if not normalized_already_written:
                _atomic_write_bytes(
                    normalized_path,
                    json.dumps(sealed.to_dict(), indent=2, sort_keys=True).encode("utf-8"),
                )
            return normalized_path, ()

    def verify_raw_artifact(self, snapshot: ConstituentSnapshot) -> tuple[str, ...]:
        """Re-verify a previously archived raw artifact against the checksum
        the snapshot declares -- durable, independent of any ephemeral
        fixture that may no longer exist by the time this is called."""
        recorded_checksums = recorded_raw_checksums(snapshot)
        if not recorded_checksums:
            return ("snapshot carries no raw source checksum binding",)
        if len(recorded_checksums) != 1:
            return ("snapshot carries multiple raw source checksum bindings",)
        recorded = recorded_checksums[0]
        raw_path = self._raw_path(snapshot)
        if not raw_path.is_file():
            return (f"no retained raw artifact found at {raw_path}",)
        actual = raw_artifact_checksum(raw_path.read_bytes())
        if actual != recorded:
            return (
                "retained raw artifact checksum mismatch (tampered or stale):"
                f" declared {recorded}, recomputed {actual}",
            )
        return ()


def _with_checksum(snapshot: ConstituentSnapshot) -> ConstituentSnapshot:
    return replace(snapshot, checksum=snapshot.compute_checksum())


def _constituent_from_dict(item: Mapping[str, object]) -> Constituent:
    """Rebuild one constituent, INCLUDING whether its identity was resolved.

    Restoring this mattered more than it looks: ``identity_resolved`` is part of
    the checksummed body, so dropping it made any stored list containing a
    CIK-less constituent fail its own integrity check on the way back in — and a
    last-known-good list that cannot be loaded is not a last-known-good list.

    The stored value is reproduced verbatim rather than re-derived. A loader
    that "corrects" a field on the way in cannot round-trip, which would defeat
    the checksum that is the actual integrity mechanism here. Producing the flag
    correctly is the parser's job; proving the file is unaltered is this one's.
    Only a file that never carried the field at all falls back to deriving it.
    """
    cik = str(item["cik"]) if item.get("cik") else None
    date_added = item.get("date_added")
    return Constituent(
        ticker=str(item["ticker"]),
        name=str(item.get("name", "")),
        security_id=str(item["security_id"]),
        cik=cik,
        gics_sector=str(item["gics_sector"]) if item.get("gics_sector") else None,
        gics_sub_industry=(
            str(item["gics_sub_industry"]) if item.get("gics_sub_industry") else None
        ),
        date_added=date.fromisoformat(str(date_added)) if date_added else None,
        identity_resolved=bool(item.get("identity_resolved", cik is not None)),
    )


def load_snapshot(path: Path) -> ConstituentSnapshot:
    """Load a snapshot, RECOMPUTING its checksum."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SourceAdapterError(f"unreadable universe snapshot {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SourceAdapterError("universe snapshot must be a JSON object")
    missing = {"index_id", "as_of", "fetched_at", "source_id"} - payload.keys()
    if missing:
        # Not a snapshot at all (e.g. the maintenance state file living
        # alongside). Reported as a source error so the store can skip it.
        raise SourceAdapterError(
            f"{path} is not a universe snapshot; missing {sorted(missing)}"
        )
    declared = payload.get("checksum")
    snapshot = ConstituentSnapshot(
        index_id=str(payload["index_id"]),
        as_of=date.fromisoformat(str(payload["as_of"])),
        fetched_at=datetime.fromisoformat(str(payload["fetched_at"])),
        source_id=str(payload["source_id"]),
        source_authority=str(payload.get("source_authority", SourceAuthority.PUBLIC_SECONDARY)),
        constituents=tuple(
            _constituent_from_dict(item) for item in payload.get("constituents", ())
        ),
        is_point_in_time_history=bool(payload.get("is_point_in_time_history", False)),
        gics_mode=str(payload.get("gics_mode", GicsMode.FREE_RECONSTRUCTED.value)),
        notes=tuple(str(note) for note in payload.get("notes", ())),
    )
    recomputed = snapshot.compute_checksum()
    if isinstance(declared, str) and declared and declared != recomputed:
        raise SourceAdapterError(
            f"{path}: universe snapshot checksum mismatch (tampered or stale):"
            f" declared {declared}, recomputed {recomputed}"
        )
    return _with_checksum(snapshot)


# -- free constituent dataset (approved PUBLIC_SECONDARY source) --------------

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HEADER_CELL_RE = re.compile(r"<th[\s>]", re.IGNORECASE)


def _clean(cell: str) -> str:
    text = _TAG_RE.sub(" ", cell)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#160;", " ")
    return " ".join(text.split())


def parse_constituent_table(
    raw: bytes,
    *,
    index_id: str = "SP500",
    as_of: date,
    fetched_at: datetime,
    source_id: str,
) -> ConstituentSnapshot:
    """Parse a free public constituent table into a CURRENT snapshot.

    Marked ``PUBLIC_SECONDARY`` and ``is_point_in_time_history=False``: it is an
    operational universe filter, never historical evidence. Rows missing a
    ticker are skipped and reported rather than invented.
    """
    text = raw.decode("utf-8", errors="replace")
    constituents: list[Constituent] = []
    skipped = 0
    for row in _ROW_RE.findall(text):
        # A header row is not a constituent. "Symbol" happens to match the
        # ticker shape, so headers must be excluded structurally rather than by
        # hoping the pattern rejects them.
        if _HEADER_CELL_RE.search(row):
            continue
        cells = [_clean(cell) for cell in _CELL_RE.findall(row)]
        if len(cells) < 2:
            continue
        ticker = cells[0].upper()
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,6}", ticker):
            skipped += 1
            continue
        cik = next((c for c in cells if re.fullmatch(r"\d{7,10}", c)), None)
        added = next(
            (c for c in cells if re.fullmatch(r"\d{4}-\d{2}-\d{2}", c)), None
        )
        constituents.append(
            Constituent(
                ticker=ticker,
                name=cells[1],
                # A stable, non-ticker identity: CIK when the source carries it.
                security_id=(
                    f"sec-cik:{cik.zfill(10)}" if cik else f"unresolved-ticker:{ticker}"
                ),
                identity_resolved=cik is not None,
                cik=cik.zfill(10) if cik else None,
                gics_sector=cells[2] if len(cells) > 2 and cells[2] else None,
                gics_sub_industry=cells[3] if len(cells) > 3 and cells[3] else None,
                date_added=date.fromisoformat(added) if added else None,
            )
        )
    notes = [
        "PUBLIC_SECONDARY free dataset: CURRENT membership only.",
        "This is NOT point-in-time history and must never be used as such.",
        "Sector context here is CURRENT classification (FREE_RECONSTRUCTED).",
        "A constituent without a CIK carries an UNRESOLVED identity: the ticker"
        " is operational routing only and is never a stable security_id.",
    ]
    if skipped:
        notes.append(f"{skipped} row(s) skipped: no parseable ticker")
    return _with_checksum(
        ConstituentSnapshot(
            index_id=index_id, as_of=as_of, fetched_at=fetched_at, source_id=source_id,
            source_authority=SourceAuthority.PUBLIC_SECONDARY.value,
            constituents=tuple(constituents), notes=tuple(notes),
        )
    )


def refresh_universe(
    store: UniverseStore,
    candidate: ConstituentSnapshot | RawConstituentRefresh | None,
    *,
    now: datetime,
    policy: FreshnessPolicy,
    allow_normalized_only: bool = False,
) -> tuple[ConstituentSnapshot | None, UniverseListStatus, tuple[str, ...]]:
    """Apply a refresh safely and report the resulting freshness.

    A failed (``None``) or invalid candidate leaves the last-known-good snapshot
    in place; it is never overwritten by a bad fetch.
    """
    findings: list[str] = []
    if candidate is None:
        findings.append("refresh produced no snapshot; keeping the last-known-good list")
    else:
        raw_bytes: bytes | None = None
        problems: tuple[str, ...] | None = None
        if isinstance(candidate, RawConstituentRefresh):
            raw_bytes = candidate.raw_bytes
            if (
                candidate.snapshot.checksum
                and candidate.snapshot.checksum != candidate.snapshot.compute_checksum()
            ):
                candidate = candidate.snapshot
                problems = (
                    "refresh candidate checksum does not authenticate before raw"
                    " binding; refusing to archive",
                )
                raw_bytes = None
            else:
                candidate = (
                    candidate.snapshot
                    if recorded_raw_checksum(candidate.snapshot) is not None
                    else bind_raw_source(candidate.snapshot, raw_artifact_checksum(raw_bytes))
                )
        if raw_bytes is not None:
            _, problems = store.accept_with_raw_artifact(candidate, raw_bytes)
        elif problems is None:
            if allow_normalized_only and store.allow_normalized_only:
                _, problems = store.accept(candidate)
            elif allow_normalized_only:
                problems = (
                    "normalized-only refresh requested but this store was not"
                    " constructed with allow_normalized_only=True",
                )
            else:
                problems = (
                    "refresh candidate lacks retained raw bytes; production monthly"
                    " archive requires RawConstituentRefresh/raw-bound provenance",
                )
        if problems:
            findings.append(
                "refresh rejected, last-known-good retained: " + "; ".join(problems)
            )
            # A rejection that only says "snapshot is empty" is safe and
            # undiagnosable; the parse already knows what it saw, so say it.
            findings.extend(
                f"diagnostic: {line}" for line in diagnostic_notes(candidate)
            )
    current = store.latest()
    return current, classify_freshness(current, now=now, policy=policy), tuple(findings)


def ensure_current_universe(
    store: UniverseStore,
    *,
    now: datetime,
    policy: FreshnessPolicy,
    fetch: Callable[[], ConstituentSnapshot | RawConstituentRefresh] | None = None,
) -> tuple[ConstituentSnapshot | None, UniverseListStatus, tuple[str, ...]]:
    """Fresh? use the cache. Due? refresh. Failed? keep the last-known-good.

    Cheap by design so it can run at startup or universe initialisation rather
    than on every trade: when the stored snapshot is inside the monthly
    interval this performs NO fetch at all. The owner never has to remember a
    manual monthly command; the CLI stays available for diagnostics and a
    forced refresh.
    """
    if not store.due_for_refresh(now=now):
        current = store.latest()
        return current, classify_freshness(current, now=now, policy=policy), (
            "refresh not due; using the stored snapshot",
        )
    if fetch is None:
        current = store.latest()
        return current, classify_freshness(current, now=now, policy=policy), (
            "refresh is due but no fetcher was supplied; keeping the"
            " last-known-good list",
        )
    candidate: ConstituentSnapshot | RawConstituentRefresh | None = None
    findings: list[str] = []
    try:
        candidate = fetch()
    except SourceAdapterError as error:
        findings.append(f"refresh fetch failed: {error}")
    current, status, refresh_findings = refresh_universe(
        store, candidate, now=now, policy=policy
    )
    return current, status, tuple(findings) + refresh_findings


def sector_context(
    snapshot: ConstituentSnapshot | None, ticker: str, *, gics_mode: GicsMode
) -> tuple[str | None, str]:
    """Optional sector context and its provenance. UNKNOWN is always safe."""
    if gics_mode is GicsMode.DISABLED:
        return None, "GICS is DISABLED; sector is not consulted"
    if snapshot is None:
        return None, "UNKNOWN: no current universe snapshot"
    sector = snapshot.sector_of(ticker)
    if sector is None:
        return None, f"UNKNOWN: {ticker} carries no sector in the current snapshot"
    return sector, (
        "FREE_RECONSTRUCTED: current classification from a PUBLIC_SECONDARY"
        " dataset; non-authoritative and not point-in-time"
    )


__all__ = [
    "parse_wikitext_constituents",
    "fetch_current_constituents",
    "fetch_current_constituents_with_raw",
    "RawConstituentRefresh",
    "DIAGNOSTIC_NOTE_PREFIX",
    "RAW_CHECKSUM_NOTE_PREFIX",
    "WIKIPEDIA_API_URL",
    "WIKIPEDIA_HTML_API_URL",
    "FREE_SOURCE_ID",
    "DEFAULT_USER_AGENT",
    "MAX_PLAUSIBLE_CONSTITUENTS",
    "MIN_PLAUSIBLE_CONSTITUENTS",
    "REFRESH_INTERVAL_DAYS",
    "UNIVERSE_SCHEMA_VERSION",
    "Constituent",
    "ConstituentSnapshot",
    "UniverseStore",
    "bind_raw_source",
    "classify_freshness",
    "ensure_current_universe",
    "load_snapshot",
    "diagnostic_notes",
    "parse_constituent_table",
    "parse_html_constituents",
    "raw_artifact_checksum",
    "recorded_raw_checksum",
    "refresh_universe",
    "sector_context",
    "validate_snapshot",
]


# -- the REAL free refresh path (owner host only; never exercised in CI) -------


#: Wikipedia's action API is a stable, documented, versioned endpoint. Parsing
#: its returned wikitext beats scraping a rendered page, which changes shape
#: without notice. Marked PUBLIC_SECONDARY: operational universe, never history.
WIKIPEDIA_API_URL = (
    "https://en.wikipedia.org/w/api.php"
    "?action=parse&page=List_of_S%26P_500_companies&prop=wikitext&format=json"
    "&formatversion=2"
)

#: The SAME page through MediaWiki's other documented representation: its own
#: rendered HTML. Used only as a fallback, and deliberately not a second source
#: -- same endpoint, same page, same PUBLIC_SECONDARY provenance, same
#: CURRENT-only meaning. Nothing here scrapes an unrelated site.
WIKIPEDIA_HTML_API_URL = (
    "https://en.wikipedia.org/w/api.php"
    "?action=parse&page=List_of_S%26P_500_companies&prop=text&format=json"
    "&formatversion=2"
)
FREE_SOURCE_ID = "wikipedia:List_of_S&P_500_companies"

#: Sent so the source can identify us, per normal API etiquette. It carries no
#: credential and no personal data.
DEFAULT_USER_AGENT = "TradingSystem-DatasetV1/1.0 (current-universe refresh; contact via repo)"


#: Diagnostic notes carry this prefix so a rejected refresh can surface them
#: without the caller having to know anything about parsing.
DIAGNOSTIC_NOTE_PREFIX = "parse diagnostic: "

_CIK_DIGITS_RE = re.compile(r"\d+")


def diagnostic_notes(snapshot: ConstituentSnapshot) -> tuple[str, ...]:
    """The parse diagnostics carried by a snapshot, if any."""
    return tuple(
        note[len(DIAGNOSTIC_NOTE_PREFIX) :]
        for note in snapshot.notes
        if note.startswith(DIAGNOSTIC_NOTE_PREFIX)
    )


def _normalized_cik(text: str | None) -> str | None:
    """The source writes CIKs zero-padded, bare, or not at all.

    Anything that is not a plausible CIK becomes None, which keeps the identity
    UNRESOLVED. A ticker is never promoted to a stable identity to fill the gap.
    """
    if not text:
        return None
    digits = "".join(_CIK_DIGITS_RE.findall(text))
    if not digits or len(digits) > 10:
        return None
    return digits.zfill(10)


def _parsed_date(text: str | None) -> date | None:
    if not text:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(0))
    except ValueError:
        return None


def _snapshot_from_rows(
    rows: tuple[ConstituentRow, ...],
    diagnostics: ParseDiagnostics,
    *,
    index_id: str,
    as_of: date,
    fetched_at: datetime,
    source_id: str,
) -> ConstituentSnapshot:
    """Turn parsed rows into a snapshot, making the identity decision here.

    The parser deals in text; this is where a CIK becomes a stable SEC identity
    and its absence becomes an explicitly unresolved one.
    """
    constituents = tuple(
        Constituent(
            ticker=row.ticker,
            name=row.name,
            security_id=(
                f"sec-cik:{cik}" if (cik := _normalized_cik(row.cik))
                else f"unresolved-ticker:{row.ticker}"
            ),
            identity_resolved=_normalized_cik(row.cik) is not None,
            cik=_normalized_cik(row.cik),
            gics_sector=row.gics_sector,
            gics_sub_industry=row.gics_sub_industry,
            date_added=_parsed_date(row.date_added),
        )
        for row in rows
    )
    notes = [
        "PUBLIC_SECONDARY free dataset: CURRENT membership only.",
        "This is NOT point-in-time history and must never be used as such.",
        "Sector context here is CURRENT classification (FREE_RECONSTRUCTED).",
        "A constituent without a CIK carries an UNRESOLVED identity: the ticker"
        " is operational routing only and is never a stable security_id.",
    ]
    # Recorded on every parse, not only on failure: knowing which representation
    # and which schema produced a stored list is what makes the next upstream
    # format change diagnosable from the snapshot itself.
    notes.extend(DIAGNOSTIC_NOTE_PREFIX + line for line in diagnostics.lines())
    return _with_checksum(
        ConstituentSnapshot(
            index_id=index_id, as_of=as_of, fetched_at=fetched_at, source_id=source_id,
            source_authority=SourceAuthority.PUBLIC_SECONDARY.value,
            constituents=constituents, notes=tuple(notes),
        )
    )


def _mediawiki_payload(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SourceAdapterError(f"constituent source did not return JSON: {error}") from error
    if not isinstance(payload, dict):
        raise SourceAdapterError("constituent payload must be a JSON object")
    parse = payload.get("parse")
    if not isinstance(parse, dict):
        raise SourceAdapterError("constituent payload carries no MediaWiki parse result")
    return parse


def parse_wikitext_constituents(
    raw: bytes,
    *,
    index_id: str = "SP500",
    as_of: date,
    fetched_at: datetime,
    source_id: str = FREE_SOURCE_ID,
) -> ConstituentSnapshot:
    """Parse the constituent table out of the API's wikitext payload.

    Rejects an unexpected payload rather than returning a thin snapshot that
    might later overwrite a good list. The table is read through its own header
    row and its cells through a nesting-aware scanner, so a ticker wrapped in an
    exchange template (``{{NasdaqSymbol|AAPL}}``) is read as a ticker rather
    than torn in half -- the defect an owner-host run surfaced as an empty
    snapshot.
    """
    parse = _mediawiki_payload(raw)
    wikitext = parse.get("wikitext")
    if isinstance(wikitext, dict):  # formatversion=1 wraps the text
        wikitext = wikitext.get("*")
    if not isinstance(wikitext, str) or "{|" not in wikitext:
        raise SourceAdapterError(
            "constituent payload carries no wikitext table; refusing to parse"
        )
    try:
        rows, diagnostics = parse_wikitext_table(wikitext)
    except TableParseError as error:
        raise SourceAdapterError(f"constituent wikitext is unreadable: {error}") from error
    return _snapshot_from_rows(
        rows, diagnostics, index_id=index_id, as_of=as_of,
        fetched_at=fetched_at, source_id=source_id,
    )


def parse_html_constituents(
    raw: bytes,
    *,
    index_id: str = "SP500",
    as_of: date,
    fetched_at: datetime,
    source_id: str = FREE_SOURCE_ID,
) -> ConstituentSnapshot:
    """Parse the SAME page from MediaWiki's own rendered HTML.

    Second documented representation of one page, not a second source: same
    endpoint, same provenance, same CURRENT-only meaning. MediaWiki has already
    expanded the ticker templates here, which is exactly why this path survives
    the template drift that broke the wikitext parse.
    """
    parse = _mediawiki_payload(raw)
    text = parse.get("text")
    if isinstance(text, dict):  # formatversion=1 wraps the markup
        text = text.get("*")
    if not isinstance(text, str) or "<table" not in text.lower():
        raise SourceAdapterError(
            "constituent payload carries no rendered HTML table; refusing to parse"
        )
    try:
        rows, diagnostics = parse_html_table(text)
    except TableParseError as error:
        raise SourceAdapterError(f"constituent HTML is unreadable: {error}") from error
    return _snapshot_from_rows(
        rows, diagnostics, index_id=index_id, as_of=as_of,
        fetched_at=fetched_at, source_id=source_id,
    )


def _fetch_json(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
    """One validated HTTPS GET. Shape is checked before anything is parsed."""
    import urllib.error
    import urllib.request

    if not url.lower().startswith("https://"):
        raise SourceAdapterError("the constituent source must be fetched over HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            status = int(response.status)
            content_type = str(response.headers.get("Content-Type", ""))
            raw = bytes(response.read())
    except urllib.error.HTTPError as error:
        raise SourceAdapterError(f"constituent source returned HTTP {error.code}") from error
    except OSError as error:
        raise SourceAdapterError(f"constituent source unreachable: {error}") from error
    if status != 200:
        raise SourceAdapterError(f"constituent source returned HTTP {status}")
    if "json" not in content_type.lower():
        raise SourceAdapterError(
            f"unexpected content type {content_type!r}; refusing to parse"
        )
    return raw


def fetch_current_constituents_with_raw(
    *,
    url: str = WIKIPEDIA_API_URL,
    html_url: str | None = WIKIPEDIA_HTML_API_URL,
    timeout_seconds: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
    as_of: date | None = None,
    now: datetime | None = None,
) -> RawConstituentRefresh:
    """Fetch the free CURRENT constituent list over HTTPS with retained raw bytes.

    Owner-host only; unit tests parse recorded payloads and never call this.
    Content type and payload shape are validated before parsing, so a captive
    portal or error page cannot masquerade as an index.

    Two representations of ONE page, in order:

    1. **wikitext** -- the article's own source. Exact, and what we parse first.
    2. **rendered HTML** -- MediaWiki's other documented representation of the
       same page, tried only if the first yields an implausible list.

    The second exists because of how the first failed empirically: the article
    changed how it wraps ticker cells, and wikitext exposes that change while
    rendered HTML does not (MediaWiki has already expanded the templates). The
    fallback is a different *view* of the same source, never a different source,
    so provenance, authority and CURRENT-only semantics are identical either
    way. Passing ``html_url=None`` disables it.
    """
    fetched_at = now or datetime.now(tz=UTC)
    stamp = as_of or fetched_at.date()
    raw = _fetch_json(url, timeout_seconds=timeout_seconds, user_agent=user_agent)
    snapshot = parse_wikitext_constituents(raw, as_of=stamp, fetched_at=fetched_at)
    if html_url is None or not validate_snapshot(snapshot):
        return RawConstituentRefresh(snapshot=snapshot, raw_bytes=raw)

    # The primary view produced something a validator would refuse. Rather than
    # store nothing, ask the same page for its other representation.
    try:
        fallback_raw = _fetch_json(
            html_url, timeout_seconds=timeout_seconds, user_agent=user_agent
        )
        fallback = parse_html_constituents(fallback_raw, as_of=stamp, fetched_at=fetched_at)
    except SourceAdapterError:
        # The fallback failing must never hide what the primary said.
        return RawConstituentRefresh(snapshot=snapshot, raw_bytes=raw)
    if validate_snapshot(fallback):
        # Both views are unusable. Return the primary, whose diagnostics
        # describe the representation that is actually authoritative.
        return RawConstituentRefresh(snapshot=snapshot, raw_bytes=raw)
    return RawConstituentRefresh(snapshot=fallback, raw_bytes=fallback_raw)


def fetch_current_constituents(**kwargs) -> ConstituentSnapshot:
    """Fetch the free CURRENT constituent list, returning only the parsed snapshot.

    Kept for compatibility. Production refresh/archive callers must use
    :func:`fetch_current_constituents_with_raw` so the monthly archive can retain
    and re-verify the exact raw source artifact.
    """
    return fetch_current_constituents_with_raw(**kwargs).snapshot
