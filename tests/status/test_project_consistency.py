"""Consistency guards for the canonical project map.

The master plan, the machine-readable component matrix, the PoI registry, the
Phase-13 deferred ledger and the canonical task list are five views of one
truth. Documentation drifts silently; these tests make drift a build failure.

Each test corresponds to a way the project could be misrepresented in a way
that actually matters — a component claiming integration it does not have, a
strategy called eligible without the evidence, a blocked PoI shown as active,
PAPER_AUTO or LIVE described as on. Nothing here connects to anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS = REPO_ROOT / "docs/baseline-v0.5/CLAUDE_CODE_TASKS.yaml"
MATRIX = REPO_ROOT / "docs/status/COMPONENT_EVIDENCE_MATRIX.yaml"
POI_STATUS = REPO_ROOT / "docs/poi/STATUS.yaml"
DEFERRED = REPO_ROOT / "docs/operations/PHASE_13_DEFERRED_OWNER_RUNS.yaml"
MASTER_PLAN = REPO_ROOT / "docs/roadmaps/TRADING_SYSTEM_MASTER_PLAN.md"
ACCEL_PLAN = REPO_ROOT / "docs/roadmaps/ACCELERATED_EXECUTION_PLAN.md"
FREEZE_TEMPLATE = REPO_ROOT / "docs/strategies/STRATEGY_FREEZE_TEMPLATE.md"
CURRENT_STATUS = REPO_ROOT / "docs/status/CURRENT_PROJECT_STATUS.md"
EVIDENCE_DIR = REPO_ROOT / "docs/poi/evidence"

#: Every planning document this suite governs.
PLANNING_DOCS = (MASTER_PLAN, ACCEL_PLAN, FREEZE_TEMPLATE, CURRENT_STATUS)


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def matrix() -> dict[str, Any]:
    data: dict[str, Any] = _load(MATRIX)
    return data


@pytest.fixture(scope="module")
def components(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    comps: list[dict[str, Any]] = matrix["components"]
    return comps


@pytest.fixture(scope="module")
def poi_items() -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in _load(POI_STATUS)["items"]}


@pytest.fixture(scope="module")
def deferred() -> dict[str, str]:
    return {r["id"]: r["current_status"] for r in _load(DEFERRED)["records"]}


# -- 1. the files exist and are structurally sound ---------------------------


def test_every_planning_document_exists() -> None:
    for path in (*PLANNING_DOCS, MATRIX):
        assert path.is_file(), f"missing canonical planning document: {path}"


def test_every_component_declares_every_required_field(
    components: list[dict[str, Any]],
) -> None:
    required = {
        "component_id",
        "canonical_task_ids",
        "implementation_status",
        "evidence_class",
        "real_integration_status",
        "strategy_dependency",
        "current_blocker",
        "owner_dependency",
        "next_action",
        "exit_criterion",
    }
    for component in components:
        missing = required - set(component)
        assert not missing, f"{component.get('component_id')} missing {sorted(missing)}"


def test_component_vocabularies_are_respected(
    matrix: dict[str, Any], components: list[dict[str, Any]]
) -> None:
    enums = matrix["enums"]
    for component in components:
        for field, allowed in enums.items():
            value = component.get(field)
            if value is not None:
                assert value in allowed, (
                    f"{component['component_id']}.{field}={value!r} not in {allowed}"
                )


def test_the_mandated_components_are_all_covered(
    components: list[dict[str, Any]],
) -> None:
    """The audit scope is fixed; a component may not quietly disappear."""
    mandated = {
        "platform", "persistence", "security",
        "whatsapp-user", "discord-user", "telegram-user", "telegram-bot",
        "telegram-coexistence", "investingpro", "news", "tradingview",
        "market-data", "external-market-data-provider",
        "ai-provider", "ai-analysis-engines", "ai-review-committee",
        "strategy-manifests", "fusion", "decision", "risk", "allocation",
        "pre-submit", "ibkr-adapter", "execution", "protection",
        "reconciliation", "dashboard", "replay", "backtest", "shadow",
        "ibc", "watchdog", "event-bus-nats",
    }
    present = {c["component_id"] for c in components}
    assert mandated <= present, f"uncovered components: {sorted(mandated - present)}"


# -- 2. no component may claim integration it cannot evidence ----------------


def test_integration_verified_requires_a_real_evidence_file(
    components: list[dict[str, Any]],
) -> None:
    """A component is integration-verified only against a file that EXISTS."""
    for component in components:
        if component["real_integration_status"] not in {
            "INTEGRATION_VERIFIED",
            "READ_ONLY_VERIFIED",
        }:
            continue
        evidence = component.get("evidence")
        assert evidence, (
            f"{component['component_id']} claims"
            f" {component['real_integration_status']} with no evidence file"
        )
        assert (REPO_ROOT / evidence).is_file(), (
            f"{component['component_id']} points at missing evidence: {evidence}"
        )


def test_evidence_class_integration_verified_matches_integration_status(
    components: list[dict[str, Any]],
) -> None:
    for component in components:
        if component["evidence_class"] == "INTEGRATION_VERIFIED":
            assert component["real_integration_status"] == "INTEGRATION_VERIFIED", (
                f"{component['component_id']} is evidence_class"
                " INTEGRATION_VERIFIED but not integration-verified"
            )


def test_a_component_with_a_blocked_poi_is_not_presented_as_active(
    components: list[dict[str, Any]], poi_items: dict[str, dict[str, Any]]
) -> None:
    """A BLOCKED PoI can never back a fully integration-verified component."""
    for component in components:
        poi_id = component.get("poi_id")
        if poi_id is None:
            continue
        assert poi_id in poi_items, f"{component['component_id']} names unknown {poi_id}"
        if poi_items[poi_id]["status"] == "BLOCKED":
            assert component["real_integration_status"] != "INTEGRATION_VERIFIED", (
                f"{component['component_id']} claims INTEGRATION_VERIFIED while"
                f" {poi_id} is BLOCKED"
            )
            assert component["current_blocker"] not in (None, "", "none"), (
                f"{component['component_id']} has a BLOCKED PoI but no blocker"
            )


def test_an_external_source_is_never_active_without_poi_evidence(
    components: list[dict[str, Any]], poi_items: dict[str, dict[str, Any]]
) -> None:
    """External sources are the easiest thing to over-claim."""
    external = {
        "whatsapp-user", "discord-user", "telegram-user", "telegram-bot",
        "telegram-coexistence", "investingpro", "news", "tradingview",
        "ai-provider",
    }
    for component in components:
        if component["component_id"] not in external:
            continue
        poi_id = component.get("poi_id")
        assert poi_id, f"{component['component_id']} must name its PoI"
        if poi_items[poi_id]["status"] != "PASS":
            assert component["real_integration_status"] == "BLOCKED", (
                f"{component['component_id']} is not BLOCKED but {poi_id} is not PASS"
            )
            assert component["evidence_class"] == "BLOCKED", (
                f"{component['component_id']} evidence_class must be BLOCKED"
            )
            # Erasing the reason is how a blocked source starts looking active.
            blocker = str(component["current_blocker"]).strip().lower()
            assert blocker not in {"", "none", "n/a", "-"}, (
                f"{component['component_id']} is blocked by {poi_id} but states"
                " no blocker reason"
            )
            assert component["owner_dependency"] != "NONE", (
                f"{component['component_id']} is blocked but declares no owner"
                " dependency"
            )


def test_social_sources_stay_read_only_for_trading(
    components: list[dict[str, Any]],
) -> None:
    social = {
        "whatsapp-user", "discord-user", "telegram-user", "telegram-bot",
        "telegram-coexistence",
    }
    for component in components:
        if component["component_id"] in social:
            assert component.get("read_only_for_trading") is True, (
                f"{component['component_id']} must be read-only for trading input"
            )


def test_the_telegram_bot_is_marked_supplemental(
    components: list[dict[str, Any]],
) -> None:
    """The bot path may never substitute for the personal-user path."""
    bot = next(c for c in components if c["component_id"] == "telegram-bot")
    assert bot.get("supplemental_only") is True


# -- 3. strategy eligibility ------------------------------------------------


def test_no_strategy_is_eligible_without_oos_and_shadow(
    matrix: dict[str, Any],
) -> None:
    for strategy in matrix["strategies"]:
        if strategy.get("eligible"):
            assert strategy.get("out_of_sample_pass") is True, (
                f"{strategy['strategy_id']} eligible without OUT_OF_SAMPLE_PASS"
            )
            assert strategy.get("shadow_pass") is True, (
                f"{strategy['strategy_id']} eligible without SHADOW_PASS"
            )


def test_every_strategy_defaults_to_profitability_not_proven(
    matrix: dict[str, Any],
) -> None:
    for strategy in matrix["strategies"]:
        if not (strategy.get("out_of_sample_pass") and strategy.get("shadow_pass")):
            assert strategy["evidence_class"] == "PROFITABILITY_NOT_PROVEN", (
                f"{strategy['strategy_id']} must read PROFITABILITY_NOT_PROVEN"
            )


def test_the_freeze_template_invents_no_trading_parameter() -> None:
    """The template must collect decisions, never supply numbers."""
    text = FREEZE_TEMPLATE.read_text(encoding="utf-8")
    # A filled-in parameter would look like `risk per trade | 0.5%`.
    offenders = re.findall(r"\|\s*\d+(?:\.\d+)?\s*%\s*\|", text)
    assert not offenders, f"the freeze template contains concrete parameters: {offenders}"
    assert "baseline-trend-breakout-v1" in text
    assert "no parameter" in text.lower() or "never invent" in text.lower()


def test_the_recommended_baseline_needs_no_social_or_ai(
    matrix: dict[str, Any],
) -> None:
    baseline = next(
        s for s in matrix["strategies"] if s["strategy_id"] == "baseline-trend-breakout-v1"
    )
    assert baseline["eligible"] is False
    assert baseline["hypothesis_frozen"] is False
    assert "no social" in baseline["rationale"].lower()


# -- 4. DEF statuses may not diverge ----------------------------------------


def test_matrix_def_statuses_match_the_phase_13_ledger(
    matrix: dict[str, Any], deferred: dict[str, str]
) -> None:
    for def_id, claimed in matrix["deferred_owner_runs"]["highlighted"].items():
        assert def_id in deferred, f"{def_id} is not in the Phase-13 ledger"
        assert deferred[def_id] == claimed, (
            f"{def_id}: matrix says {claimed}, ledger says {deferred[def_id]}"
        )


def test_component_deferred_ids_exist_in_the_ledger(
    components: list[dict[str, Any]], deferred: dict[str, str]
) -> None:
    for component in components:
        ids = component.get("deferred_ids") or (
            [component["deferred_id"]] if component.get("deferred_id") else []
        )
        for def_id in ids:
            assert def_id in deferred, (
                f"{component['component_id']} names unknown deferred run {def_id}"
            )


def test_the_broker_changing_follow_ups_are_still_unrun(deferred: dict[str, str]) -> None:
    """DEF-18/DEF-20 change broker state; documentation may not mark them done."""
    assert deferred["DEF-18-post-fix-cancellation-verification"] == "NOT_RUN"
    assert deferred["DEF-20-restart-recovery-cancellation"] == "NOT_RUN"


def test_the_master_plan_states_the_def_reality() -> None:
    text = MASTER_PLAN.read_text(encoding="utf-8")
    assert "DEF-19" in text and "PASS" in text
    assert "DEF-18" in text
    assert "DEF-20" in text
    assert "NOT_RUN" in text


# -- 5. the AI committee is never an execution authority --------------------


def test_the_committee_is_declared_a_decision_input_only(
    components: list[dict[str, Any]],
) -> None:
    committee = next(c for c in components if c["component_id"] == "ai-review-committee")
    assert committee["execution_authority"] is False
    assert committee["role"] == "DECISION_INPUT_ONLY"
    # It is designed, not running: it must never claim real integration.
    assert committee["real_integration_status"] == "NONE"
    assert committee["implementation_status"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize("doc", PLANNING_DOCS, ids=lambda p: p.name)
def test_no_document_grants_the_committee_execution_authority(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8").lower()
    forbidden = (
        "committee executes",
        "committee submits",
        "committee approves the order",
        "committee is the execution authority",
        "committee creates submitauthorization",
        "committee may place",
    )
    for phrase in forbidden:
        assert phrase not in text, f"{doc.name} grants the committee authority: {phrase!r}"


# -- 6. PAPER_AUTO and LIVE stay off ----------------------------------------


def test_execution_modes_are_disabled(matrix: dict[str, Any]) -> None:
    modes = matrix["execution_modes"]
    assert modes["paper_auto"] == "DISABLED"
    assert modes["live"] == "DISABLED"
    assert modes["shadow"] == "EVALUATE_ONLY"
    assert modes["flatten"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize("doc", PLANNING_DOCS, ids=lambda p: p.name)
def test_no_document_describes_paper_auto_or_live_as_enabled(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8").lower()
    forbidden = (
        "paper_auto is enabled",
        "paper auto is enabled",
        "paper_auto: enabled",
        "live is enabled",
        "live trading is enabled",
        "live is now active",
        "enable live",
    )
    for phrase in forbidden:
        assert phrase not in text, f"{doc.name} claims a frozen mode is on: {phrase!r}"


def test_paper_auto_gates_are_all_still_open(
    matrix: dict[str, Any], deferred: dict[str, str], poi_items: dict[str, dict[str, Any]]
) -> None:
    """PAPER_AUTO may only read enabled once every gate has actually passed."""
    gates_open = (
        poi_items["POI-IBKR-001"]["status"] != "PASS"
        or deferred["DEF-18-post-fix-cancellation-verification"] != "PASS"
        or deferred["DEF-20-restart-recovery-cancellation"] != "PASS"
        or not any(
            s.get("out_of_sample_pass") and s.get("shadow_pass")
            for s in matrix["strategies"]
        )
    )
    if gates_open:
        assert matrix["execution_modes"]["paper_auto"] == "DISABLED", (
            "PAPER_AUTO must stay DISABLED while any gate is open"
        )


# -- 7. task accounting may not drift ---------------------------------------


def test_the_canonical_task_count_matches_the_baseline(matrix: dict[str, Any]) -> None:
    actual = len(_load(TASKS)["tasks"])
    claimed = matrix["canonical_tasks"]["total"]
    assert claimed == actual, f"matrix claims {claimed} tasks, YAML has {actual}"


def test_implemented_plus_remaining_equals_the_total(matrix: dict[str, Any]) -> None:
    counts = matrix["canonical_tasks"]
    assert counts["implemented"] + counts["remaining"] == counts["total"]


def test_the_documents_state_the_same_task_count(matrix: dict[str, Any]) -> None:
    total = matrix["canonical_tasks"]["total"]
    for doc in (MASTER_PLAN, CURRENT_STATUS):
        text = doc.read_text(encoding="utf-8")
        assert f"**{total}**" in text or f"{total} tasks" in text, (
            f"{doc.name} does not state the canonical task count {total}"
        )


def test_every_component_task_id_is_canonical(components: list[dict[str, Any]]) -> None:
    canonical = {t["task_id"] for t in _load(TASKS)["tasks"]}
    for component in components:
        for task_id in component["canonical_task_ids"]:
            assert task_id in canonical, (
                f"{component['component_id']} names non-canonical task {task_id}"
            )


# -- 8. the prose plan and the matrix must agree ----------------------------


def test_the_master_plan_references_the_matrix() -> None:
    text = MASTER_PLAN.read_text(encoding="utf-8")
    assert "COMPONENT_EVIDENCE_MATRIX.yaml" in text


def test_the_master_plan_agrees_with_the_matrix_on_poi_pass_count(
    poi_items: dict[str, dict[str, Any]],
) -> None:
    passing = sum(1 for i in poi_items.values() if i["status"] == "PASS")
    text = MASTER_PLAN.read_text(encoding="utf-8")
    assert f"{passing} of {len(poi_items)} PoIs PASS" in text, (
        f"the master plan must state '{passing} of {len(poi_items)} PoIs PASS'"
    )


def test_the_master_plan_agrees_with_the_registry_on_blocked_pois(
    poi_items: dict[str, dict[str, Any]],
) -> None:
    """A PoI that is BLOCKED in the registry may not read PASS in the plan."""
    text = MASTER_PLAN.read_text(encoding="utf-8")
    for poi_id, item in poi_items.items():
        if item["status"] != "BLOCKED":
            continue
        # find the plan's table row for this PoI, if present
        for line in text.splitlines():
            if line.startswith(f"| {poi_id} "):
                assert "BLOCKED" in line, (
                    f"{poi_id} is BLOCKED in the registry but the plan row says: {line}"
                )


def test_the_matrix_and_registry_agree_that_the_headline_pois_are_blocked(
    poi_items: dict[str, dict[str, Any]],
) -> None:
    assert poi_items["POI-IBKR-001"]["status"] == "BLOCKED"
    assert poi_items["POI-MD-001"]["status"] == "BLOCKED"


def test_the_accelerated_plan_covers_every_mandated_item() -> None:
    text = ACCEL_PLAN.read_text(encoding="utf-8")
    for item in (
        "DEF-18", "DEF-20", "B-02", "B-03", "B-04",
        "IMP-1401", "IMP-1402", "IMP-1403", "IMP-1404",
        "IMP-1501", "IMP-1502", "IMP-1503",
        "baseline-trend-breakout-v1", "walk-forward", "shadow",
        "Go / No-Go", "ablation", "point-in-time",
    ):
        assert item in text, f"the accelerated plan omits {item!r}"
    for track in ("Track A", "Track B", "Track C", "Track D"):
        assert track in text, f"missing {track}"


def test_the_accelerated_plan_keeps_live_disabled() -> None:
    text = ACCEL_PLAN.read_text(encoding="utf-8")
    assert "LIVE remains DISABLED" in text or "Live stays DISABLED" in text


@pytest.mark.parametrize("doc", PLANNING_DOCS, ids=lambda p: p.name)
def test_every_planning_document_records_what_was_not_executed(doc: Path) -> None:
    """Planning documents must not imply work that never happened."""
    if doc is FREEZE_TEMPLATE:
        pytest.skip("the freeze template is a form, not a status document")
    text = doc.read_text(encoding="utf-8")
    assert "NOT EXECUTED" in text or "NOT_RUN" in text, (
        f"{doc.name} must state what has not been executed"
    )


# -- 9. broker-mutating owner-runs must stay serialized ----------------------
#
# Two owner-runs on one Paper account cannot be reconciled: an order or position
# seen by one may have been created, cancelled or orphaned by the other, and a
# Gateway restart can sever a run mid-flight. The plan describing them as
# concurrent would be an instruction to destroy the evidence, so the phrasing is
# pinned here.

#: Work packages that change broker state or restart the Gateway.
MUTATING_WORK_PACKAGES = ("B-WP1", "B-WP2", "B-WP3", "B-WP4b")

#: Words that assert concurrency.
_CONCURRENCY = ("in parallel", "fully parallel", "concurrent", "concurrently", "alongside")

#: A concurrency word is acceptable only inside a negated clause.
_NEGATIONS = ("never", "not ", "no other", "no two", "must not", "cannot", "⛔")


def _section(text: str, heading_prefix: str) -> str:
    """The body of one '### <heading_prefix>...' block."""
    lines = text.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith(f"### {heading_prefix}")
    )
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(("### ", "## "))),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _claims_concurrency(block: str) -> list[str]:
    """Sentences that assert concurrency without negating it."""
    offenders = []
    for raw in re.split(r"(?<=[.!?])\s+|\n-\s+", block):
        sentence = " ".join(raw.split())
        lowered = sentence.lower()
        if not any(word in lowered for word in _CONCURRENCY):
            continue
        if any(negation in lowered for negation in _NEGATIONS):
            continue
        offenders.append(sentence)
    return offenders


@pytest.mark.parametrize("work_package", MUTATING_WORK_PACKAGES)
def test_a_broker_mutating_work_package_never_claims_concurrency(
    work_package: str,
) -> None:
    text = ACCEL_PLAN.read_text(encoding="utf-8")
    offenders = _claims_concurrency(_section(text, work_package))
    assert not offenders, (
        f"{work_package} is broker-mutating but claims concurrency: {offenders}"
    )


def test_def20_runs_after_def18_in_its_own_clean_account_window() -> None:
    block = _section(ACCEL_PLAN.read_text(encoding="utf-8"), "B-WP3")
    lowered = block.lower()
    assert "def-20" in lowered
    assert "own controlled\n  clean-account window" in lowered or (
        "own" in lowered and "clean-account window" in lowered
    ), "DEF-20 must declare its own clean-account window"
    assert "b-wp1" in lowered, "DEF-20 must depend on DEF-18 (B-WP1)"
    assert "not concurrent with b-wp2" in lowered, (
        "DEF-20 must state explicitly that it is NOT concurrent with the fill path"
    )
    assert "verify-clean" in lowered


def test_destructive_host_drills_have_their_own_scheduled_window() -> None:
    block = _section(ACCEL_PLAN.read_text(encoding="utf-8"), "B-WP4b")
    lowered = block.lower()
    assert "scheduled" in lowered and "window" in lowered
    for phase in ("def-18", "def-20"):
        assert phase in lowered, f"B-WP4b must name {phase} as mutually exclusive"
    assert "never" in lowered


def test_non_mutating_operational_hardening_stays_parallel() -> None:
    """Serializing broker runs must not accidentally serialize safe work."""
    block = _section(ACCEL_PLAN.read_text(encoding="utf-8"), "B-WP4a").lower()
    assert "parallel" in block, "non-mutating hardening must remain parallel"
    assert "no broker state" in block or "changes no broker state" in block
    # ...and it must not be *labelled* exclusive anywhere, heading included:
    # a contradictory heading over a parallel-safe body is exactly how this
    # would silently regress.
    for exclusive in ("serialized", "exclusive", "own scheduled window",
                      "own window", "one at a time"):
        assert exclusive not in block, (
            f"B-WP4a is non-mutating but is labelled {exclusive!r}; that would"
            " needlessly serialize safe work"
        )
    # The safe-work list mandated by the owner review.
    for item in ("systemd", "watchdog", "port", "permission", "log rotation",
                 "ntp", "reproducib"):
        assert item in block, f"B-WP4a omits non-mutating item {item!r}"


def test_the_plan_states_the_global_serialization_hard_stop() -> None:
    text = ACCEL_PLAN.read_text(encoding="utf-8").lower()
    assert "no two broker-mutating or gateway-restarting owner-run procedures may" in text
    assert "same paper account/gateway" in text


def test_the_master_plan_repeats_the_serialization_rule() -> None:
    text = MASTER_PLAN.read_text(encoding="utf-8").lower()
    assert "never overlap" in text or "exclusive" in text
    assert "verify-clean" in text


# -- 10. DEF-18 session wording ---------------------------------------------


@pytest.mark.parametrize(
    "doc", (ACCEL_PLAN, MASTER_PLAN, CURRENT_STATUS), ids=lambda p: p.name
)
def test_def18_is_not_described_as_requiring_regular_hours(doc: Path) -> None:
    """The lifecycle gate accepts regular, pre-market and after-hours alike."""
    text = doc.read_text(encoding="utf-8").lower()
    # The cancel path must never be described as regular-hours-only.
    for phrase in ("regular market hours for def-18", "def-18 requires regular hours"):
        assert phrase not in text
    assert "non-closed session" in text, (
        f"{doc.name} must describe DEF-18's session as a permitted non-closed session"
    )


def test_the_permitted_sessions_are_named_explicitly() -> None:
    text = ACCEL_PLAN.read_text(encoding="utf-8").lower()
    for session in ("regular", "pre-market", "after-hours"):
        assert session in text, f"the plan must name the {session} session"
    assert "closed" in text and "refused" in text


def test_the_fill_path_still_requires_market_open_conditions() -> None:
    """Relaxing the cancel-path session must not relax the fill path."""
    block = _section(ACCEL_PLAN.read_text(encoding="utf-8"), "B-WP2").lower()
    assert "market-open conditions" in block


# -- 11. strategy verdict must be precise, not categorical -------------------


def test_the_strategy_verdict_is_precise_not_categorical() -> None:
    text = MASTER_PLAN.read_text(encoding="utf-8")
    assert "There is no strategy." not in text, (
        "the categorical claim erases the infrastructure and hypotheses that exist"
    )
    assert (
        "no frozen, owner-approved, evidence-qualified or\nproduction-eligible\nstrategy"
        in text
        or "no frozen, owner-approved, evidence-qualified or" in text
    )
    lowered = text.lower()
    # The distinctions that must survive.
    assert "infrastructure" in lowered
    assert "hypothes" in lowered
    assert "profitability_not_proven" in lowered


# -- 12. AI track: safe work early, ablation still gated ---------------------


def test_the_ai_provider_poi_may_start_immediately() -> None:
    block = _section(ACCEL_PLAN.read_text(encoding="utf-8"), "D-WP1 ").lower()
    assert "may start immediately" in block or "start immediately" in block
    assert "budget" in block
    assert "credential" in block


def test_provider_governance_runs_parallel_with_track_a() -> None:
    block = _section(ACCEL_PLAN.read_text(encoding="utf-8"), "D-WP1b").lower()
    assert "parallel with track a" in block
    for item in ("secret storage", "budget", "timeout", "rate limit", "malformed"):
        assert item in block, f"provider governance omits {item!r}"


def test_the_committee_ablation_remains_gated_on_the_baseline() -> None:
    block = _section(ACCEL_PLAN.read_text(encoding="utf-8"), "D-WP3").lower()
    assert "gated" in block
    assert "baseline" in block
    assert "oos" in block or "out-of-sample" in block
    assert "no committee output enters the decision path" in block


def test_no_committee_output_before_ablation_anywhere() -> None:
    for doc in (ACCEL_PLAN, MASTER_PLAN):
        text = doc.read_text(encoding="utf-8").lower()
        assert "execution authority" in text or "decision input" in text
