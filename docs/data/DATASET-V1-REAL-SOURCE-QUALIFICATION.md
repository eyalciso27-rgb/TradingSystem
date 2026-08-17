# Dataset V1 — Real historical source qualification runbook

> How the four external Dataset V1 capabilities become `EMPIRICALLY_VERIFIED`,
> and the current honest status. Companion to
> [`DATASET-V1-CONTRACT.md`](DATASET-V1-CONTRACT.md) and
> [`DATASET-V1-CAPABILITY-MATRIX.yaml`](DATASET-V1-CAPABILITY-MATRIX.yaml).

## Owner source decision (2026-08-17)

**IBKR is the primary approved data source**, and FREE, PUBLIC, LEGALLY
ACCESSIBLE official sources may supplement capabilities IBKR cannot supply. No
paid provider may be integrated without a further owner decision.

Approved supplementary sources: **SEC EDGAR / data.sec.gov**, and official public
S&P Dow Jones Indices announcements where legally and technically available.

### Source authority levels

| Authority | Meaning | Used for |
|---|---|---|
| `OWNER_PRIMARY` | the owner's approved IBKR environment | prices, conId identity, adjustment behaviour |
| `OFFICIAL_PUBLIC` | regulator / index-owner official material | SEC filings, S&P announcements |
| `PUBLIC_SECONDARY` | free third-party dataset | only when labelled honestly; never as "official" |
| `FIXTURE` / `SNAPSHOT_ONLY` | test data / today-only state | never promotable |

Reconciliation across sources is deterministic: agreement corroborates; a
**strictly higher** authority resolves a disagreement (and the disagreement is
still recorded); **equal-authority disagreement stays UNRESOLVED** (fail closed).

### What each source can and cannot supply

| Capability | IBKR (primary) | SEC EDGAR | S&P public | Status |
|---|---|---|---|---|
| `delisted_security_history` | conId/contract identity; price history disappears after delisting | **Form 25 / 25-NSE / 15-12B** evidence delisting/deregistration with filing + acceptance time | — | engineering complete; **pending owner run** |
| `corporate_actions` | TRADES vs ADJUSTED_LAST characterises adjustment; event feed may need an entitlement (**detected, never assumed**) | filings evidence mergers/reorganisations | — | partial by design; **pending owner run** |
| `pit_sp500_membership` | not provided | — | official change announcements carry announced + effective dates | **pending**: needs owner-supplied announcement records |
| `asof_gics` | classification history not exposed | does not provide company-level GICS | taxonomy ≠ company classification | **BLOCKED** — no free official company-level historical GICS |

**Critical boundary:** an adjusted-price divergence (TRADES vs ADJUSTED_LAST)
proves *a provider adjustment happened*. It is **not** event-level corporate-action
evidence, and `AdjustmentCrossCheck` records that inference boundary in the
artifact so the two can never be conflated.

## Earlier discovery — result as of 2026-08-17

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

### What the runner does and does NOT do

The `SOURCE QUALIFICATION` block reports **access/configuration state, not
capability qualification**:

| Line | What it actually means |
|---|---|
| `IBKR ... GATEWAY_REACHABLE` | a TCP connection to the paper port succeeded — nothing more |
| `SEC EDGAR ... USER_AGENT_CONFIGURED` | `SEC_EDGAR_USER_AGENT` is set. **The runner issues no SEC request, performs no ingestion and runs no SEC qualification.** |
| `PREFLIGHT_TOOL_UNAVAILABLE` | no TCP probe exists on this host. This is a **tooling gap**, not a statement about the Gateway |

Capability states come only from the authenticated evidence artifacts. Reading
`USER_AGENT_CONFIGURED` as "SEC data was ingested" would be wrong.

### Gateway reachability preflight

The probe is layered and dependency-free: bash's own `/dev/tcp` first, then
Python's stdlib socket, then `nc`. Each returns *open*, *closed*, or
**unavailable** — and an unavailable probe defers to the next one instead of
voting. A missing or broken tool can therefore never be reported as an
unreachable Gateway. Port **4001 (live) is rejected before any connection is
attempted** and is never contacted.

Overrides: `IB_GATEWAY_HOST`, `IB_GATEWAY_PORT` (paper only), `IB_GATEWAY_PROBE_TIMEOUT`.

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

## Real source execution (what the runner actually does)

One owner command performs the whole pipeline:

```bash
export SEC_EDGAR_USER_AGENT='Your Name your.email@example.com'
./scripts/run_dataset_v1_real_qualification.sh
```

Stages: preflight -> IBKR read-only qualification (historical + corporate) ->
SEC discovery/ingestion/qualification -> S&P official membership ingestion ->
identity crosswalk -> evidence generation -> evidence verification -> capability
promotion -> readiness -> snapshot attempt.

The report ends with **SOURCE EXECUTION**, which describes what each source
*produced on that run*. It is fed by real exit codes, not by configuration:

| Status | Meaning |
|---|---|
| `PASS` | real authenticated evidence, promotion eligible |
| `PARTIAL` | real evidence written, genuinely incomplete (sub-capabilities listed) |
| `FAIL` | the stage ran and produced nothing usable |
| `NOT_AVAILABLE` | no legally/technically reproducible path on this host |
| `NOT_RUN` | the gate was not satisfied (e.g. no SEC User-Agent) |

`SEC_EDGAR_USER_AGENT is set` is a **gate**, never a result.

### SEC EDGAR - bounded, compliant ingestion

Delisted names are **discovered, not guessed**: the runner fetches ONE quarter of
SEC's official catalogue (`full-index/<year>/QTR<q>/master.idx`), selects a
deterministic, capped slice of real Form 25 / 25-NSE filings, then fetches those
filers' submissions. Identity comes from SEC's own payload (`cik`, `name`,
`tickers`, `exchanges`, `formerNames`), so no local guess can enter the dataset.

* Declared User-Agent with a real contact; **fails closed** without it.
* Rate-limited below SEC's published ceiling.
* Bounded: 1 index + `DATASET_V1_SEC_LIMIT` submission requests (default 5).
* The index is **not committed** - only its sha256 and a reproducible URL.

`security_id` is `sec-cik:<10-digit CIK>` - stable, not a ticker, and it survives
delisting. **A delisted name stays resolvable with no current IBKR contract.**

Form 25's statutory effective date is **never computed**. The metadata publishes
a filing date and an acceptance time; `delisting_effective_date_published`
reports honestly whether a real effective date was found.

### IBKR - what it does and does not establish

`poi_corporate_actions_qualification` compares TRADES vs ADJUSTED_LAST at
**matched bar dates only** (comparing different days would manufacture a
divergence). This characterises **adjustment behaviour**. It is not an event
ledger: no event type, no ratio, no ex-date. Entitlement messages are classified
as `ENTITLEMENT_REQUIRED`, never as a capability failure and never as
availability; a clean-but-empty response is `UNAVAILABLE`, not `AVAILABLE`.

The event-level corporate-action feed is **probed, not assumed**: no such
endpoint is reachable through the read-only historical API surface used here.
That is recorded as observed, and qualification continues with SEC.

### S&P DJI - official documents, no scraping

Automated retrieval is blocked by the publisher: a plain automated request to
`spglobal.com` returns **HTTP 403, including for `robots.txt`**. Circumventing
bot protection is not permitted, so membership is ingested from OFFICIAL
documents the owner supplies through
[`sp500-announcements.example.json`](../../config/data-sources/sp500-announcements.example.json),
each bound to a recomputed sha256.

Anti-leak rules enforced at load time: the baseline **must** be anchored at the
window start (a later baseline is refused as current-universe projection), every
announcement must sit inside the declared window, and an effect may not precede
its announcement. PIT replay honours both axes - a change announced after the
knowledge time is invisible.

## Evidence artifact types (added after the owner-host run)

The first real owner-host run failed with

```
EVIDENCE_INVALID docs/poi/evidence/dataset-v1/identity-crosswalk.json
evidence is missing a sha256 normalized_checksum
```

on a perfectly valid crosswalk: the verifier globbed `*.json` and applied the
qualification-evidence schema to everything in the directory. The fix is
**classification, not relaxation** - every type keeps its own integrity rules.

| `artifact_type` | Written by | Verified with | Promotable |
|---|---|---|---|
| `qualification_evidence` | the qualification harness | evidence checksum over the canonical body | **yes - the only one** |
| `identity_crosswalk` | the crosswalk stage | its own canonical checksum | no |
| `snapshot_report` | the snapshot stage | build-time bindings | no |
| `auxiliary_data_quality_report` | coverage/candidate reports | its own checksum | no |
| `source_manifest` | the owner (S&P documents) | per-document sha256 | no |

Artifacts self-declare `artifact_type`. Files written before the field existed
are still recognised by a narrow structural fallback that can only *recognise*,
never invent; anything unrecognisable is reported as `UNCLASSIFIED` and fails
closed rather than being forced through the wrong schema.

**A tampered auxiliary artifact still fails its own validation** - it simply
fails as a crosswalk rather than as evidence.

### Identity crosswalk integrity

`identity-crosswalk.json` is a first-class authenticated artifact: canonical
serialization (sorted throughout, so input order cannot change the digest),
`schema_version`, source bindings with real sha256 digests, identity metrics,
and a checksum recomputed on load. The snapshot **binds and re-verifies it**,
and refuses to assert any `security_id` the crosswalk did not resolve. A
tampered crosswalk invalidates the snapshot build.

## Form 25 primary documents

The owner run proved 36 delisting filings but `delisting_effective_date_published`
was 0/36: the submissions metadata carries a filing date and an acceptance time,
never a delisting effective date. That date, when published at all, is in the
primary document.

The runner now retrieves each selected Form 25 / 25-NSE primary document and
extracts only what the document explicitly publishes:

- effective delisting date, last trading date, issuer, exchange, rule provision;
- the **raw document checksum**, plus per-field provenance recording the exact
  element and raw text each value came from, so any parse can be audited;
- **nothing is computed.** SEC Rule 12d2-2 makes a delisting effective ten days
  after filing, but that is a statutory rule, not a published per-filing fact.
  A filing without an explicit date yields `None` and the sub-capability stays
  PARTIAL.

An unrecognised document is **diagnosed, not guessed**: the observed element
names are written to `form25-coverage.json` so the allow-list can be widened
from real documents rather than from assumptions about the schema. Documents
declaring a DTD/ENTITY are refused outright.

## Corporate actions - event types and the required set

| Sub-capability | Source | Status |
|---|---|---|
| `event_delisting` | SEC Form 25 / 25-NSE | proven on the owner host (36 events) |
| `event_split` / `event_reverse_split` | SEC **XBRL** `StockholdersEquityNoteStockSplitConversionRatio1` | attempted per filer |
| `event_dividend` | SEC **XBRL** `CommonStockDividendsPerShareDeclared` | attempted per filer |
| `event_merger` / `event_acquisition` / `event_spinoff` / `event_symbol_change` | no free structured official source | unproven |
| `adjustment_behaviour_characterised` | IBKR TRADES vs ADJUSTED_LAST | corroboration only |
| `ibkr_event_feed_available` | IBKR probe | UNAVAILABLE on the owner account |

XBRL is SEC's own **structured** data: each fact carries a value, a period, the
accession that reported it and the date it was filed. A concept a filer never
reported returns HTTP 404 and is recorded as unavailable - never invented. A
dividend's **ex-date is not published by XBRL**, so `effective_on` stays unset
and the period is recorded as provenance instead.

8-K item codes are published metadata but are **ambiguous** about what occurred
(item 2.01 covers an acquisition *or* a disposition). They are written to
`corporate-action-candidates.json` as review leads and **qualify nothing**.

`corporate_actions` may only PASS when every event type in the production
contract's **required set** (split, reverse split, dividend, merger, symbol
change, delisting) is proven with event-level evidence. Proving delisting alone
is a genuine PARTIAL.

### IBKR/SEC reconciliation

Where SEC reports a price-affecting event and IBKR shows an adjustment
divergence, corroboration is recorded. Where they disagree, the disagreement is
recorded explicitly and **never silently resolved**. IBKR observations never
create an event: the event count is identical with and without them.

## Current status


| Capability | State | Why |
|---|---|---|
| `pit_sp500_membership` | **BLOCKED_EXTERNAL** | adapter ready; needs owner-supplied official S&P announcement records |
| `asof_gics` | **BLOCKED_EXTERNAL** | no free official company-level historical GICS source exists - see below |
| `delisted_security_history` | **BLOCKED_EXTERNAL** | SEC adapter ready; **pending owner run** (`SEC_EDGAR_USER_AGENT` + IB Gateway) |
| `corporate_actions` | **BLOCKED_EXTERNAL** | IBKR probe + SEC adapter ready; **pending owner run** |
| six IBKR historical capabilities | `EMPIRICALLY_VERIFIED` | authenticated `POI-IBKR-HIST.json` |

`full_dataset_contract_ready = false`. No snapshot can reach
`BACKTEST_ELIGIBLE`; the maximum reachable readiness is `CONTRACT_ONLY`.

**Remaining blocker is now EXECUTION, not a decision.** The owner decision is
recorded and the adapters exist. Two host-side inputs are required, neither of
which may be fabricated here:

1. `export SEC_EDGAR_USER_AGENT='Your Name your.email@example.com'` — SEC
   automated access requires a **real contact**; the client fails closed without
   it and this repository will not invent one.
2. A running **paper** IB Gateway on `127.0.0.1:4002` (port 4001/live is refused).

Then run `./scripts/run_dataset_v1_real_qualification.sh`. Cells flip only where
the resulting authenticated evidence genuinely passes.

`asof_gics` additionally needs an owner decision, because no free official
company-level historical GICS source is known to exist — closing it would require
a licensed provider, which is outside the current approval.

## as-of GICS - the exact missing requirement

Still `BLOCKED_EXTERNAL`, and deliberately not worked around. What is missing is a
**company-level HISTORICAL GICS series**: security -> GICS code -> effective date
-> knowledge date.

Checked and rejected:

* **IBKR** exposes no classification history through the read-only API;
* **SEC** assigns **SIC**, not GICS. SIC is a different taxonomy and is not a
  GICS substitute;
* **S&P DJI** publishes the GICS *structure* (the code taxonomy) publicly, but
  not free company-level historical assignments.

Deriving history from current GICS, current sector, or a business description
would fabricate the series, so it is refused. Closing this cell requires a
licensed provider - an owner decision, not more engineering.
