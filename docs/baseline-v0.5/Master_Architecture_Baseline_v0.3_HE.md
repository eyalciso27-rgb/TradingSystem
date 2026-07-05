תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

| **מסמך זה אינו מחליף את המפרטים המפורטים. הוא קובע קדימות, גבולות, Invariants ומצב מוכנות כדי למנוע פרשנויות שונות בזמן היישום.** |
|-----------------------------------------------------------------------------------------------------------------------------------|

# **1. הגדרת המוצר**

מערכת מסחר אוטונומית רב-מודלית לגילוי, ניתוח, שילוב אותות, החלטה, סיכון, ביצוע וניהול פוזיציות. Social ו-External Signals הם מקורות אופציונליים; המערכת מסוגלת לגלות Candidates מנתוני שוק, פונדמנטל, חדשות ואירועים ללא תלות בהם.

# **2. סדר קדימות מסמכים**

| **קדימות** | **מסמך/סוג**                                        |
|------------|-----------------------------------------------------|
| 1          | Master Architecture Baseline + Decision Contract    |
| 2          | Security and Operations + Policy Parameters         |
| 3          | Strategy/Fusion + Portfolio/Model Governance/Trust  |
| 4          | System Functional + Data + Integration + Technology |
| 5          | MVP + Operations UX                                 |
| 6          | Traceability Matrix; מפנה למקור הסמכות              |

# **3. Invariants מחייבים**

> **•** Risk Engine ו-Safety Gates אינם ניתנים לעקיפה.
>
> **•** AI אינו מחזיק הרשאת Broker Submit.
>
> **•** IBKR הוא Adapter ראשון בלבד.
>
> **•** Paper ו-Live מבודדים.
>
> **•** אין Order בלי Decision, Portfolio/Risk checks ו-idempotency.
>
> **•** אין Auto position ללא protection שנדרש.
>
> **•** אין blind retry במצב Order לא ידוע.
>
> **•** אין Look-ahead ב-Replay/Backtest.
>
> **•** אין forced trades ואין Auto relaxation עקב Starvation.
>
> **•** אין Auto-promotion של Model, Strategy או Policy.
>
> **•** GLOBAL ENTRY HALT זמין בערוץ ראשי ונפרד.
>
> **•** כל Evidence, Model, Policy ו-Decision גרסאיים וניתנים לשחזור.

# **4. מפת ארכיטקטורה**

> Sources / Internet / Market Data / Official Filings / External / Social
>
> -\> Trust + Provenance + Normalization
>
> -\> Candidate Discovery
>
> -\> Technical \| Fundamental \| News/NLP \| Quant/ML \| External \| Social Assessments
>
> -\> Signal Fusion
>
> -\> Strategy Decision
>
> -\> Portfolio + Market Regime
>
> -\> Risk + Final Pre-Submit
>
> -\> Execution Port -\> IBKR Adapter / future broker
>
> -\> Reconciliation -\> Position/Exit Coordinator
>
> -\> Analytics / Calibration / Research / Governance
>
> -\> Operations Console + Alerts + Emergency Control

# **5. Action Model**

| **Tier**     | **משמעות**                        |
|--------------|-----------------------------------|
| AUTO_FULL    | ביצוע אוטומטי לפי Risk מלא מאושר. |
| AUTO_REDUCED | ביצוע אוטומטי בכמות מופחתת.       |
| MANUAL       | אישור אדם.                        |
| SHADOW       | מדידה ללא פקודה.                  |
| WATCH        | ממתין.                            |
| REJECT       | נדחה.                             |
| HALT         | אין כניסות חדשות.                 |

# **6. MVP Baseline**

> **•** US stocks/ETFs, Long, whole shares, RTH entries.
>
> **•** Autonomous technical discovery + fundamental watchlist + news/catalyst.
>
> • TradingView הוא Adapter אופציונלי; חיבורי User ל-WhatsApp, Discord ו-Telegram הם יכולת חובה, אף שזמינותם אינה Gate גלובלי בזמן ריצה.
>
> **•** IBKR Paper בלבד בתחילת הביצוע.
>
> **•** Transparent strategies and heuristic regime active; ML predictive models Shadow first.
>
> **•** Opportunity funnel, rejected shadow, portfolio limits, model registry, trust layer ו-emergency control.

# **7. Readiness Gates**

> **•** כל מסמכי החבילה קיימים בגרסה המופיעה באינדקס.
>
> **•** Traceability Matrix הושלמה.
>
> **•** PoI tasks מוגדרות ל-IBKR, sources, market data, ARM stack ו-session store.
>
> **•** החלטות פתוחות אינן מוסתרות בקוד.
>
> **•** Baseline review נוסף יאמת שאין סתירות.
>
> **•** המשתמש מאשר במפורש לפני Claude Code Implementation Plan.

# **8. החלטות פתוחות שאינן חוסמות אפיון**

> **•** ספק Market Data ו-News לאחר PoI.
>
> **•** Simulation Equity Cap לפני Paper Manual.
>
> **•** AI provider/model לכל Task לאחר Benchmark.
>
> **•** ערכי Thresholds לאחר Replay/Shadow data.
>
> **•** מיקום Execution Agent לאחר בדיקת IBKR connectivity.

# **9. סטטוס**

| **חבילת v0.3 היא Baseline Candidate מלאה מבחינת תחומי מוצר וארכיטקטורה. היא עדיין אינה הוראת יישום. השלב הבא הוא Baseline Consistency Review קצר ולאחריו Implementation Plan ו-PoI backlog.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# **10. Telegram Baseline Decision**

> • Telegram הוא מקור Runtime אופציונלי תחת Social Signals, אך USER_ACCOUNT_TDLIB הוא Adapter חובה ועיקרי.
>
> **•** שני modes נתמכים ארכיטקטונית: Bot API ו-user account via TDLib.
>
> **•** ה-Adapter הוא read-only לצורכי מסחר; Alerts פרטיים מותרים דרך Notification Service נפרד.
>
> **•** אין Telegram-to-Broker או Telegram-to-Kill-Switch path.
>
> **•** Telegram DOWN אינו משנה את מצב המערכת הגלובלי אלא אם Strategy פעילה תלויה בו במפורש.
>
> **•** Telegram PoI נוסף ל-Readiness Gates לפני קיבוע Runtime ו-Image.

# **11. החלטת Baseline סופית: Social User Access**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **Capability**            | **Baseline**                      |
|---------------------------|-----------------------------------|
| WhatsApp User             | חובה למימוש ול-PoI.               |
| Discord User              | חובה למימוש ול-PoI.               |
| Telegram User via TDLib   | חובה, עיקרי וללא תחליף.           |
| Telegram Bot API          | אפשרות נוספת ומשלימה.             |
| Core autonomous operation | אינו תלוי בזמינות Social גלובלית. |
