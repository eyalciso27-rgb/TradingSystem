# POI-IBKR-001 — DEF-19 verify-clean owner-run — Evidence

**DEF-19 result: PASS** (real owner-host run against the IBKR **Paper** Gateway).
**POI-IBKR-001 aggregate status: BLOCKED / non-PASS — unchanged.**

## Scope and non-scope (read this first)

This record proves **one read-only step**: that on the owner host, at the time
below, the Paper account had **no working PoI lifecycle order, no non-zero
position, no unresolved broker operation state, and a clean reconciliation**.

It does **NOT**:

- make POI-IBKR-001 PASS — no order lifecycle has been proven;
- prove a cancel-path execute (**DEF-18 — NOT RUN**);
- prove restart-recovery cancellation (**DEF-20 — NOT RUN**);
- prove a fill path or protection (**never run**);
- enable PAPER_AUTO or LIVE (both remain prohibited).

**No order was submitted, cancelled, modified or flattened.** The scenario is
structurally read-only: its evidence header records
`mode: READ-ONLY (nothing submitted, cancelled or flattened)`.

## Provenance

| Field | Value |
|---|---|
| run timestamp (UTC) | **2026-07-30T17:49:25Z** |
| scenario | `verify-clean` |
| harness | `poi-ibkr-lifecycle-1.0.0` |
| endpoint | `127.0.0.1:4002` (localhost + paper API port only) |
| host source | `/tmp/poi-ibkr-001-verify-clean-20260730T174925Z.md` |
| host source (log) | `/tmp/poi-ibkr-001-verify-clean-20260730T174925Z.log` (byte-identical) |
| sha256 (both) | `4d71c2918b56bbb2537146d253327da3a78952595a119fec733fae0edc28ad61` |

**Post-clock-fix.** This run is after the D-CLOCK-01 fix (PR #53, merged
2026-07-30T17:35:41Z) — note the `*_read` probe rows, which only exist in the
post-fix harness. Its timestamps are real UTC wall-clock, so unlike every
pre-fix artifact it may be used as evidence.

## The DVLT row

An earlier read-only inspection (2026-07-30T17:05:22Z) found `DVLT` quantity
**1352**. In this later run the account is **flat**: the breakdown reports
`raw_rows=1 (of which zero-quantity=1); non_zero_positions=0`. The remaining
row carries quantity **zero** only, which is not a position. The harness never
flattens and never acted on it — any change was the owner's own doing, by hand.

## Verbatim artifact

Reproduced exactly as written by the harness. Account references are masked by
the harness itself; no credential, secret-store value or confirmation token
appears in it.

```markdown
# POI-IBKR-001 lifecycle harness — verify-clean

harness: poi-ibkr-lifecycle-1.0.0
mode: READ-ONLY (nothing submitted, cancelled or flattened)

| check | result | detail |
|---|---|---|
| connect | PASS | 127.0.0.1:4002 (paper, localhost) |
| broker_health_read | PASS | read |
| broker_health | PASS | ibkr paper gateway connected |
| account_snapshot_read | PASS | read |
| paper_environment | PASS | environment=PAPER |
| paper_account_gate | PASS | alias=pa*****oi (the adapter enforces the DU prefix gate on the real account id) |
| capabilities_read | PASS | read |
| protective_stop_supported | PASS | STOP=NATIVE; bracket=UNSUPPORTED; oca=UNSUPPORTED |
| open_orders_read | PASS | read |
| open_poi_orders | PASS | 0 still open at the broker |
| positions_read | PASS | read |
| position_snapshot | PASS | raw_rows=1 (of which zero-quantity=1); non_zero_positions=0 [none]; F_positions=0 |
| verify_open_orders_read | PASS | read |
| verify_no_working_lifecycle_order | PASS | no PoI lifecycle order is working |
| verify_foreign_orders_untouched | PASS | 0 non-PoI order(s) visible; reported for the owner, never cancelled or modified by this harness |
| verify_positions_read | PASS | read |
| verify_position_breakdown | PASS | raw_rows=1 (of which zero-quantity=1); non_zero_positions=0 [none]; F_positions=0 |
| verify_no_open_position | PASS | flat |
| reconciliation | PASS | reconciled=True findings=0 entry_block_required=False |
| verify_reconciliation_read | PASS | read |
| verify_no_unresolved_broker_state | PASS | no unresolved broker operation state |
| disconnect | PASS | clean disconnect |

RESULT: PASS

This artifact alone is NOT a PoI PASS: only the owner may mark
POI-IBKR-001, from a real run, in docs/poi/STATUS.yaml.
```

## NOT EXECUTED

- ❌ no post-fix real cancel-path **execute** (DEF-18);
- ❌ no real **cancellation** after the latest changes;
- ❌ no **fill-path** owner-run;
- ❌ no Gateway **restart-recovery** cancellation (DEF-20);
- ❌ no **Live** connection and no Live order;
- ❌ no **PAPER_AUTO** activation.

**POI-IBKR-001 remains BLOCKED / non-PASS.** Only the owner may change it, in
`docs/poi/STATUS.yaml`, and only from a completed order-lifecycle owner-run.
