תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

# **1. מסמכי החבילה**

| **\#** | **מסמך**                                                        | **תפקיד**                                      |
|--------|-----------------------------------------------------------------|------------------------------------------------|
| 1      | Master Architecture Baseline v0.3                               | הגדרה, קדימות ו-Invariants.                    |
| 2      | Decision Contract v0.8                                          | חוזי החלטה, Gates, Action tiers וחירום.        |
| 3      | System Functional Specification v0.7                            | יכולות ותהליכים מקצה לקצה.                     |
| 4      | Data Model and Retention v0.6                                   | ישויות, Point-in-Time, Governance ו-Audit.     |
| 5      | Integration Specification v0.6                                  | Adapters, Sources, Providers ו-Degraded modes. |
| 6      | Security and Operations v0.6                                    | אבטחה, Trust, Sessions, DR ו-Kill Switch.      |
| 7      | Strategy and Decision Framework v0.5                            | אסטרטגיות, Fusion roles, Risk ו-Exit.          |
| 8      | MVP Scope v0.6                                                  | גבולות ו-Phases.                               |
| 9      | Technology and Deployment v0.6                                  | Stack, modules ו-deployment.                   |
| 10     | Policy Parameters v0.5                                          | ספים, risk, starvation ו-regime.               |
| 11     | Multi-Model Research, Signals and Fusion v0.4                   | ששת מנועי הניתוח ושילוב אותות.                 |
| 12     | Operations Console and UX v0.4                                  | Dashboard, research, portfolio וחירום.         |
| 13     | Opportunity, Calibration, Backtesting and Model Governance v0.1 | מחקר, קיפאון, Counterfactual ומודלים.          |
| 14     | Portfolio, Capital Allocation and Market Regime v0.1            | הקצאת הון ותנאי שוק.                           |
| 15     | Trust, Adversarial Input and Market Manipulation Controls v0.3  | שרשרת אמון וקלט עוין.                          |
| 16     | Traceability Matrix v0.3                                        | Requirement -\> Component -\> Data -\> Test.   |

# **2. הכרעות סופיות בחבילה**

> **•** המערכת אוטונומית ואינה תלויה ב-Social או TradingView.
>
> **•** המערכת אינה קשיחה בינארית: Hard Safety נפרד מ-Soft/Bonus/Context.
>
> **•** אין forced trades; קיימת מדידה שיטתית של Starvation ו-False Rejection.
>
> **•** IBKR הוא ברוקר ראשון בלבד.
>
> **•** קיימים Portfolio, Regime, Research, Model Governance ו-Trust controls.
>
> **•** ממשק מלא וכפתור חירום ראשי ונפרד מוגדרים.

# **3. השלב הבא**

> **•** Baseline Consistency Review סופי.
>
> **•** Implementation Plan ו-PoI backlog מפורט.
>
> **•** רק לאחר אישור מפורש - מסירת Markdown ל-Claude Code.

# **4. הרחבת Telegram**

> • Telegram נוסף כמקור Social שזמינותו אינה תנאי גלובלי; חיבור באמצעות חשבון המשתמש הוא יכולת חובה.
>
> • חיבור Telegram באמצעות חשבון המשתמש ו-TDLib הוא חובה ועיקרי; Bot API הוא אפשרות שנייה ומשלימה בלבד.
>
> **•** החיבור קריאה בלבד לצורכי מסחר, עם Allowlist, Trust Pipeline ו-Session מוצפן.
>
> **•** התראות פרטיות לבעל המערכת נתמכות; פקודות מסחר וחירום דרך Telegram אינן נתמכות ב-MVP.

# **5. Baseline v0.5 - הכרעה מחייבת**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **5.1 מסמכי מסירה חדשים**

> **•** Baseline Consistency Review v0.1
>
> **•** Proof-of-Integration Backlog v0.1
>
> **•** Claude Code Implementation Plan v0.1
>
> **•** POI_BACKLOG.yaml
>
> **•** CLAUDE_CODE_TASKS.yaml

# **6. סטטוס סופי**

| **Deliverable**                      | **Status** |
|--------------------------------------|------------|
| Baseline Consistency Review v0.1     | COMPLETED  |
| Proof-of-Integration Backlog v0.1    | COMPLETED  |
| Claude Code Implementation Plan v0.1 | COMPLETED  |
| Machine-readable PoI backlog         | COMPLETED  |
| Machine-readable Claude tasks        | COMPLETED  |
