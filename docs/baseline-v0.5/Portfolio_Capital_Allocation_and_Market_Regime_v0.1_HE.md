תאריך: 5 ביולי 2026

סטטוס: Baseline Candidate פנימי; טרם נמסר ל-Claude Code

| **Strategy טובה ברמת מניה יכולה להיות עסקה רעה ברמת התיק אם היא מכפילה סקטור, מקור או סיכון שכבר קיים. Portfolio Engine פועל לאחר Fusion ולפני Risk sizing סופי.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# **1. מטרות**

> **•** לנהל Risk Budgets בין אסטרטגיות ומודלים.
>
> **•** למנוע כפילויות ופוזיציות סותרות.
>
> **•** למדוד חשיפה לסקטור, Theme ו-Correlation Cluster.
>
> **•** להפעיל אסטרטגיות בהתאם למשטר שוק.
>
> **•** לשמור על Cash/Capacity ולא לכפות שימוש בהון.

# **2. Portfolio State**

| **מדד**                  | **תיאור**                         |
|--------------------------|-----------------------------------|
| NAV / Risk Budget Equity | הון ייחוס קנוני.                  |
| Open Risk                | הפסד מתוכנן עד Stops.             |
| Gross/Net Notional       | חשיפה כספית.                      |
| Cash/Buying Power        | יכולת ביצוע.                      |
| Sector/Theme Exposure    | חשיפה מצטברת.                     |
| Correlation Exposure     | אשכולות נכסים דומים.              |
| Strategy Allocation      | Risk budget ו-usage לכל Strategy. |
| Source Concentration     | תלות במקור/ערוץ/מודל.             |

# **3. Capital Allocation**

> **•** כל Strategy מקבלת risk budget share, max open positions ו-priority.
>
> **•** Unused budget אינו מועבר אוטומטית לאסטרטגיה אחרת ללא Policy.
>
> **•** Portfolio Engine יכול להקטין Quantity אך לא להגדיל מעבר ל-Risk Engine.
>
> **•** Cash reserve ו-emergency capacity נשמרים לפי Policy.

# **4. Conflict and Duplicate Resolution**

| **תרחיש**                             | **תגובה**                                                |
|---------------------------------------|----------------------------------------------------------|
| שתי אסטרטגיות Long לאותו Symbol       | בחירת Winner או מיזוג Evidence; Position אחת בלבד ב-MVP. |
| אות Long מול Exit/Negative thesis     | Exit/Veto גובר; אין כניסה חדשה.                          |
| אותות מאותו News Cluster              | אינם נחשבים אישורים עצמאיים.                             |
| שני Symbols באותו Correlation Cluster | הקטנת Quantity או דחייה לפי cap.                         |
| פוזיציה קיימת עם Update               | Position Coordinator מחליט; אין Order מקביל.             |

# **5. Market Regime Engine**

| **Regime**       | **מאפיינים**                              | **השפעה**                             |
|------------------|-------------------------------------------|---------------------------------------|
| TRENDING_RISK_ON | מדדים, breadth ו-sector momentum חיוביים. | Breakout/continuation פעילים יותר.    |
| RANGE_BOUND      | מגמה חלשה ו-reversion.                    | Breakout מופחת; mean-reversion עתידי. |
| HIGH_VOLATILITY  | ATR/VIX-like measures ו-gaps גבוהים.      | Risk קטן, liquidity gates גבוהים.     |
| RISK_OFF         | breadth שלילי, correlation עולה.          | Entry budgets מצטמצמים או Halt.       |
| EVENT_STRESS     | מאקרו/חדשות מערכתיות.                     | Manual/Shadow לפי Policy.             |
| UNKNOWN          | נתונים חסרים או Confidence נמוך.          | Reduced/Manual; לא מניחים מצב רגיל.   |

# **6. Regime Contract**

> {
>
> "regime": "TRENDING_RISK_ON",
>
> "confidence_state": "HEURISTIC\|CALIBRATED",
>
> "score": 0.81,
>
> "observed_at": "...",
>
> "valid_until": "...",
>
> "evidence": \[...\],
>
> "model_version": "regime.heuristic.v1"
>
> }

# **7. Events and Calendars**

> **•** Earnings, splits, dividends, major macro releases ו-market holidays נשמרים כ-Calendar Events.
>
> **•** Strategy מגדירה האם אירוע הוא VETO, SOFT_SCORE או CONTEXT.
>
> **•** ימים מקוצרים ו-DST מגיעים מ-Trading Calendar סמכותי.
>
> **•** Corporate action unresolved גורם Symbol Halt עד Reconciliation.

# **8. Portfolio Pre-Check**

> Candidate + Proposed Trade Plan
>
> -\> existing positions/orders
>
> -\> strategy budget
>
> -\> symbol/sector/theme/correlation caps
>
> -\> regime compatibility
>
> -\> portfolio action: ALLOW \| REDUCE \| MANUAL \| REJECT

# **9. Monitoring and UI**

> **•** Portfolio Cockpit מציג exposure, open risk, capacity ו-reasons for reduction.
>
> **•** Regime panel מציג history ו-compatible strategies.
>
> **•** Alerts מופקים עבור concentration, correlation spike, budget exhaustion ו-regime uncertainty.

# **10. Acceptance Criteria**

> **•** אין שתי פוזיציות עצמאיות לאותו Symbol ב-MVP.
>
> **•** סכום Budgets אינו חורג מ-Safety Envelope.
>
> **•** כל Quantity reduction מוסברת ומבוססת Policy version.
>
> **•** UNKNOWN regime אינו מטופל כ-NORMAL.
>
> **•** Corporate action unresolved חוסם כניסה חדשה.
