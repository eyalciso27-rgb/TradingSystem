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
