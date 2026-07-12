"use client";

/**
 * Dashboard shell (IMP-1101): mode, health, P&L, risk, positions, alerts
 * and the halt banner, updating live over the authenticated SSE stream.
 * Read-only: every dangerous action renders visibly DISABLED from the
 * DANGEROUS_ACTIONS registry — there is no code path that submits,
 * cancels, flattens or resumes anything from this console.
 */

import { useEffect, useState } from "react";

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

  return (
    <main className="console">
      {status?.halt.active === true ? (
        <div className="halt-banner" role="alert">
          HALT ACTIVE — {status.halt.scope} ({status.halt.reasonCode}); protective orders kept
        </div>
      ) : null}
      <header className="console-header">
        <h1>Operations Console</h1>
        <span className={`mode mode-${status?.mode ?? "UNKNOWN"}`}>
          {status?.mode ?? "connecting…"} / {status?.environment ?? "PAPER"}
        </span>
        <span className="stream-state">
          SSE: {connected ? "live" : "disconnected"} · as of {status?.asOf ?? "—"}
        </span>
        <a className="nav-link" href="/sources">
          Source connections →
        </a>
        <a className="nav-link" href="/candidates">
          Candidates →
        </a>
        <a className="nav-link" href="/approvals">
          Approvals →
        </a>
        <span className="operator">
          {operator} ({role}, read-only)
        </span>
      </header>

      <section className="grid">
        <article className="panel">
          <h2>Health</h2>
          <ul>
            {(status?.health ?? []).map((item) => (
              <li key={item.component} className={item.healthy ? "ok" : "bad"}>
                {item.component}: {item.healthy ? "healthy" : "UNHEALTHY"}
              </li>
            ))}
          </ul>
        </article>

        <article className="panel">
          <h2>P&amp;L</h2>
          <dl>
            <dt>Realized today</dt>
            <dd>
              {status?.pnl.realizedToday ?? "—"} {status?.pnl.currency ?? ""}
            </dd>
            <dt>Unrealized</dt>
            <dd>
              {status?.pnl.unrealized ?? "—"} {status?.pnl.currency ?? ""}
            </dd>
          </dl>
        </article>

        <article className="panel">
          <h2>Risk</h2>
          <dl>
            <dt>Open risk</dt>
            <dd>{status?.risk.openRiskAmount ?? "—"}</dd>
            <dt>Envelope used</dt>
            <dd>{status?.risk.openRiskEnvelopeUsedPct ?? "—"}%</dd>
            <dt>Positions</dt>
            <dd>
              {status?.risk.concurrentPositions ?? "—"} / {status?.risk.maxConcurrentPositions ?? "—"}
            </dd>
          </dl>
        </article>

        <article className="panel panel-wide">
          <h2>Positions</h2>
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
                  <td>{position.protectedQuantity}</td>
                  <td>{position.accountAliasMasked}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>

        <article className="panel panel-wide">
          <h2>Alerts</h2>
          <ul>
            {(status?.alerts ?? []).map((alert) => (
              <li key={alert.alertId} className={`alert-${alert.severity}`}>
                [{alert.severity}] {alert.message} · {alert.raisedAt}
              </li>
            ))}
          </ul>
        </article>

        <article className="panel panel-wide">
          <h2>Actions (disabled by policy)</h2>
          <ul className="actions">
            {DANGEROUS_ACTIONS.map((entry) => (
              <li key={entry.action}>
                <button type="button" disabled title={entry.reason}>
                  {entry.action}
                </button>
                <span className="action-reason">{entry.reason}</span>
              </li>
            ))}
          </ul>
        </article>
      </section>
    </main>
  );
}
