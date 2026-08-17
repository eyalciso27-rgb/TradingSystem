# Dataset V1 — Real historical source qualification runbook

> How the four external Dataset V1 capabilities become `EMPIRICALLY_VERIFIED`,
> and the current honest status. Companion to
> [`DATASET-V1-CONTRACT.md`](DATASET-V1-CONTRACT.md) and
> [`DATASET-V1-CAPABILITY-MATRIX.yaml`](DATASET-V1-CAPABILITY-MATRIX.yaml).

## Source discovery — result as of 2026-08-17

A repository/environment discovery was performed **before** writing any adapter:

| Checked | Finding |
|---|---|
| Secret store (`secrets/`) | **absent** — no source credentials configured |
| Committed historical datasets | **none** (no CSV/Parquet/JSONL data in the repo) |
| `config/assumptions/provider-runtime-catalog.yaml` | `market-data-primary` and `market-data-fallback` are **`PENDING_POI`** (blocked on POI-MD-001) — *not approved* |
| InvestingPro | `PENDING_POI`; also ToS-blocked for programmatic access (see `PHASE6_SOURCE_ADAPTERS.md`) |
| `POI-IBKR-HIST.json` (authenticated) | proves **active-name** daily TRADES/RTH, conId, head timestamp, BID/ASK/MIDPOINT, symbol matching. Its own notes state that **delisted history, PIT S&P 500 membership and as-of GICS are NOT covered** |

**Conclusion: no owner-approved real historical source exists for any of the four
capabilities.** No adapter was therefore written against a specific provider, no
data was ingested, and **no evidence was fabricated**. All four cells remain
`BLOCKED_EXTERNAL`. What this package delivers instead is the **qualification
harness** that will promote them the moment a real source is supplied.

## Acceptance criteria (executable)

`CAPABILITY_CRITERIA` in
`packages/py/market-data-core/src/trading_market_data_core/dataset_v1_qualification.py`
encodes, per capability, what a real source must demonstrate:

| Capability | Required proofs (in addition to the common criteria) |
|---|---|
| `pit_sp500_membership` | `historical_addition`, `historical_removal` |
| `asof_gics` | `historical_classification_change` |
| `delisted_security_history` | `inactive_security_resolvable` |
| `corporate_actions` | `event_level_action` |

Common criteria, all mandatory: source kind **REAL**; historical effective dates;
knowledge/observation time; revision-safe; stable identity (**ticker-only is
never sufficient**); ≥1 genuine historical change; bounded coverage window; zero
validation errors; clean identity resolution (no ambiguous/unresolved/conflicting
rows); a **raw source checksum** bound into the evidence.

## Evidence schema (authenticated, shared by all four)

`QualificationEvidence` carries: schema version · capability · source
(id/name/version/kind/raw checksum/artifact/access note) · coverage window ·
temporal semantics · identity semantics · data-quality metrics · identity
resolution report · validation errors · proofs · result (PASS/PARTIAL/FAIL) ·
promotion eligibility · blocked reasons · `observed_at` · `normalized_checksum`.

The checksum is computed over the **canonical body** (sorted keys, tight
separators, ASCII) and is **recomputed and verified on load** — a tampered or
stale artifact is rejected, never trusted. Note the two distinct digests, both
real and intentionally different:

- `QualificationEvidence.normalized_checksum` authenticates the **canonical body**;
- `EvidenceBinding.checksum` (builder) authenticates the **file bytes**.

## Promotion path (the only one)

`promote_capability(evidence)` returns `EMPIRICALLY_VERIFIED` **only** when the
evidence re-authenticates against its body **and** the source is `REAL` **and**
the result is `PASS` **and** `promotion_eligible` is true. A `FIXTURE` source or a
`SNAPSHOT_ONLY` source can never be promoted, whatever its metrics; a hand-edited
artifact fails the checksum re-check. `capability_states_from_evidence` seeds
non-qualifiable cells from other authenticated paths (the six IBKR capabilities)
but **cannot** be used to smuggle a qualifiable cell to verified.

### Trust boundary (stated explicitly)

`SourceKind` (REAL / FIXTURE / SNAPSHOT_ONLY) and the temporal/identity semantics
are **declared by the adapter** that produced the run. The harness cannot detect a
mislabelled source by inspection alone — its defence is that every declaration is
recorded in the authenticated evidence together with the raw source checksum and
coverage window, so a false declaration is attributable and reviewable. Owner
approval of the source, and review of the evidence artifact, remain part of the
promotion process.

## Owner-host run

```bash
./scripts/run_dataset_v1_real_qualification.sh
```

Preflights the host, prints the acceptance criteria, discovers configured sources
and existing evidence, **verifies every artifact's checksum**, prints per-capability
state and blocked reasons, and exits non-zero while
`full_dataset_contract_ready` is false — so an owner run can never be mistaken
for success. It never prints credentials and never promotes anything by itself.

Environment overrides: `DATASET_V1_SOURCE_CONFIG`, `DATASET_V1_EVIDENCE_DIR`.

The equivalent CLI (also usable directly):

```bash
uv run --no-sync python -m trading_market_data_core.dataset_v1_qualification_cli report
```

## When a real source is supplied

1. Owner approves the source (licensing + access) and declares it in
   `config/data-sources/dataset-v1-sources.yaml` (**non-secret metadata only**;
   credentials stay in the `FileSecretStore`).
2. Implement its adapter behind the existing provider-neutral port
   (`MembershipSource` / `GicsSource` / `DelistingSource` /
   `CorporateActionSource`) — provider parsing stays in the adapter, never in
   Dataset V1 core.
3. Ingest a qualification-scale slice, run validation and identity resolution.
4. Call `qualify_source(...)`, write the sealed evidence to
   `docs/poi/evidence/dataset-v1/<capability>.json`.
5. Re-run the owner script; only genuinely passing cells flip to
   `EMPIRICALLY_VERIFIED`.

Licensed bulk data must **not** be committed where redistribution is not
permitted; the evidence artifact carries metadata and checksums, not the data.

## Current status

| Capability | State | Why |
|---|---|---|
| `pit_sp500_membership` | **BLOCKED_EXTERNAL** | no approved real source |
| `asof_gics` | **BLOCKED_EXTERNAL** | no approved real source |
| `delisted_security_history` | **BLOCKED_EXTERNAL** | no approved real source |
| `corporate_actions` | **BLOCKED_EXTERNAL** | no approved real source |
| six IBKR historical capabilities | `EMPIRICALLY_VERIFIED` | authenticated `POI-IBKR-HIST.json` |

`full_dataset_contract_ready = false`. No snapshot can reach
`BACKTEST_ELIGIBLE`; the maximum reachable readiness is `CONTRACT_ONLY`.

**Remaining blocker:** an owner decision approving (and licensing) at least one
real historical source per capability. Until then these four cells stay blocked —
by design, not by omission.
