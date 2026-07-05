תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

| **מטרת המסמך היא להבטיח שמערכת המסחר תישאר נשלטת, ניתנת לשחזור ובטוחה גם כאשר מקור מידע, מודל AI, מסד הנתונים, השרת או הברוקר נכשלים. המסמך מגדיר מה נעצר, מה ממשיך לפעול, מי רשאי לבצע פעולה, ואיך מגנים על פוזיציות פתוחות בלי לקבל החלטות פזיזות בזמן תקלה.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 0. מסמכי בסיס והנחות תכנון

| **מסמך**                                    | **תפקיד**                                                          |
|---------------------------------------------|--------------------------------------------------------------------|
| Decision Contract v0.8                      | סמכויות, החלטות, Hard Veto, כניסות ויציאות.                        |
| System Functional Specification v0.7        | פונקציונליות, מסכים, תהליכים ותרחישי כשל.                          |
| Data Model and Retention Specification v0.6 | אחסון, Retention, Snapshots, Orders ו-Positions.                   |
| Integration Specification v0.6              | WhatsApp, Discord, Telegram, InvestingPro, Market Data, AI ו-IBKR. |

## 0.1 הנחת תשתית נוכחית

> **•** שרת Oracle Linux ARM עם 4 ליבות, כ-22GB RAM וכ-112GB מקום פנוי בעת האפיון.
>
> **•** Docker ו-Docker Compose זמינים.
>
> **•** Reverse Proxy ו-TLS משמשים לחשיפת שירותי Web בלבד.
>
> **•** PostgreSQL, Queue, Workers ו-Object Storage ירוצו ברשת פנימית שאינה חשופה לאינטרנט.
>
> **•** Execution Agent של IBKR עשוי לרוץ במארח נפרד בהתאם לדרישות IB Gateway/TWS.

# 1. מטרות אבטחה ותפעול

> **•** למנוע ביצוע פקודות לא מורשות, כפולות או בסביבה שגויה.
>
> **•** להגן על Secrets, Sessions, חשבונות ברוקר ונתוני משתמש.
>
> **•** לשמור יכולת ניהול בטוחה של פוזיציות פתוחות בזמן תקלה.
>
> **•** לאפשר שחזור מלא של החלטות, פקודות, Fills וגרסאות.
>
> **•** להבטיח שכשל במקור, מודל או Scraper אינו הופך אוטומטית להחלטת מסחר.
>
> **•** לספק ניטור, התראות ו-Runbooks לפני מעבר ל-Paper Auto או Live.
>
> **•** להפריד בין עצירת כניסות חדשות לבין יציאה כפויה מפוזיציות קיימות.

# 2. נכסים מוגנים ואיומים מרכזיים

| **נכס**                            | **משמעות**                                    |
|------------------------------------|-----------------------------------------------|
| Broker Credentials and Sessions    | גישה לחשבון Paper ובעתיד Live.                |
| WhatsApp/Discord/Telegram Sessions | גישה לקבוצות ולמקורות המידע.                  |
| InvestingPro Session               | גישה למנוי ולנתוני המחקר.                     |
| AI Provider Keys                   | עלות, שימוש וחשיפת נתונים.                    |
| Database and Object Store          | הודעות, החלטות, עסקאות, Audit ו-Raw Evidence. |
| Configuration and Policies         | ספים, הרשאות, אסטרטגיות ומצבי הפעלה.          |
| Order Queues                       | פקודות שטרם בוצעו ותגובות הברוקר.             |
| Dashboard and Approval Actions     | אישורים, Kill Switch וקידום גרסאות.           |
| Server and Execution Host          | תשתית ההפעלה והחיבורים החיצוניים.             |

## 2.1 תרחישי איום

| **איום**                   | **השפעה**                                         |
|----------------------------|---------------------------------------------------|
| Credential Theft           | גניבת Token, Cookie, API Key או סיסמת ברוקר.      |
| Environment Confusion      | פקודת Paper שמגיעה בטעות ל-Live.                  |
| Unauthorized Approval      | אישור עסקה או קידום Policy בידי משתמש לא מורשה.   |
| Duplicate/Replay Order     | אותה פקודה נשלחת יותר מפעם אחת.                   |
| Data Poisoning             | נתון שגוי, ישן או מניפולטיבי נכנס להחלטה.         |
| Scraper Drift              | שינוי מבנה דף מפיק שדות שגויים בלי כשל גלוי.      |
| Compromised Source Adapter | Adapter מקבל הרשאות רחבות מדי או מדליף Session.   |
| Server Compromise          | גישה לא מורשית למארח, ל-Docker או למסד הנתונים.   |
| Operational Failure        | דיסק מלא, מסד לא זמין, Queue תקוע או Clock Drift. |
| Broker Unknown State       | המערכת אינה יודעת אם פקודה התקבלה או בוצעה.       |

# 3. עקרונות אבטחה מחייבים

| **מזהה** | **עיקרון**            | **דרישה**                                          |
|----------|-----------------------|----------------------------------------------------|
| SEC-01   | Least Privilege       | כל משתמש ושירות מקבלים רק את ההרשאות הדרושות.      |
| SEC-02   | Deny by Default       | גישה ושינוי אסורים אלא אם הותרו במפורש.            |
| SEC-03   | Fail Closed           | אי ודאות תפעולית חוסמת כניסות חדשות.               |
| SEC-04   | Separation of Duties  | AI, Decision, Risk, Approval ו-Broker מופרדים.     |
| SEC-05   | Tamper-Evident Audit  | אישורים, שינויים ופקודות נשמרים Append-only.       |
| SEC-06   | Environment Isolation | Paper ו-Live מבודדים בכל שכבה.                     |
| SEC-07   | Secret Minimization   | כל Service מקבל Secret Ref מינימלי.                |
| SEC-08   | No Direct Trust       | Source או Model אינם נחשבים אמינים ללא Validation. |
| SEC-09   | Safe Degradation      | כשל מקור אינו מוחק הגנות קיימות.                   |
| SEC-10   | Human Control         | פעולות הרסניות או Live רגישות מחייבות אישור מפורש. |

# 4. זהויות, התחברות והרשאות

| **תפקיד**        | **הרשאות**                                                          |
|------------------|---------------------------------------------------------------------|
| OWNER            | הגדרות מערכת, Secrets References, קידום גרסאות, Live ו-Kill Switch. |
| TRADER_APPROVER  | אישור/דחיית עסקאות במצבים ידניים.                                   |
| OPERATIONS_ADMIN | Health, Restart, Reconnect, Backup ו-Runbooks.                      |
| REVIEWER         | תחקור, תיוג ותיקון Entity ללא הרשאת ביצוע.                          |
| READ_ONLY        | צפייה בלבד.                                                         |
| SERVICE_IDENTITY | הרשאות מכונה מצומצמות לפי שירות.                                    |

## 4.1 דרישות התחברות

> **•** MFA חובה ל-OWNER, TRADER_APPROVER ו-OPERATIONS_ADMIN.
>
> **•** Session קצר יותר עבור פעולות Live, Secrets ו-Kill Switch.
>
> **•** אישור פעולה רגישה דורש Re-authentication כאשר חלף פרק הזמן שהוגדר.
>
> **•** אין חשבון Administrator משותף.
>
> **•** Service Identities אינן משתמשות בסיסמאות אנושיות.
>
> **•** כשלי התחברות, שינוי הרשאה ו-MFA events נרשמים ב-Audit.

## 4.2 Matrix הרשאות בסיסית

| **פעולה**             | **OWNER** | **TRADER**       | **OPS**   | **REVIEWER** | **READ ONLY** |
|-----------------------|-----------|------------------|-----------|--------------|---------------|
| צפייה בדאשבורד        | כן        | כן               | כן        | כן           | כן            |
| אישור Paper Manual    | כן        | כן               | לא        | לא           | לא            |
| אישור Live Manual     | כן        | לפי הרשאה מפורשת | לא        | לא           | לא            |
| קידום Strategy/Policy | כן        | לא               | לא        | לא           | לא            |
| הפעלת Kill Switch     | כן        | לפי הרשאה        | כן        | לא           | לא            |
| שינוי Secrets         | כן        | לא               | לפי הרשאה | לא           | לא            |
| תיקון Entity Label    | כן        | כן               | כן        | כן           | לא            |

# 5. Secrets, Tokens ו-Sessions

| **סוג**          | **דוגמאות**                             |
|------------------|-----------------------------------------|
| BROKER_SECRET    | IBKR credentials/session/config.        |
| SOCIAL_SESSION   | WhatsApp, Discord ו-Telegram sessions.  |
| RESEARCH_SESSION | InvestingPro session/cookies.           |
| AI_API_KEY       | מפתחות ספקי מודלים.                     |
| DATABASE_SECRET  | PostgreSQL credentials.                 |
| ENCRYPTION_KEY   | הצפנת Sessions, Backups ו-Object Store. |
| TLS_SECRET       | Certificates ו-private keys.            |

## 5.1 כללי שמירה

> **•** Secrets אינם נשמרים ב-Git, ב-Markdown, ב-Environment Dumps, ב-Audit או בלוגים.
>
> **•** קונפיגורציה מכילה secret_ref בלבד.
>
> **•** Sessions נשמרים מוצפנים ומופרדים לפי אינטגרציה וסביבה.
>
> **•** Paper ו-Live משתמשים ב-Secrets Stores נפרדים.
>
> **•** Workers מקבלים Secrets בזמן ריצה בלבד ולא מעבירים אותם לשירותים אחרים.
>
> **•** Rotation או Re-auth נרשמים ללא שמירת הערך הסודי.
>
> **•** לוגים עוברים Redaction של Tokens, Cookies, Authorization Headers, QR ו-Passwords.

## 5.2 מחזור חיים

| **שלב**    | **פעולה**                             |
|------------|---------------------------------------|
| CREATE     | יצירת Secret דרך ממשק/כלי מורשה.      |
| DISTRIBUTE | הזרקה לשירות הדרוש בלבד.              |
| USE        | שימוש בזיכרון או קובץ זמני מוגן.      |
| ROTATE     | החלפה מתוזמנת או בעקבות אירוע.        |
| REVOKE     | ביטול מיידי בעת חשד.                  |
| DESTROY    | מחיקה מאובטחת של עותקים שאינם נחוצים. |

# 6. הפרדת Paper ו-Live

| **הפרדת Paper ו-Live אינה רק ערך בקונפיגורציה. היא חייבת להתקיים בחשבונות, Secrets, תורים, Processes, Allowlist, UI, Audit ו-Execution Agents.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------|

| **שכבה**             | **הפרדה**                                                      |
|----------------------|----------------------------------------------------------------|
| Credentials          | נפרדים לחלוטין.                                                |
| Account Allowlist    | רשימה נפרדת ומחייבת.                                           |
| Order Queue          | paper.orders ו-live.orders.                                    |
| Execution Process    | Process/Container נפרד.                                        |
| Environment Variable | ערך קשיח בעת הפעלה; אינו משתנה דרך UI.                         |
| Database Field       | environment חובה בכל Decision, Intent, Order, Fill ו-Position. |
| UI                   | Banner קבוע ושפה חזותית מובחנת.                                |
| Promotion            | Strategy חייבת להיות מאושרת ל-Live.                            |
| Approval             | LIVE_MANUAL מחייב אישור טרי ובעל תוקף.                         |
| Kill Switch          | נפרד לכל סביבה וכן Global.                                     |

## 6.1 Guard Conditions לפני Live Order

> **•** ה-Execution Agent הופעל מראש במצב LIVE ולא ניתן לשינוי בזמן ריצה.
>
> **•** Account Alias נמצא ב-Live Allowlist.
>
> **•** OrderIntent, Decision ו-RiskDecision כולם environment=LIVE.
>
> **•** Strategy Version מאושרת ל-LIVE_MANUAL או LIVE_LIMITED_AUTO.
>
> **•** האישור האנושי לא פג.
>
> **•** Broker Reconciliation תקין.
>
> **•** Global/Live/Account/Strategy Kill Switch אינם פעילים.

# 7. ארכיטקטורת רשת והקשחת שרת

| **אזור**    | **תכולה**                                           |
|-------------|-----------------------------------------------------|
| PUBLIC EDGE | Reverse Proxy בלבד; פורטים 80/443.                  |
| APPLICATION | API, Dashboard ו-Workers ברשת Docker פנימית.        |
| DATA        | PostgreSQL, Queue ו-Object Store ללא חשיפה ציבורית. |
| EXECUTION   | IBKR Execution Agent וגישה מוגבלת ל-Order Queue.    |
| MANAGEMENT  | SSH/VPN/Administrative access מוגבל.                |

## 7.1 הקשחת מארח

> **•** SSH באמצעות מפתחות בלבד; ביטול Password Authentication כאשר מתאפשר.
>
> **•** גישה מנהלית מוגבלת לכתובות/רשת מורשות או VPN.
>
> **•** firewalld מאפשר רק פורטים נדרשים.
>
> **•** PostgreSQL, Queue, Docker sockets וממשקי Admin אינם חשופים לאינטרנט.
>
> **•** עדכוני אבטחה מותקנים לפי חלון תחזוקה; תיקונים קריטיים מקבלים מסלול מואץ.
>
> **•** אין הרצת Containers כ-root כאשר אין צורך.
>
> **•** Volumes ו-Directories מקבלים הרשאות מינימליות.
>
> **•** Docker socket אינו מותקן בתוך Containers רגילים.
>
> **•** Clock synchronization חובה; Drift משמעותי מפעיל Alert ויכול לחסום ביצוע.
>
> **•** Malware/Integrity checks יוגדרו לפי כלי התפעול שייבחר.

## 7.2 חשיפה חיצונית

> **•** Dashboard ו-API נחשפים רק דרך TLS ו-Reverse Proxy.
>
> **•** Management endpoints אינם נגישים ציבורית.
>
> **•** Rate limiting והגנת Login מופעלים בשכבת ה-Edge.
>
> **•** Webhook עתידי יקבל Signature Verification ו-Replay Protection.
>
> **•** אין גישה ישירה ל-IBKR API מהאינטרנט.

# 8. הגנת נתונים ופרטיות

| **סיווג**             | **דוגמאות**                              |
|-----------------------|------------------------------------------|
| PUBLIC_MARKET         | מחירים, דיווחים וחדשות ציבוריות.         |
| LICENSED_RESEARCH     | נתוני InvestingPro ומקורות מורשים.       |
| PRIVATE_COMMUNICATION | הודעות WhatsApp/Discord/Telegram וקבצים. |
| TRADING_SENSITIVE     | החלטות, חשיפות, פוזיציות ופקודות.        |
| SECURITY_SECRET       | Credentials, Sessions, Keys ו-Tokens.    |
| AUDIT                 | פעולות משתמש ושירות.                     |

## 8.1 בקרות

> **•** TLS לכל תעבורה בין Browser, API ו-Reverse Proxy.
>
> **•** הצפנה במנוחה ל-Secrets, Session Stores ו-Backups.
>
> **•** Raw Social Content מוצג רק לבעלי הרשאה מתאימה.
>
> **•** שמות, מספרים ומזהים שאינם דרושים לניתוח יכולים לעבור Pseudonymization.
>
> **•** Export של נתונים רגישים נרשם ב-Audit.
>
> **•** Retention מוגדר לפי Data Model; Evidence משויך להחלטה נשמר בהתאם ל-Pinning.
>
> **•** מחיקה לוגית או פיזית אינה מוחקת Audit של עצם הפעולה.

# 9. Audit, Logs, Metrics ו-Traces

| **קטגוריה** | **אירועים**                                           |
|-------------|-------------------------------------------------------|
| AUTH        | Login, logout, MFA, failure, role change.             |
| CONFIG      | Policy/Strategy/Prompt/Provider version changes.      |
| APPROVAL    | Approve, reject, override, expiration.                |
| EXECUTION   | OrderIntent, submit, cancel, modify, fill, reconcile. |
| SECURITY    | Secret rotate, auth required, suspicious access.      |
| OPERATIONS  | restart, pause, circuit open, backup, restore.        |
| EMERGENCY   | kill switch, protective action, flatten request.      |

## 9.1 כללי Logging

> **•** Logs מובנים ב-JSON עם timestamp, service, environment, correlation_id ו-severity.
>
> **•** אין Secrets או תוכן מלא של Sessions בלוג.
>
> **•** Order, Decision ו-Broker IDs ניתנים לקישור דרך Correlation ID.
>
> **•** Audit Events הם Append-only ואינם ניתנים לעריכה דרך UI.
>
> **•** Clock לכל השירותים מסונכרן.
>
> **•** לוגים מקומיים עוברים Rotation ומגבלת נפח.

# 10. ניטור ו-SLOs ראשוניים

| הערכים בטבלה הם יעדי תכנון ראשוניים ולא התחייבות סופית. הם יישמרו בקונפיגורציה ויעודכנו לאחר תקופת מדידה ב-Shadow וב-Paper. |
|-----------------------------------------------------------------------------------------------------------------------------|

| **מדד**                 | **יעד ראשוני**              | **חלון**             |
|-------------------------|-----------------------------|----------------------|
| API Availability        | 99.5% בשלב Paper            | חודשי                |
| Decision Worker Health  | אין כשל שקט מעל חלון ההתראה | רציף                 |
| Broker Reconciliation   | תקין לפני כל חידוש ביצוע    | אירועי               |
| Critical Alert Delivery | פחות מדקה יעד ראשוני        | P95                  |
| Clock Drift             | פחות משנייה יעד ראשוני      | רציף                 |
| Database Backup Success | 100% Jobs מתוזמנים          | יומי                 |
| Restore Test            | שחזור מוכח                  | חודשי/רבעוני לפי שלב |
| Disk Utilization        | התראה לפני סיכון            | 70/80/90%            |

## 10.1 Metrics קריטיים

| **תחום**    | **מדדים**                                                      |
|-------------|----------------------------------------------------------------|
| Host        | CPU, RAM, load, disk, inode, network, clock.                   |
| Docker      | container restarts, health, CPU/RAM, volume usage.             |
| PostgreSQL  | connections, locks, replication/WAL, query latency, size.      |
| Queue       | depth, oldest message age, retry and DLQ.                      |
| Adapters    | health, lag, error rate, last success.                         |
| AI          | latency, failures, validation errors, cost.                    |
| Market Data | freshness, gaps, provider conflict.                            |
| Decision    | processing profile, deadline misses, outcomes.                 |
| Broker      | connection, order ack, rejects, unknown state, reconciliation. |
| Positions   | missing stop, quantity mismatch, unprotected exposure.         |

# 11. התראות והסלמה

| **דרגה** | **דוגמאות**                                                                                        |
|----------|----------------------------------------------------------------------------------------------------|
| SEV-1    | סיכון מיידי לכסף/פקודות: Live mismatch, duplicate order, missing protection, unknown broker state. |
| SEV-2    | פגיעה מהותית: DB unavailable, market data stale בזמן פעילות, broker disconnect with positions.     |
| SEV-3    | פגיעה חלקית: scraper drift, source auth required, model provider degraded.                         |
| SEV-4    | תחזוקה/מידע: backup warning, approaching capacity, scheduled re-auth.                              |

## 11.1 ערוצי התראה

> **•** Dashboard Banner.
>
> **•** התראה מיידית לטלפון/ערוץ מאושר עבור SEV-1 ו-SEV-2.
>
> **•** Email או ערוץ תפעולי עבור SEV-3 ו-SEV-4.
>
> **•** התראה חוזרת עד Acknowledge כאשר האירוע פתוח.
>
> **•** כל Acknowledge, Suppression וסגירה נרשמים ב-Audit.

# 12. Kill Switch ובלמי חירום

| **Kill Switch אינו שווה אוטומטית 'למכור הכול'. ברירת המחדל היא לעצור כניסות חדשות ולשמור או לנהל את פקודות ההגנה הקיימות. Flatten של פוזיציות הוא פעולה נפרדת, מפורשת ומבוקרת.** |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **רמה**           | **פעולה**                                 |
|-------------------|-------------------------------------------|
| GLOBAL_ENTRY_HALT | עצירת כל כניסה חדשה בכל הסביבות המוגדרות. |
| ENVIRONMENT_HALT  | עצירת Paper או Live בלבד.                 |
| BROKER_HALT       | עצירת פקודות לברוקר מסוים.                |
| ACCOUNT_HALT      | עצירת חשבון מסוים.                        |
| STRATEGY_HALT     | עצירת אסטרטגיה/גרסה.                      |
| SYMBOL_HALT       | עצירת נכס מסוים.                          |
| SOURCE_HALT       | הפסקת שימוש במקור לצורך החלטות.           |
| MODEL_HALT        | השבתת Model/Task מסוים.                   |

## 12.1 מצבי חירום לפוזיציות

| **פעולה**                     | **משמעות**                                       |
|-------------------------------|--------------------------------------------------|
| HALT_NEW_ENTRIES              | עצירת עסקאות חדשות; ניהול פוזיציות ממשיך.        |
| KEEP_PROTECTION               | שמירת Stops/Targets קיימים.                      |
| CANCEL_UNFILLED_ENTRIES       | ביטול פקודות כניסה שלא בוצעו.                    |
| CANCEL_AND_REBUILD_PROTECTION | רק לאחר Reconciliation ואישור Runbook.           |
| REDUCE_RISK                   | הקטנה מבוקרת לפי Policy ואישור.                  |
| FLATTEN_ACCOUNT               | סגירת כל הפוזיציות; פעולה נפרדת, מסוכנת ומאושרת. |

## 12.2 הרשאות ו-Audit

> **•** Global או Live Kill Switch דורשים OWNER או הרשאה מפורשת.
>
> **•** FLATTEN_ACCOUNT מחייב Re-authentication ואישור כפול כאשר Live יופעל.
>
> **•** הפעלת Kill Switch כוללת reason_code וטקסט הסבר.
>
> **•** ביטול Kill Switch מחייב בדיקות Health ו-Reconciliation.
>
> **•** אין Resume אוטומטי לאחר SEV-1.

# 13. גיבויים, שחזור ושרידות

| **רכיב**         | **גיבוי**                                                   |
|------------------|-------------------------------------------------------------|
| PostgreSQL       | Full backup + WAL/PITR לפי יכולת.                           |
| Object Store     | Raw Evidence, attachments, snapshots.                       |
| Configuration    | Policies, Strategies, Prompts ו-Active Pointers.            |
| Secrets Metadata | References והרשאות; ערכי Secret מגובים במנגנון מאובטח נפרד. |
| Audit            | עותק מוגן ובלתי תלוי.                                       |
| Infrastructure   | Compose, migrations, deployment manifests ו-runbooks.       |

## 13.1 מדיניות ראשונית

| **סוג**                  | **תדירות**                         | **שמירה**        |
|--------------------------|------------------------------------|------------------|
| Database Full            | יומי                               | 30 ימים ראשוני   |
| WAL/PITR                 | רציף/תדיר                          | לפי נפח ויעד RPO |
| Object Store Incremental | יומי                               | לפי Retention    |
| Config Export            | בכל שינוי + יומי                   | גרסאות היסטוריות |
| Offsite Encrypted Copy   | יומי/שבועי                         | חובה לפני Live   |
| Restore Test             | חודשי ב-Paper; תדיר יותר לפני Live | דוח מוכח         |

## 13.2 RPO/RTO יעדים ראשוניים

| **סביבה**    | **RPO**                       | **RTO**                    |
|--------------|-------------------------------|----------------------------|
| Paper        | RPO עד 15 דקות יעד            | RTO עד 4 שעות יעד          |
| Live Limited | RPO עד 5 דקות יעד             | RTO עד שעה יעד             |
| Broker State | הברוקר מקור אמת לפקודות/Fills | Reconciliation לפני Resume |

## 13.3 שחזור

> **•** Restore מבוצע לסביבה מבודדת לפני החלפת Production.
>
> **•** נבדקת עקביות Decisions, Intents, Orders, Fills ו-Positions.
>
> **•** לאחר שחזור, Broker Reconciliation חובה.
>
> **•** Active Pointers ו-Environment נשמרים או מוגדרים מחדש במפורש.
>
> **•** Live Execution נשאר כבוי עד אישור Recovery Checklist.

# 14. תפעול פוזיציות בזמן תקלה

| **תרחיש**            | **התנהגות**                                                               |
|----------------------|---------------------------------------------------------------------------|
| Market Data Down     | עצירת כניסות; הגנות ברוקר נשארות; אין שינוי Stop על בסיס נתון לא אמין.    |
| AI Provider Down     | עצירת החלטות התלויות ב-AI; Stops ו-Time Exits דטרמיניסטיים ממשיכים.       |
| Decision Engine Down | אין עסקאות חדשות; Position Manager ו-Broker Protection ממשיכים.           |
| Database Down        | Fail Closed לכניסות; אין שליחת Intent חדש; התראות מיידיות.                |
| Queue Down           | אין ביצוע חדש; Reconciliation לאחר שחזור.                                 |
| IBKR Disconnected    | אין פקודות חדשות; הגנות שכבר נמצאות בברוקר נשארות; Reconnect + Reconcile. |
| Execution Host Down  | הברוקר מנהל פקודות קיימות; הפעלה מחדש במצב Halt ואז Reconcile.            |
| Server Down          | אותו עיקרון: ללא כניסות חדשות; הגנות Broker-side הן קו ההגנה.             |

## 14.1 כלל Broker-side Protection

| **לפני מעבר ל-PAPER_AUTO ובהמשך ל-Live, כל אסטרטגיה שמסתמכת על Stop חייבת להוכיח שפקודת ההגנה הוצבה אצל הברוקר ולא רק נשמרה בזיכרון המערכת. פוזיציה ללא הגנה צפויה מייצרת SEV-1 וכניסות חדשות נעצרות.** |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 15. Runbooks מחייבים

| **Runbook**                                 | **פעולות עיקריות**                                                                            |
|---------------------------------------------|-----------------------------------------------------------------------------------------------|
| RB-01 Broker Disconnect with Open Positions | Halt entries, verify broker UI, reconnect, reconcile, verify protection, resume manually.     |
| RB-02 Missing Protective Order              | SEV-1, halt strategy/account, reconcile, place protection or reduce/close by approved action. |
| RB-03 Unknown Order State                   | Do not resend blindly, query broker, reconcile by idempotency key, resolve manually.          |
| RB-04 Duplicate Order Suspected             | Global/account halt, reconcile, cancel excess if confirmed, audit and incident.               |
| RB-05 Market Data Stale                     | Halt affected strategies, keep broker protection, switch provider only if configured.         |
| RB-06 InvestingPro Drift                    | Open circuit, disable publication, retain raw, validate new parser in shadow.                 |
| RB-07 Social Adapter Auth Expired           | Mark AUTH_REQUIRED, do not infer empty feed, re-auth manually.                                |
| RB-08 Database Unavailable                  | Halt new intents, preserve broker protection, restore service, integrity checks.              |
| RB-09 Disk Capacity Critical                | Halt noncritical ingestion, rotate logs, expand/clean by approved policy.                     |
| RB-10 AI Provider Failure                   | Task-specific fallback or halt; never synthesize missing output.                              |
| RB-11 Server Restart                        | Start in MAINTENANCE/HALT, health checks, broker reconcile, explicit resume.                  |
| RB-12 Security Incident                     | Revoke credentials, isolate service, halt live, preserve evidence, incident response.         |

# 16. תחזוקה, שינויים ופריסות

> **•** כל שינוי קוד, Config, Policy, Schema או Parser מקבל Version ו-Change Record.
>
> **•** פריסה עוברת Dev/Test/Shadow לפני Production.
>
> **•** Database Migration כוללת Roll-forward ו-Rollback plan.
>
> **•** שינוי מסוכן אינו מתבצע כאשר קיימות פוזיציות פתוחות, אלא אם Runbook מאפשר.
>
> **•** Deployment מתחיל ב-MAINTENANCE או Entry Halt לפי הסיכון.
>
> **•** לאחר פריסה: Health, Contract Tests, Smoke Test ו-Reconciliation.
>
> **•** Auto-update של Containers או OS אינו מופעל ללא שליטה.
>
> **•** Emergency Patch נרשם ומקבל Review בדיעבד.

## 16.1 Change Risk Levels

| **רמה**  | **דוגמאות**                                                              |
|----------|--------------------------------------------------------------------------|
| LOW      | UI, דוחות או שינוי שאינו משפיע החלטה/ביצוע.                              |
| MEDIUM   | Adapter, Parser, AI Prompt או Feature שאינו Active.                      |
| HIGH     | Decision/Risk Policy, Active Strategy, Market Data או Queue.             |
| CRITICAL | Broker Adapter, Live isolation, secrets, kill switch או migrations ליבה. |

# 17. ניהול אירועים ותגובה לאירועי אבטחה

| **שלב** | **שם**   | **פעולה**                                                |
|---------|----------|----------------------------------------------------------|
| 1       | Detect   | התראה, דיווח משתמש או חריגה.                             |
| 2       | Classify | SEV, סביבה, חשבונות ונכסים מושפעים.                      |
| 3       | Contain  | Kill Switch, isolate service, revoke secret.             |
| 4       | Preserve | Audit, logs, snapshots וראיות.                           |
| 5       | Recover  | שחזור, rotation, reconcile ובדיקות.                      |
| 6       | Resume   | אישור מפורש ו-Checklist.                                 |
| 7       | Review   | Root cause, impact, corrective actions ו-policy changes. |

## 17.1 תנאים לחידוש מסחר

> **•** שורש התקלה זוהה או קיים Mitigation מאושר.
>
> **•** כל Secrets החשודים בוטלו או הוחלפו.
>
> **•** Health של השירותים הנדרשים תקין.
>
> **•** Broker Reconciliation הושלם.
>
> **•** אין פוזיציות או פקודות לא ידועות.
>
> **•** Audit ו-Incident Record הושלמו ברמה הנדרשת.
>
> **•** OWNER או גורם מוסמך אישר Resume.

# 18. Capacity, Disk ו-Resource Management

| **משאב**     | **בקרה**                                                |
|--------------|---------------------------------------------------------|
| CPU          | Alert על עומס מתמשך; Workers מקבלים Concurrency limits. |
| RAM          | Reserve למערכת ההפעלה; מניעת OOM; Limits ל-Containers.  |
| Disk         | Thresholds 70/80/90%; הפרדת DB, raw ו-logs לפי יכולת.   |
| PostgreSQL   | Growth, indexes, vacuum, WAL ו-connections.             |
| Object Store | Lifecycle, compression ו-retention.                     |
| Logs         | Rotation, compression ו-delete policy.                  |
| Queue        | Depth, oldest age, DLQ ו-backpressure.                  |

## 18.1 תגובה לסף דיסק

| **סף** | **פעולה**                                                 |
|--------|-----------------------------------------------------------|
| 70%    | Warning; בדיקת קצב גידול.                                 |
| 80%    | High alert; עצירת Backfill והאצת Archive.                 |
| 90%    | Critical; עצירת קליטה לא קריטית, שמירת Trading Core בלבד. |
| 95%    | Emergency; Entry Halt, הגנת DB, פינוי מאושר/הרחבה.        |

# 19. בדיקות אבטחה ותפעול

| **בדיקה**                   | **מטרה**                                     |
|-----------------------------|----------------------------------------------|
| Authentication Tests        | MFA, session expiry, lockout ו-re-auth.      |
| Authorization Tests         | RBAC ו-deny-by-default.                      |
| Secret Leakage Tests        | Git, logs, exceptions ו-backups.             |
| Environment Isolation Tests | Paper cannot reach Live.                     |
| Order Safety Tests          | duplicate, retry, timeout ו-unknown state.   |
| Kill Switch Drills          | global, environment, strategy ו-account.     |
| Restore Drills              | database, object store ו-full-host recovery. |
| Broker Failure Drills       | disconnect, partial fill, missing stop.      |
| Capacity Drills             | disk, queue backlog ו-DB saturation.         |
| Incident Tabletop           | תרחיש אבטחה עם פוזיציות פתוחות.              |

# 20. קריטריוני קבלה

| **מזהה**  | **קריטריון**                                                        |
|-----------|---------------------------------------------------------------------|
| SEC-AC-01 | אין Secret בקוד, Config גלוי או Logs.                               |
| SEC-AC-02 | Paper Order אינו מסוגל להגיע ל-Live Process/Queue/Account.          |
| SEC-AC-03 | משתמש ללא הרשאה אינו יכול לאשר עסקה או לשנות Policy.                |
| SEC-AC-04 | MFA נדרש לפעולות רגישות.                                            |
| SEC-AC-05 | Global Entry Halt עוצר פקודות חדשות אך אינו מוחק הגנות קיימות.      |
| SEC-AC-06 | FLATTEN_ACCOUNT הוא פעולה נפרדת ומאושרת.                            |
| SEC-AC-07 | Broker Disconnect עם פוזיציות מפעיל SEV-2 לפחות ו-Reconciliation.   |
| SEC-AC-08 | פוזיציה ללא Protective Order מפעילה SEV-1.                          |
| SEC-AC-09 | Restore Test משחזר Decisions, Orders, Fills ו-Positions באופן עקבי. |
| SEC-AC-10 | Clock Drift משמעותי מזוהה וחוסם פעולות תלויות זמן.                  |
| SEC-AC-11 | Disk Critical משמר Trading Core ועוצר קליטה לא קריטית.              |
| SEC-AC-12 | כל Kill Switch, Resume ו-Emergency Action נשמרים ב-Audit.           |
| SEC-AC-13 | Incident Resume אינו אוטומטי לאחר SEV-1.                            |
| SEC-AC-14 | Service Identity אינה יכולה לגשת ל-Secrets שאינם נחוצים לה.         |
| SEC-AC-15 | Live אינו מופעל לפני מעבר מלא של Drills ו-Runbooks.                 |

# 21. החלטות פתוחות

| **מזהה**    | **החלטה**                                                   |
|-------------|-------------------------------------------------------------|
| SEC-OPEN-01 | בחירת Secrets Manager או מנגנון מוצפן מקומי.                |
| SEC-OPEN-02 | בחירת VPN/Zero-Trust לגישה מנהלית.                          |
| SEC-OPEN-03 | Stack לניטור, Logs, Metrics ו-Alerts.                       |
| SEC-OPEN-04 | מיקום ויעד Offsite Backups.                                 |
| SEC-OPEN-05 | ערוצי Alert לטלפון/Email/אפליקציה.                          |
| SEC-OPEN-06 | מדיניות MFA ומנגנון Identity Provider.                      |
| SEC-OPEN-07 | האם Execution Agent ירוץ על מארח Windows, VM או שרת ייעודי. |
| SEC-OPEN-08 | RPO/RTO סופיים לפני Live.                                   |
| SEC-OPEN-09 | האם נדרש אישור כפול לפעולות Live מסוימות.                   |
| SEC-OPEN-10 | כלי Integrity/Endpoint Security למארחים.                    |

# 22. השלב הבא

> 1\. Strategy and Decision Framework v0.5 — הגדרת משפחות האסטרטגיה, כניסות, יציאות, תזמון ולמידה.
>
> 2\. MVP Scope v0.6 — מה נכנס לגרסה הראשונה ומה נדחה.
>
> 3\. Technology and Deployment Decisions v0.6 — בחירת Stack בפועל.
>
> **4.** Consolidated Review — בדיקת סתירות וחוסרים בין כל המסמכים.
>
> **5.** Claude Code Implementation Plan — רק לאחר השלמת ואישור כל האפיון.

# 23. Baseline אבטחה ותפעול מתוקן

| **הפרק משלב הפרדת Secrets/Sessions, Audit חסין-שינוי, טיפול בפקודות שבתור ו-Offsite Recovery.** |
|-------------------------------------------------------------------------------------------------|

## 23.1 Static Secrets לעומת Runtime Sessions

| **מאגר**              | **תוכן**                                                                   | **גישה**                         |
|-----------------------|----------------------------------------------------------------------------|----------------------------------|
| Static Secret Store   | API keys, DB password, encryption roots, TLS references.                   | Deploy/Service Identity מצומצמת. |
| Runtime Session Store | WhatsApp/Discord/Telegram/InvestingPro session ciphertext.                 | Connector identity בלבד.         |
| Application DB        | metadata, expiry, key version, audit refs; ללא ciphertext אם המאגר חיצוני. | API/operations לפי RBAC.         |

## 23.2 Kill Switch ו-Queued Orders

> **•** Halt נבדק בזמן יצירת Intent, בזמן צריכת התור ולפני Submit.
>
> **•** Intent ממתין שאינו מורשה מסומן CANCELLED_BY_POLICY ונשמר ב-Audit.
>
> **•** פקודה שכבר ACKNOWLEDGED אינה מבוטלת אוטומטית אלא לפי Runbook.
>
> **•** Protective Orders אינם מתבטלים בגלל Entry Halt.

## 23.3 Tamper-Evident Audit

> **•** Audit הוא Append-only ומקושר ב-Hash Chain.
>
> **•** Checkpoint תקופתי נשמר Offsite או נחתם באמצעות מפתח נפרד.
>
> **•** Verification Job בודק את השרשרת ומתריע על BROKEN.
>
> **•** המונח המחייב הוא tamper-evident, לא הבטחה מוחלטת ל-immutable בתוך DB יחיד.

## 23.4 Offsite Backup

> **•** עותק מוצפן מחוץ לשרת Oracle חובה לפני PAPER_AUTO.
>
> **•** Database, Object Evidence, Config Versions, Audit Checkpoints ו-Runbooks כלולים.
>
> **•** Restore Drill בודק גם Instrument Mappings, Active Pointers ו-Consumer Inbox.
>
> **•** לאחר Restore, Execution נשאר Halt עד Broker Reconciliation.

## 23.5 Exit Coordinator Security

> **•** רק Service Identity של Position Coordinator רשאית לפרסם position exit command.
>
> **•** Manual Exit עובר דרך API authorization ויוצר Command, לא Broker call ישיר.
>
> **•** expected_generation מונע Command על מצב פוזיציה ישן.
>
> **•** FLATTEN_ACCOUNT דורש Re-auth ואישור כפול כאשר Live יופעל.

## 23.6 Corporate Action Incident Mode

> **•** Corporate Action conflict מפעיל SYMBOL_HALT.
>
> **•** Position עוברת RECONCILIATION_REQUIRED.
>
> **•** אין התאמה ידנית שקטה של Quantity או Stop.
>
> **•** Resume דורש Broker Snapshot, Provider Evidence ו-Adjustment Record.

## 23.7 PoI Security Gates

> **•** Session encryption survives restart and rotation.
>
> **•** No secrets appear in logs, dumps or browser screenshots.
>
> **•** Paper process cannot read Live namespace.
>
> **•** Kill switch cancels queued intents under load.
>
> **•** Audit verification detects modified test event.

# 24. אבטחת אוטומציה, מודלים וממשק חירום

| **אוטונומיה מותרת רק בתוך Envelope מאושר. המפעיל חייב להיות מסוגל לעצור מיד עסקאות חדשות, לזהות איזה מנוע יצר החלטה ולבודד Model, Strategy, Source או Broker בלי לפגוע בהגנות קיימות.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 24.1 Emergency Control UX

| **פעולה**                  | **התנהגות**                                       | **אימות**                                             |
|----------------------------|---------------------------------------------------|-------------------------------------------------------|
| GLOBAL ENTRY HALT          | מיידי; עוצר candidates-to-order ו-queued intents. | ללא MFA כדי לא לעכב חירום; Audit חובה.                |
| STRATEGY/MODEL/SOURCE HALT | מונע assessments/decisions חדשים מהיעד.           | Role permission.                                      |
| CANCEL UNFILLED ENTRIES    | לאחר broker state check.                          | Confirmation.                                         |
| KEEP PROTECTION            | ברירת מחדל לכל halt.                              | אוטומטי.                                              |
| FLATTEN ACCOUNT            | סגירה מבוקרת של positions.                        | Re-auth + typed confirmation; double approval ב-Live. |
| RESUME                     | רק לאחר Health, Reconciliation ו-Runbook.         | MFA + owner/authorized role.                          |

## 24.2 Model Security

> **•** Artifact hash/signature נבדקים לפני טעינה.
>
> **•** Training environment אינו מחזיק Broker Credentials.
>
> **•** Model output עובר Schema Validation, bounds checks ו-calibration status.
>
> **•** Prompt injection או תוכן אינטרנטי אינו יכול לייצר OrderIntent ישירות.
>
> **•** Drift, anomaly או error rate יכולים להפעיל MODEL_HALT אוטומטי לפי Policy.

## 24.3 Webhook Security

> **•** TradingView ו-External webhooks משתמשים ב-endpoint secret, TLS, rate limit, idempotency ו-payload size limits.
>
> **•** אין לקבל broker account, order size או unrestricted command מתוך payload חיצוני.
>
> **•** כל payload נשמר כ-untrusted input ומנורמל לפני שימוש.

## 24.4 Dashboard Safety

> **•** Environment, Auto Mode, Broker Sync ו-Kill State מוצגים באופן קבוע ואינם נעלמים בגלילה.
>
> **•** פעולות מסוכנות אינן תלויות בצבע בלבד ומציגות scope מפורש.
>
> **•** Read-only mode נשמר גם כאשר חלק מה-Backend degraded.
>
> **•** ממשק חירום נבדק ב-Chaos Drills ובמכשיר שולחני ומובייל.

# **22. הגנת קלט עוין, מניפולציות ושרשרת אמון**

| **איום**                           | **בקרה**                                                                              |
|------------------------------------|---------------------------------------------------------------------------------------|
| Prompt Injection בתוכן אינטרנטי    | התוכן מסומן כ-UNTRUSTED_DATA; מודל אינו מקבל כלי ברוקר/Secrets; פלט Schema בלבד.      |
| קובץ או URL זדוני                  | Sandbox, content-type allowlist, size limit, malware scan ו-no active content.        |
| Webhook מזויף או Replay            | Signature, timestamp window, nonce ו-idempotency key.                                 |
| Pump-and-Dump / Coordinated Social | Cluster dedup, source independence, abnormal-volume flags ו-independent confirmation. |
| Fake News / מקור מתחזה             | Source class, official verification, domain allowlist ו-provenance chain.             |
| Data Poisoning                     | Cross-provider checks, anomaly detection, quarantines ו-versioned corrections.        |
| Model Prompt/Output Abuse          | Strict schemas, length limits, allowlisted enums ו-no executable output.              |
| Emergency UI outage                | Minimal emergency service ו-local authenticated CLI over restricted admin path.       |

## **22.1 הפרדת סמכויות**

> **•** שום מודל AI אינו מחזיק Credential לברוקר או הרשאת Submit.
>
> **•** Content fetcher אינו מחזיק הרשאות מסחר או גישה ל-Secrets שאינם דרושים.
>
> **•** Execution Agent מקבל רק OrderIntent קנוני שעבר Final Pre-Submit Check.
>
> **•** Notification channel אינו יכול לבצע Resume, Flatten או שינוי Policy.

## **22.2 בקרת חירום**

> **•** GLOBAL_ENTRY_HALT הוא Fail-closed, מופץ לכל Consumers ונבדק שוב ב-Execution Agent.
>
> **•** פקודות בתור מסומנות CANCELLED_BY_POLICY או מוחזקות עד Reconciliation.
>
> **•** ערוץ חירום נפרד דורש MFA/SSH key, least privilege, audit ו-rate limit.

# **25. אבטחה ותפעול של Telegram**

| **נכס**                 | **בקרה**                                                      |
|-------------------------|---------------------------------------------------------------|
| Bot token               | Secret סטטי; ללא Frontend, Logs או Repository.                |
| api_id/api_hash         | Secrets סטטיים עבור TDLib; הרשאת גישה מצומצמת ל-Adapter.      |
| TDLib Auth Key/Database | Runtime Session מוצפן, Volume נפרד ו-Backup לפי Policy.       |
| Webhook                 | HTTPS, secret header, allowed updates, dedup ו-rate limiting. |
| Chat Allowlist          | Default deny; שינוי דורש OWNER ואודיט.                        |
| Attachments             | Trust pipeline, size/type limits ו-quarantine.                |
| Notifications           | צ'אט פרטי מוגדר; אין מידע רגיש מלא כברירת מחדל.               |
| Network                 | אין גישה ל-execution_net או Broker credentials.               |

## **25.1 איסור שליטה במסחר דרך Telegram**

| **ב-MVP Telegram אינו ערוץ פיקוד. הודעה כגון BUY, SELL, HALT או FLATTEN שנשלחת לצ'אט אינה פעולה מורשית. היא נחשבת תוכן מקור בלבד ועוברת את Pipeline הרגיל.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **25.2 Runbook תפעולי**

> **•** AUTH_REQUIRED: עצירת Adapter, Alert, פתיחת Re-auth flow ואימות Owner.
>
> **•** TOKEN_COMPROMISE: Rotation מיידי, ביטול Webhook/Session, Audit ו-Review של Events.
>
> **•** PERMISSION_CHANGED: מעבר ל-DEGRADED, עצירת אותו Source Profile ו-Alert.
>
> **•** RATE_LIMITED/FLOOD_WAIT: Backoff לפי השרת, ללא Retry אגרסיבי.
>
> **•** SESSION_CORRUPTION: בידוד Volume, שחזור או Login מחדש; אין העתקת Session בלתי מבוקרת.

# **26. אבטחת User-first Social Sessions**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **בקרה**              | **דרישה**                                                                                   |
|-----------------------|---------------------------------------------------------------------------------------------|
| Session isolation     | Volume/secret namespace נפרד לכל WhatsApp User, Discord User, Telegram User ו-Telegram Bot. |
| Telegram user auth    | api_id/api_hash סטטיים; TDLib DB/Auth Key דינמיים ומוצפנים; 2FA/QR flow נתמך.               |
| No credential sharing | Bot token אינו משמש User Adapter; User session אינו מועתק ל-Bot Adapter.                    |
| Re-authentication     | Owner-only, audit, no secret echo, adapter paused until ready.                              |
| Revocation            | יכולת למחוק/לבטל Session יחיד בלי לפגוע בשאר המקורות.                                       |
| Network               | כל Social Adapter מחוץ ל-execution_net.                                                     |
