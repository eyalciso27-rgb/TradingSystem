תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

| **מטרת ה-MVP אינה להוכיח שהמערכת מרוויחה כסף, אלא להוכיח שכל שרשרת העבודה פועלת בצורה נכונה, ניתנת לשחזור ובטוחה: קליטת מידע, הבנת הוראות מסחר, יצירת מועמד, החלטה, ניהול סיכון, אישור, ביצוע ב-IBKR Paper, ניהול פוזיציה, יציאה ותחקור.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 0. מסמכי בסיס והחלטות נעולות

| **מסמך**                                    | **תפקיד**                                   |
|---------------------------------------------|---------------------------------------------|
| Decision Contract v0.8                      | סמכויות, תוצאות, כניסות, Stops ו-Hard Veto. |
| System Functional Specification v0.7        | רכיבי המערכת, מסכים ותהליכים.               |
| Data Model and Retention Specification v0.6 | מודל נתונים ואחסון.                         |
| Integration Specification v0.6              | מקורות מידע, Market Data, AI ו-IBKR.        |
| Security and Operations Specification v0.6  | אבטחה, תפעול, Kill Switch, גיבוי ושחזור.    |
| Strategy and Decision Framework v0.5        | משפחות אסטרטגיה, כניסה, יציאה ולמידה.       |

> **•** לא מתחילים ב-Live; ה-MVP עובד רק ב-Research, Replay, Shadow ו-IBKR Paper.
>
> **•** המוצר מיועד בתחילה למשתמש יחיד, עם הרשאות פנימיות בסיסיות ולא כ-SaaS ציבורי.
>
> **•** המערכת תהיה Broker-Agnostic, אך רק IBKR Adapter ימומש בפועל ב-MVP.
>
> • Market Data, סריקות טכניות ופונדמנטליות, חדשות ודיווחים רשמיים הם מקורות הליבה. InvestingPro ו-TradingView הם מקורות משלימים; הזמינות בזמן ריצה של WhatsApp, Discord ו-Telegram אינה Gate גלובלי; עם זאת, מימוש User Adapter לכל אחד מהם הוא דרישת חובה.
>
> **•** אין Model Gateway כשירות נפרד ב-MVP; יש Clients פנימיים להחלפת ספק.
>
> **•** כל שינוי התנהגותי נשמר ב-Config/Version ולא מקודד בתוך הליבה.
>
> **•** Claude Code לא מקבל את המסמכים עד להשלמת כל האפיון והסקירה המאוחדת.

# 1. מטרות ה-MVP

| **מזהה** | **מטרה**                                                                |
|----------|-------------------------------------------------------------------------|
| MVP-G1   | לקלוט מידע אמיתי ממקורות שנבחרו ולשמור אותו ללא אובדן או כפילות.        |
| MVP-G2   | להבין הודעות מסחר מפורשות: טיקר, כיוון, מחיר כניסה, יעד, Stop ועדכונים. |
| MVP-G3   | להעשיר מועמד באמצעות נתוני שוק, InvestingPro, חדשות ומקור.              |
| MVP-G4   | להפעיל מסגרת אסטרטגיה גרסאית ולהפיק החלטה מנומקת.                       |
| MVP-G5   | להפעיל Risk Engine קשיח ולהפיק גודל פוזיציה ותכנית יציאה.               |
| MVP-G6   | לאפשר אישור אנושי ולבצע עסקה ב-IBKR Paper באופן Idempotent.             |
| MVP-G7   | לנהל Stop, יעד, Partial Fill, Position ויציאה.                          |
| MVP-G8   | לשחזר כל החלטה ולבצע Post-Trade Review.                                 |
| MVP-G9   | להריץ Champion/Challenger ו-Replay בלי לשנות גרסה פעילה.                |
| MVP-G10  | להוכיח תפעול יציב, ניטור, גיבוי ותגובה לתקלות.                          |

# 2. מדד ההצלחה של ה-MVP

| **הצלחה של ה-MVP אינה נמדדת רק ב-P&L. מערכת יכולה להרוויח בדמו בגלל מזל ועדיין להיות מסוכנת. הצלחה מחייבת נכונות נתונים, דיוק תפעולי, יכולת שחזור, מניעת כפילויות, הגנות ברוקר והתנהגות יציבה תחת תקלות.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **תחום**              | **מה נדרש**                                               |
|-----------------------|-----------------------------------------------------------|
| Functional            | הזרימה מקצה לקצה עובדת לפי האפיון.                        |
| Data Quality          | הודעות, טיקרים, מחירים ועדכונים נשמרים ומקושרים נכון.     |
| Decision Quality      | כל החלטה כוללת ראיות, גרסאות, תזה ותנאי ביטול.            |
| Execution Safety      | אין פקודות כפולות, סביבה שגויה או פוזיציה ללא הגנה צפויה. |
| Operational Stability | Health, Alerts, Backups ו-Recovery פועלים.                |
| Research Value        | נאסף מאגר המאפשר למדוד מקור, אסטרטגיה וטעות.              |
| Trading Performance   | נמדד ומתועד, אך אינו תנאי יחיד לסיום MVP.                 |

# 3. Universe ונכסים

| **תחום**               | **היקף MVP**                                                    |
|------------------------|-----------------------------------------------------------------|
| Markets                | בורסות ארה״ב הנתמכות על ידי IBKR ו-Market Data Provider.        |
| Asset Classes          | מניות ו-ETF בלבד.                                               |
| Direction              | Long בלבד; Sell נועד לסגירת פוזיציה קיימת.                      |
| Currency               | USD כברירת מחדל.                                                |
| Sessions               | Regular Trading Hours לביצוע אוטומטי; Pre/After נאספים ומוצגים. |
| Instrument Eligibility | מחיר, נזילות, Spread, Market Cap ו-Halt לפי Risk Policy גרסאית. |
| Corporate Actions      | זיהוי Split, Symbol Change ו-Delisting.                         |

## 3.1 מחוץ ל-Universe של MVP

> **•** אופציות.
>
> **•** פתיחת פוזיציות Short.
>
> **•** חוזים עתידיים, מט״ח, אג״ח וקריפטו.
>
> **•** OTC ומכשירים שאינם נתמכים במדיניות הסיכון.
>
> **•** מסחר אוטומטי מחוץ לשעות הרגילות.

# 4. מצבי הפעלה הכלולים ב-MVP

| **מצב**           | **התנהגות**                                  | **MVP**          |
|-------------------|----------------------------------------------|------------------|
| RESEARCH          | איסוף, ניתוח ודוחות ללא Trade Proposal פעיל. | כן               |
| REPLAY            | הרצת אירועים שמורים מול גרסה נבחרת.          | כן               |
| SHADOW            | החלטה מלאה ללא שליחת פקודה.                  | כן               |
| PAPER_MANUAL      | IBKR Paper לאחר אישור אנושי.                 | כן               |
| PAPER_AUTO        | אוטומציה מוגבלת לאסטרטגיה מאושרת.            | שלב מתקדם של MVP |
| LIVE_MANUAL       | כסף אמיתי באישור אדם.                        | לא               |
| LIVE_LIMITED_AUTO | אוטומציה בכסף אמיתי.                         | לא               |

# 5. מקורות מידע הכלולים

| **מקור**                     | **היקף**                                                                 | **סטטוס**                                              |
|------------------------------|--------------------------------------------------------------------------|--------------------------------------------------------|
| WhatsApp User Adapter        | קבוצות Allowlist, הודעות, Replies וקבצים.                                | יכולת חובה; PoI מחייב; אינו Runtime gate גלובלי        |
| Discord User Adapter         | שרתים/ערוצים Allowlist, Threads, Replies ו-Attachments.                  | יכולת חובה; PoI מחייב; אינו Runtime gate גלובלי        |
| InvestingPro Scraper         | עמודי חברה, נתונים נבחרים, Screener/Watchlist לפי יכולת.                 | חובה                                                   |
| Market Data                  | Quotes, Bars, Volume, Spread, Session ו-Calendar.                        | חובה — Core                                            |
| News Adapter                 | לפחות ספק חדשות אחד או מקור מוסכם.                                       | מותנה ב-PoI ובחירת ספק; Feature flag                   |
| Filings/Official Sources     | SEC/Investor Relations לפי Trigger.                                      | חובה                                                   |
| Manual Candidate             | יצירת מועמד ידנית דרך Dashboard.                                         | חובה                                                   |
| Email/RSS/Other Social       | מקורות נוספים.                                                           | נדחה                                                   |
| Technical Scanner            | Intraday scanners and internally calculated technical features.          | חובה — Core                                            |
| Fundamental Scanner          | Daily/weekly watchlist generation from point-in-time fundamentals.       | חובה — Core                                            |
| TradingView Adapter          | External alerts; internally revalidated.                                 | MVP integration; לא חובה להחלטה                        |
| Quant/ML Regime              | Shadow ranking/regime model.                                             | Shadow בלבד עד calibration                             |
| Telegram User + Bot Adapters | TDLib בחשבון המשתמש חובה ועיקרי; Bot API משלים; קבוצות/ערוצים Allowlist. | User PoI חובה; Bot PoI משלים; אינו Runtime gate גלובלי |

## **5.1 היקף קליטת WhatsApp, Discord ו-Telegram**

> **•** קליטה פסיבית בלבד; אין שליחת הודעות אוטומטית.
>
> **•** רק קבוצות, שרתים וערוצים שאושרו ב-Allowlist.
>
> **•** הודעה גולמית, מחבר, זמן, Reply/Thread וקבצים לפי יכולת.
>
> **•** עריכות, מחיקות ועדכוני עסקה כאשר החיבור מספק אותם.
>
> **•** קישור הודעות המשך כגון 'להעלות Stop', 'למכור חצי' או 'יצאתי'.
>
> **•** אין ניסיון לנתח את כל החשבון ללא הבחנה.

## 5.2 היקף InvestingPro

| **תחום**              | **MVP**                              |
|-----------------------|--------------------------------------|
| Instrument Identity   | טיקר, שם, בורסה וסקטור.              |
| Fair Value            | ערך וכיוון כאשר זמינים.              |
| Financial Health      | ציון/קטגוריה כאשר זמינים.            |
| Selected Fundamentals | קבוצת שדות שתוגדר במפרט השדות.       |
| Screener Results      | לפחות מסך/סינון אחד הניתן לתזמון.    |
| Watchlist/Alerts      | לפי יכולת הטכנית שתאושר.             |
| Raw Evidence          | Snapshot/Reference ו-Parser Version. |

# 6. משימות AI הכלולות

| **Task**                     | **תפקיד**                                 | **MVP**     |
|------------------------------|-------------------------------------------|-------------|
| ENTITY_EXTRACTION            | זיהוי טיקר/חברה/נכס ועמימות.              | חובה        |
| TRADE_INSTRUCTION_EXTRACTION | Entry, target, stop, validity ו-revision. | חובה        |
| INTENT_CLASSIFICATION        | כניסה, יציאה, עדכון, דיון או חדשות.       | חובה        |
| DIRECTION_SENTIMENT          | LONG/NEUTRAL/NEGATIVE בהקשר.              | חובה        |
| CATALYST_CLASSIFICATION      | סוג אירוע וחומרה.                         | חובה        |
| THREAD_CONTEXT_SUMMARY       | סיכום הקשר רק כאשר נדרש.                  | חובה מוגבלת |
| CRITIC_REVIEW                | בדיקת סתירות לפי Activation Policy.       | חובה מוגבלת |
| POST_TRADE_CLASSIFICATION    | סיווג שגיאה ואיכות החלטה.                 | חובה        |
| AUTONOMOUS_MODEL_TRAINING    | אימון או Fine-tuning אוטומטי.             | נדחה        |

# 7. אסטרטגיות הכלולות ב-MVP

| **ה-MVP אינו מנסה להפעיל את כל משפחות האסטרטגיה. הוא מתמקד במקרי השימוש המרכזיים: המלצות מפורשות מהערוצים ואירועים חדשותיים. רכיבים טכניים ופונדמנטליים משמשים כאימות, לא בהכרח כאסטרטגיות עצמאיות.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **אסטרטגיה**                | **תיאור**                                                      | **סטטוס**                                    |
|-----------------------------|----------------------------------------------------------------|----------------------------------------------|
| social.explicit_plan        | הודעה עם Entry/Target/Stop או הנחיית מסחר ברורה.               | אופציונלי; Shadow/Manual; לא Core dependency |
| social.callout_confirmation | המלצה חברתית ללא תכנית מלאה, עם אימות שוק.                     | אופציונלי; דורש independent confirmation     |
| news.catalyst_momentum      | אירוע חדשותי/רשמי עם אישור מחיר ומחזור.                        | Core Shadow/Manual                           |
| exit.position_management    | Stop, targets, time exit, source update ו-thesis invalidation. | MVP Core                                     |
| BREAKOUT                    | אסטרטגיה עצמאית מלאה.                                          | נדחה; Features בלבד                          |
| SWING_CONTINUATION          | אסטרטגיה עצמאית.                                               | נדחה                                         |
| MEAN_REVERSION              | אסטרטגיה עצמאית.                                               | נדחה                                         |
| EARNINGS_EVENT              | אסטרטגיה ייעודית סביב דוחות.                                   | נדחה                                         |
| FUNDAMENTAL_VALUE           | תזה ארוכת טווח.                                                | נדחה                                         |

## 7.1 Features זמינים לאסטרטגיות ה-MVP

> **•** Source reliability לפי Author/Channel/Strategy/Horizon.
>
> **•** זמן מאז ההודעה וגיל האות.
>
> **•** מחיר בזמן ההודעה לעומת מחיר נוכחי.
>
> **•** מרחק מאזור הכניסה ומ-Target.
>
> **•** Relative Volume, Spread, ATR ו-Price Extension.
>
> **•** News/Official Catalyst verification.
>
> **•** InvestingPro selected fundamentals.
>
> **•** Market/Sector context.
>
> **•** Data quality ו-entity confidence.

# 8. הוראות מסחר שה-MVP חייב להבין

| **Instruction**   | **דוגמה**                               |
|-------------------|-----------------------------------------|
| BUY_LIMIT_MAX     | לקנות ב-25 / עד 25.                     |
| BUY_STOP_TRIGGER  | לקנות מעל 25 / בפריצה של 25.            |
| BUY_ZONE          | כניסה בין 24.50 ל-25.                   |
| ENTER_IMMEDIATELY | לקנות עכשיו, בכפוף ל-Max Chase ול-Risk. |
| TARGET_PRICE      | למכור ב-30.                             |
| MULTIPLE_TARGETS  | חצי ב-28 והיתרה ב-30.                   |
| STOP_PRICE        | לצאת מתחת ל-23.                         |
| STOP_PERCENT      | לצאת בירידה של 4% עם Anchor מוגדר.      |
| TIME_EXIT         | לצאת אם לא מתקדם עד זמן/יום מסוים.      |
| MOVE_STOP         | להעלות Stop ל-26.                       |
| PARTIAL_EXIT      | למכור חצי.                              |
| CANCEL_PLAN       | לבטל/טעיתי/לא להיכנס.                   |
| SOURCE_EXIT       | יצאתי/למכור עכשיו.                      |

## 8.1 מגבלות פרשנות

> **•** טיקר או כוונה עמומים עוברים ל-NEEDS_REVIEW.
>
> **•** Stop באחוזים ללא Anchor אינו הופך אוטומטית לפקודה.
>
> **•** עדכון מקור אינו משנה פוזיציה אוטומטית אלא לפי Update Policy.
>
> **•** הודעה שהגיעה לאחר שהמחיר עבר את Max Chase אינה נרדפת.
>
> **•** תיקון ידני נשמר כתווית ולא משנה את Raw Event.

# 9. מנגנון החלטה ב-MVP

| **שלב** | **שם**               | **פעולה**                                       |
|---------|----------------------|-------------------------------------------------|
| 1       | Data Gate            | Entity, freshness, completeness, source health. |
| 2       | Strategy Eligibility | בחירת אחת מאסטרטגיות ה-MVP.                     |
| 3       | Feature Snapshot     | חישוב Features גרסאיים.                         |
| 4       | Strategy Evaluation  | Rules/Weighted Hybrid לפי Manifest.             |
| 5       | Critic Activation    | רק כאשר Policy דורשת.                           |
| 6       | Decision             | REJECT/WATCH/SHADOW/APPROVE/NEEDS_REVIEW.       |
| 7       | Risk                 | Quantity, stop, exposure ו-veto.                |
| 8       | Approval             | אדם או Auto Policy ב-Paper.                     |
| 9       | OrderIntent          | פקודה כללית ל-IBKR Adapter.                     |

## 9.1 סוג Evaluator

ב-MVP ייושם Hybrid Evaluator: Hard Gates דטרמיניסטיים, Rules ו-Weighted Features. לא ייושם בשלב הראשון מודל סטטיסטי מאומן שמקבל לבדו החלטת קנייה.

## 9.2 Outcomes

| **Outcome**  | **משמעות**               |
|--------------|--------------------------|
| REJECT       | נפסל.                    |
| WATCH        | ממתין ל-Trigger או מחיר. |
| SHADOW       | עסקה היפותטית.           |
| APPROVE      | עובר ל-Risk.             |
| NEEDS_REVIEW | דורש הכרעה אנושית.       |
| EXPIRE       | פג תוקף.                 |
| INVALID      | קלט או פלט לא תקין.      |

# 10. ניהול סיכונים ב-MVP

| **יכולת**                    | **MVP**                                     |
|------------------------------|---------------------------------------------|
| Position Sizing              | כמות לפי סכום סיכון ומרחק Stop.             |
| Per-Trade Risk               | ערך גרסאי בקונפיגורציה.                     |
| Daily/Weekly Loss Limits     | Hard Veto.                                  |
| Symbol/Sector Exposure       | Hard Gate או Reduce.                        |
| Liquidity and Spread         | Eligibility ורמת פקודה.                     |
| Buying Power                 | בדיקה מול IBKR.                             |
| Maximum Concurrent Positions | Policy.                                     |
| No Averaging Down            | אסור ב-MVP.                                 |
| No Naked Position            | Stop/Exit Plan חובה לפי אסטרטגיה.           |
| Kill Switch                  | Global/Environment/Account/Strategy/Symbol. |

## 10.1 Defaults ראשוניים

| הערכים המספריים עצמם לא נקבעים במסמך זה. Claude Code יקבל Config Placeholders. הערכים ייקבעו במסמך Policy Parameters לאחר סקירת כל האפיון. |
|--------------------------------------------------------------------------------------------------------------------------------------------|

# 11. ביצוע ופקודות ב-MVP

| **סוג פקודה** | **שימוש**                                     |
|---------------|-----------------------------------------------|
| LIMIT         | כניסה באזור/מחיר מרבי.                        |
| STOP          | כניסת פריצה או הגנה.                          |
| STOP_LIMIT    | כניסה/הגנה כאשר Policy מאפשרת.                |
| MARKET        | יציאת חירום או מקרה מפורש בלבד.               |
| BRACKET       | Entry + target + protective stop.             |
| OCA/OCO       | יעדים והגנות חלופיים.                         |
| TRAILING      | לא כברירת מחדל; רק אם Strategy Policy מאושרת. |

## 11.1 Broker Scope

> **•** IBKR Paper בלבד.
>
> **•** Broker Mock מלא לבדיקות.
>
> **•** BrokerAdapter interface עבור ברוקר עתידי.
>
> **•** Preview/What-if כאשר זמין.
>
> **•** Partial Fills, Cancel, Modify ו-Reconciliation.
>
> **•** Broker-side Protective Order חובה לפני PAPER_AUTO.

# 12. ניהול פוזיציה ויציאה ב-MVP

| **Trigger**         | **התנהגות**                         |
|---------------------|-------------------------------------|
| Protective Stop     | Stop Price/Percent לאחר Resolve.    |
| Take Profit         | יעד אחד או מספר יעדים.              |
| Partial Exit        | מכירת חלק ועדכון Stop לכמות שנותרה. |
| Time Exit           | יציאה לאחר חלון זמן לפי Strategy.   |
| Thesis Invalidation | אירוע/מבנה שסותר את העסקה.          |
| Source Update       | Review של הוראת מקור חדשה.          |
| Manual Exit         | סגירה דרך Dashboard.                |
| Risk Reduction      | הקטנה לפי Risk Policy.              |
| Trailing/Break-even | רק אם הוגדר ב-Strategy Policy.      |

## 12.1 תרחיש הכרחי

> Source:
>
> Buy at 25
>
> Target 30
>
> Stop 23
>
> System:
>
> Detect current price
>
> Reject if max-chase exceeded
>
> Calculate quantity from risk
>
> Submit bracket to IBKR Paper
>
> Reconcile fills
>
> Keep stop and targets aligned with filled quantity
>
> Exit if stop/target/source-approved update triggers

# 13. למידה ותחקור ב-MVP

| **יכולת**           | **MVP**                                                           |
|---------------------|-------------------------------------------------------------------|
| Decision Snapshot   | כל Evidence, Feature, Version ו-Policy.                           |
| Execution Analysis  | Signal price, decision price, order price, fill, fees ו-slippage. |
| MFE/MAE             | תנועה מיטבית ומנוגדת במהלך העסקה.                                 |
| Decision Quality    | טובה/גרועה בנפרד מהתוצאה.                                         |
| Error Taxonomy      | Source, entity, data, timing, strategy, risk, execution.          |
| Source Performance  | לפי מקור, אסטרטגיה, סקטור ואופק.                                  |
| Champion/Challenger | אותם Events ב-Replay/Shadow.                                      |
| Change Proposal     | הצעה גרסאית; אין שינוי אוטומטי של Active Policy.                  |

## 13.1 מה לא נחשב למידה ב-MVP

> **•** שינוי משקל לאחר עסקה בודדת.
>
> **•** שכתוב אוטומטי של Strategy פעילה.
>
> **•** Auto-promotion של Challenger.
>
> **•** Fine-tuning אוטומטי של מודל.
>
> **•** החלטת LLM להגדיל סיכון משום ש'הוא בטוח'.

# 14. מסכי MVP

| **מסך**                      | **תוכן**                                                           |
|------------------------------|--------------------------------------------------------------------|
| Login and Environment Banner | כניסה וזיהוי Paper.                                                |
| Overview Dashboard           | Health, candidates, positions, orders, P&L ו-drawdown.             |
| Live Event Feed              | הודעות, חדשות וסטטוס עיבוד.                                        |
| Candidate Workspace          | Raw, entity, source plan, features, strategy, critic ו-risk.       |
| Approval Queue               | אישור/דחייה/Watch/Review.                                          |
| Orders and Executions        | OrderIntent, broker orders, fills ושגיאות.                         |
| Positions                    | quantity, average price, stop, targets, exposure ו-exit triggers.  |
| Sources and Health           | WhatsApp, Discord, Telegram, InvestingPro, AI, Market Data ו-IBKR. |
| Source Performance           | מדדים ראשוניים למקורות.                                            |
| Strategies and Policies      | Versions, state ו-active pointer.                                  |
| Replay and Comparison        | הרצת גרסאות והשוואת תוצאות בסיסית.                                 |
| Post-Trade Review            | תחקור, תיוג וטעות.                                                 |
| Audit and Emergency          | Audit, kill switch ו-runbooks.                                     |

## 14.1 נדחה מה-UI

> **•** אפליקציית מובייל נפרדת.
>
> **•** ממשק SaaS מרובה לקוחות.
>
> **•** Builder ויזואלי מלא לאסטרטגיות.
>
> **•** דוחות BI מתקדמים ומאות גרפים.
>
> **•** מערכת Billing או Subscriptions.

# 15. תשתית MVP

| **רכיב**              | **תפקיד**                                                         |
|-----------------------|-------------------------------------------------------------------|
| Oracle ARM Server     | API, Dashboard, Workers, DB, Queue, Object/Files ו-Observability. |
| PostgreSQL            | מסד ראשי.                                                         |
| Docker Compose        | פריסה ראשונית.                                                    |
| Internal Queue        | תיבחר במסמך הטכנולוגי.                                            |
| Object Storage/Volume | Raw, attachments ו-snapshots.                                     |
| Execution Agent       | IBKR Gateway/TWS וה-Adapter.                                      |
| Reverse Proxy/TLS     | חשיפה מאובטחת של Dashboard/API.                                   |

# 16. Phases של ה-MVP

| **Phase**                  | **תכולה**                                                              |
|----------------------------|------------------------------------------------------------------------|
| M0 — Foundation            | Contracts, Config, DB, Audit, Mocks, Health ו-Repository.              |
| M1 — Ingestion             | WhatsApp, Discord, Telegram, InvestingPro, News/Filings ו-Market Data. |
| M2 — Understanding         | Entity, instructions, AI schemas, candidates ו-features.               |
| M3 — Shadow Decision       | Strategies, decision, risk, replay ו-shadow.                           |
| M4 — Paper Manual          | IBKR Paper, approvals, bracket, fills, positions ו-exits.              |
| M5 — Operational Hardening | alerts, backups, restore, runbooks, kill switch ו-chaos tests.         |
| M6 — Restricted Paper Auto | אסטרטגיה אחת מאושרת, Universe מוגבל ו-Broker protection.               |
| M7 — Six-Month Research    | איסוף תוצאות, calibration, source scoring ו-challengers.               |

## 16.1 כלל מעבר בין Phases

אין מעבר אוטומטי בין Phases. כל Phase נסגר רק לאחר מעבר קריטריוני הקבלה, תיקון ליקויים ואישור מפורש.

# 17. קריטריוני קבלה לכל Phase

| **Phase** | **Exit Criterion**                                                 |
|-----------|--------------------------------------------------------------------|
| M0        | Contracts ו-Mocks עוברים; Config versioning ו-Audit פועלים.        |
| M1        | מקורות נקלטים, Dedup עובד, Health מוצג ו-Raw ניתן ל-Replay.        |
| M2        | Benchmark מתויג מוכיח זיהוי Instructions ו-NEEDS_REVIEW.           |
| M3        | אותו Event ניתן להרצה חוזרת; Decision/Risk דטרמיניסטיים.           |
| M4        | עסקה מלאה ב-IBKR Paper כולל Partial Fill ו-Reconciliation.         |
| M5        | Restore, disconnect, duplicate, disk ו-missing-stop drills עוברים. |
| M6        | 30 ימים רצופים ללא כשל בטיחות קריטי ב-Paper Manual/Shadow.         |
| M7        | לפחות שישה חודשים Paper ודו״ח מעבר מסודר לפני דיון ב-Live.         |

# 18. קריטריוני קבלה כלליים ל-MVP

| **מזהה**  | **קריטריון**                                                |
|-----------|-------------------------------------------------------------|
| MVP-AC-01 | כל Trade Decision ניתן לשחזור מ-Snapshot.                   |
| MVP-AC-02 | אין פקודה ללא Decision ו-Risk תקפים.                        |
| MVP-AC-03 | אין כפילות פקודות גם לאחר Retry/Restart.                    |
| MVP-AC-04 | Paper אינו מסוגל להגיע ל-Live.                              |
| MVP-AC-05 | טיקר עמום אינו מגיע ל-PAPER_AUTO.                           |
| MVP-AC-06 | המערכת מבינה את Fixtures של Entry, Target, Stop ו-Revision. |
| MVP-AC-07 | InvestingPro Drift עוצר פרסום ומייצר Alert.                 |
| MVP-AC-08 | Market Data stale חוסם פעולה לפי Policy.                    |
| MVP-AC-09 | Broker disconnect מחייב Reconciliation.                     |
| MVP-AC-10 | פוזיציה הדורשת Stop אינה נשארת ללא הגנת ברוקר ב-PAPER_AUTO. |
| MVP-AC-11 | Kill Switch עוצר כניסות בלי למחוק Stops קיימים.             |
| MVP-AC-12 | Backup ו-Restore מוכחים.                                    |
| MVP-AC-13 | Source/Strategy performance ניתן לחישוב.                    |
| MVP-AC-14 | Change Proposal אינו משנה Active Version ללא אישור.         |
| MVP-AC-15 | Dashboard מציג Health, Orders, Positions ו-Audit.           |

# 19. מחוץ ל-MVP

> **•** כסף אמיתי מכל סוג.
>
> **•** מסחר באופציות, Short, Forex, Futures ו-Crypto.
>
> **•** מסחר אוטומטי ב-Pre-market או After-hours.
>
> **•** Multi-broker production; נשמר רק Interface ומוק.
>
> **•** High-Frequency Trading או Microsecond execution.
>
> **•** Portfolio Optimizer מתקדם.
>
> **•** למידה עצמית שמשנה אסטרטגיה פעילה.
>
> **•** Fine-tuning אוטומטי.
>
> **•** מאות מקורות חברתיים או Crawling פתוח של האינטרנט.
>
> **•** אפליקציית מובייל נפרדת.
>
> **•** SaaS מרובה לקוחות, Billing ו-RBAC ארגוני מלא.
>
> **•** Social write-back או שליחת הודעות אוטומטיות.
>
> **•** אלגוריתמי ביצוע מוסדיים לפקודות גדולות.

# 20. החלטות פתוחות לפני תכנית היישום

| **מזהה**    | **החלטה**                                             |
|-------------|-------------------------------------------------------|
| MVP-OPEN-01 | מימוש טכני מדויק של WhatsApp ו-Discord User Adapters. |
| MVP-OPEN-02 | Primary Market Data Provider לאחר Benchmark.          |
| MVP-OPEN-03 | ספק News ראשי.                                        |
| MVP-OPEN-04 | ספקי AI ומודל לכל Task.                               |
| MVP-OPEN-05 | Queue ו-Observability stack.                          |
| MVP-OPEN-06 | Secrets Manager.                                      |
| MVP-OPEN-07 | מיקום Execution Agent.                                |
| MVP-OPEN-08 | Policy Parameters המספריים הראשוניים.                 |
| MVP-OPEN-09 | רשימת הקבוצות, הערוצים וה-Watchlists הראשונית.        |
| MVP-OPEN-10 | האסטרטגיה היחידה שתאושר ראשונה ל-PAPER_AUTO.          |

# 21. השלב הבא

> 1\. Technology and Deployment Decisions v0.6 — בחירת Stack ורכיבי תשתית.
>
> 2\. Policy Parameters and Initial Assumptions v0.5 — ערכי סף, סיכון, TTL ו-Latency ראשוניים.
>
> **3.** Consolidated Architecture Review — בדיקת סתירות, כפילויות וחוסרים בכל המסמכים.
>
> **4.** Traceability Matrix — קישור דרישה למסמך, רכיב, בדיקה וקריטריון קבלה.
>
> **5.** Claude Code Implementation Plan — רק לאחר השלמת כל השלבים ואישור מפורש.

# 22. Scope מתוקן ומחייב ל-MVP

| **הפרק מעדכן את ה-MVP בעקבות הבדיקה הרוחבית: Whole Shares, PoI Track, Scanner deferral והפרדת Operational Gates מראיות סטטיסטיות.** |
|-------------------------------------------------------------------------------------------------------------------------------------|

## 22.1 Scope סופי

> **•** US stocks and ETFs, Long only, whole shares only.
>
> **•** RTH execution בלבד; Pre/After לצפייה ומחקר.
>
> **•** IBKR Paper הוא Adapter הביצוע הראשון, אך כל הליבה Broker-agnostic.
>
> • סורקי ליבה טכניים, פונדמנטליים ואירועיים כלולים. Builder מתקדם לסורקים וסטטיסטיקל ארביטראז׳ נדחים.
>
> **•** Official Filings חובה; external News provider conditional.
>
> **•** news.catalyst_momentum אינו Paper Auto ב-MVP.

## 22.2 Phase M0.5 — Proof of Integration

| **PoI**      | **Pass Condition**                      |
|--------------|-----------------------------------------|
| IBKR         | full paper lifecycle + reconcile        |
| WhatsApp     | stable IDs/reply/reconnect              |
| Discord      | threads/edits/resume                    |
| InvestingPro | login/canary/field catalog              |
| Market Data  | freshness/bars/calendar/field ownership |
| Sessions     | encrypted runtime store                 |
| ARM Stack    | all candidate runtimes/images execute   |

> **•** כישלון PoI אינו מוסתר באמצעות קוד ספציפי בליבה; מחליפים Adapter או Scope.

## 22.3 אסטרטגיות MVP

| **Strategy**                | **Mode**                                                     |
|-----------------------------|--------------------------------------------------------------|
| social.explicit_plan        | Shadow -\> Paper Manual -\> Restricted Paper Auto candidate. |
| social.callout_confirmation | Shadow/Paper Manual.                                         |
| news.catalyst_momentum      | Shadow/Manual after source PoI.                              |
| exit.position_management    | Core deterministic/policy engine.                            |

## 22.4 Gates

> **•** Operational Promotion: zero duplicate/environment/protection failures, stable reconciliation, extraction benchmarks.
>
> **•** Statistical Evidence: positive expectancy after costs, confidence intervals, regime coverage and holdout stability.
>
> **•** מעבר ל-Paper Auto דורש Operational Gate; מעבר עתידי ל-Live דורש גם Statistical Evidence.
>
> **•** אין Auto Promotion.

## 22.5 Out of Scope נוסף

> **•** Fractional shares.
>
> **•** Independent technical scanner.
>
> **•** Automatic broker failover.
>
> **•** External news Auto strategy before provider selection.
>
> **•** Corporate-action auto-adjustment without reconciliation.

## 22.6 MVP Acceptance נוסף

> **•** PoI failures are reported before full connector build.
>
> **•** All Auto gates use calibrated confidence.
>
> **•** Queued intents obey kill state.
>
> **•** Replay passes point-in-time test.
>
> **•** Every position has a single exit command writer.

# 23. Scope רב-מודלי מחייב ל-MVP

| MVP מתוקן כך שיוכל לגלות ולנתח עסקאות ללא WhatsApp, Discord או Telegram. המערכת תכלול סריקות פנימיות, נתוני אינטרנט/שוק, מנועי Technical/Fundamental/News ושכבת Fusion. ML יתחיל ב-Shadow ולא יקבל סמכות Auto לפני כיול. |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 23.1 Core Capabilities

| **יכולת**             | **MVP**                                                                        |
|-----------------------|--------------------------------------------------------------------------------|
| Technical Scanner     | Breakout, volume spike, moving-average trend, pullback and volatility filters. |
| Fundamental Watchlist | Rules-based quality/growth/valuation scoring from point-in-time data.          |
| News/Filings Scanner  | Catalyst discovery, official verification, novelty and veto.                   |
| TradingView Adapter   | Webhook ingestion and internal validation; not direct execution.               |
| Signal Fusion         | Required/optional/veto Manifest and explanation.                               |
| Quant/ML              | Regime/ranking in REPLAY/SHADOW only until calibrated.                         |
| Social                | Optional candidate source and explicit source plan.                            |

## 23.2 אסטרטגיות MVP מתוקנות

| **Priority** | **Strategy**                    | **מצב יעד**                                     |
|--------------|---------------------------------|-------------------------------------------------|
| 1            | technical.breakout_news_guard   | ראשונה ל-Restricted Paper Auto לאחר Gates.      |
| 2            | techno_fundamental.breakout     | Shadow/Manual; Auto לאחר Fundamental PoI ומדגם. |
| 3            | news.catalyst_momentum          | Shadow/Manual.                                  |
| 4            | social.independent_confirmation | Optional Shadow/Manual.                         |
| 5            | exit.position_management        | Deterministic core.                             |

## 23.3 מה נדחה

> **•** Pairs Trading/Statistical Arbitrage, Short Selling ו-Multi-leg execution.
>
> **•** Online Learning, Auto-retuning ו-Auto-promotion.
>
> **•** Visual no-code strategy/scanner builder.
>
> **•** High-frequency or tick-level execution.
>
> **•** ML position sizing independent of Risk Engine.

## 23.4 Phases מתוקנות

| **Phase** | **תכולה**                                                              |
|-----------|------------------------------------------------------------------------|
| M0        | Contracts, DB, events, audit, broker abstraction and UX shell.         |
| M0.5      | PoI: market/fundamental/news/TradingView/IBKR/ARM; social in parallel. |
| M1        | Market/news/fundamental ingestion and core scanners.                   |
| M2        | Technical/Fundamental/News assessments and Fusion.                     |
| M3        | Replay/Shadow and model registry; ML shadow.                           |
| M4        | Paper Manual and full operational console.                             |
| M5        | Hardening, emergency drills, backup/restore and monitoring.            |
| M6        | Restricted Paper Auto for one approved non-social-dependent strategy.  |
| M7        | Six-month research and challenger evaluation.                          |

## 23.5 Acceptance נוסף

| **מזהה**  | **קריטריון**                                                       |
|-----------|--------------------------------------------------------------------|
| MVP-MM-01 | Core pipeline completes with WhatsApp and Discord disabled.        |
| MVP-MM-02 | Technical scanner creates candidates from market data.             |
| MVP-MM-03 | Fundamental engine creates point-in-time watchlist memberships.    |
| MVP-MM-04 | TradingView alert is revalidated and cannot execute directly.      |
| MVP-MM-05 | Candidate page shows six analysis families and fusion explanation. |
| MVP-MM-06 | Emergency halt works from every main screen.                       |

# **22. השלמות MVP למערכת אוטונומית וגמישה**

| **יכולת**            | **היקף MVP**                                                               |
|----------------------|----------------------------------------------------------------------------|
| Autonomous Discovery | Scanner טכני רציף, Watchlist פונדמנטלית תקופתית ו-Event candidates.        |
| Multi-Model Fusion   | Required/Soft/Veto/Bonus לפי Strategy Manifest; אין דרישת הסכמה מלאה.      |
| Opportunity Funnel   | מדידת כל שלב, Gate concentration, starvation ו-overtrading.                |
| Rejected Shadow      | מעקב Counterfactual אחר Candidates תקפים שלא בוצעו.                        |
| Portfolio Allocation | Basic risk budgets, symbol/sector/conflict/correlation limits.             |
| Market Regime        | סיווג בסיסי המשמש Context ו-Strategy activation.                           |
| Model Governance     | Registry, version, metrics, shadow challenger ו-manual promotion.          |
| Trust Layer          | Webhook verification, provenance, quarantine ו-prompt-injection isolation. |
| Emergency Control    | Dashboard button + minimal out-of-band control.                            |
| Notifications        | Critical operational and trading alerts דרך Adapter כללי.                  |

## **22.1 גבולות ML ב-MVP**

> **•** ML predictive model יכול לפעול ב-Replay/Shadow; אינו תנאי הכרחי להפעלת המסחר הראשוני.
>
> **•** Market Regime יכול להתחיל כחוקים שקופים ולהתחרות במודל ML כ-Challenger.
>
> **•** אין Online Learning, Auto-Retrain או Auto-Promotion.

## **22.2 מדד הצלחה**

> **•** אין מכסת עסקאות מינימלית.
>
> **•** נדרש להסביר כמות פעילות נמוכה באמצעות Funnel ו-Gate results.
>
> **•** נדרש למדוד עסקאות שנדחו כדי להבחין בין בטיחות נכונה לבין קשיחות יתר.
>
> **•** נדרש Alert כאשר Gate לא-בטיחותי יחיד חוסם שיעור חריג לאורך חלון מוגדר.

# **24. Telegram ב-MVP**

| **יכולת**                          | **החלטת MVP**                                                |
|------------------------------------|--------------------------------------------------------------|
| Inbound source                     | כן; קבוצות וערוצים מאושרים בלבד.                             |
| Access mode                        | TDLib בחשבון המשתמש חובה; Bot API אופציונלי ומשלים לאחר PoI. |
| Read history                       | רק לפי הרשאות ומדיניות Point-in-Time; לא לצורך זיוף זמן אמת. |
| Edits/Deletes/Replies/Topics       | כן, כאשר ה-API הנבחר מספק.                                   |
| Attachments                        | כן, דרך Trust/Quarantine.                                    |
| Outbound alerts                    | כן, אופציונלי לצ'אט פרטי של הבעלים.                          |
| Outbound social posting            | לא.                                                          |
| Remote trade or emergency commands | לא.                                                          |
| Core dependency                    | לא; Core Pipeline חייב לעבור Acceptance כאשר Telegram כבוי.  |

## **24.1 PoI Gate**

> **•** בחירת מסלול Bot API או TDLib למקורות הראשונים.
>
> **•** הוכחת reconnect, dedup, edits, deletes, attachments ו-Allowlist.
>
> **•** הוכחת Session מוצפן ו-Re-authentication.
>
> **•** הוכחת Failure Isolation ו-Health בממשק.

# **25. Scope מחייב של חיבורים חברתיים**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **יכולת**                             | **In MVP**        | **הערה**                                                    |
|---------------------------------------|-------------------|-------------------------------------------------------------|
| WhatsApp via user account             | כן - חובה         | לא מוחלף ב-Bot.                                             |
| Discord via user account              | כן - חובה         | לא מוחלף ב-Bot.                                             |
| Telegram via user account/TDLib       | כן - חובה ועיקרי  | Gate למסירת Social Integration.                             |
| Telegram Bot API                      | כן - אפשרות שנייה | משלים לקליטה/התראות; אינו תחליף.                            |
| Remote trade commands via social apps | לא                | תוכן נכנס הוא Evidence בלבד.                                |
| Autonomous core without social uptime | כן                | המערכת ממשיכה עם scanners, market/news/fundamental sources. |
