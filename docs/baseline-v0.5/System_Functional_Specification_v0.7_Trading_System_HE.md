תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

# תקציר מנהלים

| **המערכת קולטת מידע ממקורות חברתיים, InvestingPro, חדשות, דיווחים ונתוני שוק; מזהה מניות ואירועי מסחר; מעשירה ומנתחת אותם; מפעילה אסטרטגיות גרסאיות; מקבלת החלטה מתועדת; מפעילה ניהול סיכונים; ומבצעת ב-Interactive Brokers Paper. המערכת תיבנה מראש כך שהחלפת מודל, מקור נתונים, חוקים או ברוקר לא תחייב כתיבה מחדש.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 1. מטרות המוצר

> **•** ליצור תמונת מודיעין מאוחדת ממקורות רבים ולאתר הזדמנויות וסיכונים בזמן.
>
> **•** להפריד בין גילוי רעיון מסחר לבין החלטה, ניהול סיכונים וביצוע.
>
> **•** למדוד ביצועי מקורות, אסטרטגיות, החלטות וביצועים בפועל.
>
> **•** לאפשר Replay, Shadow ו-Paper Trading לפני כל שימוש בכסף אמיתי.
>
> **•** ללמוד מהיסטוריה באמצעות הצעות לשינוי גרסאיות, לא באמצעות שינוי עצמי בלתי מבוקר.
>
> **•** לאפשר מעבר עתידי בין ברוקרים וספקי נתונים.

# 2. גבולות הגרסה

## 2.1 כלול

> **•** מניות ו-ETF בארה״ב כמסגרת ברירת מחדל; נכסים נוספים יוגדרו בהחלטה נפרדת.
>
> **•** קליטה בזמן אמת או קרוב לזמן אמת ממקורות שהוגדרו.
>
> • נתוני שוק, סריקות טכניות ופונדמנטליות, חדשות ודיווחים רשמיים, InvestingPro ו-TradingView/External Signals; בנוסף, User Adapters מחייבים ל-WhatsApp, Discord ו-Telegram שאינם Gate גלובלי בזמן ריצה.
>
> **•** IBKR Paper ובניית תשתית ל-Live.
>
> **•** מסכי Dashboard, אישור, מעקב, ניהול גרסאות ותחקור.

## 2.2 לא כלול כעת

> **•** High-Frequency Trading.
>
> **•** מסחר אוטומטי בלתי מוגבל ב-Live.
>
> **•** שינוי אסטרטגיה פעילה ללא גרסה ואישור.
>
> **•** ניהול כספים של לקוחות חיצוניים.

# 3. משתמשים ותפקידים

| **תפקיד**       | **הרשאות עיקריות**                                  |
|-----------------|-----------------------------------------------------|
| OWNER           | שליטה מלאה, הגדרות, קידום גרסאות, הפעלת Live בעתיד. |
| TRADER_APPROVER | אישור/דחיית הצעות במצב ידני, צפייה בתיק ובהחלטות.   |
| REVIEWER        | תחקור עסקאות, סימון שגיאות, יצירת תוויות אמת.       |
| SYSTEM_SERVICE  | שירותים פנימיים עם הרשאות מצומצמות לפי תפקיד.       |
| READ_ONLY       | צפייה בדוחות ללא שינוי או ביצוע.                    |

# 4. מצבי הפעלה

| **מצב**           | **התנהגות**                                      |
|-------------------|--------------------------------------------------|
| RESEARCH          | איסוף, ניתוח ודוחות; ללא החלטות מסחר.            |
| REPLAY            | הרצת נתוני עבר על גרסה נבחרת.                    |
| SHADOW            | החלטות בזמן אמת ללא פקודות.                      |
| PAPER_MANUAL      | הצעת עסקה ואישור אנושי לפני IBKR Paper.          |
| PAPER_AUTO        | ביצוע אוטומטי ב-Paper תחת Policy.                |
| LIVE_MANUAL       | עסקה אמיתית רק לאחר אישור אנושי.                 |
| LIVE_LIMITED_AUTO | אוטומציה מוגבלת לפי Allowlist ומגבלות.           |
| MAINTENANCE       | אין החלטות חדשות; אפשר סנכרון ותחקור.            |
| EMERGENCY_STOP    | עצירת פקודות חדשות וביטול/ניהול בהתאם ל-Runbook. |

# 5. מפת רכיבי המערכת

| **רכיב**                    | **אחריות**                                                            |
|-----------------------------|-----------------------------------------------------------------------|
| Source Adapters             | WhatsApp, Discord, Telegram, InvestingPro, News, Filings, Watchlists. |
| Ingestion & Normalization   | קליטה, מניעת כפילויות, חותמות זמן, Raw Store.                         |
| Entity Resolver             | זיהוי טיקר, נכס, כיוון, כוונה ואופק זמן.                              |
| Candidate Discovery         | יצירת מועמד מסחר מכל מקור או סורק.                                    |
| Enrichment                  | נתוני שוק, חדשות, פונדמנטל, מקור ואמינות.                             |
| Feature Engine              | המרת המידע ל-Features גרסאיים.                                        |
| Strategy Engine             | בדיקת מועמד מול אסטרטגיות.                                            |
| Decision Engine             | REJECT/WATCH/SHADOW/APPROVE/NEEDS_REVIEW.                             |
| Risk Engine                 | כמות, Stop, חשיפה, מגבלות ו-Veto.                                     |
| Approval Workflow           | אישור משתמש כאשר נדרש.                                                |
| Order & Broker Layer        | OrderIntent, IBKR Adapter, Broker Reconciliation.                     |
| Position Manager            | מעקב, יציאות, התאמות וחריגות.                                         |
| Post-Trade Review           | תחקור, תיוג שגיאות ומדדי ביצוע.                                       |
| Configuration & Experiments | גרסאות, Replay, Champion/Challenger, Rollback.                        |
| Dashboard & Alerts          | תצוגה, אישורים, בריאות מערכת והתראות.                                 |
| Technical Scanner & Engine  | סריקות מחיר/מחזור, Indicators, Triggers ו-Technical Assessments.      |
| Fundamental Engine          | Watchlists, Quality/Growth/Valuation ו-Fundamental Assessments.       |
| News/NLP & Event Engine     | חדשות, SEC/IR, Catalyst, Sentiment ו-News Veto.                       |
| Quant/ML Engine             | Regime, Ranking ו-Calibrated Probabilities.                           |
| Signal Fusion Engine        | שילוב Required/Optional/Veto assessments לפי Strategy Manifest.       |

# 6. מקורות מידע וקליטה

| **מקור**                                 | **מה נקלט**                                                                | **פלט**          |
|------------------------------------------|----------------------------------------------------------------------------|------------------|
| WhatsApp User Adapter                    | קליטת הודעות, מחבר, קבוצה, זמן, Reply Context וקבצים לפי יכולת החיבור.     | SourceEvent      |
| Discord User Adapter                     | ערוץ, שרת, מחבר, הודעה, Thread/Reply, עריכות וקבצים.                       | SourceEvent      |
| InvestingPro Scraper                     | נתוני חברה, דירוגים, Fair Value, Health, Screener ועמודים שהוגדרו.         | SourceEvent      |
| News Adapter                             | כותרות, גוף, מקור, זמן, טיקרים ואירוע.                                     | SourceEvent      |
| Filings Adapter                          | SEC/IR/דיווחים רשמיים וקבצים.                                              | SourceEvent      |
| InvestingPro Screener / Manual Watchlist | מניות שעומדות בתנאי סריקה גרסאיים.                                         | CandidateTrigger |
| Watchlist Monitor                        | שינויי מחיר/מחזור/אירוע במניות שהוגדרו.                                    | CandidateTrigger |
| Position Monitor                         | אירועים המשפיעים על פוזיציות פתוחות.                                       | ExitTrigger      |
| Technical Scanner                        | Bars/Quotes/Volume -\> Technical SignalAssessment                          |                  |
| Fundamental Scanner                      | Financial statements/ratios/estimates -\> Watchlist/Fundamental Assessment |                  |
| TradingView Adapter                      | Webhook alert -\> External Signal; revalidated internally                  |                  |
| Internet/Official Sources                | News, SEC, IR and public web evidence subject to licensing                 |                  |

## 6.1 דרישות קליטה משותפות

> **•** שמירת observed_at, ingested_at, source_id, external_id ו-content_hash.
>
> **•** מניעת כפילויות תוך שמירת Revision/Edition כאשר המקור השתנה.
>
> **•** Raw Payload נשמר או מופנה ל-Object Storage.
>
> **•** כל Adapter מפרסם Health, Lag, Error Rate ו-Last Success.
>
> **•** קליטה אינה שולחת עסקה ואינה משנה החלטה.

# 7. נרמול, זיהוי ישויות וכוונת מסחר

> **•** זיהוי טיקר וסוג נכס.
>
> **•** זיהוי שם חברה כאשר אין טיקר.
>
> **•** זיהוי אם ההודעה היא כניסה, יציאה, עדכון, דיון, שמועה או חדשות.
>
> **•** זיהוי כיוון LONG/SHORT/NEUTRAL.
>
> **•** חילוץ מחיר, טווח כניסה, יעד, Stop, אופק זמן וביטויי ודאות.
>
> **•** בדיקת Context של Reply/Thread כדי למנוע פירוש משפט מנותק.
>
> **•** העברה ל-NEEDS_REVIEW כאשר יש מספר נכסים אפשריים או כוונה לא ברורה.

# 8. גילוי מועמדי מסחר

| **סוג**           | **תנאי יצירה**                                    |
|-------------------|---------------------------------------------------|
| SOCIAL_TRIGGER    | טיקר/חברה הופיעו בהודעה בעלת משמעות מסחרית.       |
| NEWS_TRIGGER      | אירוע חדשותי או דיווח רשמי.                       |
| SCANNER_TRIGGER   | חריגה במחיר, מחזור, Gap, תנודתיות או Feature אחר. |
| WATCHLIST_TRIGGER | תנאי שנקבע ברשימת מעקב התממש.                     |
| SCHEDULED_EVENT   | דוח, החלטה רגולטורית או אירוע מתוזמן.             |
| POSITION_TRIGGER  | שינוי המשפיע על פוזיציה קיימת.                    |
| MANUAL_TRIGGER    | משתמש יצר מועמד ידנית.                            |

## **8.1 רשומת מועמד (Candidate Record)**

> {
>
> "candidate_id": "cand\_...",
>
> "instrument_id": "inst\_...",
>
> "trigger_type": "SOCIAL_TRIGGER",
>
> "trigger_event_ids": \[\],
>
> "observed_at": "...",
>
> "priority": "URGENT\|HIGH\|NORMAL\|LOW",
>
> "processing_profile": "FAST\|STANDARD\|DEEP",
>
> "expires_at": "...",
>
> "status": "NEW\|PROCESSING\|WATCH\|DECIDED\|EXPIRED\|INVALID"
>
> }

# 9. העשרת מידע

| **תחום**          | **נתונים**                                                       |
|-------------------|------------------------------------------------------------------|
| Market Context    | מחיר, Bid/Ask, Volume, Relative Volume, Bars, ATR, Gap, Session. |
| Broad Market      | מדדים, סקטור, תנודתיות שוק ומשטר שוק.                            |
| Fundamental       | הכנסות, רווחיות, חוב, מכפילים, נתוני InvestingPro לפי זמינות.    |
| Catalyst          | סוג אירוע, חומרה, מקור רשמי, זמן וחדשות קשורות.                  |
| Source Profile    | אמינות מחבר/ערוץ לפי אסטרטגיה, סקטור ואופק.                      |
| Portfolio Context | פוזיציות, קורלציה, חשיפת סקטור ו-Buying Power.                   |

# **10. מנוע תכונות (Feature Engine)**

Feature הוא ערך מחושב, גרסאי וניתן לשחזור. אסטרטגיות אינן קוראות ישירות דפי HTML, הודעות או APIs.

| **Feature**         | **משמעות**                       |
|---------------------|----------------------------------|
| source_reliability  | אמינות מקור בהקשר הרלוונטי.      |
| sentiment_direction | כיוון המסר לאחר ניתוח הקשר.      |
| catalyst_strength   | חוזק ומהימנות האירוע.            |
| relative_volume     | יחס מחזור מול בסיס מוגדר.        |
| price_extension     | מרחק מהמחיר בזמן האות/רמת ייחוס. |
| spread_ratio        | Spread ביחס למחיר/ATR.           |
| market_regime       | מצב שוק מסווג.                   |
| data_quality        | איכות ושלמות הקלט.               |
| signal_latency      | זמן מהאירוע ועד החלטה.           |

# 11. מסגרת אסטרטגיות

| **משפחה**          | **מטרה**                                  |
|--------------------|-------------------------------------------|
| SOCIAL             | רעיון ממקור חברתי המאומת מול השוק והמידע. |
| NEWS_EVENT         | תגובה לאירוע חדשותי עם אישור מחיר/מחזור.  |
| BREAKOUT           | פריצה ממבנה טכני שהוגדר.                  |
| SWING_CONTINUATION | המשך מגמה לאחר התבססות.                   |
| MEAN_REVERSION     | תגובה מוגזמת ביחס לאירוע/טווח.            |
| EARNINGS_EVENT     | מסחר סביב דוח או אירוע מתוזמן.            |
| FUNDAMENTAL_VALUE  | תזה ארוכה יותר על בסיס נתוני חברה.        |
| EXIT_MANAGEMENT    | ניהול יציאה מפוזיציה קיימת.               |

## **11.1 דרישות לכל מניפסט אסטרטגיה**

> **•** Strategy ID ו-Version.
>
> **•** מצב קידום.
>
> **•** נכסים וסשנים מותרים.
>
> **•** Processing Profile.
>
> **•** Features חובה ואופציונליים.
>
> **•** Eligibility Rules.
>
> **•** Entry Thesis ו-Invalidation Condition.
>
> **•** Decision, Critic, Risk ו-Exit Policy references.
>
> **•** זמן תפוגה ומדדי הצלחה.

# 12. מנוע החלטות

| **שלב**             | **פעולה**                                       |
|---------------------|-------------------------------------------------|
| Data Gate           | בדיקת Freshness, Entity, Completeness ו-Health. |
| Eligibility         | בחירת אסטרטגיות מתאימות.                        |
| Evaluation          | הפעלת Evaluators על Feature Snapshot.           |
| Conflict Resolution | טיפול באסטרטגיות סותרות לפי Policy.             |
| Critic              | בדיקת סתירות/מידע חסר לפי Activation Policy.    |
| Outcome             | REJECT/WATCH/SHADOW/APPROVE/NEEDS_REVIEW.       |
| Expiry              | קביעת תוקף ההחלטה.                              |

## 12.1 כללי סתירה בין אסטרטגיות

> **•** כל אסטרטגיה שומרת Decision עצמאי; אין ממוצע אוטומטי בין תזות שונות.
>
> **•** Portfolio Policy רשאי לבחור אחת, להקטין הקצאה או למנוע עסקאות מנוגדות.
>
> **•** מועמד יכול להיות WATCH באסטרטגיה אחת ו-REJECT באחרת.
>
> **•** ביצוע נושא strategy_id יחיד או Basket Policy מפורש.

# 13. ניהול סיכונים

> **•** חישוב סכום סיכון וכמות.
>
> **•** בדיקת Stop/Invalidation.
>
> **•** מגבלת הפסד יומית/שבועית/אסטרטגית.
>
> **•** מגבלת חשיפה למניה, סקטור, כיוון ואסטרטגיה.
>
> **•** בדיקת קורלציה ופוזיציות קיימות.
>
> **•** בדיקת נזילות, Spread וסוג פקודה.
>
> **•** בדיקת Buying Power וחשבונות.
>
> **•** Kill Switch ו-Hard Veto.

## 13.1 פלטים

> APPROVE -\> quantity and constraints
>
> REDUCE -\> reduced quantity and reasons
>
> VETO -\> veto codes and no OrderIntent

# 14. אישור אנושי

| **תרחיש**        | **פעולה**                             |
|------------------|---------------------------------------|
| PAPER_MANUAL     | אישור/דחייה/העברה למעקב.              |
| NEEDS_REVIEW     | בחירת טיקר/כוונה או דחייה.            |
| LIVE_MANUAL      | אישור מחודש קרוב לזמן הביצוע.         |
| POLICY_PROMOTION | אישור מעבר גרסה בין מצבים.            |
| EMERGENCY_ACTION | עצירה, ביטול פקודות או הפעלת Runbook. |

## 14.1 מסך אישור

> **•** מניה, כיוון, אסטרטגיה וגרסה.
>
> **•** מחיר נוכחי, גיל האות ו-Deadline.
>
> **•** תזה, סיבות בעד וסיבות נגד.
>
> **•** ראיות מקוריות וקישורים פנימיים.
>
> **•** כמות, סיכון, Stop, יעד וחשיפה לאחר העסקה.
>
> **•** Hard Gates שעברו ו-Warnings.
>
> **•** כפתורי אישור, דחייה, WATCH ובקשת ניתוח מחדש.

# 15. ביצוע וברוקרים

> **•** OrderIntent נוצר רק לאחר Decision ו-Risk תקפים.
>
> **•** Order Builder אינו תלוי בברוקר.
>
> **•** Broker Adapter מתרגם, מבצע ומחזיר Broker Order ID.
>
> **•** פקודות Idempotent כדי למנוע כפילות.
>
> **•** Preview/What-if כאשר הברוקר תומך.
>
> **•** Reconciliation מחזורי של חשבון, פקודות, Fills ופוזיציות.
>
> **•** אי התאמה גורמת BROKER_UNSYNCED ומונעת פקודות חדשות.

# 16. ניהול פוזיציה ויציאה

| **Trigger**        | **משמעות**                                  |
|--------------------|---------------------------------------------|
| PROTECTIVE_STOP    | הגנת הפסד.                                  |
| TAKE_PROFIT        | יעד מלא או חלקי.                            |
| TRAILING_POLICY    | הזזת הגנה לפי Policy.                       |
| THESIS_INVALIDATED | העובדה/מבנה שעליהם התבססה העסקה אינם תקפים. |
| TIME_EXIT          | חלף אופק הזמן.                              |
| RISK_REDUCTION     | חשיפה/Drawdown/קורלציה מחייבים הקטנה.       |
| NEW_MATERIAL_EVENT | מידע חדש מהותי.                             |
| MANUAL_EXIT        | משתמש הורה לסגור.                           |
| SYSTEM_SAFETY_EXIT | כשל תפעולי לפי Runbook.                     |

## 16.1 עקרונות

> **•** Decision Contract ליציאה נפרד מהחלטת כניסה אך משתמש באותה תשתית גרסאות ו-Audit.
>
> **•** שינוי סנטימנט לבדו אינו מכירה אוטומטית אלא Feature או Trigger להערכה.
>
> **•** פקודות הגנה אצל הברוקר מסונכרנות מול Position Manager.
>
> **•** Partial Fills ו-Partial Exits נתמכים.

# 17. תחקור ולמידה

> **•** שמירת Decision Snapshot מלא.
>
> **•** השוואת מחיר האות, מחיר החלטה, מחיר פקודה ו-Fill.
>
> **•** MFE, MAE, Slippage, Fees ו-Latency.
>
> **•** סיווג איכות החלטה בנפרד מתוצאת העסקה.
>
> **•** סיווג שגיאת מקור, נתונים, Entity, Strategy, Risk או Execution.
>
> **•** יצירת Change Proposal בלבד; אין שינוי אוטומטי של Active Policy.
>
> **•** Replay של Proposal מול נתוני עבר ו-Shadow בזמן אמת.

# 18. גרסאות וניסויים

| **מצב**           | **פעולה**             |
|-------------------|-----------------------|
| DRAFT             | יצירה ו-Validation.   |
| REPLAY            | בדיקה על Event Store. |
| SHADOW            | השוואה בזמן אמת.      |
| PAPER_MANUAL      | אישור אנושי.          |
| PAPER_AUTO        | דמו אוטומטי.          |
| LIVE_MANUAL       | לייב ידני.            |
| LIVE_LIMITED_AUTO | לייב מוגבל.           |
| RETIRED           | שמירה היסטורית.       |

# 19. מסכי המערכת

| **מסך**               | **תוכן**                                                 |
|-----------------------|----------------------------------------------------------|
| Overview Dashboard    | מצב חשבון, P&L, Drawdown, Candidates, Positions, Health. |
| Live Event Feed       | כל ההודעות, חדשות, טריגרים וסטטוס העיבוד.                |
| Candidate Workspace   | ראיות, Features, אסטרטגיות, Critic והחלטות.              |
| Approval Queue        | הצעות שממתינות לאישור.                                   |
| Orders & Executions   | פקודות, Fills, שגיאות ו-Reconciliation.                  |
| Positions             | פוזיציות, הגנות, Exit Triggers וסיכון.                   |
| Sources & Health      | Adapters, Lag, Scraper Drift, Auth ותקלות.               |
| Source Performance    | ביצועי כותבים/ערוצים לפי אסטרטגיה ואופק.                 |
| Strategies & Policies | גרסאות, Active Pointer וקידום.                           |
| Replay & Experiments  | Champion/Challenger ותוצאות.                             |
| Post-Trade Review     | תחקור ותיוג.                                             |
| Audit & Settings      | הרשאות, שינויים, Secrets References ו-Kill Switch.       |

# 20. התראות

| **רמה**  | **דוגמאות**                                                      |
|----------|------------------------------------------------------------------|
| CRITICAL | Live/Paper mismatch, Broker unsynced, כפילות פקודה, Kill Switch. |
| HIGH     | אישור ממתין לאות דחוף, Risk Limit, Position ללא הגנה.            |
| MEDIUM   | Scraper degraded, Market Data lag, Model failures.               |
| LOW      | דוח יומי, גרסה חדשה ל-Replay, Source performance update.         |

# 21. תרחישי זרימה מרכזיים

| **זרימה**                                     | **מסלול**                                                                                                     |
|-----------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| F-01 הודעה חברתית                             | קליטה -\> Context -\> Entity -\> Candidate -\> Market -\> Strategy -\> Decision -\> Risk -\> Approval/Shadow. |
| F-02 News Spike                               | FAST -\> Entity -\> Cached Market + Critical Catalyst -\> Decision -\> Risk -\> Deadline.                     |
| F-03 InvestingPro Screener / Manual Watchlist | Scan Trigger -\> Candidate -\> Enrichment -\> Strategy -\> Decision.                                          |
| F-04 InvestingPro                             | Scrape -\> Canary/Validation -\> Normalize -\> Features/Fundamental Snapshot.                                 |
| F-05 Paper Manual                             | APPROVE -\> Risk -\> Approval Queue -\> OrderIntent -\> IBKR Paper -\> Reconcile.                             |
| F-06 Paper Auto                               | APPROVE -\> Risk -\> Policy Check -\> OrderIntent -\> IBKR Paper.                                             |
| F-07 Exit                                     | Position Trigger -\> Exit Strategy -\> Risk/Order -\> Broker -\> Reconcile.                                   |
| F-08 Strategy Change                          | Change Proposal -\> New Version -\> Replay -\> Shadow -\> Promotion/Rollback.                                 |

# 22. טיפול בכשלים

| **כשל**               | **התנהגות**                                                                  |
|-----------------------|------------------------------------------------------------------------------|
| Source Down           | שאר המקורות ממשיכים; מקור מסומן DOWN; אסטרטגיות תלויות עוברות WATCH/INVALID. |
| Scraper Drift         | Circuit Breaker; אין פרסום ערכים חדשים; Alert.                               |
| Model Failure         | Retry מוגבל; Provider fallback רק אם הוגדר; אחרת INVALID.                    |
| Market Data Stale     | Hard Veto או WATCH לפי Strategy.                                             |
| Broker Disconnect     | אין פקודות חדשות; Reconciliation לאחר חזרה.                                  |
| Database Degraded     | Fail Closed לביצוע; קליטה לפי Buffer מוגדר בלבד.                             |
| Deadline Exceeded     | Outcome לפי Latency Policy.                                                  |
| Duplicate Event/Order | Deduplication ו-Idempotency.                                                 |
| Partial Fill          | עדכון כמות, הגנות ופוזיציה.                                                  |

# 23. דרישות לא פונקציונליות

| **תחום**        | **דרישה**                                           |
|-----------------|-----------------------------------------------------|
| Latency         | Profiles ו-Budgets גרסאיים; Metrics לכל Stage.      |
| Availability    | שירותים מבודדים; כשל מקור אינו מפיל החלטות אחרות.   |
| Auditability    | כל שינוי והחלטה ניתנים לשחזור.                      |
| Security        | Secrets מחוץ לקוד; Least Privilege; הצפנה והרשאות.  |
| Observability   | Logs מובנים, Metrics, Traces ו-Health.              |
| Scalability     | Queues ו-Workers; Backpressure; Partitioning עתידי. |
| Portability     | ARM64 ותמיכה ב-Docker על Oracle Server.             |
| Testability     | Mocks, Contract Tests, Replay ו-Sandbox.            |
| Maintainability | Adapters, Registries ו-Config over Code.            |

# 24. דרישות פריסה ראשוניות

> **•** שרת Oracle ARM כמארח הליבה.
>
> **•** Docker Compose לשירותים בשלב הראשון.
>
> **•** PostgreSQL למסד מובנה.
>
> **•** Object Storage או Volume נפרד ל-Raw Snapshots וקבצים.
>
> **•** Message Queue פנימי ייבחר במסמך הטכנולוגי.
>
> **•** Execution Agent נפרד אם IBKR Gateway דורש סביבת הפעלה שונה.
>
> **•** Dashboard יכול לרוץ על השרת או בשירות Frontend חיצוני; החלטה תתקבל בהמשך.

# 25. קריטריוני קבלה פונקציונליים

| **מזהה**  | **קריטריון**                                                     |
|-----------|------------------------------------------------------------------|
| SFS-AC-01 | הודעה חברתית עם טיקר ברור יוצרת Candidate ותמונת החלטה.          |
| SFS-AC-02 | הודעה עמומה מגיעה ל-NEEDS_REVIEW ולא מבוצעת אוטומטית.            |
| SFS-AC-03 | נפילת InvestingPro אינה מפילה את מנוע ההחלטות.                   |
| SFS-AC-04 | אפשר להריץ Candidate ב-FAST וב-STANDARD ולהשוות Latency/Outcome. |
| SFS-AC-05 | שינוי Policy משפיע ללא Build ומופיע ב-Audit.                     |
| SFS-AC-06 | Paper Order נשלח רק לאחר Decision + Risk + Approval לפי המצב.    |
| SFS-AC-07 | כפל של אותו OrderIntent אינו יוצר שתי פקודות.                    |
| SFS-AC-08 | Broker Reconciliation מזהה אי התאמה ועוצר פקודות.                |
| SFS-AC-09 | Position Manager יוצר Exit Decision לפי Trigger.                 |
| SFS-AC-10 | Replay משחזר החלטה היסטורית עם גרסה קודמת.                       |
| SFS-AC-11 | Champion ו-Challenger מקבלים אותו Candidate.                     |
| SFS-AC-12 | Health Dashboard מציג כל Adapter, Provider ו-Broker.             |

# 26. החלטות פתוחות

| **מזהה** | **החלטה**                                |
|----------|------------------------------------------|
| OPEN-01  | משפחות האסטרטגיה שייכללו ב-MVP.          |
| OPEN-02  | מניות בלבד או גם ETF/אופציות.            |
| OPEN-03  | Primary Market Data Provider ו-Fallback. |
| OPEN-04  | ספקי מודלים לכל Analyzer.                |
| OPEN-05  | Latency Budgets לפי Profile.             |
| OPEN-06  | Retention ו-Object Storage.              |
| OPEN-07  | תנאי קידום בין מצבים.                    |
| OPEN-08  | סוגי פקודות ו-Exit Policies ראשונים.     |
| OPEN-09  | ערוצי התראה ואישור.                      |
| OPEN-10  | טכנולוגיית Queue ו-Orchestration.        |

# **27. הנחיות ל-Claude Code**

| **מסמך זה מגדיר מה המערכת חייבת לעשות, אך אינו אישור להתחיל לכתוב את כל המערכת. Claude Code רשאי בשלב הבא להקים Repository Skeleton, Contracts בסיסיים ו-Mocks בלבד, לאחר אישור המשתמש. אין לבחור אסטרטגיה, Provider, Threshold, Queue או Retention על דעת עצמו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 28. השלב הבא

• System Functional Specification v0.7 נכלל ב-Baseline הסופי.

• הכנת Data Model and Retention Specification v0.6.

• Integration Specification v0.6 הושלם; הביצוע עובר ל-Proof-of-Integration Backlog.

**•** הכנת Security and Operations Specification.

**•** הכנת MVP Scope and Claude Code Implementation Plan.

# **29. הודעות מסחר מפורשות ורמות מחיר**

המערכת חייבת לתמוך בהודעות שבהן המקור מציין במפורש מחיר קנייה, טווח כניסה, מחיר מכירה, יעד אחד או יותר, Stop במחיר או באחוזים, ועדכונים לאחר הכניסה. פונקציונליות זו חלה על WhatsApp, Discord, InvestingPro, חדשות מובנות והזנה ידנית.

## **29.1 מידע שיש לחלץ מהודעה**

| **שדה**                | **תוכן**                                       |
|------------------------|------------------------------------------------|
| intent                 | ENTRY / EXIT / UPDATE / DISCUSSION.            |
| direction              | LONG / SHORT / NEUTRAL / UNKNOWN.              |
| entry_semantics        | קנייה עד מחיר, מעל מחיר, בטווח או עכשיו.       |
| entry_levels           | מחיר יחיד, מינימום/מקסימום וטווח סבילות.       |
| targets                | יעד אחד או מספר יעדים, כולל אחוז כמות לכל יעד. |
| stop                   | מחיר, אחוז, תנאי טכני או ביטול תזה.            |
| time_horizon           | תוך יומי, Swing, ארוך או זמן מפורש.            |
| validity               | מועד תפוגה או תנאי שפוקע.                      |
| revision_of            | קישור לקריאה קודמת כאשר זו הודעת עדכון.        |
| confidence/ambiguities | ביטחון הפירוש ופרטים שאינם ברורים.             |

## **29.2 זרימת עבודה לקריאה מפורשת**

| **שלב** | **שם**                  | **פעולה**                                                   |
|---------|-------------------------|-------------------------------------------------------------|
| 1       | Capture                 | שמירת ההודעה, המחבר, הערוץ, הזמן וה-Reply Context.          |
| 2       | Parse                   | חילוץ טיקר, כיוון, Entry, Targets, Stop ותוקף.              |
| 3       | Validate                | בדיקת לוגיקה, מחיר שוק נוכחי, Freshness ועמימות.            |
| 4       | Create Candidate        | יצירת Candidate עם Processing Profile מתאים.                |
| 5       | Build Source Plan       | שמירת התכנית כפי שהמקור כתב אותה.                           |
| 6       | Build System Trade Plan | Strategy ו-Risk יוצרים תכנית מאושרת.                        |
| 7       | Approval/Execution      | אישור לפי מצב המערכת ושליחה לברוקר.                         |
| 8       | Monitor                 | Position Manager מנטר Stop, Targets, Updates ו-Invaliation. |

# **30. Trade Plan וניהול פקודות**

## **30.1 רכיבי Trade Plan**

> **•** Entry Order: סוג פקודה, מחיר/Trigger, תוקף, Session ו-Max Chase.
>
> **•** Position Size: כמות מאושרת לפי סיכון ומרחק Stop.
>
> **•** Protective Stop: מחיר/אחוז/תנאי ביטול, Anchor וסוג פקודה.
>
> **•** Profit Targets: יעד אחד או יותר והכמות לכל יעד.
>
> **•** Trailing Policy: אופציונלי וגרסאי.
>
> **•** Time Exit: סגירה או הערכה מחדש לאחר פרק זמן.
>
> **•** Thesis Invalidation: אירוע או תנאי שמבטלים את הסיבה לעסקה.
>
> **•** Expiry: המועד שלאחריו אין להיכנס לפי התכנית.

## **30.2 עקרון אי-הרדיפה**

| **כאשר הודעה מתקבלת באיחור והמחיר כבר התרחק מאזור הכניסה, עבר את היעד או חרג מ-Max Chase, המערכת אינה קונה רק משום שהמקור המליץ. היא מעבירה את המניה ל-WATCH, פוסלת או ממתינה ל-Setup חדש.** |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **30.3 Bracket, OCO ופקודות חלקיות**

> **•** לאחר כניסה, המערכת תציב Stop ויעדים כפקודות מקושרות כאשר הברוקר תומך בכך.
>
> **•** ביצוע של יעד מבטל או מתאים את ה-Stop לכמות שנותרה.
>
> **•** ביצוע Stop מבטל יעדים פתוחים.
>
> **•** כאשר קיימים מספר יעדים, כל יעד מחזיק quantity_fraction וסך השברים חייב להיות תקין.
>
> **•** פקודות היציאה נוצרות לפי Fill בפועל, לא לפי הכמות שביקשנו לקנות.

# **31. זיהוי עסקה שלא עבדה והחלטת מכירה**

המערכת אינה ממתינה להפסד גדול כדי להחליט בדיעבד שהעסקה נכשלה. לכל עסקה מוגדרים מראש תנאי Failure/Invalidation. לאחר הכניסה, מנגנון יציאה נפרד בוחן אותם באופן רציף.

| **Trigger**            | **משמעות**                                        | **פעולה**                     |
|------------------------|---------------------------------------------------|-------------------------------|
| HARD_STOP_PRICE        | המחיר הגיע ל-Stop הקשיח.                          | מכירה/כיסוי ללא המתנה ל-LLM.  |
| MAX_LOSS_PERCENT       | הפסד מה-Fill הממוצע הגיע לאחוז שהוגדר.            | מכירה או הקטנה לפי Policy.    |
| THESIS_INVALIDATED     | האירוע, המבנה או הנתון שעליהם התבססה העסקה בוטלו. | Exit Evaluation מיידי.        |
| TIME_STOP              | העסקה לא התקדמה בזמן שנקבע.                       | סגירה או הקטנה.               |
| RISK_EVENT             | Drawdown, חשיפה או Kill Switch.                   | הקטנה/סגירה בעדיפות עליונה.   |
| SOURCE_UPDATE          | המקור פרסם יציאה או שינוי Stop.                   | הערכה מחדש; ביצוע לפי Policy. |
| MARKET_STRUCTURE_BREAK | שבירת רמה/מבנה שנקבעו באסטרטגיה.                  | Exit Evaluation.              |
| DATA_OR_BROKER_SAFETY  | כשל בטיחותי המחייב פעולה.                         | Runbook תפעולי.               |

## **31.1 סדר תגובה**

> **•** Stop ו-Kill Switch פועלים במסלול דטרמיניסטי ומהיר.
>
> **•** שינוי חדשות או סנטימנט מפעיל הערכת יציאה, אך אינו מוכר לבדו ללא Policy מתאים.
>
> **•** המערכת שומרת את סיבת היציאה בפועל: STOP, TARGET, INVALIDATION, TIME, RISK, MANUAL או ERROR.
>
> **•** לאחר סגירה מתבצע Post-Trade Review שבודק גם את איכות ה-Entry וגם את איכות ה-Exit.

# **32. שינויים במסכי המערכת**

| **מסך**             | **תוספת**                                                            |
|---------------------|----------------------------------------------------------------------|
| Candidate Workspace | הצגת הטקסט המקורי, Source Plan, פירוש המחירים ו-System Trade Plan.   |
| Approval Queue      | השוואה בין מחיר המקור למחיר הנוכחי, Max Chase, Stop, Targets וסיכון. |
| Position Screen     | Fill ממוצע, P&L, Stop פעיל, יעדים, כמות שנותרה ו-Exit Triggers.      |
| Instruction History | כל עדכון של המקור כ-Revision נפרד.                                   |
| Post-Trade Review   | האם המקור, הפירוש, הכניסה, ה-Stop והיציאה היו נכונים.                |

# **33. קריטריוני קבלה נוספים**

| **מזהה**  | **קריטריון**                                                      |
|-----------|-------------------------------------------------------------------|
| SFS-AC-13 | הודעה "קנה עד 25" יוצרת הוראת Limit ולא Trigger פריצה.            |
| SFS-AC-14 | הודעה "קנה מעל 25" יוצרת Entry Trigger מתאים ולא Limit הפוך.      |
| SFS-AC-15 | הודעה עם שני יעדים מייצרת Partial Exit Plan תקין.                 |
| SFS-AC-16 | ירידה באחוז מוגדר מפעילה Exit ללא קריאת LLM חדשה.                 |
| SFS-AC-17 | הודעת עדכון מקושרת לקריאה המקורית ושומרת את שתי הגרסאות.          |
| SFS-AC-18 | מחיר שכבר עבר יעד או Max Chase אינו מבוצע.                        |
| SFS-AC-19 | Partial Fill מתאים אוטומטית את כמות ה-Stop והיעדים.               |
| SFS-AC-20 | מסך הפוזיציה מציג בכל רגע את סיבת היציאה הפעילה ואת הכמות המוגנת. |

# 40. Baseline פונקציונלי קנוני

| **הפרק משלים את האפיון הפונקציונלי ומגדיר State Machines, Final Pre-Submit Check, Calendar, Corporate Actions ו-Scope מתוקן.** |
|--------------------------------------------------------------------------------------------------------------------------------|

## 40.1 תיקון Scope

> **•** Market Scanner עצמאי נדחה מה-MVP. נשארים InvestingPro Screener ו-Manual Watchlist.
>
> **•** Official Filings/Investor Relations הם מקור חובה; ספק News מסחרי מופעל רק לאחר בחירה ו-PoI.
>
> **•** news.catalyst_momentum נשאר Shadow/Manual עד סגירת ספק, Timestamp semantics ו-Golden Set.
>
> **•** מניות שלמות בלבד ב-MVP.

## 40.2 Candidate State Machine

| **מצב**      | **מעבר**                                                  |
|--------------|-----------------------------------------------------------|
| NEW          | PROCESSING \| INVALID                                     |
| PROCESSING   | NEEDS_REVIEW \| WATCHING \| DECIDED \| EXPIRED \| INVALID |
| NEEDS_REVIEW | PROCESSING \| REJECTED \| EXPIRED                         |
| WATCHING     | PROCESSING \| EXPIRED \| REJECTED                         |
| DECIDED      | CLOSED                                                    |
| EXPIRED      | סופי                                                      |
| INVALID      | סופי                                                      |

## 40.3 Order ו-Position State Machines

> • Order משתמש ב-CanonicalOrderStatus שנקבע ב-Decision Contract v0.8.

| **PositionStatus**      | **מעבר**                                             |
|-------------------------|------------------------------------------------------|
| OPENING                 | OPEN \| RECONCILIATION_REQUIRED \| CLOSED            |
| OPEN                    | REDUCING \| CLOSING \| RECONCILIATION_REQUIRED       |
| REDUCING                | OPEN \| CLOSING \| CLOSED \| RECONCILIATION_REQUIRED |
| CLOSING                 | CLOSED \| RECONCILIATION_REQUIRED                    |
| RECONCILIATION_REQUIRED | OPEN \| REDUCING \| CLOSING \| CLOSED                |

## 40.4 Final Pre-Submit Workflow

> **•** Refresh Market Snapshot, Account Snapshot ו-Trading Calendar.
>
> **•** בדיקת Decision expiry, Approval expiry ו-Strategy promotion state.
>
> **•** בדיקת Kill State, Broker Health ו-Reconciliation state.
>
> **•** הרצת Risk Engine סופית ויצירת SubmitAuthorization חד-פעמי.
>
> **•** רק Execution Agent רשאי לצרוך SubmitAuthorization.

## 40.5 Trading Calendar

> **•** Calendar Provider סמכותי מגדיר Open, Close, Early Close, Holiday ו-Halt.
>
> **•** Policies בשעון America/New_York ומאוחסנות ב-UTC.
>
> **•** אין חישוב שעות באמצעות Offset קבוע בגלל DST.
>
> **•** יום מקוצר מעדכן אוטומטית Entry Cutoff ו-Time Exits.

## 40.6 Corporate Actions

> **•** Split, Reverse Split, Symbol Change, Delisting ו-Merger יוצרים CorporateActionEvent.
>
> **•** Position מושפע עובר SYMBOL_HALT ו-RECONCILIATION_REQUIRED.
>
> **•** Stops, targets, quantity ו-average price אינם משתנים בלי Broker Reconciliation ו-Adjustment Record.
>
> **•** כניסות חדשות בנכס מושפעות חסומות עד Resume מפורש.

## 40.7 Position Exit Coordinator

> **•** כל Exit Trigger נכנס לתור פוזיציה יחיד.
>
> **•** המסך מציג Trigger מנצח, Triggers שנדחו ומצב פקודת היציאה.
>
> **•** Manual Exit אינו עוקף Lock או Risk; הוא מקבל קדימות מוגדרת.
>
> **•** Source Update הוא Trigger ל-Review, לא פקודת מכירה ישירה, אלא אם Policy מפורשת קובעת אחרת.

## 40.8 Functional PoI Gates

| **PoI**      | **תוצאה נדרשת**                                                       |
|--------------|-----------------------------------------------------------------------|
| IBKR         | Order lifecycle, partial fill, cancel, bracket, reconnect, reconcile. |
| WhatsApp     | stable external IDs, reply linking, reconnect and gap behavior.       |
| Discord      | threads, edits, attachments and resume.                               |
| InvestingPro | login, canary, field catalog, drift.                                  |
| Market Data  | quotes, bars, calendar and field ownership.                           |
| News/Filings | primary-source timestamps and clustering.                             |

# 41. ארכיטקטורה פונקציונלית רב-מודלית

| **Candidate Discovery הוא שירות עצמאי שאינו תלוי במקורות חברתיים. המערכת סורקת את השוק והאינטרנט, מפיקה Assessments נפרדים ומחברת אותם באמצעות Strategy/Fusion Manifest גרסאי.** |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 41.1 ערוצי גילוי מועמדים

| **ערוץ**                 | **תדירות/טריגר**                 | **פלט**                                                       |
|--------------------------|----------------------------------|---------------------------------------------------------------|
| Technical Scanner        | Intraday לפי Universe ו-Schedule | Breakout, volume spike, pullback, trend ו-anomaly candidates. |
| Fundamental Scanner      | יומי/שבועי/חודשי                 | Watchlists לפי quality, growth, valuation ו-risk.             |
| News and Filings Scanner | Event-driven                     | Catalyst candidates ו-risk events.                            |
| External Signals         | Webhook/API                      | TradingView או ספק אחר; דורש revalidation.                    |
| Social Signals           | Event-driven                     | Idea, explicit plan, update או source context.                |
| Manual Research          | לפי משתמש                        | Candidate עם evidence ידני.                                   |

## 41.2 תהליך רב-מודלי

> Discovery -\> Instrument Resolution -\> Point-in-Time Enrichment
>
> -\> Technical/Fundamental/News/Quant/External/Social Assessments
>
> -\> Signal Fusion -\> Decision -\> Risk -\> Final Pre-Submit -\> Broker

## 41.3 שילובים נתמכים

| **Strategy Pattern**       | **מה קובע מה**           | **מה קובע מתי**               | **Gate/Veto**                  |
|----------------------------|--------------------------|-------------------------------|--------------------------------|
| TECHNO_FUNDAMENTAL         | Fundamental eligibility  | Technical trigger             | News, risk, data quality       |
| TECHNICAL_SENTIMENT_FILTER | Technical candidate      | Technical timing              | Negative material catalyst     |
| FUNDAMENTAL_CATALYST       | Quality/value + catalyst | Technical confirmation        | Overextension/event risk       |
| SOCIAL_CONFIRMED           | Social idea              | Independent technical trigger | News/fundamental/source checks |
| TECHNICAL_REGIME_FILTER    | Technical setup          | Technical trigger             | ML regime compatibility        |
| MULTI_SIGNAL_ENSEMBLE      | Manifest requirements    | Trigger specified by strategy | Hard vetoes; no blind average  |

## 41.4 TradingView

> **•** TradingView הוא ExternalSignalProvider אופציונלי ואינו מקור אמת יחיד.
>
> **•** כל Alert נשמר גולמית, עובר Idempotency, Resolving ו-TTL, ולאחר מכן מחושב מחדש מול נתוני השוק של המערכת.
>
> **•** אין Webhook שמדבר ישירות עם הברוקר ואין Pine Script שמקבל Broker Credentials.

## 41.5 ML Governance

> **•** Training נעשה Offline בלבד על Dataset גרסאי ו-Point-in-Time.
>
> **•** Model Artifact אינו פעיל לפני Replay, Out-of-Sample, Calibration, Shadow ו-Promotion אנושי.
>
> **•** Online self-modification, auto-retuning ו-auto-promotion אסורים.

# 42. דרישות ממשק תפעול מלא

| **הממשק אינו רק Dashboard מידע. הוא Console תפעולי שמאפשר למפעיל לצפות, להבין, לאשר, לעצור, לחדש ולתחקר את כל מחזור העסקה והמערכת.** |
|--------------------------------------------------------------------------------------------------------------------------------------|

## 42.1 מסכים מחייבים

| **מסך**                  | **יכולות מרכזיות**                                                                 |
|--------------------------|------------------------------------------------------------------------------------|
| Overview                 | מצב Auto, חשבון, P&L, סיכון, פוזיציות, Candidates, Alerts ו-Emergency State.       |
| Scanner & Watchlists     | סריקות טכניות/פונדמנטליות/אירועים, Filters, Runs ותוצאות.                          |
| Candidate Research       | שש שכבות ניתוח, evidence, model versions, fusion, vetoes ו-trade plan.             |
| Approval Queue           | אישור ידני, דחייה, refresh, reduce quantity ו-expiry.                              |
| Positions                | Stops, targets, fills, P&L, thesis, source updates ו-exit control.                 |
| Orders & Reconciliation  | Intent, broker order, state, errors, unknown state ו-reconcile.                    |
| Models & Strategies      | Active/challenger, calibration, version, promotion ו-rollback.                     |
| Sources & Health         | Market data, news, TradingView, InvestingPro, social, AI ו-broker health.          |
| Emergency Control Center | Global halt, strategy/source/model/symbol halt, queued intents ו-separate flatten. |
| Audit & Review           | כל שינוי, החלטה, emergency action ותחקור.                                          |

## 42.2 Emergency UX

> **•** כפתור אדום וקבוע לעצירת כניסות חדשות זמין מכל מסך ואינו תלוי בזמינות AI.
>
> **•** הפעלת HALT מתבצעת מיד; Resume דורש MFA, Health Check ו-Reconciliation.
>
> **•** FLATTEN ACCOUNT מוצג בנפרד, דורש Re-authentication ואישור מפורש של היקף הסגירה.
>
> **•** Banner גלובלי מציג PAPER/LIVE, מצב Auto, Kill State ו-Broker Sync בכל עת.

# **30. רכיבים משלימים שננעלו ב-Baseline**

| **רכיב**                                  | **אחריות**                                                                          |
|-------------------------------------------|-------------------------------------------------------------------------------------|
| Opportunity and Policy Calibration Engine | מודד Funnel, קיפאון, Overtrading, Gate concentration ו-Counterfactual Shadow.       |
| Research and Backtesting Lab              | Replay, Backtest, Simulation, Experiments, Dataset snapshots ו-Champion/Challenger. |
| Model Registry and Governance             | גרסאות מודל, Calibration, Drift, Approval ו-Rollback.                               |
| Portfolio and Capital Allocation Engine   | תקציבי סיכון, חשיפות, כפילויות, קורלציה והקצאת הון בין אסטרטגיות.                   |
| Market Regime Engine                      | מסווג תנאי שוק ומפעיל/מגביל אסטרטגיות בהתאם.                                        |
| Trust and Adversarial Input Layer         | Provenance, Quarantine, Prompt Injection, Manipulation ו-Webhook authentication.    |
| Notification Service                      | התראות תפעול, עסקאות, סיכון, Drift וקיפאון דרך Adapters.                            |
| Emergency Control Service                 | Entry Halt ו-Account Flatten בערוץ נפרד מה-UI המלא.                                 |

## **30.1 זרימת גילוי עצמאית**

> Scheduled / Continuous Scans
>
> -\> Technical / Fundamental / Event / External discovery
>
> -\> Candidate
>
> -\> Independent assessments
>
> -\> Signal Fusion
>
> -\> Strategy + Portfolio + Risk
>
> -\> Action Tier
>
> -\> Paper execution or Shadow tracking

## **30.2 מעקב אחר דחיות**

> **•** כל Candidate שעבר כשירות נתונים אך לא בוצע יכול להמשיך במסלול Counterfactual Shadow.
>
> **•** המערכת מציגה באיזה Gate נעצר כל Candidate ומה היה קורה לפי Target/Stop מוגדרים.
>
> **•** אין שימוש בתוצאות עתידיות לשינוי בזמן אמת; הן זמינות רק למחקר ו-Change Proposal.

## **30.3 דרישות ממשק נוספות**

> **•** Opportunity Funnel, Policy Starvation, Overtrading, Portfolio Exposure, Market Regime, Model Drift ו-Quarantine יוצגו בממשק.
>
> **•** המשתמש יוכל להריץ What-if לשינוי סף בלי להפעיל אותו.
>
> **•** כל פעולה מהותית תציג השפעה צפויה, גרסה פעילה ונתוני בסיס.

# **50. דרישות פונקציונליות לחיבור Telegram**

| **יכולת**   | **דרישה**                                                                              |
|-------------|----------------------------------------------------------------------------------------|
| מצבי גישה   | USER_ACCOUNT_TDLIB חובה ועיקרי; BOT_API נתמך בנוסף כמסלול משלים ואופציונלי.            |
| Scope       | קבוצות, ערוצים, Supergroups ו-Topics הנמצאים ב-Allowlist.                              |
| תוכן        | הודעות, פוסטים בערוץ, עריכות, מחיקות, Replies, Topics, Attachments ו-Forward metadata. |
| מצב פעולה   | קריאה בלבד ב-MVP; אין תגובות, פרסום או שינוי תוכן.                                     |
| נרמול       | כל Update הופך ל-Canonical Source Event עם IDs, timestamps, revision ו-content hash.   |
| שימוש במסחר | Candidate/Source Plan/Update בלבד; אין Direct-to-Broker.                               |
| התראות      | Notification Service רשאי לשלוח Alerts לבעל המערכת בצ'אט פרטי ייעודי.                  |
| שליטה מרחוק | פקודות קנייה, מכירה, Halt או Flatten דרך Telegram אסורות ב-MVP.                        |

## **50.1 זרימת קליטה**

> **•** Telegram Adapter מקבל Update ומוודא שה-Chat או Channel נמצא ב-Allowlist.
>
> **•** Deduplication מתבצע לפי access_mode, account/bot identity, chat_id, message_id ו-revision.
>
> **•** Forwarded content נשמר עם מקור ההעברה ואינו נחשב אישור עצמאי.
>
> **•** Attachment עובר Trust, malware/content validation ו-Quarantine לפי הצורך.
>
> **•** Event תקין נכנס לאותו Pipeline של Entity Resolution, Candidate Discovery ו-Signal Fusion.

## **50.2 קריטריוני קבלה**

| **מזהה** | **קריטריון**                                                        |
|----------|---------------------------------------------------------------------|
| TG-FR-01 | המערכת קולטת הודעה חדשה, עריכה ומחיקה ממקור Allowlisted.            |
| TG-FR-02 | Reply, Topic ו-Forward מקושרים לאירוע המקורי כאשר המידע זמין.       |
| TG-FR-03 | ניתוק או Auth Required מוצגים בממשק ואינם מפילים מקורות אחרים.      |
| TG-FR-04 | Telegram מושבת לחלוטין ועדיין Core Pipeline משלים עסקה ממקור עצמאי. |
| TG-FR-05 | לא קיימת דרך לשלוח פקודת מסחר מה-Telegram Adapter.                  |

# **51. פונקציונליות חובה: Social User Access**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **Requirement ID** | **דרישה**                                                | **קריטריון קבלה**                                                                         |
|--------------------|----------------------------------------------------------|-------------------------------------------------------------------------------------------|
| SOC-USER-01        | WhatsApp User Adapter נשאר חלק חובה מה-MVP.              | חיבור, allowlist, message/reply, reconnect ו-auth expiry עוברים PoI.                      |
| SOC-USER-02        | Discord User Adapter נשאר חלק חובה מה-MVP.               | חיבור, channels/threads, edits, attachments, resume ו-auth expiry עוברים PoI.             |
| SOC-USER-03        | Telegram User Adapter באמצעות TDLib הוא חלק חובה מה-MVP. | Login/QR, chats/channels/topics, edits/deletes, reconnect ו-session encrypted עוברים PoI. |
| SOC-BOT-04         | Telegram Bot Adapter נבנה בנוסף.                         | Bot API קולט מקורות שהבוט מורשה לראות ושולח Alerts פרטיים לפי Policy.                     |
| SOC-COEX-05        | User ו-Bot יכולים לפעול יחד.                             | אין Candidate כפול מאותו message/content.                                                 |
| SOC-ISO-06         | כשל Social אינו מפיל את הליבה.                           | Scanners ו-Strategies שאינם תלויים במקור ממשיכים לפעול.                                   |
