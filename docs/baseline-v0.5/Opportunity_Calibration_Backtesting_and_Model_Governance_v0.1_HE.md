תאריך: 5 ביולי 2026

סטטוס: Baseline Candidate פנימי; טרם נמסר ל-Claude Code

| **המערכת אינה נדרשת לבצע מספר מינימלי של עסקאות. היא נדרשת למדוד האם אין עסקאות משום שאין הזדמנויות תקפות, משום שהשוק אינו מתאים, משום שחסר מידע או משום שה-Policy קשיחה מדי.** |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# **1. מטרות**

> **•** למדוד את משפך ההזדמנויות מקליטה ועד ביצוע.
>
> **•** לזהות Policy Starvation ו-Overtrading בלי לעקוף בטיחות.
>
> **•** להמשיך למדוד Candidates שלא בוצעו באמצעות Counterfactual Shadow.
>
> **•** להריץ Replay, Backtest ו-Champion/Challenger ללא Look-ahead.
>
> **•** לנהל Dataset, Model, Calibration ו-Promotion באופן גרסאי.

# **2. Taxonomy של Gates**

| **קטגוריה**       | **האם יכולה לחסום Auto** | **האם ניתן להקל אוטומטית** |
|-------------------|--------------------------|----------------------------|
| SAFETY_HARD       | כן, תמיד                 | לא                         |
| STRATEGY_REQUIRED | כן, באסטרטגיה המסוימת    | לא; רק גרסה חדשה           |
| SOFT_SCORE        | לא בהכרח                 | לא אוטומטית; What-if בלבד  |
| BONUS             | לא                       | לא רלוונטי                 |
| CONTEXT           | לא                       | לא רלוונטי                 |

# **3. Opportunity Funnel**

> SCANNED -\> DISCOVERED -\> DATA_ELIGIBLE -\> STRATEGY_ELIGIBLE
>
> -\> FUSION_EVALUATED -\> PORTFOLIO_ELIGIBLE -\> RISK_ELIGIBLE
>
> -\> AUTO_FULL \| AUTO_REDUCED \| MANUAL \| SHADOW \| WATCH \| REJECT
>
> **•** כל מעבר נשמר עם reason_code, gate_id, policy_version ו-timestamp.
>
> **•** Gate concentration נמדד בנפרד עבור Safety לעומת איכות.
>
> **•** מוצגים absolute counts וגם rates כדי למנוע מסקנות מטעות.

# **4. Policy Starvation**

| **מצב**           | **זיהוי**                                          | **תגובה**                               |
|-------------------|----------------------------------------------------|-----------------------------------------|
| No opportunity    | מעט Candidates עוברים Data/Safety eligibility.     | אין שינוי Policy; ניתוח Scanner/Regime. |
| Data starvation   | הרבה Candidates נכשלים עקב מקור חסר/ישן.           | Alert תפעולי ותיקון Provider.           |
| Policy starvation | Candidates איכותיים רבים נעצרים ב-Gate לא-בטיחותי. | What-if, Challenger ו-Change Proposal.  |
| Regime mismatch   | אסטרטגיות אינן תואמות למשטר.                       | הפעלת אסטרטגיות מתאימות או המתנה.       |
| Overtrading       | קצב חריג, turnover או repeated signals.            | Budget reduction/Halt לפי Policy.       |

# **5. Counterfactual Shadow**

> **•** נוצר רק כאשר Entry, Stop, Target/Exit Policy ו-validity היו ידועים בזמן אמת.
>
> **•** Fill simulation משתמשת בנתונים Point-in-Time ובמודל Slippage גרסאי.
>
> **•** אין להשלים תנאים בדיעבד באמצעות מידע שלא היה זמין.
>
> **•** תוצאות מסומנות COUNTERFACTUAL ואינן מתערבבות עם Paper/Live P&L.

# **6. Backtesting and Simulation**

| **שכבה**        | **דרישה**                                                             |
|-----------------|-----------------------------------------------------------------------|
| Data snapshot   | Hash, sources, time range, corporate-action treatment ו-as_of.        |
| Split           | Train/validation/test או in-sample/out-of-sample לפי סוג מודל.        |
| Costs           | Spread, commissions, slippage, borrow בעתיד ו-rejected/partial fills. |
| Latency         | Signal-to-decision-to-order delays לפי Profile.                       |
| Execution model | Marketable limit, stop activation, gap ו-liquidity caps.              |
| Reproducibility | Seed, code commit, config versions, runtime ו-artifact hashes.        |

# **7. Model Registry and Lifecycle**

> DRAFT -\> OFFLINE_VALIDATED -\> REPLAY -\> SHADOW_CHALLENGER
>
> -\> PAPER_MANUAL -\> PAPER_LIMITED_AUTO -\> RETIRED
>
> **•** אין Online Learning או Auto-Retrain ב-MVP.
>
> **•** Model artifact, feature schema, training data snapshot ו-metrics נשמרים יחד.
>
> **•** Model card מתאר מטרה, limitations, horizons, regimes, required inputs ו-failure modes.
>
> **•** Promotion ו-Rollback הם פעולות אנושיות מתועדות.

# **8. Calibration and Evaluation**

| **סוג**          | **מדדים**                                                     |
|------------------|---------------------------------------------------------------|
| Classification   | precision, recall, F1, PR-AUC, calibration error.             |
| Trading decision | target-before-stop, expectancy, drawdown, turnover, coverage. |
| Execution        | fill rate, slippage, partial fills, latency, unknown states.  |
| Opportunity      | false rejection, gate concentration, starvation, overtrading. |
| Robustness       | regime slices, sectors, liquidity buckets, source outages.    |

# **9. Drift and Monitoring**

> **•** Feature drift, prediction drift, calibration drift ו-outcome drift נמדדים בנפרד.
>
> **•** Drift אינו גורם לאימון אוטומטי; הוא יכול לגרום ל-DEGRADED, SHADOW או Demotion.
>
> **•** מוצגים data volume, missingness ו-provider changes כדי להבחין בין Model drift ל-Data drift.

# **10. Champion/Challenger**

> **•** Champion ו-Challenger מקבלים אותם Events, snapshots ו-deadlines.
>
> **•** השוואה כוללת Coverage, Risk, drawdown ו-operational failures, לא רק P&L.
>
> **•** Challenger אינו מקבל גישה לביצוע לפני Gate ואישור.
>
> **•** מספר שינויים בבת אחת דורש Change Set ברור ו-ablation.

# **11. Acceptance Criteria**

> **•** 100% מה-Candidates ניתנים למיקום בשלב Funnel.
>
> **•** 100% מה-Policies וה-Models בהחלטה מצביעים לגרסה קבועה.
>
> **•** Backtest זהה חוזר לאותה תוצאה במסגרת tolerance מוגדר.
>
> **•** אין Look-ahead ב-As-of audit.
>
> **•** Starvation alert אינו משנה Active Policy.
>
> **•** Counterfactual results מופרדים מעסקאות אמיתיות.
