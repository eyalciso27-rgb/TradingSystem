תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

| **המסמך מגדיר כיצד כל מקור מידע, ספק מודל, ספק נתוני שוק וברוקר מתחברים למערכת; אילו נתונים הם רשאים להפיק; כיצד מטפלים בניתוקים, כפילויות ושינויי מבנה; ואיך מונעים מאינטגרציה אחת להפוך לנקודת כשל של המערכת כולה.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 0. מסמכי בסיס והחלטות נעולות

| **מסמך**                                    | **תפקיד**                                                                      |
|---------------------------------------------|--------------------------------------------------------------------------------|
| Decision Contract v0.8                      | סמכויות, תוצאות החלטה, Profiles, Entity Resolution, Risk ו-Trade Instructions. |
| System Functional Specification v0.7        | התנהגות המערכת מקצה לקצה, מסכים, כשלים ויציאות.                                |
| Data Model and Retention Specification v0.6 | טבלאות, הפרדות, Retention, Idempotency ו-Storage.                              |

> **•** InvestingPro ייקלט באמצעות Scraping אוטומטי דרך Adapter מבודד.
>
> • WhatsApp, Discord ו-Telegram ייקלטו באמצעות החשבונות האישיים של המשתמש, דרך Adapters ייעודיים.
>
> **•** בגרסה הראשונה ספקי המודלים מחוברים ישירות באמצעות Clients פנימיים; אין שירות Model Gateway נפרד.
>
> **•** Interactive Brokers הוא ברוקר ה-MVP, תחילה ב-Paper, עם אפשרות עתידית ל-Live.
>
> **•** Market Data מופרד לוגית מהברוקר וניתן להחלפה.
>
> **•** כל Adapter, Provider ו-Broker חייבים Health, Metrics, Retry, Circuit Breaker וגרסה.

# 1. עקרונות אינטגרציה מחייבים

| **מזהה** | **עיקרון**          | **דרישה**                                              |
|----------|---------------------|--------------------------------------------------------|
| I-01     | Adapter Boundary    | לוגיקת הליבה אינה מכירה SDK, HTML או Session של ספק.   |
| I-02     | Canonical Events    | כל אינטגרציה ממירה מידע לחוזי מערכת אחידים.            |
| I-03     | No Silent Defaults  | חוסר נתון אינו הופך ל-0, Neutral או ערך ישן בלי סימון. |
| I-04     | Fail Isolated       | כשל באינטגרציה אינו מפיל רכיבים בלתי תלויים.           |
| I-05     | Replayable Raw      | נשמרת ראיה מספקת לבדיקת Parser/Normalizer בדיעבד.      |
| I-06     | Idempotent Delivery | חזרה על אותו Event או Order אינה יוצרת כפילות.         |
| I-07     | Explicit Freshness  | כל נתון כולל observed_at, received_at ו-freshness.     |
| I-08     | Versioned Parsing   | Parser, Normalizer, Mapping ו-Schema מקבלים גרסה.      |
| I-09     | Health Before Trust | נתון ממקור DEGRADED/DOWN אינו נצרך כאילו הוא תקין.     |
| I-10     | Replaceability      | ניתן להחליף ספק מבלי לשנות Strategy, Decision או Risk. |

# 2. חוזה משותף לכל אינטגרציה

## **2.1 ממשק מתאם אינטגרציה (Integration Adapter)**

> interface IntegrationAdapter {
>
> id: string;
>
> type: IntegrationType;
>
> version: string;
>
> initialize(configRef: string): Promise\<void\>;
>
> healthCheck(): Promise\<IntegrationHealth\>;
>
> capabilities(): Promise\<IntegrationCapabilities\>;
>
> collect(cursor?: Cursor): Promise\<IntegrationBatch\>;
>
> normalize(item: RawIntegrationItem): Promise\<CanonicalEvent\>;
>
> pause(reason: string): Promise\<void\>;
>
> resume(): Promise\<void\>;
>
> shutdown(): Promise\<void\>;
>
> }

## **2.2 בריאות האינטגרציה (Integration Health)**

> {
>
> "integration_id": "investingpro",
>
> "status": "HEALTHY\|DEGRADED\|DOWN\|AUTH_REQUIRED\|PAUSED",
>
> "checked_at": "...",
>
> "last_success_at": "...",
>
> "last_event_at": "...",
>
> "lag_seconds": 0,
>
> "consecutive_failures": 0,
>
> "error_rate_5m": 0.0,
>
> "schema_drift_detected": false,
>
> "auth_expires_at": null,
>
> "alert_codes": \[\]
>
> }

## 2.3 מצבי אינטגרציה

| **מצב**       | **משמעות**                                   |
|---------------|----------------------------------------------|
| INITIALIZING  | טעינת Config, Secrets ובדיקת קישוריות.       |
| HEALTHY       | עבודה תקינה.                                 |
| DEGRADED      | חלק מהיכולות או השדות אינם זמינים.           |
| AUTH_REQUIRED | נדרשת התחברות/QR/אימות מחדש.                 |
| RATE_LIMITED  | המקור הגביל קצב; מופעל Backoff.              |
| CIRCUIT_OPEN  | הפרסום נעצר לאחר רצף כשלים.                  |
| PAUSED        | הושהה ידנית או עקב תחזוקה.                   |
| DOWN          | אין חיבור או שאין יכולת להפיק נתונים אמינים. |

## **2.4 אירוע מקור אחיד (Canonical Source Event)**

> {
>
> "event_id": "evt\_...",
>
> "workspace_id": "ws\_...",
>
> "source_type": "WHATSAPP\|DISCORD\|TELEGRAM\|INVESTINGPRO\|NEWS\|FILING\|MARKET",
>
> "connector_id": "conn\_...",
>
> "source_account_id": "srcacct\_...",
>
> "channel_id": "channel\_...\|null",
>
> "author_id": "author\_...\|null",
>
> "external_id": "provider-id\|null",
>
> "event_type": "MESSAGE_CREATED\|MESSAGE_UPDATED\|PAGE_SNAPSHOT\|ALERT\|...",
>
> "observed_at": "...",
>
> "received_at": "...",
>
> "content_ref": "raw-object-ref",
>
> "content_hash": "sha256:...",
>
> "parser_version": "parser-...",
>
> "schema_version": "1.0",
>
> "freshness_seconds": 0,
>
> "quality": {
>
> "completeness": 0.0,
>
> "parse_confidence": 0.0
>
> },
>
> "metadata": {}
>
> }

# **3. מתאם WhatsApp לחשבון משתמש**

| מטרת ה-Adapter היא קליטה פסיבית של הודעות מהקבוצות שהוגדרו. ב-MVP אין שליחת הודעות, תגובות אוטומטיות או שינוי תכני הקבוצה. החיבור מנוהל כ-Session של חשבון המשתמש ומבודד משאר המערכת. |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 3.1 יכולות נדרשות

| **יכולת**       | **דרישה**                                                |
|-----------------|----------------------------------------------------------|
| Session Pairing | התחברות באמצעות תהליך Pairing/QR ושמירת Session מוצפן.   |
| Group Allowlist | קליטה רק מקבוצות שהוגדרו במפורש.                         |
| Message Capture | טקסט, זמן, מחבר, מזהה קבוצה ומזהה הודעה.                 |
| Reply Context   | הודעה מצוטטת/Reply כדי להבין עדכון לעסקה קודמת.          |
| Attachments     | Metadata וקובץ/Reference לתמונה, PDF או מסמך.            |
| Revisions       | זיהוי עריכה/מחיקה כאשר החיבור מספק זאת.                  |
| Reconnect       | חיבור מחדש ושחזור Cursor ללא כפילויות.                   |
| Update Linking  | שיוך 'להעלות סטופ', 'למכור חצי' או 'יצאתי' להוראה קודמת. |

## 3.2 אירועים

| **Event Type**                | **משמעות**         |
|-------------------------------|--------------------|
| WHATSAPP_MESSAGE_CREATED      | הודעה חדשה.        |
| WHATSAPP_MESSAGE_UPDATED      | שינוי תוכן ידוע.   |
| WHATSAPP_MESSAGE_DELETED      | מחיקה ידועה.       |
| WHATSAPP_ATTACHMENT_AVAILABLE | קובץ זמין לעיבוד.  |
| WHATSAPP_SESSION_CONNECTED    | Session פעיל.      |
| WHATSAPP_SESSION_DISCONNECTED | ניתוק.             |
| WHATSAPP_AUTH_REQUIRED        | נדרש Pairing מחדש. |

## 3.3 כללי עיבוד

> **•** הודעה נשמרת תחילה כ-Raw Event לפני ניתוח AI.
>
> **•** External Message ID הוא מפתח Dedup ראשי; Hash משמש Fallback.
>
> **•** Reply/Quoted Message נשמר כקישור, לא כהעתקה בלבד.
>
> **•** הודעת עדכון אינה משנה SourceTradeInstruction קיים; היא יוצרת Revision.
>
> **•** הורדת קבצים מתבצעת Worker נפרד כדי לא לעכב קליטת טקסט.
>
> **•** הודעה ללא תוכן מלא יכולה להישמר עם completeness נמוך ולהמתין להשלמה.
>
> **•** אין קידום אוטומטי למסחר כאשר הקשר ההודעה חסר או המחבר אינו מזוהה.

## 3.4 כשלים ותגובות

| **כשל**           | **תגובה**                                      |
|-------------------|------------------------------------------------|
| SESSION_EXPIRED   | AUTH_REQUIRED; עצירת קליטה והתראה.             |
| QR_REQUIRED       | מסך תפעולי מציג צורך ב-Pairing.                |
| GROUP_NOT_FOUND   | הקבוצה מסומנת MISSING; ללא החלפה אוטומטית.     |
| MESSAGE_GAP       | נרשם Gap; ניסיון Backfill אם נתמך, אחרת Alert. |
| ATTACHMENT_FAILED | הטקסט ממשיך; הקובץ מועבר ל-Retry/DLQ.          |
| DUPLICATE_EVENT   | נזרק בבטחה באמצעות Idempotency.                |

# **4. מתאם Discord לחשבון משתמש**

| **ה-Adapter מיועד לקריאת שרתים, ערוצים ו-Threads שהוגדרו מראש באמצעות חשבון המשתמש. הוא שירות מקור מבודד, ואינו מקבל גישה לברוקר, למנוע ההחלטות או ל-Secrets של אינטגרציות אחרות.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 4.1 יכולות נדרשות

| **יכולת**               | **דרישה**                                          |
|-------------------------|----------------------------------------------------|
| Guild/Channel Allowlist | מעקב רק אחר שרתים וערוצים שהוגדרו.                 |
| Message Capture         | טקסט, מחבר, זמן, Message ID, Channel ו-Guild.      |
| Threads and Replies     | Thread ID, Reply Reference והקשר קודם.             |
| Attachments and Embeds  | קבצים, תמונות, קישורים ו-Embeds.                   |
| Edits and Deletes       | Revision או Tombstone.                             |
| Reconnect/Resume        | חזרה לאחר ניתוק עם Cursor/Last Seen ID.            |
| Author Mapping          | שיוך משתמש לזהות מקור פנימית ושמירת Alias היסטורי. |
| Trade Update Linking    | קישור עדכון למחיר/עסקה המקוריים.                   |

## **4.2 סוגי אירועים**

| **Event Type**               | **משמעות**          |
|------------------------------|---------------------|
| DISCORD_MESSAGE_CREATED      | הודעה חדשה.         |
| DISCORD_MESSAGE_UPDATED      | עריכה.              |
| DISCORD_MESSAGE_DELETED      | מחיקה.              |
| DISCORD_THREAD_CREATED       | Thread חדש רלוונטי. |
| DISCORD_ATTACHMENT_AVAILABLE | קובץ/תמונה.         |
| DISCORD_SESSION_CONNECTED    | חיבור פעיל.         |
| DISCORD_SESSION_DISCONNECTED | ניתוק.              |
| DISCORD_AUTH_REQUIRED        | נדרש אימות מחדש.    |

## 4.3 כללי זהות ומקור

> **•** אין להשתמש בשם תצוגה כמזהה יחיד; נשמר External Author ID כאשר זמין.
>
> **•** שינוי Nickname אינו יוצר מקור חדש אלא Alias חדש.
>
> **•** ביצועי מקור נמדדים לפי Author, Channel, Strategy, Sector ו-Horizon.
>
> **•** Webhook, Bot או ציטוט של הודעה אחרת מסומנים בסוג מחבר נפרד.
>
> **•** הודעה שהועתקה ממקור אחר אינה נחשבת ראיה בלתי תלויה ללא בדיקה.

# **5. מתאם Scraper ל-InvestingPro**

| **ה-Scraper הוא מקור העשרה וגילוי, לא מקור האמת לביצוע. כל שדה נשמר עם זמן, עמוד מקור, Parser Version ואיכות. שינוי HTML או Login אינו רשאי לייצר ערכים שקטים או מיושנים.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 5.1 תחומי קליטה

| **תחום**            | **שדות**                                      |
|---------------------|-----------------------------------------------|
| Company Snapshot    | שם, טיקר, בורסה, סקטור ונתוני זיהוי.          |
| Fair Value          | ערך, Upside/Downside, שיטה/תאריך כאשר זמינים. |
| Financial Health    | ציון/קטגוריה ותתי-רכיבים כאשר זמינים.         |
| Fundamental Metrics | הכנסות, רווחיות, חוב, מכפילים וצמיחה.         |
| Analyst Data        | יעדים, קונצנזוס ועדכונים כאשר זמינים.         |
| Screener Results    | תוצאות מסננים שהוגדרו בקונפיגורציה.           |
| Watchlists/Alerts   | רשימות והתראות שהמערכת מסוגלת לקרוא.          |
| Page Evidence       | HTML/צילום/Raw Reference לצורכי Parser QA.    |

## **5.2 תהליך איסוף אוטומטי (Scraping)**

| **שלב** | **שם**        | **פעולה**                                     |
|---------|---------------|-----------------------------------------------|
| 1       | Session Check | בדיקת Login, Cookie/Session ועמוד Canary.     |
| 2       | Schedule      | יצירת Jobs לפי Priority, TTL ו-Rate Profile.  |
| 3       | Fetch         | טעינת עמוד ושמירת Response Metadata.          |
| 4       | Parse         | חילוץ באמצעות Parser Version פעיל.            |
| 5       | Validate      | שדות חובה, סוגים, טווחים, Cross-field checks. |
| 6       | Persist Raw   | שמירת Raw Snapshot דחוס לפי Retention.        |
| 7       | Publish       | יצירת SourceEvent/InvestingProSnapshot.       |
| 8       | Health Update | עדכון Success, Drift, Lag ושגיאות.            |

## **5.3 הגדרת Parser חיצונית**

> parser:
>
> id: investingpro-company
>
> version: 0.1.0
>
> page_type: COMPANY_OVERVIEW
>
> required_fields:
>
> \- instrument_identity
>
> optional_fields:
>
> \- fair_value
>
> \- financial_health
>
> \- analyst_target
>
> selectors:
>
> fair_value: "externalized-selector"
>
> validation:
>
> fair_value:
>
> type: decimal
>
> min: 0
>
> on_required_field_missing: FAIL_PAGE
>
> on_optional_field_missing: PUBLISH_PARTIAL

## **5.4 זיהוי שינוי מבנה ו-Circuit Breaker**

> **•** Canary Pages קבועים נבדקים לפני Batch משמעותי.
>
> **•** ירידה חריגה בכמות השדות/הרשומות מפעילה SCHEMA_DRIFT_SUSPECTED.
>
> **•** רצף כשלי Parser פותח Circuit ומפסיק פרסום נתונים חדשים.
>
> **•** AUTH_REQUIRED ו-CAPTCHA_DETECTED אינם מטופלים כ-Empty Result.
>
> **•** נתונים שכבר נשמרו נשארים זמינים אך מסומנים בגילם; אין הארכת Freshness מלאכותית.
>
> **•** Parser חדש מתחיל ב-Shadow Parsing מול אותו Raw לפני קידום.

## 5.5 תדירות וקדימויות

| **Priority** | **מקרה**                                   | **התנהגות**           |
|--------------|--------------------------------------------|-----------------------|
| URGENT       | מניה מתוך Candidate פעיל או Position פתוח. | לפי TTL ו-Policy.     |
| HIGH         | מניה שהוזכרה בקבוצה/חדשות.                 | קרוב לזמן האירוע.     |
| NORMAL       | Watchlist וסריקות שגרתיות.                 | מחזורי.               |
| LOW          | Backfill והיסטוריה.                        | רק כאשר קיימת קיבולת. |

# **6. מתאמי חדשות ודיווחים רשמיים**

## **6.1 מתאם חדשות**

> **•** Headline, summary/body reference, publisher, author, published_at, received_at.
>
> **•** Canonical URL/External ID ו-content hash למניעת כפילויות.
>
> **•** Tickers/entities שסיפק המקור לעומת אלה שזוהו על ידי המערכת.
>
> **•** Revision/correction/retraction כאשר המקור מעדכן כתבה.
>
> **•** Language, region, category ו-urgency.

## **6.2 מתאם דיווחים רשמיים**

> **•** דיווחים רשמיים, הודעות Investor Relations ומסמכים רגולטוריים.
>
> **•** Document ID, form/type, filing time, period ו-instrument mapping.
>
> **•** קובץ מקור נשמר ב-Object Store או כ-Reference בר-אימות.
>
> **•** דיווח רשמי מקבל evidence class נפרד מהעתקה עיתונאית.
>
> **•** תיקון/החלפה של מסמך יוצרים Revision.

## **6.3 מניעת כפילויות ועצמאות מקורות**

> **•** מספר כתבות המעתיקות אותה הודעה אינן נספרות כמקורות בלתי תלויים.
>
> **•** נבנה Story/Cluster ID לפי URL, Hash, ציטוטים וזמן.
>
> **•** Primary Source מקושר לכתבות המשניות.
>
> **•** אות קונצנזוס משתמש במספר Clusters בלתי תלויים ולא במספר כותרות.

# **7. אינטגרציית נתוני שוק**

| **שכבת Market Data היא עצמאית מ-IBKR Execution. IBKR רשאי לשמש Provider, אך המערכת תומכת ב-Primary, Secondary ו-Cache כדי שנוכל להחליף ספק ללא שינוי באסטרטגיות.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **7.1 חוזה ספק נתוני שוק**

> interface MarketDataProvider {
>
> id: string;
>
> version: string;
>
> capabilities(): Promise\<MarketDataCapabilities\>;
>
> resolveInstrument(query: InstrumentQuery): Promise\<InstrumentMatch\>;
>
> getSnapshot(request: SnapshotRequest): Promise\<MarketSnapshot\>;
>
> subscribe(request: SubscriptionRequest): AsyncIterable\<MarketEvent\>;
>
> getBars(request: BarsRequest): Promise\<BarSeries\>;
>
> getTradingCalendar(request: CalendarRequest): Promise\<TradingCalendar\>;
>
> getCorporateActions(request: CorporateActionRequest): Promise\<CorporateAction\[\]\>;
>
> healthCheck(): Promise\<IntegrationHealth\>;
>
> }

## 7.2 נתונים נדרשים

| **תחום**          | **נתונים**                                                   |
|-------------------|--------------------------------------------------------------|
| Reference Data    | Symbol, exchange, currency, asset class, conid/provider IDs. |
| Quotes            | Bid, Ask, sizes, last, timestamp.                            |
| Trades            | Price, size, exchange, timestamp לפי זמינות.                 |
| Bars              | OHLCV במרווחים הדרושים.                                      |
| Session           | Regular, Pre-market, After-hours, Halt/Closed.               |
| Corporate Actions | Splits, dividends, symbol changes.                           |
| Calendar          | Trading days, early close, holidays.                         |

## **7.3 רמות עדכניות נתונים (Freshness)**

| **Class**     | **שימוש**                              |
|---------------|----------------------------------------|
| REALTIME      | מתאים להחלטות וביצוע לפי Profile.      |
| NEAR_REALTIME | מתאים ל-Standard/Deep בלבד לפי Policy. |
| DELAYED       | מחקר/Replay; לא למסחר אוטומטי.         |
| STALE         | אינו מורשה להחלטה חדשה.                |
| UNKNOWN       | Fail Closed.                           |

## **7.4 כללי ספק ראשי וספק חלופי**

> **•** Fallback אינו מערבב Quotes משני ספקים באותו Snapshot בלי Source Attribution.
>
> **•** מעבר ספק באמצע החלטה יוצר Snapshot חדש ולא משנה Snapshot קפוא.
>
> **•** חוסר התאמה מהותי בין ספקים מייצר MARKET_DATA_CONFLICT.
>
> **•** אין Fallback לנתון Delayed כאשר Strategy דורשת Realtime.
>
> **•** Cache נצרך רק בתוך TTL שהוגדר ל-Feature ול-Processing Profile.

## **7.5 מבחן השוואה לפני בחירת ספק**

| **מדד**      | **בדיקה**                                    |
|--------------|----------------------------------------------|
| Coverage     | מניות/ETF, Pre/After, corporate actions.     |
| Latency      | P50/P95/P99 מקצה לקצה.                       |
| Completeness | שדות חסרים ו-Gaps.                           |
| Stability    | ניתוקים ו-Reconnect.                         |
| Limits       | Subscriptions, snapshots, historical pacing. |
| Replay Match | התאמה בין היסטוריה ל-Live.                   |
| Cost         | עלות חודשית לפי Universe ושימוש.             |

# **8. לקוחות ספקי AI**

| **אין Model Gateway כשירות רשת בגרסה הראשונה. עם זאת, כל Analyzer משתמש ב-Client פנימי אחיד, כדי שספק או מודל יוחלפו בקונפיגורציה ולא בתוך לוגיקת ההחלטה.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **8.1 משימות בינה מלאכותית**

| **Task**                     | **פלט**                                  |
|------------------------------|------------------------------------------|
| TICKER_ENTITY_EXTRACTION     | טיקר, חברה, נכס, בורסה ועמימות.          |
| TRADE_INSTRUCTION_EXTRACTION | Entry, target, stop, validity, revision. |
| INTENT_SENTIMENT             | כניסה/יציאה/עדכון/דיון וכיוון.           |
| NEWS_CATALYST_CLASSIFICATION | סוג אירוע, חומרה ומקור.                  |
| SOURCE_CONTEXT_SUMMARY       | הקשר Thread/Conversation.                |
| CRITIC_REVIEW                | סתירות, מידע חסר וסיבות אפשריות לכשל.    |
| POST_TRADE_CLASSIFICATION    | סיווג איכות החלטה ושגיאה.                |

## **8.2 חוזה Client**

> interface AIProviderClient {
>
> providerId: string;
>
> modelId: string;
>
> analyze\<TInput, TOutput\>(
>
> task: AITask,
>
> input: TInput,
>
> schema: JsonSchema\<TOutput\>,
>
> options: AIRequestOptions
>
> ): Promise\<ValidatedAIResponse\<TOutput\>\>;
>
> healthCheck(): Promise\<IntegrationHealth\>;
>
> }

## 8.3 כללי בקשה ותגובה

> **•** כל בקשה כוללת task, prompt_version, schema_version, deadline ו-correlation_id.
>
> **•** הפלט אינו נצרך לפני JSON Schema Validation.
>
> **•** Model Confidence נשמר בנפרד מ-Data Quality.
>
> **•** טקסט חופשי של המודל נשמר לצורכי Audit אך אינו מחליף שדות מובנים.
>
> **•** Timeout/Retry מוגדרים לפי Task; אין Retry בלתי מוגבל.
>
> **•** Fallback מופעל רק אם Task Policy מגדירה ספק חלופי.
>
> **•** כל שימוש נשמר עם token/cost/latency כאשר הספק מספק אותם.

## 8.4 בחירת מודל לפי משימה

| **מסלול**       | **עיקרון**                                                |
|-----------------|-----------------------------------------------------------|
| Extraction      | מודל זול ומהיר לאחר Benchmark.                            |
| Complex Context | מודל חזק יותר רק כאשר נדרש.                               |
| Critic          | מופעל לפי Critic Policy ולא לכל Event.                    |
| Replay/Batch    | מודל זול עם Batch/Concurrency מוגבלים.                    |
| Fallback        | ספק שני רק לאחר כשל טכני או פלט לא תקין, לא כדי 'להצביע'. |

# **9. אינטגרציית Interactive Brokers**

| **Baseline מוצע: Execution Agent נפרד המחובר ל-IB Gateway/TWS באמצעות TWS API. הבחירה הסופית בין TWS API ל-Web API תיבדק ב-Proof of Integration, אך חוזה הברוקר נשאר זהה.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **9.1 חוזה מתאם ברוקר**

> interface BrokerAdapter {
>
> connect(): Promise\<void\>;
>
> disconnect(): Promise\<void\>;
>
> capabilities(): Promise\<BrokerCapabilities\>;
>
> resolveInstrument(query: InstrumentQuery): Promise\<BrokerInstrument\>;
>
> getAccountSnapshot(accountAlias: string): Promise\<AccountSnapshot\>;
>
> getPositions(accountAlias: string): Promise\<BrokerPosition\[\]\>;
>
> getOpenOrders(accountAlias: string): Promise\<BrokerOrder\[\]\>;
>
> previewOrder(intent: OrderIntent): Promise\<OrderPreview\>;
>
> submitOrder(intent: OrderIntent): Promise\<BrokerOrderReceipt\>;
>
> modifyOrder(request: ModifyOrderRequest): Promise\<BrokerOrderReceipt\>;
>
> cancelOrder(request: CancelOrderRequest): Promise\<CancelReceipt\>;
>
> streamOrderEvents(): AsyncIterable\<BrokerOrderEvent\>;
>
> reconcile(accountAlias: string): Promise\<ReconciliationResult\>;
>
> healthCheck(): Promise\<IntegrationHealth\>;
>
> }

## **9.2 סביבת סוכן הביצוע (Execution Agent)**

> **•** תהליך נפרד ממנוע ההחלטות וממשק המשתמש.
>
> **•** מחזיק רק Secrets של הברוקר וה-Queue הרלוונטי לסביבה.
>
> **•** Paper ו-Live רצים ב-Instances/Configs נפרדים.
>
> **•** Client ID, Account Alias, Environment ו-Allowlist מחייבים התאמה.
>
> **•** אין גישה ישירה של Dashboard או LLM ל-API הברוקר.
>
> **•** בניתוק: אין פקודות חדשות עד Reconciliation מוצלח.

## 9.3 סוגי פקודות נדרשים ל-MVP

| **סוג**       | **שימוש**                                      |
|---------------|------------------------------------------------|
| LIMIT         | כניסה במחיר מרבי/מינימלי.                      |
| STOP          | כניסה בפריצה או הגנת הפסד.                     |
| STOP_LIMIT    | Trigger עם מגבלת מחיר.                         |
| MARKET        | רק כאשר Policy מפורשת מאפשרת.                  |
| BRACKET       | Parent Entry עם Take Profit ו-Protective Stop. |
| OCA/OCO       | יעדים/הגנות חלופיים לפי תמיכת הברוקר.          |
| TRAILING STOP | רק באסטרטגיות שיאושרו.                         |

## **9.4 פקודות Bracket וביצוע חלקי**

> **•** פקודות הגנה מקושרות ל-Parent Order ולכמות שמולאה בפועל.
>
> **•** Child Orders אינם משודרים לפני שהמבנה כולו תקין.
>
> **•** Partial Fill מעדכן Position, Protection Quantity ויעדים.
>
> **•** ביטול Parent לפני Fill מבטל Children בהתאם למצב הברוקר.
>
> **•** ביצוע יעד חלקי מקטין את Stop לכמות שנותרה.
>
> **•** כל Broker Event נשמר Append-only ומעובד Idempotently.

## **9.5 התאמה מול הברוקר (Reconciliation)**

| **מועד**         | **פעולה**                                              |
|------------------|--------------------------------------------------------|
| Startup          | Account, positions, orders, executions ו-buying power. |
| Periodic         | מחזורי בזמן פעילות.                                    |
| After Disconnect | חובה לפני חידוש פקודות.                                |
| After Error      | לאחר מצב לא ידוע/timeout.                              |
| End of Session   | צילום מצב והשוואה למסד.                                |

> **•** אי התאמה יוצרת Reconciliation Issue ו-BROKER_UNSYNCED.
>
> **•** הברוקר הוא מקור האמת ל-Order/Fill; המערכת היא מקור האמת ל-Decision/Intent.
>
> **•** Position נגזרת מ-Fills ומאומתת מול הברוקר.
>
> **•** אין תיקון שקט של היסטוריה; נרשם Position Adjustment Event.

## **9.6 הפרדת Paper ו-Live**

| **תחום**             | **דרישה**                         |
|----------------------|-----------------------------------|
| Credentials          | נפרדים.                           |
| Account Allowlist    | נפרד ומחייב.                      |
| Queues               | paper.orders / live.orders.       |
| Database Environment | שדה חובה בכל Order/Fill/Position. |
| UI Banner            | סימון ברור בכל מסך.               |
| Promotion            | Strategy חייבת סטטוס מתאים.       |
| Manual Approval      | חובה ב-LIVE_MANUAL.               |

# **10. תהליכים חוצי אינטגרציות**

| **זרימה**                    | **תיאור**                                                                                                                    |
|------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| W-01 Social Callout          | WhatsApp/Discord/Telegram -\> Raw -\> Entity -\> Source Instruction -\> Candidate -\> Market/InvestingPro/News -\> Strategy. |
| W-02 Explicit Trade Plan     | Message -\> Entry/Target/Stop extraction -\> validation -\> Source Plan -\> System Plan.                                     |
| W-03 Source Update           | Reply/Thread -\> link to Source Instruction -\> Revision -\> Position/Decision review.                                       |
| W-04 InvestingPro Enrichment | Candidate -\> scrape priority HIGH -\> validated snapshot -\> Features.                                                      |
| W-05 News Spike              | News/Filings -\> FAST Candidate -\> Market snapshot -\> Decision deadline.                                                   |
| W-06 Paper Entry             | Decision -\> Risk -\> Approval Policy -\> OrderIntent -\> IBKR Paper -\> Order Events.                                       |
| W-07 Protective Exit         | Broker/Market trigger -\> deterministic exit -\> Broker -\> Reconciliation.                                                  |
| W-08 Integration Failure     | Health change -\> circuit/fallback -\> affected Candidates WATCH/INVALID -\> Alert.                                          |

# **11. Retry, Backoff, Circuit Breaker ותור שגיאות**

| **סוג כשל**          | **מדיניות**                                            |
|----------------------|--------------------------------------------------------|
| Transient Network    | Retry עם exponential backoff ו-jitter.                 |
| Authentication       | ללא Retry אינסופי; AUTH_REQUIRED.                      |
| Rate Limit           | Retry-After או Backoff לפי Config.                     |
| Schema/Parser        | Circuit Open; אין Retry על אותו Parser ללא שינוי.      |
| Invalid Model Output | Retry מוגבל או Fallback לפי Task Policy.               |
| Broker Unknown State | לא שולחים שוב אוטומטית; Reconcile לפי Idempotency Key. |
| Attachment Download  | Retry נפרד; טקסט אינו נחסם.                            |

## **11.1 אירוע בתור שגיאות (Dead Letter)**

> {
>
> "dead_letter_id": "dlq\_...",
>
> "integration_id": "discord-user",
>
> "original_event_ref": "raw\_...",
>
> "failure_stage": "NORMALIZE",
>
> "error_code": "UNSUPPORTED_PAYLOAD",
>
> "attempt_count": 3,
>
> "first_failed_at": "...",
>
> "last_failed_at": "...",
>
> "next_action": "MANUAL_REVIEW\|REPROCESS\|DROP",
>
> "resolved_at": null
>
> }

# **12. הפניות ל-Secrets, Sessions וקונפיגורציה**

| זהו פירוט אינטגרטיבי בלבד. מדיניות אבטחה מלאה תוגדר במסמך Security and Operations. |
|------------------------------------------------------------------------------------|

> **•** אין Secret בתוך קובצי Git, טבלאות עסקיות או Logs.
>
> **•** Config מכיל secret_ref בלבד.
>
> • Session Stores של WhatsApp, Discord, Telegram, InvestingPro ו-IBKR נפרדים.
>
> **•** Worker מקבל רק Secrets הנחוצים לתפקידו.
>
> **•** Rotation/Re-auth יוצרים Audit Event.
>
> **•** צילום מסך, HTML ו-Payloads עוברים Redaction לפי Policy לפני תצוגה.
>
> **•** Logs אינם כוללים Token, Cookie, QR, Password או Account Credentials.

# **13. ניטור ותצפיתיות (Observability)**

| **תחום**     | **Metrics**                                                    |
|--------------|----------------------------------------------------------------|
| Availability | health_status, uptime, reconnect_count.                        |
| Throughput   | events_received, events_published, events_dropped.             |
| Latency      | source_lag, fetch_duration, parse_duration, end_to_end.        |
| Quality      | missing_fields, parse_confidence, drift_rate.                  |
| Errors       | error_rate, retries, circuit_open_count, dlq_count.            |
| AI           | requests, validation_failures, latency, token/cost.            |
| Market Data  | freshness, gaps, provider conflicts.                           |
| Broker       | connection, order_ack_latency, rejects, reconciliation issues. |

## **13.1 התראות**

| **רמה**  | **מקרים**                                                                                   |
|----------|---------------------------------------------------------------------------------------------|
| CRITICAL | Live/Paper mismatch, duplicate order risk, broker unknown state, credential leak suspicion. |
| HIGH     | IBKR disconnected with open positions, protective order missing, market data stale.         |
| MEDIUM   | Scraper drift, source auth required, AI provider degraded, event lag.                       |
| LOW      | Backfill delayed, noncritical field missing, scheduled re-auth approaching.                 |

# **14. אסטרטגיית בדיקות אינטגרציה**

| **סוג בדיקה**        | **מטרה**                                                |
|----------------------|---------------------------------------------------------|
| Contract Tests       | כל Adapter עומד ב-Canonical Schemas.                    |
| Golden Fixtures      | Raw samples קבועים להודעות, HTML, News ו-Broker events. |
| Parser Regression    | Raw ישן מורץ מול Parser חדש ונבדקים שינויים.            |
| Replay Tests         | אותם Events דרך גרסאות שונות.                           |
| Chaos Tests          | ניתוק, timeout, rate limit, duplicate ו-out-of-order.   |
| Broker Sandbox/Paper | פקודות, ביטולים, Partial Fills ו-Reconciliation.        |
| End-to-End Shadow    | מקור אמיתי עד Decision ללא פקודה.                       |
| Security Tests       | Secret leakage, permissions ו-log redaction.            |

## **14.1 מקרי בדיקה קבועים (Fixtures)**

> **•** הודעת 'קנה ב-25, יעד 30, סטופ 23'.
>
> **•** הודעת 'מעל 25 פריצה' לעומת 'עד 25'.
>
> **•** הודעת Reply: 'להעלות סטופ ל-26'.
>
> **•** הודעה צינית או שלילית עם טיקר.
>
> **•** הודעה עם שני טיקרים ואסטרטגיות שונות.
>
> **•** InvestingPro page תקין, שדה חסר, Login page ו-Schema drift.
>
> **•** Quote realtime, delayed, stale ו-conflicting.
>
> **•** IBKR order accepted, rejected, partial fill, cancel pending ו-disconnect.

# **15. טופולוגיית פריסה ראשונית**

> Oracle ARM Server
>
> ├── api
>
> ├── dashboard
>
> ├── ingestion-workers
>
> │ ├── whatsapp-user-adapter
>
> │ ├── discord-user-adapter  
> │ ├── telegram-bot-adapter  
> │ └── telegram-user-adapter
>
> │ ├── investingpro-scraper
>
> │ ├── news-filings
>
> │ └── market-data
>
> ├── ai-analyzers
>
> ├── decision-risk-workers
>
> ├── postgresql
>
> ├── object-store / file-volume
>
> ├── queue
>
> └── observability
>
> Execution Host / Agent
>
> ├── ib-gateway-or-tws
>
> ├── ibkr-paper-adapter
>
> └── future-ibkr-live-adapter
>
> **•** הטופולוגיה היא לוגית; בחירת Queue, Object Store וכלי Observability תיעשה במסמך הטכנולוגי.
>
> **•** Execution Agent יכול לרוץ על מארח נפרד אם דרישות IBKR מחייבות זאת.
>
> **•** אין חשיפת PostgreSQL, Queue או IBKR API ישירות לאינטרנט.

# 16. קריטריוני קבלה

| **מזהה**  | **קריטריון**                                         |
|-----------|------------------------------------------------------|
| INT-AC-01 | נפילת Adapter אחד אינה מפילה Adapters אחרים.         |
| INT-AC-02 | אותו External Event אינו יוצר שני SourceEvents.      |
| INT-AC-03 | WhatsApp/Discord/Telegram Reply נקשר להודעה המקורית. |
| INT-AC-04 | עדכון מחיר/Stop נשמר כ-Revision ולא מוחק מקור.       |
| INT-AC-05 | InvestingPro Drift מפסיק פרסום ומייצר Alert.         |
| INT-AC-06 | AI output לא תקין אינו עובר ל-Decision.              |
| INT-AC-07 | Market Data stale אינו מורשה ל-PAPER_AUTO.           |
| INT-AC-08 | ניתן להחליף MarketDataProvider ללא שינוי Strategy.   |
| INT-AC-09 | OrderIntent כפול אינו יוצר Broker Order כפול.        |
| INT-AC-10 | IBKR disconnect מחייב Reconciliation לפני חידוש.     |
| INT-AC-11 | Partial Fill מעדכן Protection ו-Position.            |
| INT-AC-12 | Paper event אינו יכול להישלח ל-Live queue/account.   |
| INT-AC-13 | כל Integration מפרסמת Health ו-Metrics.              |
| INT-AC-14 | Raw fixture ניתן לעיבוד מחדש ב-Parser Version חדש.   |

# 17. החלטות פתוחות

| **מזהה**    | **החלטה**                                                           |
|-------------|---------------------------------------------------------------------|
| INT-OPEN-01 | מימוש טכני מדויק של WhatsApp User Adapter.                          |
| INT-OPEN-02 | מימוש טכני מדויק של Discord User Adapter.                           |
| INT-OPEN-03 | בחירת Primary/Secondary Market Data לאחר Benchmark.                 |
| INT-OPEN-04 | בחירת TWS API או Web API כ-IBKR baseline לאחר Proof of Integration. |
| INT-OPEN-05 | ספקי News ו-Filings.                                                |
| INT-OPEN-06 | ספקי AI ומודל לכל Task.                                             |
| INT-OPEN-07 | תדירויות InvestingPro ו-Rate Profiles.                              |
| INT-OPEN-08 | Queue, Object Store ו-Observability stack.                          |
| INT-OPEN-09 | מיקום Execution Agent בפועל.                                        |
| INT-OPEN-10 | ערוצי Alert ואישור אנושי.                                           |

# 18. מקורות תיעוד שנבדקו

> **•** Interactive Brokers — TWS API Documentation.
>
> **•** Interactive Brokers — Web API / Client Portal API Documentation.
>
> **•** Interactive Brokers — Bracket Orders, Attaching Orders and OCA documentation.
>
> **•** Interactive Brokers — Market Data Subscriptions documentation.

# 19. השלב הבא

> • Security and Operations Specification v0.6.
>
> • Strategy and Decision Framework v0.5 — פירוט האסטרטגיות, הכניסות, היציאות והלמידה.
>
> • MVP Scope v0.6.
>
> • Technology and Deployment Decisions v0.6.
>
> **•** Claude Code Implementation Plan — רק לאחר השלמת ואישור כל המסמכים.

# 20. Baseline אינטגרציות מתוקן

| **הפרק סוגר את חסמי PoI, Field Ownership, Broker Capabilities, Runtime Sessions ו-News Scope.** |
|-------------------------------------------------------------------------------------------------|

## 20.1 Proof-of-Integration הוא Gate

| **אינטגרציה**         | **Exit Criteria**                                                                             |
|-----------------------|-----------------------------------------------------------------------------------------------|
| IBKR                  | Connect, account, instrument, limit/stop/bracket, partial fill, cancel, reconnect, reconcile. |
| WhatsApp              | pairing, stable IDs, allowlist, reply, reconnect, gap and auth expiry.                        |
| Discord               | channels, threads, edits, attachments, resume and auth expiry.                                |
| InvestingPro          | login, canary, 3-5 page types, field validation and drift.                                    |
| Market Data           | quote freshness, bars, calendar, ATR/ADV/spread source and limits.                            |
| Official Filings      | filing timestamps, documents, revisions and entity mapping.                                   |
| Runtime Session Store | encrypt, decrypt, rotate, revoke, restart and least privilege.                                |
| ARM Stack             | all selected containers and browser automation run on target architecture.                    |

## 20.2 Instrument Resolution

> **•** Adapters מחזירים external identifiers בלבד לשכבת mapping.
>
> **•** Instrument Master מחזיר instrument_id פנימי יחיד.
>
> **•** Symbol אינו Primary Key ואינו מזהה יציב.
>
> **•** כל Mapping גרסאי עם valid_from/valid_to.

## 20.3 Market Data Field Ownership

| **שדה**           | **Primary**                        | **Fallback/הערה**               |
|-------------------|------------------------------------|---------------------------------|
| Bid/Ask/Last      | Realtime MarketDataProvider        | אין fallback ל-delayed ב-Auto.  |
| Bars/OHLCV        | Primary MarketDataProvider         | Historical provider מאושר בלבד. |
| ATR               | Feature Engine על Bars קפואים      | formula_version חובה.           |
| ADV20 Dollar      | Feature Engine על Bars             | לא Scraper.                     |
| Spread            | Quote snapshot                     | timestamp ו-provider חובה.      |
| Market Cap        | Reference/Fundamental provider     | TTL ארוך יותר; stale marker.    |
| Trading Calendar  | Authoritative Calendar Provider    | America/New_York.               |
| Corporate Actions | Official/Broker/Reference provider | conflict -\> halt.              |

## 20.4 Broker Capability Matrix

| **יכולת**                    | **מצבים**                                        |
|------------------------------|--------------------------------------------------|
| LIMIT/STOP/STOP_LIMIT/MARKET | NATIVE \| UNSUPPORTED                            |
| BRACKET                      | NATIVE \| EMULATED \| UNSUPPORTED                |
| OCO/OCA                      | NATIVE \| EMULATED \| UNSUPPORTED                |
| TRAILING                     | NATIVE \| EMULATED \| MANUAL_ONLY \| UNSUPPORTED |
| FRACTIONAL                   | DISABLED ב-MVP                                   |
| OUTSIDE_RTH                  | DISABLED ב-MVP                                   |
| WHAT_IF                      | NATIVE \| UNSUPPORTED                            |
| STREAMING_EVENTS             | NATIVE \| POLLING_EMULATION                      |

> **•** Emulation דורשת Policy מפורשת ואינה ברירת מחדל. אין Emulation של הגנת Stop שאינה שורדת נפילת המערכת ב-PAPER_AUTO.

## 20.5 InvestingPro Field Catalog

| **Field Definition**     | **חובה**                                |
|--------------------------|-----------------------------------------|
| field_id                 | מזהה קנוני.                             |
| page_type                | סוג עמוד.                               |
| selector/parser          | גרסאי.                                  |
| data_type/unit/currency  | חובה.                                   |
| observed_at/available_at | חובה.                                   |
| ttl                      | לפי שדה.                                |
| validation               | range/cross-field.                      |
| raw_evidence_ref         | חובה לשדה שהשתתף בהחלטה.                |
| missing_behavior         | FAIL_PAGE \| PUBLISH_PARTIAL \| IGNORE. |

## 20.6 News ו-Filings

> **•** Official Filings ו-Investor Relations הם מקור חובה.
>
> **•** External News Provider הוא feature-flag עד בחירה ו-PoI.
>
> **•** news.catalyst_momentum אינו מועמד ל-Auto לפני timestamp semantics, clustering ו-Golden Set.
>
> **•** כתבות משוכפלות מקבלות Story Cluster יחיד.

## 20.7 Runtime Sessions

> **•** SOPS/age משמש ל-Secrets סטטיים בלבד.
>
> **•** Sessions דינמיים נשמרים ב-Encrypted Runtime Session Store.
>
> **•** Connector מקבל גישה רק ל-session namespace שלו.
>
> **•** AUTH_REQUIRED אינו Empty Feed ואינו נמחק על ידי Retry.

## 20.8 Generic Event Subjects

> events.source.\<source_type\>
>
> work.analysis.\<task\>
>
> orders.\<environment\>.\<broker\>
>
> broker.events.\<environment\>.\<broker\>
>
> positions.commands.\<environment\>
>
> deadletter.\<domain\>
>
> **•** Consumer Inbox חובה לכל Durable Consumer.

# 21. אינטגרציות אינטרנט, איתותים חיצוניים ונתוני מחקר

| WhatsApp, Discord ו-Telegram הם מקורות Runtime אופציונליים, אך User Adapter לכל אחד מהם הוא יכולת מימוש חובה. ליבת המערכת נשענת על Market Data, מקורות רשמיים, חדשות, Fundamental Data וסריקות פנימיות. TradingView הוא ספק איתות חיצוני אחד מתוך ממשק כללי. |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 21.1 ExternalSignalProvider

> interface ExternalSignalProvider {
>
> providerId: string;
>
> receive(payload: unknown, authContext: AuthContext): Promise\<RawExternalSignal\>;
>
> normalize(raw: RawExternalSignal): Promise\<CanonicalExternalSignal\>;
>
> healthCheck(): Promise\<IntegrationHealth\>;
>
> }

## 21.2 TradingView Adapter

> **•** Webhook endpoint ייעודי, TLS, high-entropy secret, rate limit, replay protection ו-idempotency.
>
> **•** Payload כולל provider alert ID, symbol, exchange, timeframe, condition, indicator values ו-fired_at כאשר זמינים.
>
> **•** האיתות אינו OrderIntent; הוא Candidate evidence בלבד.
>
> **•** המערכת מאמתת מחדש Symbol, Session, Price, Indicator/Trigger ו-TTL מול Market Data פנימי.
>
> **•** כשל ב-TradingView אינו מפסיק סריקות פנימיות או ניהול פוזיציות.

## 21.3 ספקי נתונים פונדמנטליים

| **תחום**             | **שדות מחייבים**                        | **Point-in-Time**                        |
|----------------------|-----------------------------------------|------------------------------------------|
| Financial Statements | revenue, margins, EPS, cash flow, debt  | report period, filed_at, available_at    |
| Valuation            | P/E, EV/EBITDA, P/S, PEG, peer medians  | as_of and currency                       |
| Estimates/Revisions  | consensus, revisions, analyst targets   | published_at/available_at                |
| Reference            | market cap, shares, sector, industry    | effective_at                             |
| InvestingPro         | fair value, health and selected metrics | scraped_at, parser version, stale marker |

## 21.4 חדשות ואינטרנט

> **•** Primary official sources: SEC/IR/company releases and authoritative market notices.
>
> **•** News provider supplies discovery; official source is preferred for material fact verification.
>
> **•** Public web retrieval is allowed only according to licensing, robots/terms and Source Policy.
>
> **•** כל ידיעה עוברת clustering, novelty, correction/retraction and available_at.

## 21.5 מודלים ו-Artifacts

> **•** AI Provider Client מיועד ל-NLP/extraction/reasoning tasks; ML Runtime loads approved local/remote artifacts through ModelRuntimePort.
>
> **•** Model Registry is not a provider SDK and exposes model_id, version, schema, calibration and status.
>
> **•** Inference failure affects only strategies that require that assessment; deterministic exits continue.

## 21.6 PoI חדש

| **PoI**              | **הוכחה**                                                                          |
|----------------------|------------------------------------------------------------------------------------|
| TradingView          | secure webhook, duplicate replay, TTL, symbol mapping and internal revalidation.   |
| Fundamental Provider | point-in-time statements, revisions, units, corporate actions and licensing.       |
| Technical Data       | bars/quotes quality sufficient to reproduce scanner triggers.                      |
| ML Runtime           | ARM-compatible inference, artifact verification, deterministic schema and latency. |

# **20. מקורות אינטרנט, איתותים והתראות - הרחבת Baseline**

| **Adapter/Port**          | **דרישה**                                                             |
|---------------------------|-----------------------------------------------------------------------|
| WebResearchProvider       | איסוף מאתרים מאושרים, Provenance, timestamp, snapshot ו-content hash. |
| OfficialFilingsProvider   | SEC/IR ומקורות רשמיים עם Source Class גבוה.                           |
| TradingViewWebhookAdapter | Webhook חתום, nonce, timestamp, replay protection ו-allowlist.        |
| ExternalSignalProvider    | חוזה כללי שאינו מאפשר ביצוע ישיר.                                     |
| NotificationProvider      | Email/Push/Chat בעתיד דרך Port כללי; אין תלות בערוץ יחיד.             |
| ModelProvider             | Budget, timeout, fallback, schema validation ו-no broker tools.       |
| MarketDataProvider        | Field ownership, freshness, entitlement ו-primary/fallback.           |

## **20.1 Source Usage Profile**

> **•** לכל מקור נשמרים allowed_use, authentication_type, rate_limit, retention_rule, data_license_status ו-owner.
>
> **•** מקור שאינו מאושר לשימוש או שמבנהו השתנה נכנס ל-PAUSED/CIRCUIT_OPEN.
>
> **•** אין Scraping או Fetch כללי מתוך Prompt של מודל; כל URL עובר Allowlist, egress policy ו-sandbox.
>
> **•** אות TradingView או Social הוא Evidence בלבד ומאומת מול Market Data פנימי לפני Decision.

## **20.2 Redundancy ו-Degraded Mode**

> **•** כשל מקור משלים מוריד איכות או מעביר ל-Manual/Shadow; אינו בהכרח משבית את כל המערכת.
>
> **•** כשל במקור קריטי שה-Strategy הגדירה REQUIRED חוסם רק אותה Strategy.
>
> **•** Market Data, Broker state ו-Kill State הם מקורות בטיחות קריטיים וחוסמים Entry בעת כשל.

# **22. מתאם Telegram**

| המערכת תומכת בשני מסלולי גישה רשמיים. TDLib דרך חשבון המשתמש הוא המסלול העיקרי והמחייב. Bot API נתמך בנוסף כמסלול משלים בלבד, ואינו רשאי להחליף את יכולת הגישה באמצעות חשבון המשתמש. אין שימוש ב-Scraping של Telegram Web. |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **22.1 מצבי גישה**

| **מצב**            | **שימוש**                                           | **סודות/Session**                                          | **הערה**                                                 |
|--------------------|-----------------------------------------------------|------------------------------------------------------------|----------------------------------------------------------|
| USER_ACCOUNT_TDLIB | חשבון המשתמש לקבוצות, ערוצים ו-Topics מורשים.       | api_id/api_hash סטטיים; Session/Auth Key דינמיים ומוצפנים. | REQUIRED_PRIMARY; Read-only; Re-authentication workflow. |
| BOT_API            | Bot שנוסף לקבוצה/ערוץ מורשה או משמש להתראות פרטיות. | Bot token סטטי ונפרד.                                      | SUPPLEMENTAL; אינו מחליף USER_ACCOUNT_TDLIB.             |

## **22.2 חוזה TelegramAdapter**

| **פעולה**            | **דרישה**                                                  |
|----------------------|------------------------------------------------------------|
| connect              | טעינת Profile, Secrets/Session ובדיקת הרשאות.              |
| list_sources         | החזרת Chats/Channels לצורך בחירת Allowlist בלבד.           |
| start_ingestion      | קליטת Updates רק ממקורות מורשים.                           |
| health               | מצב Connection, Auth, Backlog, Flood Wait ו-last_event_at. |
| reauthenticate       | Flow מאובטח ל-Code/QR כאשר נדרש.                           |
| download_attachment  | דרך Trust Layer, מגבלות גודל ו-Quarantine.                 |
| send_notification    | אופציונלי, רק לצ'אט פרטי מוגדר ול-Alerts מערכתיים.         |
| submit_trade_command | אסור; אינו חלק מהממשק.                                     |

## **22.3 אירועים קנוניים**

| **Event Type**                | **משמעות**                    |
|-------------------------------|-------------------------------|
| TELEGRAM_MESSAGE_CREATED      | הודעה חדשה בקבוצה/Supergroup. |
| TELEGRAM_CHANNEL_POST_CREATED | פוסט חדש בערוץ.               |
| TELEGRAM_MESSAGE_UPDATED      | עריכה.                        |
| TELEGRAM_MESSAGE_DELETED      | מחיקה/Tombstone.              |
| TELEGRAM_TOPIC_CREATED        | Topic חדש כאשר רלוונטי.       |
| TELEGRAM_ATTACHMENT_AVAILABLE | קובץ או מדיה.                 |
| TELEGRAM_SESSION_CONNECTED    | חיבור תקין.                   |
| TELEGRAM_SESSION_DISCONNECTED | ניתוק.                        |
| TELEGRAM_AUTH_REQUIRED        | נדרשת התחברות.                |
| TELEGRAM_PERMISSION_CHANGED   | הרשאה או חברות השתנו.         |
| TELEGRAM_RATE_LIMITED         | Flood Wait/Rate Limit.        |

## **22.4 כללי אמון ועיבוד**

> **•** Chat/Channel שאינו ב-Allowlist נזרק לפני יצירת Source Event.
>
> **•** Forward אינו נחשב מקור עצמאי; נשמר forward_origin ונכנס ל-Content Cluster.
>
> **•** Bots אחרים, פרסומות והודעות מערכת ניתנים לסינון בקונפיגורציה.
>
> **•** אין שימוש בהיסטוריה ישנה לפני available_at כאילו התקבלה בזמן אמת.
>
> **•** Telegram אינו REQUIRED אלא אם Strategy Manifest מסוים מציין זאת במפורש.

## **22.5 אבטחת Webhook ו-Session**

> **•** Webhook של Bot API חייב HTTPS, secret token header, Replay/Dedup protection ו-allowed_updates מצומצם.
>
> **•** Bot token אינו נכתב ל-Logs או ל-Frontend.
>
> **•** TDLib database/Auth Key נשמרים ב-Runtime Session Store מוצפן וב-Volume נפרד.
>
> **•** כל מצב AUTH_REQUIRED יוצר Alert; המערכת אינה מנסה לעקוף אימות.
>
> **•** ה-Adapter אינו נמצא ב-execution_net ואינו מחזיק Broker credentials.

## **22.6 Proof of Integration**

| **בדיקה**            | **Pass Condition**                                                    |
|----------------------|-----------------------------------------------------------------------|
| BOT_API message flow | קליטה, edit, delete, reply, channel post ו-dedup.                     |
| Permissions          | זיהוי נכון של מקור שאינו קריא או Privacy Mode שאינו מתאים.            |
| Webhook/Long Polling | Restart ללא אובדן או כפילות לא נשלטת.                                 |
| TDLib auth           | Login/QR, restart, encrypted session ו-reauth.                        |
| Allowlist            | אפס Events ממקור לא מורשה.                                            |
| Attachments          | הורדה מבוקרת, hash ו-quarantine.                                      |
| Failure isolation    | Telegram DOWN אינו מפיל Core Pipeline.                                |
| ARM64                | ה-Adapter הנבחר נבנה ורץ על ה-Deployment target או במארח חלופי מוגדר. |

## **22.7 תיעוד רשמי שנבדק**

> **•** Telegram Bot API and Bots FAQ.
>
> **•** Telegram Database Library (TDLib) documentation.
>
> **•** Telegram API application credentials and user authorization documentation.

# **23. הכרעת Baseline: User-first Social Adapters**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **Adapter**           | **מעמד**                      | **Runtime/Library**                              | **Pass Gate**   |
|-----------------------|-------------------------------|--------------------------------------------------|-----------------|
| whatsapp-user-adapter | REQUIRED                      | ייבחר ב-PoI; Session של המשתמש                   | POI-SOC-WA-USER |
| discord-user-adapter  | REQUIRED                      | ייבחר ב-PoI; Session של המשתמש                   | POI-SOC-DC-USER |
| telegram-user-adapter | REQUIRED_PRIMARY              | TDLib הרשמי; api_id/api_hash + encrypted session | POI-SOC-TG-USER |
| telegram-bot-adapter  | REQUIRED_SUPPLEMENTAL_FEATURE | Telegram Bot API; token נפרד                     | POI-SOC-TG-BOT  |

## **23.1 כללי קדימות וחפיפה**

> **•** חיבור Telegram User אינו fallback אלא הנתיב הראשי המחייב.
>
> **•** Telegram Bot נועד למקורות שבהם הוספת Bot מאושרת, ל-Alerts פרטיים ולכיסוי משלים.
>
> **•** אין מעבר שקט מ-User ל-Bot כאשר User נכשל; Health מציג Degraded/Auth Required והמערכת ממשיכה רק לפי Strategy dependencies.
>
> **•** שני המסלולים רשאים לפעול יחד, אך Dedup ו-Provenance חייבים למנוע ספירה כפולה.
>
> **•** WhatsApp ו-Discord נשארים User Adapters כפי שסוכם; כל Bot עתידי הוא תוספת שאינה מחליפה אותם.

## **23.2 תנאי כישלון מפורשים**

> **•** PoI נכשל אם Telegram User אינו מסוגל לשמור Session מוצפן ולעבור Restart ללא Login מחדש בכל הפעלה רגילה.
>
> **•** PoI נכשל אם Bot API בלבד מוצג כמימוש מלא של Telegram.
>
> **•** PoI נכשל אם WhatsApp או Discord הוסבו ל-Bot-only.
>
> **•** PoI נכשל אם ל-Adapter חברתי יש גישה ל-execution_net או ל-Broker credentials.
