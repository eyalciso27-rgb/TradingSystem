# ADR-0018 — Canonical Execution Runtime

- **Status**: ACCEPTED
- **Date**: 2026-08-19
- **Supersedes**: nothing
- **Related**: ADR-0017 (optional GICS and historical membership), IMP-0906
  (final pre-submit check), IMP-1003 (execution agent), Integration 9.1/9.2

## Context

By the end of PR #92 every safety component existed and none of them were joined
up. A decision could be risk-checked, a quote could be fetched, readiness could
be computed and an order could be submitted — but no single component carried
one decision through all of them. "The safety architecture works" was therefore
a statement about parts, not about a trade.

Two gaps made that visible:

1. `ReadinessGuardedBroker` had a `QuoteSource` seam with no production
   implementation, so every readiness evaluation priced its decision from a test
   double.
2. The `AuditSink` seam likewise had no production implementation, so "the
   decision was recorded" was also a claim about a double.

Meanwhile the repository already contained the hard parts: `ExecutionAgent` (the
sole consumer of a single-use `SubmitAuthorization`, with idempotency, halt gates
and a reconciliation latch), `RiskEngine` (absolute veto), the tamper-evident
audit chain, and a real IBKR market-data client that POI-MD-001 proved works on
the owner host.

## Decision

Add **one** application-layer orchestrator, `ExecutionService`, that carries one
decision through the canonical path, and bind the two empty seams to real
implementations. Specifically:

1. **`ExecutionService` does not submit.** The submit call site stays exactly
   where it was — `ExecutionAgent` — and the agent's `BrokerPort` becomes a
   `ReadinessGuardedBroker`. The guard is a decorator on the port the agent
   already held, so composing them adds no second route to the broker.
2. **Business intent in, observations made internally.** `ExecutionIntent`
   carries a symbol, a side, a strategy and the strategy's own `EngineDecision`.
   It has no field for readiness, risk, tradability, quote freshness or audit
   health; those are produced by the components that own them.
3. **A real IBKR quote source** (`IbkrQuoteSource`) drives the existing
   `IbkrMarketDataSession` rather than opening a second Gateway connection stack.
4. **A durable audit sink** (`DurableExecutionAuditSink`) writes to the existing
   tamper-evident chain plus the content-addressed object store. **No new table
   and no new migration**: `audit_events` is already append-only, hash-linked and
   queryable by `target_ref`, which is what an execution audit needs.
5. **Market-data state is modelled explicitly.** `MarketDataState`
   (REALTIME / DELAYED / FROZEN / DELAYED_FROZEN / UNAVAILABLE / UNKNOWN) is
   carried on `QuoteState` and gated by policy: Paper accepts REALTIME or
   DELAYED by default, Live accepts REALTIME only, and neither accepts UNKNOWN
   or UNAVAILABLE.
6. **Authorization is a separate boundary that defaults to DENY.**
   `DenyAllExecutionAuthorization` is the only production implementation.

## Why the service is not the gate

`ExecutionService` computes a readiness report before deciding whether to hand
the agent a request. That report is **not** the enforcement point: the guard
re-evaluates at the submission boundary, fetching its own quote. A market that
moves between planning and submitting is caught there, and a test proves the
guard fetches a second quote and refuses.

If the earlier evaluation were treated as the check, the whole design would have
a time-of-check/time-of-use hole in it. It exists so an operator can read *why*
something was refused, and for no other reason.

## Consequences

**Accepted.** One guard and one agent live for the service's lifetime, because a
fresh agent per decision would reset the idempotency map that makes a retry safe.
That means the guard's risk source outlives a single decision, so risk results
are held in a registry keyed by `(client_order_id, symbol)` and an unregistered
order gets `None`, which blocks.

**Accepted.** `BrokerPort` does not carry an existing position's protective stop,
so its true open risk is unknown. The service assumes each open position is
protected at the same stop distance this system enforces on new entries.
Treating the whole notional as at risk would be *more* conservative and would
make any account holding a position untradeable — unusable conservatism that
gets weakened later under pressure. A `portfolio` port overrides the estimate
when a real per-position risk view exists.

**Accepted.** Paper Auto and Live remain prohibited. `PAPER_READY` states that
technical prerequisites hold; it is not permission to trade. Enabling submission
means supplying an authority that mints a single-use authorization through the
canonical pre-submit check — not loosening anything in the service.

## Amendments after deep review (2026-08-19)

Review of the first implementation found several defects that the tests did not
catch. Each fix is an architectural commitment, not a patch.

**The audit sink owns its transactions.** It takes an `Engine` and commits each
append itself. Taking a caller's `Connection` meant `persist` could return a
reference from an uncommitted transaction — the order could go to the broker and
a crash before the caller's commit would lose the pre-submit record. "No submit
until the pre-submit audit is durably committed" is only true if the sink is the
one committing.

**Session P&L is an explicitly nullable canonical fact.** `AccountSnapshot`
gained `session_pnl: Decimal | None`. The risk engine computes both daily halts
from it, so the previous `session_pnl=Decimal(0)` presented an account that had
lost money today as flat and silently disabled them. IBKR's account summary has
no canonical session-P&L tag, so on the owner host this is normally `None` — and
`None` fails the decision closed. **That is a real Paper blocker**, and reporting
it is correct.

**Existing positions are classified `UNKNOWN`, never as the candidate.** Copying
the new decision's sector, cluster, source and strategy onto every open position
corrupts four risk caps at once. Unknown classification refuses by default
(`require_classified_portfolio`); a portfolio authority satisfies it honestly.
The open-risk *amount* remains an explicit estimate, which is the only part of
this the ADR ever sanctioned.

**Broker-side protection means the broker actually holds it.** A supported STOP
order *type* proves nothing: the entry is one LIMIT order carrying a stop price,
which the adapter maps to that order's aux price. Real protection for a new
position requires the broker to establish it *atomically* — a native bracket or
OCA. The IBKR paper adapter declares both `UNSUPPORTED`, so **a protected entry
is not currently possible through it and is refused** (option B: fail closed).
"Submit the entry, place the stop afterwards" is not a protection mechanism; it
is a window of unbounded exposure. This is the second real Paper blocker.

**Idempotency is durable, not in-memory.** The agent's outcome map and the
adapter's client-order map both die with the process. A `DurableExecutionHistory`
read over the audit chain answers, across restarts: a recorded terminal outcome
returns without resubmitting; a pre-submit with no recorded outcome is
`NEEDS_RECONCILIATION`, never a blind retry; unreadable history blocks.

**The final quantity is bound to the risk approval.** The sizing gate checked
`approved_quantity > 0` while the submitted quantity came from the
authorization, so an authority minting 10 against an approval of 1 would have
passed. The risk registry now refuses an order larger than the engine approved —
at the last boundary before submission.

**Configuration cannot disagree with itself.** Mode and environment must pair
(PAPER↔PAPER, LIVE↔LIVE), and the configured risk policy version must match the
policy the injected engine actually applies.

**Rejected: a new `execution_decisions` table.** The append-only chain plus the
object store already provide durability, tamper evidence, payload retrieval and
query-by-decision. Adding a table would have meant a migration for no property
we did not already have.

**Rejected: an autonomous execution loop.** Nothing in this ADR starts, schedules
or triggers a trade. The scheduler remains automatic; execution does not.

## Guardrails

`scripts/check_architecture.py` now fails the build when production code calls
`submit_order` outside the port, the adapter, the guard and the agent; when a
`RiskResult` is constructed outside the risk engine; or when the
default-to-deny, submit-through-the-agent and audit-before-submit invariants are
edited out of the files that carry them.
