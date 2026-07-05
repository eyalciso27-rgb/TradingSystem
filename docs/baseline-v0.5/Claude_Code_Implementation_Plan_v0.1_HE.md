תאריך: 5 ביולי 2026

**סטטוס: APPROVED FOR CONTROLLED IMPLEMENTATION AFTER P0 PoI GATES**

| **התכנית אינה Prompt יחיד ל-Claude Code. כל Task נמסר בנפרד, עם Dependencies, Scope, Deliverables ו-Done Criteria. Claude Code אינו רשאי לבחור Architecture חלופית, להסיר User Adapters מחייבים, לעקוף Risk או לחבר Live.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# **1. כללי הפעלה מול Claude Code**

> **•** משימה אחת או Work Package קטן בכל פעם; אין “בנה את כל המערכת”.
>
> **•** לפני התחלה: Claude Code קורא את מסמכי הסמכות וה-Contracts הרלוונטיים בלבד.
>
> **•** אין Commit/Push ללא אישור המשתמש בהתאם להעדפה הקיימת.
>
> **•** כל Task כולל Tests; Done ללא Tests אינו מתקבל.
>
> **•** אין Secrets, חשבונות אמיתיים או Live credentials בסביבת פיתוח.
>
> **•** PoI code נפרד מ-Production code עד Review.
>
> **•** כל סטייה מה-Baseline נעצרת ומועברת ל-ADR/Change Request.

# **2. Phase Map**

| **Phase** | **Name**                                     | **Dependencies** | **Task count** |
|-----------|----------------------------------------------|------------------|----------------|
| PHASE-0   | Repository and Governance                    | \-               | 4              |
| PHASE-1   | Canonical Contracts and Configuration        | PHASE-0          | 5              |
| PHASE-2   | Platform Data and Messaging                  | PHASE-1          | 5              |
| PHASE-3   | Security, Identity, Audit and Sessions       | PHASE-2          | 5              |
| PHASE-4   | Infrastructure, Docker and Operations Core   | PHASE-3          | 5              |
| PHASE-5   | Proof-of-Integration Execution               | PHASE-4          | 3              |
| PHASE-6   | Production Source Adapters                   | PHASE-5          | 8              |
| PHASE-7   | Instrument, Market Data and Trading Calendar | PHASE-6          | 5              |
| PHASE-8   | Research and Signal Engines                  | PHASE-7          | 6              |
| PHASE-9   | Fusion, Decision, Risk and Portfolio         | PHASE-8          | 6              |
| PHASE-10  | Broker Execution and Position Management     | PHASE-9          | 6              |
| PHASE-11  | Operations Console                           | PHASE-10         | 7              |
| PHASE-12  | Replay, Backtesting and Calibration          | PHASE-11         | 6              |
| PHASE-13  | End-to-End Shadow and Paper Manual           | PHASE-12         | 4              |
| PHASE-14  | Restricted Paper Auto                        | PHASE-13         | 4              |
| PHASE-15  | Six-Month Paper Validation                   | PHASE-14         | 3              |

# **PHASE-0: Repository and Governance**

הקמת בסיס פרויקט, CI וכללי שינוי.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | None                                          |
| Exit condition | All 4 tasks DONE and phase regression passes. |

## **IMP-0001 - Create monorepo skeleton**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | apps, packages, infra, docs, tests; no business logic.              |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Repository builds on ARM64; lint/typecheck/test commands exist.     |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0002 - Add architecture guardrails**

| **Field**     | **Definition**                                                                  |
|---------------|---------------------------------------------------------------------------------|
| Scope         | ADR template, CODEOWNERS, forbidden import rules, environment naming.           |
| Deliverables  | Code + tests + documentation + migration/config where applicable.               |
| Done Criteria | CI rejects ibapi outside broker-ibkr and provider SDK leakage outside adapters. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.             |

## **IMP-0003 - Add baseline documents**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Store version manifest and contract authority map.                  |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Build records baseline hash and versions.                           |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0004 - Configure CI**

| **Field**     | **Definition**                                                            |
|---------------|---------------------------------------------------------------------------|
| Scope         | Python/Node lint, tests, schema validation, container build, secret scan. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.         |
| Done Criteria | Clean clone passes all CI on PR.                                          |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.       |

# **PHASE-1: Canonical Contracts and Configuration**

יצירת חוזים גרסאיים לפני שירותים.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-0                                       |
| Exit condition | All 5 tasks DONE and phase regression passes. |

## **IMP-0101 - Define canonical enums**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Entry, outcome, order, position, health, evidence, access modes.    |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | JSON schemas and Python/TS generated types match.                   |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0102 - Define event envelope**

| **Field**     | **Definition**                                                                         |
|---------------|----------------------------------------------------------------------------------------|
| Scope         | event_id, type, version, occurred/observed/available/ingested, correlation, causation. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.                      |
| Done Criteria | Contract tests reject missing point-in-time fields.                                    |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.                    |

## **IMP-0103 - Define source and social contracts**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Canonical social message + observations; user/bot modes.            |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Cross-mode examples validate and dedup key is explicit.             |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0104 - Define decision/risk/order contracts**

| **Field**     | **Definition**                                                             |
|---------------|----------------------------------------------------------------------------|
| Scope         | Candidate, assessments, trade plan, order intent, normalized broker event. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.          |
| Done Criteria | No broker-specific field in domain schemas.                                |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.        |

## **IMP-0105 - Implement versioned config loader**

| **Field**     | **Definition**                                                        |
|---------------|-----------------------------------------------------------------------|
| Scope         | Policy/strategy/provider/source active pointers and validation.       |
| Deliverables  | Code + tests + documentation + migration/config where applicable.     |
| Done Criteria | Invalid config fails startup; rollback changes pointer without build. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.   |

# **PHASE-2: Platform Data and Messaging**

PostgreSQL, NATS, outbox/inbox, migrations and storage.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-1                                       |
| Exit condition | All 5 tasks DONE and phase regression passes. |

## **IMP-0201 - Create PostgreSQL schema core**

| **Field**     | **Definition**                                                                   |
|---------------|----------------------------------------------------------------------------------|
| Scope         | Instrument, evidence, candidates, decisions, orders, fills, positions, versions. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.                |
| Done Criteria | Alembic migration applies/rolls back on empty DB.                                |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.              |

## **IMP-0202 - Implement point-in-time storage**

| **Field**     | **Definition**                                                             |
|---------------|----------------------------------------------------------------------------|
| Scope         | observed/effective/available/ingested/superseded fields and as-of queries. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.          |
| Done Criteria | Replay test proves no future record is visible.                            |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.        |

## **IMP-0203 - Implement transactional outbox**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Write domain state and outbox atomically.                           |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Crash after commit produces exactly one logical event.              |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0204 - Implement consumer inbox**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Idempotent processing at every service boundary.                    |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Duplicate deliveries do not duplicate state/orders.                 |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0205 - Implement object storage port**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Content-addressed raw evidence/attachments/artifacts.               |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Hash verification and retention tests pass.                         |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-3: Security, Identity, Audit and Sessions**

RBAC, secrets, sessions, tamper evidence.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-2                                       |
| Exit condition | All 5 tasks DONE and phase regression passes. |

## **IMP-0301 - Implement authentication and RBAC**

| **Field**     | **Definition**                                                         |
|---------------|------------------------------------------------------------------------|
| Scope         | OWNER/OPERATOR/VIEWER/SERVICE roles with MFA hooks.                    |
| Deliverables  | Code + tests + documentation + migration/config where applicable.      |
| Done Criteria | Unauthorized approval/policy/emergency actions are denied and audited. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.    |

## **IMP-0302 - Implement static secret loading**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | SOPS/age runtime file secrets; no secrets in images/env dumps.      |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Secret scan and log tests pass.                                     |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0303 - Implement runtime session store**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Per-connector encrypted session blobs, rotate/revoke/re-auth.       |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | One connector session can be revoked without affecting others.      |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0304 - Implement tamper-evident audit**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Append-only events, hash chain, offsite checkpoints.                |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Mutation breaks verification; checkpoint restore verifies chain.    |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0305 - Implement trust/quarantine service**

| **Field**     | **Definition**                                                              |
|---------------|-----------------------------------------------------------------------------|
| Scope         | Untrusted content, attachments, prompt injection markers, content clusters. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.           |
| Done Criteria | Malicious fixture cannot call tools or reach execution.                     |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.         |

# **PHASE-4: Infrastructure, Docker and Operations Core**

Compose topology, networks, observability, backup, emergency.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-3                                       |
| Exit condition | All 5 tasks DONE and phase regression passes. |

## **IMP-0401 - Create Docker Compose stacks**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | dev, test, paper with isolated networks and volumes.                |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Only approved endpoints exposed; all services healthchecked.        |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0402 - Implement observability**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | OTel, Prometheus, Grafana, Loki/Alloy, alerts.                      |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Source-to-decision trace and secret redaction tests pass.           |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0403 - Implement backup and restore**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Postgres, NATS, object/session storage encrypted offsite.           |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Restore drill meets documented RPO/RTO.                             |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0404 - Implement emergency control service**

| **Field**     | **Definition**                                                         |
|---------------|------------------------------------------------------------------------|
| Scope         | Global/account/strategy/source halts; persistent state.                |
| Deliverables  | Code + tests + documentation + migration/config where applicable.      |
| Done Criteria | Halt blocks queued and pre-submit entries, preserves protective exits. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.    |

## **IMP-0405 - Implement notification service**

| **Field**     | **Definition**                                                        |
|---------------|-----------------------------------------------------------------------|
| Scope         | UI + Telegram private alerts, dedup/escalation/ack.                   |
| Deliverables  | Code + tests + documentation + migration/config where applicable.     |
| Done Criteria | Critical test alert delivered and correlated without remote commands. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.   |

# **PHASE-5: Proof-of-Integration Execution**

ביצוע Backlog ה-PoI לפני Production adapters.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-4                                       |
| Exit condition | All 3 tasks DONE and phase regression passes. |

## **IMP-0501 - Execute all P0 PoIs**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Run backlog, capture evidence, issue ADRs.                          |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Every P0 item PASS/approved alternate ADR.                          |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0502 - Execute P1 PoIs**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Telegram Bot, TradingView, observability refinements.               |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Each item has decision and fixtures.                                |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0503 - Freeze provider/runtime choices**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Update config catalogs and capability matrices.                     |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No unresolved provider assumption remains in implementation tasks.  |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-6: Production Source Adapters**

מימוש מקורות לאחר PoI.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-5                                       |
| Exit condition | All 8 tasks DONE and phase regression passes. |

## **IMP-0601 - Build WhatsApp user adapter**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Required user session, allowlist, events, health, re-auth.          |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Contract/integration tests from PoI pass in production adapter.     |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0602 - Build Discord user adapter**

| **Field**     | **Definition**                                                       |
|---------------|----------------------------------------------------------------------|
| Scope         | Required user session, channels/threads, edits, attachments, resume. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.    |
| Done Criteria | Contract/integration tests pass; no bot-only substitution.           |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.  |

## **IMP-0603 - Build Telegram user adapter**

| **Field**     | **Definition**                                                       |
|---------------|----------------------------------------------------------------------|
| Scope         | Required-primary TDLib user connector.                               |
| Deliverables  | Code + tests + documentation + migration/config where applicable.    |
| Done Criteria | Login/QR/2FA, restart, allowlist, events and encrypted session pass. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.  |

## **IMP-0604 - Build Telegram bot adapter**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Supplemental Bot API ingestion and private notifications.           |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Works alongside user adapter and cannot satisfy user gate.          |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0605 - Implement cross-adapter social normalization**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Canonical messages, observations, revisions, clusters.              |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Same Telegram post via user/bot creates one candidate.              |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0606 - Build InvestingPro adapter**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Parser canary, field catalog, raw snapshots, circuit breaker.       |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Schema drift causes alert/circuit, never silent defaults.           |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0607 - Build news/filings adapters**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Official source, news provider, corrections, clustering.            |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Point-in-time and independent-source tests pass.                    |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0608 - Build TradingView adapter**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Authenticated webhook external signal only.                         |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No direct order path; replay/idempotency pass.                      |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-7: Instrument, Market Data and Trading Calendar**

נתוני שוק, זיהוי נכסים ופעולות תאגידיות.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-6                                       |
| Exit condition | All 5 tasks DONE and phase regression passes. |

## **IMP-0701 - Implement instrument master**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Internal IDs and provider/broker mappings.                          |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Ambiguous symbol requires review; mappings versioned.               |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0702 - Implement market data provider port**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Quotes, bars, fundamentals metadata and health.                     |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Primary/fallback and freshness policies enforced.                   |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0703 - Implement feature data snapshots**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | ATR, ADV, spread, relative volume and point-in-time snapshots.      |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Feature replay deterministic.                                       |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0704 - Implement trading calendar**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | America/New_York, DST, holidays, half days, RTH windows.            |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Known calendar fixtures pass.                                       |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0705 - Implement corporate action workflow**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Splits, symbol changes, mergers, delisting and halts.               |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Unresolved action blocks affected instrument only.                  |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-8: Research and Signal Engines**

Technical, fundamental, news/NLP, quant and external/social assessments.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-7                                       |
| Exit condition | All 6 tasks DONE and phase regression passes. |

## **IMP-0801 - Technical engine v1**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Breakout, volume, trend, ATR, support/resistance features.          |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Golden market fixtures produce expected assessments.                |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0802 - Fundamental engine v1**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Quality, growth, valuation and financial risk scores.               |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Field lineage and missing-data behavior pass.                       |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0803 - News/NLP engine v1**

| **Field**     | **Definition**                                                        |
|---------------|-----------------------------------------------------------------------|
| Scope         | Entity, catalyst, materiality, source class, sentiment and revisions. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.     |
| Done Criteria | Structured output and adversarial tests pass.                         |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.   |

## **IMP-0804 - AI extraction tasks**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Entity/instruction/context/critic with schema validation.           |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Golden-set thresholds and fallback behavior pass.                   |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0805 - Market regime engine v1**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Trend/range/volatility/risk-on/off/unknown.                         |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Unknown handled explicitly; no look-ahead.                          |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0806 - Quant/ML registry scaffolding**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Shadow-only models, artifacts, calibration status and drift.        |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No unapproved model can become active.                              |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-9: Fusion, Decision, Risk and Portfolio**

ליבת החלטה דטרמיניסטית.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-8                                       |
| Exit condition | All 6 tasks DONE and phase regression passes. |

## **IMP-0901 - Implement strategy manifest evaluator**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | REQUIRED/VETO/SOFT/BONUS/CONTEXT per strategy.                      |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No global all-model agreement gate.                                 |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0902 - Implement signal fusion**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Assessments, provenance, conflicts, score/action tier.              |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Duplicate source observations do not double count.                  |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0903 - Implement deterministic decision engine**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Outcomes and action tiers with explanation.                         |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Same snapshot/version produces same decision.                       |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0904 - Implement risk engine**

| **Field**     | **Definition**                                                             |
|---------------|----------------------------------------------------------------------------|
| Scope         | Canonical risk formula, loss/exposure limits, veto and quantity reduction. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.          |
| Done Criteria | Boundary/property tests pass.                                              |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.        |

## **IMP-0905 - Implement portfolio allocator**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Aggregate risk, sector/theme/correlation/source/strategy caps.      |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Concurrent candidate tests produce deterministic allocation.        |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-0906 - Implement final pre-submit check**

| **Field**     | **Definition**                                                       |
|---------------|----------------------------------------------------------------------|
| Scope         | Refresh market/account/approval/kill/risk immediately before submit. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.    |
| Done Criteria | Price/risk/kill changes invalidate or resize safely.                 |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.  |

# **PHASE-10: Broker Execution and Position Management**

IBKR Paper, protection, exit coordination and reconciliation.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-9                                       |
| Exit condition | All 6 tasks DONE and phase regression passes. |

## **IMP-1001 - Implement broker core port and conformance suite**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Capabilities, order mapping, normalized events.                     |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Mock adapter passes suite; no IBKR leakage.                         |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1002 - Implement IBKR Paper adapter**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Connection, accounts, orders, events and reconciliation.            |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | PoI fixtures and conformance suite pass.                            |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1003 - Implement execution agent**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Queue consume, idempotency, kill check, submit/cancel/modify.       |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No blind retry on unknown state.                                    |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1004 - Implement position coordinator**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Single writer for stops, targets, reductions and exits.             |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Concurrent exit triggers produce one coherent action plan.          |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1005 - Implement broker-side protection**

| **Field**     | **Definition**                                                          |
|---------------|-------------------------------------------------------------------------|
| Scope         | Protect every fill, adjust on partial fills, OCA/bracket by capability. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.       |
| Done Criteria | No required position remains unprotected.                               |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.     |

## **IMP-1006 - Implement reconciliation**

| **Field**     | **Definition**                                                        |
|---------------|-----------------------------------------------------------------------|
| Scope         | Startup/periodic/event-driven account-order-position reconciliation.  |
| Deliverables  | Code + tests + documentation + migration/config where applicable.     |
| Done Criteria | Mismatch creates controlled state and blocks new entries if required. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.   |

# **PHASE-11: Operations Console**

ממשק מלא להפעלה, החלטות, נתונים וחירום.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-10                                      |
| Exit condition | All 7 tasks DONE and phase regression passes. |

## **IMP-1101 - Build dashboard shell**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Mode, health, P&L, risk, positions, alerts, halt banner.            |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Responsive authenticated UI with live SSE updates.                  |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1102 - Build source connections screens**

| **Field**     | **Definition**                                                                 |
|---------------|--------------------------------------------------------------------------------|
| Scope         | WhatsApp/Discord/Telegram user, Telegram bot, InvestingPro, news, market data. |
| Deliverables  | Code + tests + documentation + migration/config where applicable.              |
| Done Criteria | Capability/runtime status separated; re-auth flows available.                  |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.            |

## **IMP-1103 - Build candidate workspace**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Evidence, model cards, fusion, vetoes, plan and freshness.          |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Every decision is explainable from UI.                              |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1104 - Build approval queue**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Approve/reject/watch/reanalyse/reduce quantity with expiry.         |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Expired/materially changed plan requires refresh.                   |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1105 - Build positions/orders/reconciliation**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Stops, targets, fills, order states and mismatches.                 |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | UI mirrors canonical state, not broker raw status.                  |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1106 - Build emergency controls**

| **Field**     | **Definition**                                                                            |
|---------------|-------------------------------------------------------------------------------------------|
| Scope         | Global halt and separate flatten preview/reauth.                                          |
| Deliverables  | Code + tests + documentation + migration/config where applicable.                         |
| Done Criteria | Halt drill passes; flatten remains disabled in initial release unless explicitly enabled. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.                       |

## **IMP-1107 - Build research/calibration screens**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Funnel, starvation, challengers, backtests, models.                 |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No UI action auto-promotes policy/model.                            |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-12: Replay, Backtesting and Calibration**

מחקר ללא Look-ahead ומניעת קשיחות/Overtrading.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-11                                      |
| Exit condition | All 6 tasks DONE and phase regression passes. |

## **IMP-1201 - Implement replay runner**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | As-of snapshots, exact versions and deterministic event ordering.   |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Repeated replay yields identical results.                           |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1202 - Implement backtest engine**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Costs, spread, slippage, fill assumptions and walk-forward splits.  |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Look-ahead and leakage tests pass.                                  |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1203 - Implement opportunity funnel**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Stage counts and rejection reason attribution.                      |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Every candidate has terminal/stage history.                         |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1204 - Implement counterfactual shadow**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Track rejected/watch/manual candidates with virtual outcomes.       |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No counterfactual event affects live/paper state.                   |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1205 - Implement champion/challenger**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Same events, isolated state, comparison report.                     |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Challenger cannot execute or change active pointer.                 |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1206 - Implement policy change workflow**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Proposal, replay, shadow, approval and rollback.                    |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No direct edit of active immutable version.                         |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-13: End-to-End Shadow and Paper Manual**

הפעלת מערכת מלאה ללא אוטומציה חופשית.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-12                                      |
| Exit condition | All 4 tasks DONE and phase regression passes. |

## **IMP-1301 - Run E2E shadow scenarios**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | All source types, independent discovery, failures and recovery.     |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Traceability acceptance suite passes.                               |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1302 - Run Paper Manual**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | User approves complete plans; execution/protection/reconciliation.  |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Zero duplicate orders and zero required unprotected positions.      |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1303 - Run chaos and recovery drills**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Broker/data/source/db/NATS/UI outages and restarts.                 |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Documented safe behavior and runbooks pass.                         |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1304 - Close operational defects**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Resolve P0/P1 defects and rerun regression.                         |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | No open critical/high safety defect.                                |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-14: Restricted Paper Auto**

אוטומציה מוגבלת לאחר Gates בלבד.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-13                                      |
| Exit condition | All 4 tasks DONE and phase regression passes. |

## **IMP-1401 - Select one eligible strategy**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Meets promotion criteria and has approved policy.                   |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Explicit owner approval recorded.                                   |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1402 - Enable restricted auto scope**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Allowlist, lower risk, max positions, probation period.             |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Config cannot exceed approved envelope.                             |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1403 - Monitor and auto-demote**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Safety incidents, drawdown, data/broker errors and drift.           |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Any demotion condition halts new auto entries.                      |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1404 - Complete probation review**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Compare decisions, execution and operational reliability.           |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Continue, modify or demote decision documented.                     |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

# **PHASE-15: Six-Month Paper Validation**

איסוף ראיות לפני כל דיון ב-Live.

| **Field**      | **Value**                                     |
|----------------|-----------------------------------------------|
| Dependencies   | PHASE-14                                      |
| Exit condition | All 3 tasks DONE and phase regression passes. |

## **IMP-1501 - Operate for minimum six months**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Daily/weekly/monthly reviews and immutable records.                 |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Six-month evidence window complete.                                 |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1502 - Evaluate strategy and operations**

| **Field**     | **Definition**                                                      |
|---------------|---------------------------------------------------------------------|
| Scope         | Expectancy, drawdown, regimes, execution, safety, calibration.      |
| Deliverables  | Code + tests + documentation + migration/config where applicable.   |
| Done Criteria | Report separates decision quality from luck/data/execution.         |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs. |

## **IMP-1503 - Produce future Live readiness recommendation**

| **Field**     | **Definition**                                                          |
|---------------|-------------------------------------------------------------------------|
| Scope         | No automatic activation; separate policy and legal/broker review.       |
| Deliverables  | Code + tests + documentation + migration/config where applicable.       |
| Done Criteria | Owner receives GO/NO-GO/RESEARCH recommendation; Live remains disabled. |
| Forbidden     | No unapproved architecture change; no Live; no secret in code/logs.     |

# **3. Global Quality Gates**

| **Gate** | **Requirement**                                                                               |
|----------|-----------------------------------------------------------------------------------------------|
| QG-01    | All canonical schemas validate and generated Python/TypeScript types are synchronized.        |
| QG-02    | Unit, contract, integration and safety tests pass on ARM64 CI or approved target.             |
| QG-03    | No critical/high security or trading-safety defect remains open at phase exit.                |
| QG-04    | No duplicate order, blind retry or unprotected required position.                             |
| QG-05    | All external content remains untrusted and isolated from execution.                           |
| QG-06    | WhatsApp User, Discord User and Telegram User are never replaced by Bot-only implementations. |
| QG-07    | Traceability Matrix is updated for every changed requirement.                                 |

# **4. Initial handoff order**

> **1.** IMP-0001 through IMP-0004
>
> **2.** IMP-0101 through IMP-0105
>
> **3.** P0 Proof-of-Integration work packages
>
> **4.** Platform/Security phases only after PoI prerequisites
>
> **5.** Production adapters only after their individual PoI PASS
