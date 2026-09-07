"""A-WP2-FREE-HISTORICAL-GICS-CROSSCHECK-EVAL — fail-closed GICS cross-check
candidate source registry.

``config/data-sources/gics-sector-crosscheck-source-registry.json`` records
every concrete free candidate source EVALUATED for independently
cross-checking Wikipedia-revision-captured GICS Sector values at historical
as-of dates (CLAUDE.md "prevent look-ahead"; "no substitute for historical
GICS"; ADR-0017). Unlike its sibling
(``sp500-historical-source-registry.json``, purely offline), exactly ONE
candidate here (``spdji-msci-gics-structure-change-press-releases``) points at
a real, bounded experiment already performed by
``scripts/run_wikipedia_gics_sector_crosscheck_experiment.py`` and recorded
under ``docs/poi/evidence/dataset-v1/wikipedia-gics-sector-crosscheck/``.

This module asserts the registry file says what it must say (and only what
it is allowed to say), and independently re-derives the referenced
experiment evidence's own checksum rather than trusting its
``corroborated``/``evidence_checksum`` labels. Nothing here fetches anything
over the network.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "config" / "data-sources" / "gics-sector-crosscheck-source-registry.json"

ALLOWED_CLASSIFICATIONS = frozenset(
    {"NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC", "NOT_SUFFICIENT_FOR_GICS_CROSSCHECK"}
)
ALLOWED_REJECTION_REASONS = frozenset(
    {
        "DIFFERENT_TAXONOMY_NOT_GICS",
        "NO_TEMPORAL_SEMANTICS_CURRENT_ONLY",
        "NO_FREE_DATED_HISTORICAL_ARCHIVE_IDENTIFIED",
    }
)
ELIGIBILITY_FLAGS = (
    "authoritative_official_evidence",
    "eligible_for_promotion_without_validation",
    "substitute_for_historical_gics",
)

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


def _validate_eligibility(eligibility: object, where: str) -> None:
    _require(isinstance(eligibility, dict), f"{where}: eligibility object is required")
    assert isinstance(eligibility, dict)
    for flag in ELIGIBILITY_FLAGS:
        _require(flag in eligibility, f"{where}: eligibility missing {flag!r}")
        _require(eligibility[flag] is False, f"{where}: eligibility.{flag} must be False")


def _validate_narrow_event_scoped(candidate: dict[str, Any], where: str) -> None:
    _require(
        candidate.get("source_authority") == "OFFICIAL_PUBLIC",
        f"{where}: NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC must declare source_authority OFFICIAL_PUBLIC",
    )
    experiment = candidate.get("bounded_real_experiment")
    _require(isinstance(experiment, dict), f"{where}: bounded_real_experiment object is required")
    assert isinstance(experiment, dict)
    for key in ("performed_at", "performed_by", "evidence_path", "summary"):
        value = experiment.get(key)
        _require(
            isinstance(value, str) and value.strip() != "",
            f"{where}: bounded_real_experiment.{key} must be a non-empty string",
        )
    evidence_path = REPO_ROOT / str(experiment["evidence_path"])
    _require(
        evidence_path.is_file(),
        f"{where}: bounded_real_experiment.evidence_path {experiment['evidence_path']!r} does not exist",
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    declared_checksum = evidence.get("evidence_checksum")
    _require(
        isinstance(declared_checksum, str) and declared_checksum.startswith("sha256:"),
        f"{where}: referenced experiment evidence carries no real evidence_checksum",
    )
    body_without_checksum = {k: v for k, v in evidence.items() if k != "evidence_checksum"}
    serialized = json.dumps(
        body_without_checksum, sort_keys=True, separators=(",", ":")
    ).encode("ascii", "backslashreplace")
    recomputed = "sha256:" + hashlib.sha256(serialized).hexdigest()
    _require(
        declared_checksum == recomputed,
        f"{where}: referenced experiment evidence_checksum does not match its own recomputed body"
        " (tampered or stale)",
    )
    for key in ("temporal_semantics", "access_constraints"):
        value = candidate.get(key)
        _require(
            isinstance(value, str) and value.strip() != "",
            f"{where}: {key} must be a non-empty string",
        )
    limitations = candidate.get("limitations")
    _require(
        isinstance(limitations, list) and len(limitations) > 0,
        f"{where}: limitations must be a non-empty list",
    )


def _validate_not_sufficient(candidate: dict[str, Any], where: str) -> None:
    reason = candidate.get("rejection_reason")
    _require(
        reason in ALLOWED_REJECTION_REASONS,
        f"{where}: rejection_reason {reason!r} is not one of {sorted(ALLOWED_REJECTION_REASONS)}",
    )
    _require(
        "bounded_real_experiment" not in candidate,
        f"{where}: NOT_SUFFICIENT_FOR_GICS_CROSSCHECK must not carry an experiment reference",
    )
    limitations = candidate.get("limitations")
    _require(
        isinstance(limitations, list) and len(limitations) > 0,
        f"{where}: limitations must be a non-empty list",
    )


def validate_registry(data: dict[str, Any]) -> None:
    """Fail-closed structural + invariant validation of the registry payload."""
    _require(
        data.get("network_fetch_performed_by_this_registry") is False,
        "registry: must declare it never fetches",
    )
    _require(data.get("status") == "NOT_INTEGRATED", "registry: status must be NOT_INTEGRATED")
    _require(
        data.get("integration_evidence_claim") == "NONE",
        "registry: integration_evidence_claim must be NONE",
    )

    invariants = data.get("invariants")
    _require(isinstance(invariants, dict), "registry: invariants object is required")
    assert isinstance(invariants, dict)
    for key in (
        "no_candidate_is_authoritative_official_evidence",
        "no_candidate_is_eligible_for_promotion_without_validation",
        "no_candidate_substitutes_for_historical_gics",
    ):
        _require(invariants.get(key) is True, f"registry: invariants.{key} must be True")

    classifications = data.get("classifications")
    _require(isinstance(classifications, dict), "registry: classifications object is required")
    assert isinstance(classifications, dict)
    _require(
        set(classifications.keys()) == set(ALLOWED_CLASSIFICATIONS),
        f"registry: classifications keys must be exactly {sorted(ALLOWED_CLASSIFICATIONS)}",
    )
    for name, spec in classifications.items():
        assert isinstance(spec, dict)
        _validate_eligibility(spec.get("eligibility"), f"classifications.{name}")

    candidates = data.get("candidates")
    _require(isinstance(candidates, list) and len(candidates) > 0, "registry: candidates must be a non-empty list")
    assert isinstance(candidates, list)

    seen_ids: set[str] = set()
    saw_narrow_event_scoped = False
    for index, raw_candidate in enumerate(candidates):
        _require(isinstance(raw_candidate, dict), f"candidates[{index}]: object is required")
        candidate = cast(dict[str, Any], raw_candidate)
        where = f"candidates[{index}] ({candidate.get('candidate_id', '?')})"

        candidate_id = candidate.get("candidate_id")
        _require(
            isinstance(candidate_id, str) and bool(candidate_id.strip()),
            f"{where}: candidate_id is mandatory",
        )
        assert isinstance(candidate_id, str)
        _require(candidate_id not in seen_ids, f"{where}: duplicate candidate_id")
        seen_ids.add(candidate_id)

        source_name = candidate.get("source_name")
        _require(
            isinstance(source_name, str) and bool(source_name.strip()),
            f"{where}: source_name is mandatory",
        )

        classification = candidate.get("classification")
        _require(
            classification in ALLOWED_CLASSIFICATIONS,
            f"{where}: classification {classification!r} is not recognised",
        )

        _validate_eligibility(candidate.get("eligibility"), where)

        if classification == "NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC":
            saw_narrow_event_scoped = True
            _validate_narrow_event_scoped(candidate, where)
        else:
            _validate_not_sufficient(candidate, where)

    _require(
        saw_narrow_event_scoped,
        "registry: at least one NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC candidate is required"
        " (the whole point of this registry is that one such candidate was found and used)",
    )


# -- 1. the registry file itself -------------------------------------------


def test_registry_file_is_valid_json_and_loads() -> None:
    data = load_registry()
    assert data["registry_id"] == "gics-sector-crosscheck-source-registry"
    assert isinstance(data["candidates"], list) and len(data["candidates"]) >= 1


def test_registry_never_fetches_and_claims_no_integration() -> None:
    data = load_registry()
    assert data["network_fetch_performed_by_this_registry"] is False
    assert data["status"] == "NOT_INTEGRATED"
    assert data["integration_evidence_claim"] == "NONE"


def test_registry_passes_its_own_fail_closed_validator() -> None:
    validate_registry(load_registry())


def test_registry_never_makes_a_forbidden_claim_anywhere() -> None:
    raw_text = REGISTRY_PATH.read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_CLAIM_STRINGS:
        assert forbidden not in raw_text, f"registry must never contain the claim {forbidden!r}"


@pytest.mark.parametrize("flag", ELIGIBILITY_FLAGS)
def test_no_candidate_is_ever_eligible(flag: str) -> None:
    data = load_registry()
    for candidate in data["candidates"]:
        assert candidate["eligibility"][flag] is False


def test_every_candidate_id_is_unique() -> None:
    data = load_registry()
    ids = [c["candidate_id"] for c in data["candidates"]]
    assert len(ids) == len(set(ids))


# -- 2. the one real, referenced experiment ----------------------------------


def _candidate(data: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    matches = [c for c in data["candidates"] if c["candidate_id"] == candidate_id]
    assert len(matches) == 1
    return cast(dict[str, Any], matches[0])


def test_press_release_candidate_points_at_a_real_self_consistent_experiment() -> None:
    data = load_registry()
    candidate = _candidate(data, "spdji-msci-gics-structure-change-press-releases")
    assert candidate["classification"] == "NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC"
    assert candidate["source_authority"] == "OFFICIAL_PUBLIC"

    evidence_path = REPO_ROOT / candidate["bounded_real_experiment"]["evidence_path"]
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["classification"] == "SECONDARY_RESEARCH_PIT"
    assert evidence["eligibility"] == {
        "authoritative_official_evidence": False,
        "eligible_for_promotion_without_validation": False,
        "substitute_for_historical_gics": False,
    }


def test_referenced_experiment_records_both_transitions_scoped_to_named_tickers() -> None:
    data = load_registry()
    candidate = _candidate(data, "spdji-msci-gics-structure-change-press-releases")
    evidence_path = REPO_ROOT / candidate["bounded_real_experiment"]["evidence_path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    before = evidence["before_transition"]["sector_cross_check"]
    after = evidence["after_transition"]["sector_cross_check"]
    assert before["scope"] == "NARROW_CLASSIFICATION_CHANGE_CASE"
    assert after["scope"] == "NARROW_CLASSIFICATION_CHANGE_CASE"
    named_tickers = set(evidence["reclassification_event"]["named_reclassifications"])
    assert set(before["scope_tickers"]) == named_tickers
    assert set(after["scope_tickers"]) == named_tickers
    # The real, honest result: the AFTER transition corroborates; the BEFORE
    # transition does not (a genuine recorded disagreement, never hidden).
    assert after["corroborated"] is True
    assert isinstance(before["corroborated"], bool)
    if not before["corroborated"]:
        assert before["disagreement_count"] >= 1


def test_referenced_experiment_raw_provenance_files_exist_and_checksum() -> None:
    data = load_registry()
    candidate = _candidate(data, "spdji-msci-gics-structure-change-press-releases")
    evidence_path = REPO_ROOT / candidate["bounded_real_experiment"]["evidence_path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    for leg in ("before_transition", "after_transition"):
        raw_path = REPO_ROOT / evidence[leg]["raw_provenance_path"]
        assert raw_path.is_file()
        digest = "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest()
        assert digest == evidence[leg]["raw_checksum"]

    press_release_raw_path = REPO_ROOT / evidence["press_release_source"]["raw_artifact_path"]
    assert press_release_raw_path.is_file()
    digest = "sha256:" + hashlib.sha256(press_release_raw_path.read_bytes()).hexdigest()
    assert digest == evidence["press_release_source"]["full_file_checksum"]


def test_referenced_experiment_never_claims_official_or_unbounded_evidence() -> None:
    data = load_registry()
    candidate = _candidate(data, "spdji-msci-gics-structure-change-press-releases")
    evidence_path = REPO_ROOT / candidate["bounded_real_experiment"]["evidence_path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["official_licensed_gap_status"] == "DEFERRED_NOT_BLOCKING"
    not_usable = evidence["not_usable_for"]
    assert any("PIT_STRICT" in item for item in not_usable)
    assert any("full-universe" in item for item in not_usable)


# -- 3. rejected candidates are documented, not silently dropped ------------


def test_rejected_candidates_carry_an_allowed_reason_and_no_experiment() -> None:
    data = load_registry()
    rejected = [
        c for c in data["candidates"] if c["classification"] == "NOT_SUFFICIENT_FOR_GICS_CROSSCHECK"
    ]
    assert len(rejected) >= 1
    for candidate in rejected:
        assert candidate["rejection_reason"] in ALLOWED_REJECTION_REASONS
        assert "bounded_real_experiment" not in candidate


def test_sec_sic_is_rejected_as_a_different_taxonomy() -> None:
    data = load_registry()
    candidate = _candidate(data, "sec-edgar-sic-codes")
    assert candidate["rejection_reason"] == "DIFFERENT_TAXONOMY_NOT_GICS"


# -- 4. the validator is genuinely fail-closed -------------------------------


def _tampered(mutator: Any) -> dict[str, Any]:
    data = copy.deepcopy(load_registry())
    mutator(data)
    return data


def test_validator_rejects_authoritative_flag_flipped_true() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"][0]["eligibility"]["authoritative_official_evidence"] = True

    with pytest.raises(RegistryError, match="authoritative_official_evidence"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_substitute_flag_flipped_true() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"][0]["eligibility"]["substitute_for_historical_gics"] = True

    with pytest.raises(RegistryError, match="substitute_for_historical_gics"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_unknown_classification() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"][0]["classification"] = "OFFICIAL_CONFIRMED"

    with pytest.raises(RegistryError, match="not recognised"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_unknown_rejection_reason() -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = next(
            c for c in data["candidates"] if c["classification"] == "NOT_SUFFICIENT_FOR_GICS_CROSSCHECK"
        )
        candidate["rejection_reason"] = "JUST_BECAUSE"

    with pytest.raises(RegistryError, match="rejection_reason"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_nonexistent_evidence_path() -> None:
    def mutate(data: dict[str, Any]) -> None:
        candidate = next(
            c for c in data["candidates"] if c["classification"] == "NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC"
        )
        candidate["bounded_real_experiment"]["evidence_path"] = "docs/poi/evidence/does-not-exist.json"

    with pytest.raises(RegistryError, match="does not exist"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_tampered_referenced_evidence_checksum(tmp_path: Path) -> None:
    """A hand-edited evidence file whose body no longer matches its own
    declared evidence_checksum must be rejected, not trusted."""
    real_evidence_path = REPO_ROOT / _candidate(
        load_registry(), "spdji-msci-gics-structure-change-press-releases"
    )["bounded_real_experiment"]["evidence_path"]
    tampered_evidence = json.loads(real_evidence_path.read_text(encoding="utf-8"))
    tampered_evidence["classification"] = "OFFICIAL_PUBLIC"  # tamper, checksum now stale
    tampered_path = tmp_path / "tampered-evidence.json"
    tampered_path.write_text(json.dumps(tampered_evidence), encoding="utf-8")

    # tmp_path is outside REPO_ROOT; REPO_ROOT / <absolute path> collapses to
    # the absolute path in pathlib, so an absolute evidence_path still
    # resolves correctly for this test.
    def mutate_absolute(data: dict[str, Any]) -> None:
        candidate = next(
            c for c in data["candidates"] if c["classification"] == "NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC"
        )
        candidate["bounded_real_experiment"]["evidence_path"] = str(tampered_path)

    data = copy.deepcopy(load_registry())
    mutate_absolute(data)
    with pytest.raises(RegistryError, match="does not match its own recomputed body"):
        validate_registry(data)


def test_validator_rejects_duplicate_candidate_ids() -> None:
    def mutate(data: dict[str, Any]) -> None:
        clone = copy.deepcopy(data["candidates"][0])
        data["candidates"].append(clone)

    with pytest.raises(RegistryError, match="duplicate candidate_id"):
        validate_registry(_tampered(mutate))


def test_validator_rejects_missing_classification_key() -> None:
    def mutate(data: dict[str, Any]) -> None:
        del data["classifications"]["NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC"]

    with pytest.raises(RegistryError, match="classifications keys must be exactly"):
        validate_registry(_tampered(mutate))


def test_validator_requires_at_least_one_narrow_event_scoped_candidate() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["candidates"] = [
            c for c in data["candidates"] if c["classification"] != "NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC"
        ]

    with pytest.raises(RegistryError, match="at least one NARROW_EVENT_SCOPED_OFFICIAL_PUBLIC"):
        validate_registry(_tampered(mutate))
