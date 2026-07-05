תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

| מסמך זה מתרגם את Decision Contract v0.8 ואת System Functional Specification v0.7 למודל נתונים. המטרה היא לשמר כל ראיה, החלטה, הוראת מחיר, פקודה וביצוע באופן שניתן לשחזור, ובמקביל למנוע התנפחות בלתי מבוקרת של הדיסק. |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 1. מטרות והיקף

> **•** להגדיר את הישויות, הטבלאות והקשרים הנדרשים להפעלת המערכת מקצה לקצה.
>
> **•** להפריד בין מידע גולמי, מידע מפורש, Features, החלטות, תכניות עסקה, פקודות וביצועים.
>
> **•** להבטיח Audit, Replay, Idempotency ויכולת החלפת גרסאות ללא אובדן היסטוריה.
>
> **•** לקבוע מדיניות Retention ניתנת לשינוי בקונפיגורציה, ולא באמצעות קוד קשיח.
>
> **•** לתכנן את האחסון עבור שרת Oracle הקיים, עם יכולת מעבר עתידית לאחסון חיצוני.

# 2. החלטות ארכיטקטוניות מחייבות

| **מזהה** | **החלטה**                                                                                                          |
|----------|--------------------------------------------------------------------------------------------------------------------|
| DM-001   | PostgreSQL הוא מסד הנתונים המרכזי למידע מובנה ולמטא-דאטה.                                                          |
| DM-002   | קבצים, HTML, תמונות ו-Raw Payloads אינם נשמרים כ-BLOB בתוך PostgreSQL אלא ב-Object Store/Filesystem עם URI ו-Hash. |
| DM-003   | כל הזמנים נשמרים ב-UTC מסוג timestamptz; אזור הזמן מוצג רק בממשק.                                                  |
| DM-004   | מזהים הם UUIDv7 שנוצרים באפליקציה; אין תלות ב-Sequence מרכזי.                                                      |
| DM-005   | כסף נשמר numeric(20,8), כמות numeric(28,10), יחסים numeric(18,10); אין float לעסקאות.                              |
| DM-006   | אירועים, החלטות, גרסאות, Fills ו-Audit הם Append-Only; אין עריכה היסטורית.                                         |
| DM-007   | מצב נוכחי יכול להישמר בטבלה Mutable, אך כל שינוי חייב Event History מקביל.                                         |
| DM-008   | Critical fields מנורמלים לעמודות; JSONB מיועד לתוכן משתנה, Payload ופרטי ספק.                                      |
| DM-009   | Raw Data מקבל Content Hash, Compression ו-Deduplication.                                                           |
| DM-010   | Retention, Partitioning ו-Archive Rules מוגדרים ב-Policy גרסאי.                                                    |

# 3. שכבות האחסון

| **שכבה**            | **תוכן**                                                                    | **דפוס גישה**        |
|---------------------|-----------------------------------------------------------------------------|----------------------|
| PostgreSQL / Hot    | ישויות פעילות, סטטוסים, החלטות, פקודות, Fills, אינדקסים ודוחות שוטפים.      | גישה מיידית          |
| Object Store / Warm | הודעות גולמיות, HTML, תמונות, קבצים, תשובות מודל גדולות ו-Snapshots דחוסים. | לפי URI/Hash         |
| Archive / Cold      | Partitions ישנים, גיבויים וראיות שאינן נדרשות לתפעול יומי.                  | שחזור מתוכנן         |
| External Provider   | היסטוריית שוק רחבה שאינה נדרשת לשחזור החלטה ספציפית.                        | נשלפת מחדש לפי הרשאה |

## 3.1 כלל Evidence Pinning

| **כל פריט Raw, הודעה, קובץ, תשובת מודל או Market Snapshot שקושר ל-Candidate, Decision, Trade Plan, Order או Post-Trade Review מסומן pinned=true. פריט מוצמד אינו נמחק לפי מדיניות ברירת המחדל של מקור גולמי.** |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 4. מוסכמות שמות וסוגי נתונים

| **נושא**    | **כלל**                                                                                 |
|-------------|-----------------------------------------------------------------------------------------|
| טבלאות      | snake_case, שם רבים: source_events, decision_runs.                                      |
| מפתח ראשי   | id uuid; UUIDv7 נוצר בשכבת האפליקציה.                                                   |
| מפתחות זרים | \<entity\>\_id עם FK מפורש.                                                             |
| זמנים       | observed_at, created_at, updated_at, effective_from, effective_to.                      |
| סביבה       | environment text: RESEARCH/PAPER/LIVE לפי Contract.                                     |
| גרסה        | version text + immutable row; Active Pointer בטבלה נפרדת.                               |
| מחיקה       | deleted_at רק לישויות תפעוליות; היסטוריה קריטית אינה Soft Delete אלא Archive/Retention. |
| Hash        | sha256 text עבור Payload, Snapshot ו-Decision integrity.                                |
| מטבע        | currency char(3) לפי ISO; ללא הנחת USD בקוד.                                            |

# 5. מפת קשרים מרכזית

> source_event -\> source_event_revision -\> entity_resolution_run
>
> -\> source_trade_instruction -\> trade_candidate
>
> trade_candidate -\> feature_snapshot -\> strategy_evaluation -\> decision_run
>
> -\> critic_review
>
> decision_run -\> risk_evaluation -\> system_trade_plan -\> order_intent
>
> order_intent -\> broker_order -\> broker_order_event -\> fill -\> position/position_lot
>
> position -\> exit_trigger -\> exit_decision -\> order_intent
>
> all critical entities -\> evidence_links -\> raw_object / source_event / market_snapshot
>
> all changes -\> audit_events

# 6. קטלוג טבלאות לפי תחום

## 6.1 זהויות, משתמשים והרשאות

| **טבלה**   | **מטרה**                                      | **אופי**        | **עדיפות** |
|------------|-----------------------------------------------|-----------------|------------|
| workspaces | התקנת מערכת/מרחב עבודה; בשלב ראשון רשומה אחת. | Mutable         | MVP        |
| app_users  | משתמשי Dashboard.                             | Mutable + Audit | MVP        |
| roles      | תפקידי OWNER/APPROVER/REVIEWER/READ_ONLY.     | Reference       | MVP        |
| user_roles | שיוך משתמש לתפקיד.                            | Mutable + Audit | MVP        |

## 6.2 מקורות, קליטה ו-Raw Data

| **טבלה**                | **מטרה**                                     | **אופי**                 | **עדיפות** |
|-------------------------|----------------------------------------------|--------------------------|------------|
| source_connectors       | הגדרת Adapter, סוג מקור, גרסה ומצב.          | Mutable + History        | MVP        |
| source_accounts         | זהות חשבון/Session לוגית; ללא Secret עצמו.   | Mutable + Audit          | MVP        |
| source_channels         | קבוצה, שרת, ערוץ או Feed.                    | Mutable                  | MVP        |
| source_authors          | מחבר/אנליסט/משתמש מקור.                      | Mutable                  | MVP        |
| source_events           | מעטפת אירוע גולמי עם זמנים ו-Hash.           | Append-Only, Partitioned | MVP        |
| source_event_revisions  | עריכות, מחיקות ועדכוני הודעה.                | Append-Only              | MVP        |
| raw_objects             | URI, MIME, גודל, Hash, Compression, Pinning. | Append-Only              | MVP        |
| source_attachments      | קישור אירוע לקובץ/תמונה.                     | Append-Only              | MVP        |
| ingestion_runs          | ריצת Connector, Cursor, Counts ו-Lag.        | Append-Only              | MVP        |
| connector_health_events | HEALTHY/DEGRADED/DOWN/AUTH_REQUIRED.         | Append-Only, Partitioned | MVP        |
| scraper_runs            | URL/Entity, Parser Version, Success/Failure. | Append-Only              | MVP        |
| parser_versions         | גרסאות Parser ו-Schema Fingerprint.          | Immutable                | MVP        |

## 6.3 נכסים, ישויות והוראות מסחר מהמקור

| **טבלה**                     | **מטרה**                                               | **אופי**            | **עדיפות** |
|------------------------------|--------------------------------------------------------|---------------------|------------|
| instruments                  | נכס פנימי קנוני: Symbol, Venue, Asset Class, Currency. | Reference + Updates | MVP        |
| instrument_aliases           | שמות חברה, Cashtags, טיקרים היסטוריים ו-Aliases.       | Mutable + History   | MVP        |
| entity_resolution_runs       | תוצאת זיהוי טיקר/כוונה/כיוון.                          | Append-Only         | MVP        |
| entity_resolution_candidates | כל מועמדי הנכס והביטחון.                               | Append-Only         | MVP        |
| source_trade_instructions    | פירוש העסקה כפי שנכתבה במקור.                          | Append-Only         | MVP        |
| source_instruction_entries   | מחיר/טווח/סמנטיקת כניסה.                               | Append-Only         | MVP        |
| source_instruction_targets   | יעדי מכירה מלאים/חלקיים.                               | Append-Only         | MVP        |
| source_instruction_stops     | Stop במחיר/אחוז/תנאי ו-Anchor.                         | Append-Only         | MVP        |
| source_instruction_revisions | עדכון כמו העלאת Stop, מכירת חצי או יציאה.              | Append-Only         | MVP        |

## 6.4 מועמדים, Market Data, חדשות ו-Fundamental

| **טבלה**                 | **מטרה**                                            | **אופי**                 | **עדיפות** |
|--------------------------|-----------------------------------------------------|--------------------------|------------|
| trade_candidates         | מועמד מרכזי עם Trigger, Priority, Profile ו-Expiry. | Mutable + History        | MVP        |
| candidate_status_events  | כל מעבר מצב של Candidate.                           | Append-Only              | MVP        |
| candidate_evidence_links | קישור Candidate לראיות.                             | Append-Only              | MVP        |
| market_data_providers    | ספקי Market Data ויכולות.                           | Reference                | MVP        |
| market_snapshots         | Quote/Trade/Session/Spread בעת החלטה.               | Append-Only, Partitioned | MVP        |
| market_bars_cache        | Bars נבחרים לצורך Features/Replay.                  | Partitioned, TTL         | MVP        |
| market_data_health       | Lag, Entitlement, Rate Limit, Error.                | Append-Only              | MVP        |
| news_items               | כתבות/כותרות/מקור/זמן וקישור Raw.                   | Append-Only              | MVP        |
| filings                  | דיווחים רשמיים ומסמכים.                             | Append-Only              | שלב 2      |
| catalysts                | אירוע מסווג המקושר לנכס ולראיות.                    | Append-Only              | MVP        |
| fundamental_snapshots    | נתונים פונדמנטליים בנקודת זמן.                      | Append-Only              | MVP        |
| investingpro_snapshots   | שדות InvestingPro + Parser Version + Raw Ref.       | Append-Only              | MVP        |

## 6.5 AI, Features, אסטרטגיות וגרסאות

| **טבלה**                | **מטרה**                                     | **אופי**                 | **עדיפות** |
|-------------------------|----------------------------------------------|--------------------------|------------|
| model_providers         | ספקי מודלים ישירים.                          | Reference                | MVP        |
| model_definitions       | Model ID, capabilities, parameters defaults. | Immutable Version        | MVP        |
| prompt_versions         | Prompt, Output Schema ו-Hash.                | Immutable                | MVP        |
| analysis_runs           | כל קריאת מודל/Analyzer, פלט, Latency ועלות.  | Append-Only, Partitioned | MVP        |
| analysis_evidence_links | ראיות ששימשו כל Analysis.                    | Append-Only              | MVP        |
| feature_definitions     | שם Feature, סוג, Calculator Version.         | Immutable Version        | MVP        |
| feature_snapshots       | קבוצת Features קפואה למועמד/החלטה.           | Append-Only              | MVP        |
| feature_values          | ערכי Feature עם Quality ו-Lineage.           | Append-Only, Partitioned | MVP        |
| strategy_definitions    | משפחות אסטרטגיה.                             | Reference                | MVP        |
| strategy_versions       | Manifest, State ו-Policy References.         | Immutable                | MVP        |
| policy_definitions      | Decision/Risk/Latency/Critic/Exit.           | Reference                | MVP        |
| policy_versions         | תוכן Policy, Hash, Effective dates.          | Immutable                | MVP        |
| active_version_pointers | הגרסה הפעילה לכל Context/Environment.        | Mutable + Audit          | MVP        |
| assumptions             | קטלוג הנחות ותנאי בחינה מחדש.                | Immutable Version        | MVP        |

## 6.6 החלטות, סיכון ואישור

| **טבלה**                | **מטרה**                                          | **אופי**                 | **עדיפות** |
|-------------------------|---------------------------------------------------|--------------------------|------------|
| strategy_evaluations    | הערכת אסטרטגיה עבור Candidate ו-Feature Snapshot. | Append-Only              | MVP        |
| critic_reviews          | סתירות, מידע חסר והמלצות Veto.                    | Append-Only              | MVP        |
| decision_runs           | Outcome, Policy Version, Deadline ו-Hash.         | Append-Only, Partitioned | MVP        |
| decision_evidence_links | קישור החלטה לכל ראיה.                             | Append-Only              | MVP        |
| decision_snapshots      | JSON קפוא/דחוס לשחזור מלא.                        | Append-Only, Pinned      | MVP        |
| portfolio_snapshots     | פוזיציות, Cash, Buying Power וחשיפה בזמן החלטה.   | Append-Only              | MVP        |
| risk_evaluations        | APPROVE/REDUCE/VETO וכמות מאושרת.                 | Append-Only              | MVP        |
| risk_vetoes             | קודי Veto ופרטים.                                 | Append-Only              | MVP        |
| human_approvals         | אישור/דחייה/הערה וזהות משתמש.                     | Append-Only              | MVP        |

## 6.7 תכנית עסקה, ברוקר, פקודות ופוזיציות

| **טבלה**              | **מטרה**                                          | **אופי**                 | **עדיפות** |
|-----------------------|---------------------------------------------------|--------------------------|------------|
| system_trade_plans    | תכנית שהמערכת אישרה; נפרדת מהוראת המקור.          | Immutable Revision       | MVP        |
| trade_plan_entries    | סוג פקודה, מחיר, Zone, Max Chase, Expiry.         | Append-Only              | MVP        |
| trade_plan_targets    | יעדים וכמויות/אחוזים.                             | Append-Only              | MVP        |
| trade_plan_stops      | Protective Stop, Percent Stop, Thesis/Time Stop.  | Append-Only              | MVP        |
| trade_plan_revisions  | שינוי מאושר בתכנית לאחר כניסה.                    | Append-Only              | MVP        |
| brokers               | הגדרת ברוקר ויכולות.                              | Reference                | MVP        |
| broker_accounts       | Account Alias, Environment, Currency; ללא Secret. | Mutable + Audit          | MVP        |
| order_intents         | פקודה כללית עם Idempotency Key.                   | Append-Only              | MVP        |
| broker_orders         | מיפוי ל-Broker Order ID ומצב נוכחי.               | Mutable + History        | MVP        |
| broker_order_events   | Submitted/Ack/Cancelled/Rejected/Status.          | Append-Only, Partitioned | MVP        |
| fills                 | ביצועים בפועל, מחיר, כמות, עמלה.                  | Append-Only              | MVP        |
| positions             | מצב פוזיציה נוכחי, נגזר מ-Fills.                  | Mutable + History        | MVP        |
| position_lots         | Lots ומחיר עלות.                                  | Append-Only/Derived      | MVP        |
| position_protections  | Stop/Target פעילים והקשר לפקודות.                 | Mutable + History        | MVP        |
| reconciliation_runs   | השוואת המערכת לברוקר.                             | Append-Only              | MVP        |
| reconciliation_issues | אי התאמות פתוחות/סגורות.                          | Mutable + History        | MVP        |

## 6.8 יציאה, תחקור ותפעול

| **טבלה**            | **מטרה**                                     | **אופי**                 | **עדיפות** |
|---------------------|----------------------------------------------|--------------------------|------------|
| exit_triggers       | Stop, יעד, שינוי תזה, Time Exit, Risk Event. | Append-Only              | MVP        |
| exit_decisions      | החלטת יציאה ו-Exit Policy Version.           | Append-Only              | MVP        |
| position_events     | פתיחה, הגדלה, הקטנה, הגנה, סגירה.            | Append-Only              | MVP        |
| post_trade_reviews  | איכות החלטה, תוצאה, שגיאה ומסקנות.           | Append-Only              | MVP        |
| trade_metrics       | P&L, R, MFE, MAE, Slippage, Latency.         | Derived + Snapshot       | MVP        |
| change_proposals    | הצעת שינוי Assumption/Policy/Strategy.       | Append-Only              | שלב 2      |
| experiment_runs     | Replay/Shadow/Champion/Challenger.           | Append-Only              | MVP        |
| experiment_results  | מדדים לפי גרסה.                              | Append-Only              | MVP        |
| audit_events        | כל שינוי ניהולי ופעולה רגישה.                | Append-Only, Partitioned | MVP        |
| system_alerts       | התראות תפעול, סיכון ואבטחה.                  | Mutable + History        | MVP        |
| dead_letter_events  | אירועים שלא עובדו לאחר Retry.                | Append-Only, TTL         | MVP        |
| idempotency_records | מניעת כפל פעולות ופקודות.                    | TTL + Pinned if order    | MVP        |

# 7. סכמות ליבה מפורטות

## 7.1 source_events

> source_events
>
> id uuid PK
>
> workspace_id uuid FK
>
> connector_id uuid FK
>
> source_account_id uuid FK NULL
>
> channel_id uuid FK NULL
>
> author_id uuid FK NULL
>
> external_event_id text NULL
>
> event_type text NOT NULL
>
> observed_at timestamptz NOT NULL
>
> ingested_at timestamptz NOT NULL
>
> content_text text NULL
>
> content_language text NULL
>
> content_hash text NOT NULL
>
> raw_object_id uuid FK NULL
>
> current_revision_no integer NOT NULL DEFAULT 1
>
> is_deleted_at_source boolean NOT NULL DEFAULT false
>
> pinned boolean NOT NULL DEFAULT false
>
> metadata jsonb NOT NULL DEFAULT '{}'
>
> created_at timestamptz NOT NULL
>
> UNIQUE(connector_id, external_event_id) WHERE external_event_id IS NOT NULL
>
> INDEX(observed_at DESC)
>
> INDEX(channel_id, observed_at DESC)
>
> INDEX(author_id, observed_at DESC)
>
> INDEX(content_hash)

## 7.2 source_trade_instructions

> source_trade_instructions
>
> id uuid PK
>
> source_event_id uuid FK NOT NULL
>
> resolution_run_id uuid FK NOT NULL
>
> instrument_id uuid FK NOT NULL
>
> instruction_type text NOT NULL
>
> intent text NOT NULL -- ENTRY / EXIT / UPDATE / DISCUSSION
>
> direction text NOT NULL -- LONG / SHORT / NEUTRAL / UNKNOWN
>
> valid_from timestamptz NULL
>
> valid_until timestamptz NULL
>
> time_horizon text NULL
>
> extraction_confidence numeric(18,10) NOT NULL
>
> ambiguity_status text NOT NULL -- CLEAR / NEEDS_REVIEW / INVALID
>
> source_text_excerpt text NULL
>
> instruction_version integer NOT NULL DEFAULT 1
>
> created_at timestamptz NOT NULL
>
> UNIQUE(source_event_id, instruction_version)

## 7.3 source_instruction_entries / targets / stops

> source_instruction_entries
>
> id uuid PK
>
> instruction_id uuid FK NOT NULL
>
> semantics text NOT NULL -- BUY_LIMIT_MAX / BUY_STOP_TRIGGER / BUY_ZONE / ENTER_IMMEDIATELY
>
> price numeric(20,8) NULL
>
> price_low numeric(20,8) NULL
>
> price_high numeric(20,8) NULL
>
> currency char(3) NOT NULL
>
> anchor_type text NULL
>
> max_chase_price numeric(20,8) NULL
>
> sequence_no integer NOT NULL
>
> source_instruction_targets
>
> id uuid PK
>
> instruction_id uuid FK NOT NULL
>
> target_type text NOT NULL -- PRICE / PERCENT / CONDITION
>
> target_price numeric(20,8) NULL
>
> target_percent numeric(18,10) NULL
>
> exit_fraction numeric(18,10) NULL
>
> sequence_no integer NOT NULL
>
> source_instruction_stops
>
> id uuid PK
>
> instruction_id uuid FK NOT NULL
>
> stop_type text NOT NULL -- PRICE / PERCENT / THESIS / TIME / STRUCTURE
>
> stop_price numeric(20,8) NULL
>
> stop_percent numeric(18,10) NULL
>
> anchor_type text NULL -- FILL_PRICE / ENTRY_PRICE / SOURCE_PRICE / HIGH_WATERMARK
>
> condition_json jsonb NULL
>
> sequence_no integer NOT NULL

## 7.4 trade_candidates

> trade_candidates
>
> id uuid PK
>
> workspace_id uuid FK NOT NULL
>
> instrument_id uuid FK NOT NULL
>
> trigger_type text NOT NULL
>
> primary_source_event_id uuid FK NULL
>
> primary_instruction_id uuid FK NULL
>
> priority text NOT NULL
>
> processing_profile text NOT NULL
>
> status text NOT NULL
>
> observed_at timestamptz NOT NULL
>
> expires_at timestamptz NULL
>
> current_stage text NOT NULL
>
> correlation_id uuid NOT NULL
>
> created_at timestamptz NOT NULL
>
> updated_at timestamptz NOT NULL
>
> INDEX(status, priority, created_at)
>
> INDEX(instrument_id, observed_at DESC)
>
> UNIQUE(correlation_id)

## 7.5 decision_runs

> decision_runs
>
> id uuid PK
>
> candidate_id uuid FK NOT NULL
>
> strategy_version_id uuid FK NOT NULL
>
> decision_policy_version_id uuid FK NOT NULL
>
> feature_snapshot_id uuid FK NOT NULL
>
> processing_profile text NOT NULL
>
> outcome text NOT NULL
>
> score_display numeric(8,4) NULL
>
> signal numeric(18,10) NULL
>
> probability_estimate numeric(18,10) NULL
>
> data_quality numeric(18,10) NOT NULL
>
> started_at timestamptz NOT NULL
>
> completed_at timestamptz NOT NULL
>
> deadline_at timestamptz NULL
>
> expires_at timestamptz NULL
>
> latency_ms integer NOT NULL
>
> reason_codes text\[\] NOT NULL DEFAULT '{}'
>
> snapshot_id uuid FK NOT NULL
>
> decision_hash text NOT NULL
>
> created_at timestamptz NOT NULL
>
> UNIQUE(candidate_id, strategy_version_id, decision_policy_version_id, feature_snapshot_id)
>
> INDEX(outcome, completed_at DESC)

## 7.6 system_trade_plans

> system_trade_plans
>
> id uuid PK
>
> decision_run_id uuid FK NOT NULL
>
> risk_evaluation_id uuid FK NOT NULL
>
> source_instruction_id uuid FK NULL
>
> instrument_id uuid FK NOT NULL
>
> direction text NOT NULL
>
> environment text NOT NULL
>
> plan_version integer NOT NULL
>
> status text NOT NULL -- DRAFT / APPROVED / ACTIVE / COMPLETED / CANCELLED / EXPIRED
>
> valid_from timestamptz NOT NULL
>
> valid_until timestamptz NULL
>
> thesis text NOT NULL
>
> invalidation_summary text NOT NULL
>
> max_loss_amount numeric(20,8) NOT NULL
>
> currency char(3) NOT NULL
>
> created_at timestamptz NOT NULL
>
> UNIQUE(decision_run_id, plan_version)

## 7.7 order_intents, broker_orders, fills

> order_intents
>
> id uuid PK
>
> trade_plan_id uuid FK NOT NULL
>
> broker_account_id uuid FK NOT NULL
>
> environment text NOT NULL
>
> intent_type text NOT NULL -- ENTRY / EXIT / PROTECTION / MODIFY / CANCEL
>
> side text NOT NULL
>
> order_type text NOT NULL
>
> quantity numeric(28,10) NOT NULL
>
> limit_price numeric(20,8) NULL
>
> stop_price numeric(20,8) NULL
>
> time_in_force text NOT NULL
>
> outside_rth boolean NOT NULL
>
> idempotency_key text NOT NULL
>
> expires_at timestamptz NULL
>
> status text NOT NULL
>
> created_at timestamptz NOT NULL
>
> UNIQUE(environment, broker_account_id, idempotency_key)
>
> broker_orders
>
> id uuid PK
>
> order_intent_id uuid FK NOT NULL
>
> broker_order_id text NOT NULL
>
> current_status text NOT NULL
>
> submitted_at timestamptz NULL
>
> last_broker_update_at timestamptz NULL
>
> raw_broker_payload jsonb NULL
>
> updated_at timestamptz NOT NULL
>
> UNIQUE(broker_account_id, broker_order_id)
>
> fills
>
> id uuid PK
>
> broker_order_id uuid FK NOT NULL
>
> broker_execution_id text NOT NULL
>
> executed_at timestamptz NOT NULL
>
> quantity numeric(28,10) NOT NULL
>
> price numeric(20,8) NOT NULL
>
> commission numeric(20,8) NULL
>
> currency char(3) NOT NULL
>
> liquidity_flag text NULL
>
> raw_payload jsonb NULL
>
> UNIQUE(broker_order_id, broker_execution_id)

## 7.8 positions and exit

> positions
>
> id uuid PK
>
> broker_account_id uuid FK NOT NULL
>
> instrument_id uuid FK NOT NULL
>
> environment text NOT NULL
>
> strategy_version_id uuid FK NOT NULL
>
> trade_plan_id uuid FK NOT NULL
>
> side text NOT NULL
>
> quantity_open numeric(28,10) NOT NULL
>
> average_entry_price numeric(20,8) NOT NULL
>
> realized_pnl numeric(20,8) NOT NULL DEFAULT 0
>
> unrealized_pnl numeric(20,8) NOT NULL DEFAULT 0
>
> status text NOT NULL -- OPEN / PARTIAL / CLOSED / RECONCILIATION_REQUIRED
>
> opened_at timestamptz NOT NULL
>
> closed_at timestamptz NULL
>
> updated_at timestamptz NOT NULL
>
> exit_triggers
>
> id uuid PK
>
> position_id uuid FK NOT NULL
>
> trigger_type text NOT NULL
>
> observed_at timestamptz NOT NULL
>
> trigger_value jsonb NOT NULL
>
> source_event_id uuid FK NULL
>
> status text NOT NULL
>
> created_at timestamptz NOT NULL
>
> exit_decisions
>
> id uuid PK
>
> position_id uuid FK NOT NULL
>
> exit_trigger_id uuid FK NULL
>
> exit_policy_version_id uuid FK NOT NULL
>
> outcome text NOT NULL -- HOLD / REDUCE / EXIT / NEEDS_REVIEW
>
> quantity_to_exit numeric(28,10) NOT NULL
>
> reason_codes text\[\] NOT NULL
>
> created_at timestamptz NOT NULL

# 8. מקור מול תכנית מערכת

| **ישות**                 | **משמעות**             | **כלל**                       |
|--------------------------|------------------------|-------------------------------|
| Source Trade Instruction | מה שהכותב אמר בפועל    | אסור לערוך; רק Revision חדש.  |
| System Trade Plan        | מה שהמערכת החליטה לבצע | נוצר רק לאחר Decision + Risk. |
| Order Intent             | הוראת ביצוע כללית      | לא תלויה ב-IBKR.              |
| Broker Order             | הפקודה שנשלחה לברוקר   | מכילה מזהה וסטטוס ברוקר.      |
| Fill                     | מה שבוצע בפועל         | מקור האמת לכמות ולמחיר.       |
| Position                 | מצב מחושב מה-Fills     | חייב Reconciliation.          |

# 9. Immutability, Revisions ו-Lineage

> **•** source_events אינם נדרסים: עריכה יוצרת source_event_revision.
>
> **•** Source Instruction חדש אינו משנה הוראה קודמת; הוא מקושר revision_of_instruction_id.
>
> **•** Policy, Prompt, Model, Parser, Feature ו-Strategy גרסאיים ואינם ניתנים לעריכה לאחר שימוש.
>
> **•** Decision Snapshot כולל IDs ו-Hashes של כל הקלט והגרסאות.
>
> **•** System Trade Plan משתנה רק באמצעות trade_plan_revision; אין שינוי שקט של Stop/Target.
>
> **•** Broker Order Events ו-Fills הם Append-Only; current_status הוא Cache נוח בלבד.
>
> **•** כל Derived Metric שומר calculator_version ו-input_ids.

# 10. אינדקסים, Constraints ו-Idempotency

| **תחום**          | **דרישה**                                                                |
|-------------------|--------------------------------------------------------------------------|
| Dedup Source      | UNIQUE(connector_id, external_event_id) או content_hash + time bucket.   |
| Order Idempotency | UNIQUE(environment, broker_account_id, idempotency_key).                 |
| Broker Fill       | UNIQUE(broker_order_id, broker_execution_id).                            |
| Active Queries    | Partial indexes על status IN (NEW, PROCESSING, OPEN, PENDING).           |
| Time Series       | BRIN על observed_at/created_at בטבלאות גדולות; B-tree על קשרים פעילים.   |
| JSONB             | GIN רק על שדות שנמדדו כשאילתות נפוצות; לא על כל Payload.                 |
| Money Validation  | CHECK(quantity \> 0), CHECK(percent בין טווחים תקפים), Currency לא NULL. |
| Environment       | CHECK שהתאמת PAPER/LIVE נשמרת לאורך OrderIntent, Account ו-Position.     |

# 11. Partitioning ו-Data Volume

| **טבלה**                | **חלוקה**                      |
|-------------------------|--------------------------------|
| source_events           | חודשי לפי observed_at.         |
| connector_health_events | חודשי לפי created_at.          |
| market_snapshots        | יומי או חודשי לפי קצב בפועל.   |
| market_bars_cache       | חודשי לפי bar_time + Provider. |
| analysis_runs           | חודשי לפי created_at.          |
| feature_values          | חודשי לפי observed_at.         |
| decision_runs           | חודשי לפי completed_at.        |
| broker_order_events     | חודשי לפי event_at.            |
| audit_events            | חודשי לפי created_at.          |

## 11.1 עיקרון אחסון נתוני שוק

> **•** לא שומרים Tick Data מלא לכל השוק בשרת.
>
> **•** שומרים Snapshots ששימשו החלטות, Bars הנדרשים ל-Features וחלונות סביב Candidate/Trade.
>
> **•** Historical Data רחב נשאר אצל ספק הנתונים ונשלף ל-Replay לפי צורך ורישוי.
>
> **•** Daily Bars יכולים להישמר לטווח ארוך; 1-Minute Bars נשמרים לפי Retention מוגבל.

# 12. מדיניות Retention ראשונית

| **הערכים להלן הם Assumptions ראשוניים, לא קבועים בקוד. הם יישמרו ב-retention-policy.v0.1 וניתנים לשינוי דרך Active Version Pointer.** |
|---------------------------------------------------------------------------------------------------------------------------------------|

| **Class**               | **תוכן**                                                                                                          | **ברירת מחדל**                                    |
|-------------------------|-------------------------------------------------------------------------------------------------------------------|---------------------------------------------------|
| R0 - Permanent          | Decision Snapshots, Policies/Strategies used, Risk, Approvals, Orders, Fills, Positions, Post-Trade, Audit קריטי. | ללא מחיקה אוטומטית; Archive בלבד.                 |
| R1 - Evidence           | Source/Raw/Model/Market ששימשו החלטה או עסקה.                                                                     | לפחות 7 שנים טכנית או עד Archive מפורש; Pinned.   |
| R2 - Learning           | Parsed Instructions, Features, Candidates, Analyses ו-Source Performance.                                         | ללא מחיקה אוטומטית; ניתן להעביר ל-Cold Storage.   |
| R3 - Raw Unreferenced   | הודעות ו-Raw שלא יצרו Candidate/Decision.                                                                         | 24 חודשים; לאחר מכן מחיקה/אנונימיזציה לפי Policy. |
| R4 - Large Attachments  | תמונות/קבצים לא מוצמדים.                                                                                          | 180 ימים; מוצמדים עוברים R1.                      |
| R5 - Scraped HTML       | HTML מלא של InvestingPro לא מוצמד.                                                                                | 90 ימים דחוסים; מוצמד עובר R1.                    |
| R6 - Market 1m Cache    | Bars של דקה שאינם חלק Evidence.                                                                                   | 180 ימים; Evidence נשמר R1.                       |
| R7 - Daily Bars         | Bars יומיים נבחרים.                                                                                               | ללא מגבלה בשלב הראשון; בדיקת נפח שנתית.           |
| R8 - Model Raw Unlinked | בקשות/תשובות מודל שלא השתתפו בהחלטה.                                                                              | 180 ימים; Metadata/Cost נשמרים 24 חודשים.         |
| R9 - Operational Logs   | INFO 30 ימים; ERROR 90; Security/Trade alerts 365 ימים.                                                           | לפי חומרה.                                        |
| R10 - Dead Letter       | אירועים שלא עובדו.                                                                                                | 90 ימים לאחר סגירה; פתוחים אינם נמחקים.           |
| R11 - Idempotency       | פעולות רגילות 30 ימים; Order keys מוצמדים.                                                                        | Order-related נשמר R0.                            |

# 13. תהליך מחיקה ו-Archive

**1.** Retention Worker מזהה פריטים שהגיעו לתאריך תפוגה.

**2.** הוא מוודא שאין Evidence Link, Pin, Legal Hold טכני, Open Review או Reprocessing Job.

**3.** נוצר Retention Candidate Report לפני מחיקה פיזית.

**4.** מחיקה מתבצעת לפי Batch קטן ומתועדת ב-audit_events.

**5.** Raw Object נמחק רק כאשר reference_count=0.

**6.** Partition ישן יכול לעבור Export דחוס לפני DROP.

**7.** לא מוחקים Decision, Fill, Order או Audit קריטי באמצעות Retention Worker.

# 14. תקציב אחסון לשרת Oracle

בהתבסס על כ-112GB מקום פנוי שדווח בשרת, המערכת תוגדר עם Budget ולא תשתמש בדיסק עד אפס מקום. הערכים הם גבולות תפעוליים ראשוניים וניתנים לשינוי.

| **קטגוריה**                 | **Budget** | **כלל**                                       |
|-----------------------------|------------|-----------------------------------------------|
| PostgreSQL Active + Indexes | 20GB       | Warning 16GB; Critical 19GB.                  |
| Raw/Object Store Active     | 30GB       | Compression + Dedup; Archive כאשר חורג.       |
| Local Backups               | 20GB       | Rotation; עותק חיצוני יוגדר במסמך Operations. |
| Logs/Temp/Exports           | 5GB        | Auto cleanup.                                 |
| Growth Reserve              | לפחות 25GB | אין לצרוך בתפעול רגיל.                        |

## 14.1 ספי דיסק

> **•** 70% שימוש: WARNING ודו״ח מגמות.
>
> **•** 80% שימוש: Archive/Cleanup מואץ ועצירת Raw Attachments לא קריטיים.
>
> **•** 90% שימוש: עצירת קליטה לא קריטית; שמירת Decisions/Orders/Fills בלבד.
>
> **•** 95% שימוש: EMERGENCY_STOP לפעולות היוצרות נתונים רבים; אין לפגוע בניהול פוזיציות פתוחות.

# 15. גיבוי ושחזור - דרישות נתונים

| **רכיב**                 | **דרישה**                                            | **הערה**                      |
|--------------------------|------------------------------------------------------|-------------------------------|
| PostgreSQL               | גיבוי יומי + WAL/Incremental לפי כלי שייבחר.         | נבדק בשחזור תקופתי.           |
| Object Store             | גיבוי Incremental לפי Hash.                          | אין צורך להעתיק כפילויות.     |
| Configs/Policies/Prompts | Version control + DB export.                         | כל Active Pointer נכלל.       |
| Broker State             | לא גיבוי בלבד; Reconciliation מול הברוקר לאחר שחזור. | הברוקר הוא מקור אמת לביצועים. |
| Secrets                  | אינם נכללים ב-DB dump רגיל.                          | נוהל נפרד במסמך Security.     |

## 15.1 תנאי שחזור

> **•** לאחר Restore המערכת עולה ל-MAINTENANCE.
>
> **•** נבדקים Integrity Hashes וגרסאות Migration.
>
> **•** מתבצע Broker Reconciliation לפני פתיחת פקודות חדשות.
>
> **•** Active Version Pointers ו-Environment Allowlist נבדקים ידנית.
>
> **•** רק לאחר Validation המערכת עוברת ל-RESEARCH/PAPER.

# 16. פרטיות, סודיות והרשאות ברמת הנתונים

> **•** Secrets אינם נשמרים בטבלאות; נשמר secret_ref בלבד.
>
> **•** Source identifiers רגישים יכולים להישמר מוצפנים או עם Alias; ההכרעה הטכנולוגית תוגדר במסמך Security.
>
> • Raw WhatsApp/Discord/Telegram מוגבל ל-OWNER/REVIEWER מורשה.
>
> **•** Dashboard מציג Masking למספרי טלפון ופרטים שאינם דרושים למסחר.
>
> **•** Audit Event נוצר לכל צפייה/ייצוא של Raw Evidence רגיש כאשר יופעל Live.
>
> **•** מחיקה לפי Retention חייבת לכלול DB, Object Store ו-Search Index אם יתווסף.

# 17. Views ו-Read Models

| **View**               | **מטרה**                                                            |
|------------------------|---------------------------------------------------------------------|
| v_candidate_workspace  | Candidate + Instrument + latest Entity + Feature + Decision status. |
| v_pending_approvals    | הצעות בתוקף שממתינות לאישור.                                        |
| v_open_positions       | פוזיציות, הגנות, P&L ו-Exit Triggers.                               |
| v_source_performance   | ביצועי מחבר/ערוץ לפי אסטרטגיה ואופק.                                |
| v_strategy_performance | Champion/Challenger וגרסאות.                                        |
| v_connector_health     | מצב אחרון, Lag, Failures ו-Alerts.                                  |
| v_storage_usage        | נפח לפי Retention Class ו-Partition.                                |

# 18. סדר Migrations עבור Claude Code

**1.** 0001_core: workspaces, users, roles, audit_events.

**2.** 0002_sources: connectors, accounts, channels, authors, source_events, raw_objects.

**3.** 0003_instruments_entity: instruments, aliases, resolution, source instructions.

**4.** 0004_candidates_market: candidates, evidence links, market snapshots, providers.

**5.** 0005_ai_features: model/prompt versions, analysis, features.

**6.** 0006_strategy_policy: strategy, policy, assumptions, active pointers.

**7.** 0007_decision_risk: evaluations, decisions, snapshots, risk, approvals.

**8.** 0008_trade_execution: plans, intents, brokers, orders, fills, positions.

**9.** 0009_exit_review: exit triggers/decisions, metrics, reviews, experiments.

**10.** 0010_ops_retention: health, alerts, dead letter, retention jobs and views.

## 18.1 כללי Migration

> **•** כל Migration קדימה בלבד; תיקון נעשה ב-Migration חדש.
>
> **•** אין DROP/ALTER מסוכן ללא Backfill Plan ו-Rollback/Restore Plan.
>
> **•** Schema Version נשמר בטבלת schema_migrations.
>
> **•** Contract Tests חייבים לרוץ מול DB ריק ומול DB משודרג.
>
> **•** Seeds כוללים רק Reference Data ו-Mocks; לא Credentials.

# 19. קריטריוני קבלה

| **מזהה** | **קריטריון**                                                    |
|----------|-----------------------------------------------------------------|
| DM-AC-01 | הודעה, Revision וקובץ נשמרים ללא כפילות ועם Hash.               |
| DM-AC-02 | ניתן לשמור Source Plan ו-System Trade Plan נפרדים ולקשר ביניהם. |
| DM-AC-03 | Stop באחוזים אינו תקף ללא Anchor.                               |
| DM-AC-04 | Decision Snapshot משחזר את כל IDs והגרסאות ששימשו החלטה.        |
| DM-AC-05 | אותו idempotency_key אינו יוצר שתי פקודות.                      |
| DM-AC-06 | Partial Fill מעדכן Position והגנות בלי לאבד היסטוריה.           |
| DM-AC-07 | Source Event מוצמד אינו נמחק גם לאחר TTL רגיל.                  |
| DM-AC-08 | Raw Object נמחק רק לאחר שאין אליו References.                   |
| DM-AC-09 | Partition ישן ניתן ל-Archive ולשחזור.                           |
| DM-AC-10 | שינוי Retention Policy אינו דורש Build.                         |
| DM-AC-11 | שחזור DB מחייב Reconciliation לפני פקודות חדשות.                |
| DM-AC-12 | ניתן להחליף Broker/Market Provider בלי לשנות טבלאות החלטה.      |

# 20. החלטות פתוחות

| **מזהה**   | **החלטה**                                                               |
|------------|-------------------------------------------------------------------------|
| OPEN-DM-01 | בחירת Object Store מקומי/S3-compatible ונתיב Volume.                    |
| OPEN-DM-02 | בחירת כלי Backup ועותק Off-site.                                        |
| OPEN-DM-03 | קצב Market Data בפועל והאם partition יומי או חודשי.                     |
| OPEN-DM-04 | Retention סופי ל-Raw WhatsApp/Discord/Telegram/Telegram ודרישות פרטיות. |
| OPEN-DM-05 | האם לשמור Embeddings; אין להוסיף לפני Use Case מדיד.                    |
| OPEN-DM-06 | האם יופעל Search Engine נפרד או PostgreSQL Full Text בלבד.              |
| OPEN-DM-07 | RPO/RTO סופיים.                                                         |
| OPEN-DM-08 | הצפנת עמודות והפרדת Database Roles.                                     |

# 21. הנחיה מחייבת ל-Claude Code

| **Claude Code רשאי להשתמש במסמך זה ליצירת migrations, schemas, repositories, fixtures ו-contract tests. הוא אינו רשאי לקצר את המודל על ידי איחוד Source Instruction עם System Trade Plan, Decision עם Order, או Broker Order עם Fill. כל ערך Retention, Budget או Status פתוח ייושם כ-Config/Reference ולא כקבוע מפוזר בקוד.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 24. מודל נתונים קנוני נוסף

| **הפרק משלב את תיקוני Point-in-Time, Inbox, Instrument Master, State Machines, Runtime Sessions ו-Tamper-Evident Audit.** |
|---------------------------------------------------------------------------------------------------------------------------|

## 24.1 Instrument Master

| **טבלה**                     | **תפקיד**                                                 |
|------------------------------|-----------------------------------------------------------|
| instruments                  | instrument_id פנימי, asset_class, currency, status.       |
| instrument_symbols           | symbol, exchange, valid_from, valid_to.                   |
| provider_instrument_mappings | provider_id, external_instrument_id, valid_from/to.       |
| broker_instrument_mappings   | broker_id, external_instrument_id, capabilities metadata. |
| corporate_actions            | type, effective_at, source, status.                       |
| instrument_halts             | reason, started_at, resolved_at, approval.                |

## 24.2 Point-in-Time Fields

> **•** observed_at — מועד האירוע בעולם החיצוני.
>
> **•** ingested_at — מועד קליטת המערכת.
>
> **•** effective_at — מועד תחולת הנתון.
>
> **•** available_at — המועד הראשון שבו הנתון היה מותר לשימוש בהחלטה.
>
> **•** superseded_at — מועד החלפה.
>
> **•** as_of — מועד חיתוך Decision/Replay.
>
> **•** Feature, Market, Fundamental, Source Score ו-Policy Snapshot חייבים לתמוך ב-As-of Join.

## 24.3 Outbox ו-Consumer Inbox

| **טבלה**        | **שדות חיוניים**                                                             |
|-----------------|------------------------------------------------------------------------------|
| outbox_messages | message_id, aggregate_id, type, payload, created_at, published_at, attempts. |
| consumer_inbox  | consumer_id, message_id, first_seen_at, completed_at, result_hash, status.   |
| dead_letters    | message_id, stage, error_code, attempts, next_action.                        |

> **•** Unique(consumer_id, message_id) מונע עיבוד כפול ב-At-Least-Once delivery.

## 24.4 Orders, Positions ו-Single Writer

| **טבלה/שדה**                   | **דרישה**                                           |
|--------------------------------|-----------------------------------------------------|
| order_intents.canonical_status | CanonicalOrderStatus בלבד.                          |
| broker_order_events            | Append-only; provider status נשמר ב-raw_status.     |
| positions.generation           | מונה אופטימי לכל שינוי.                             |
| position_commands              | idempotency_key, expected_generation, command_type. |
| position_locks                 | logical lock/lease עבור Exit Coordinator.           |
| submit_authorizations          | חד-פעמי, expiry, market/account/risk snapshot refs. |

## 24.5 Confidence ו-Source Score Snapshots

| **טבלה**                | **שדות**                                                                                 |
|-------------------------|------------------------------------------------------------------------------------------|
| ai_assessments          | model_reported_confidence, calibrated_confidence, calibration_status, benchmark_version. |
| source_score_snapshots  | source_id, strategy_id, horizon, score, sample_size, valid_from, valid_to, as_of.        |
| decision_source_context | decision_id, source_score_snapshot_id.                                                   |

## 24.6 Runtime Sessions

> **•** runtime_session_metadata נשמרת ב-DB ללא הערך הסודי.
>
> **•** Encrypted Runtime Session Store מחזיק Ciphertext, key_version, connector_id ו-expiry.
>
> **•** Session rotation/revocation נרשמים ב-Audit.
>
> **•** אין Session content ב-JSONB עסקי או בלוגים.

## 24.7 Tamper-Evident Audit

| **שדה**             | **משמעות**                              |
|---------------------|-----------------------------------------|
| prev_event_hash     | Hash של האירוע הקודם בשרשרת.            |
| event_hash          | Hash קנוני של תוכן האירוע וה-prev hash. |
| checkpoint_id       | Checkpoint חתום/מוצפן מחוץ למסד.        |
| checkpoint_at       | מועד פרסום checkpoint.                  |
| verification_status | VALID \| BROKEN \| UNKNOWN              |

## 24.8 Partitioning ו-Retention

> **•** הסכמות יהיו Partition-ready אך יצירת Partitions פיזיים תופעל רק כאשר נפח/זמן שאילתה עוברים Threshold.
>
> **•** Raw Evidence המקושר להחלטה נשמר באמצעות Evidence Pinning.
>
> **•** Offsite backup אינו מחליף Retention; מחיקה לוגית נשמרת ב-Audit.

## 24.9 UUID

> **•** Python 3.13 משתמש בספריית UUIDv7 תואמת RFC 9562 שננעלה בגרסה לאחר PoI; אין להניח uuid.uuid7 מה-stdlib.

# 25. מודל נתונים למחקר רב-מודלי ו-Signal Fusion

| **מודל הנתונים מורחב כך שכל מנוע מחקר מפיק Assessment עצמאי, וכל החלטת Fusion ניתנת לשחזור ללא תלות בהודעה חברתית או בספק יחיד.** |
|-----------------------------------------------------------------------------------------------------------------------------------|

## 25.1 טבלאות חדשות

| **טבלה**              | **תפקיד**                                                        | **Retention**               |
|-----------------------|------------------------------------------------------------------|-----------------------------|
| scanner_definitions   | הגדרת Scanner, Universe, Schedule, version ו-query manifest.     | Immutable versions          |
| scanner_runs          | כל הרצה, as_of, status, counts ו-errors.                         | Hot 12m; warm archive       |
| scanner_results       | Instrument, rank, trigger, evidence ו-run reference.             | Pin when candidate/decision |
| watchlists            | רשימת מעקב פנימית/חיצונית ו-owner.                               | Mutable + revisions         |
| watchlist_memberships | membership validity, reason ו-model version.                     | Point-in-time               |
| model_definitions     | משפחת מודל, task, owner ו-runtime requirements.                  | Reference                   |
| model_versions        | artifact, feature schema, training set, calibration ו-lifecycle. | Immutable                   |
| model_runs            | input snapshot, output, latency, cost ו-status.                  | Decision pinned             |
| signal_assessments    | פלט קנוני מכל מנוע ניתוח.                                        | Pinned with decisions       |
| technical_snapshots   | Indicators, patterns, levels ו-trigger state.                    | Time-series policy          |
| fundamental_snapshots | Ratios, statements, estimates, valuation ו-quality.              | Point-in-time               |
| news_assessments      | Catalyst, sentiment, severity, novelty, source cluster ו-veto.   | Evidence pinned             |
| regime_assessments    | market/sector regime, probability ו-calibration.                 | Time-series                 |
| external_signals      | TradingView/other raw and normalized signals.                    | Raw + normalized            |
| fusion_runs           | Strategy Manifest, required/optional/veto evidence ו-outcome.    | Immutable                   |
| fusion_evidence       | Assessment links, role, weight/condition ו-explanation.          | Immutable                   |
| training_datasets     | Dataset manifest, cutoffs, labels, leakage checks ו-hash.        | Immutable                   |
| model_evaluations     | OOS, calibration, drift, regime results ו-approval.              | Immutable                   |

## 25.2 SignalAssessment fields

| **שדה**                              | **דרישה**                                            |
|--------------------------------------|------------------------------------------------------|
| assessment_id                        | UUIDv7 internal ID.                                  |
| instrument_id                        | Instrument Master ID.                                |
| signal_family                        | TECHNICAL/FUNDAMENTAL/NEWS/QUANT_ML/EXTERNAL/SOCIAL. |
| model_version_id                     | Exact producing version.                             |
| direction/status                     | Canonical enums.                                     |
| score                                | Display/policy score; not probability.               |
| calibrated_probability               | Nullable; only when CALIBRATED.                      |
| observed_at/available_at/valid_until | Point-in-time and TTL.                               |
| evidence_json                        | Structured references and values.                    |
| risk_flags/invalidation              | Machine-readable blockers and invalidation.          |

## 25.3 Fusion lineage

> **•** fusion_runs references exact Strategy Definition, Manifest, Feature Snapshot, Policy Set and every Assessment.
>
> **•** fusion_evidence.role is REQUIRED, OPTIONAL, VETO or CONTEXT.
>
> **•** הסבר משתמש נשמר בנפרד מהחלטת המכונה ואינו משנה את Audit trail.
>
> **•** אות חיצוני נשמר גם כ-Raw payload וגם כ-normalized external_signal.

## 25.4 ML Artifact governance

> **•** Model binary נשמר ב-Object Store; metadata/hash/signature במסד.
>
> **•** אין overwrite ל-artifact פעיל.
>
> **•** Training cutoff ו-label availability נשמרים כדי למנוע Look-ahead.
>
> **•** Active Model Pointer נפרד לכל task, environment ו-strategy.

## 25.5 UI Read Models

| **Read Model**          | **תוכן**                                                            |
|-------------------------|---------------------------------------------------------------------|
| rm_overview_console     | Mode, kill state, account, risk, candidates, positions, alerts.     |
| rm_candidate_multimodel | Technical, fundamental, news, ML, external, social and fusion.      |
| rm_scanner_runs         | Run health, results, filters and candidate conversion.              |
| rm_model_registry       | Active/challenger versions, calibration, drift and promotion state. |
| rm_emergency_console    | Active halts, queued intents, broker sync and protective orders.    |

# **18. הרחבות מודל נתונים ל-Baseline הרב-מודלי**

| **טבלה**                     | **תפקיד**                                                                  |
|------------------------------|----------------------------------------------------------------------------|
| scan_runs                    | הרצת Scanner, Universe, query, schedule, started/ended, counts.            |
| signal_assessments           | פלט קנוני מכל מנוע ניתוח, role, score, probability, evidence, valid_until. |
| fusion_evaluations           | Manifest, required/soft/veto/bonus results ותוצאת Fusion.                  |
| policy_gate_results          | תוצאה מפורטת לכל Gate וקטגורייתו.                                          |
| opportunity_funnel_events    | מעבר Candidate בין שלבים וסיבת עצירה.                                      |
| counterfactual_shadow_trades | עסקאות וירטואליות עבור Candidates שלא בוצעו.                               |
| experiment_runs              | Replay/Backtest/Shadow experiment וגרסאות קלט.                             |
| dataset_snapshots            | רשימת מקורות, as_of, hash, split ו-lineage.                                |
| model_registry               | Model ID, family, artifact, status, calibration ו-approval.                |
| model_evaluations            | Metrics, slices, drift, fairness/robustness ו-acceptance.                  |
| portfolio_snapshots          | NAV, exposures, open risk, cash ו-strategy budgets.                        |
| capital_allocations          | תקציב לפי Strategy/Model/Regime ו-validity.                                |
| regime_snapshots             | Market regime, confidence, inputs ו-valid interval.                        |
| correlation_clusters         | Symbol/sector/theme cluster, methodology ו-version.                        |
| content_trust_assessments    | Provenance, source class, manipulation flags ו-trust score.                |
| quarantined_content          | Payload חשוד, סיבה, storage reference ו-review status.                     |
| emergency_commands           | Command, scope, actor, auth level, ack ו-result.                           |
| notification_events          | Severity, channel, delivery status, dedup key ו-ack.                       |

## **18.1 אילוצים ו-Lineage**

> **•** כל Assessment מקושר ל-model_version, feature_snapshot, dataset/source evidence ו-as_of.
>
> **•** Counterfactual Shadow אינו משנה Order/Position אמיתי ואינו נטמע ב-P&L החשבון.
>
> **•** Model artifact נשמר ב-Object Storage עם hash; המסד שומר Metadata ו-Approval state.
>
> **•** Emergency command הוא Append-only ואינו ניתן למחיקה רגילה.
>
> **•** Quarantined content אינו זמין ל-Fusion עד שחרור מפורש או Parser בטוח.
>
> **•** Partitioning מופעל רק לפי Threshold נפח; הסכמות נשארות Partition-ready.

# **30. הרחבת מודל הנתונים עבור Telegram**

## **30.1 telegram_source_profiles**

| **שדה**                 | **תיאור**                                        |
|-------------------------|--------------------------------------------------|
| profile_id              | UUID פנימי.                                      |
| access_mode             | BOT_API \| USER_ACCOUNT_TDLIB.                   |
| integration_id          | קישור ל-Integration Connection.                  |
| chat_id                 | מזהה 64-bit כמחרוזת קנונית כדי למנוע בעיות המרה. |
| chat_type               | PRIVATE \| GROUP \| SUPERGROUP \| CHANNEL.       |
| topic_id                | אופציונלי; Forum Topic.                          |
| display_name            | שם תצוגה בזמן הקליטה.                            |
| allowlisted             | האם המקור מורשה.                                 |
| ingestion_enabled       | האם הקליטה פעילה.                                |
| notification_target     | האם מותר לשלוח אליו Alerts פרטיים.               |
| secret_ref              | Reference בלבד; אין Token או Session בטבלה.      |
| created_at / updated_at | UTC.                                             |

## **30.2 telegram_event_metadata**

| **שדה**             | **תיאור**                                    |
|---------------------|----------------------------------------------|
| update_id           | מזהה Update כאשר זמין.                       |
| message_id          | מזהה הודעה בתוך Chat.                        |
| sender_id           | מזהה שולח כאשר זמין והרשאות הפרטיות מאפשרות. |
| reply_to_message_id | קישור ל-Reply.                               |
| topic_id            | קישור ל-Topic.                               |
| media_group_id      | קיבוץ Attachments.                           |
| forward_origin_json | מקור Forward; אינו אישור עצמאי.              |
| edit_date           | זמן עריכה.                                   |
| adapter_mode        | BOT_API או USER_ACCOUNT_TDLIB.               |
| revision            | מספר Revision פנימי append-only.             |

## **30.3 Constraints ו-Retention**

> **•** Unique key מומלץ: integration_id + chat_id + message_id + revision.
>
> **•** Bot token, api_hash, auth keys ו-TDLib session database אינם נשמרים בטבלאות הדומיין.
>
> **•** Raw content ו-Attachments כפופים לאותה מדיניות פרטיות ו-Retention של מקורות חברתיים.
>
> **•** מחיקה ב-Telegram יוצרת Tombstone/Revision; אינה מוחקת Audit או Decision Evidence שכבר ננעל.
>
> **•** Forwarded posts מאותו Content Cluster אינם נספרים כמקורות בלתי תלויים.

# **31. מודל אחיד ל-User/Bot Observations**

| **ישות/שדה**               | **דרישה**                                                                         |
|----------------------------|-----------------------------------------------------------------------------------|
| social_connection_profile  | provider, access_mode, identity_ref, required_capability, enabled, health_status. |
| access_mode                | WHATSAPP_USER \| DISCORD_USER \| TELEGRAM_USER_TDLIB \| TELEGRAM_BOT.             |
| required_capability        | TRUE עבור שלושת User Adapters; FALSE עבור Telegram Bot.                           |
| canonical_social_message   | זהות הודעה קנונית שאינה תלויה ב-Adapter יחיד.                                     |
| social_message_observation | תצפית נפרדת לכל User/Bot עם external IDs ו-received_at.                           |
| dedup_key                  | provider + canonical chat/source + message/post ID; fallback content cluster.     |
| session_ref                | Reference מוצפן בלבד; אין Session material בטבלת הדומיין.                         |

> **•** אותו Telegram post שנקלט גם ב-TDLib וגם ב-Bot API נשמר כ-Canonical Message אחד.
>
> **•** מחיקה או עריכה בכל מסלול יוצרת Revision append-only.
>
> **•** Identity aliasing אינו מאחד בטעות שני Chats שונים ללא Mapping מאושר.
