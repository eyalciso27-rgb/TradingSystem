# POI-IBKR-001 — owner-run sequence (Paper lifecycle evidence)

> **Nothing in this file has been run.** It is the preparation deliverable of
> WP-A3. The harness (`scripts/poi_ibkr_lifecycle.py`) is the first code path
> in this repository that can send a real broker order; it sends nothing
> without `--execute` **and** a confirmation token from a dry run of the
> identical parameters. Only the owner may execute the steps below, and only
> the owner may mark POI-IBKR-001. Until that happens the PoI stays
> **BLOCKED / NOT RUN**.
>
> Governance: [ADR-0016](../adr/ADR-0016-poi-ibkr-001-market-data-dependency.md)
> (market-data dependency), [ADR-0015](../adr/ADR-0015-evidence-delivery-governance.md)
> (evidence delivery),
> [ADR-0014](../adr/ADR-0014-defer-external-pois-allow-core-development.md)
> (no real external run before its PoI passes). Gap analysis:
> [POI-IBKR-001-READINESS.md](../poi/POI-IBKR-001-READINESS.md).

## Safety envelope (enforced in code, not by discipline)

| Rule | Enforcement |
|---|---|
| localhost only | `ALLOWED_HOSTS = {127.0.0.1, localhost}`; no override flag |
| Paper API port 4002 only | port `4001` refused **by name** as the LIVE port; any other port refused |
| Paper account only | `Environment.PAPER` asserted on the snapshot; the adapter enforces the `DU` prefix and disconnects otherwise |
| fixed instrument allowlist | `F`, `T`, `AAPL`; AAPL is cancel-path only, never a deliberate fill |
| minimal size | quantity ≤ 1 share **and** notional ≤ $50 |
| BUY entries only | a SELL entry (which could open a short) is refused |
| LIMIT entries only | the only STOP is the protective leg the ProtectionManager produces |
| no unrestricted submit surface | every order is built from a validated `LifecycleEnvelope`; there is no free-form intent path |
| dry-run first | `--execute` requires `--confirm <token>`; the token is derived from scenario+symbol+side+quantity+price+alias+port, so a stale or copied token fails closed |
| one order at a time | the `client_order_id` is deterministic per scenario; a re-run returns the original outcome and sends nothing |
| no blind retry | broker `UNKNOWN` ⇒ `NEEDS_REVIEW` ⇒ reconciliation only |
| fail-closed + cleanup | any error aborts, attempts to cancel what was sent, and reports FAIL |
| no flatten | not implemented; positions are closed by the owner in the Gateway UI |
| crash-safe | the `client_order_id` is deterministic per scenario, so a re-run after a crash reuses it and the broker returns the original receipt; preflight separately refuses to start on top of a leftover PoI order |
| sanitized output | account ids masked (`DU*****xx`); no credential is read, prompted for or printed |

## Preconditions

1. IB **Gateway Paper** running on the ARM64 host under IBC/systemd, logged in
   (see [IB_GATEWAY_AUTO_LOGIN.md](IB_GATEWAY_AUTO_LOGIN.md)), API enabled,
   **Read-Only API disabled** (it must be off for this run — the read-only
   harnesses required it on).
2. Official `ibapi` **10.48.1** installed on the host (runbook §4b) — the
   10.48 error-callback fix (PR #36) is already merged.
3. `POI-MD-001.realtime_subgate` = PASS (2026-07-21); per ADR-0016 that
   satisfies the market-data dependency for this PoI only.
4. Regular US market hours for the fill path. Outside them run steps 1–4 only.
5. `--broker-module` must point at a module exposing `create_broker()` or
   `build_paper_adapter()` — i.e. `trading_broker_ibkr.paper_adapter`.

## The seven steps

Replace `<ALIAS>` with the configured account **alias** (never a `DU` id) and
`<TOKEN>` with the token the preceding dry run printed.

```bash
cd ~/TradingSystem
BM=trading_broker_ibkr.paper_adapter
```

### Step 1 — preflight (read-only; nothing is built)

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario preflight --account-alias <ALIAS> --broker-module "$BM" \
  --evidence-out /tmp/poi-ibkr-001-step1-preflight.md
```

Expect PASS on: connect, broker_health, paper_environment, paper_account_gate,
protective_stop_supported, no_stale_poi_orders, position_snapshot, disconnect.
**Stop if anything fails.**

### Step 2 — dry run of the cancel path (what-if only; prints the token)

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario dry-run --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --quantity 1 --limit-price <FAR_BELOW_MARKET> \
  --evidence-out /tmp/poi-ibkr-001-step2-dryrun.md
```

Nothing is sent. Record the printed `Confirmation token`.

### Step 3 — cancel path (**first real order**; must not fill)

Pick `--limit-price` at least **20 % below** the current bid and pass the
current price as `--reference-price` so the harness proves the order cannot
fill.

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario cancel-path --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --quantity 1 --limit-price <FAR_BELOW_MARKET> \
  --reference-price <CURRENT_PRICE> \
  --execute --confirm <TOKEN> \
  --evidence-out /tmp/poi-ibkr-001-step3-cancel.md
```

Proves: submit → ACKNOWLEDGED → duplicate delivery creates **no** second order
→ cancel → CANCELLED → clean reconciliation. **Stop here if the cancel is not
confirmed**; clear the order in the Gateway UI and re-run step 1.

### Step 4 — verify flat, then re-dry-run the fill path

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario preflight --account-alias <ALIAS> --broker-module "$BM"

uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario dry-run --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --quantity 1 --limit-price <MARKETABLE_LIMIT>
```

`<MARKETABLE_LIMIT>`: at or just above the ask, still ≤ $50 notional. Record
the new token — it differs from step 2's because the price changed.

### Step 5 — fill path (**one share; a real position is opened**)

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario fill-path --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --quantity 1 --limit-price <MARKETABLE_LIMIT> \
  --execute --confirm <TOKEN> \
  --evidence-out /tmp/poi-ibkr-001-step5-fill.md
```

Proves: submit → FILLED → position reported → ProtectionManager plans a
protective STOP 2 % below the average fill price → the STOP is submitted
through the same Execution Agent → no unprotected position → reconciliation
sees position **and** protection.

If no fill arrives within the timeout the harness **cancels the entry** rather
than leaving it resting. That is a valid, recordable outcome — not a failure
of the safety envelope.

### Step 6 — reconnect / restart evidence

With the position and its stop still open, restart the Gateway
(`sudo systemctl restart ibgateway`), wait for login, then:

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario reconnect --account-alias <ALIAS> --broker-module "$BM" \
  --evidence-out /tmp/poi-ibkr-001-step6-reconnect.md
```

`reconnect` is read-only — it submits and cancels nothing. It asserts the
invariant that matters after a restart: **no naked position**. The position
must still be reported and a protective order must still be resting;
reconciliation must come back clean. Record what the Gateway did with the
resting stop across the restart — this is the "deterministic recovery"
criterion.

Use `reconnect`, not `preflight`, here: `preflight` deliberately refuses to
start while a PoI order is still open, which is exactly the state step 6
inspects.

### Step 7 — cleanup and close-out

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario cleanup --account-alias <ALIAS> --broker-module "$BM" \
  --evidence-out /tmp/poi-ibkr-001-step7-cleanup.md
```

Cleanup cancels only orders whose `client_order_id` starts with
`poi-ibkr-001`; it never touches anything else and **never flattens**. Close
the 1-share position manually in the Gateway UI, then re-run step 1 until
`no_stale_poi_orders` and `position_snapshot` are clean.

## Abort at any point

- Order behaving unexpectedly → `--scenario cleanup`; if the harness is dead,
  cancel in the Gateway UI (VNC), then re-run preflight.
- Position left over → close it manually in the Gateway UI. Never from code.
- Anything `UNKNOWN` → stop. Reconcile. **Never resend.**
- Gateway unstable → stop the run (that is itself a FAIL criterion) and keep
  the logs.

## What the owner does with the output

1. Collect the `--evidence-out` artifacts (already sanitized; re-read them
   before pasting anywhere).
2. Create `docs/poi/evidence/POI-IBKR-001.md` with: date, host class, Gateway
   and `ibapi` versions, masked account, per-step check tables, observed
   normalized state transitions, the reconciliation result, and anything that
   did **not** reproduce (e.g. a partial fill).
3. Mark PASS/FAIL in `docs/poi/STATUS.yaml` — **owner only**.

PASS requires all of: zero duplicate orders including forced re-delivery;
every fill protected at the broker within the run; events normalized to
canonical states (no raw IBKR statuses in the evidence); deterministic
recovery after restart via reconciliation; host decision documented. Any blind
retry, unprotected fill, unstable gateway or IBKR-field leakage ⇒ FAIL.

A PASS here proves **broker mechanics only**. It does not enable PAPER_AUTO,
does not enable LIVE, does not close POI-MD-001, and proves nothing about
strategy profitability (`PROFITABILITY_NOT_PROVEN` stands).
