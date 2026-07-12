"use client";

/**
 * Approval queue (IMP-1104): approve / reject / watch / reanalyse /
 * reduce-quantity with expiry. DoD: an expired or materially changed plan
 * shows REFRESH REQUIRED and every plan-affirming action is blocked
 * fail-closed until refresh. Every action only RECORDS an audit fixture —
 * no SubmitAuthorization, no order, no Execution Agent, no broker.
 */

import { useState } from "react";

import { AppNav } from "./app-nav";
import {
  APPROVAL_FIXTURE_NOW,
  APPROVAL_QUEUE,
  APPROVAL_TTL_SECONDS,
  type ApprovalAuditRecord,
  type ApprovalItem,
  applyAction,
  isExpired,
  isMateriallyChanged,
  refreshItem,
  requiresRefresh,
} from "../lib/approvals";
import { chipClass, explain } from "../lib/explain";

export function ApprovalQueue({ operator, role }: { operator: string; role: string }) {
  // Fixture clock: deterministic review time (no live clock in the shell).
  const now = APPROVAL_FIXTURE_NOW;
  const [items, setItems] = useState<readonly ApprovalItem[]>(APPROVAL_QUEUE);
  const [audit, setAudit] = useState<readonly ApprovalAuditRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reduceTo, setReduceTo] = useState<string>("");

  const staleCount = items.filter(
    (item) =>
      requiresRefresh(item, now) && item.status !== "APPROVED" && item.status !== "REJECTED",
  ).length;

  function replaceItem(updated: ApprovalItem): void {
    setItems((current) =>
      current.map((item) => (item.approvalId === updated.approvalId ? updated : item)),
    );
  }

  function act(
    item: ApprovalItem,
    action: "APPROVE" | "REJECT" | "WATCH" | "REANALYSE" | "REDUCE_QUANTITY",
  ): void {
    setError(null);
    const result = applyAction(
      item,
      action,
      operator,
      now,
      action === "REDUCE_QUANTITY" ? { reduceTo } : undefined,
    );
    if (!result.ok) {
      setError(`${item.approvalId}: ${result.reason}`);
      return;
    }
    replaceItem(result.item);
    setAudit((current) => [...current, result.record]);
  }

  function refresh(item: ApprovalItem): void {
    setError(null);
    const refreshed = refreshItem(item, item.plan, now, APPROVAL_TTL_SECONDS, operator);
    replaceItem({ ...refreshed.item, currentPlanHash: refreshed.item.reviewedPlanHash });
    setAudit((current) => [...current, refreshed.record]);
  }

  return (
    <>
      <AppNav current="approvals" operator={operator} role={role} />
      <main className="console">
        <header className="console-header">
          <h1>Approval Queue</h1>
        </header>
        <p className="page-intro">
          Human review only. <strong>Approve records a decision</strong> — it never creates a
          SubmitAuthorization and never sends an order.
        </p>

        {staleCount > 0 ? (
          <div className="callout callout-danger" role="alert">
            <span className="callout-body">
              <strong>{staleCount} item(s) need a refresh</strong>
              {explain("REFRESH_REQUIRED")}
            </span>
          </div>
        ) : null}
        {error === null ? null : (
          <div className="callout callout-warn" role="alert">
            <span className="callout-body">
              <strong>Blocked</strong>
              {error}
            </span>
          </div>
        )}

        <section className="grid">
          {items.map((item) => {
            const stale = requiresRefresh(item, now);
            const terminal = item.status !== "PENDING" && item.status !== "WATCH";
            const displayStatus = stale && !terminal ? "REFRESH_REQUIRED" : item.status;
            const cause = isExpired(item, now)
              ? "the review window expired"
              : isMateriallyChanged(item)
                ? "the plan changed since review"
                : "";
            return (
              <article className="panel" key={item.approvalId}>
                <h2>
                  {item.symbol} · {item.direction}
                  <span className={chipClass(displayStatus)}>{displayStatus}</span>
                </h2>
                <p className="muted">
                  {item.approvalId} · candidate {item.candidateId} · expires {item.expiresAt}
                </p>

                {stale && !terminal ? (
                  <div className="callout callout-danger" role="status">
                    <span className="callout-body">
                      <strong>Refresh required</strong>
                      {explain("REFRESH_REQUIRED")}
                      {cause ? <small> Reason: {cause}.</small> : null}
                    </span>
                  </div>
                ) : null}

                <dl className="plan">
                  <dt>Plan</dt>
                  <dd>
                    {item.plan.entryType} · qty {item.plan.quantity} @ {item.plan.entryPrice}
                  </dd>
                  <dt>Stop / target</dt>
                  <dd>
                    {item.plan.stopPrice} / {item.plan.targetPrice ?? "—"} · risk{" "}
                    {item.plan.riskAmount}
                  </dd>
                </dl>

                <ul className="actions">
                  <li>
                    <button
                      type="button"
                      disabled={stale || terminal}
                      title={
                        stale
                          ? "blocked: refresh the plan first"
                          : "records a human review decision only"
                      }
                      onClick={() => act(item, "APPROVE")}
                    >
                      Approve
                    </button>
                    <span className="action-reason">record only — no order is sent</span>
                  </li>
                  <li>
                    <button type="button" disabled={terminal} onClick={() => act(item, "REJECT")}>
                      Reject
                    </button>
                    <button type="button" disabled={terminal} onClick={() => act(item, "WATCH")}>
                      Watch
                    </button>
                    <button
                      type="button"
                      disabled={stale || terminal}
                      onClick={() => act(item, "REANALYSE")}
                    >
                      Reanalyse
                    </button>
                  </li>
                  <li>
                    <input
                      aria-label={`reduce quantity for ${item.approvalId}`}
                      placeholder="reduce qty to…"
                      value={reduceTo}
                      onChange={(event) => setReduceTo(event.target.value)}
                    />
                    <button
                      type="button"
                      disabled={stale || terminal}
                      onClick={() => act(item, "REDUCE_QUANTITY")}
                    >
                      Reduce (down only)
                    </button>
                  </li>
                  {stale && !terminal ? (
                    <li>
                      <button type="button" onClick={() => refresh(item)}>
                        Refresh plan
                      </button>
                    </li>
                  ) : null}
                </ul>
              </article>
            );
          })}

          <article className="panel panel-wide">
            <h2>Audit trail</h2>
            {audit.length === 0 ? (
              <p className="empty">No actions recorded yet.</p>
            ) : (
              <ul>
                {audit.map((entry, index) => (
                  <li key={`${entry.approvalId}-${entry.action}-${index}`}>
                    <span className="chip chip-info">{entry.action}</span>{" "}
                    {entry.approvalId} → {entry.resultingStatus} ·{" "}
                    <span className="muted">
                      {entry.operator} at {entry.at}
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
