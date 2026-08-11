# IB Gateway (Paper) Auto-Login via IBC — Runbook

Keeps IB Gateway **Paper** logged in and its API (port 4002) healthy on the
Oracle Linux ARM64 host, using the official [IbcAlpha/IBC](https://github.com/IbcAlpha/IBC)
release plus a watchdog. It **never** bypasses IBKR Mobile 2FA, never touches a
LIVE account, never places an order, and never enables Paper-Auto.

> Scope: infrastructure availability only. It does not change any PoI status
> and does not authorize execution (POI-IBKR-001 / IMP-1302 remain gated).

## 1. Version & checksum (pinned)

| Item | Value |
|------|-------|
| IBC release | **3.24.1** (official, published 2026-07-02) |
| Asset | `IBCLinux-3.24.1.zip` |
| SHA-256 | `d99ee28cc3539e3843fb00d28dc484c255006b0063d38f0808ac3ef07dd88bb8` |
| Size | 283,529 bytes |
| Source | https://github.com/IbcAlpha/IBC/releases/tag/3.24.1 |

IBC is pure Java, so this Linux release runs on **ARM64** through the JRE that
ships with IB Gateway and drives any current Gateway version, **10.48**
included. The installer verifies the SHA-256 (from the GitHub release asset
digest) **before** extracting; a mismatch aborts with no install.

## 2. Install IBC (owner, on the host)

```bash
sudo IBC_DEST=/opt/ibc bash /opt/trading/scripts/install_ibc_gateway.sh
```

The script downloads the pinned asset, verifies the checksum, extracts to
`/opt/ibc`, and writes `/opt/ibc/INSTALLED_VERSION.txt`. It handles **no
credentials** and contacts **no** IBKR service.

## 3. Secure credentials (never in the repo)

Credentials live **only** in the IBC config, outside the repo, `chmod 600`:

```bash
install -d -m 700 -o opc -g opc /home/opc/.config/ibc
cp scripts/ibc/config.example.ini /home/opc/.config/ibc/config.ini
chmod 600 /home/opc/.config/ibc/config.ini
chown opc:opc /home/opc/.config/ibc/config.ini
# edit and set IbLoginId / IbPassword (Paper) in that file only
```

Rules enforced by the tooling:
- No username/password in the repo, on any command line, or in a visible
  `Environment=` entry.
- The config is git-ignored; a test guardrail (`test_ibc_no_committed_secrets`)
  fails the build if a non-empty `IbLoginId`/`IbPassword` is ever committed.
- The start script refuses to launch unless the config is `chmod 600`, owned by
  the running user, and `TradingMode=paper`. It never prints the config.

Mandatory IBC settings (see `scripts/ibc/config.example.ini`): `TradingMode=paper`,
`ReloginAfterSecondFactorAuthenticationTimeout=yes`,
`SecondFactorAuthenticationExitInterval=60`,
`AcceptNonBrokerageAccountWarning=yes`, `AcceptIncomingConnectionAction=reject`,
`OverrideTwsApiPort=4002`, `BindAddress=127.0.0.1`, `ReadOnlyApi=yes`.

## 4. systemd units

```bash
sudo cp systemd/ibgateway-ibc.service /etc/systemd/system/
sudo cp systemd/ibgateway-watchdog.service /etc/systemd/system/
sudo cp systemd/ibgateway-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
# start Gateway once the owner is ready to approve the 2FA prompt:
sudo systemctl start ibgateway-ibc.service
# then enable the periodic watchdog:
sudo systemctl enable --now ibgateway-watchdog.timer
```

### Recovery policy — bounded and fail-closed (A / B / C)

`ibgateway-ibc.service` uses **`Restart=on-failure`** with a burst cap
(`StartLimitBurst=3` / `StartLimitIntervalSec=900`) and
`RestartPreventExitStatus=78`. systemd is the single restart manager; the
watchdog still only supervises and alerts (it does not restart). Three cases:

- **A. Native scheduled IB Gateway Auto Restart — ALLOWED.** IBC performs it
  inside the running process (the same Jts/API/IBC settings return with no
  manual VNC work), so the unit's main process never exits and systemd is not
  involved.
- **B. Bounded recovery after an UNEXPECTED crash — ALLOWED.** `on-failure`
  restarts at most 3 times per 15 minutes, then the unit stays `failed`.
- **C. Blind/unbounded recovery, or any resend of trading actions —
  PROHIBITED.** Known fail-closed conditions (invalid config, non-paper,
  missing Java/files, **2FA timeout**, **authentication failure**) make the
  launcher exit `78`, which `RestartPreventExitStatus` exempts — so they are
  **never** auto-restarted (no 2FA loop). A deliberate `systemctl stop` stays
  stopped.

Process liveness is **not** trading permission: after any recovery, entries
stay inhibited until canonical broker reconciliation (positions, open orders,
executions/order state, lifecycle markers) succeeds.

## 4b. IBKR Python API (`ibapi`) — owner-run host prerequisite

The read-only PoI clients import `ibapi` **lazily, on the host only**. It is
deliberately **not** a declared dependency of `packages/py/broker-ibkr`
(`dependencies = []`) and is **not installed in CI**:

- The old PyPI package `ibapi==9.81.1.post1` is **forbidden** — it predates the
  10.48 callback signature and would reintroduce the error-callback defect.
- No public pin exists for 10.48.1; the supported build comes from the
  **official IBKR source distribution**, so it cannot be reproduced in CI.

Owner-run installation on the ARM64 host (already performed for the current
box — recorded here for reproducibility):

```bash
# from the IBKR TWS API source distribution, e.g. /home/opc/twsapi
cd /home/opc/twsapi/IBJts/source/pythonclient
python3 -m pip install --user .        # installs ibapi 10.48.1
python3 -c "import ibapi; print(getattr(ibapi, '__version__', 'unknown'))"
```

Verified host baseline: `ibapi==10.48.1`, `protobuf==5.29.5`, IB Gateway 10.48,
Oracle Linux ARM64. The code detects the runtime via
`trading_broker_ibkr.ibkr_error_callback.ibapi_runtime_diagnostic()`, which
reports the installed version (or a clear "NOT INSTALLED" message) without
touching credentials.

## 5. 2FA flow (never bypassed)

1. Server boots / Gateway falls / a relogin is needed.
2. IBC starts IB Gateway and auto-enters the Paper `IbLoginId`/`IbPassword`
   from the `chmod 600` config.
3. Login proceeds to the **IBKR Mobile 2FA** step. **You approve the push
   notification on your phone.**
4. If the 2FA window expires, `ReloginAfterSecondFactorAuthenticationTimeout=yes`
   makes IBC re-login and trigger a **new** notification.
5. The watchdog verifies API port 4002 is open and `reqCurrentTime` answers.

The tooling never enters, reads, or automates the 2FA code.

## 6. Watchdog states & policy

| State | Trigger | Action |
|-------|---------|--------|
| `PROCESS_DOWN` | Gateway/IBC process absent | start via IBC (budget-limited) |
| `LOGIN_REQUIRED` | up, not logged in / login failed | wait while IBC enters creds; on repeated `LOGIN_FAILED`, rate-limited relogin, else alert |
| `TWO_FA_PENDING` | 2FA prompt/timeout in the log | **wait** for owner approval; on timeout, rate-limited relogin (never an immediate restart) |
| `API_DOWN` | logged in but 4002 / `reqCurrentTime` fails | one controlled restart within the window |
| `API_READY` | 4002 open + `reqCurrentTime` OK | no action |
| `UNKNOWN` | inconsistent signals | alert + fail closed |

- A `FileLock` prevents two concurrent launches.
- Windowed budgets (`--max-restarts`, `--restart-window`, `--max-relogins`,
  `--relogin-window`; defaults 3 / 30 min) prevent restart/relogin loops.
- **Reconciliation gate:** if `/run/ibgateway/order-lifecycle.active` exists, all
  restart/kill actions are inhibited (the watchdog only alerts and waits) — so a
  future order lifecycle is never killed without reconciliation.

### Exit codes (journal severity)

| Code | Meaning |
|------|---------|
| 0 | API ready / benign in-progress |
| 10 / 11 / 12 | started / relogin / restarted |
| 20 | 2FA pending — waiting (not a failure) |
| 21 | restart inhibited by reconciliation gate |
| 22 | another instance held the lock; skipped |
| 30 | restart/relogin budget exhausted (fail-closed) |
| 40 | unknown state (fail-closed) |
| 41 | login-failure budget exhausted (fail-closed) |

## 7. Alerts

For now, alerts are **journal/log only** with the distinct exit codes above; no
Telegram/WhatsApp is wired yet. A future alert router can key off the exit code
and the `ALERT state=… action=… reason=…` log line. Alerts fire on
`TWO_FA_PENDING`, login failure, API-not-up, and budget-exhaustion.

## 8. Safety checklist

- [ ] `TradingMode=paper` (no LIVE path anywhere)
- [ ] Config `chmod 600`, `opc:opc`, dir `chmod 700`
- [ ] No credentials in repo / CLI / visible env / logs / `ps`
- [ ] Read-only API; **no orders**, no Paper-Auto
- [ ] 2FA approved by the owner only; never bypassed
- [ ] `shellcheck scripts/*.sh` clean; watchdog tests green

## 9. Verify (host)

```bash
shellcheck scripts/install_ibc_gateway.sh scripts/start_ibc_gateway.sh
systemctl status ibgateway-ibc.service
journalctl -u ibgateway-watchdog.service -n 50 --no-pager
```
