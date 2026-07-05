תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

| **הממשק הוא Console תפעולי למערכת אוטומטית. הוא חייב לאפשר למפעיל להבין מה המערכת עושה, לקבל נתונים מלאים על עסקאות, לעצור מיד פעילות חדשה, לנהל חריגים ולתחקר כל החלטה. אין להסתפק במסך גרפים או בצ׳אט.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 1. עקרונות UX

> **•** Persistent visibility of environment, mode, broker sync and emergency state.
>
> **•** Show evidence and uncertainty, not only a single score.
>
> **•** Emergency actions accessible within one interaction from every main screen.
>
> **•** No hidden automatic mode changes.
>
> **•** Hebrew RTL, desktop-first operations and responsive emergency control.
>
> **•** Accessible labels and icons; critical state is not communicated by color alone.

# 2. Roles and Permissions

| **Role**         | **Capabilities**                                                    |
|------------------|---------------------------------------------------------------------|
| OWNER            | Modes, policies, model promotion, emergency, secrets references.    |
| TRADER_APPROVER  | Approvals, manual exits, reduce quantity, strategy halt if allowed. |
| OPERATIONS_ADMIN | Health, reconnect, pause, backups, runbooks and emergency halt.     |
| REVIEWER         | Candidates, labels, post-trade review; no execution.                |
| READ_ONLY        | View only.                                                          |
| SERVICE          | No UI login; scoped API identity.                                   |

# 3. Global Application Shell

> **•** Top status strip always shows PAPER/LIVE, RESEARCH/SHADOW/MANUAL/AUTO, broker sync, market session, data freshness and kill state.
>
> **•** Persistent red Emergency button appears on every page.
>
> **•** Global command palette supports symbol lookup, candidate, position, order and incident.
>
> **•** Alert drawer shows severity, acknowledgement and required action.
>
> **•** No direct broker command from browser; all actions go through authenticated command API.

# 4. Navigation

| **Section**          | **Purpose**                                                    |
|----------------------|----------------------------------------------------------------|
| Overview             | Operational summary and current risk.                          |
| Scanner & Watchlists | Technical, fundamental and event discovery.                    |
| Candidates           | Research queue and multi-model detail.                         |
| Approvals            | Manual decision queue.                                         |
| Positions            | Open/closed positions and exit control.                        |
| Orders               | OrderIntent, broker orders, fills and reconciliation.          |
| Models & Strategies  | Active/challenger versions, calibration and manifests.         |
| Sources & Health     | Providers, adapters, freshness and outages.                    |
| Emergency            | Halts, queued intents, protection and runbooks.                |
| Audit & Review       | Immutable history, post-trade and incidents.                   |
| Settings             | Permissions, policy pointers, integrations and UI preferences. |

# 5. Overview Dashboard

| **Widget**         | **Required data**                                                             |
|--------------------|-------------------------------------------------------------------------------|
| System State       | Mode, auto state, emergency state, broker/account and last reconciliation.    |
| Account & Risk     | NLV, risk budget equity, buying power, open risk, daily/weekly P&L, drawdown. |
| Open Positions     | Symbol, qty, entry, P&L, stop, targets, protection status.                    |
| Candidate Funnel   | Discovered, enriched, watching, approved, rejected and expired.               |
| Model Signals      | Counts by technical/fundamental/news/ML/external/social.                      |
| Operational Health | Market data, news, fundamental, TradingView, social, AI, DB, queue and disk.  |
| Alerts             | SEV-1/2 incidents and acknowledgement.                                        |
| Recent Decisions   | Outcome, strategy, reason and timestamps.                                     |

# 6. Scanner and Watchlists

> **•** Run technical, fundamental and event scanners manually or by schedule within permissions.
>
> **•** Display scanner version, universe, as_of, duration, data freshness and result count.
>
> **•** Filter/sort by model scores, liquidity, sector, catalyst, regime and source.
>
> **•** Open result as Candidate or add/remove from Watchlist with reason.
>
> **•** Show why a symbol entered or left a fundamental watchlist.

# 7. Candidate Research Workspace

| **Panel**         | **Content**                                                             |
|-------------------|-------------------------------------------------------------------------|
| Identity & Market | Instrument, exchange, price, spread, session and corporate actions.     |
| Technical         | Setup, trigger, score, levels, volume, indicators and invalidation.     |
| Fundamental       | Quality, growth, valuation, financial risk and watchlist reason.        |
| News/NLP          | Catalysts, materiality, novelty, source class, sentiment and veto.      |
| Quant/ML          | Regime/rank/probability, calibration status, model version and drift.   |
| External          | TradingView/other alerts, raw condition, TTL and revalidation result.   |
| Social            | Messages/source plan, author performance and update links.              |
| Fusion            | Required/optional/veto evidence, missing data, outcome and explanation. |
| Risk & Plan       | Quantity, entry, stop, targets, R/R, exposure and final gate.           |
| Timeline          | All events, assessments, revisions and user actions.                    |

# 8. Approval Queue

> **•** Displays current versus source/trigger price, max chase, expiry, risk, protection plan and missing evidence.
>
> **•** Actions: Approve, Reject, Watch, Refresh, Reduce Quantity, Request Review.
>
> **•** Cannot approve expired or stale decisions without refresh.
>
> **•** Risk increase is forbidden; user may only reduce size or reject.
>
> **•** Every action requires reason code and Audit.

# 9. Positions and Exit Control

| **Field/Action** | **Requirement**                                                          |
|------------------|--------------------------------------------------------------------------|
| Position summary | Qty, average fill, notional, realized/unrealized P&L and risk.           |
| Protection       | Broker-held stop, targets, OCO relation and last verification.           |
| Thesis           | Entry reason, invalidation, horizon and current state.                   |
| Exit triggers    | Stop, target, time, source update, risk, news and manual.                |
| Manual actions   | Reduce, close, cancel target, rebuild protection via runbook.            |
| Warnings         | Unprotected, quantity mismatch, stale market data, broker unknown state. |

# 10. Orders and Reconciliation

> **•** Show canonical order state and broker-native details separately.
>
> **•** Link Decision -\> Risk -\> Approval -\> OrderIntent -\> BrokerOrder -\> Fill -\> Position.
>
> **•** Unknown state is visually critical and offers Reconcile, not blind resend.
>
> **•** Partial fills show protected quantity and remaining entry order.
>
> **•** Operators can inspect idempotency key and event timeline.

# 11. Models and Strategies

| **Page**          | **Functions**                                                                     |
|-------------------|-----------------------------------------------------------------------------------|
| Model Registry    | Active/challenger, artifact hash, training cutoff, calibration, drift and status. |
| Strategy Registry | Manifest, required/optional/veto assessments, lifecycle and environments.         |
| Comparison        | Champion/challenger replay/shadow metrics.                                        |
| Promotion         | Checklist, evidence, approvals and rollback target.                               |
| Policy Viewer     | Read-only effective parameters with source version and allowed range.             |

# 12. Sources and Health

> **•** Per provider: HEALTHY/DEGRADED/DOWN/AUTH_REQUIRED, last event, lag, error rate and circuit state.
>
> • Separate cards for Market Data, fundamentals, news/SEC, TradingView, InvestingPro, WhatsApp, Discord, Telegram, AI and broker.
>
> **•** Actions are scoped: Pause, Resume, Re-auth, Open Runbook and View Logs.
>
> **•** Source outage clearly states which strategies are blocked and which continue.

# 13. Emergency Control Center

| **Emergency Halt and Flatten Account are never the same control. Halting entries is the default safe emergency action; protective orders remain active.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------|

| **Control**             | **Immediate effect**                                  | **Resume**                                            |
|-------------------------|-------------------------------------------------------|-------------------------------------------------------|
| GLOBAL ENTRY HALT       | Stops new decisions-to-order and queued entries.      | MFA + health + reconciliation.                        |
| ENVIRONMENT HALT        | Stops Paper or Live only.                             | Authorized role.                                      |
| STRATEGY HALT           | Stops one strategy/version.                           | Review and pointer check.                             |
| MODEL/SOURCE HALT       | Stops dependent assessments and strategies.           | Health/calibration check.                             |
| SYMBOL HALT             | Blocks new trades for instrument.                     | Manual review.                                        |
| CANCEL UNFILLED ENTRIES | Cancels eligible unfilled entries after broker check. | Not applicable.                                       |
| FLATTEN ACCOUNT         | Closes positions via explicit plan.                   | Re-auth, typed confirmation and Live double approval. |

> **•** Global halt action must be reachable from every screen and work even when AI is unavailable.
>
> **•** After activation, a red banner and timestamp appear throughout the application.
>
> **•** Queued intents are marked cancelled by policy; already-submitting orders require reconciliation.
>
> **•** The system never auto-resumes after SEV-1.

# 14. Alerts and Notifications

| **Severity** | **UX**                                                                                   |
|--------------|------------------------------------------------------------------------------------------|
| SEV-1        | Blocking red banner, persistent sound/notification if enabled, explicit acknowledgement. |
| SEV-2        | Prominent alert and action deadline.                                                     |
| SEV-3        | Warning drawer and health page.                                                          |
| SEV-4        | Informational log and maintenance queue.                                                 |

# 15. Audit and Post-Trade Review

> **•** Filter by trade, symbol, model, strategy, user, source, incident and time.
>
> **•** Post-trade screen separates decision quality, outcome, data, model, timing, execution and luck.
>
> **•** Show counterfactual Shadow/Challenger results without rewriting historical decisions.
>
> **•** Export is permissioned and audited.

# 16. UX States and Error Handling

| **State**       | **Behavior**                                                      |
|-----------------|-------------------------------------------------------------------|
| Loading         | Skeleton and last updated time; no stale action buttons.          |
| No data         | Explain expected source and next action; never show zero as fact. |
| Stale           | Visible age badge; actions disabled according to policy.          |
| Degraded        | Show affected capabilities and safe fallback.                     |
| Unauthorized    | Hide/disable action and explain required role.                    |
| Command pending | Idempotency key and status; prevent duplicate click.              |
| Command unknown | Critical state and reconciliation path.                           |

# 17. Acceptance Criteria

| **ID**   | **Criterion**                                                              |
|----------|----------------------------------------------------------------------------|
| UX-AC-01 | Environment, mode, broker sync and kill state are visible on every screen. |
| UX-AC-02 | Emergency entry halt is reachable from every main screen.                  |
| UX-AC-03 | Halt does not remove protective orders.                                    |
| UX-AC-04 | Flatten is separate and requires stronger confirmation.                    |
| UX-AC-05 | Candidate page shows six analysis families and Fusion.                     |
| UX-AC-06 | Every score shows model version and calibration state.                     |
| UX-AC-07 | Approval cannot use stale or expired decision.                             |
| UX-AC-08 | Position page shows broker-held protection and quantity coverage.          |
| UX-AC-09 | Unknown order state offers reconcile, not retry.                           |
| UX-AC-10 | Source outage shows affected strategies.                                   |
| UX-AC-11 | Dashboard supports full operations without command-line use.               |
| UX-AC-12 | All dangerous actions create audit events.                                 |

# **18. Opportunity, Portfolio and Research Views**

| **מסך**              | **תוכן**                                                                                          |
|----------------------|---------------------------------------------------------------------------------------------------|
| Opportunity Funnel   | Scanned -\> Candidate -\> Eligible -\> Strategy -\> Fusion -\> Risk -\> Action; Drop-off by gate. |
| Policy Starvation    | חלון זמן, Candidates זמינים, Gate concentration, rejected-shadow outcomes ו-What-if.              |
| Overtrading Monitor  | קצב עסקאות, turnover, strategy budgets וחריגה מהתנהגות רגילה.                                     |
| Portfolio Cockpit    | Open risk, notional, sector/theme/correlation, strategy allocation ו-capacity.                    |
| Market Regime        | Regime, confidence, inputs, compatible strategies ו-history.                                      |
| Research Lab         | Replay/Backtest runs, Champion/Challenger, datasets, metrics ו-promotion requests.                |
| Model Registry       | Model cards, versions, calibration, drift, status ו-rollback.                                     |
| Trust and Quarantine | מקורות חשודים, prompt-injection flags, cluster manipulation ו-review.                             |

## **18.1 Emergency UX**

> **•** כפתור GLOBAL ENTRY HALT קבוע בכל מסך עם מצב ACTIVE/INACTIVE ברור.
>
> **•** מסך מציג האם ערוץ החירום הנפרד וה-Execution Agent אישרו את ה-Halt.
>
> **•** Resume Wizard דורש Reconciliation, health, no unknown orders, MFA וסיבה.
>
> **•** FLATTEN ACCOUNT מופרד חזותית ותפקודית, עם Preview ותיבת אישור מפורשת.

## **18.2 הסבר החלטה**

> **•** לכל Candidate מוצגים Required, Veto, Soft, Bonus ו-Context בנפרד.
>
> **•** מוצג Action Tier והסיבה להפחתת Risk Multiplier.
>
> **•** מוצג מה היה צריך להשתנות כדי לעבור מ-Watch/Manual ל-Auto.
>
> **•** הממשק אינו מציג Score יחיד ללא פירוט המקורות והגרסאות.

# **19. Telegram Operations UX**

| **מסך/רכיב**       | **מידע ופעולות**                                                                  |
|--------------------|-----------------------------------------------------------------------------------|
| Source Card        | Mode, bot/user identity, Health, last event, backlog, rate-limit ו-auth status.   |
| Source Profiles    | רשימת Groups/Channels/Topics, Allowlist, ingestion enabled ו-display name.        |
| Authentication     | Bot token setup או TDLib code/QR flow; secrets אינם מוצגים לאחר שמירה.            |
| Diagnostics        | Permissions, Privacy Mode, webhook/long-poll state, session storage ו-last error. |
| Notifications      | בחירת צ'אט פרטי לקבלת Alerts ובדיקת Test Notification.                            |
| Candidate Evidence | הודעה מקורית, Channel/Group, sender, forward origin, reply/topic ו-revision.      |
| Emergency          | מציג Telegram health בלבד; אין כפתור שמאפשר Telegram remote commands.             |

## **19.1 UX Safety**

> **•** הוספת Source Profile דורשת Owner permission ואישור Allowlist מפורש.
>
> **•** ביטול מקור עוצר קליטה חדשה אך אינו מוחק Evidence היסטורי.
>
> **•** AUTH_REQUIRED מוצג כ-Banner ו-Alert עם קישור ל-Re-authentication.
>
> **•** הודעות Telegram אינן מוצגות כאמת מאומתת; Trust state ו-Independent Confirmation גלויים.

# **20. מסך Social Connections המחייב**

| **כרטיס**             | **סטטוס חובה**      | **פעולות**                                                         |
|-----------------------|---------------------|--------------------------------------------------------------------|
| WhatsApp User         | REQUIRED CAPABILITY | Pair/Re-auth, allowlist, pause, health, last event.                |
| Discord User          | REQUIRED CAPABILITY | Login/Re-auth, server/channel allowlist, pause, health.            |
| Telegram User (TDLib) | REQUIRED PRIMARY    | Phone/QR/2FA flow, chat/channel allowlist, session revoke, health. |
| Telegram Bot          | SUPPLEMENTAL        | Token setup, webhook/polling, allowed sources, test notification.  |

> **•** הממשק מציג בנפרד Capability Status ו-Runtime Availability כדי לא לבלבל בין חובה לבנות לבין חובה להיות מחובר בכל רגע.
>
> **•** אי אפשר לסמן Social Integration כ-Complete אם Telegram User לא עבר PoI.
>
> **•** אין כפתורי BUY/SELL/HALT/FLATTEN בתוך מסכי Social.
