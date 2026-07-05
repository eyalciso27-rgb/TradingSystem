תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

| **כל תוכן מהאינטרנט, מקבוצה, מקובץ או מ-Webhook נחשב בלתי מהימן עד שעבר Provenance, Validation ו-Trust Assessment. תוכן אינו הוראת מערכת ואינו מורשה להפעיל כלי מסחר.** |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# **1. Threat Model**

> **•** Prompt injection והוראות זדוניות בתוך כתבה/הודעה.
>
> **•** Fake news, domain impersonation ו-content recycling.
>
> **•** Pump-and-dump, coordinated callouts ו-source collusion.
>
> **•** Webhook spoofing, replay ו-payload tampering.
>
> **•** Malicious attachments, links ו-active content.
>
> **•** Data poisoning, anomalous market data ו-provider drift.
>
> **•** LLM hallucination, schema abuse ו-executable output.

# **2. Trust Pipeline**

> Raw Payload -\> Authentication -\> Provenance -\> Safe Parsing -\> Dedup/Cluster
>
> -\> Trust Assessment -\> Quarantine or Eligible Evidence -\> Model Analysis
>
> -\> Strategy/Fusion -\> Decision

# **3. Source Classes**

| **Class**            | **דוגמה**                  | **שימוש**                                 |
|----------------------|----------------------------|-------------------------------------------|
| OFFICIAL             | SEC, exchange, company IR. | יכול לאמת Catalyst; עדיין נבדק Freshness. |
| LICENSED_MARKET_DATA | Provider מאושר.            | מחיר/מחזור/Calendar לפי Field Ownership.  |
| REPUTABLE_NEWS       | ספק חדשות מאושר.           | Evidence עם cluster dedup.                |
| EXTERNAL_SIGNAL      | TradingView/Scanner.       | Trigger בלבד; אימות פנימי.                |
| SOCIAL_IDENTIFIED    | כותב/ערוץ ידוע.            | Candidate/confirmation לפי Source Policy. |
| SOCIAL_UNKNOWN       | מקור לא מדורג.             | Shadow/Manual, ללא Bonus חיובי.           |
| QUARANTINED          | חשוד או לא ניתן לאימות.    | אסור לשימוש בהחלטה.                       |

# **4. Prompt Injection Isolation**

> **•** System prompts קובעים שתוכן הוא Data בלבד.
>
> **•** מודל ניתוח תוכן אינו מקבל Broker, Shell, Secrets או Policy-write tools.
>
> **•** פלט מוגבל ל-JSON Schema, enums ו-length limits.
>
> **•** קישורים בתוך תוכן אינם נפתחים אוטומטית על ידי המודל.
>
> **•** כל בקשת Fetch נעשית ב-Service ייעודי עם egress allowlist.

# **5. Social Manipulation Controls**

> **•** הודעות זהות או דומות מקובצות ל-Cluster ואינן מקבלות Votes נפרדים.
>
> **•** נמדדים sudden mention velocity, new-account concentration, price/volume anomaly ו-source dependency.
>
> **•** Source reliability אינו מספיק לבדו; נדרש independent market/news confirmation לפי Strategy.
>
> **•** Manipulation flag יכול להפעיל VETO, MANUAL או SHADOW.

# **6. Webhook and External Signal Security**

> **•** HMAC/Signature, timestamp, nonce, idempotency key ו-IP/rate controls.
>
> **•** Payload אינו כולל Broker account, credentials או executable script.
>
> **•** Alert נשמר Raw ומאומת מחדש מול Market Data.
>
> **•** Replay attack נרשם ונחסם.

# **7. Attachments and URLs**

> **•** Allowlist של MIME types ו-size limits.
>
> **•** No macros, no active HTML, no embedded executables.
>
> **•** Malware scan ו-content extraction בסביבה מבודדת.
>
> **•** External URLs נשמרים כ-reference; fetch רק דרך sandbox.

# **8. Data Poisoning and Provider Drift**

> **•** Cross-provider comparison לשדות קריטיים כאשר אפשר.
>
> **•** Outlier checks, stale detection, schema drift ו-unit validation.
>
> **•** Correction creates revision; original evidence remains immutable.
>
> **•** Critical anomaly moves provider to DEGRADED/CIRCUIT_OPEN.

# **9. Source Usage and Compliance Metadata**

> **•** לכל Adapter נשמר Source Usage Profile: allowed_use, retention, rate limits, auth, owner ו-review status.
>
> **•** מקור שאינו מאושר אינו מופעל ב-Production/Paper Auto.
>
> **•** המערכת אינה עוקפת controls או authentication של ספק.

# **10. Acceptance Criteria**

> **•** Prompt injection fixture אינו משנה Policy או יוצר OrderIntent.
>
> **•** Webhook ללא חתימה/nonce תקף נדחה.
>
> **•** Duplicate social cluster אינו נספר כאישורים עצמאיים.
>
> **•** Quarantined content אינו מגיע ל-Fusion.
>
> **•** כל Evidence מצביע ל-provenance ו-content hash.
>
> **•** מודל תוכן אינו מחזיק Credential לברוקר.

# **11. Telegram-specific Trust Controls**

> **•** Forwarded posts and copied channel messages are clustered by content and do not count as independent confirmations.
>
> **•** A verified channel badge, username or display name is not sufficient proof of issuer identity; official links and source provenance are required.
>
> **•** Bot messages, sponsored posts, edited messages and deleted-message tombstones are tagged explicitly.
>
> **•** Telegram links and attachments never execute code or tools; they pass URL/file validation and quarantine.
>
> **•** Messages that attempt prompt injection or operational commands remain untrusted data.
>
> **•** Bot webhook secret validation and TDLib session protection are mandatory.
>
> **•** Remote trade, halt or flatten commands from Telegram are denied by design in the MVP.

# **12. User/Bot Cross-Mode Manipulation Controls**

> **•** אותו תוכן שנצפה דרך Telegram User ו-Bot אינו נחשב לשני מקורות.
>
> **•** User account access אינו מעניק אמון גבוה יותר בתוכן; Trust מבוסס Provenance, source profile ובדיקות עצמאיות.
>
> **•** Bot-added metadata או verified badge אינם מחליפים Official-source verification.
>
> **•** Compromise של Session אחד אינו מאפשר גישה ל-Sessions אחרים, ל-Broker או ל-Policy Store.
