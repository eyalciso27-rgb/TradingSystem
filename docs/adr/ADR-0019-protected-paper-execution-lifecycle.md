# ADR-0019 — Protected Paper Execution Lifecycle

- **Status**: PROPOSED
- **Date**: 2026-08-27
- **Supersedes**: nothing
- **Amends**: ADR-0018 (Canonical Execution Runtime) — specifically its
  "option B: fail closed" treatment of protected entries, which was explicitly
  provisional ("a protected entry is not currently possible through it and is
  refused").
- **Related**: Integration 9.1/9.2/9.4/9.5, 20.4 (Broker Capability Matrix);
  Decision_Contract 12.5 (canonical OrderState; UNKNOWN resolved only by
  reconciliation); Security 23.2/24.1 (KEEP_PROTECTION); IMP-1002 (submit),
  IMP-1005 (protection), IMP-1003 (execution agent).

## Context

ADR-0018 wired one decision through the whole canonical path and then, at the
protection gate, **refused**: the IBKR paper adapter declared `bracket` and
`oco` `UNSUPPORTED`, so `ReadinessGuardedBroker._observe_protection` could not
obtain `ProtectionEvidence` and blocked. ADR-0018 named this deliberately:

> "Real protection for a new position requires the broker to establish it
> *atomically* — a native bracket or OCA. The IBKR paper adapter declares both
> `UNSUPPORTED`, so a protected entry is not currently possible through it and
> is refused (option B: fail closed). 'Submit the entry, place the stop
> afterwards' is not a protection mechanism; it is a window of unbounded
> exposure. This is the second real Paper blocker."

The empirical owner-host probe on PR #93 (`b44448a`) confirmed the runtime
reaches exactly that gate on a real Paper connection — persistent P&L,
real-time quote on the exact conId, portfolio, risk `APPROVE`, durable audit —
and then stops at `PROTECTION_UNAVAILABLE` with no order submitted.

This ADR closes that gap: it makes the IBKR paper adapter establish a **real,
broker-resident, atomic protective structure** for an entry, proves it, records
it durably, recovers it after a restart, and reconciles it against the broker —
so a Paper trade can move from an already-approved executable plan through a
protected-entry lifecycle end to end. It does **not** enable Paper Auto or Live
and changes no authorization default.

## Decision

### D1 — Atomicity lives in the adapter, not in a new port method

The canonical `BrokerPort` **Protocol methods are unchanged**. A protected entry
still crosses the boundary through the single sanctioned surface,
`submit_order(intent)`, which `check_architecture.py` already fences to the
port, adapter, guard and agent. The parent entry and its protective child(ren)
are one *atomic broker action performed inside the adapter's `submit_order`*,
not two independent port calls — because two independent calls are precisely the
"submit then place the stop afterwards" window ADR-0018 forbids.

`OrderIntent` gains one **additive, optional** field, `protective:
ProtectiveBracket | None` (default `None`). When present, it names the protective
stop (and optional take-profit) that must be established *atomically with* the
entry, plus the deterministic client refs the protective child(ren) will carry.
An intent without it behaves exactly as before. This is a dataclass extension,
not a Protocol-method change: the `FakeBroker`, conformance suite and every
existing caller keep compiling, and a plain single order is still a plain single
order.

### D2 — `IbkrProtectedEntryLifecycle` is the protection authority; it does not submit

`ReadinessGuardedBroker` already injects a `ProtectedEntryLifecycle` whose
`protection_for(intent) -> ProtectionEvidence` is consulted **during readiness**,
before `submit_order` is delegated. The production implementation,
`IbkrProtectedEntryLifecycle`:

- verifies the intent carries a valid protective stop on the correct side and
  at a sane level relative to the entry;
- verifies the adapter reports `bracket` NATIVE for this environment;
- returns `ProtectionEvidence(established=True, mechanism="native-bracket",
  protective_order_refs=(deterministic stop ref, [take-profit ref]))` — the
  refs the child order(s) *will* carry — **without sending anything**.

It never submits, so there is no double-submit: the guard's subsequent single
`submit_order(intent)` is the one and only broker action, and it establishes the
whole bracket atomically. The evidence names the protective refs the adapter is
contractually bound to attach; the durable OUTCOME later records the
broker-assigned ids that prove they exist. "Established (or WILL atomically
establish) ... must name the protective order(s)" (ADR-0018 `ProtectionEvidence`)
is satisfied by the deterministic refs; proof-of-existence is satisfied by the
recorded broker ids and reconciliation.

### D3 — IBKR-native bracket construction (inside the adapter only)

For a BUY entry the adapter builds, under one parent order id:

- **parent**: the entry (LIMIT), `transmit=False`;
- **protective stop child**: SELL STOP for the entry quantity, `parentId =
  parent id`, in a one-cancels-all group with the take-profit, `transmit=False`
  (or `True` if it is the last leg);
- **optional take-profit child**: SELL LIMIT, `parentId = parent id`, same OCA
  group, `transmit=True` on the last transmitted leg only.

Only the last child carries `transmit=True`; IBKR transmits the whole structure
when it arrives, so there is never a moment where the parent is live and the
stop is not. `orderRef` on every leg is our canonical correlation
(`client_order_id`, and `client_order_id:stop` / `:target` for children) — never
broker deduplication (Integration 9.1; `OrderIntent` docstring). The instrument
is the exact `conId`-bound identity already carried on the intent; the adapter
does **not** re-resolve by ticker at submission time.

`IBKR_PAPER_CAPABILITIES` becomes `bracket=NATIVE` and `oco=NATIVE`. SELL/short
entries are out of scope for this milestone's proof (the POI is a long AAPL
entry); the adapter fails closed on any protective structure it cannot build
natively, never emulating.

### D4 — Durable lifecycle facts extend the existing chain; no new table

Per ADR-0018 ("Rejected: a new `execution_decisions` table") and section 8 of
the milestone brief, the protective facts are carried on the **existing**
append-only, hash-linked audit chain + content-addressed object store, through
the **existing** `DurableExecutionAuditSink` (`persist_prepared` /
`persist_outcome`) and read back by `DurableExecutionHistoryReader`. **No new
table, no Alembic migration.**

The `execution_fingerprint` contract gains **conditionally-required** protective
fields (`PROTECTIVE_ORDER_FIELDS`): `bracket_mechanism`,
`protective_order_refs`, `protective_quantity`. They are required **only** on a
record that declares itself a protected entry (`bracket_mechanism` present) — a
plain order and every existing record stay readable, exactly as
`limit_price`/`stop_price` are excluded from `MANDATORY_PREPARED_FIELDS` for a
MARKET order. The SUBMIT_PREPARED record carries the intended protective
structure; the OUTCOME record carries the broker-assigned parent and child ids
and the proven protective quantity. `require_consistent_lifecycle` is extended
so a terminal record that resolves a *different protective structure* than was
prepared is `ExecutionHistoryUnreadable` → reconcile, never replay.

### D5 — Explicit, durable state machine

States (durable; a record exists at every point a crash could occur):

| State | Entered when | Durable fact | Allowed next | Fails closed to |
|---|---|---|---|---|
| `PLANNED` | readiness APPROVE, protection proven by lifecycle | `EVALUATED` | `PROTECTION_PREPARED` | refuse (no prepared record) |
| `PROTECTION_PREPARED` | authorized; bracket frozen; **before** any broker call | `SUBMIT_PREPARED` (carries frozen bracket) | `ENTRY_SUBMITTING` | `NEEDS_RECONCILIATION` |
| `ENTRY_SUBMITTING` | `placeOrder` calls in flight | (still `SUBMIT_PREPARED`) | `ACCEPTED` / `REJECTED` / `NEEDS_RECONCILIATION` | `NEEDS_RECONCILIATION` |
| `PROTECTED` | broker acknowledged parent **and** protective child | `OUTCOME=SUBMITTED` (parent+child ids, protective qty) | fills, cancel, reconcile | `NEEDS_RECONCILIATION` |
| `PARTIALLY_FILLED` | fill < ordered; protective qty follows filled qty | `OUTCOME` fill fact | more fills, cancel remainder | `NEEDS_RECONCILIATION` |
| `FILLED` | fill == ordered; protection covers full exposure | `OUTCOME` fill fact | protective lifecycle only | `NEEDS_RECONCILIATION` |
| `CANCEL_PENDING` | cancel requested | `OUTCOME` cancel-requested | `CANCELLED` / reconcile | `NEEDS_RECONCILIATION` |
| `CANCELLED` | broker confirmed cancel; no residual exposure | `OUTCOME=CANCELLED` | terminal | — |
| `REJECTED` | broker rejected before any working order | `OUTCOME=REFUSED/FAILED` | terminal | — |
| `NEEDS_RECONCILIATION` / `UNKNOWN` | any ambiguity: parent-without-child, child-without-parent, wrong shape/qty/side/instrument, crash between PREPARED and OUTCOME, disconnect mid-submit | recorded as needing review | reconcile only; **no auto-submit** | itself (safe) |

**Load-bearing invariant:** the system never reports `PROTECTED`/`FILLED` while
*proven protective coverage < filled exposure*. If coverage cannot be proven,
the state is `NEEDS_RECONCILIATION`, which fails closed.

### D6 — Restart, recovery, reconciliation (same PR)

On restart the durable history is read for the order id. A `SUBMIT_PREPARED`
with no terminal `OUTCOME` is **ambiguous by construction** and never a blind
retry. Recovery reads what the broker actually holds via
`recover_working_orders` (broker-observed shapes) and reconciles the durable
prepared bracket against it, distinguishing at least: definitely-not-sent,
working-entry-only (missing child → **unprotected → NEEDS_RECONCILIATION**),
working-protected-structure, filled+active-protection, cancelled, rejected,
ambiguous/inconsistent, unexpected-shape, wrong-quantity, wrong-instrument,
wrong-side/price/type. Only facts IBKR returns are used; no correlation field
the broker did not preserve is fabricated. Ambiguity ⇒ `NEEDS_RECONCILIATION`;
an unexpected broker state is never silently repaired.

### D7 — Cancel / exposure reduction stays safe

Exposure reduction is never gated by entry-readiness (the guard already exempts
`cancel_order`). A protective replacement that cannot be proven to leave no
unprotected window fails closed to reconciliation rather than executing a
"cancel-all-then-recreate" sequence. Cancelling the entry cancels the whole OCA
structure; cancelling remaining quantity after a partial fill resizes the
protective stop to the remaining filled quantity (ProtectionManager 9.4).

### D8 — Owner-safe Paper proof, with a scoped Paper-only authorization

Unlike PR #93's read-only probe, this milestone requires a **real** Paper order.
A new owner-safe proof command mints a **single-use, Paper-only** authorization
that is defined **inside the proof script**, never in production wiring: the
production default remains `DenyAllExecutionAuthorization`, and
`check_architecture.py` continues to fence submission to the agent. The proof:
requires the Paper endpoint and refuses Live; uses canonical secrets; redacts
account ids and secrets; never resets or deletes DB volumes; preserves existing
evidence; uses quantity 1 on a highly liquid symbol (AAPL) with controlled
prices that minimise accidental market exposure; and carries explicit
cleanup/reconciliation that cancels every order it created. **The owner runs it;
this PR only returns the exact command.**

## Consequences

- **Accepted.** `OrderIntent` grows one optional field. The additive change keeps
  the `FakeBroker`, conformance suite and all existing call sites valid; the
  `FakeBroker` learns to model a native bracket (parent + OCA children) so the
  deterministic tests exercise the same lifecycle the adapter runs.
- **Accepted.** The `execution_fingerprint` contract grows conditionally-required
  protective fields. Existing and plain records stay readable; only a record that
  *claims* to be a protected entry must prove it.
- **Accepted, unchanged.** Paper Auto and Live remain prohibited. Authorization
  still defaults to DENY in production. `PAPER_READY` remains "prerequisites
  hold", not permission.
- **Preserved.** Every safety property proven in PR #93 — exact `conId`
  identity, REALTIME quote binding, persistent session P&L, UNKNOWN P&L never
  zero, portfolio read, risk-result identity, executable-plan seal, durable
  audit, final guard, durable idempotency, fail-closed unknowns, no alternate
  submit path — is untouched and re-asserted by tests.

## Guardrails

`scripts/check_architecture.py` continues to fail the build if `submit_order` is
called outside the port/adapter/guard/agent, if a `RiskResult` is built outside
the risk engine, or if the default-to-deny / submit-through-the-agent /
audit-before-submit invariants are edited out. This ADR adds one guardrail: a
protective child order may be constructed only inside the IBKR adapter, so no
bracket/OCA/parentId/transmit wiring can leak outside `trading_broker_ibkr`.
