# Host port security — detect, document, never auto-change

> **Nothing in this repository changes your firewall, your OCI Security Lists
> or Network Security Groups, your SSH configuration or your VNC setup.**
> Detection is automated; remediation is an owner decision, performed by hand.
>
> That split is deliberate. A script that "helpfully" tightened a firewall on a
> cloud host can remove the only route back into that host, and a repository
> cannot know which route you are currently using. Detection is safe; blind
> remediation is not.

---

## Why this matters

The IB Gateway API port is an **unauthenticated control channel to a broker
account**. There is no password on it: anything that can open a TCP connection
to `4002` and speak the IB API can read the account and — outside the
read-only paths in this repository — place orders. The only thing standing
between the internet and that channel is network scoping.

VNC on `5901` is the same problem with a different shape: it exposes an
interactive desktop session that is, at that moment, logged into the broker.

## Recommended end state

| Surface | Recommended | Why |
|---|---|---|
| IBKR Paper API `4002` | reachable **only** from `127.0.0.1` | unauthenticated broker control channel |
| IBKR Live API `4001` | **not listening at all** | Live is disabled in this project |
| VNC `5901` | reachable **only** via an SSH tunnel | interactive session into a logged-in Gateway |
| SSH | preserved and reachable | never break your own access path |

An SSH tunnel gives VNC transport security and authentication without exposing
the port:

```bash
# On your workstation. Nothing on the host changes.
ssh -N -L 5901:127.0.0.1:5901 <user>@<host>
# then point the VNC client at 127.0.0.1:5901
```

## What is detected automatically

`scripts/host_operations_audit.py` is read-only and reports:

| Check | FAIL when |
|---|---|
| `port_4002_listener` | the Paper API is bound beyond `127.0.0.1` |
| `port_5901_listener` | VNC is bound beyond `127.0.0.1` |
| `port_4001_live_api` | the Live API port is listening at all |
| `nftables_ruleset` | WARN when no local packet filter is configured |
| `firewalld` | reported as observed state only |
| `oci_network_security` | **always `UNKNOWN`** — see below |

```bash
uv run --no-sync python scripts/host_operations_audit.py
uv run --no-sync python scripts/host_operations_audit.py --json \
  --json-out /tmp/host-audit.json
```

A FAIL makes the command exit non-zero.

## The honest limit: OCI network security is NOT verifiable from the host

The audit reports `oci_network_security` as **`UNKNOWN`, permanently and by
design.**

A host can see its own listening sockets and its own local packet filter. It
**cannot** see the cloud provider's Security Lists or Network Security Groups,
which sit in front of it. A host-local `nftables` rule proves nothing about
what OCI permits, and an empty local ruleset does not prove the port is exposed
either — the NSG may already be blocking it.

> **Never claim OCI network security is verified from repository or host-local
> firewall evidence alone.** Confirm the ingress rules in the OCI console, and
> record that confirmation separately as owner evidence.

## Remediation — owner actions, by hand

None of these are automated. Each is listed so the decision is informed, not so
a script can take it.

**1. Bind the API to localhost only.** In IB Gateway → *Configure* → *API* →
*Settings*, ensure "Allow connections from localhost only" is enabled. This is
the strongest control because it removes the listener from other interfaces
entirely, rather than filtering it afterwards.

**2. Restrict at the host firewall.** If `4002` or `5901` must remain bound
more widely for a local reason, restrict ingress with your existing tool
(`firewalld` or `nftables`). Review the rule before applying it, and keep an
open SSH session while you do — a mistake here can lock you out.

**3. Restrict at OCI.** Remove any ingress rule permitting `4002` or `5901`
from `0.0.0.0/0`. Keep SSH reachable from the addresses you actually use.

**4. Re-audit and record.**

```bash
uv run --no-sync python scripts/host_operations_audit.py \
  --markdown-out /tmp/host-audit.md --json-out /tmp/host-audit.json
```

Then record the OCI console confirmation alongside it, because the audit
cannot make that claim for you.

## What this does not do

- It does not open, close or modify any port.
- It does not install, enable or reload a firewall.
- It does not change SSH or VNC configuration.
- It does not touch OCI.
- It does not prove your host is secure — it reports a bounded set of
  observable facts, and marks everything else `UNKNOWN`.
