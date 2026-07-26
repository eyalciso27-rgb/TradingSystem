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
| dry-run first | `--execute` requires `--confirm <token>`; the token is derived from run-date+scenario+symbol+side+quantity+limit+reference price+alias+port, so a stale or copied token fails closed — **a token from an earlier day can never be replayed** |
| one order at a time | the `client_order_id` is deterministic per scenario **and per run-date**; a re-run on the same day returns the original outcome and sends nothing, while a run on another day never collides with an earlier one |
| no blind retry | broker `UNKNOWN` ⇒ `NEEDS_REVIEW` ⇒ reconciliation only |
| fail-closed + cleanup | any error aborts, attempts to cancel what was sent, and reports FAIL |
| no flatten | not implemented; positions are closed by the owner in the Gateway UI |
| protective leg checked too | the generated STOP is re-validated before sending: SELL only, STOP only, positive stop price, quantity > 0 and never more than the position held (a larger SELL would open a short) |
| loud abort | any state that may leave real exposure records a FAILED `owner_action_required` check plus `>>> OWNER ACTION REQUIRED <<<` steps; the artifact can then never read `RESULT: PASS` |
| crash-safe | a re-run after a crash reuses the same day's `client_order_id`, so the broker returns the original receipt; preflight separately refuses to start on top of a leftover PoI order or a pre-existing position in the traded symbol |
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
6. `SECRETS_ROOT` must be exported. `--account-alias` is an **alias only**;
   the real `DU` account id is read from
   `$SECRETS_ROOT/PAPER/ibkr/paper-account-id` — the directory is the
   canonical environment name, **uppercase** (override the reference with
   `--account-secret`). The id never appears in argv, in shell history or in
   any output — every report line is masked. A missing `SECRETS_ROOT`, a
   missing secret file, or a non-`DU` value all fail closed before the
   adapter is constructed.

## The seven steps

Replace `<ALIAS>` with the configured account **alias** (never a `DU` id) and
`<TOKEN>` with the token the preceding dry run printed.

```bash
cd ~/TradingSystem
BM=trading_broker_ibkr.paper_adapter
export SECRETS_ROOT=/path/to/runtime/secrets   # no default; never in the repo
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

The confirmation token is **scenario-bound**: it is derived from the scenario
together with symbol, side, quantity, limit, reference price, alias, port and
run date. The dry run must therefore name **the same `--scenario` you intend
to execute**, simply without `--execute`. A token produced by
`--scenario dry-run` is rejected by `--scenario cancel-path --execute`.

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario cancel-path --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --quantity 1 --limit-price <FAR_BELOW_MARKET> \
  --reference-price <CURRENT_PRICE> \
  --evidence-out /tmp/poi-ibkr-001-step2-dryrun.md
```

Nothing is sent — without `--execute` the run takes the what-if branch.
Record the printed `Confirmation token`.

### Step 3 — cancel path (**first real order**; must not fill)

Pick `--limit-price` at least **20 % below** the current bid and pass the
current price as `--reference-price`. Both are **mandatory** for this
scenario — without them the envelope refuses to build, and a limit closer than
20 % is refused before any connection is made.

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario cancel-path --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --quantity 1 --limit-price <FAR_BELOW_MARKET> \
  --reference-price <CURRENT_PRICE> \
  --execute --confirm <TOKEN> \
  --evidence-out /tmp/poi-ibkr-001-step3-cancel.md
```

Proves: submit → ACKNOWLEDGED → duplicate delivery creates **no** second order
→ cancel → CANCELLED → `never_filled` → clean reconciliation. **Stop here if
the cancel is not confirmed**; clear the order in the Gateway UI and re-run
step 1.

If a rapid market move fills it anyway (either before the acknowledgement is
matched or while the cancel is in flight), the harness aborts with
`>>> OWNER ACTION REQUIRED <<<`: a real unprotected position exists and the
cancel path has no protection machinery. Follow the printed steps by hand.

### Step 4 — verify flat, then re-dry-run the fill path

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario preflight --account-alias <ALIAS> --broker-module "$BM"

uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario fill-path --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --quantity 1 --limit-price <MARKETABLE_LIMIT>
```

Again: the same `--scenario` you intend to execute, without `--execute`.
`<MARKETABLE_LIMIT>`: at or just above the ask, still ≤ $50 notional. Record
the new token — it differs from step 2's because both the scenario and the
price changed.

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

On a **partial** fill the harness first cancels the working remainder (so no
unprotected quantity can arrive later), then protects what filled.

If no fill arrives within the timeout the harness **cancels the entry** rather
than leaving it resting. That is a valid, recordable outcome — not a failure
of the safety envelope.

If anything prevents protection — the stop is rejected, the broker reports no
position or a non-positive `average_price`, or the generated stop falls outside
the envelope — the harness stops and prints
`>>> OWNER ACTION REQUIRED <<< UNPROTECTED PAPER POSITION MAY BE OPEN` with the
exact manual steps. **It never places a corrective order and never flattens.**
Reconciliation is fed the protection actually **accepted**, never the protection
merely planned, so this failure shows up as a real `PROTECTION_MISMATCH`.

### Step 6 — reconnect / restart evidence

With the position and its stop still open, restart the Gateway
(`sudo systemctl restart ibgateway`), wait for login, then run `reconnect`
bound to the **exact fill-path run** via `--run-ref`. The fill-path evidence
(step 5) printed a line `run reference (pass to reconnect as --run-ref): …`;
`<RUN_REF>` below is that value (it is the fill-path confirmation token).

```bash
uv run python scripts/poi_ibkr_lifecycle.py \
  --scenario reconnect --account-alias <ALIAS> --broker-module "$BM" \
  --symbol F --run-ref <RUN_REF> \
  --evidence-out /tmp/poi-ibkr-001-step6-reconnect.md
```

`reconnect` is read-only — it submits and cancels nothing. It asserts the
invariant that matters after a restart: **no naked position**. `--run-ref`
binds the check to this specific fill-path run's protective stop, so a stale
stop from a different run (even the same day and symbol) can never mask a
naked position; **without `--run-ref` the scenario fails closed** because
same-run ownership cannot be proven. The position must still be reported and
this run's protective stop must still be resting; reconciliation must come
back clean. The resting stop's **quantity is not exposed by the API**, so the
scenario notes that you must **confirm in the Gateway/TWS UI that the stop
quantity covers the full position**. Record what the Gateway did with the
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

- **`>>> OWNER ACTION REQUIRED <<<` printed → do exactly what it says, by hand,
  before running anything else.** It appears only when the run may have left
  real exposure.
- Order behaving unexpectedly → `--scenario cleanup`; if the harness is dead,
  cancel in the Gateway UI (VNC), then re-run preflight.
- Position left over → close it manually in the Gateway UI. Never from code.
- Anything `UNKNOWN` → stop. The harness prints that an order **may** exist at the broker; inspect the Gateway UI by hand. **Never resend.**
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
