/**
 * Positions / orders / reconciliation screen (IMP-1105). Read-only: no
 * approve/submit/cancel exists here. DoD: only CANONICAL order states are
 * rendered (guarded at render time by isCanonicalOrderState); UNKNOWN is
 * shown as "RECONCILE REQUIRED", never as a positive state.
 */

import { AppNav } from "./app-nav";
import { chipClass } from "../lib/explain";
import {
  FILL_ROWS,
  MISMATCH_ROWS,
  ORDER_ROWS,
  POSITION_ROWS,
  type OrderRow,
  isCanonicalOrderState,
  needsReconcile,
} from "../lib/positions";

function stateChip(order: OrderRow): { text: string; className: string } {
  // Render-time guard: anything non-canonical is treated like UNKNOWN and
  // demands reconciliation — a raw broker string can never reach the screen.
  if (!isCanonicalOrderState(order.state) || needsReconcile(order.state)) {
    return { text: "RECONCILE REQUIRED (UNKNOWN)", className: chipClass("RECONCILE_REQUIRED") };
  }
  const closed = ["CANCELLED", "CANCELLED_BY_POLICY", "REJECTED", "EXPIRED"];
  return {
    text: order.state,
    className: closed.includes(order.state) ? "chip chip-muted" : "chip chip-ok",
  };
}

export function PositionsOrders({ operator, role }: { operator: string; role: string }) {
  const reconcileCount = ORDER_ROWS.filter((order) => needsReconcile(order.state)).length;
  const underProtected = POSITION_ROWS.filter(
    (position) => position.protectedQuantity !== position.quantity,
  ).length;

  return (
    <>
      <AppNav current="positions" operator={operator} role={role} />
      <main className="console">
        <header className="console-header">
          <h1>Positions · Orders · Reconciliation</h1>
        </header>
        <p className="page-intro">
          A read-only mirror of the system&apos;s canonical state — never the broker&apos;s raw
          status. No action can be taken here.
        </p>

        {reconcileCount > 0 || MISMATCH_ROWS.length > 0 ? (
          <div className="callout callout-danger" role="alert">
            <span className="callout-body">
              <strong>Reconciliation needed</strong>
              {reconcileCount} order(s) in an unconfirmed state and {MISMATCH_ROWS.length}{" "}
              mismatch(es). New entries stay blocked until an operator reconciles.
            </span>
          </div>
        ) : (
          <div className="callout callout-info" role="status">
            <span className="callout-body">
              <strong>Reconciled</strong>
              <small>Canonical state matches the broker snapshot.</small>
            </span>
          </div>
        )}

        <section className="grid">
          <article className="panel panel-wide">
            <h2>
              Positions
              <span className="muted">stops · targets · protection</span>
            </h2>
            {underProtected > 0 ? (
              <p className="action-reason">
                {underProtected} position(s) not fully protected — highlighted below.
              </p>
            ) : null}
            <div className="table-wrap">
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
                      <td>
                        <span
                          className={
                            position.protectedQuantity === position.quantity
                              ? "chip chip-ok"
                              : "chip chip-danger"
                          }
                        >
                          {position.protectedQuantity}/{position.quantity}
                        </span>
                      </td>
                      <td>{position.accountAliasMasked}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>

          <article className="panel panel-wide">
            <h2>
              Orders
              <span className="muted">canonical states only</span>
            </h2>
            <div className="table-wrap">
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
                    const chip = stateChip(order);
                    return (
                      <tr key={order.clientOrderId}>
                        <td>{order.clientOrderId}</td>
                        <td>{order.symbol}</td>
                        <td>{order.role}</td>
                        <td>
                          <span className={chip.className}>{chip.text}</span>
                        </td>
                        <td>{order.quantity}</td>
                        <td>{order.filledQuantity}</td>
                        <td>{order.remainingQuantity}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </article>

          <article className="panel">
            <h2>Fills</h2>
            {FILL_ROWS.length === 0 ? (
              <p className="empty">No fills yet.</p>
            ) : (
              <ul>
                {FILL_ROWS.map((fill) => (
                  <li key={fill.fillId}>
                    {fill.symbol} {fill.quantity} @ {fill.price} · {fill.clientOrderId} ·{" "}
                    <span className="muted">{fill.filledAt}</span>
                  </li>
                ))}
              </ul>
            )}
          </article>

          <article className="panel">
            <h2>Reconciliation mismatches</h2>
            {MISMATCH_ROWS.length === 0 ? (
              <p className="empty">None — reconciled.</p>
            ) : (
              <ul className="actions">
                {MISMATCH_ROWS.map((mismatch) => (
                  <li key={`${mismatch.kind}-${mismatch.subjectRef}`}>
                    <span className={chipClass(mismatch.recommendation)}>
                      {mismatch.recommendation}
                    </span>
                    <span>
                      <strong>{mismatch.kind}</strong> · {mismatch.subjectRef}: expected{" "}
                      {mismatch.expected}, observed {mismatch.observed}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </article>
        </section>
      </main>
    </>
  );
}
