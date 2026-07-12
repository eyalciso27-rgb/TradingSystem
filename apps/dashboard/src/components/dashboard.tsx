"use client";

/**
 * Dashboard shell (IMP-1101): mode, health, P&L, risk, positions, alerts and
 * the halt banner over the authenticated SSE stream. Read-only — every
 * dangerous action renders visibly DISABLED from the DANGEROUS_ACTIONS
 * registry; there is no code path that submits, cancels, flattens or
 * resumes anything from this console.
 */

import { useEffect, useState } from "react";

import { AppNav } from "./app-nav";
import { chipClass } from "../lib/explain";
import { STATUS_STREAM_PATH } from "../lib/routes";
import { DANGEROUS_ACTIONS, type DashboardStatus } from "../lib/status";

export function Dashboard({ operator, role }: { operator: string; role: string }) {
  const [status, setStatus] = useState<DashboardStatus | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const source = new EventSource(STATUS_STREAM_PATH);
    source.addEventListener("status", (event) => {
      setConnected(true);
      setStatus(JSON.parse((event as MessageEvent<string>).data) as DashboardStatus);
    });
    source.onerror = () => setConnected(false);
    return () => source.close();
  }, []);

  const unhealthy = (status?.health ?? []).filter((item) => !item.healthy);
  const attention: string[] = [];
  if (status?.halt.active === true) {
    attention.push("Trading is halted.");
  }
  for (const item of unhealthy) {
    attention.push(`${item.component} is unhealthy.`);
  }

  return (
    <>
      <AppNav current="dashboard" operator={operator} role={role} />
      <main className="console">
        {status?.halt.active === true ? (
          <div className="halt-banner" role="alert">
            HALT ACTIVE — {status.halt.scope} ({status.halt.reasonCode}). Protective orders are
            kept.
          </div>
        ) : null}

        <header className="console-header">
          <h1>Dashboard</h1>
        </header>

        {status === null ? (
          <p className="callout callout-info" role="status">
            <span className="callout-body">
              <strong>Connecting to the live status stream…</strong>
              <small>The console is authenticated; waiting for the first update.</small>
            </span>
          </p>
        ) : attention.length > 0 ? (
          <div className="callout callout-warn" role="status">
            <span className="callout-body">
              <strong>Needs attention</strong>
              {attention.join(" ")}
            </span>
          </div>
        ) : (
          <div className="callout callout-info" role="status">
            <span className="callout-body">
              <strong>All systems nominal</strong>
              <small>Everything healthy · read-only paper observation.</small>
            </span>
          </div>
        )}

        <div className="kpi-grid">
          <div className="kpi">
            <p className="kpi-label">Mode</p>
            <p className="kpi-value">{status?.mode ?? "—"}</p>
            <p className="kpi-sub">{status?.environment ?? "PAPER"}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Live updates</p>
            <p className="kpi-value">
              <span className={chipClass(connected ? "LIVE" : "DISCONNECTED")}>
                {connected ? "live" : "disconnected"}
              </span>
            </p>
            <p className="kpi-sub">as of {status?.asOf ?? "—"}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Realized P&amp;L (today)</p>
            <p className="kpi-value">
              {status?.pnl.realizedToday ?? "—"} {status?.pnl.currency ?? ""}
            </p>
            <p className="kpi-sub">unrealized {status?.pnl.unrealized ?? "—"}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Open risk</p>
            <p className="kpi-value">{status?.risk.openRiskAmount ?? "—"}</p>
            <p className="kpi-sub">
              envelope {status?.risk.openRiskEnvelopeUsedPct ?? "—"}% · positions{" "}
              {status?.risk.concurrentPositions ?? "—"}/{status?.risk.maxConcurrentPositions ?? "—"}
            </p>
          </div>
        </div>

        <section className="grid">
          <article className="panel">
            <h2>Component health</h2>
            <ul className="actions">
              {(status?.health ?? []).map((item) => (
                <li key={item.component}>
                  <span className={chipClass(item.healthy ? "HEALTHY" : "UNHEALTHY")}>
                    {item.healthy ? "healthy" : "unhealthy"}
                  </span>
                  <span>{item.component}</span>
                </li>
              ))}
              {status === null ? <li className="empty">waiting…</li> : null}
            </ul>
          </article>

          <article className="panel panel-wide">
            <h2>Open positions</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Qty</th>
                    <th>Avg price</th>
                    <th>Protected</th>
                    <th>Account</th>
                  </tr>
                </thead>
                <tbody>
                  {(status?.positions ?? []).map((position) => (
                    <tr key={position.symbol}>
                      <td>{position.symbol}</td>
                      <td>{position.quantity}</td>
                      <td>{position.averagePrice}</td>
                      <td>
                        {position.protectedQuantity}/{position.quantity}
                      </td>
                      <td>{position.accountAliasMasked}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {status !== null && status.positions.length === 0 ? (
              <p className="empty">No open positions.</p>
            ) : null}
          </article>

          <article className="panel">
            <h2>Alerts</h2>
            {status !== null && status.alerts.length === 0 ? (
              <p className="empty">No alerts.</p>
            ) : (
              <ul className="actions">
                {(status?.alerts ?? []).map((alert) => (
                  <li key={alert.alertId}>
                    <span className={`chip chip-${alert.severity === "CRITICAL" ? "danger" : alert.severity === "WARNING" ? "warn" : "info"}`}>
                      {alert.severity}
                    </span>
                    <span>{alert.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </article>

          <article className="panel panel-wide">
            <h2>Emergency &amp; trading actions</h2>
            <p className="page-intro">
              These are disabled in this console by design — each requires the full pipeline
              (Risk → Pre-submit → Authorization → Execution Agent) or the emergency runbook.
            </p>
            <ul className="actions">
              {DANGEROUS_ACTIONS.map((entry) => (
                <li key={entry.action}>
                  <button type="button" disabled title={entry.reason}>
                    {entry.action}
                  </button>
                  <span className="action-reason">disabled — {entry.reason}</span>
                </li>
              ))}
            </ul>
          </article>
        </section>
      </main>
    </>
  );
}
