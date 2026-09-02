"""A-WP2 — fail-closed secondary historical S&P 500 PIT source registry.

Authority: CLAUDE.md ("Preserve point-in-time semantics and prevent
look-ahead"; "no substitute for historical GICS"; "never claim integration
evidence") and ADR-0017 (historical S&P membership and GICS are optional
context, never authoritative, never a trading gate).

``config/data-sources/sp500-historical-source-registry.json`` is a purely
OFFLINE, hand-authored provenance registry of candidate SECONDARY sources for
historical (point-in-time) S&P 500 constituent data. It fetches nothing: no
network call was made to produce it, and no test here makes one either.

This module is both:

* a set of assertions on the actual registry file (it says what it must say,
  and only what it is allowed to say), and
* a small fail-closed validator (``validate_registry``) exercised against
  deliberately tampered copies of the payload, proving the invariants are
  actually enforced and not just true by accident of the current file
  contents.

Nothing here fetches fja05680/sp500 or any other external source, bypasses an
access control, touches PR #101/#102 dataset-manifest machinery, alters a
strategy parameter, or claims empirical/integration evidence: the registry's
own ``status`` is ``NOT_INTEGRATED`` and ``integration_evidence_claim`` is
``NONE``, and this file asserts that, it does not override it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard, cast

import pytest

REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "data-sources"
    / "sp500-historical-source-registry.json"
)

ALLOWED_CLASSIFICATIONS = frozenset({"SECONDARY_RESEARCH_PIT", "NOT_FOUND_OR_COMMERCIAL_ONLY"})
ALLOWED_UNAVAILABILITY_REASONS = frozenset(
    {"NOT_FOUND", "AMBIGUOUS_PROVENANCE", "COMMERCIAL_LICENSE_REQUIRED"}
)
ELIGIBILITY_FLAGS = (
    "authoritative_official_evidence",
    "eligible_for_promotion_without_validation",
    "substitute_for_historical_gics",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Claims this registry must never make about itself (it is a provenance
# ledger, not proof that anything was ingested, verified or traded on).
FORBIDDEN_CLAIM_STRINGS = (
    "EMPIRICALLY_VERIFIED",
    "REAL_DATASET_BUILT",
    "LIVE_READY",
    "PAPER_AUTO",
)


class RegistryError(ValueError):
    """Fail-closed registry validation failure."""


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    if not isinstance(payload, dict):
        raise RegistryError("registry file must contain a JSON object at the top level")
    return payload


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistryError(message)


def _is_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _expect_dict(value: object, where: str) -> dict[str, Any]:
    if not _is_dict(value):
        raise RegistryError(f"{where}: object is required")
    return value


def _validate_eligibility(eligibility: object, where: str) -> None:
    checked = _expect_dict(eligibility, f"{where}: eligibility")
    for flag in ELIGIBILITY_FLAGS:
        _require(flag in checked, f"{where}: eligibility missing {flag!r}")
        _require(checked[flag] is False, f"{where}: eligibility.{flag} must be False")


def _parse_iso8601(value: object, where: str) -> datetime:
    """Parse a real, timezone-AWARE ISO-8601 instant and normalize it to UTC.
    Fails closed on anything else, including a missing/null value (VERIFIED
    timestamps must be recorded, not implied) and a naive timestamp: a PIT
    instant whose ordering depends on a later, unspecified timezone
    interpretation is not a fixed point on the timeline and can silently
    invalidate the no-look-ahead boundary (CLAUDE.md point-in-time semantics)."""
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{where}: must be a non-empty ISO-8601 timestamp string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RegistryError(f"{where}: {value!r} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RegistryError(
            f"{where}: {value!r} must be timezone-aware (no naive clocks) - "
            "a PIT instant must not depend on an unspecified later timezone"
        )
    return parsed.astimezone(UTC)


def _validate_bound_artifact(candidate: dict[str, Any], checksum: str, where: str) -> None:
    """A VERIFIED checksum must be bound to real bytes on disk, re-derived here
    - never trusted from the label alone (this closes the exact gap a review
    flagged: an all-zero digest with no artifact behind it was previously
    accepted as 'genuinely verified')."""
    artifact_path = candidate.get("artifact_path")
    _require(
        isinstance(artifact_path, str) and artifact_path.strip() != "",
        f"{where}: checksum_status VERIFIED requires a non-empty artifact_path",
    )
    assert isinstance(artifact_path, str)
    path = Path(artifact_path)
    _require(
        path.is_absolute(),
        f"{where}: artifact_path must be an absolute path, not {artifact_path!r}",
    )
    _require(path.is_file(), f"{where}: artifact_path {artifact_path!r} does not exist")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _require(
        digest == checksum.lower(),
        f"{where}: checksum_sha256 does not match the recomputed digest of artifact_path bytes",
    )


def _validate_verified_timestamps(timestamps: dict[str, Any], where: str) -> None:
    effective = _parse_iso8601(timestamps.get("effective_at"), f"{where}: timestamps.effective_at")
    observed = _parse_iso8601(timestamps.get("observed_at"), f"{where}: timestamps.observed_at")
    available = _parse_iso8601(timestamps.get("available_at"), f"{where}: timestamps.available_at")
    _require(
        effective <= observed <= available,
        f"{where}: timestamps must satisfy effective_at <= observed_at <= available_at",
    )


def _validate_passed_cross_check(
    candidate: dict[str, Any], cross_check: dict[str, Any], minimum: int, where: str
) -> None:
    """A PASSED cross-check must name real, distinct, independent sources -
    never the candidate's own URL, never duplicates padding out the count."""
    _require(
        cross_check.get("status") == "PASSED",
        f"{where}: cross_check.status must be PASSED",
    )
    corroborating = cross_check.get("corroborating_sources")
    _require(
        isinstance(corroborating, list),
        f"{where}: cross_check.corroborating_sources must be a list",
    )
    assert isinstance(corroborating, list)
    cleaned = [s.strip() for s in corroborating if isinstance(s, str) and s.strip()]
    _require(
        len(cleaned) == len(corroborating),
        f"{where}: cross_check.corroborating_sources must all be non-empty strings",
    )
    distinct = set(cleaned)
    _require(
        len(distinct) >= minimum,
        f"{where}: cross_check needs at least {minimum} distinct corroborating sources",
    )
    own_url = candidate.get("source_url")
    _require(
        own_url not in distinct,
        f"{where}: a corroborating source cannot be the candidate's own source_url",
    )


def _validate_secondary_research_pit(candidate: dict[str, Any], where: str) -> None:
    _require(
        candidate.get("source_authority") == "PUBLIC_SECONDARY",
        f"{where}: SECONDARY_RESEARCH_PIT must declare source_authority PUBLIC_SECONDARY",
    )
    timestamps = _expect_dict(candidate.get("timestamps"), f"{where}: timestamps")
    for key in ("effective_at", "observed_at", "available_at"):
        _require(key in timestamps, f"{where}: timestamps.{key} slot is required")
        status_key = f"{key}_status"
        _require(status_key in timestamps, f"{where}: timestamps.{status_key} is required")

    cross_check = _expect_dict(candidate.get("cross_check"), f"{where}: cross_check")
    _require(
        cross_check.get("required") is True,
        f"{where}: SECONDARY_RESEARCH_PIT must require cross-check",
    )
    minimum = cross_check.get("minimum_independent_corroborating_sources", 0)
    _require(
        isinstance(minimum, int) and minimum >= 2,
        f"{where}: cross_check.minimum_independent_corroborating_sources must be >= 2",
    )

    checksum = candidate.get("checksum_sha256")
    checksum_status = candidate.get("checksum_status")
    if checksum_status == "VERIFIED":
        # Reaching VERIFIED requires ALL FOUR legs of evidence at once, each
        # independently re-derived by this validator - never accepted from a
        # label. A real-shaped checksum with nothing bound behind it (the
        # gap a prior review flagged) is rejected by _validate_bound_artifact.
        is_real_sha256 = isinstance(checksum, str) and SHA256_RE.match(checksum) is not None
        _require(
            is_real_sha256,
            f"{where}: checksum_status VERIFIED requires a real 64-hex sha256, not {checksum!r}",
        )
        assert isinstance(checksum, str)
        _validate_bound_artifact(candidate, checksum, where)
        _validate_verified_timestamps(timestamps, where)
        _validate_passed_cross_check(candidate, cross_check, minimum, where)
    else:
        _require(
            checksum is None,
            f"{where}: checksum_sha256 must be null until checksum_status is VERIFIED",
        )
        _require(
            candidate.get("artifact_path") is None,
            f"{where}: artifact_path must be null until checksum_status is VERIFIED",
        )
        for key in ("effective_at", "observed_at", "available_at"):
            _require(
                timestamps.get(key) is None,
                f"{where}: timestamps.{key} must be null until checksum_status is VERIFIED",
            )
        _require(
            cross_check.get("status") != "PASSED",
            f"{where}: cross_check.status cannot be PASSED before checksum_status is VERIFIED",
        )


def _validate_not_found_or_commercial_only(candidate: dict[str, Any], where: str) -> None:
    reason = candidate.get("unavailability_reason")
    _require(
        reason in ALLOWED_UNAVAILABILITY_REASONS,
        f"{where}: unavailability_reason {reason!r} is not allowed",
    )
    _require(
        candidate.get("checksum_sha256") is None,
        f"{where}: NOT_FOUND_OR_COMMERCIAL_ONLY must not carry a checksum",
    )
    cross_check = _expect_dict(candidate.get("cross_check"), f"{where}: cross_check")
    _require(
        cross_check.get("required") is False,
        f"{where}: NOT_FOUND_OR_COMMERCIAL_ONLY must not require cross-check",
    )


def validate_registry(data: dict[str, Any]) -> None:
    """Fail-closed structural + invariant validation of the registry payload.

    Raises ``RegistryError`` the moment any invariant is violated. This is the
    same function exercised against both the real, committed file (must pass)
    and deliberately tampered copies (must raise) below.
    """
    _require(
        data.get("network_fetch_performed_by_this_registry") is False,
        "registry: must not fetch",
    )
    _require(data.get("status") == "NOT_INTEGRATED", "registry: status must be NOT_INTEGRATED")
    _require(
        data.get("integration_evidence_claim") == "NONE",
        "registry: integration_evidence_claim must be NONE",
    )

    invariants = _expect_dict(data.get("invariants"), "registry: invariants")
    for key in (
        "no_candidate_is_authoritative_official_evidence",
        "no_candidate_is_eligible_for_promotion_without_validation",
        "no_candidate_substitutes_for_historical_gics",
    ):
        _require(invariants.get(key) is True, f"registry: invariants.{key} must be True")

    classifications = _expect_dict(data.get("classifications"), "registry: classifications")
    _require(
        set(classifications.keys()) == set(ALLOWED_CLASSIFICATIONS),
        f"registry: classifications keys must be exactly {sorted(ALLOWED_CLASSIFICATIONS)}",
    )
    for name, spec in classifications.items():
        spec_dict = _expect_dict(spec, f"classifications.{name}")
        _validate_eligibility(spec_dict.get("eligibility"), f"classifications.{name}")

    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise RegistryError("registry: candidates must be a list")
    _require(len(candidates) > 0, "registry: candidates must be a non-empty list")

    seen_ids: set[str] = set()
    for index, raw_candidate in enumerate(candidates):
        candidate = _expect_dict(raw_candidate, f"candidates[{index}]")
        where = f"candidates[{index}] ({candidate.get('candidate_id', '?')})"
        candidate_id = candidate.get("candidate_id")
        is_valid_id = isinstance(candidate_id, str) and bool(candidate_id.strip())
        _require(is_valid_id, f"{where}: candidate_id is mandatory")
        assert isinstance(candidate_id, str)  # narrowed by is_valid_id above
        _require(candidate_id not in seen_ids, f"{where}: duplicate candidate_id")
        seen_ids.add(candidate_id)

        source_name = candidate.get("source_name")
        has_source_name = isinstance(source_name, str) and bool(source_name.strip())
        _require(has_source_name, f"{where}: source_name is mandatory")

        classification = candidate.get("classification")
        _require(
            classification in ALLOWED_CLASSIFICATIONS,
            f"{where}: classification {classification!r} is not recognised",
        )

        _validate_eligibility(candidate.get("eligibility"), where)

        if classification == "SECONDARY_RESEARCH_PIT":
            _validate_secondary_research_pit(candidate, where)
        else:
            _validate_not_found_or_commercial_only(candidate, where)


# --------------------------------------------------------------------------
# 1. The registry file itself: valid JSON, loads, matches the schema shape.
# --------------------------------------------------------------------------


def test_registry_file_is_valid_json_and_loads() -> None:
    data = load_registry()
    assert isinstance(data, dict)
    assert data["registry_id"] == "sp500-historical-source-registry"
    assert isinstance(data["candidates"], list) and len(data["candidates"]) >= 1


def test_registry_never_fetched_anything_and_claims_no_integration() -> None:
    data = load_registry()
    assert data["network_fetch_performed_by_this_registry"] is False
    assert data["status"] == "NOT_INTEGRATED"
    assert data["integration_evidence_claim"] == "NONE"


def test_registry_passes_its_own_fail_closed_validator() -> None:
    """The committed file must be clean by construction."""
    validate_registry(load_registry())


def test_registry_never_makes_a_forbidden_claim_anywhere() -> None:
    raw_text = REGISTRY_PATH.read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_CLAIM_STRINGS:
        assert forbidden not in raw_text, f"registry must never contain the claim {forbidden!r}"


# --------------------------------------------------------------------------
# 2. Classification-level invariants: both allowed classifications, and only
#    those, each permanently ineligible in every respect.
# --------------------------------------------------------------------------


def test_classifications_are_exactly_the_two_allowed() -> None:
    data = load_registry()
    assert set(data["classifications"].keys()) == ALLOWED_CLASSIFICATIONS


@pytest.mark.parametrize("classification_name", sorted(ALLOWED_CLASSIFICATIONS))
def test_classification_eligibility_is_always_false(classification_name: str) -> None:
    data = load_registry()
    eligibility = data["classifications"][classification_name]["eligibility"]
    for flag in ELIGIBILITY_FLAGS:
        assert eligibility[flag] is False


# --------------------------------------------------------------------------
# 3. fja05680/sp500 is classified SECONDARY_RESEARCH_PIT with full provenance
#    scaffolding required (checksum + three timestamps + cross-check).
# --------------------------------------------------------------------------


def _candidate(data: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    matches = [c for c in data["candidates"] if c["candidate_id"] == candidate_id]
    assert len(matches) == 1, f"expected exactly one candidate {candidate_id!r}"
    return cast(dict[str, Any], matches[0])


def test_fja05680_is_classified_secondary_research_pit() -> None:
    data = load_registry()
    candidate = _candidate(data, "fja05680-sp500-github")
    assert candidate["classification"] == "SECONDARY_RESEARCH_PIT"
    assert "fja05680/sp500" in candidate["source_url"]
    assert candidate["source_authority"] == "PUBLIC_SECONDARY"


def test_fja05680_requires_checksum_and_three_timestamps_and_cross_check() -> None:
    data = load_registry()
    candidate = _candidate(data, "fja05680-sp500-github")

    # Not yet computed/performed - this registry never fetched the source.
    assert candidate["checksum_sha256"] is None
    assert candidate["checksum_status"] == "NOT_YET_COMPUTED"
    assert candidate["artifact_path"] is None

    timestamps = candidate["timestamps"]
    for key in ("effective_at", "observed_at", "available_at"):
        assert key in timestamps
        assert timestamps[f"{key}_status"] == "REQUIRED_NOT_YET_RECORDED"

    cross_check = candidate["cross_check"]
    assert cross_check["required"] is True
    assert cross_check["status"] == "NOT_PERFORMED"
    assert cross_check["minimum_independent_corroborating_sources"] >= 2
    assert cross_check["corroborating_sources"] == []


def test_fja05680_is_never_eligible_for_anything() -> None:
    data = load_registry()
    candidate = _candidate(data, "fja05680-sp500-github")
    for flag in ELIGIBILITY_FLAGS:
        assert candidate["eligibility"][flag] is False


# --------------------------------------------------------------------------
# 4. Unavailable/ambiguous candidates are NOT_FOUND_OR_COMMERCIAL_ONLY, with
#    no checksum to verify and no cross-check to perform.
# --------------------------------------------------------------------------


def test_unavailable_or_ambiguous_candidates_are_not_found_or_commercial_only() -> None:
    data = load_registry()
    not_found = [
        c for c in data["candidates"] if c["classification"] == "NOT_FOUND_OR_COMMERCIAL_ONLY"
    ]
    assert len(not_found) >= 1
    for candidate in not_found:
        assert candidate["unavailability_reason"] in ALLOWED_UNAVAILABILITY_REASONS
        assert candidate["checksum_sha256"] is None
        assert candidate["cross_check"]["required"] is False


def test_every_candidate_id_is_unique() -> None:
    data = load_registry()
    ids = [c["candidate_id"] for c in data["candidates"]]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# 5. Absolute invariants, on every candidate, regardless of classification:
#    never authoritative, never promotable without validation, never a GICS
#    substitute. Checked both on the real file and enforced by the validator.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ELIGIBILITY_FLAGS)
def test_no_candidate_is_ever_eligible(flag: str) -> None:
    data = load_registry()
    for candidate in data["candidates"]:
        assert candidate["eligibility"][flag] is False, (
            f"candidate {candidate['candidate_id']!r} must never set {flag}"
        )


# --------------------------------------------------------------------------
# 6. The validator is genuinely fail-closed: tampered copies are rejected,
#    not just described as invalid in prose.
# --------------------------------------------------------------------------


def _tampered(mutator: Any) -> dict[str, Any]:
    data = copy.deepcopy(load_registry())
    mutator(data)
    return data


def test_validator_rejects_authoritative_flag_flipped_true() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"][0]["eligibility"]["authoritative_official_evidence"] = True

    with pytest.raises(RegistryError, match="authoritative_official_evidence"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_promotion_without_validation_flipped_true() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"][0]["eligibility"]["eligible_for_promotion_without_validation"] = True

    with pytest.raises(RegistryError, match="eligible_for_promotion_without_validation"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_gics_substitute_flipped_true() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"][0]["eligibility"]["substitute_for_historical_gics"] = True

    with pytest.raises(RegistryError, match="substitute_for_historical_gics"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_unknown_classification() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"][0]["classification"] = "OFFICIAL_CONFIRMED"

    with pytest.raises(RegistryError, match="not recognised"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_checksum_without_real_sha256() -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate["checksum_status"] = "VERIFIED"
        # checksum_sha256 left null - claiming VERIFIED with nothing behind it.

    with pytest.raises(RegistryError, match="VERIFIED requires a real 64-hex sha256"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_bogus_checksum_value_even_if_present() -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate["checksum_status"] = "VERIFIED"
        candidate["checksum_sha256"] = "not-a-real-sha256"

    with pytest.raises(RegistryError, match="VERIFIED requires a real 64-hex sha256"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_a_bare_checksum_shape_with_no_bound_evidence() -> None:
    """The exact gap a prior review flagged: a real-shaped 64-hex digest with
    no artifact, no timestamps and no cross-check behind it must NOT reach
    VERIFIED. Decorative metadata is not evidence."""

    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate["checksum_status"] = "VERIFIED"
        candidate["checksum_sha256"] = "0" * 64  # no artifact_path behind it

    with pytest.raises(RegistryError, match="requires a non-empty artifact_path"):
        validate_registry(_tampered(mutate))


def _verified_evidence_patch(tmp_path: Path) -> dict[str, Any]:
    """A genuinely verifiable VERIFIED patch: real bytes on disk, a checksum
    recomputed from those exact bytes, ordered ISO-8601 timestamps, and a
    PASSED cross-check naming two distinct sources that are not the
    candidate's own URL. Mirrors the EvidenceBinding pattern used for the
    dataset-manifest fixtures (checksum over ACTUAL bytes, never a label)."""
    artifact = tmp_path / "fja05680-sp500-snapshot.csv"
    artifact.write_bytes(b"date,ticker,action\n2024-01-01,ABCX,ADDED\n")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return {
        "checksum_status": "VERIFIED",
        "checksum_sha256": digest,
        "artifact_path": str(artifact),
        "timestamps": {
            "effective_at": "2024-01-01T00:00:00+00:00",
            "effective_at_status": "RECORDED",
            "observed_at": "2024-01-02T00:00:00+00:00",
            "observed_at_status": "RECORDED",
            "available_at": "2024-01-03T00:00:00+00:00",
            "available_at_status": "RECORDED",
        },
        "cross_check": {
            "required": True,
            "status": "PASSED",
            "minimum_independent_corroborating_sources": 2,
            "corroborating_sources": [
                "https://www.sec.gov/cgi-bin/browse-edgar",
                "https://www.wsj.com/market-data/quotes/index/US/SPX",
            ],
        },
    }


def test_validator_accepts_a_genuinely_bound_verified_candidate(tmp_path: Path) -> None:
    """Positive control: the validator is not permanently closed - full,
    re-derivable evidence (real bytes + real digest + ordered timestamps +
    an independent PASSED cross-check) is accepted."""

    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))

    validate_registry(_tampered(mutate))  # must not raise


def test_validator_rejects_verified_with_nonexistent_artifact_path(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["artifact_path"] = str(tmp_path / "does-not-exist.csv")

    with pytest.raises(RegistryError, match="does not exist"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_with_relative_artifact_path(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["artifact_path"] = "relative/path.csv"

    with pytest.raises(RegistryError, match="must be an absolute path"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_with_mismatched_artifact_bytes(tmp_path: Path) -> None:
    """The checksum is re-derived from the file on disk, not trusted from the
    field: tampering the bytes AFTER the checksum was recorded is caught."""

    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        patch = _verified_evidence_patch(tmp_path)
        candidate.update(patch)
        Path(patch["artifact_path"]).write_bytes(b"tampered bytes, different content")

    with pytest.raises(RegistryError, match="does not match the recomputed digest"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_with_missing_timestamp(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["timestamps"]["observed_at"] = None

    with pytest.raises(RegistryError, match="non-empty ISO-8601 timestamp"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_with_out_of_order_timestamps(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["timestamps"]["effective_at"] = "2024-06-01T00:00:00+00:00"  # after available_at

    with pytest.raises(RegistryError, match="effective_at <= observed_at <= available_at"):
        validate_registry(_tampered(mutate))


@pytest.mark.parametrize("naive_key", ["effective_at", "observed_at", "available_at"])
def test_validator_rejects_verified_with_timezone_naive_timestamp(
    tmp_path: Path, naive_key: str
) -> None:
    """A naive timestamp's ordering depends on a later, unspecified timezone
    interpretation - it is not a fixed PIT instant and must be rejected, not
    silently accepted as whatever the reader's local/UTC assumption happens
    to be."""

    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["timestamps"][naive_key] = "2024-01-02T00:00:00"  # no offset, no "Z"

    with pytest.raises(RegistryError, match="must be timezone-aware"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_without_passed_cross_check(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["cross_check"]["status"] = "NOT_PERFORMED"

    with pytest.raises(RegistryError, match="cross_check.status must be PASSED"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_cross_check_naming_its_own_source_url(
    tmp_path: Path,
) -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["cross_check"]["corroborating_sources"] = [
            candidate["source_url"],
            "https://www.wsj.com/market-data/quotes/index/US/SPX",
        ]

    with pytest.raises(RegistryError, match="cannot be the candidate's own source_url"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_verified_cross_check_with_duplicate_sources(
    tmp_path: Path,
) -> None:
    """Two copies of the same string never count as two independent sources."""

    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate.update(_verified_evidence_patch(tmp_path))
        candidate["cross_check"]["corroborating_sources"] = [
            "https://www.sec.gov/cgi-bin/browse-edgar",
            "https://www.sec.gov/cgi-bin/browse-edgar",
        ]

    with pytest.raises(RegistryError, match="at least 2 distinct corroborating sources"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_cross_check_marked_passed_before_checksum_is_verified() -> None:
    """Cross-check PASSED can never run ahead of checksum VERIFIED - a
    candidate cannot be half-promoted."""

    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate["cross_check"]["status"] = "PASSED"
        candidate["cross_check"]["corroborating_sources"] = [
            "https://www.sec.gov/cgi-bin/browse-edgar",
            "https://www.wsj.com/market-data/quotes/index/US/SPX",
        ]

    with pytest.raises(RegistryError, match="cannot be PASSED before checksum_status is VERIFIED"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_secondary_research_pit_with_low_cross_check_minimum() -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate["cross_check"]["minimum_independent_corroborating_sources"] = 1

    with pytest.raises(RegistryError, match="must be >= 2"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_not_found_candidate_carrying_a_checksum() -> None:
    def mutate(data: dict[str, Any]) -> None:
        not_found = next(
            c for c in data["candidates"] if c["classification"] == "NOT_FOUND_OR_COMMERCIAL_ONLY"
        )
        not_found["checksum_sha256"] = "1" * 64

    with pytest.raises(RegistryError, match="must not carry a checksum"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_not_found_candidate_requiring_cross_check() -> None:
    def mutate(data: dict[str, Any]) -> None:
        not_found = next(
            c for c in data["candidates"] if c["classification"] == "NOT_FOUND_OR_COMMERCIAL_ONLY"
        )
        not_found["cross_check"]["required"] = True

    with pytest.raises(RegistryError, match="must not require cross-check"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_secondary_research_pit_missing_source_authority() -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = _candidate(data, "fja05680-sp500-github")
        candidate["source_authority"] = "OFFICIAL_PUBLIC"

    with pytest.raises(RegistryError, match="PUBLIC_SECONDARY"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_duplicate_candidate_ids() -> None:
    def mutate(data: dict[str, Any]) -> None:
        clone = copy.deepcopy(data["candidates"][0])
        data["candidates"].append(clone)

    with pytest.raises(RegistryError, match="duplicate candidate_id"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_missing_classification_key() -> None:
    def mutate(data: dict[str, Any]) -> None:
        del data["classifications"]["SECONDARY_RESEARCH_PIT"]

    with pytest.raises(RegistryError, match="classifications keys must be exactly"):
        validate_registry(_tampered(mutate))
