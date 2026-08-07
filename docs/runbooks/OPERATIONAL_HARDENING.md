# Operational hardening — implementation, activation and evidence

> **Implementation readiness is not host activation evidence.** This document
> separates the two on purpose, and the separation is load-bearing:
>
> | | State |
> |---|---|
> | Code + units + tests | ✅ **IMPLEMENTED AND TESTED WITH FAKES** |
> | Owner-host installation | 🟡 **READY FOR OWNER-HOST ACTIVATION** |
> | Owner-host evidence | ❌ **NOT EXECUTED** |
>
> **B-03 and B-04 remain OPEN. DEF-12 and DEF-13 remain `NOT_RUN`.**
> Nothing here marks POI-PLAT-ARM-001 newly passed, POI-IBKR-001 PASS,
> IMP-1303 complete or IMP-1304 complete.

---

## 1. What changed, and why it matters

The watchdog used to restart IB Gateway whenever the process was down or the
API was unresponsive. It knew about **processes**; it knew nothing about the
**account**.

That is the dangerous kind of gap, because the two look similar and are not.
Restarting a Gateway while an order is working can orphan that order. Doing it
while a position is open removes the session that was supposed to be watching
it. Doing it while the broker state is unresolved destroys the very evidence
reconciliation needs. In each case the watchdog would have reported success:
the process came back.

So a restart now requires a **positive proof about the account**, not the
absence of a process.

### The account-safety gate

Every unattended `START` / `RESTART` / `RELOGIN` is gated on one complete
read-only snapshot:

| Classification | Meaning | Unattended action |
|---|---|---|
| **CLEAN** | every check below passed | policy may be evaluated |
| **NOT_CLEAN** | a **complete** snapshot proves something exists | `WAIT` + alert, launcher never called |
| **UNKNOWN** | the proof could not be completed | `ALERT`, fail closed, launcher never called |

`CLEAN` requires **all** of: localhost only · Paper port 4002 only · `DU`/Paper
account validation · successful API connection · successful liveness round trip
· complete open-order snapshot · complete position snapshot · zero working
orders · zero non-zero positions · zero unresolved operation states · clean
reconciliation with zero blocking findings · clean disconnect.

> **Connectivity failure is `UNKNOWN`. It is never `CLEAN`.** Absence of
> evidence is not evidence of absence. A watchdog that cannot reach the broker
> knows *less*, not more.

Historical, cached or last-known-clean state is **never** consulted. Only a
snapshot taken *now* can speak for *now*.

### One snapshot, not several reads

"One complete snapshot" is structural, not aspirational. A single probe run
reads the broker **exactly once per method** — account, open orders, positions,
order events, reconciliation — and every count and finding is derived from that
captured data.

The reconciliation engine calls `reconcile()` and `order_events()` itself. It
is therefore handed a **snapshot replay view**, never the live broker, so it
cannot issue a second round trip. Previously it received the live view: the
counts came from one instant and the findings from another, and a `CLEAN`
verdict built from two instants describes a moment that never existed — an
order can appear between the reads and be counted by neither.

Because `reconcile()` also returns the account, orders and positions, the two
reads **overlap**, and that overlap is a free coherence check. If they
disagree — an order gained or lost, a state or quantity changed, a different
account answering — the parts are not one snapshot and the result is
**`UNKNOWN`**. Duplicated orders or positions within one snapshot are likewise
`UNKNOWN`. *Mixed-time data is never classified.*

### Exactly one disconnect

Each successful connect is followed by exactly one disconnect attempt, on every
path including an unexpected exception. A failed connect never calls
disconnect. **A failed disconnect downgrades the verdict to `UNKNOWN` with
`DISCONNECT_FAILED`** — a session that could not be closed cannot be reasoned
about, and there is no path from a failed disconnect to `CLEAN`. The gathered
counts are kept in the structured fields for the alert; only the verdict
changes. The probe previously disconnected twice and swallowed the second
failure entirely.

### The consequence: the unattended watchdog no longer restarts anything

This follows directly and is intended. Every state that could trigger a restart
cannot answer a broker query:

| State | Why the account cannot be proven | Result |
|---|---|---|
| `PROCESS_DOWN` | nothing to query | `UNKNOWN` → refuse |
| `API_DOWN` | API not answering | `UNKNOWN` → refuse |
| `LOGIN_REQUIRED` | login incomplete | `UNKNOWN` → refuse |
| `TWO_FA_PENDING` | awaiting the owner | `UNKNOWN` → refuse |
| `API_READY` | probe can run — but no action is needed | n/a |

So the timer now performs **passive supervision**: observe, classify, alert.
Bringing a Gateway back is an **owner-authorized** action.

**Manual owner-authorized recovery and unattended watchdog restart are separate
capabilities, and only the second one was removed.** The controlled, serialized
DEF-20 restart under `POI_IBKR_001_OWNER_RUN.md` and
`CHAOS_RECOVERY_DRILLS.md` is untouched.

The lifecycle/reconciliation marker still inhibits action. It is **necessary
but not sufficient** — its absence proves nothing on its own, which is exactly
why the gate exists above it.

Budgets, locks, structured alerts and 2FA waiting semantics are all preserved,
and **none of them can override the gate.** A freed restart budget is not a
proof about the account.

---

## 2. Read-only commands (safe at any time)

Both are strictly read-only. Neither constructs a launcher; neither can start,
restart or relogin anything. Both work with `uv run --no-sync`, so the owner
host never needs `uv sync`.

### Status

```bash
uv run --no-sync python scripts/ib_gateway_watchdog.py --status
```

Reports process state, TCP state, login marker, lifecycle marker, configured
unit, restart budget, whether any action would be permitted, and an explicit
**proof level**.

### Status with the deep account-safety probe

```bash
uv run --no-sync python scripts/ib_gateway_watchdog.py \
  --deep-probe --status \
  --account-alias <ALIAS> --secrets-root "$SECRETS_ROOT"
```

Adds `CLEAN` / `NOT_CLEAN` / `UNKNOWN`, order and position counts,
unresolved-state count, reconciliation result, sanitized reason codes and
connect/disconnect results.

### JSON for evidence capture


---

## 3b. What each status command actually does

| Command | Broker sessions | Proof ceiling | What it reports |
|---|---|---|---|
| `--status` | **none** | `TCP_OPEN` | process, raw TCP-open, sanitized IBC login marker, lifecycle/state metadata |
| `--deep-probe --status` | **exactly one** | `FULL_ACCOUNT_SNAPSHOT` | everything above plus one complete read-only account snapshot |

**Plain `--status` opens no IBKR session.** It constructs no transport, sends
no `startApi`, requests no current time, receives no `managedAccounts`, builds
no account-safety adapter, creates no launcher, takes no lock and writes no
state. This is structural: the status path is handed a `StatusProbes` that
defines no round-trip probe at all.

It previously *did* connect — it called a round-trip probe that connected, sent
`startApi`, requested the current time and disconnected, while being documented
as doing none of that. A read-only status that attaches an API client changes
the thing it is observing.

**Honest vocabulary.** Observation cannot establish API readiness, so plain
status cannot say `API_READY` — the observation vocabulary does not contain it.
It reports `observed_state: API_PORT_OPEN_UNVERIFIED`, `state: null` and
`api_round_trip_ok: null`. *Null means not tested, which is not the same fact
as tested-and-failed.*

**`--deep-probe --status` uses exactly one session.** Connectivity, round trip,
Paper validation, orders, positions, unresolved states, reconciliation,
disconnect and the verdict all come from that single captured snapshot.

### Account identifiers are contained

A broker account id is sensitive **operational data** — not a credential; it
cannot be used to log in. It must still never reach a terminal, a journal or an
evidence file.

`configure_logging()` runs before any connection: one handler, **root at
WARNING**, `ibapi` loggers pinned to WARNING, and a redaction filter on every
handler and dependency logger that scrubs account-shaped tokens from the
message, the formatted arguments and the exception text. Status output is
scrubbed once more immediately before printing.

The previous code called `logging.basicConfig(level=INFO)`, which raises the
**root** level and therefore enables INFO for every library in the process.
ibapi's `managedAccounts` callback then wrote the complete Paper account id to
stderr — around the sanitized JSON model, which was the only containment that
existed. Containment never depends on shell redirection.

### The canonical watchdog runtime

```
WorkingDirectory=/home/opc/projects/TradingSystem
ExecStart=/home/opc/projects/TradingSystem/.venv/bin/python   /home/opc/projects/TradingSystem/scripts/ib_gateway_watchdog.py …
```

Absolute interpreter, absolute script, both inside the reviewed repository. No
`/usr/bin/env`, no system Python, no `uv sync`, no copy under `/opt`. The host
previously ran a **stale** `/opt` copy with an interpreter that could not
import `trading_broker_ibkr` at all, so the deep probe could never have worked.

### Drop-in policy

Installed **files** matching the repository proves nothing — on the owner host
they matched byte-for-byte while drop-ins rewrote `Type`, `RemainAfterExit`,
`KillMode` and every confinement directive, leaving systemd reporting
`active (exited)` with `MainPID=0` while the live Gateway ran in a user session
scope it did not own. Only **effective** properties and the drop-in set can
establish the contract.

Drop-ins are judged by **content, not filename**. Any override of `Type`,
`RemainAfterExit`, `KillMode`, `Restart`, `ExecStart`, `User`, `Group`,
`WorkingDirectory`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`,
`ReadWritePaths` or `RuntimeDirectory` fails closed. `DISPLAY` and
`XAUTHORITY` are the only permitted host-specific settings, and an allowed
setting does not launder the file it travels in.

### The Gateway sandbox exception list

`ProtectHome=read-only` and `ProtectSystem=strict` stay on, so every tree the
Gateway writes has to be named. The required set is
`systemd_contract.CANONICAL_GATEWAY_WRITABLE_PATHS`:

```
/home/opc/Jts  /home/opc/ibc  /home/opc/.config/ibc  /run/ibgateway
```

`/home/opc/Jts` is the IB Gateway installation **and** its settings tree — IBC's
`ibcstart.sh` uses `~/Jts` as the TWS/Gateway path and writes there during
startup, and the Java the unit executes lives under it.

Read this as an exception list, not a relaxation: the rest of `/home/opc` — the
repository, the SSH keys, the owner's files — remains unwritable to the
Gateway, and `/home`, `/home/opc` and `/` are rejected outright.

**A missing entry does not look like a permission error.** The unit starts, the
launch fails, and systemd reports `inactive (dead)` /
`Deactivated successfully.` — output that reads like an orderly shutdown. That
is why the check is positive (every required path must be granted) and why a
blank `ReadWritePaths=` is a FAIL rather than an UNKNOWN.

### One remediation tool

```bash
scripts/remediate_owner_host_runtime.sh            # --check (default), read-only
scripts/remediate_owner_host_runtime.sh --apply
scripts/remediate_owner_host_runtime.sh --verify
```

It never starts, stops, restarts or relogins the Gateway, never enables the
timer, never touches firewall/OCI/SSH/VNC, and never prints Java command-line
arguments, an account id or IBC config content. `--apply` quarantines
contradicting drop-ins after a private `0600` backup and renames the stale
`/opt` copy only once the canonical one is verified. `--verify` proves the
Gateway PID and start time did not change.

> **Correcting the Gateway's process ownership requires a restart**, which is
> an owner-authorized action under `docs/runbooks/CHAOS_RECOVERY_DRILLS.md`.
> The tool makes the next start correct and proves the current one untouched.

### Owner-host activation status

**NOT complete.** Installation succeeded and was idempotent, `/run/ibgateway`
is correct, and the timer is still disabled — but the runtime contract was
wrong in the ways above. **B-02/B-03/B-04 remain OPEN, DEF-12/DEF-13 remain
`NOT_RUN`, IMP-1303/IMP-1304 remain incomplete, POI-IBKR-001 and POI-MD-001
remain BLOCKED, PAPER_AUTO and LIVE remain DISABLED** until this bundle is
merged and the remediation tool has been run.

Ports 4002 and 5901 listen on all interfaces; firewalld currently permits only
22, 80, 81 and 443 publicly; OCI remains UNKNOWN. Three separate facts, never
merged: a revocable firewall rule does not make an all-interfaces bind
acceptable. **No firewall, VNC or OCI change is made here.**

---

```bash
uv run --no-sync python scripts/ib_gateway_watchdog.py --status --json \
  > /tmp/watchdog-status-$(date -u +%Y%m%dT%H%M%SZ).json
```

### Proof levels

Each level proves a strictly different claim. A higher level is not "more
confident about the same thing".

| Level | Proves | Does **not** prove |
|---|---|---|
| `NONE` | nothing observed | — |
| `PROCESS_ONLY` | a matching process exists | nothing about the API or account |
| `TCP_OPEN` | the port accepts connections | **not** that the Gateway is logged in; **never** that the account is clean |
| `API_ROUND_TRIP` | the API answered a request | nothing about orders, positions or reconciliation |
| `FULL_ACCOUNT_SNAPSHOT` | a complete read-only classification | — the only level that can support `CLEAN` |

---

## 3. Host audit (read-only)

```bash
uv run --no-sync python scripts/host_operations_audit.py
uv run --no-sync python scripts/host_operations_audit.py --json \
  --json-out /tmp/host-audit.json --markdown-out /tmp/host-audit.md
```

Collects: architecture and OS · Python/uv availability · unit definitions and
actual `ExecStart` paths · Gateway/IBC/Java paths · active process count ·
listeners on 4002/5901 and whether they bind localhost or all interfaces ·
firewalld/nftables local state (read-only) · chrony/NTP synchronization ·
ownership and mode of the lifecycle journal, watchdog state, lock and IBC logs
· logrotate configuration · service restart policy · whether the broker package
the deep probe needs is importable.

It **redacts** usernames, account ids and credential-shaped values, **never**
prints IBC `config.ini` contents or environment secrets, **never** changes
firewall or service state, and exits non-zero on a security or readiness
failure.

---

## 4. Installing the assets

```bash
scripts/install_ops_assets.sh                      # CHECK / DRY-RUN (default)
scripts/install_ops_assets.sh --apply              # real install
scripts/install_ops_assets.sh --apply --no-reload  # skip the reload only
scripts/install_ops_assets.sh --rehearsal          # isolated REHEARSAL
```

That is the **whole** public interface. There is no destination-root option:
the rehearsal picks its own directory (see below).

Installs `ibgateway-ibc.service`, `ibgateway-watchdog.service`,
`ibgateway-watchdog.timer`, `/etc/tmpfiles.d/ibgateway.conf` and
`/etc/logrotate.d/ibgateway`.

**It never enables or starts a unit**, never touches firewall/OCI/SSH/VNC, and
never reads a credential.

### Execution matrix

| Mode | copies files | `systemctl daemon-reload` | `systemd-tmpfiles` | runtime directory |
|---|---|---|---|---|
| **CHECK** (default) | no | **never** | **never** | untouched (read-only) |
| **APPLY**, a unit changed | yes | **once** | `--create /etc/tmpfiles.d/ibgateway.conf` | create + verify `/run/ibgateway` |
| **APPLY**, nothing changed | no writes needed | no (nothing stale) | `--create …` | **create + verify** |
| **APPLY --no-reload** | yes | no | `--create …` | **create + verify** |
| **`--rehearsal`** | into a private scratch dir | **never** | **never** | validate rule + create + verify `<scratch>/run/ibgateway` |

CHECK mode runs **no system command at all** — it only diffs.

### Rehearsal is isolated in two independent ways

**1. It invokes no privileged command.** Neither `systemctl` nor
`systemd-tmpfiles` — not "invokes them harmlessly"; the calls are not written
in that branch, and a test parses the script to prove it.

That matters because staging the config elsewhere **isolates nothing**. The
rule is `d /run/ibgateway 0700 opc opc`: an **absolute** path. Running
`systemd-tmpfiles --create <scratch>/etc/tmpfiles.d/ibgateway.conf` would have
created and chowned the **real** `/run/ibgateway`. A rehearsal that mutates the
thing it is rehearsing is not a rehearsal.

**2. It writes only beneath a directory it creates itself.** The rehearsal ran
`install -D`, `mkdir -p` and `chmod` — and those three alone are enough to
overwrite a host given a bad destination. **Absence of privileged commands is
not containment.** There used to be a `--dest-root=DIR` option taking an
arbitrary path, concatenated straight onto the absolute destinations; with
`--dest-root=/` the "rehearsal" installed into the real `/etc/systemd/system`
and chmod'ed the real `/run/ibgateway`.

So `--dest-root` is gone from the public interface. `--rehearsal` creates its
own root with `mktemp -d` under a **fixed base**, and that root is:

- **newly created** by this invocation, and proven empty before use;
- **`0700`** and owned by the caller, both verified after creation;
- **not a symlink**, and canonically **not `/`**;
- a canonical **direct child of the approved base**;
- **printed**, and deliberately **never deleted** — the point is to inspect it.

Every single write then re-resolves its destination and proves it is still
beneath that root, so a symlink planted *after* validation cannot redirect it.

**The base is the literal `/tmp`, and no environment variable feeds it.** It
was `${TMPDIR:-/tmp}`, and `TMPDIR` is caller-controlled: `TMPDIR=/etc` (or
`/run`, or a symlink to one, or a relative path resolving into one) made a
*public* rehearsal create its directory inside a real system tree. The unique
leaf name made that narrower than the old `--dest-root=/` overwrite, but it
still contradicted the claim above — so **saying "there is no caller-supplied
destination" was only true once `TMPDIR` was removed from the decision.**
Before anything is created the fixed base is itself checked for symlinked
components, for being `/`, and for resolving into a system tree; the generated
root is then re-checked and must be a direct child of it.

### CHECK creates nothing

CHECK performs read-only path validation and diffs and puts **not one byte on
disk** — including the marker. Root handling is split: `resolve_rehearsal_root`
is read-only and always runs, while `establish_rehearsal_root` (which creates
the directory, sets `0700` and writes the marker) runs **only** under
`--apply`. Previously the root and its marker were created while establishing
the root, which happened before the CHECK exit — so a dry run wrote a file.

Rehearsal still validates the tmpfiles rule, creates and verifies
`<scratch>/run/ibgateway`, and reports what a real apply *would* do about the
reload, so that decision is observable without a privileged run. It cannot
`chown` to `opc:opc`, and it says so rather than implying otherwise.

> A deterministic destination is still needed by the test suite, so
> `--dest-root=` exists **only** when `OPS_INSTALL_TEST_MODE=1`; without it the
> script exits 2 and points at `--rehearsal`. Even in test mode the supplied
> path is canonicalized and rejected if it is empty, `/`, a symlink, reached
> through a symlinked parent, inside a system tree, an existing directory
> without this installer's `.ops-rehearsal-root` marker, not owned by the
> caller, or not `0700`.

### The runtime directory step is mandatory

Creation and verification of `/run/ibgateway` runs on **every** apply,
independently of whether any file changed and independently of `--no-reload`.
It was previously nested under `reload enabled AND changed > 0`, so an apply
where every asset already matched reported success while the directory could
be missing entirely. **A tmpfiles config on disk is not a runtime directory on
the filesystem**: `/run` is volatile, so the directory disappears on reboot
while the config still looks perfectly installed.

Verification checks existence, that it *is* a directory, mode `0700` and owner
`opc:opc` — read from the tmpfiles rule itself, so the installer and the
canonical owner can never drift apart. `--no-reload` suppresses the reload and
nothing else.

**Any failure exits non-zero**, prints no success message and gives no
instruction to enable the timer. The script distinguishes: files copied ·
daemon reload performed or intentionally skipped · tmpfiles validated or
applied · runtime directory created and verified · install COMPLETE or
INCOMPLETE. It previously ran tmpfiles with `|| true` and printed
`tmpfiles applied` regardless.

Enabling is an owner decision:

```bash
systemctl enable --now ibgateway-watchdog.timer   # passive supervision only
```

### One canonical Gateway service

There is exactly one: **`ibgateway-ibc.service`**. A second unit, a user-level
copy, a cron entry or a supervisor wrapper would be a competing restart
manager — two managers can race and produce two API clients on one account.
The host audit's `single_gateway_service` check FAILs if it finds another.

### Runtime directory: one owner, one lifetime

`/run/ibgateway` holds the watchdog state (the restart **budget**), the launch
lock and the order-lifecycle **marker**. Because the marker *inhibits*
unattended action and the budget *rate-limits* it, this directory is
supervision authority — losing it silently removes both brakes.

**`/etc/tmpfiles.d/ibgateway.conf` is its single canonical owner** (`0700`,
`opc:opc`, created at boot by `systemd-tmpfiles-setup.service`). **Neither unit
declares `RuntimeDirectory=`, and neither may.**

That is a correction, not a preference. `ibgateway-watchdog.service` is
`Type=oneshot`: it goes inactive the instant its cycle ends, and
`RuntimeDirectory=` is torn down when a unit goes inactive unless
`RuntimeDirectoryPreserve=` says otherwise. Both units previously declared it,
so **every watchdog cycle would have deleted the marker, the budget and the
lock** — a supervisor quietly erasing the two things that stop it acting, while
appearing healthy. `RuntimeDirectory=` is also wrong here on its own terms: the
directory must exist while the Gateway is **stopped** (exactly when the marker
matters) and before either unit has ever started.

Consequences, all deliberate:

- both units are ordered `After=systemd-tmpfiles-setup.service`, so reboot-time
  recreation is deterministic rather than a race;
- both grant `/run/ibgateway` in `ReadWritePaths=` with **no leading `-`** — a
  missing directory *fails the unit* instead of being ignored, because running
  the watchdog without its budget, lock and marker is the dangerous case;
- the CLI's last-resort `mkdir` sets `0700` explicitly (`mkdir(mode=…)` is
  masked by the umask) and never widens an existing directory — drift is a
  finding for the host audit, not something the watchdog papers over;
- `systemd-tmpfiles` failing during install is **fatal** (see §4).

`tests/ops/test_runtime_directory_ownership.py` parses the units and asserts
all of the above, so a re-added `RuntimeDirectory=` cannot pass review.

### State and lock file integrity

`watchdog-state.json` is written **atomically** (temp file in the same
directory, `fsync`, `os.replace`) at mode `0600`, enforced by an explicit
`chmod` rather than inherited from the invoking service's umask. A truncated
state file would load as an *empty* budget — that is, a fresh full set of
restarts — so a failed write must leave the previous valid state untouched.
`watchdog.lock` is likewise `0600` by explicit `chmod`, because `os.open`'s
mode argument is umask-masked.

Service hardening deliberately stops short of network namespacing: the Gateway
must keep binding its local API socket and the watchdog must keep reaching it,
so `RestrictAddressFamilies` permits local IP access on purpose.

---

## 5. Port security

See [`HOST_PORT_SECURITY.md`](HOST_PORT_SECURITY.md). Detection is automated;
remediation is not. **OCI network security cannot be verified from the host and
is always reported `UNKNOWN`.**

---

## 6. Owner-host evidence commands (NOT executed)

Run on the owner host, in this order. Every command below is read-only: none
mutates broker state, none restarts anything, none contains a quote or token.

```bash
# 1. Read-only host audit -> B-03/B-04 evidence
uv run --no-sync python scripts/host_operations_audit.py \
  --markdown-out /tmp/host-audit-$(date -u +%Y%m%dT%H%M%SZ).md \
  --json-out    /tmp/host-audit-$(date -u +%Y%m%dT%H%M%SZ).json

# 2. Read-only watchdog status (no launcher, no broker connection)
uv run --no-sync python scripts/ib_gateway_watchdog.py --status --json \
  > /tmp/watchdog-status-$(date -u +%Y%m%dT%H%M%SZ).json

# 3. Read-only deep probe (connects read-only; places nothing)
uv run --no-sync python scripts/ib_gateway_watchdog.py \
  --deep-probe --status --json \
  --account-alias <ALIAS> --secrets-root "$SECRETS_ROOT" \
  > /tmp/watchdog-deep-probe-$(date -u +%Y%m%dT%H%M%SZ).json

# 4. Asset installation, CHECK mode first
scripts/install_ops_assets.sh
```

Then review the artifacts, redact nothing further (they are already
sanitized), and record them. **Only after that review may B-03/B-04 and
DEF-12/DEF-13 change state — and only the owner may change them.**

---

## 7. Status

| Item | State |
|---|---|
| Account-safety probe, gate, CLI, units, audit, installer | ✅ IMPLEMENTED AND TESTED WITH FAKES |
| Owner-host installation | 🟡 READY FOR OWNER-HOST ACTIVATION |
| B-02 chaos drills | ❌ OPEN / NOT_RUN |
| B-03 IBC/watchdog host evidence | ❌ OPEN / NOT_RUN (DEF-12) |
| B-04 watchdog deep probe activation | ❌ OPEN / NOT_RUN (DEF-13) |
| POI-PLAT-ARM-001 | unchanged (PASS from its own earlier run; **not** re-proven here) |
| POI-IBKR-001 | **BLOCKED** — unchanged |
| IMP-1303 / IMP-1304 | **not complete** — unchanged |

### NOT EXECUTED

- ❌ no IBKR connection of any kind;
- ❌ no `systemctl` command run against a real host;
- ❌ no Gateway start, stop, restart or relogin;
- ❌ no order submitted, cancelled, modified or flattened;
- ❌ no firewall, nftables, firewalld or OCI change;
- ❌ no credential, account id or IBC config content added;
- ❌ no PAPER_AUTO, no LIVE.
