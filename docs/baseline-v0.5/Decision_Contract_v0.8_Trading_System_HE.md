תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

# 0. מטרת העדכון והכרעה בהערות הביקורת

| **גרסה 0.2 אינה מבטלת את v0.1. היא מחזקת ארבעה תחומים שהתבררו כחסרים: תקציבי זמן ונתיב מהיר, פתרון ישויות וטיקרים, הפרדת נתוני שוק מהברוקר, ובריאות תפעולית של Scrapers. ההערה שהמערכת כבר מוכנה לחלוטין לכתיבת קוד אינה מתקבלת: לפני יישום מלא נדרש גם אפיון פונקציונלי, מודל נתונים ומפרטי אינטגרציה.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **הערה**                           | **הכרעה**         | **נימוק**                                                                      | **שינוי במסמך**                                                            |
|------------------------------------|-------------------|--------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| Latency לאורך 14 שלבים             | מתקבלת            | השלבים אינם כולם סינכרוניים, אך חסרו Deadline, מסלולי עיבוד ומדדי זמן.         | נוספו Fast/Standard/Deep Path, תקציבי זמן, TTL ו-Stage Metrics.            |
| Entity Resolution קשה מטקסט חופשי  | מתקבלת            | אי אפשר להסתמך על קריאת LLM יחידה.                                             | נוסף Pipeline רב-שלבי, מצב NEEDS_REVIEW ופסילת טיקר עמום.                  |
| נתוני IBKR כנראה לא יספיקו         | מתקבלת חלקית      | ל-IBKR קיימות מגבלות קצב וקווי מידע, אך אין בסיס לקבוע מראש שאינו מתאים ל-MVP. | נוספה שכבת MarketDataProvider נפרדת ומבחן התאמה לפני בחירת ספק.            |
| התראת תקלות ל-InvestingPro Scraper | מתקבלת            | Parser שבור עלול להחדיר נתונים חסרים או ישנים.                                 | נוספו Canary, Drift Detection, Circuit Breaker ו-Health Dashboard.         |
| להתחיל מיד ב-JSON Schemas ובקוד    | לא מתקבלת בשלב זה | חוזי נתונים ללא אפיון פונקציונלי מלא עלולים לקבע מבנה שגוי.                    | השלב הבא הוא System Functional Specification; Schemas מדויקים ייגזרו ממנו. |
| המסמך ברמת Quant Fund              | לא החלטת מוצר     | זו הערכה מחמיאה, אך אינה דרישה הנדסית ואינה הוכחת איכות.                       | אין שינוי.                                                                 |
| A/B Testing מהיר                   | מתוקן             | בשוק אין חלוקה עצמאית ופשוטה כמו בניסוי מוצר רגיל.                             | המונח המחייב הוא Champion/Challenger על אותם Events עם Replay ו-Shadow.    |

# 1. החלטות נעולות

| **מזהה** | **החלטה**                                                                   |
|----------|-----------------------------------------------------------------------------|
| DC-001   | מנוע מודיעין וקבלת החלטות; לא סקריפט אינדיקטור יחיד.                        |
| DC-002   | שישה חודשים לפחות ב-IBKR Paper לפני מעבר אפשרי ל-Live.                      |
| DC-003   | Broker-Agnostic באמצעות BrokerAdapter.                                      |
| DC-004   | InvestingPro נקלט ב-Scraping אוטומטי דרך Adapter עצמאי.                     |
| DC-005   | בשלב הראשון אין Model Gateway כשירות נפרד; יש Clients פנימיים לספקי מודלים. |
| DC-006   | LLM אינו שולח פקודה ואינו קובע כמות.                                        |
| DC-007   | Decision Engine דטרמיניסטי; Risk Engine בעל Veto מוחלט.                     |
| DC-008   | כל חוקים, משקלים, ספים ומודלים גרסאיים וניתנים להחלפה ללא שינוי ליבת הקוד.  |
| DC-009   | Paper ו-Live מבודדים בהרשאות, תורים, חשבונות ו-Adapters.                    |
| DC-010   | Claude Code מיישם את המסמכים ואינו ממציא החלטות מוצר.                       |

# 2. מסלולי החלטה ותקציבי זמן

## 2.1 עיקרון

מודל המצבים נשאר מלא לצורך Audit, אך לא כל שלב חייב לעכב את ההחלטה. כל אסטרטגיה בוחרת Processing Profile גרסאי. התקציבים עצמם אינם מקודדים בקוד ויישמרו ב-Policy.

| **Profile** | **שימוש**                    | **שלבים סינכרוניים**                                                   | **הערה**                                                             |
|-------------|------------------------------|------------------------------------------------------------------------|----------------------------------------------------------------------|
| FAST        | חדשות/אירוע חברתי מתפרץ      | Entity Resolution מהיר, Market Cache, Features קריטיים, Decision, Risk | Enrichment לא קריטי וניתוח עמוק יכולים לרוץ לאחר יצירת WATCH/SHADOW. |
| STANDARD    | Swing, Breakout, המלצה רגילה | כל ה-Enrichment הנדרש, Critic לפי Policy, Decision, Risk               | ברירת המחדל.                                                         |
| DEEP        | פונדמנטלי/אירוע מורכב        | מסמכים, נתונים עמוקים, מספר מודלים וביקורת מלאה                        | אינו מיועד לכניסה תוך שניות.                                         |

## **2.2 הקשר זמן (Deadline Context)**

> {
>
> "processing_profile": "FAST\|STANDARD\|DEEP",
>
> "signal_observed_at": "...",
>
> "decision_deadline_at": "...",
>
> "max_signal_age_ms": 0,
>
> "stage_budgets_ms": {
>
> "entity_resolution": 0,
>
> "market_context": 0,
>
> "strategy_evaluation": 0,
>
> "risk_check": 0
>
> },
>
> "on_deadline": "REJECT\|WATCH\|SHADOW"
>
> }
>
> **•** כל שלב מקבל Deadline ומחזיר latency_ms ו-completed_within_budget.
>
> **•** כאשר נתון קריטי לא הושלם בזמן, אין להחליפו בהנחה חיובית.
>
> **•** נתונים אופציונליים יכולים להילקח מ-Cache רק אם Freshness Policy מאפשרת.
>
> **•** Critic LLM מופעל לפי Critic Activation Policy ולא בהכרח בכל אירוע FAST.
>
> **•** החלטה שפגה לפני ביצוע מקבלת DECISION_EXPIRED.

# **3. חוזה פתרון ישויות (Entity Resolution)**

## **3.1 תהליך מחייב**

| **שלב** | **רכיב**                 | **פעולה**                                                       |
|---------|--------------------------|-----------------------------------------------------------------|
| 1       | Deterministic Extraction | סימני \$, מילון טיקרים, שמות חברות, Cashtags וקישורים.          |
| 2       | LLM Context Analysis     | האם מדובר בנכס, בהשוואה, בציניות, בציטוט או בהמלצה.             |
| 3       | Instrument Validation    | בדיקה מול Instrument Master/Market Data/ברוקר שהנכס קיים ונסחר. |
| 4       | Disambiguation           | התאמת בורסה, סוג נכס, Class Share והקשר.                        |
| 5       | Decision                 | RESOLVED / NEEDS_REVIEW / INVALID.                              |

> {
>
> "resolution_status": "RESOLVED\|NEEDS_REVIEW\|INVALID",
>
> "candidates": \[
>
> {
>
> "symbol": "XYZ",
>
> "instrument_id": "internal-id",
>
> "asset_class": "STOCK",
>
> "exchange": "NASDAQ",
>
> "confidence": 0.93
>
> }
>
> \],
>
> "selected_instrument_id": "internal-id\|null",
>
> "intent": "ENTRY\|EXIT\|UPDATE\|DISCUSSION\|UNKNOWN",
>
> "direction": "LONG\|SHORT\|NEUTRAL\|UNKNOWN",
>
> "evidence_ids": \[\],
>
> "ambiguity_reasons": \[\],
>
> "resolver_version": "entity-resolver-0.2"
>
> }

## **3.2 כללי נסיגה (Fallback)**

> **•** אין מסחר אוטומטי כאשר resolution_status אינו RESOLVED.
>
> **•** NEEDS_REVIEW יכול להופיע במסך אישור אנושי אך אינו עובר ל-PAPER_AUTO.
>
> **•** INVALID נשמר לצורך Benchmark של מודל החילוץ.
>
> **•** כל תיקון ידני נשמר כתווית אמת לשיפור עתידי, בלי לשנות את האירוע המקורי.

# **4. הפרדת נתוני שוק מהברוקר**

| **ExecutionBroker ו-MarketDataProvider הם תפקידים נפרדים. IBKR יכול לממש את שניהם, אך המערכת אינה מניחה שספק הביצוע הוא גם ספק הנתונים הטוב ביותר לכל שימוש.** |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------|

> interface MarketDataProvider {
>
> capabilities(): Promise\<MarketDataCapabilities\>;
>
> resolveInstrument(query: InstrumentQuery): Promise\<Instrument\>;
>
> getSnapshot(request: SnapshotRequest): Promise\<MarketSnapshot\>;
>
> subscribe(request: SubscriptionRequest): AsyncIterable\<MarketEvent\>;
>
> getBars(request: BarsRequest): Promise\<BarSeries\>;
>
> getCorporateActions(request: CorporateActionRequest): Promise\<CorporateAction\[\]\>;
>
> healthCheck(): Promise\<ProviderHealth\>;
>
> }

## 4.1 בחירת ספק

| **קריטריון** | **בדיקה**                                          |
|--------------|----------------------------------------------------|
| כיסוי        | בורסות, מניות, ETF, אופציות, Pre/After Market.     |
| Latency      | זמן מקצה לקצה ויציבות בזמן עומס.                   |
| Granularity  | Quotes, Trades, Bars, NBBO, Tick-by-Tick לפי צורך. |
| Limits       | Subscriptions, Requests, Historical Pacing.        |
| Entitlements | הרשאות ומנויי נתונים.                              |
| עלות         | עלות חודשית והשלכה על MVP.                         |
| Replay       | יכולת לקבל נתונים היסטוריים תואמים ל-Live.         |

החלטת OPEN-03 לא נסגרת בגרסה זו. ייבנה Market Data Benchmark שמריץ את מקרי השימוש מול IBKR ומול ספק חיצוני אחד לפחות. רק לאחר מדידה ייקבע Primary Provider ו-Fallback.

# **5. חוזה אמינות ה-Scraper**

| **יכולת**              | **דרישה**                                                         |
|------------------------|-------------------------------------------------------------------|
| Parser Canary          | דף/מניה ידועים נבדקים מחזורית; שינוי שדות או מבנה מייצר כשל גלוי. |
| Schema Drift Detection | בדיקת Selector, סוג נתון, טווחים ושדות חובה.                      |
| Circuit Breaker        | לאחר רצף כשלים ה-Adapter מפסיק לפרסם נתונים ומסומן DEGRADED/DOWN. |
| No Silent Defaults     | שדה חסר אינו מוחלף ב-0, Neutral או ערך קודם כאילו הוא עדכני.      |
| Freshness Marking      | כל ערך כולל observed_at, scraped_at ו-freshness.                  |
| Alerting               | התראה על רצף כשלים, ירידה בכמות רשומות, שינוי Login או CAPTCHA.   |
| Parser Version         | כל שינוי חילוץ נרשם ב-parser_version.                             |
| Raw Snapshot           | נשמרת ראיה מספקת לבדיקת Parser ותחקור.                            |

> {
>
> "adapter_id": "investingpro",
>
> "health": "HEALTHY\|DEGRADED\|DOWN\|AUTH_REQUIRED",
>
> "last_success_at": "...",
>
> "consecutive_failures": 0,
>
> "schema_drift_detected": false,
>
> "parser_version": "investingpro-0.2",
>
> "published_events_last_hour": 0,
>
> "alert_codes": \[\]
>
> }

# **6. תוצאות ושערים קשיחים מעודכנים**

| **Outcome**  | **משמעות**                                  |
|--------------|---------------------------------------------|
| REJECT       | נפסל.                                       |
| WATCH        | מעקב עד Trigger נוסף.                       |
| SHADOW       | עסקה היפותטית.                              |
| APPROVE      | רשאי לעבור ל-Risk.                          |
| NEEDS_REVIEW | נדרשת הכרעה אנושית בגלל עמימות או מידע חסר. |
| EXPIRE       | פג תוקף.                                    |
| INVALID      | קלט/פלט אינו תקין.                          |

## **קודי פסילה קשיחים ראשוניים (Hard Veto):**

> DATA_STALE
>
> TICKER_AMBIGUOUS
>
> BROKER_UNSYNCED
>
> MARKET_DATA_UNAVAILABLE
>
> RISK_LIMIT_REACHED
>
> NO_EXIT_PLAN
>
> ORDER_NOT_SUPPORTED
>
> ENVIRONMENT_MISMATCH
>
> STRATEGY_NOT_PROMOTED
>
> DECISION_EXPIRED
>
> SCRAPER_DATA_INVALID
>
> MODEL_OUTPUT_INVALID

# 7. החלפת הנחות ללא שינוי קוד

המנגנון מ-v0.1 נשאר מחייב ומורחב: Assumption Catalog, Feature Registry, Strategy Manifest, Decision/Risk/Latency Policies, Prompt Registry, Source Config, Market Data Config ו-Broker Config.

> config/
>
> assumptions/
>
> features/
>
> strategies/
>
> policies/
>
> decision/
>
> risk/
>
> latency/
>
> critic/
>
> approval/
>
> prompts/
>
> sources/
>
> market-data/
>
> brokers/
>
> **•** כל ערך התנהגותי נטען בזמן ריצה דרך Config Loader מאומת.
>
> **•** Active Version Pointer קובע איזו גרסה פעילה.
>
> **•** שינוי גרסה אינו דורש Build או Deploy, אלא Validation ו-Audit.
>
> **•** גרסה חדשה עוברת REPLAY -\> SHADOW -\> PAPER_MANUAL -\> PAPER_AUTO.
>
> **•** Rollback מחזיר Pointer לגרסה קודמת.
>
> **•** שינוי אלגוריתם מהותי עשוי לדרוש Plug-in חדש; בחירתו עדיין נעשית דרך Strategy Manifest.

# **8. דרישות מעודכנות ל-Claude Code**

> **•** לא ליישם Pipeline סינכרוני יחיד לכל סוגי האותות; יש לתמוך ב-Processing Profiles.
>
> **•** לא לקשור Market Data ל-IBKR Adapter.
>
> **•** לא לסמוך על תשובת LLM יחידה לזיהוי טיקר.
>
> **•** לא לפרסם SourceEvent מ-Scraper כאשר Health אינו מאפשר זאת.
>
> **•** לא לקודד Latency Budgets, Thresholds או Provider IDs בקוד.
>
> **•** להקים Mocks ל-Source, Market Data, Model ו-Broker לפני אינטגרציות חיות.
>
> **•** לשמור Correlation ID ו-Deadline בכל המסלול.
>
> **•** כל שינוי ב-Contract מחייב Contract Tests ותאימות גרסה.

# 9. קריטריוני קבלה חדשים

| **מזהה** | **קריטריון**                                                         |
|----------|----------------------------------------------------------------------|
| AC-13    | FAST ו-STANDARD מעבדים אותו Event במסלולים נפרדים לפי Policy.        |
| AC-14    | חריגה מ-Deadline מחזירה Outcome מוגדר ואינה שולחת פקודה.             |
| AC-15    | טיקר עמום אינו מגיע ל-PAPER_AUTO.                                    |
| AC-16    | ניתן להחליף IBKR Market Data ב-Mock/Provider אחר ללא שינוי Strategy. |
| AC-17    | Schema Drift ב-InvestingPro עוצר פרסום נתונים ומייצר Alert.          |
| AC-18    | כל Stage מפרסם Latency, Freshness ו-Health Metrics.                  |
| AC-19    | NEEDS_REVIEW מוצג לאדם עם כל המועמדים והסיבות לעמימות.               |

# 10. מסמכים משלימים

> • System Functional Specification v0.7 — נוצר במקביל לגרסה זו.
>
> • Data Model and Retention Specification v0.6 — מסמך Baseline סמכותי שהושלם.
>
> • Integration Specifications — InvestingPro, Telegram, Discord, WhatsApp, Market Data, IBKR ו-AI.
>
> **•** Security and Operations Specification.
>
> **•** MVP Scope and Claude Code Implementation Plan.

# **11. חוזה הוראות מחיר, כניסה ויציאה**

| **הודעות ממקורות עשויות לכלול מחיר קנייה, טווח כניסה, מחיר יעד, Stop או הוראת עדכון. המערכת חייבת לשמר את ההוראה המקורית, לפרש אותה למבנה אחיד, לבדוק אותה מול נתוני השוק והסיכון, ולהפיק Trade Plan משלה. ההודעה עצמה היא ראיה והצעת מסחר — לא פקודה ישירה לברוקר.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **11.1 הבחנה בין מחיר לבין סוג הוראה**

| **סוג הוראה**     | **דוגמה**                    | **משמעות תפעולית**                                         |
|-------------------|------------------------------|------------------------------------------------------------|
| BUY_LIMIT_MAX     | "לקנות ב-25" או "עד 25"      | Limit Buy; מותר מילוי במחיר 25 או נמוך יותר.               |
| BUY_STOP_TRIGGER  | "לקנות מעל 25.10"            | Trigger לפריצה; Stop/Stop-Limit לפי Policy, לא Limit רגיל. |
| BUY_ZONE          | "כניסה 24.50-25.00"          | טווח כניסה; Strategy קובעת כיצד להציב פקודות בתוכו.        |
| ENTER_IMMEDIATELY | "לקנות עכשיו"                | מחייב בדיקת מחיר, Spread, Slippage ו-Max Chase.            |
| SELL_TARGET       | "למכור ב-30"                 | יעד רווח; יכול להיות מלא או חלקי.                          |
| STOP_PRICE        | "לצאת מתחת ל-23"             | מחיר ביטול/הגנה.                                           |
| STOP_PERCENT      | "למכור בירידה של 3% מהכניסה" | Stop מחושב ממחיר המילוי הממוצע או מעוגן לפי ההודעה.        |
| UPDATE            | "להעלות Stop ל-26"           | גרסת עדכון המקושרת לקריאה המקורית; אין דריסה של ההיסטוריה. |

## **11.2 Raw Source Plan לעומת System Trade Plan**

> **•** Raw Source Plan שומר בדיוק מה נכתב: מחיר, טווח, יעד, Stop, אחוז, זמן והקשר.
>
> **•** System Trade Plan הוא התכנון שאושר על ידי Strategy, Decision ו-Risk Engines.
>
> **•** לעולם אין לדרוס את ערכי המקור בערכי המערכת; שניהם נשמרים ומשווים בדיעבד.
>
> **•** כאשר מחיר המקור אינו תקף, מיושן, סותר או בלתי אפשרי — התוצאה היא WATCH, NEEDS_REVIEW או REJECT.
>
> {
>
> "source_trade_instruction": {
>
> "intent": "ENTRY\|EXIT\|UPDATE\|DISCUSSION",
>
> "direction": "LONG\|SHORT\|UNKNOWN",
>
> "entry_type": "BUY_LIMIT_MAX\|BUY_STOP_TRIGGER\|BUY_ZONE\|ENTER_IMMEDIATELY\|null",
>
> "entry_price": 25.00,
>
> "entry_range": {"min": 24.50, "max": 25.00},
>
> "targets": \[
>
> {"price": 28.00, "quantity_fraction": 0.50},
>
> {"price": 30.00, "quantity_fraction": 0.50}
>
> \],
>
> "source_stop": {"type": "PRICE\|PERCENT", "value": 23.00, "anchor": "ENTRY_FILL"},
>
> "valid_until": "...",
>
> "parse_confidence": 0.94,
>
> "ambiguities": \[\]
>
> },
>
> "system_trade_plan": {
>
> "entry_order": {},
>
> "protective_stop": {},
>
> "profit_targets": \[\],
>
> "max_chase": {},
>
> "time_exit": {},
>
> "strategy_version": "...",
>
> "risk_policy_version": "..."
>
> }
>
> }

## **11.3 כללי תקינות**

| **קוד**                     | **בדיקה**                                        | **תוצאה**                   |
|-----------------------------|--------------------------------------------------|-----------------------------|
| LONG_TARGET_BELOW_ENTRY     | יעד מכירה מתחת לכניסה בעסקת Long ללא הסבר מתאים. | INVALID/NEEDS_REVIEW        |
| LONG_STOP_ABOVE_ENTRY       | Stop מעל הכניסה בעסקת Long לפני שהפוזיציה ברווח. | INVALID/NEEDS_REVIEW        |
| PRICE_ALREADY_BEYOND_TARGET | בזמן הקליטה המחיר כבר עבר את היעד.               | EXPIRE או WATCH; אין רדיפה. |
| MAX_CHASE_EXCEEDED          | המחיר התרחק מעבר למגבלת הרדיפה.                  | REJECT/WATCH.               |
| PERCENT_ANCHOR_AMBIGUOUS    | נכתב "ירידה 3%" בלי לציין מאיזו נקודה.           | NEEDS_REVIEW.               |
| NO_PROTECTIVE_EXIT          | לא ניתן לבנות Stop/Invalidation תקינים.          | Hard Veto.                  |
| STALE_SOURCE_LEVELS         | רמות המחיר אינן רלוונטיות עוד לפי TTL.           | EXPIRE.                     |

## **11.4 סדר סמכויות וקדימות**

| **קדימות** | **רכיב**                      | **סמכות**                                                              |
|------------|-------------------------------|------------------------------------------------------------------------|
| 1          | Emergency/Kill Switch         | יכול לסגור או לחסום ללא המתנה ל-AI.                                    |
| 2          | Risk Engine                   | יכול לפסול, להקטין או להדק הגנה; אינו יכול להרחיב סיכון מעבר ל-Policy. |
| 3          | Active Strategy & Exit Policy | קובעים את Trade Plan המאושר.                                           |
| 4          | Explicit Source Levels        | נלקחים בחשבון רק לאחר Validation והתאמה לאסטרטגיה.                     |
| 5          | LLM Commentary                | מסביר ומחלץ; אינו גובר על חוקים או פקודות הגנה.                        |

## **11.5 ניהול העסקה לאחר כניסה**

> **•** החלטת "העסקה לא עבדה" חייבת להיות מוגדרת מראש באמצעות Stop, Invalidation, Time Exit או Risk Trigger.
>
> **•** Protective Stop אינו מחכה לקריאת LLM חדשה. ככל שהברוקר תומך, ההגנה תוצב כפקודה אצל הברוקר.
>
> **•** Position Manager מנטר מחיר, פקודות, Fills, יעדים, מצב השוק ואירועים חדשים.
>
> **•** Targets יכולים להיות מלאים או חלקיים. כמות פקודות היציאה מתעדכנת לפי Fill בפועל.
>
> **•** פקודות יעד ו-Stop ינוהלו כ-Bracket/OCO כאשר הדבר נתמך ומתאים.
>
> **•** Partial Fill מחייב התאמת Stop ויעדים לכמות שנקנתה בפועל.
>
> **•** עדכון של מקור על Stop/Target יוצר Instruction Revision חדש ומפעיל הערכה מחדש; הוא אינו משנה פקודה אוטומטית ללא Policy.

## **11.6 מצבי פוזיציה מחייבים**

| **מצב**          | **משמעות**                                     |
|------------------|------------------------------------------------|
| PLANNED          | Trade Plan אושר אך טרם נשלחה פקודה.            |
| ARMED            | תנאי כניסה ממתין ל-Trigger.                    |
| ENTRY_SUBMITTED  | פקודת הכניסה נשלחה.                            |
| PARTIALLY_FILLED | נרכשה כמות חלקית; ההגנות מותאמות לכמות בפועל.  |
| OPEN             | הפוזיציה פתוחה ומוגנת.                         |
| EXIT_SUBMITTED   | נשלחה פקודת יציאה מלאה או חלקית.               |
| PARTIALLY_CLOSED | חלק מהפוזיציה נסגר.                            |
| CLOSED           | הפוזיציה נסגרה במלואה.                         |
| ERROR            | אי התאמה או כשל המחייב Reconciliation/התערבות. |

## **11.7 קריטריוני קבלה**

| **מזהה** | **קריטריון**                                                      |
|----------|-------------------------------------------------------------------|
| AC-20    | המערכת מבדילה בין "לקנות ב-25" לבין "לקנות מעל 25".               |
| AC-21    | מחיר יעד ו-Stop נשמרים גם כערכי מקור וגם כ-Trade Plan מאושר.      |
| AC-22    | מחיר שעבר את היעד או את Max Chase אינו יוצר כניסה מאוחרת.         |
| AC-23    | Stop אחוזי מחושב מה-Anchor המוגדר ואינו מופעל כאשר ה-Anchor עמום. |
| AC-24    | לאחר Fill חלקי, כמות ה-Stop והיעדים שווה לכמות הפוזיציה בפועל.    |
| AC-25    | Protective Stop יכול לפעול ללא זמינות LLM.                        |
| AC-26    | עדכון של מקור יוצר Revision ואינו מוחק את ההוראה המקורית.         |
| AC-27    | אין פוזיציה פתוחה ללא Exit Plan תקף או Hard Veto מתועד.           |

# 12. Baseline קנוני מחייב לאחר הבדיקה הרוחבית

| **סעיף זה מרכז את חוזי ההחלטה המחייבים. הוא מחליף שמות ישנים וסוגר את חסמי CAR-001 עד CAR-015 הרלוונטיים למסמך.** |
|-------------------------------------------------------------------------------------------------------------------|

## 12.1 מילון קנוני

| **טיפוס**       | **ערכים מחייבים**                                                                                                  |
|-----------------|--------------------------------------------------------------------------------------------------------------------|
| SourceEntryType | BUY_LIMIT_MAX \| BUY_STOP_TRIGGER \| BUY_ZONE \| ENTER_IMMEDIATELY \| BUY_PULLBACK \| CONDITIONAL_ENTRY \| UNKNOWN |
| StrategyFamily  | SOCIAL \| NEWS_EVENT \| TECHNICAL \| EVENT \| FUNDAMENTAL \| EXIT                                                  |
| DecisionOutcome | REJECT \| WATCH \| SHADOW \| APPROVE \| NEEDS_REVIEW \| EXPIRE \| INVALID                                          |
| OrderRole       | ENTRY \| PROTECTIVE_STOP \| TAKE_PROFIT \| REDUCE \| EXIT                                                          |
| OrderType       | LIMIT \| STOP \| STOP_LIMIT \| MARKET                                                                              |
| PositionStatus  | OPENING \| OPEN \| REDUCING \| CLOSING \| CLOSED \| RECONCILIATION_REQUIRED                                        |

> **•** ENTER_IMMEDIATELY הוא Intent מהמקור בלבד. הוא אינו מתיר Market Order אוטומטי.
>
> **•** StrategyFamily ו-StrategyDefinition ID הם שדות נפרדים. לדוגמה: family=SOCIAL, id=social.explicit_plan.

## 12.2 Confidence ו-Calibration

| **שדה**                   | **שימוש**                                        |
|---------------------------|--------------------------------------------------|
| model_reported_confidence | Telemetry בלבד; אינו Gate.                       |
| calibrated_confidence     | הסתברות/ציון שעבר Benchmark וכיול על Golden Set. |
| calibration_status        | UNVERIFIED \| HEURISTIC \| CALIBRATED            |

> **•** PAPER_AUTO דורש calibration_status=CALIBRATED לכל משימת AI המשמשת Gate.

## 12.3 Final Pre-Submit Decision Gate

> Decision APPROVE
>
> -\> RiskDecision APPROVE/REDUCE
>
> -\> Approval valid (when required)
>
> -\> refresh market/account/calendar
>
> -\> re-run final risk + kill-state + environment checks
>
> -\> create immutable submit authorization
>
> -\> BrokerAdapter.submit_order
>
> **•** הבדיקה הסופית מתבצעת סמוך ככל האפשר לשליחה לברוקר.
>
> **•** שינוי מחיר, חשיפה, Buying Power, Session, Calendar או Kill Switch מבטל את ההרשאה הקודמת.
>
> **•** OrderIntent שפג אינו מתחדש אוטומטית; נדרש Decision Refresh.

## 12.4 Position Exit Coordinator

> **•** לכל Position יש Writer יחיד המורשה ליצור Exit Order.
>
> **•** Stop, Target, Source Update, Risk Reduction ו-Manual Exit נכנסים לאותו Coordinator.
>
> **•** ה-Coordinator מחזיק logical lock, generation number ו-idempotency key.
>
> **•** Trigger מאוחר או כפול נרשם אך אינו יוצר פקודה נוספת.

## 12.5 Canonical Order State Machine

| **מצב**          | **מעברים עיקריים**                                       |
|------------------|----------------------------------------------------------|
| CREATED          | VALIDATED \| REJECTED \| EXPIRED                         |
| VALIDATED        | QUEUED \| CANCELLED                                      |
| QUEUED           | SUBMITTING \| CANCELLED_BY_POLICY                        |
| SUBMITTING       | ACKNOWLEDGED \| UNKNOWN \| REJECTED                      |
| ACKNOWLEDGED     | PARTIALLY_FILLED \| FILLED \| CANCEL_PENDING \| REJECTED |
| PARTIALLY_FILLED | FILLED \| CANCEL_PENDING \| CANCELLED                    |
| CANCEL_PENDING   | CANCELLED \| FILLED \| UNKNOWN                           |
| UNKNOWN          | רק Reconciliation קובע מצב חדש                           |

## 12.6 Kill Switch ותורים

> **•** Execution Agent בודק Kill State בזמן צריכת ההודעה ושוב לפני Submit.
>
> **•** OrderIntent שטרם נשלח מסומן CANCELLED_BY_POLICY כאשר Entry Halt פעיל.
>
> **•** Kill Switch אינו מבטל אוטומטית Protective Orders קיימים.
>
> **•** FLATTEN הוא Command נפרד ואינו תוצאה אוטומטית של Halt.

## 12.7 זמן ו-Point-in-Time

> **•** כל Decision כולל as_of, trading_session_id ו-calendar_version.
>
> **•** כל Evidence כולל observed_at, ingested_at, effective_at ו-available_at.
>
> **•** אסור ל-Replay להשתמש בנתון ש-available_at שלו מאוחר מ-as_of.
>
> **•** America/New_York הוא אזור הזמן העסקי; האחסון נשאר UTC.

## 12.8 עצמאות מברוקר

> **•** IBKR הוא Adapter ראשון בלבד.
>
> **•** מזהי ברוקר נשמרים ב-BrokerInstrumentMapping ולא ב-Instrument או OrderIntent.
>
> **•** Capability יכולה להיות NATIVE, EMULATED, UNSUPPORTED או MANUAL_ONLY.
>
> **•** אין מעבר אוטומטי לברוקר אחר כאשר פקודה נמצאת ב-UNKNOWN.

## 12.9 קריטריוני קבלה נוספים

> **•** DC-AC-20: Final Pre-Submit Check נכשל אם המחיר/סיכון/אישור אינם עדכניים.
>
> **•** DC-AC-21: שני Exit Triggers מקבילים יוצרים לכל היותר Exit Order אחד.
>
> **•** DC-AC-22: Confidence גולמי של מודל אינו מאפשר Auto.
>
> **•** DC-AC-23: Queued Entry מבוטל כאשר Kill State הופעל לפני Submit.
>
> **•** DC-AC-24: Replay אינו רואה מידע עתידי.

# 13. חוזה החלטה רב-מודלי ואוטונומי

| **המערכת אינה תלויה בהודעת WhatsApp או Discord כדי ליצור עסקה. Candidate יכול להיווצר מסריקה טכנית, סריקה פונדמנטלית, אירוע חדשותי, דיווח רשמי, איתות חיצוני, מודל כמותי או מקור חברתי. כל המקורות עוברים לאותו חוזה החלטה ואינם שולחים פקודה ישירות לברוקר.** |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 13.1 משפחות מנועי הניתוח

| **מנוע**                    | **סמכות**                                                                     |
|-----------------------------|-------------------------------------------------------------------------------|
| Technical Analysis Engine   | מחיר, מחזור, תנודתיות, מבנה שוק, טריגרים ו-Invalidation.                      |
| Fundamental Analysis Engine | איכות, צמיחה, שווי, סיכון פיננסי ו-Watchlist Eligibility.                     |
| News/NLP Engine             | Catalyst, חומרה, אמינות, חידוש הידיעה ו-Veto חדשותי.                          |
| Quant/ML Engine             | דירוג, Regime, הסתברות מכוילת ו-Anomaly; אינו עוקף Risk.                      |
| External Signals Engine     | TradingView וספקי איתות אחרים כראיה חיצונית בלבד.                             |
| Social Signals Engine       | WhatsApp/Discord/Telegram כרעיון, חיזוק, מחיר מקור או עדכון; אינו תנאי למסחר. |

## 13.2 חוזה SignalAssessment

> {
>
> "instrument_id": "internal-id",
>
> "signal_family": "TECHNICAL\|FUNDAMENTAL\|NEWS\|QUANT_ML\|EXTERNAL\|SOCIAL",
>
> "model_id": "technical.breakout.v1",
>
> "model_version": "1.0.0",
>
> "direction": "LONG\|NEUTRAL\|NEGATIVE",
>
> "status": "TRIGGERED\|ELIGIBLE\|BLOCKED\|INCONCLUSIVE\|EXPIRED",
>
> "score": 0,
>
> "calibrated_probability": null,
>
> "calibration_status": "UNVERIFIED\|HEURISTIC\|CALIBRATED",
>
> "observed_at": "...",
>
> "available_at": "...",
>
> "valid_until": "...",
>
> "evidence": \[\],
>
> "risk_flags": \[\],
>
> "invalidation": {}
>
> }

## 13.3 Signal Fusion

> **•** אין ממוצע אוטומטי ופשוט בין ציונים. Strategy Manifest מגדיר אילו Assessments הם REQUIRED, OPTIONAL, VETO או CONTEXT.
>
> **•** Hard Veto, איכות נתונים, Calendar, Risk ו-Kill Switch קודמים לכל Ensemble או מודל ML.
>
> **•** מודל פונדמנטלי יכול לבחור מה להכניס ל-Watchlist, והמודל הטכני יכול לקבוע מתי להיכנס.
>
> **•** מודל NLP יכול לחסום טריגר טכני כאשר נמצא Catalyst שלילי מהותי ואמין.
>
> **•** מודל ML רשאי לדרג או לסווג Regime רק כאשר גרסתו ותהליך הכיול מאושרים למצב ההפעלה.

## 13.4 אוטונומיה ושליטה אנושית

> **•** ב-PAPER_AUTO המערכת רשאית לגלות Candidate, להחליט, לחשב סיכון, לבצע ולנהל יציאה ללא אישור לכל עסקה.
>
> **•** Emergency Halt זמין למפעיל מכל מסך ועוצר כניסות חדשות ופקודות שטרם נשלחו, בלי להסיר הגנות קיימות.
>
> **•** FLATTEN ACCOUNT הוא פעולה נפרדת, מפורשת ובעלת אימות מחמיר.
>
> **•** Resume לאחר Emergency Halt אינו אוטומטי ודורש Health, Reconciliation והרשאה.

## 13.5 קריטריוני קבלה

| **מזהה** | **קריטריון**                                                |
|----------|-------------------------------------------------------------|
| DC-MM-01 | Candidate נוצר גם כאשר Social Adapters כבויים.              |
| DC-MM-02 | TradingView/External Signal אינו שולח פקודה ישירות.         |
| DC-MM-03 | כל Assessment נשמר עם Model Version ו-Point-in-Time.        |
| DC-MM-04 | Fusion Decision מסביר Required, Optional ו-Veto evidence.   |
| DC-MM-05 | Emergency Halt גובר על כל Model, Strategy או queued intent. |

# **22. מדיניות גמישות מבוקרת ומניעת קיפאון**

| **בטיחות קשיחה חלה רק על זהות הנכס, תקינות נתונים, מצב הברוקר, מגבלות סיכון והגנת יציאה. איכות אסטרטגית שאינה מושלמת אינה הופכת אוטומטית ל-Hard Veto.** |
|---------------------------------------------------------------------------------------------------------------------------------------------------------|

| **סוג תנאי**      | **משמעות**                                | **תגובה**                                      |
|-------------------|-------------------------------------------|------------------------------------------------|
| SAFETY_HARD       | כשל שעלול ליצור עסקה שגויה או בלתי נשלטת. | REJECT או HALT בלבד.                           |
| STRATEGY_REQUIRED | תנאי הכרחי לאסטרטגיה המסוימת.             | WATCH/REJECT לאותה אסטרטגיה; אינו Gate גלובלי. |
| SOFT_SCORE        | איכות, התאמה או אי-ודאות.                 | הפחתת ציון, כמות או מעבר ל-Manual/Shadow.      |
| BONUS             | חיזוק שאינו חובה.                         | מעלה ציון או קדימות בלבד.                      |
| CONTEXT           | מידע תיאורי.                              | מוצג ונשמר; אינו משנה החלטה לבדו.              |

## **22.1 סולם פעולה**

| **Action Tier** | **תיאור**                              | **Risk Multiplier**      |
|-----------------|----------------------------------------|--------------------------|
| AUTO_FULL       | כל דרישות האסטרטגיה עברו; איכות גבוהה. | עד 1.00 לפי Policy.      |
| AUTO_REDUCED    | עסקה תקפה עם אי-ודאות שאינה בטיחותית.  | עד 0.70 לפי Policy.      |
| MANUAL          | דורשת שיקול אדם או חריגה מאושרת.       | עד 0.40-0.70 לפי Policy. |
| SHADOW          | נמדדת ללא פקודה.                       | 0                        |
| WATCH           | ממתינה לטריגר או מידע.                 | 0                        |
| REJECT          | אינה תקפה או לא כדאית.                 | 0                        |

## **22.2 כללים מחייבים**

> **•** אין דרישת מינימום עסקאות ואין הורדת Gate בטיחות כדי לייצר פעילות.
>
> • חוסר ב-WhatsApp, Discord, Telegram, TradingView או מודל משלים אינו פוסל עסקה אלא אם ה-Strategy Manifest הגדיר אותו REQUIRED.
>
> **•** כל Decision שומר action_tier, risk_multiplier, gate_results, supporting_assessments, vetoes ו-policy_versions.
>
> **•** לפני Submit מבוצעים Market Refresh, Portfolio Pre-Check, Risk Check, Kill-State Check ו-Approval Expiry Check.
>
> **•** תוכן אינטרנטי או חברתי לעולם אינו מפעיל כלי ברוקר או משנה Policy ישירות.

# **23. עצירת חירום בלתי תלויה בממשק הראשי**

> **•** GLOBAL_ENTRY_HALT זמין מה-Dashboard ומערוץ חירום נפרד ומצומצם.
>
> **•** ערוץ החירום אינו תלוי ברכיבי Scanner, AI, Fusion או Frontend המלא.
>
> **•** עצירה משמרת פקודות הגנה קיימות; FLATTEN_ACCOUNT הוא פעולה נפרדת ומאומתת.
>
> **•** אין Resume אוטומטי. חזרה לפעילות דורשת Reconciliation, Health Check, MFA ו-Audit.

# **50. Telegram כמקור Runtime לא-חוסם עם User Adapter חובה**

| Telegram אינו תלות Runtime גלובלית, אך Source Adapter דרך חשבון המשתמש הוא יכולת חובה. הוא אינו Gate גלובלי, אינו מחזיק סמכות ביצוע ואינו רשאי לשלוח פקודה ישירות לברוקר. |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

> **•** הודעה, פוסט בערוץ או עדכון ב-Telegram רשאים ליצור Candidate, Source Trade Instruction או Position Review.
>
> **•** המערכת תומכת במצב Bot API ובמצב חשבון משתמש; access_mode נשמר כחלק מהראיה.
>
> **•** אסטרטגיה אוטומטית המבוססת על Telegram חייבת לדרוש אימות עצמאי, אלא אם Manifest עתידי מאושר מגדיר אחרת.
>
> **•** ניתוק Telegram משפיע רק על אסטרטגיות שמגדירות אותו REQUIRED; יתר המערכת ממשיכה לעבוד.
>
> **•** שליחת התראות פרטיות לבעל המערכת מותרת דרך Notification Service, אך קבלת פקודות מסחר או Kill Commands דרך Telegram אינה כלולה ב-MVP.

# **51. חוזה מחייב לחיבורי משתמש חברתיים**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **מקור** | **מסלול משתמש חובה**                       | **מסלול נוסף**                   | **האם יכול להחליף את המשתמש** |
|----------|--------------------------------------------|----------------------------------|-------------------------------|
| WhatsApp | WhatsApp User Adapter באמצעות החשבון האישי | לא נקבע כתחליף ב-Baseline        | לא                            |
| Discord  | Discord User Adapter באמצעות החשבון האישי  | Bot עתידי אפשרי רק כתוספת מאושרת | לא                            |
| Telegram | USER_ACCOUNT_TDLIB באמצעות החשבון האישי    | BOT_API משלים ואופציונלי         | לא                            |

> **•** Acceptance של שכבת Social נכשל אם אחד משלושת מסלולי המשתמש החובה לא עבר PoI.
>
> **•** כאשר Telegram User ו-Telegram Bot קולטים אותו תוכן, המערכת יוצרת Evidence קנוני אחד ומקשרת אליו שתי Observations.
>
> **•** שום Adapter חברתי אינו מקבל הרשאת Broker, Risk override, Policy change או Emergency command.
>
> **•** חיבורי משתמש נשמרים ב-Runtime Session Store מוצפן ומנוהלים דרך Re-authentication מבוקר.
