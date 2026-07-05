תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

| **עקרון מחייב: Interactive Brokers הוא הברוקר הראשון בלבד. אף טיפוס נתונים, כלל אסטרטגיה, שירות ליבה או מסך לא יהיו תלויים ב-IBKR. כל תלות בברוקר נשארת בתוך Broker Adapter ו-Execution Agent. מעבר לברוקר אחר יתבצע באמצעות Adapter חדש, מיפוי יכולות והרצת אותה חבילת בדיקות תאימות.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 0. מסמכי בסיס וגבולות החלטה

| **מסמך**                                    | **תפקיד**                              |
|---------------------------------------------|----------------------------------------|
| Decision Contract v0.8                      | חוזי החלטה, סיכון, פקודות ויציאות.     |
| System Functional Specification v0.7        | התנהגות המוצר ומסכים.                  |
| Data Model and Retention Specification v0.6 | מסד הנתונים, אחסון ו-Retention.        |
| Integration Specification v0.6              | חיבורי מקורות, AI, Market Data ו-IBKR. |
| Security and Operations Specification v0.6  | אבטחה, תפעול, גיבוי ו-Kill Switch.     |
| Strategy and Decision Framework v0.5        | אסטרטגיות, כניסות, יציאות ולמידה.      |
| MVP Scope v0.6                              | גבולות הגרסה הראשונה.                  |

> **•** המסמך בוחר טכנולוגיות ל-MVP, אך אינו מאשר עדיין כתיבת המערכת.
>
> **•** גרסאות Patch ינעלו בקובצי lock; גרסאות Major יישארו בטווחים מאושרים.
>
> **•** כל טכנולוגיה חייבת לתמוך ב-Linux ARM64 או להיות מבודדת במארח Execution נפרד.
>
> **•** פשטות תפעולית על שרת יחיד עדיפה על פיצול למיקרו-שירותים רבים.
>
> **•** התכנון שומר אפשרות להתרחבות, אך אינו מכניס Kubernetes, Kafka או Temporal ל-MVP.

# 1. סגנון הארכיטקטורה

| **הבחירה היא Modular Monolith עם Workers ו-Adapters נפרדים, לא Microservices מלאים. הגבולות הלוגיים נשמרים בחבילות ובחוזים, בעוד שמספר התהליכים וה-Containers נשאר מצומצם.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **רכיב**          | **אחריות**                                                                 |
|-------------------|----------------------------------------------------------------------------|
| Core API          | REST, SSE, Auth, Dashboard API, Queries ו-Commands.                        |
| Core Worker       | Entity, Enrichment, Features, Strategy, Decision, Risk ו-Review.           |
| Scheduler         | Jobs מתוזמנים, Backfill, Retention ותחזוקה.                                |
| Connector Runtime | WhatsApp, Discord, Telegram, InvestingPro ו-Connectors תלויי Browser/Node. |
| Execution Agent   | Broker Adapter, פקודות, Events ו-Reconciliation.                           |
| Dashboard         | ממשק Next.js.                                                              |
| Data Services     | PostgreSQL, NATS JetStream ו-Object Storage Adapter.                       |
| Observability     | Prometheus, Grafana, Loki, Alloy ו-Alertmanager.                           |

> **•** אין קריאות ישירות בין מודולי Domain ל-SDK חיצוני.
>
> **•** כל תקשורת אסינכרונית עוברת דרך Canonical Event Contracts.
>
> **•** Queries סינכרוניים עוברים דרך API או Repository interfaces.
>
> **•** המערכת יכולה לפצל Module לשירות עצמאי בעתיד בלי לשנות את החוזה העסקי.

# 2. החלטות טכנולוגיה מרכזיות

| **ADR** | **נושא**            | **החלטה**                                          | **נימוק**                                                                                  |
|---------|---------------------|----------------------------------------------------|--------------------------------------------------------------------------------------------|
| ADR-001 | שפת ליבת המסחר      | Python 3.13.x                                      | אקוסיסטם נתונים, AI, IBKR וכתיבה אסינכרונית; נבחר ענף בוגר ולא Python 3.14 בתחילת הפרויקט. |
| ADR-002 | Backend API         | FastAPI + Pydantic                                 | Typed contracts, validation, async I/O ו-OpenAPI.                                          |
| ADR-003 | Database Access     | SQLAlchemy 2.x Async + Alembic                     | Repository pattern, migrations ו-PostgreSQL.                                               |
| ADR-004 | Connector Runtime   | Node.js 24 LTS + TypeScript                        | Browser automation ואקוסיסטם Connectors; מבודד מה-Domain.                                  |
| ADR-005 | Frontend            | Next.js App Router + TypeScript                    | Dashboard מודרני, SSR/Client components ו-self-hosting.                                    |
| ADR-006 | Database            | PostgreSQL 18.x                                    | מקור האמת העסקי, transactions, JSONB ו-partitioning.                                       |
| ADR-007 | Event Bus           | NATS JetStream                                     | Persistence, replay, durable consumers, work queues ו-deduplication.                       |
| ADR-008 | Object Storage      | Filesystem Content-Addressed Adapter ב-MVP         | פשוט לשרת יחיד; החלפה עתידית ל-OCI/S3 דרך Interface.                                       |
| ADR-009 | Deployment          | Docker Compose על Oracle ARM                       | מתאים לשרת יחיד ומאפשר Profiles/overrides.                                                 |
| ADR-010 | Observability       | Prometheus + Grafana + Loki + Alloy + Alertmanager | Metrics, logs, dashboards ו-alerting עצמי.                                                 |
| ADR-011 | Secrets             | SOPS + age; runtime file secrets                   | Secrets מוצפנים מחוץ לקוד, בלי להפעיל Vault כבד ב-MVP.                                     |
| ADR-012 | Broker Architecture | Broker Port + Adapter + Conformance Suite          | IBKR הוא Plug-in ראשון ולא תלות של הליבה.                                                  |

# 3. גרסאות ו-Dependency Policy

| **רכיב**      | **גרסה**         | **מדיניות**                                                 |
|---------------|------------------|-------------------------------------------------------------|
| Python        | 3.13.x           | Pin ל-Patch מאושר; שדרוג 3.14 רק אחרי Compatibility Matrix. |
| Node.js       | 24 LTS           | Production משתמש רק LTS; Pin באמצעות .nvmrc/Volta/Corepack. |
| PostgreSQL    | 18.x             | Pin ל-Minor מאובטח; אין שימוש ב-Beta/RC.                    |
| SQLAlchemy    | 2.x              | טווח נעול ב-lockfile; Async API.                            |
| Next.js       | Major יציב נתמך  | Pin Major ו-lockfile; אין Auto-upgrade.                     |
| NATS Server   | Major יציב נתמך  | JetStream enabled; Pin image digest.                        |
| Docker Images | Immutable digest | אין שימוש ב-latest.                                         |

> **•** Python packages ינוהלו באמצעות uv ו-uv.lock.
>
> **•** Node packages ינוהלו באמצעות pnpm workspace ו-pnpm-lock.yaml.
>
> **•** Dependabot/Renovate רשאי להציע עדכונים, אך לא למזג או לפרוס אוטומטית.
>
> **•** עדכון Dependency המשפיע על Broker, Parser, Decision או Risk דורש Replay/Contract Tests.

# 4. Repository ו-Monorepo

> trading-system/
>
> apps/
>
> api/ \# FastAPI
>
> core-worker/ \# Python domain workers
>
> scheduler/ \# Python scheduled jobs
>
> connectors/ \# Node/TypeScript adapters
>
> dashboard/ \# Next.js
>
> execution-agent/ \# Python broker runtime
>
> packages/
>
> py/
>
> contracts/
>
> domain/
>
> application/
>
> config/
>
> persistence/
>
> event-bus/
>
> market-data-core/
>
> broker-core/
>
> broker-ibkr/
>
> observability/
>
> ts/
>
> contracts-generated/
>
> connector-sdk/
>
> ui/
>
> config/
>
> assumptions/
>
> strategies/
>
> policies/
>
> prompts/
>
> sources/
>
> market-data/
>
> brokers/
>
> database/
>
> alembic/
>
> seeds/
>
> deploy/
>
> compose.yaml
>
> compose.dev.yaml
>
> compose.paper.yaml
>
> compose.observability.yaml
>
> tests/
>
> fixtures/
>
> contract/
>
> integration/
>
> replay/
>
> e2e/
>
> docs/
>
> **•** Python הוא מקור האמת ל-JSON Schemas באמצעות Pydantic models.
>
> **•** OpenAPI ו-JSON Schema מייצרים Types ל-TypeScript; אין שכפול ידני של Contracts.
>
> **•** Domain אינו מייבא packages של ibapi, Playwright, NATS client או FastAPI.
>
> **•** Adapter packages תלויים ב-Core interfaces, אך Core אינו תלוי בהם.

# 5. Backend ולוגיקת Domain

| **רכיב**                     | **שימוש**                                                      |
|------------------------------|----------------------------------------------------------------|
| FastAPI                      | HTTP API, Auth, SSE ו-OpenAPI בלבד; אין לוגיקת מסחר ב-Routers. |
| Pydantic                     | Contracts, config validation ו-structured AI outputs.          |
| SQLAlchemy Async             | Repositories ו-transactions.                                   |
| Alembic                      | Schema migrations; forward/rollback plan.                      |
| httpx                        | External HTTP clients.                                         |
| structlog / standard logging | JSON structured logs עם redaction.                             |
| Decimal                      | כל כסף, מחיר, אחוז וכמות שאינה שלמה; אין float לחישובי מסחר.   |
| UTC datetime                 | אחסון UTC בלבד; אזור זמן רק בתצוגה.                            |

## 5.1 גבולות Transaction

> **•** Decision Snapshot, Decision Result ו-Outbox Event נכתבים באותה Transaction.
>
> **•** OrderIntent ו-Outbox Event נכתבים באותה Transaction לפני פרסום ל-NATS.
>
> **•** Broker Event נשמר תחילה Append-only ורק אחר כך מעדכן Projection/Position.
>
> **•** אין distributed transaction בין PostgreSQL, NATS והברוקר; משתמשים ב-Outbox, Idempotency ו-Reconciliation.

# 6. Event Bus ו-Workflow

| **PostgreSQL נשאר מקור האמת. NATS JetStream הוא תשתית העברה ועבודה, לא המקום היחיד שבו נשמרת היסטוריית החלטה.** |
|-----------------------------------------------------------------------------------------------------------------|

| **Stream**    | **Subjects**                                                                  | **Retention**   | **תפקיד**                       |
|---------------|-------------------------------------------------------------------------------|-----------------|---------------------------------|
| EVENTS        | events.source.\*, events.candidate.\*, events.decision.\*, events.position.\* | Interest/Limits | אירועי Domain להפצה.            |
| WORK          | work.normalize, work.enrich, work.analyze, work.review                        | WorkQueuePolicy | משימות Workers.                 |
| ORDERS        | orders.paper.intent, orders.paper.cancel                                      | WorkQueuePolicy | פקודות Paper ל-Execution Agent. |
| BROKER_EVENTS | broker.paper.order.\*, broker.paper.fill.\*, broker.paper.account.\*          | Limits          | Events מהברוקר.                 |
| DLQ           | dlq.\*                                                                        | Limits          | כשלי עיבוד הדורשים Review.      |

> **•** Delivery semantics הן at-least-once; כל Handler חייב Idempotency.
>
> **•** JetStream message id משמש Dedup בנוסף למפתחות העסקיים.
>
> **•** Consumers הם durable עם MaxDeliver ו-Ack מפורש.
>
> **•** Replay מחקרי מתבצע בעיקר מ-PostgreSQL Event Store; JetStream משמש Operational replay קצר.

## 6.1 Transactional Outbox

> Database transaction:
>
> 1\. persist domain state
>
> 2\. persist outbox_event(status=PENDING)
>
> 3\. commit
>
> Outbox publisher:
>
> 4\. publish to NATS with event_id as message-id
>
> 5\. mark outbox_event=PUBLISHED
>
> Consumer:
>
> 6\. check processed_event(event_id, consumer)
>
> 7\. apply idempotently
>
> 8\. persist processed_event
>
> 9\. ACK

# 7. PostgreSQL

> **•** PostgreSQL 18.x כ-Primary Store יחיד ב-MVP.
>
> **•** Native partitioning לטבלאות Raw Events, Market Snapshots, Audit ו-Broker Events.
>
> **•** JSONB מותר ל-Payload גרסאי, אך שדות חיפוש/יחסים נשמרים בעמודות typed.
>
> **•** SCRAM authentication; אין משתמש Application שהוא superuser.
>
> **•** Connection pooling באמצעות SQLAlchemy pool; PgBouncer נדחה עד צורך מוכח.
>
> **•** Backups: pg_dump/base backup ו-WAL/PITR לפי מסמך התפעול.

# 8. Object Storage

| **תחום**           | **החלטה**                                                                 |
|--------------------|---------------------------------------------------------------------------|
| MVP implementation | Encrypted/protected filesystem volume with content-addressed paths.       |
| Metadata           | PostgreSQL stores hash, MIME, size, retention, encryption and object key. |
| Write rule         | Write temp -\> fsync -\> atomic rename -\> DB reference.                  |
| Integrity          | SHA-256 verification on write/read/backup.                                |
| Future             | OCI Object Storage or S3 adapter without changing domain contracts.       |

# 9. Frontend ו-API

| **טכנולוגיה**            | **שימוש**                                                 |
|--------------------------|-----------------------------------------------------------|
| Next.js App Router       | Dashboard, layouts, server/client components.             |
| TypeScript strict        | ללא any לא מבוקר.                                         |
| Node 24 LTS              | Runtime ו-build.                                          |
| Tailwind CSS + shadcn/ui | RTL ועיצוב מודרני.                                        |
| TanStack Query           | Server state, cache ו-mutations.                          |
| Generated API types      | OpenAPI/JSON Schema מה-Backend.                           |
| REST                     | Commands ו-Queries.                                       |
| SSE                      | Event feed, health, decisions, orders ו-position updates. |
| Playwright               | E2E UI tests.                                             |
| Vitest                   | Unit/component tests.                                     |

> **•** אין GraphQL ב-MVP.
>
> **•** אין Server Actions לפעולות מסחר קריטיות; הן עוברות API מפורש, Auth, CSRF/Origin ו-Audit.
>
> **•** כל פעולה רגישה מציגה Environment ו-Confirmation context.
>
> **•** UI אינו מייצר Order; הוא שולח Approval/Command בלבד.

# 10. Connector Runtime

> **•** Node.js 24 LTS + TypeScript עבור Browser/Web connectors שבהם האקוסיסטם מתאים יותר.
>
> **•** Playwright הוא מנגנון הדפדפן המועדף ל-InvestingPro ולבדיקות Browser adapters.
>
> **•** כל Connector מפיק Canonical SourceEvent בלבד ואינו ניגש לטבלאות Decision/Order ישירות.
>
> **•** Connector SDK משותף מספק Health, Retry, Circuit Breaker, Raw storage ו-NATS publisher.
>
> **•** אם Connector מסוים יפותח ב-Python, הוא חייב לעמוד באותו Contract; השפה אינה חלק מה-Domain.

# 11. Broker Abstraction - דרישה מחייבת

| **IBKR אינו מופיע בשום Signature של Strategy, Decision, Risk, Position Manager או Dashboard. רק BrokerAdapter ו-BrokerInstrumentMapping מכירים מזהי IBKR, conid, clientId או Order Status ייחודי.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 11.1 Broker Port

> interface BrokerAdapter:
>
> adapter_id: str
>
> adapter_version: str
>
> connect() -\> None
>
> disconnect() -\> None
>
> capabilities() -\> BrokerCapabilities
>
> resolve_instrument(query: InstrumentQuery) -\> BrokerInstrument
>
> get_account_snapshot(account_alias: str) -\> AccountSnapshot
>
> get_positions(account_alias: str) -\> list\[BrokerPosition\]
>
> get_open_orders(account_alias: str) -\> list\[BrokerOrder\]
>
> preview_order(intent: OrderIntent) -\> OrderPreview
>
> submit_order(intent: OrderIntent) -\> BrokerOrderReceipt
>
> modify_order(request: ModifyOrderRequest) -\> BrokerOrderReceipt
>
> cancel_order(request: CancelOrderRequest) -\> CancelReceipt
>
> stream_events() -\> AsyncIterator\[NormalizedBrokerEvent\]
>
> reconcile(account_alias: str) -\> ReconciliationResult

## 11.2 טיפוסים קנוניים

| **טיפוס**             | **תוכן**                                                                        |
|-----------------------|---------------------------------------------------------------------------------|
| Instrument            | internal_instrument_id, symbol, exchange, currency, asset_class.                |
| OrderIntent           | side, quantity, canonical_order_type, limit/stop, tif, protection, environment. |
| BrokerCapabilities    | assets, order types, sessions, fractional, bracket, OCO, modify semantics.      |
| BrokerOrderReceipt    | internal intent id, external order id, accepted/rejected, timestamps.           |
| NormalizedBrokerEvent | ACK, REJECT, PARTIAL_FILL, FILL, CANCEL, EXPIRE, ERROR.                         |
| ReconciliationResult  | differences, unknown orders, missing positions, recommended halt.               |

## 11.3 איסורים ארכיטקטוניים

> **•** אין conid, permId, clientId או IBKR status בתוך Domain models.
>
> **•** אין קריאה ל-ibapi מחוץ ל-package broker-ibkr.
>
> **•** אין Strategy שבודקת "אם הברוקר הוא IBKR".
>
> **•** אין מסך שמניח שמות שדות או קודי פקודה של IBKR.
>
> **•** אין שמירת External Broker ID במקום Internal ID; שומרים את שניהם במיפוי.
>
> **•** אין שימוש בנתוני שוק של הברוקר מתוך BrokerAdapter; MarketDataProvider הוא Port נפרד.

## 11.4 Capability Negotiation

> SystemTradePlan
>
> -\> OrderIntentBuilder
>
> -\> BrokerCapabilities check
>
> -\> SUPPORTED: create canonical OrderIntent
>
> -\> EMULATABLE: create explicit execution plan if policy allows
>
> -\> UNSUPPORTED: reject with ORDER_NOT_SUPPORTED
>
> -\> BrokerAdapter
>
> **•** המערכת אינה מניחה שכל ברוקר תומך Bracket, OCO, fractional shares או modify באותה צורה.
>
> **•** Emulation מותרת רק באמצעות Policy מפורשת ולא כברירת מחדל.
>
> **•** לפני מעבר לברוקר חדש מריצים Broker Conformance Suite מלאה ב-Sandbox/Paper.

## 11.5 מסלול הוספת ברוקר חדש

| **שלב** | **שם**              | **פעולה**                                               |
|---------|---------------------|---------------------------------------------------------|
| 1       | Implement Adapter   | מימוש Broker Port ללא שינוי Domain.                     |
| 2       | Instrument Mapping  | מיפוי Internal Instrument למזהה הברוקר.                 |
| 3       | Capability Manifest | הצהרת נכסים, פקודות, Sessions ומגבלות.                  |
| 4       | Contract Tests      | אותה חבילת Broker Conformance Tests.                    |
| 5       | Mock/Replay         | הרצת Fixtures ו-Broker Events.                          |
| 6       | Sandbox/Paper       | פקודות, ביטולים, Partial Fills ו-Reconciliation.        |
| 7       | Shadow Comparison   | השוואת Preview/quotes/executions ללא מעבר Production.   |
| 8       | Promotion           | Broker Config חדש ו-Active Pointer; אין שינוי קוד ליבה. |

# 12. IBKR Adapter הראשון

> **•** מימוש ראשון: Python Execution Agent עם TWS API דרך IB Gateway או TWS.
>
> **•** ה-Adapter מקבל OrderIntent קנוני ומתרגם אותו ל-Contract/Order של IBKR.
>
> **•** Callbacks של IBKR מומרים ל-NormalizedBrokerEvent.
>
> **•** Reconciliation קורא Account, Positions, Open Orders ו-Executions.
>
> **•** Paper credentials ו-Live credentials מופרדים; ב-MVP נטען רק Paper Adapter.
>
> **•** בחירת המארח הפיזי ל-IB Gateway/TWS נשארת מותנית ב-Proof of Integration; הגבול הלוגי כבר נעול.

## 12.1 Package Boundary

> packages/py/broker-core/
>
> models.py
>
> ports.py
>
> conformance.py
>
> mock.py
>
> packages/py/broker-ibkr/
>
> adapter.py
>
> connection.py
>
> instrument_mapper.py
>
> order_mapper.py
>
> event_normalizer.py
>
> reconciliation.py
>
> errors.py

# 13. Market Data Port

> **•** Market Data הוא Port עצמאי לחלוטין מהברוקר.
>
> **•** IBKR יכול להיות Provider ראשון או Fallback, אך Strategy רואה MarketSnapshot קנוני בלבד.
>
> **•** Provider ID, external instrument ID ו-entitlements נשמרים בטבלת mapping.
>
> **•** החלפת Provider נעשית בקונפיגורציה וב-Active Pointer.
>
> **•** Benchmark מחייב Coverage, P95 latency, gaps, limits, cost ו-Pre/After support.

# 14. Authentication ו-Secrets

| **תחום**         | **החלטה**                                                                                        |
|------------------|--------------------------------------------------------------------------------------------------|
| Application Auth | Local single-owner account in PostgreSQL; Argon2id password hash; TOTP MFA; server-side session. |
| Authorization    | RBAC middleware ב-FastAPI; action-level permissions.                                             |
| CSRF/Origin      | SameSite secure cookies, Origin checks ו-CSRF token לפעולות state-changing.                      |
| Secrets at Rest  | SOPS + age encrypted files; age private key מחוץ ל-Git.                                          |
| Runtime Secrets  | Docker Compose secrets / read-only mounted files.                                                |
| Future           | SecretsProvider interface מאפשר OCI Vault/OpenBao ללא שינוי Consumers.                           |

# 15. Observability Stack

| **רכיב**           | **תפקיד**                                                          |
|--------------------|--------------------------------------------------------------------|
| OpenTelemetry SDK  | Tracing/metrics context בכל שירות.                                 |
| Prometheus         | Scrape metrics ושמירת time series.                                 |
| Grafana            | Dashboards וחקירה.                                                 |
| Alertmanager       | Routing, grouping ו-silencing של alerts.                           |
| Loki single-binary | אחסון לוגים ב-MVP.                                                 |
| Grafana Alloy      | איסוף לוגים/metrics; לא Promtail.                                  |
| Exporters          | node exporter, cAdvisor, postgres exporter, NATS metrics לפי צורך. |

> **•** Logs הם JSON ומוגבלים בנפח/Retention.
>
> **•** High-cardinality values כמו order_id אינם Labels של Prometheus/Loki; הם נשארים בשדה Log.
>
> **•** Correlation ID, decision_id ו-order_intent_id מאפשרים Trace end-to-end.
>
> **•** Observability מופעל ב-Compose profile נפרד אך חובה ב-Paper Manual ומעלה.

# 16. Deployment Topology

> Oracle ARM Server
>
> ├── nginx-proxy-manager (existing)
>
> ├── dashboard \[Node 24 / Next.js\]
>
> ├── api \[Python 3.13 / FastAPI\]
>
> ├── core-worker \[Python 3.13\]
>
> ├── scheduler \[Python 3.13\]
>
> ├── connectors \[Node 24 / TypeScript\]
>
> ├── postgresql \[18.x\]
>
> ├── nats \[JetStream\]
>
> ├── object-volume
>
> ├── prometheus
>
> ├── alertmanager
>
> ├── grafana
>
> ├── loki
>
> └── alloy
>
> Execution Host (physical location after PoC)
>
> ├── IB Gateway or TWS
>
> ├── execution-agent-paper \[Python 3.13\]
>
> └── secure NATS connection / private network

## 16.1 Docker Networks

| **Network**   | **שירותים**                      | **כלל**                          |
|---------------|----------------------------------|----------------------------------|
| edge          | NPM, dashboard, api              | רק API/Dashboard נחשפים דרך NPM. |
| app           | api, workers, connectors, nats   | רשת פנימית.                      |
| data          | postgres, nats, object access    | ללא host ports ציבוריים.         |
| observability | prometheus, grafana, loki, alloy | גישה מוגבלת.                     |
| execution     | NATS endpoint/agent tunnel       | מבודד ומוצפן.                    |

## 16.2 Compose Files

> compose.yaml \# common services
>
> compose.dev.yaml \# bind mounts, debug, mocks
>
> compose.paper.yaml \# paper configuration and safety guards
>
> compose.observability.yaml \# metrics/logging stack
>
> compose.backup.yaml \# backup jobs
>
> \# no compose.live.yaml in MVP repository activation path

# 17. CI/CD ו-Artifact Management

> **•** GitHub Actions מריץ lint, type checks, unit, contract, integration ו-build.
>
> **•** Images נבנים ל-linux/arm64 עבור Oracle; Execution Agent נבנה גם לארכיטקטורת המארח שנבחר.
>
> **•** Images נשמרים ב-GHCR עם semantic version ו-digest.
>
> **•** אין Deploy אוטומטי ל-Paper ללא Approval.
>
> **•** Database migration רצה כ-Job מפורש לפני services.
>
> **•** SBOM ו-vulnerability scan נשמרים כחלק מה-build.
>
> **•** Release artifact כולל image digests, config schema versions ו-migration revision.

# 18. Testing Toolchain

| **סוג**        | **כלים/מטרה**                                             |
|----------------|-----------------------------------------------------------|
| Python Unit    | pytest, pytest-asyncio, Hypothesis.                       |
| Python Quality | Ruff, Pyright/Mypy, Bandit לפי החלטה סופית.               |
| Database       | Testcontainers/PostgreSQL, Alembic migration tests.       |
| NATS           | Ephemeral JetStream in integration tests.                 |
| Contracts      | JSON Schema compatibility and generated TS type checks.   |
| Connectors     | Vitest + Playwright fixtures.                             |
| Frontend       | Vitest + Playwright E2E.                                  |
| Broker         | Broker Mock + IBKR Paper conformance.                     |
| Replay         | Golden Decisions and deterministic snapshots.             |
| Chaos          | disconnect, duplicate, stale data, partial fill, restart. |

# 19. Resource Budget לשרת Oracle

| **רכיב**                      | **תקציב ראשוני**    | **הערה**                                 |
|-------------------------------|---------------------|------------------------------------------|
| PostgreSQL                    | 2-4 GB RAM baseline | Tune after measurements.                 |
| NATS JetStream                | 0.5-1 GB            | Disk-backed streams with limits.         |
| API + Core Worker + Scheduler | 2-4 GB              | Concurrency controlled.                  |
| Connectors/Browser            | 2-6 GB              | Browser count limited; largest variable. |
| Next.js                       | 0.5-1 GB            | Single instance.                         |
| Observability                 | 2-4 GB              | Retention limits; Loki single-binary.    |
| OS/Reserve                    | 4+ GB               | Avoid memory pressure and OOM.           |

> **•** אין להריץ Local LLM כבד כחלק מ-MVP.
>
> **•** Browser workers מקבלים concurrency limit קשיח.
>
> **•** Raw files/logs/JetStream מקבלים quotas ו-retention.
>
> **•** הערכים הם Budget ראשוני ויעודכנו לאחר load test.

# 20. טכנולוגיות שנדחות או נשללות ב-MVP

| **טכנולוגיה/גישה**                | **החלטה**                                                        |
|-----------------------------------|------------------------------------------------------------------|
| Kubernetes                        | מורכבות תפעולית לא מוצדקת לשרת יחיד.                             |
| Kafka                             | כבד ביחס לנפח ולשלב; NATS JetStream מספיק.                       |
| Temporal                          | Workflow engine חזק אך מיותר כרגע; state machine + DB + NATS.    |
| Redis                             | לא נדרש ב-MVP; cache מקומי/NATS/PostgreSQL. יתווסף רק לפי מדידה. |
| TimescaleDB                       | Native PostgreSQL partitioning קודם; extension רק אם צורך מוכח.  |
| MinIO                             | Filesystem adapter פשוט יותר; S3 interface נשמר.                 |
| GraphQL                           | REST + SSE מספקים את מקרי השימוש.                                |
| Microservice per agent            | מגדיל כשל ותפעול ללא ערך ב-MVP.                                  |
| Direct broker calls from strategy | אסור ארכיטקטונית.                                                |

# 21. קריטריוני קבלה טכנולוגיים

| **מזהה**   | **קריטריון**                                               |
|------------|------------------------------------------------------------|
| TECH-AC-01 | Broker Mock ו-IBKR Adapter עוברים אותה Conformance Suite.  |
| TECH-AC-02 | הסרת package broker-ibkr אינה שוברת Domain/Decision tests. |
| TECH-AC-03 | שום Domain model אינו מכיל IBKR-specific field.            |
| TECH-AC-04 | MarketDataProvider ניתן להחלפה ללא שינוי Strategy.         |
| TECH-AC-05 | Outbox מונע אובדן Event בין PostgreSQL ל-NATS.             |
| TECH-AC-06 | Consumer restart אינו יוצר עיבוד עסקי כפול.                |
| TECH-AC-07 | ARM64 images נבנים ורצים על Oracle.                        |
| TECH-AC-08 | Execution Agent מתחבר מרחוק בלי לחשוף Broker API ציבורית.  |
| TECH-AC-09 | Secrets אינם קיימים ב-image, Git או logs.                  |
| TECH-AC-10 | Generated TypeScript contracts תואמים ל-Pydantic schemas.  |
| TECH-AC-11 | Full Compose Paper stack עולה ב-command מתועד אחד.         |
| TECH-AC-12 | Restore של DB/Object/JetStream metadata מחזיר מערכת עקבית. |
| TECH-AC-13 | Observability מציגה end-to-end event/order trace.          |
| TECH-AC-14 | Dependency versions pinned and reproducible.               |

# 22. החלטות פתוחות שנותרו

| **מזהה**     | **החלטה**                                                      |
|--------------|----------------------------------------------------------------|
| TECH-OPEN-01 | המארח הפיזי וה-OS של IB Gateway/TWS לאחר Proof of Integration. |
| TECH-OPEN-02 | Market Data Provider הראשי לאחר Benchmark.                     |
| TECH-OPEN-03 | News Provider הראשי.                                           |
| TECH-OPEN-04 | מימוש מדויק של WhatsApp/Discord/Telegram adapters לאחר PoC.    |
| TECH-OPEN-05 | ספריות Auth/TOTP המדויקות לאחר Security review.                |
| TECH-OPEN-06 | יעד Offsite Backup ו-Object Storage עתידי.                     |
| TECH-OPEN-07 | ערוץ Alert לטלפון.                                             |
| TECH-OPEN-08 | Pyright לעומת Mypy; הבחירה תיעשה ב-Implementation Plan.        |

# 23. מקורות תיעוד שנבדקו

> **•** Python.org - Python 3.13/3.14 release status.
>
> **•** Node.js - Release schedule and Node 24 LTS.
>
> **•** PostgreSQL - Versioning policy and supported releases.
>
> **•** FastAPI - async and API documentation.
>
> **•** SQLAlchemy - asyncio documentation.
>
> **•** Next.js - App Router and self-hosting documentation.
>
> **•** Docker - Compose production documentation.
>
> **•** NATS - JetStream persistence, replay and work queues.
>
> **•** IBKR Campus - TWS API, IB Gateway, Paper Trading and Web API.
>
> **•** Prometheus and Grafana Loki documentation.
>
> **•** SOPS documentation for age-encrypted configuration.

# 24. השלב הבא

> • שלב 1: Policy Parameters and Initial Assumptions v0.5 - ספים, TTL, סיכון, Latency ותנאי קידום.
>
> • שלב 2: Proof-of-Integration Plan v0.1 - בדיקות קצרות לפני נעילת מארח IBKR, ספק נתוני שוק וה-Connectors.
>
> • שלב 3: Consolidated Architecture Review - בדיקת סתירות וחוסרים בין כל המסמכים.
>
> • שלב 4: Traceability Matrix - קישור כל דרישה לרכיב, Contract, Test וקריטריון קבלה.
>
> • שלב 5: Claude Code Implementation Plan - רק לאחר אישור מפורש של כל החבילה.

# 30. Baseline טכנולוגי מתוקן

| **הטכנולוגיות נשארות בחירות מועמדות עד Proof of Integration על ARM ועל IBKR. ליבת הדומיין אינה תלויה בגרסה או בספק.** |
|-----------------------------------------------------------------------------------------------------------------------|

## 30.1 Runtime Versions

| **רכיב**       | **החלטה**                                                                       |
|----------------|---------------------------------------------------------------------------------|
| Python         | 3.13.x candidate; pin after PoI. UUIDv7 באמצעות ספריית RFC 9562 חיצונית מאושרת. |
| Node.js        | 24 LTS candidate; pin exact patch after ARM/Playwright PoI.                     |
| PostgreSQL     | 18.x candidate; exact image pinned after migration/backup PoI.                  |
| NATS JetStream | candidate queue; exact version pinned after persistence/recovery PoI.           |
| IBKR API       | Adapter package pinned independently from domain/core.                          |

## 30.2 Cross-Language Contracts

> **•** Python/Pydantic הוא מקור האמת לליבת Contracts.
>
> **•** JSON Schema מופק ונצרך על ידי TypeScript Connectors/Dashboard.
>
> **•** StrategyEvaluator הוא Python Protocol.
>
> **•** CI בודק Schema compatibility ומונע drift.

## 30.3 Messaging Reliability

> PostgreSQL transaction
>
> -\> domain changes + outbox row
>
> Publisher
>
> -\> NATS JetStream
>
> Consumer
>
> -\> consumer_inbox uniqueness check
>
> -\> handler transaction
>
> -\> completed_at/result_hash
>
> **•** Generic subjects: orders.\<environment\>.\<broker\> ו-broker.events.\<environment\>.\<broker\>.

## 30.4 Runtime Session Store

> **•** SOPS/age משמשים ל-Secrets סטטיים בקבצי Deployment.
>
> **•** Runtime Sessions נשמרים במאגר מוצפן עם key version, namespace ו-rotation.
>
> **•** המימוש יכול להיות PostgreSQL encrypted blob או Vault-compatible store; ננעל לאחר PoI.
>
> **•** Connector אינו מקבל root encryption key ישירות אם ניתן להשתמש ב-envelope encryption.

## 30.5 Instrument Master

> **•** packages/py/domain/instruments מגדיר Instrument קנוני.
>
> **•** Mappings לספקי נתונים וברוקרים נשמרים ב-DB.
>
> **•** conid/permId אינם מופיעים מחוץ broker-ibkr ו-mapping records.
>
> **•** Symbol changes הם temporal mappings.

## 30.6 Broker Conformance Suite

> **•** capabilities and mapping
>
> **•** submit/cancel/modify
>
> **•** partial fill and event ordering
>
> **•** unknown state and reconciliation
>
> **•** idempotency
>
> **•** paper/live isolation
>
> **•** bracket/OCO native or emulated behavior

## 30.7 Storage and Backups

> **•** Filesystem Object Adapter נשאר ממשק, לא תלות.
>
> **•** Offsite encrypted backup חובה לפני Paper Auto.
>
> **•** Audit checkpoints נשמרים מחוץ ל-DB failure domain.
>
> **•** Partitioning מופעל לפי thresholds ולא ביום הראשון.

## 30.8 Repository Additions

> packages/py/domain/instruments
>
> packages/py/domain/orders
>
> packages/py/domain/positions
>
> packages/py/strategy-sdk
>
> packages/py/broker-conformance
>
> packages/schemas/json
>
> apps/session-store
>
> apps/outbox-publisher
>
> tests/poi/
>
> tests/point_in_time/

# 31. טכנולוגיית מחקר רב-מודלי ו-UX תפעולי

| **ה-Stack מורחב בשירותי Scanner, Feature, Fundamental, News/NLP, Quant/ML ו-Signal Fusion. כל שירות נשאר מאחורי Ports וחוזי JSON Schema; אין תלות ישירה ב-TradingView, בספק נתונים או ב-IBKR בליבת הדומיין.** |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 31.1 Services/Packages

| **Package/Service**         | **אחריות**                                                         |
|-----------------------------|--------------------------------------------------------------------|
| services/scanner-worker     | Schedules, universes, scanner runs and candidate emission.         |
| packages/technical-engine   | Indicators, patterns, levels, triggers and technical assessments.  |
| packages/fundamental-engine | Normalized fundamentals, scoring, valuation and watchlist rules.   |
| services/news-nlp-worker    | Article/filing clustering, catalyst, materiality and sentiment.    |
| services/quant-ml-worker    | Approved model inference, regime and ranking; no broker access.    |
| packages/signal-fusion      | Manifest evaluation, required/optional/veto and explanation.       |
| services/model-registry     | Artifact metadata, active/challenger pointers and promotion state. |
| connectors/tradingview      | ExternalSignalProvider webhook adapter.                            |
| apps/dashboard              | Operations Console and UX.                                         |

## 31.2 Data/ML Stack

> **•** NumPy/Polars or equivalent approved libraries for deterministic feature calculation; indicator formulas have Golden Tests.
>
> **•** scikit-learn baseline for calibration, classical models and evaluation; gradient boosting libraries are optional challengers after ARM PoI.
>
> **•** Model artifacts stored in Object Storage with hash/signature; metadata and lifecycle in PostgreSQL.
>
> **•** Training jobs run offline and separately from execution; no Broker Credentials in training runtime.
>
> **•** Feature schemas are versioned and shared through JSON Schema/Parquet manifests as appropriate.

## 31.3 Event Subjects

> scanner.run.requested
>
> scanner.result.created
>
> assessment.technical.created
>
> assessment.fundamental.created
>
> assessment.news.created
>
> assessment.quant_ml.created
>
> assessment.external.created
>
> assessment.social.created
>
> fusion.requested
>
> fusion.completed
>
> model.promoted
>
> model.halted

## 31.4 TradingView Webhook

> **•** Dedicated Connector endpoint behind reverse proxy; high-entropy token, rate limiting and idempotency.
>
> **•** Webhook handler writes Raw Event + Outbox and returns quickly; analysis happens asynchronously.
>
> **•** Connector has no database write outside ingestion boundary and no broker network access.

## 31.5 Operations Console

> **•** Next.js UI consumes read models and SSE/WebSocket-compatible event stream; commands use authenticated API endpoints.
>
> **•** Emergency Halt endpoint is a dedicated high-priority command path and does not depend on AI services.
>
> **•** UI supports RTL Hebrew, desktop-first operational layout and responsive emergency access.
>
> **•** Every screen includes persistent environment/mode/kill/broker-sync status strip.

## 31.6 Resource Budget

| **Workload**       | **Deployment rule**                                                            |
|--------------------|--------------------------------------------------------------------------------|
| Technical scanners | Incremental scheduling and bounded universe; no full tick archive in MVP.      |
| Fundamental scans  | Batch off-market hours where possible.                                         |
| NLP/LLM            | Provider APIs with concurrency/cost limits; optional local classical NLP only. |
| ML inference       | Small approved models on CPU; heavy training off the production server.        |
| Dashboard          | Cached read models; no direct analytical joins in browser.                     |

# **23. מודולים נוספים ב-Baseline**

| **Module/Package**      | **אחריות**                                                   |
|-------------------------|--------------------------------------------------------------|
| research-lab            | Replay, Backtest, Simulation ו-Experiment runner.            |
| model-registry          | Artifacts, metrics, calibration, drift ו-approval state.     |
| opportunity-calibration | Funnel, starvation, overtrading ו-counterfactual evaluation. |
| portfolio-engine        | Capital allocation, exposure, conflict ו-correlation.        |
| regime-engine           | Regime snapshots ו-strategy compatibility.                   |
| content-trust           | Provenance, quarantine, dedup ו-adversarial flags.           |
| notification-service    | Channel-agnostic alerts, dedup, escalation ו-ack.            |
| emergency-control       | Minimal authenticated halt/flatten coordinator.              |

## **23.1 עקרון פריסה**

> **•** המודולים הם גבולות לוגיים; אין חובה להפוך כל אחד למיקרו-שירות ב-MVP.
>
> **•** ברירת המחדל נשארת Modular Monolith עם Workers נפרדים רק כאשר נדרש בידוד תפעולי.
>
> **•** Artifacts ו-Dataset snapshots נשמרים ב-Object Storage; Metadata ב-PostgreSQL.
>
> **•** אין Feature Store חיצוני ב-MVP; Feature snapshots קנוניים נשמרים במסד/אחסון גרסאי.
>
> **•** נדרש Build reproducible, lockfiles, hashes ו-runtime versions שעברו ARM PoI.

## **23.2 שירות חירום**

> **•** Endpoint מינימלי נפרד מה-Frontend הראשי, עם scope מצומצם ל-HALT/STATUS בלבד.
>
> **•** CLI מקומי מאומת יכול להפעיל Halt גם כאשר Dashboard אינו זמין.
>
> **•** FLATTEN דורש מסלול נפרד, Preview ו-strong re-authentication.

# **32. פריסת Telegram ב-Docker**

| **רכיב**               | **החלטה**                                                                           |
|------------------------|-------------------------------------------------------------------------------------|
| telegram-bot-adapter   | Container קל משקל ל-Bot API; Webhook או Long Polling לפי PoI.                       |
| telegram-user-adapter  | Container נפרד ל-TDLib; מופעל רק אם נדרש חשבון משתמש.                               |
| source profile         | כל Chat/Channel משויך ל-Adapter mode אחד בלבד.                                      |
| runtime session volume | Volume מוצפן ונפרד עבור TDLib.                                                      |
| network                | application/data/observability בלבד; ללא execution_net.                             |
| public endpoint        | רק Bot webhook מאומת דרך Reverse Proxy; TDLib אינו נחשף.                            |
| event subjects         | source.telegram.message, source.telegram.channel_post, integration.telegram.health. |
| resource limits        | CPU/RAM/Storage נמדדים ב-PoI; Restart policy עם backoff.                            |

## **32.1 מבנה Repository**

> **•** apps/connectors/telegram-bot/
>
> **•** apps/connectors/telegram-user/
>
> **•** packages/contracts/telegram/
>
> **•** tests/contract/telegram/
>
> **•** tests/integration/telegram/
>
> **•** fixtures/telegram/

## **32.2 החלטת Runtime**

| אין נועלים ספריית Client לפני PoI. Bot API משתמש ב-HTTP הרשמי. חיבור חשבון משתמש באמצעות TDLib הרשמי הוא חובה ועיקרי; בנייה והרצה על ARM64 נבדקות לפני קיבוע Image. |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## **32.3 Notification Channel**

> **•** Notification Service רשאי להשתמש ב-Bot נפרד מ-Bot הקליטה.
>
> **•** Token התראות מופרד מ-Token קליטה.
>
> **•** הודעות Critical דורשות dedup, correlation_id ו-acknowledgement בממשק.
>
> **•** אין parsing של תשובת המשתמש כפקודת מסחר ב-MVP.

# **33. Docker topology לחיבורי משתמש**

| **דרישת Baseline מחייבת: WhatsApp, Discord ו-Telegram חייבים לכלול Adapter הפועל באמצעות החשבון האישי של המשתמש. ב-Telegram יתווסף גם Bot API כאפשרות שנייה ומשלימה. Bot אינו מחליף, אינו דוחה ואינו מבטל את מימוש USER_ACCOUNT_TDLIB. הדרישה היא יכולת יישום חובה; בזמן ריצה ניתוק מקור חברתי מסוים אינו עוצר את הליבה האוטונומית אלא אם Strategy Manifest מסוים דורש אותו.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

> connectors:
>
> whatsapp-user-adapter: required
>
> discord-user-adapter: required
>
> telegram-user-adapter: required_primary
>
> telegram-bot-adapter: supplemental
>
> **•** כל Adapter הוא Container נפרד עם Healthcheck, resource limits ו-Volume/secret scope נפרד.
>
> **•** הרצת Telegram User משתמשת ב-TDLib ומאומתת על ARM64; אם PoI נכשל על ARM, ה-Container יועבר ל-host x86 מבודד בלי שינוי Contracts.
>
> **•** Bot API אינו משמש fallback שקט ל-TDLib ואינו מסמן את דרישת User Adapter כ-Done.
>
> **•** Notification Bot יכול להיות Token נפרד מ-Ingestion Bot.
