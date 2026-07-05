תאריך: 5 ביולי 2026

**סטטוס: APPROVED BACKLOG FOR PHASE 1**

| **כל משימת PoI היא Spike מוגבל בזמן. תפקידה להכריע יכולת, ספרייה, Provider או Host. אסור להפוך Spike לקוד Production ללא Review. כישלון אינו נעקף באמצעות הסרת דרישה מחייבת; הוא יוצר ADR לחלופה.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# **1. כללי ביצוע**

> **•** Branch נפרד לכל PoI.
>
> **•** אין שימוש בחשבון Live.
>
> **•** Secrets אינם נשמרים ב-Git.
>
> **•** כל PoI מסתיים ב-PASS, FAIL או CONDITIONAL PASS וב-ADR.
>
> **•** P0 קודם ל-P1.
>
> **•** Telegram User, WhatsApp User ו-Discord User הם Gates מחייבים; Bot-only אינו Pass.

# **2. מטריצת Backlog**

| **ID**           | **Priority** | **Area**         | **Title**                               | **Depends**                        | **Timebox** |
|------------------|--------------|------------------|-----------------------------------------|------------------------------------|-------------|
| POI-PLAT-ARM-001 | P0           | Platform         | ARM64 Docker stack                      | \-                                 | 2 days      |
| POI-SEC-SESS-002 | P0           | Security         | Encrypted Runtime Session Store         | POI-PLAT-ARM-001                   | 3 days      |
| POI-SOC-WA-USER  | P0           | Social           | WhatsApp User Adapter                   | POI-SEC-SESS-002                   | 5 days      |
| POI-SOC-DC-USER  | P0           | Social           | Discord User Adapter                    | POI-SEC-SESS-002                   | 5 days      |
| POI-SOC-TG-USER  | P0           | Social           | Telegram User Adapter via TDLib         | POI-SEC-SESS-002, POI-PLAT-ARM-001 | 5 days      |
| POI-SOC-TG-BOT   | P1           | Social           | Telegram Bot API supplemental mode      | POI-PLAT-ARM-001                   | 3 days      |
| POI-SOC-TG-COEX  | P0           | Social/Data      | Telegram User/Bot coexistence and dedup | POI-SOC-TG-USER, POI-SOC-TG-BOT    | 2 days      |
| POI-MD-001       | P0           | Market Data      | Market data provider benchmark          | POI-PLAT-ARM-001                   | 5 days      |
| POI-IBKR-001     | P0           | Broker           | IBKR Paper execution                    | POI-PLAT-ARM-001, POI-MD-001       | 7 days      |
| POI-INVPRO-001   | P0           | Source           | InvestingPro parser and drift canary    | POI-PLAT-ARM-001, POI-SEC-SESS-002 | 5 days      |
| POI-NEWS-001     | P0           | News/Filings     | Official filings and news ingestion     | POI-PLAT-ARM-001                   | 5 days      |
| POI-TV-001       | P1           | External Signals | TradingView webhook                     | POI-PLAT-ARM-001                   | 2 days      |
| POI-AI-001       | P0           | AI               | Structured extraction benchmark         | \-                                 | 5 days      |
| POI-EMERG-001    | P0           | Operations       | Emergency halt path                     | POI-PLAT-ARM-001                   | 3 days      |
| POI-OBS-001      | P1           | Observability    | End-to-end telemetry and alerting       | POI-PLAT-ARM-001                   | 3 days      |

## **3.1 POI-PLAT-ARM-001 - ARM64 Docker stack**

| **Field**    | **Value**                                                                                     |
|--------------|-----------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                            |
| Area         | Platform                                                                                      |
| Dependencies | None                                                                                          |
| Timebox      | 2 days                                                                                        |
| Objective    | להוכיח שכל רכיבי הבסיס נבנים ורצים ב-Oracle ARM.                                              |
| PASS         | כל images נבנים arm64; services healthy; restart preserves volumes; no privileged containers. |
| FAIL         | רכיב ליבה חסר image או דורש emulation בלתי יציבה.                                             |
| Outputs      | compose-poi.yaml, image matrix, resource report, decision record                              |

### **Execution steps**

> **•** Build Python API/worker image
>
> **•** Build Node connector/UI image
>
> **•** Start PostgreSQL, NATS, observability
>
> **•** Run health and restart tests

## **3.2 POI-SEC-SESS-002 - Encrypted Runtime Session Store**

| **Field**    | **Value**                                                                                         |
|--------------|---------------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                                |
| Area         | Security                                                                                          |
| Dependencies | POI-PLAT-ARM-001                                                                                  |
| Timebox      | 3 days                                                                                            |
| Objective    | להוכיח אחסון והחזרת sessions דינמיים ללא חשיפה.                                                   |
| PASS         | Session survives restart; secrets absent from logs/DB/domain tables; single-session revoke works. |
| FAIL         | session plaintext, shared secret scope, or unrecoverable restart.                                 |
| Outputs      | session-store spike, threat notes, key rotation test                                              |

### **Execution steps**

> **•** Encrypt session at rest
>
> **•** Restart adapter
>
> **•** Rotate wrapping key
>
> **•** Revoke one session
>
> **•** Verify logs redaction

## **3.3 POI-SOC-WA-USER - WhatsApp User Adapter**

| **Field**    | **Value**                                                                                          |
|--------------|----------------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                                 |
| Area         | Social                                                                                             |
| Dependencies | POI-SEC-SESS-002                                                                                   |
| Timebox      | 5 days                                                                                             |
| Objective    | להוכיח קליטה באמצעות חשבון המשתמש כפי שסוכם.                                                       |
| PASS         | user session works; stable IDs/dedup; allowlist; reconnect; auth-required state; no broker access. |
| FAIL         | Bot-only solution, unstable IDs, uncontrolled gaps, or session not securable.                      |
| Outputs      | adapter recommendation, golden fixtures, PoI report, go/no-go                                      |

### **Execution steps**

> **•** Pair user session
>
> **•** Allowlist groups
>
> **•** Capture message/reply/attachment
>
> **•** Restart/reconnect
>
> **•** Simulate auth expiry
>
> **•** Measure gaps and IDs

## **3.4 POI-SOC-DC-USER - Discord User Adapter**

| **Field**    | **Value**                                                                                |
|--------------|------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                       |
| Area         | Social                                                                                   |
| Dependencies | POI-SEC-SESS-002                                                                         |
| Timebox      | 5 days                                                                                   |
| Objective    | להוכיח קריאת שרתים/ערוצים/Threads דרך חשבון המשתמש.                                      |
| PASS         | user-mode requirements pass; events normalized; resume/dedup reliable; no broker access. |
| FAIL         | Bot-only substitution or inability to secure/operate required user mode.                 |
| Outputs      | adapter recommendation, event fixtures, PoI report                                       |

### **Execution steps**

> **•** Authorize user session
>
> **•** Allowlist servers/channels
>
> **•** Capture edits/deletes/threads/attachments
>
> **•** Restart/resume
>
> **•** Auth expiry test

## **3.5 POI-SOC-TG-USER - Telegram User Adapter via TDLib**

| **Field**    | **Value**                                                                                                           |
|--------------|---------------------------------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                                                  |
| Area         | Social                                                                                                              |
| Dependencies | POI-SEC-SESS-002, POI-PLAT-ARM-001                                                                                  |
| Timebox      | 5 days                                                                                                              |
| Objective    | להוכיח את נתיב החובה והעיקרי דרך חשבון Telegram של המשתמש.                                                          |
| PASS         | TDLib user flow passes all events, restart, encrypted session, allowlist and health; Bot is not used as substitute. |
| FAIL         | only Bot API works, session insecure, or no viable isolated host.                                                   |
| Outputs      | TDLib adapter spike, host decision, fixtures, PoI report, go/no-go                                                  |

### **Execution steps**

> **•** Obtain api_id/api_hash
>
> **•** Login by phone/QR/2FA
>
> **•** List and allowlist chats/channels/topics
>
> **•** Capture messages/posts/edits/deletes/replies/forwards
>
> **•** Restart with encrypted TDLib DB
>
> **•** Re-auth and revoke
>
> **•** Build/run arm64 or document x86 host fallback

## **3.6 POI-SOC-TG-BOT - Telegram Bot API supplemental mode**

| **Field**    | **Value**                                                                         |
|--------------|-----------------------------------------------------------------------------------|
| Priority     | P1                                                                                |
| Area         | Social                                                                            |
| Dependencies | POI-PLAT-ARM-001                                                                  |
| Timebox      | 3 days                                                                            |
| Objective    | להוכיח Bot API כמסלול נוסף לקליטה/התראות.                                         |
| PASS         | Bot mode works where authorized; token isolated; no claim of replacing user mode. |
| FAIL         | token leakage, duplicate uncontrolled updates, or coupling to user requirement.   |
| Outputs      | bot adapter spike, permission matrix, notification test                           |

### **Execution steps**

> **•** Create dedicated bot
>
> **•** Test privacy/permissions
>
> **•** Webhook secret or long polling
>
> **•** Capture channel/group updates
>
> **•** Send private test alert

## **3.7 POI-SOC-TG-COEX - Telegram User/Bot coexistence and dedup**

| **Field**    | **Value**                                                                              |
|--------------|----------------------------------------------------------------------------------------|
| Priority     | P0                                                                                     |
| Area         | Social/Data                                                                            |
| Dependencies | POI-SOC-TG-USER, POI-SOC-TG-BOT                                                        |
| Timebox      | 2 days                                                                                 |
| Objective    | להוכיח קליטה מקבילה ללא Candidate כפול.                                                |
| PASS         | one canonical evidence/candidate; both observations retained; revisions deterministic. |
| FAIL         | double scoring, double candidate or lost provenance.                                   |
| Outputs      | dedup algorithm decision, test fixtures, contract examples                             |

### **Execution steps**

> **•** Ingest same post via both modes
>
> **•** Edit/delete from one/both modes
>
> **•** Verify canonical message and observations
>
> **•** Run content cluster test

## **3.8 POI-MD-001 - Market data provider benchmark**

| **Field**    | **Value**                                                                                             |
|--------------|-------------------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                                    |
| Area         | Market Data                                                                                           |
| Dependencies | POI-PLAT-ARM-001                                                                                      |
| Timebox      | 5 days                                                                                                |
| Objective    | לבחור primary/fallback ל-quotes, bars, corporate actions and calendar.                                |
| PASS         | field ownership matrix and provider decision satisfy FAST/STANDARD freshness and point-in-time needs. |
| FAIL         | missing required fields or no reliable timestamp/fallback.                                            |
| Outputs      | benchmark report, field ownership matrix, provider ADR                                                |

### **Execution steps**

> **•** Benchmark IBKR and external provider
>
> **•** Measure freshness/completeness
>
> **•** Validate symbols and timestamps
>
> **•** Test outage/fallback
>
> **•** Cost/limits review

## **3.9 POI-IBKR-001 - IBKR Paper execution**

| **Field**    | **Value**                                                                                                           |
|--------------|---------------------------------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                                                  |
| Area         | Broker                                                                                                              |
| Dependencies | POI-PLAT-ARM-001, POI-MD-001                                                                                        |
| Timebox      | 7 days                                                                                                              |
| Objective    | להוכיח account, orders, protection, fills and reconciliation in Paper.                                              |
| PASS         | no duplicate orders; protection follows fills; normalized events; recovery deterministic; host decision documented. |
| FAIL         | blind retry, unprotected fill, unstable gateway or domain leakage of IBKR fields.                                   |
| Outputs      | broker adapter spike, capability matrix, host ADR, reconciliation fixtures                                          |

### **Execution steps**

> **•** Connect TWS/IB Gateway
>
> **•** Account snapshot
>
> **•** Instrument mapping
>
> **•** Limit/stop/bracket
>
> **•** Partial fill/cancel
>
> **•** Disconnect/reconnect
>
> **•** Unknown-state reconciliation

## **3.10 POI-INVPRO-001 - InvestingPro parser and drift canary**

| **Field**    | **Value**                                                                                       |
|--------------|-------------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                              |
| Area         | Source                                                                                          |
| Dependencies | POI-PLAT-ARM-001, POI-SEC-SESS-002                                                              |
| Timebox      | 5 days                                                                                          |
| Objective    | להוכיח login, field extraction, raw snapshot and drift detection.                               |
| PASS         | no silent defaults; schema drift opens circuit; raw snapshots retained; field catalog approved. |
| FAIL         | fragile parsing without canary or unverifiable fields.                                          |
| Outputs      | parser spike, field catalog, fixtures, drift report                                             |

### **Execution steps**

> **•** Login/session
>
> **•** Parse 3-5 page types
>
> **•** Define field catalog/units
>
> **•** Golden fixtures
>
> **•** Break selector to test canary
>
> **•** Restart session

## **3.11 POI-NEWS-001 - Official filings and news ingestion**

| **Field**    | **Value**                                                                               |
|--------------|-----------------------------------------------------------------------------------------|
| Priority     | P0                                                                                      |
| Area         | News/Filings                                                                            |
| Dependencies | POI-PLAT-ARM-001                                                                        |
| Timebox      | 5 days                                                                                  |
| Objective    | להוכיח primary-source timestamps, dedup and clustering.                                 |
| PASS         | official source path works; point-in-time fields reliable; duplicate headlines cluster. |
| FAIL         | timestamp ambiguity or no primary-source verification.                                  |
| Outputs      | provider decision, fixtures, timestamp rules                                            |

### **Execution steps**

> **•** SEC/IR retrieval
>
> **•** News provider sample
>
> **•** Published/observed/available timestamps
>
> **•** Cluster duplicates
>
> **•** Correction/revision test

## **3.12 POI-TV-001 - TradingView webhook**

| **Field**    | **Value**                                                               |
|--------------|-------------------------------------------------------------------------|
| Priority     | P1                                                                      |
| Area         | External Signals                                                        |
| Dependencies | POI-PLAT-ARM-001                                                        |
| Timebox      | 2 days                                                                  |
| Objective    | להוכיח authenticated external signal ingestion without direct trading.  |
| PASS         | alert creates external signal only; duplicates ignored; no broker path. |
| FAIL         | direct-to-order or unauthenticated/replayable endpoint.                 |
| Outputs      | webhook contract, security test, fixtures                               |

### **Execution steps**

> **•** TLS endpoint
>
> **•** Secret/idempotency
>
> **•** Replay/duplicate test
>
> **•** Payload schema
>
> **•** Market-data revalidation

## **3.13 POI-AI-001 - Structured extraction benchmark**

| **Field**    | **Value**                                                                                                     |
|--------------|---------------------------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                                            |
| Area         | AI                                                                                                            |
| Dependencies | None                                                                                                          |
| Timebox      | 5 days                                                                                                        |
| Objective    | לבחור model/provider per task and quantify quality/latency/cost.                                              |
| PASS         | selected providers meet task thresholds or are restricted to manual/shadow; no self-reported confidence gate. |
| FAIL         | unreliable structured output or unbounded prompt/tool behavior.                                               |
| Outputs      | benchmark report, provider matrix, prompt/model versions, golden set                                          |

### **Execution steps**

> **•** Build golden set
>
> **•** Test entity/instruction/catalyst/critic
>
> **•** Schema validation
>
> **•** Adversarial prompts
>
> **•** Latency/cost comparison
>
> **•** Calibration assessment

## **3.14 POI-EMERG-001 - Emergency halt path**

| **Field**    | **Value**                                                                                |
|--------------|------------------------------------------------------------------------------------------|
| Priority     | P0                                                                                       |
| Area         | Operations                                                                               |
| Dependencies | POI-PLAT-ARM-001                                                                         |
| Timebox      | 3 days                                                                                   |
| Objective    | להוכיח GLOBAL_ENTRY_HALT עצמאי מה-UI וה-AI.                                              |
| PASS         | queued/new entries blocked; protective exits remain; state survives restart; full audit. |
| FAIL         | order submits after halt or protection is cancelled.                                     |
| Outputs      | runbook, drill evidence, test automation                                                 |

### **Execution steps**

> **•** Set halt via UI
>
> **•** Set halt via emergency endpoint/CLI
>
> **•** Queue order then halt
>
> **•** Verify pre-submit check
>
> **•** Restart services
>
> **•** Resume with MFA/reconcile

## **3.15 POI-OBS-001 - End-to-end telemetry and alerting**

| **Field**    | **Value**                                                                     |
|--------------|-------------------------------------------------------------------------------|
| Priority     | P1                                                                            |
| Area         | Observability                                                                 |
| Dependencies | POI-PLAT-ARM-001                                                              |
| Timebox      | 3 days                                                                        |
| Objective    | להוכיח tracing, metrics, logs and notification routing.                       |
| PASS         | correlation ID end-to-end; no secrets; alerts dedup/escalate; health visible. |
| FAIL         | missing trace across boundary or secret exposure.                             |
| Outputs      | dashboards, alert rules, redaction tests                                      |

### **Execution steps**

> **•** Trace source-to-decision
>
> **•** Redact secrets
>
> **•** Create critical alerts
>
> **•** Send Telegram private alert
>
> **•** Simulate adapter down/data stale

# **4. Exit Gate**

| **Phase 2 Production implementation may begin only when every P0 PoI is PASS or an approved ADR supplies an equivalent implementation path. POI-SOC-TG-USER cannot be satisfied by POI-SOC-TG-BOT.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
