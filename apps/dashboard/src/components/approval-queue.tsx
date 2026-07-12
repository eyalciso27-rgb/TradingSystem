"use client";

/**
 * Approval queue (IMP-1104): approve / reject / watch / reanalyse /
 * reduce-quantity with expiry. DoD: an expired or materially changed plan
 * shows REFRESH REQUIRED and every plan-affirming action is blocked
 * fail-closed until refresh. Every action only RECORDS an audit fixture —
 * no SubmitAuthorization, no order, no Execution Agent, no broker.
 */

import { useState } from "react";

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

function StaleBanner({ item, now }: { item: ApprovalItem; now: string }) {
  if (!requiresRefresh(item, now)) {
    return null;
  }
  const cause = isExpired(item, now) ? "EXPIRED" : "PLAN CHANGED";
  return (
    <p className="runtime-bad" role="alert">
      REFRESH REQUIRED ({cause}) — approve/reduce/reanalyse are blocked until refresh.
    </p>
  );
}

export function ApprovalQueue({ operator, role }: { operator: string; role: string }) {
  // Fixture clock: deterministic review time (no live clock in the shell).
  const now = APPROVAL_FIXTURE_NOW;
  const [items, setItems] = useState<readonly ApprovalItem[]>(APPROVAL_QUEUE);
  const [audit, setAudit] = useState<readonly ApprovalAuditRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reduceTo, setReduceTo] = useState<string>("");

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
    // Fixture refresh: re-issue against the current plan (upstream hash).
    const refreshed = refreshItem(item, item.plan, now, APPROVAL_TTL_SECONDS, operator);
    replaceItem({ ...refreshed.item, currentPlanHash: refreshed.item.reviewedPlanHash });
    setAudit((current) => [...current, refreshed.record]);
  }

  return (
    <main className="console">
      <header className="console-header">
        <h1>Approval Queue</h1>
        <a className="nav-link" href="/">
          ← Dashboard
        </a>
        <a className="nav-link" href="/candidates">
          Candidates →
        </a>
        <span className="operator">
          {operator} ({role})
        </span>
      </header>
      <p className="env-tag">
        Review recording only. APPROVE creates an audit record — never a
        SubmitAuthorization, never an order.
      </p>
      {error === null ? null : (
        <p className="runtime-bad" role="alert">
          {error}
        </p>
      )}

      <section className="grid">
        {items.map((item) => {
          const stale = requiresRefresh(item, now);
          const terminal = item.status !== "PENDING" && item.status !== "WATCH";
          return (
            <article className="panel" key={item.approvalId}>
              <h2>
                {item.symbol} · {item.direction} ·{" "}
                <span className={stale || terminal ? "runtime-bad" : "runtime-ok"}>
                  {stale && !terminal ? "REFRESH_REQUIRED" : item.status}
                </span>
              </h2>
              <p className="muted">
                {item.approvalId} · candidate {item.candidateId} · issued {item.issuedAt} ·
                expires {item.expiresAt}
              </p>
              <StaleBanner item={item} now={now} />
              {isMateriallyChanged(item) ? (
                <p className="muted">
                  reviewed plan {item.reviewedPlanHash} ≠ current {item.currentPlanHash}
                </p>
              ) : null}
              <dl className="plan">
                <dt>Plan</dt>
                <dd>
                  {item.plan.entryType} · qty {item.plan.quantity} @ {item.plan.entryPrice} ·
                  stop {item.plan.stopPrice} · target {item.plan.targetPrice ?? "—"} · risk{" "}
                  {item.plan.riskAmount}
                </dd>
              </dl>
              <ul className="actions">
                <li>
                  <button
                    type="button"
                    disabled={stale || terminal}
                    title="records a human review decision only"
                    onClick={() => act(item, "APPROVE")}
                  >
                    APPROVE (record only)
                  </button>
                </li>
                <li>
                  <button type="button" disabled={terminal} onClick={() => act(item, "REJECT")}>
                    REJECT
                  </button>
                  <button type="button" disabled={terminal} onClick={() => act(item, "WATCH")}>
                    WATCH
                  </button>
                  <button
                    type="button"
                    disabled={stale || terminal}
                    onClick={() => act(item, "REANALYSE")}
                  >
                    REANALYSE
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
                    REDUCE
                  </button>
                </li>
                {stale && !terminal ? (
                  <li>
                    <button type="button" onClick={() => refresh(item)}>
                      REFRESH PLAN
                    </button>
                  </li>
                ) : null}
              </ul>
            </article>
          );
        })}

        <article className="panel panel-wide">
          <h2>Audit trail (fixture records only)</h2>
          {audit.length === 0 ? (
            <p className="muted">no actions recorded yet</p>
          ) : (
            <ul>
              {audit.map((entry, index) => (
                <li key={`${entry.approvalId}-${entry.action}-${index}`}>
                  [{entry.at}] {entry.operator}: {entry.action} on {entry.approvalId} →{" "}
                  {entry.resultingStatus} · {entry.detail}
                </li>
              ))}
            </ul>
          )}
        </article>
      </section>
    </main>
  );
}
