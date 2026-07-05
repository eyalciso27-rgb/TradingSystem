תאריך: 5 ביולי 2026

**סטטוס: גרסה מלאה ומתוקנת לאחר Consolidated Architecture Review; מועמדת ל-Baseline פנימי; אין למסור ל-Claude Code לפני השלמת Traceability Matrix ואישור מפורש.**

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

| **מטרת המסמך היא להגדיר כיצד רעיון מסחר הופך - או אינו הופך - לעסקה: כיצד מבינים הודעה עם מחיר כניסה, יעד ו-Stop; כיצד בוחרים אסטרטגיה; מי מכריע; איך מנהלים עסקה לאחר הכניסה; מתי מוכרים; ואיך משפרים את המערכת באמצעות נתונים בלי לשנות את עצמה בצורה בלתי מבוקרת.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 0. מסמכי בסיס והחלטות נעולות

| **מסמך**                                    | **תפקיד**                                                       |
|---------------------------------------------|-----------------------------------------------------------------|
| Decision Contract v0.8                      | סמכויות, Outcomes, Hard Veto, Trade Instructions ו-OrderIntent. |
| System Functional Specification v0.7        | רכיבי המערכת, מסכים, תהליכים, פוזיציות ויציאות.                 |
| Data Model and Retention Specification v0.6 | ישויות, היסטוריה, גרסאות, Orders, Fills ו-Positions.            |
| Integration Specification v0.6              | מקורות, נתוני שוק, AI, IBKR ואמינות אינטגרציות.                 |
| Security and Operations Specification v0.6  | הרשאות, Kill Switch, Broker Protection, Backups ו-Runbooks.     |

> **•** LLM אינו מוסמך לקנות, למכור, לקבוע כמות או לעקוף Risk Engine.
>
> **•** כל החלטה מתקבלת על Snapshot קפוא, Strategy Version ו-Policy Version.
>
> **•** מה שהמקור כתב נשמר בנפרד ממה שהמערכת אישרה בפועל.
>
> **•** המערכת תומכת בהודעות הכוללות מחיר כניסה, יעד, Stop ועדכוני המשך.
>
> **•** שינוי משקלים, ספים, מודלים או כללי מסחר יוצר גרסה חדשה ואינו משנה היסטוריה.
>
> **•** לא כל הפסד הוא טעות, ולא כל רווח הוא החלטה טובה.
>
> **•** לפני Paper Auto או Live, הגנה נדרשת חייבת להיות מוצבת אצל הברוקר.

# 1. מטרות המסגרת

> **•** להפריד בין גילוי מניה, בחירת אסטרטגיה, החלטת כניסה, ניהול סיכונים וביצוע.
>
> **•** להגדיר שפה אחידה לכל אסטרטגיה, בלי לקודד כללים בליבת המערכת.
>
> **•** לאפשר כמה אסטרטגיות על אותה מניה בלי לערבב תזות סותרות.
>
> **•** להגדיר מראש תנאי כניסה, ביטול תזה, הגנה, יעדים ואופק זמן.
>
> **•** להמשיך לנתח פוזיציה לאחר הכניסה ולמכור כאשר תנאי היציאה מתקיימים.
>
> **•** למדוד איכות החלטה, איכות ביצוע ותוצאה כספית בנפרד.
>
> **•** לאפשר Champion/Challenger, Replay, Shadow, Calibration ו-Rollback.

# 2. מושגים מחייבים

| **מונח**                 | **הגדרה**                                                        |
|--------------------------|------------------------------------------------------------------|
| Source Trade Instruction | הוראת המסחר שהופיעה במקור: כניסה, יעד, Stop, תוקף ועדכונים.      |
| Candidate                | נכס ואירוע שהמערכת החליטה להעביר לבדיקה.                         |
| Strategy Family          | משפחת תזה, כגון News Momentum או Mean Reversion.                 |
| Strategy Instance        | יישום גרסאי של אסטרטגיה עם Manifest ו-Policies.                  |
| Feature Snapshot         | ערכי Features קפואים ששימשו בהערכה.                              |
| Trade Thesis             | הסיבה הכלכלית/טכנית/אירועית לכניסה.                              |
| Invalidation             | התנאי שמוכיח שהתזה אינה תקפה עוד.                                |
| System Trade Plan        | תוכנית העסקה שאישרה המערכת לאחר אימות וסיכון.                    |
| Decision Outcome         | REJECT, WATCH, SHADOW, APPROVE, NEEDS_REVIEW, EXPIRE או INVALID. |
| Exit Decision            | החלטת סגירה/הקטנה המבוססת על טריגר ו-Exit Policy.                |
| R-Multiple               | רווח או הפסד ביחס לסיכון הראשוני המאושר.                         |
| MFE/MAE                  | התנועה המרבית לטובת/נגד הפוזיציה בזמן שהייתה פתוחה.              |

# 3. מחזור החיים של רעיון מסחר

| **שלב** | **מצב**     | **תיאור**                                       |
|---------|-------------|-------------------------------------------------|
| 1       | TRIGGERED   | הודעה, חדשות, סורק, Watchlist או אירוע פוזיציה. |
| 2       | INTERPRETED | זיהוי נכס, כוונה, כיוון ו-Trade Instruction.    |
| 3       | CANDIDATE   | נוצר Candidate עם Priority, Profile ו-Expiry.   |
| 4       | ELIGIBILITY | נבדק לאילו אסטרטגיות הוא מתאים.                 |
| 5       | EVALUATION  | חישוב Features והערכת כל Strategy Instance.     |
| 6       | DECISION    | Outcome לכל אסטרטגיה בנפרד.                     |
| 7       | RISK        | Quantity, Stop, Max Loss, Exposure ו-Hard Veto. |
| 8       | APPROVAL    | אישור אנושי כאשר נדרש.                          |
| 9       | EXECUTION   | OrderIntent, Broker Order ו-Fills.              |
| 10      | POSITION    | מעקב תזה, סיכון, הגנות וטריגרי יציאה.           |
| 11      | EXIT        | מכירה מלאה/חלקית או סגירה כפויה.                |
| 12      | REVIEW      | תחקור, תיוג, Metrics והצעת שינוי.               |

# 4. חילוץ Trade Instruction מהודעות

| **המערכת אינה מסתפקת בסנטימנט חיובי/שלילי. עליה לחלץ את semantics המדויקים של ההוראה: האם המחיר הוא Limit, Breakout Trigger, Entry Zone, Target או Stop; מה נקודת הייחוס לאחוזים; ומהו תוקף ההודעה.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 4.1 סוגי הוראות כניסה

| **סוג**           | **דוגמה**              | **משמעות**                           |
|-------------------|------------------------|--------------------------------------|
| BUY_LIMIT_MAX     | "לקנות ב-25" / "עד 25" | קנייה ב-25 או במחיר טוב יותר.        |
| BUY_STOP_TRIGGER  | "לקנות מעל 25"         | Trigger רק לאחר חציה כלפי מעלה.      |
| BREAKDOWN_BELOW   | "שורט מתחת ל-25"       | Trigger רק לאחר חציה כלפי מטה.       |
| BUY_ZONE          | "כניסה 24.5-25"        | טווח מחירים מותר.                    |
| ENTER_IMMEDIATELY | "לקנות עכשיו"          | דורש Market/Spread/Max Chase Policy. |
| BUY_PULLBACK      | "לחכות לחזרה ל-25"     | כניסה לאחר Retracement לרמה.         |
| CONDITIONAL       | "רק אם מחזיקה 25"      | תנאי מבני או זמני נוסף.              |
| UNKNOWN           | ניסוח לא חד-משמעי      | NEEDS_REVIEW או INVALID.             |

## 4.2 סוגי יעד ו-Stop

| **סוג**             | **משמעות**                            |
|---------------------|---------------------------------------|
| TARGET_PRICE        | מכירה במחיר מוגדר.                    |
| TARGET_ZONE         | מכירה באזור מחירים.                   |
| PARTIAL_TARGET      | מכירת חלק מהכמות ביעד.                |
| STOP_PRICE          | יציאה במחיר מוגדר.                    |
| STOP_PERCENT        | יציאה בירידה/עלייה נגדית באחוז מוגדר. |
| STRUCTURE_STOP      | שבירת תמיכה, VWAP, Low או מבנה אחר.   |
| TIME_STOP           | יציאה אם התזה לא התממשה בזמן.         |
| TRAILING_RULE       | Stop נגרר לפי כלל מוגדר.              |
| THESIS_INVALIDATION | אירוע/עובדה שמבטלים את התזה.          |
| SOURCE_EXIT         | המקור כתב "יצאתי" או עדכון שקול.      |

## 4.3 Source Trade Instruction Contract

> {
>
> "instruction_id": "sti\_...",
>
> "source_event_id": "evt\_...",
>
> "instrument_id": "inst\_...",
>
> "intent": "ENTRY\|EXIT\|UPDATE\|DISCUSSION",
>
> "direction": "LONG\|SHORT\|NEUTRAL",
>
> "entry": {
>
> "type": "BUY_LIMIT_MAX\|BUY_STOP_TRIGGER\|BUY_ZONE\|ENTER_IMMEDIATELY\|...",
>
> "price": null,
>
> "range_low": null,
>
> "range_high": null,
>
> "conditions": \[\]
>
> },
>
> "targets": \[\],
>
> "stops": \[\],
>
> "valid_from": "...",
>
> "expires_at": null,
>
> "source_confidence_language": "LOW\|MEDIUM\|HIGH\|UNKNOWN",
>
> "parse_confidence": 0.0,
>
> "revision_of": null,
>
> "extractor_version": "trade-instruction-0.1"
>
> }

## 4.4 נקודת ייחוס ל-Stop באחוזים

> **•** AVG_FILL_PRICE - מחיר המילוי הממוצע בפועל.
>
> **•** SOURCE_ENTRY_PRICE - המחיר שעליו דיבר המקור.
>
> **•** SYSTEM_ENTRY_REFERENCE - מחיר הייחוס שאישרה המערכת.
>
> **•** SESSION_HIGH/LOW או רמה טכנית - כאשר ה-Policy מגדירה זאת.
>
> **•** Stop Percentage ללא Anchor מפורש אינו תקין ואינו עובר לביצוע.

# 5. יצירת Candidate ותיעדוף

| **Priority** | **מקרה**                                               | **Profile**   |
|--------------|--------------------------------------------------------|---------------|
| URGENT       | אירוע מתפרץ, מקור מהיר, פוזיציה פתוחה או Deadline קצר. | FAST          |
| HIGH         | הודעת מסחר מפורשת, חדשות מהותיות או Scanner חריג.      | FAST/STANDARD |
| NORMAL       | Watchlist, Swing או רעיון רגיל.                        | STANDARD      |
| LOW          | פונדמנטלי, Backfill או מחקר.                           | DEEP          |

> **•** Candidate מקבל Expiry כבר בעת יצירתו.
>
> **•** אות שהתיישן אינו מקבל הארכת תוקף אוטומטית בגלל עיכוב מערכת.
>
> **•** אות חברתי ללא מחיר כניסה יכול להישאר Candidate, אך דורש Strategy שמסוגלת לייצר Plan עצמאי.
>
> **•** Candidate לפוזיציה פתוחה מקבל קדימות על Candidate חדש כאשר קיים סיכון מיידי.

# 6. מסגרת Strategy Plug-in

## 6.1 חוזה Strategy

> interface StrategyEvaluator {
>
> id: string;
>
> version: string;
>
> checkEligibility(context: CandidateContext): EligibilityResult;
>
> requiredFeatures(): FeatureRequirement\[\];
>
> evaluate(snapshot: StrategySnapshot): StrategyEvaluation;
>
> buildTradePlan(evaluation: StrategyEvaluation): SystemTradePlan;
>
> buildExitPlan(tradePlan: SystemTradePlan): ExitPlan;
>
> }

## 6.2 Strategy Manifest

> strategy:
>
> id: social_callout_confirmation
>
> version: 0.1.0
>
> family: SOCIAL
>
> state: DRAFT
>
> supported_assets: \[STOCK, ETF\]
>
> supported_directions: \[LONG\]
>
> processing_profile: STANDARD
>
> required_features: \[\]
>
> optional_features: \[\]
>
> eligibility_policy_ref: eligibility.social.v0.1
>
> decision_policy_ref: decision.social.v0.1
>
> risk_policy_ref: risk.social.v0.1
>
> exit_policy_ref: exit.social.v0.1
>
> critic_policy_ref: critic.social.v0.1
>
> valid_from: null
>
> valid_until: null
>
> **•** Strategy אינה קוראת ישירות HTML, WhatsApp, Discord או API.
>
> **•** Strategy מקבלת Snapshot מובנה וקפוא בלבד.
>
> **•** StrategyEvaluation היא דטרמיניסטית עבור אותו Snapshot ו-Version.
>
> **•** שינוי אלגוריתם יוצר Strategy Version חדשה.
>
> **•** שינוי סף/משקל בלבד יוצר Policy Version חדשה.

# 7. משפחות אסטרטגיה

## 7.1 SOCIAL_CALLOUT

| **רכיב**      | **הגדרה**                                                                                  |
|---------------|--------------------------------------------------------------------------------------------|
| מטרה          | להשתמש בהמלצה/תוכנית מסחר ממקור חברתי כ-Trigger, לא כהוראת ביצוע אוטומטית.                 |
| Eligibility   | זוהה נכס, כוונה מסחרית, מקור וקונטקסט מספק.                                                |
| Core Features | source reliability, latency, price extension, liquidity, catalyst, technical confirmation. |
| Entry         | Source Plan כפי שהוא או System Plan מותאם לפי Policy.                                      |
| Invalidation  | Stop מקור, Stop מערכת, שינוי מהותי, Expiry או כשל תזה.                                     |
| ייחוד         | מדידת ביצועי המקור בנפרד מביצועי ניהול המערכת.                                             |

## 7.2 NEWS_MOMENTUM

| **רכיב**      | **הגדרה**                                                                                      |
|---------------|------------------------------------------------------------------------------------------------|
| מטרה          | הצטרפות לתנועה הנובעת מ-Catalyst מהותי ומאומת.                                                 |
| Eligibility   | אירוע מזוהה, זמן פרסום ידוע, שוק פעיל ונתוני מחיר עדכניים.                                     |
| Core Features | catalyst strength, source class, relative volume, gap, spread, market regime, price extension. |
| Entry         | Breakout, Pullback או Continuation לפי גרסה.                                                   |
| Invalidation  | כשל מחיר/מחזור, תיקון מידע, Expiry או תגובת שוק מנוגדת.                                        |
| Profile       | FAST או STANDARD.                                                                              |

## 7.3 BREAKOUT

| **רכיב**      | **הגדרה**                                                                             |
|---------------|---------------------------------------------------------------------------------------|
| מטרה          | כניסה לאחר יציאה ממבנה מחיר מוגדר עם אישור.                                           |
| Eligibility   | רמת פריצה, היסטוריית Bars, נזילות ו-Session מתאימים.                                  |
| Core Features | breakout level, relative volume, false-breakout risk, spread, distance to resistance. |
| Entry         | Stop/Stop-Limit מעל/מתחת לרמה או Retest.                                              |
| Invalidation  | חזרה מתחת/מעל לרמה, Time Stop או כשל Volume.                                          |

## 7.4 SWING_CONTINUATION

| **רכיב**      | **הגדרה**                                                                   |
|---------------|-----------------------------------------------------------------------------|
| מטרה          | המשך מגמה לאחר התבססות, Pullback או Retest.                                 |
| Eligibility   | מגמה קיימת, מבנה תקין, Horizon של ימים עד שבועות.                           |
| Core Features | relative strength, trend structure, sector regime, earnings proximity, ATR. |
| Entry         | Pullback/Retest/Breakout משני.                                              |
| Invalidation  | שבירת מבנה, Event Risk, Time Stop או שינוי מגמה.                            |

## 7.5 MEAN_REVERSION

| **רכיב**      | **הגדרה**                                                                         |
|---------------|-----------------------------------------------------------------------------------|
| מטרה          | מסחר נגד תגובת יתר זמנית כאשר קיימת הצדקה לחזרה.                                  |
| Eligibility   | חריגה סטטיסטית, נזילות, אירוע שאינו מבטל את הערך הבסיסי.                          |
| Core Features | z-score/extension, catalyst severity, gap, volume exhaustion, support/resistance. |
| Entry         | אישור האטה/היפוך, לא תפיסת סכין ללא Trigger.                                      |
| Invalidation  | המשך תנועה, מידע מהותי חדש או שבירת רמת קצה.                                      |
| ייחוד         | אסור לערבב Score שלה עם Momentum; התזה הפוכה.                                     |

## 7.6 EARNINGS_EVENT

| **רכיב**      | **הגדרה**                                                                   |
|---------------|-----------------------------------------------------------------------------|
| מטרה          | מסחר לפני/אחרי דוח או אירוע מתוזמן לפי תזה מוגדרת.                          |
| Eligibility   | לוח אירועים, Session, Volatility, Liquidity ו-Policy המתאימה.               |
| Core Features | surprise, guidance, reaction, options-implied move אם זמין, gap, revisions. |
| Entry         | רק בגרסה ייעודית; אין החזקה דרך דוח כברירת מחדל.                            |
| Invalidation  | תגובה שונה מהתזה, Time Stop או שינוי Guidance.                              |

## 7.7 FUNDAMENTAL_VALUE

| **רכיב**      | **הגדרה**                                                                |
|---------------|--------------------------------------------------------------------------|
| מטרה          | תזה ארוכה יותר המבוססת על איכות, תמחור, צמיחה ומאזן.                     |
| Eligibility   | נתונים פונדמנטליים מספקים, Horizon ארוך ונזילות.                         |
| Core Features | valuation, profitability, leverage, growth, revisions, financial health. |
| Entry         | Zone/Scaling Policy נפרדת; אינה תלויה רק באות חברתי.                     |
| Invalidation  | שינוי תזה, הידרדרות נתונים, Valuation Risk או Time Review.               |
| Profile       | DEEP.                                                                    |

## 7.8 EXIT_MANAGEMENT

| **רכיב** | **הגדרה**                                                                |
|----------|--------------------------------------------------------------------------|
| מטרה     | ניהול פוזיציה קיימת, לא יצירת כניסה חדשה.                                |
| Inputs   | Position, Fill, Protection, Market, Source Updates, Thesis State ו-Risk. |
| Outputs  | HOLD, REDUCE, MOVE_STOP, TAKE_PROFIT, CLOSE, EMERGENCY_ACTION.           |
| Priority | גבוהה מ-Candidate חדש כאשר קיים סיכון מיידי.                             |

# 8. Feature Framework

| **קבוצה**   | **דוגמאות**                                               |
|-------------|-----------------------------------------------------------|
| SOURCE      | אמינות, התמחות, איחור, עריכות/מחיקות, איכות יעד ו-Stop.   |
| MESSAGE     | כוונה, כיוון, ביטחון לשוני, מפורטות והקשר.                |
| CATALYST    | סוג אירוע, חומרה, מקור ראשוני, אימות ותיקון.              |
| MARKET      | מחיר, spread, volume, bars, volatility, session, halt.    |
| TECHNICAL   | מבנה, רמות, trend, breakout, relative strength.           |
| FUNDAMENTAL | valuation, growth, profitability, leverage, health.       |
| PORTFOLIO   | חשיפה, קורלציה, buying power ו-open risk.                 |
| QUALITY     | freshness, completeness, provider health ו-conflicts.     |
| TIMING      | signal age, decision latency, price extension ו-deadline. |

> **•** כל Feature מקבל ID, Version, Type, Unit, Source Lineage ו-Freshness.
>
> **•** ערך חסר אינו הופך ל-0 אלא ל-MISSING עם Policy מפורשת.
>
> **•** Feature Snapshot אינו משתנה לאחר החלטה.
>
> **•** Features בדיעבד אינם מוזרקים להחלטה היסטורית המקורית.

# 9. מנגנוני הערכה והכרעה

## 9.1 Evaluator Types

| **סוג**           | **שימוש**                                         |
|-------------------|---------------------------------------------------|
| RULES             | חוקים דטרמיניסטיים ותנאי סף.                      |
| WEIGHTED_SCORE    | שקלול Features לפי Policy.                        |
| STATISTICAL_MODEL | מודל הסתברותי/רגרסיה לאחר שקיימים נתונים מספיקים. |
| ML_MODEL          | מודל למידת מכונה גרסאי לאחר Validation.           |
| HYBRID            | Rules + Model + Hard Gates.                       |

| **בשלב הראשון אין לראות בציון 0-100 הסתברות. Score Display הוא כלי תצוגה; Probability Estimate מותר רק לאחר Calibration על נתונים שלא שימשו לבניית המודל.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 9.2 Strategy Evaluation Contract

> {
>
> "strategy_id": "news_momentum",
>
> "strategy_version": "0.1.0",
>
> "eligible": true,
>
> "evaluator_type": "HYBRID",
>
> "signal": 0.62,
>
> "score_display": 78,
>
> "probability_estimate": null,
>
> "data_quality": 0.91,
>
> "expected_horizon": "INTRADAY\|DAYS\|WEEKS",
>
> "thesis": "...",
>
> "invalidation": \[\],
>
> "warnings": \[\],
>
> "hard_veto_recommendations": \[\],
>
> "feature_snapshot_id": "feat\_...",
>
> "decision_policy_version": "..."
>
> }

## 9.3 Data Quality Gate

> **•** טיקר RESOLVED.
>
> **•** Market Data עומד ב-Freshness שנדרשת לאסטרטגיה.
>
> **•** שדות חובה קיימים או Policy מאפשרת חסרון מפורש.
>
> **•** אין Conflict לא פתור בין ספקי נתונים קריטיים.
>
> **•** כל Provider/Adapter קריטי במצב Health מותר.
>
> **•** החלטה בתוך Deadline ו-Expiry.

## 9.4 Critic Activation Policy

| **מצב**            | **מתי**                                         |
|--------------------|-------------------------------------------------|
| ALWAYS             | אסטרטגיות Deep/Live רגישות.                     |
| ABOVE_THRESHOLD    | רק כאשר Strategy נוטה ל-APPROVE.                |
| ON_CONFLICT        | כאשר מקורות, מודלים או תזות סותרים.             |
| ON_MISSING_CONTEXT | כאשר חסר מידע משמעותי.                          |
| SAMPLED            | אחוז מהחלטות Shadow לצורכי QA.                  |
| DISABLED           | כאשר Policy קובעת שאין ערך או Deadline קצר מדי. |

## 9.5 Outcomes

| **Outcome**  | **משמעות**                          |
|--------------|-------------------------------------|
| REJECT       | התזה/איכות/תזמון אינם מתאימים.      |
| WATCH        | נדרש Trigger נוסף או מחיר טוב יותר. |
| SHADOW       | תוכנית מלאה ללא פקודה.              |
| APPROVE      | רשאי לעבור ל-Risk Engine.           |
| NEEDS_REVIEW | נדרשת הכרעה אנושית.                 |
| EXPIRE       | חלון ההזדמנות הסתיים.               |
| INVALID      | קלט או פלט אינו תקין.               |

# 10. פתרון סתירות בין אסטרטגיות

> **•** אותה מניה יכולה לקבל APPROVE ב-Momentum ו-REJECT ב-Value בלי שזו שגיאה.
>
> **•** אין ממוצע אוטומטי בין אסטרטגיות בעלות תזה, כיוון או Horizon שונים.
>
> **•** כל Trade Proposal נושא strategy_id יחיד, אלא אם Portfolio Policy מגדירה Basket מפורש.
>
> **•** עסקאות LONG ו-SHORT באותו נכס אינן מתקזזות בשקט; נוצרת Conflict Record.
>
> **•** Portfolio Constructor רשאי לבחור Proposal אחד, להקטין או לפסול בגלל קורלציה/חשיפה.
>
> **•** אסטרטגיה קיימת לפוזיציה פתוחה מקבלת קדימות על Candidate חדש הפוך עד להכרעה מפורשת.

# 11. בניית System Trade Plan

## 11.1 עקרונות

> **•** Source Plan נשמר בדיוק כפי שחולץ.
>
> **•** System Plan יכול לאמץ, לצמצם או לדחות את רמות המקור.
>
> **•** כל Plan חייב Entry, Invalidation, Exit Plan, Expiry ו-Risk Reference.
>
> **•** אין Plan ללא Max Chase או כלל מפורש שמונע כניסה מאוחרת.
>
> **•** אין Stop באחוזים ללא Anchor.
>
> **•** כאשר יחס התגמול לסיכון אינו עומד ב-Policy, התוצאה אינה APPROVE.

## 11.2 System Trade Plan Contract

> {
>
> "trade_plan_id": "plan\_...",
>
> "candidate_id": "cand\_...",
>
> "strategy_id": "social_callout_confirmation",
>
> "strategy_version": "0.1.0",
>
> "direction": "LONG",
>
> "entry_plan": {
>
> "type": "BUY_LIMIT_MAX",
>
> "price": 25.00,
>
> "max_chase_price": 25.30,
>
> "valid_until": "..."
>
> },
>
> "invalidation": \[
>
> {"type": "STOP_PRICE", "price": 23.40}
>
> \],
>
> "targets": \[
>
> {"price": 28.00, "quantity_percent": 50},
>
> {"price": 30.00, "quantity_percent": 50}
>
> \],
>
> "time_exit": null,
>
> "source_instruction_id": "sti\_...",
>
> "exit_policy_version": "...",
>
> "status": "PROPOSED"
>
> }

## 11.3 Max Chase

> **•** מגבלה במחיר מוחלט, אחוז, ATR או יחס ליעד לפי Strategy Policy.
>
> **•** אם המחיר עבר את Max Chase, אין Market Order אוטומטי כדי "לא לפספס".
>
> **•** אפשרות: WATCH ל-Pullback, EXPIRE או REJECT.
>
> **•** Max Chase מחושב על מחיר עדכני ו-Spread, לא רק Last Price.

# 12. מנוע סיכון וגודל פוזיציה

| **Strategy מציעה תזה ורמות. Risk Engine בלבד קובע אם מותר לבצע, מהו הסיכון המאושר וכמה יחידות ניתן לקנות. הוא אינו משנה את התזה כדי להכריח עסקה להיכנס.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------|

## 12.1 נוסחת בסיס

> risk_amount = risk_budget_equity \* risk_fraction
>
> risk_per_unit = abs(entry_reference - protective_stop) + estimated_cost_per_unit
>
> raw_quantity = floor(risk_amount / risk_per_unit)
>
> approved_quantity = min(raw_quantity, liquidity_limit, exposure_limit, buying_power_limit)
>
> **•** risk_fraction נשמר ב-Risk Policy ולא בקוד.
>
> **•** החישוב כולל Spread, Slippage ועמלות משוערות לפי יכולת.
>
> **•** Stop רחב יותר מקטין Quantity; הוא אינו מוזז כדי להגדיל Quantity.
>
> **•** אם Quantity קטן מהמינימום או לא כלכלי, העסקה נפסלת.
>
> **•** Risk Engine יכול APPROVE, REDUCE או VETO.

## 12.2 Hard Veto Categories

| **קטגוריה** | **דוגמאות**                                         |
|-------------|-----------------------------------------------------|
| DATA        | stale, ambiguous, missing, conflicting.             |
| MARKET      | halt, spread, liquidity, session, price extension.  |
| PORTFOLIO   | exposure, correlation, drawdown, buying power.      |
| TRADE_PLAN  | no stop, invalid anchor, poor reward/risk, expired. |
| BROKER      | unsynced, unsupported order, disconnected.          |
| ENVIRONMENT | paper/live mismatch, strategy not promoted.         |
| SECURITY    | kill switch, unauthorized approval, incident state. |

# 13. אישור אנושי

> **•** PAPER_MANUAL - המשתמש מאשר, דוחה או מעביר ל-WATCH.
>
> **•** NEEDS_REVIEW - המשתמש בוחר טיקר/כוונה או דוחה.
>
> **•** LIVE_MANUAL - אישור טרי עם Re-authentication בהתאם למסמך האבטחה.
>
> **•** האישור מתייחס ל-Decision Snapshot ול-Trade Plan Version מסוימים.
>
> **•** שינוי מחיר, Quantity, Stop או Expiry לאחר אישור עשוי לחייב אישור חדש לפי Policy.
>
> **•** אישור שפג אינו ניתן לשימוש חוזר.

# 14. ניהול פוזיציה לאחר הכניסה

| **מצב**                 | **משמעות**                                |
|-------------------------|-------------------------------------------|
| OPENING                 | פקודת הכניסה פעילה אך טרם מולאה במלואה.   |
| OPEN                    | קיימת כמות מוחזקת.                        |
| PARTIALLY_EXITED        | חלק מהכמות נסגר.                          |
| EXIT_PENDING            | פקודת יציאה פעילה.                        |
| CLOSED                  | אין כמות פתוחה.                           |
| UNPROTECTED             | קיימת פוזיציה ללא הגנה נדרשת - מצב חירום. |
| RECONCILIATION_REQUIRED | אי התאמה מול הברוקר.                      |

> **•** Position Manager מקבל Market Events, Broker Events, Source Revisions, News ו-Risk Events.
>
> **•** כל שינוי Stop/Target יוצר Plan Revision ו-Broker Order Revision.
>
> **•** אין הזזת Stop אחורה כדי להגדיל הפסד מותר, אלא אם Strategy Version מגדירה מנגנון כזה במפורש ועברה אישור.
>
> **•** הגנות אצל הברוקר נבדקות לאחר כל Partial Fill או Partial Exit.
>
> **•** פוזיציה פתוחה אינה תלויה בכך שספק AI זמין כדי להפעיל Stop דטרמיניסטי.

# 15. מסגרת החלטות יציאה

## 15.1 Exit Triggers

| **Trigger**          | **משמעות**                                        |
|----------------------|---------------------------------------------------|
| PROTECTIVE_STOP      | מחיר/מבנה הוכיח שהעסקה לא עובדת.                  |
| TAKE_PROFIT          | יעד מלא או חלקי.                                  |
| TRAILING_STOP        | הגנה נגררת לפי כלל.                               |
| BREAK_EVEN_MOVE      | העברת Stop לנקודת איזון לפי Trigger.              |
| THESIS_INVALIDATED   | האירוע/העובדה/המבנה שעליהם התבססה התזה בוטלו.     |
| TIME_STOP            | העסקה לא התקדמה בחלון הזמן.                       |
| SOURCE_UPDATE        | עדכון מקור המפעיל בדיקה, לא בהכרח מכירה אוטומטית. |
| MATERIAL_NEWS        | מידע חדש מהותי.                                   |
| MARKET_REGIME_CHANGE | מצב שוק שמבטל את התאמת האסטרטגיה.                 |
| RISK_REDUCTION       | חשיפה, Drawdown או קורלציה.                       |
| SYSTEM_SAFETY        | כשל תפעולי לפי Runbook.                           |
| MANUAL_EXIT          | הוראה מפורשת של המשתמש.                           |

## 15.2 Exit Priority

| **עדיפות** | **סוג**           | **דוגמאות**                                                |
|------------|-------------------|------------------------------------------------------------|
| 1          | BROKER/SAFETY     | Protective stop, duplicate, missing protection, emergency. |
| 2          | HARD INVALIDATION | התזה אינה תקפה עוד.                                        |
| 3          | RISK              | Portfolio/Drawdown/Exposure.                               |
| 4          | PLANNED TARGETS   | Take profit, scale-out, trailing.                          |
| 5          | SOFT SIGNALS      | Source sentiment, model score, nonmaterial context.        |

## 15.3 Exit Decision Contract

> {
>
> "exit_decision_id": "exitdec\_...",
>
> "position_id": "pos\_...",
>
> "trigger_type": "THESIS_INVALIDATED",
>
> "action": "HOLD\|MOVE_STOP\|REDUCE\|TAKE_PROFIT\|CLOSE",
>
> "quantity_percent": 100,
>
> "order_type": "MARKET\|LIMIT\|STOP\|STOP_LIMIT",
>
> "urgency": "IMMEDIATE\|HIGH\|NORMAL",
>
> "reasons": \[\],
>
> "evidence_ids": \[\],
>
> "exit_policy_version": "exit.social.v0.1",
>
> "expires_at": "..."
>
> }

## 15.4 שינוי סנטימנט

| **ירידה בציון סנטימנט אינה מוכרת אוטומטית. היא יכולה ליצור Trigger להערכה. מכירה מתבצעת רק אם Exit Policy קושרת את השינוי לביטול תזה, Risk, מחיר או תנאי מאומת אחר.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 15.5 הודעות עדכון מהמקור

| **עדכון**          | **התנהגות**                                                              |
|--------------------|--------------------------------------------------------------------------|
| "להעלות סטופ ל-26" | Plan Revision; Validation מול מחיר וכמות; שינוי Broker Order לפי Policy. |
| "למכור חצי ב-28"   | Partial Target Revision.                                                 |
| "יצאתי"            | SOURCE_EXIT Trigger; ההחלטה אם לחקות תלויה ב-Strategy Policy.            |
| "טעיתי" / "בטל"    | Thesis Invalidation Candidate בעדיפות גבוהה.                             |
| מחיקת הודעה        | Revision/Tombstone; אינה מוחקת היסטוריה ומפעילה Review לפי Policy.       |

# 16. יעדים חלקיים, Trailing ו-Break-even

> **•** כל Target כולל Price/Condition ו-Quantity Percent.
>
> **•** סכום אחוזי היעדים אינו יכול לעלות על 100%.
>
> **•** לאחר Fill חלקי, Stops ו-Targets מותאמים לכמות שנותרה.
>
> **•** Break-even Move מופעל רק לאחר Trigger שנקבע מראש, לא מתוך פחד בזמן אמת.
>
> **•** Trailing יכול להיות Broker-native או System-managed; הסוג נשמר ב-Plan.
>
> **•** System-managed Trailing דורש Market Data ו-Execution Agent זמינים; כשל מחזיר ל-Broker-side protection לפי Policy.

# 17. Short Selling והחלטות כיוון

> **•** המסגרת תומכת LONG ו-SHORT ברמת Contracts, אך הפעלת SHORT ב-MVP היא החלטה פתוחה.
>
> **•** SHORT דורש Borrow/Availability, מגבלות ברוקר, Locate אם נדרש וסיכון Gap שונה.
>
> **•** Stop ו-Target מתהפכים מתמטית אך אינם משוכפלים אוטומטית ללא Strategy ייעודית.
>
> **•** הודעה שלילית אינה בהכרח הוראת SHORT; ייתכן שזו יציאה מ-LONG או דיון.

# 18. דירוג מקורות

| **מקור אינו מקבל ציון גלובלי יחיד. הוא יכול להיות טוב בזיהוי מניות טכנולוגיה לטווח יומיים וגרוע בתזמון ביוטק תוך-יומי. הדירוג הוא רב-ממדי ותלוי הקשר.** |
|---------------------------------------------------------------------------------------------------------------------------------------------------------|

| **מדד**              | **משמעות**                                           |
|----------------------|------------------------------------------------------|
| Directional Accuracy | דיוק כיוון מהמחיר בר-הביצוע.                         |
| Entry Quality        | MFE/MAE לאחר האות והאם הכניסה הייתה מאוחרת.          |
| Target Quality       | האם היעדים הושגו ובאיזה זמן.                         |
| Stop Quality         | האם ה-Stop הגן או היה לא סביר.                       |
| Expectancy           | תוחלת ב-R לאחר עלויות מדומות.                        |
| Time to MFE/MAE      | מהירות התממשות/כישלון.                               |
| Revision Behavior    | עדכונים, מחיקות ושינויי בדיעבד.                      |
| Coverage             | מספר המלצות ומספר דוגמאות מספק.                      |
| Context Dimensions   | Strategy, sector, market regime, horizon, direction. |

> **•** לא מעדכנים משקל אחרי מספר קטן של עסקאות.
>
> **•** מקור חדש מקבל Prior שמרני ו-Uncertainty גבוהה.
>
> **•** עריכות ומחיקות נשמרות; אין למדוד רק הודעות שנותרו.
>
> **•** מחיר ההשוואה הוא מחיר בר-ביצוע לאחר ההודעה, לא מחיר תיאורטי בדיעבד.

# 19. למידה ושיפור מבוקר

## 19.1 סוגי למידה

| **סוג**      | **מטרה**                                                              |
|--------------|-----------------------------------------------------------------------|
| Descriptive  | מה קרה: ביצועים לפי מקור, Strategy, Feature, זמן ומצב שוק.            |
| Diagnostic   | למה: Source, Data, Entity, Timing, Strategy, Risk או Execution error. |
| Predictive   | מודל הסתברותי לאחר מספיק נתונים ו-Validation.                         |
| Prescriptive | Change Proposal לגרסה חדשה; לא שינוי אוטומטי.                         |

## 19.2 תהליך שינוי

| **שלב** | **שם**   | **פעולה**                                                  |
|---------|----------|------------------------------------------------------------|
| 1       | Collect  | צבירת Decisions, Shadow Trades, Paper Trades ו-Rejections. |
| 2       | Label    | תוצאה, איכות החלטה, שגיאה ו-Market Regime.                 |
| 3       | Analyze  | Metrics, Segments, Calibration ו-Sensitivity.              |
| 4       | Propose  | Change Proposal עם Hypothesis ושינוי יחיד/מוגדר.           |
| 5       | Replay   | בדיקה על נתונים היסטוריים וחלונות שלא שימשו לבנייה.        |
| 6       | Shadow   | Champion/Challenger על אותם Events.                        |
| 7       | Promote  | קידום ידני לפי Acceptance Criteria.                        |
| 8       | Rollback | חזרה ל-Active Version קודמת אם נדרש.                       |

## 19.3 כללים נגד Overfitting

> **•** אין בחירת גרסה לפי רווח כולל בלבד.
>
> **•** נדרשת בדיקה Out-of-sample או Walk-forward כאשר יש מספיק נתונים.
>
> **•** נמדדים מספר ניסויים ושינויים כדי להבין Data Snooping.
>
> **•** שינוי אחד מרכזי בכל Challenger כאשר ניתן.
>
> **•** נדרשת יציבות בכמה תקופות/מצבי שוק, לא רק חודש אחרון.
>
> **•** מודל חדש אינו מחליף Hard Risk Rules.
>
> **•** אין העלאת משקל אוטומטית רק משום שמקור הצליח לאחרונה.

## 19.4 Calibration

> **•** Score Display אינו הסתברות.
>
> **•** Probability Estimate נשמר רק עבור מודל שעבר Calibration.
>
> **•** נבדקים Reliability Curve, Brier/Log Loss או Metrics מתאימים לפי סוג מודל.
>
> **•** Calibration Version נשמר עם כל החלטה.
>
> **•** שינוי Distribution או Market Regime יכול לדרוש Recalibration.

# 20. מדדי ביצוע

| **מדד**                 | **משמעות**                                            |
|-------------------------|-------------------------------------------------------|
| Net P&L                 | לאחר עמלות ו-Slippage מדומים/אמיתיים.                 |
| Expectancy in R         | תוחלת יחסית לסיכון.                                   |
| Profit Factor           | רווחים גולמיים חלקי הפסדים גולמיים.                   |
| Win Rate                | לצד Avg Win/Avg Loss; לא לבדו.                        |
| Max Drawdown            | לתיק, Strategy וגרסה.                                 |
| MFE/MAE                 | איכות כניסה ויציאה.                                   |
| Time in Trade           | לפי תזה ו-Horizon.                                    |
| Slippage/Latency        | Signal-to-decision, decision-to-order, order-to-fill. |
| Reject Opportunity Cost | מה קרה למועמדים שנדחו.                                |
| False Approvals         | עסקאות שאושרו למרות כשל ברור.                         |
| Data/Model Failure Rate | כמה Decisions נפגעו מתשתית.                           |
| Calibration             | התאמת הסתברות לתוצאות כאשר רלוונטי.                   |

# 21. איכות החלטה מול תוצאה

| **קטגוריה**                | **משמעות**                                 |
|----------------------------|--------------------------------------------|
| GOOD_DECISION_GOOD_OUTCOME | תהליך תקין ותוצאה חיובית.                  |
| GOOD_DECISION_BAD_OUTCOME  | תהליך תקין ותוצאה שלילית סטטיסטית.         |
| BAD_DECISION_GOOD_OUTCOME  | תהליך לקוי שהצליח במקרה.                   |
| BAD_DECISION_BAD_OUTCOME   | תהליך לקוי ותוצאה שלילית.                  |
| EXECUTION_ERROR            | תזה תקינה אך פקודה/Fill/Latency פגעו.      |
| DATA_ERROR                 | קלט שגוי, חסר, ישן או ממופה לנכס הלא נכון. |
| RISK_ERROR                 | Quantity, Stop או חשיפה לא תאמו Policy.    |

# 22. דוגמאות מקצה לקצה

## 22.1 הודעה מפורשת

> Source message:
>
> "XYZ קנייה באזור 24.80-25.00, יעד 28 ואז 30, סטופ 23.40"
>
> Extracted source plan:
>
> \- BUY_ZONE: 24.80-25.00
>
> \- TARGET 1: 28, partial
>
> \- TARGET 2: 30, remaining
>
> \- STOP_PRICE: 23.40
>
> System checks:
>
> \- instrument resolved
>
> \- price still inside/near zone
>
> \- liquidity and spread acceptable
>
> \- strategy eligible
>
> \- risk engine calculates quantity
>
> Possible result:
>
> APPROVE -\> PAPER_MANUAL -\> bracket/child orders after fill

## 22.2 הודעה שהגיעה מאוחר

> Source message:
>
> "קנייה ב-25, יעד 30"
>
> Current price at ingestion: 29.40
>
> Policy result:
>
> \- price extension too high
>
> \- reward remaining is insufficient
>
> \- max chase exceeded
>
> Outcome:
>
> REJECT or WATCH_FOR_PULLBACK, not MARKET_BUY

## 22.3 עסקה שלא עבדה

> Average fill: 25.00
>
> Approved stop: 24.00 (4% from AVG_FILL_PRICE)
>
> Price reaches stop trigger
>
> Action:
>
> \- broker-side protective stop executes
>
> \- position closes or reduces according to fill
>
> \- no new AI vote is required
>
> \- post-trade review classifies whether decision was good or bad

## 22.4 עדכון מקור

> Original source plan:
>
> Entry 25, target 30, stop 23.40
>
> New reply:
>
> "להעלות סטופ ל-26 ולמכור חצי ב-28"
>
> System behavior:
>
> \- link reply to original instruction
>
> \- create revision, preserve original
>
> \- validate against current position and market
>
> \- apply only if source-following policy allows
>
> \- update broker orders and audit all changes

# 23. Testing Strategy

| **סוג**           | **מטרה**                                                    |
|-------------------|-------------------------------------------------------------|
| Contract Tests    | StrategyEvaluation, TradePlan, ExitDecision ו-RiskDecision. |
| Golden Scenarios  | הודעות כניסה/יעד/Stop, עמימות, איחור ועדכונים.              |
| Replay Tests      | אותו Snapshot ו-Version מחזירים אותה החלטה.                 |
| Shadow Comparison | Champion/Challenger על אותם Candidates.                     |
| Risk Tests        | quantity, exposure, stop anchors, vetoes.                   |
| Exit Tests        | stop, partial target, trailing, source update, time exit.   |
| Broker Simulation | partial fills, gaps, reject, cancel, disconnect.            |
| Adversarial Text  | ציניות, ציטוט, שני טיקרים, שפה עמומה.                       |
| Data Quality      | stale, missing, conflict ו-provider down.                   |

# 24. קריטריוני קבלה

| **מזהה**  | **קריטריון**                                                           |
|-----------|------------------------------------------------------------------------|
| STR-AC-01 | המערכת מבדילה בין "לקנות ב-25" לבין "לקנות מעל 25".                    |
| STR-AC-02 | Stop באחוזים ללא Anchor נפסל.                                          |
| STR-AC-03 | Source Plan ו-System Plan נשמרים בנפרד.                                |
| STR-AC-04 | אות מאוחר שעבר Max Chase אינו הופך ל-Market Buy.                       |
| STR-AC-05 | שתי אסטרטגיות סותרות אינן ממוצעות אוטומטית.                            |
| STR-AC-06 | LLM אינו יכול ליצור OrderIntent ללא Decision ו-Risk.                   |
| STR-AC-07 | פוזיציה פתוחה ממשיכה לקבל Stop גם כאשר AI אינו זמין.                   |
| STR-AC-08 | Partial Exit מעדכן Stop ויעדים לכמות שנותרה.                           |
| STR-AC-09 | הודעת עדכון יוצרת Revision ולא מוחקת תוכנית מקורית.                    |
| STR-AC-10 | Source Sentiment לבדו אינו מכירה אוטומטית.                             |
| STR-AC-11 | כל שינוי Strategy/Policy יוצר Version חדשה.                            |
| STR-AC-12 | Champion ו-Challenger מקבלים אותם Events ו-Snapshots.                  |
| STR-AC-13 | Probability מוצגת רק לאחר Calibration.                                 |
| STR-AC-14 | Post-trade review מפריד איכות החלטה מתוצאה.                            |
| STR-AC-15 | כל Trade Plan כולל Entry, Invalidation, Exit, Expiry ו-Risk reference. |

# 25. החלטות פתוחות

| **מזהה**    | **החלטה**                                                 |
|-------------|-----------------------------------------------------------|
| STR-OPEN-01 | אילו משפחות אסטרטגיה ייכללו ב-MVP.                        |
| STR-OPEN-02 | האם SHORT ייכלל ב-MVP.                                    |
| STR-OPEN-03 | אילו סוגי Entry/Exit Orders יופעלו בתחילה.                |
| STR-OPEN-04 | Risk Fraction ומגבלות חשיפה ראשוניות.                     |
| STR-OPEN-05 | Max Chase Policies לפי Strategy.                          |
| STR-OPEN-06 | האם Source Updates משנים פקודות אוטומטית או דורשים אישור. |
| STR-OPEN-07 | כללי Scale-in ו-Scale-out.                                |
| STR-OPEN-08 | מינימום נתונים לקידום מ-Shadow ל-Paper Auto.              |
| STR-OPEN-09 | Evaluator הראשוני לכל Strategy: Rules, Score או Hybrid.   |
| STR-OPEN-10 | מדדי Calibration ו-Validation למודלים עתידיים.            |
| STR-OPEN-11 | מדיניות החזקה דרך Earnings/Overnight.                     |
| STR-OPEN-12 | Universe ראשוני: מניות, ETF, מחיר מינימום ונזילות.        |

# 26. השלב הבא

> 1\. MVP Scope v0.6 - בחירת האסטרטגיות, המקורות, המסכים והיכולות של הגרסה הראשונה.
>
> 2\. Technology and Deployment Decisions v0.6 - בחירת Stack, Queue, Storage, Monitoring ו-IBKR baseline.
>
> **3.** Consolidated Review - בדיקת סתירות וחוסרים בין כל המסמכים.
>
> **4.** Master Product Requirements Document - איחוד ההחלטות למסמך מוצר אחד.
>
> **5.** Claude Code Implementation Plan - רק לאחר השלמת ואישור כל האפיון.

# 30. Baseline אסטרטגיה והחלטה מתוקן

| **הפרק מפריד בין משפחת אסטרטגיה ל-ID, מאחד Entry Types, מגדיר Confidence מכויל ו-Single Writer לפוזיציה.** |
|------------------------------------------------------------------------------------------------------------|

## 30.1 Taxonomy

| **Strategy ID**              | **Family**  | **MVP State**                          |
|------------------------------|-------------|----------------------------------------|
| social.explicit_plan         | SOCIAL      | Core; מועמד ראשון ל-Paper Auto.        |
| social.callout_confirmation  | SOCIAL      | Shadow/Paper Manual.                   |
| news.catalyst_momentum       | NEWS_EVENT  | Shadow/Manual עד Provider ו-Benchmark. |
| exit.position_management     | EXIT        | Core; Rules/Policy.                    |
| technical.breakout           | TECHNICAL   | Deferred standalone strategy.          |
| technical.swing_continuation | TECHNICAL   | Deferred.                              |
| technical.mean_reversion     | TECHNICAL   | Deferred.                              |
| event.earnings               | EVENT       | Deferred.                              |
| fundamental.value            | FUNDAMENTAL | Deferred.                              |

## 30.2 Strategy Evaluator Contract ב-Python

> class StrategyEvaluator(Protocol):
>
> strategy_id: str
>
> strategy_version: str
>
> async def eligibility(self, context: EvaluationContext) -\> EligibilityResult: ...
>
> async def evaluate(self, snapshot: FeatureSnapshot) -\> StrategyEvaluation: ...
>
> async def build_trade_plan(self, evaluation: StrategyEvaluation) -\> ProposedTradePlan: ...
>
> **•** JSON Schema/Pydantic contracts הם החוזה חוצה-השפות; TypeScript אינו מגדיר את ליבת האסטרטגיה.

## 30.3 Risk Formula

> risk_budget_equity = min(broker_nlv, simulation_equity_cap)
>
> risk_amount = risk_budget_equity \* risk_fraction
>
> risk_per_unit = abs(entry_reference - stop) + estimated_round_trip_cost
>
> quantity = floor(min(risk_amount/risk_per_unit, liquidity_limit, exposure_limit, buying_power_limit))
>
> **•** מניות שלמות בלבד; Strategy אינה קובעת Quantity סופית.

## 30.4 Confidence

> **•** model_reported_confidence נשמר לצורכי Telemetry.
>
> **•** calibrated_confidence נגזר מ-Golden Set/Benchmark גרסאי.
>
> **•** Auto דורש CALIBRATED; HEURISTIC מותר ל-Shadow/Manual בלבד.
>
> **•** Score אסטרטגי אינו מוצג כהסתברות.

## 30.5 Source Score Point-in-Time

> **•** Evaluation מקבלת source_score_snapshot_id שהיה בתוקף בזמן האות.
>
> **•** עדכון מאוחר של אמינות מקור אינו משנה Decision היסטורי.
>
> **•** Replay משתמש ב-Snapshot As-of בלבד.
>
> **•** מקור UNRATED אינו מקבל בונוס חיובי.

## 30.6 Final Pre-Submit Check

> **•** Strategy Evaluation אינה הרשאת Submit.
>
> **•** לאחר Approval מתבצע Refresh ו-Risk סופי.
>
> **•** אם R/R, Max Chase, Spread, Session או Exposure השתנו — REJECT/REDUCE/REFRESH.
>
> **•** SubmitAuthorization חד-פעמי מקשר Decision, Risk, Approval ו-Snapshots.

## 30.7 Exit Coordinator

> **•** Stop/Target/Time/Thesis/Source/Manual/Risk triggers ממוינים לפי Priority Policy.
>
> **•** ה-Coordinator מחליט Command יחיד ביחס ל-position generation.
>
> **•** אין הרחבת Stop אוטומטית.
>
> **•** Corporate Action או Broker mismatch מעבירים ל-RECONCILIATION_REQUIRED ולא ל-LLM.

## 30.8 Evidence Standards

> **•** Operational Gate מודד בטיחות ודיוק תפעולי.
>
> **•** Statistical Evidence מודד Expectancy, CI, Regime coverage ו-stability.
>
> **•** מספר עסקאות מינימלי אינו לבדו הוכחת Alpha.
>
> **•** קידום Live מחייב את שני סוגי הראיות ואישור אנושי.

# 31. מסגרת אסטרטגיות רב-מודלית

| **אסטרטגיה אינה שווה מקור. Strategy Definition מגדירה שילוב של Assessments ממנועים שונים. Social, TradingView או News יכולים ליצור Candidate, אך ההחלטה נשענת על Manifest, נתונים פנימיים ו-Risk.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 31.1 משפחות אסטרטגיה נוספות

| **Strategy ID**                   | **תפקיד**                                                              | **MVP**                                        |
|-----------------------------------|------------------------------------------------------------------------|------------------------------------------------|
| technical.breakout_news_guard     | טריגר פריצה/נפח עם News/NLP veto.                                      | Core candidate ראשון ל-Paper Auto.             |
| techno_fundamental.breakout       | Fundamental watchlist + technical entry.                               | Core Shadow/Manual; Auto לאחר data validation. |
| fundamental.catalyst_confirmation | איכות/שווי + catalyst + technical confirmation.                        | Shadow/Manual.                                 |
| technical.regime_filtered         | Technical setup active only in compatible ML regime.                   | ML in Shadow until calibrated.                 |
| social.independent_confirmation   | Social idea requires independent market/news/fundamental confirmation. | Optional; not system dependency.               |
| ensemble.multi_signal             | Manifest-defined required/optional/veto evidence.                      | Shadow research.                               |
| quant.pairs_trading               | Statistical arbitrage with long/short legs.                            | Roadmap; out of MVP.                           |

## 31.2 Fusion Manifest

> strategy:
>
> id: technical.breakout_news_guard
>
> version: 1.0.0
>
> required:
>
> \- assessment: technical.breakout
>
> condition: score \>= 75
>
> \- assessment: market.liquidity
>
> condition: eligible == true
>
> optional:
>
> \- assessment: fundamental.quality
>
> \- assessment: sector.relative_strength
>
> veto:
>
> \- assessment: news.material_negative
>
> \- assessment: market.regime_risk_off
>
> \- assessment: data_quality_stale
>
> trigger_owner: technical.breakout
>
> exit_policy: breakout_exit_v1

## 31.3 Techno-Fundamental

> **•** Fundamental Engine יוצר ומעדכן Watchlist על בסיס Quality, Growth, Valuation ו-Financial Risk.
>
> **•** Technical Engine מפעיל Entry Trigger רק כאשר membership תקפה בזמן ההחלטה.
>
> **•** News/NLP ו-Corporate Actions יכולים לחסום או להפחית Risk.
>
> **•** אין להשתמש ב-Fundamental Snapshot שפורסם לאחר מועד ההחלטה ב-Replay.

## 31.4 Technical + Sentiment

> **•** Sentiment כללי לבדו אינו Veto; נדרשים Materiality, Direct Relevance, Novelty ו-Source Class.
>
> **•** Negative official catalyst הוא Veto חזק יותר מריבוי כותרות ממוחזרות.
>
> **•** היעדר חדשות אינו מתורגם אוטומטית ל-Neutral evidence.

## 31.5 Quant/ML

> **•** מודל ML יכול לדרג Candidates, לסווג Regime או להעריך Target-before-Stop רק עם calibration status מאושר.
>
> **•** Training, feature selection ו-hyperparameter search מבוצעים Offline בלבד.
>
> **•** מודל אינו מתאים את הפרמטרים בזמן אמת ואינו משנה Risk Policy.
>
> **•** כל prediction כולל feature schema, model version, training cutoff ו-probability calibration.

## 31.6 Conflict Resolution

| **מצב**                               | **Outcome**                            |
|---------------------------------------|----------------------------------------|
| Required assessment missing           | WATCH/INCONCLUSIVE; לא APPROVE.        |
| Hard veto triggered                   | REJECT או EXIT לפי stage.              |
| Optional model unavailable            | המשך רק אם Manifest מאפשר degradation. |
| Technical and fundamental conflict    | לפי Strategy purpose; אין ממוצע סמוי.  |
| ML disagrees with deterministic gates | Hard gates גוברים.                     |

# **28. Strategy Manifest גמיש ואינו דורש הסכמה מלאה**

> inputs:
>
> required: \[technical.breakout, market_data.fresh\]
>
> soft_score: \[fundamental.quality, sector.strength\]
>
> veto: \[official_negative_catalyst, risk.halt\]
>
> bonus: \[tradingview.alert, trusted_social_confirmation\]
>
> context: \[analyst_revision, sentiment_summary\]
>
> action_tiers:
>
> auto_full: score \>= 85
>
> auto_reduced: score \>= 78
>
> manual: score \>= 68
>
> shadow: score \>= 55
>
> **•** אין Gate גלובלי המחייב Technical, Fundamental, News, ML, External ו-Social יחד.
>
> **•** כל אסטרטגיה מגדירה אילו משפחות אותות הן REQUIRED, VETO, SOFT_SCORE, BONUS או CONTEXT.
>
> **•** מודל חסר גורם ל-DEGRADED/Manual רק כאשר הוא Required או כאשר ה-Policy קובעת זאת.
>
> **•** Regime compatibility ו-Portfolio capacity נבדקים לפני Risk sizing.
>
> **•** אותות שהם העתק של אותו אירוע אינם מקבלים משקל עצמאי.

# **29. Opportunity Calibration ו-Counterfactual Shadow**

> **•** כל Gate מדווח pass/fail/score וההשפעה המדויקת על Action Tier.
>
> **•** Candidates שלא בוצעו ממשיכים להימדד כאשר ניתן להגדיר Entry, Stop ו-Target ללא מידע עתידי.
>
> **•** Policy Starvation מייצר Alert ו-Change Proposal בלבד; אינו משחרר Hard Veto.
>
> **•** Champion ו-Challenger מקבלים אותם Events ו-Snapshots.
>
> **•** אין שינוי משקל, סף או מודל פעיל ללא Replay, Shadow, Review ו-Promotion אנושי.

# **30. הקצאת הון ומשטר שוק**

> **•** Strategy Manifest מצהיר compatible_regimes, max_portfolio_risk, sector limits ו-conflict rules.
>
> **•** Portfolio Engine יכול להקטין כמות, לדחות Candidate כפול או לבחור אסטרטגיה אחת בלבד לאותו Symbol.
>
> **•** אין כפיית עסקאות כאשר שום אסטרטגיה אינה מתאימה למשטר הנוכחי.

# **54. Social Source Capability vs Strategy Dependency**

| **חיבורי המשתמש ל-WhatsApp, Discord ו-Telegram הם יכולת מערכת חובה. עם זאת, כל Strategy Manifest קובע בעצמו האם מקור חברתי הוא REQUIRED, SOFT, BONUS או CONTEXT. אין Gate חברתי גלובלי.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

> **•** User/Bot access_mode אינו משפיע לבדו על Direction או Score.
>
> **•** אות כפול מ-Telegram User ו-Bot מאוחד לפני Fusion.
>
> **•** אסטרטגיה שאינה חברתית ממשיכה לפעול כאשר כל מקורות Social מנותקים.
>
> **•** אסטרטגיה חברתית התלויה במקור מסוים עוברת WATCH/SHADOW כאשר אותו Source Profile אינו תקין; היא אינה מנחשת נתונים.
