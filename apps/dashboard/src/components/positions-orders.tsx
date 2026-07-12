"use client";

/**
 * Positions / orders / reconciliation screen (IMP-1105). Read-only: no
 * approve/submit/cancel exists here. DoD: only CANONICAL order states are
 * rendered (guarded at render time by isCanonicalOrderState); UNKNOWN is
 * shown as "RECONCILE REQUIRED", never as a positive state.
 */

import {
  FILL_ROWS,
  MISMATCH_ROWS,
  ORDER_ROWS,
  POSITION_ROWS,
  type OrderRow,
  isCanonicalOrderState,
  needsReconcile,
} from "../lib/positions";

function stateLabel(order: OrderRow): { text: string; className: string } {
  // Render-time guard: anything non-canonical is treated like UNKNOWN and
  // demands reconciliation — a raw broker string can never reach the screen.
  if (!isCanonicalOrderState(order.state) || needsReconcile(order.state)) {
    return { text: "RECONCILE REQUIRED (UNKNOWN)", className: "runtime-bad" };
  }
  const closed = ["CANCELLED", "CANCELLED_BY_POLICY", "REJECTED", "EXPIRED"];
  return {
    text: order.state,
    className: closed.includes(order.state) ? "muted" : "runtime-ok",
  };
}

export function PositionsOrders({ operator, role }: { operator: string; role: string }) {
  return (
    <main className="console">
      <header className="console-header">
        <h1>Positions · Orders · Reconciliation</h1>
        <a className="nav-link" href="/">
          ← Dashboard
        </a>
        <a className="nav-link" href="/approvals">
          Approvals →
        </a>
        <span className="operator">
          {operator} ({role}, read-only)
        </span>
      </header>
      <p className="env-tag">
        Read-only mirror of canonical state (never broker raw status). No
        action can be taken from this screen.
      </p>

      <section className="grid">
        <article className="panel panel-wide">
          <h2>Positions (stops · targets · protection)</h2>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Dir</th>
                <th>Qty</th>
                <th>Avg</th>
                <th>Stop</th>
                <th>Target</th>
                <th>Protected</th>
                <th>Account</th>
              </tr>
            </thead>
            <tbody>
              {POSITION_ROWS.map((position) => (
                <tr key={position.positionRef}>
                  <td>{position.symbol}</td>
                  <td>{position.direction}</td>
                  <td>{position.quantity}</td>
                  <td>{position.averagePrice}</td>
                  <td>{position.stopPrice ?? "—"}</td>
                  <td>{position.targetPrice ?? "—"}</td>
                  <td
                    className={
                      position.protectedQuantity === position.quantity
                        ? "runtime-ok"
                        : "runtime-bad"
                    }
                  >
                    {position.protectedQuantity}/{position.quantity}
                  </td>
                  <td>{position.accountAliasMasked}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>

        <article className="panel panel-wide">
          <h2>Orders (canonical states)</h2>
          <table>
            <thead>
              <tr>
                <th>Order</th>
                <th>Symbol</th>
                <th>Role</th>
                <th>State</th>
                <th>Qty</th>
                <th>Filled</th>
                <th>Remaining</th>
              </tr>
            </thead>
            <tbody>
              {ORDER_ROWS.map((order) => {
                const label = stateLabel(order);
                return (
                  <tr key={order.clientOrderId}>
                    <td>{order.clientOrderId}</td>
                    <td>{order.symbol}</td>
                    <td>{order.role}</td>
                    <td className={label.className}>{label.text}</td>
                    <td>{order.quantity}</td>
                    <td>{order.filledQuantity}</td>
                    <td>{order.remainingQuantity}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </article>

        <article className="panel">
          <h2>Fills</h2>
          <ul>
            {FILL_ROWS.map((fill) => (
              <li key={fill.fillId}>
                {fill.fillId}: {fill.symbol} {fill.quantity} @ {fill.price} ·{" "}
                {fill.clientOrderId} · {fill.filledAt}
              </li>
            ))}
          </ul>
        </article>

        <article className="panel">
          <h2>Reconciliation mismatches</h2>
          {MISMATCH_ROWS.length === 0 ? (
            <p className="runtime-ok">none — reconciled</p>
          ) : (
            <ul>
              {MISMATCH_ROWS.map((mismatch) => (
                <li key={`${mismatch.kind}-${mismatch.subjectRef}`} className="runtime-bad">
                  [{mismatch.kind}] {mismatch.subjectRef}: expected {mismatch.expected},
                  observed {mismatch.observed} → <strong>{mismatch.recommendation}</strong>
                </li>
              ))}
            </ul>
          )}
        </article>
      </section>
    </main>
  );
}
