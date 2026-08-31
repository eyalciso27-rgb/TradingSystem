import * as React from "react";
import { AppNav } from "./app-nav";
import {
  EXECUTION_LIFECYCLE,
  displayValue,
  type DecisionView,
  type ExecutionStage,
  type LoadState,
  type OperationsSnapshot,
} from "../lib/operations";

const danger = new Set(["ACTIVE", "BLOCKED", "FAILED", "DISCONNECTED", "UNAVAILABLE", "MISSING", "RISK_REJECTED", "HALT_BLOCKED", "AUTHORIZATION_DENIED", "NEEDS_RECONCILIATION", "UNKNOWN"]);
const warning = new Set(["STALE", "DEGRADED", "READINESS_BLOCKED", "STRATEGY_REJECTED", "NO_TRADE", "PAPER_AUTO_DISABLED", "DENY_ALL"]);
const ok = new Set(["PAPER", "CONNECTED", "USABLE", "CLEAN", "INACTIVE", "OK", "PRESENT", "SYSTEM_OWNED", "RUNNING", "READY_DENYALL", "SUBMITTED"]);

export function stateClass(value: string): string {
  if (danger.has(value)) return "chip chip-danger";
  if (warning.has(value)) return "chip chip-warn";
  if (ok.has(value)) return "chip chip-ok";
  return "chip chip-muted";
}

function Unknown({ value, suffix = "" }: { value: string | number | boolean | null | undefined; suffix?: string }) {
  const rendered = displayValue(value);
  return <span className={rendered === "UNKNOWN" ? "unknown-value" : undefined}>{rendered}{rendered === "UNKNOWN" ? "" : suffix}</span>;
}

export function DataSourceBanner({ snapshot }: { snapshot: OperationsSnapshot }) {
  return <div className="mock-banner" role="note"><strong>MOCK DATA</strong> {snapshot.sourceLabel}</div>;
}

export function OperationsLoadState({ state }: { state: LoadState<OperationsSnapshot> }) {
  if (state.status === "loading") return <div className="callout callout-info" role="status"><span className="callout-body"><strong>Loading operations state…</strong>Critical values remain UNKNOWN until a snapshot arrives.</span></div>;
  if (state.status === "error") return <div className="callout callout-danger" role="alert"><span className="callout-body"><strong>Operations state unavailable — UNKNOWN</strong>{state.message}</span></div>;
  return <DataSourceBanner snapshot={state.data} />;
}

function Shell({ current, title, operator, role, snapshot, children }: { current: Parameters<typeof AppNav>[0]["current"]; title: string; operator: string; role: string; snapshot: OperationsSnapshot; children: React.ReactNode }) {
  return <><AppNav current={current} operator={operator} role={role} /><main className="console"><header className="console-header"><h1>{title}</h1></header><DataSourceBanner snapshot={snapshot} />{children}</main></>;
}

export function OperationsOverview({ operator, role, snapshot }: { operator: string; role: string; snapshot: OperationsSnapshot }) {
  const { runtime, account, trading } = snapshot;
  return <Shell current="dashboard" title="Operations Overview" operator={operator} role={role} snapshot={snapshot}>
    {runtime.halt === "ACTIVE" ? <div className="halt-banner" role="alert">HALT ACTIVE — new entries are blocked</div> : null}
    <div className="status-grid" aria-label="Critical operating state">
      {[["Environment", runtime.environment, runtime.liveProhibited === true ? "LIVE PROHIBITED" : displayValue(runtime.liveProhibited)], ["Automation / authorization", runtime.authorizationMode, "Paper Auto is not enabled"], ["Runtime", runtime.runtime, trading.blockingReason], ["IBKR", runtime.ibkr, "Paper gateway"], ["Market data", runtime.marketData, runtime.marketDataObservedAt ? `${runtime.marketDataObservedAt} · age ${displayValue(runtime.marketDataAgeSeconds)}s` : "UNKNOWN"], ["Reconciliation", runtime.reconciliation, runtime.reconciledAt], ["Halt", runtime.halt, runtime.halt === "ACTIVE" ? "Entries blocked" : null]].map(([label, value, detail]) => <article className={`status-card ${danger.has(String(value)) ? "status-card-danger" : ""}`} key={String(label)}><p className="kpi-label">{label}</p><span className={stateClass(String(value))}>{value}</span><p className="kpi-sub"><Unknown value={detail as string | null} /></p></article>)}
    </div>
    <section className="grid">
      <article className="panel"><h2>Account summary</h2><dl><dt>Session P&amp;L</dt><dd><Unknown value={account.sessionPnl} /> {account.sessionPnl ? displayValue(account.currency) : ""}</dd><dt>Open positions</dt><dd><Unknown value={account.openPositions} /></dd><dt>Open orders</dt><dd><Unknown value={account.openOrders} /></dd></dl></article>
      <article className="panel"><h2>Trading summary</h2><dl><dt>Strategy evaluated</dt><dd><Unknown value={trading.lastStrategyEvaluation} /></dd><dt>Decision</dt><dd><Unknown value={trading.lastDecision} /></dd><dt>Risk</dt><dd><Unknown value={trading.lastRiskResult} /></dd><dt>Execution</dt><dd><Unknown value={trading.lastExecution} /></dd><dt>Blocking reason</dt><dd className="bad"><Unknown value={trading.blockingReason} /></dd></dl></article>
    </section>
  </Shell>;
}

export function PositionsScreen({ operator, role, snapshot }: { operator: string; role: string; snapshot: OperationsSnapshot }) {
  const unknown = snapshot.positions.filter((p) => p.classification === "UNKNOWN").length;
  return <Shell current="positions" title="Positions" operator={operator} role={role} snapshot={snapshot}>
    {unknown > 0 ? <div className="callout callout-danger" role="alert"><span className="callout-body"><strong>{unknown} UNKNOWN / unattributed position</strong>Unknown positions are safety-relevant and block new entries until attribution and reconciliation are complete.</span></div> : null}
    <article className="panel panel-wide"><div className="table-wrap"><table><thead><tr><th>Symbol / conId</th><th>Side / qty</th><th>Avg / last</th><th>Unrealized P&amp;L</th><th>Attribution</th><th>Protective STOP</th><th>Classification</th><th>Observed</th></tr></thead><tbody>{snapshot.positions.map((p) => <tr key={p.positionId} className={p.classification === "UNKNOWN" ? "unknown-row" : undefined}><td><strong><Unknown value={p.symbol} /></strong><br/><small>conId <Unknown value={p.conId} /></small></td><td><Unknown value={p.side} /> · <Unknown value={p.quantity} /></td><td><Unknown value={p.averagePrice} /> / <Unknown value={p.lastPrice} /></td><td><Unknown value={p.unrealizedPnl} /></td><td>Strategy: <Unknown value={p.strategyAttribution} /><br/>Source: <Unknown value={p.sourceAttribution} /></td><td><span className={stateClass(displayValue(p.protectiveStopStatus))}>{displayValue(p.protectiveStopStatus)}</span><br/><Unknown value={p.protectiveStopPrice} /></td><td><span className={stateClass(p.classification)}>{p.classification}</span></td><td><Unknown value={p.observedAt} /></td></tr>)}</tbody></table></div></article>
  </Shell>;
}

function DecisionCard({ decision }: { decision: DecisionView }) { return <article className="panel decision-card"><h2><Unknown value={decision.symbol} /> <span className={stateClass(decision.outcome)}>{decision.outcome}</span></h2><p className="muted">{decision.decisionId} · strategy <Unknown value={decision.strategyId} /> v<Unknown value={decision.strategyVersion} /> · <Unknown value={decision.decidedAt} /></p><p><strong>Why:</strong> <Unknown value={decision.explanation} /></p><p>{decision.reasonCodes.length ? decision.reasonCodes.map((r) => <code className="reason-code" key={r}>{r}</code>) : <span className="unknown-value">UNKNOWN</span>}</p></article>; }

export function DecisionsScreen({ operator, role, snapshot, approvals = false }: { operator: string; role: string; snapshot: OperationsSnapshot; approvals?: boolean }) {
  return <Shell current={approvals ? "approvals" : "candidates"} title={approvals ? "Authorization Outcomes" : "Candidates & Decisions"} operator={operator} role={role} snapshot={snapshot}><p className="page-intro">Backend truth only. This console does not reproduce strategy, risk, readiness, or authorization logic.</p><section className="grid">{snapshot.decisions.map((d) => <DecisionCard key={d.decisionId} decision={d} />)}</section></Shell>;
}

export function ExecutionHistoryScreen({ operator, role, snapshot }: { operator: string; role: string; snapshot: OperationsSnapshot }) {
  return <Shell current="executions" title="Execution & Audit History" operator={operator} role={role} snapshot={snapshot}><p className="page-intro">Read-only evidence for why a trade happened—or why it stopped before submission. Scoped/manual proof authorization is distinct from autonomous PAPER_AUTO authorization, which follows the runtime authorization mode.</p><section className="execution-list">{snapshot.executions.map((e) => <article className="panel panel-wide" key={e.executionId}><h2>{e.executionId} · <Unknown value={e.symbol} /> <span className={stateClass(e.lifecycleState)}>{e.lifecycleState}</span></h2><div className="lifecycle" aria-label={`Lifecycle for ${e.executionId}`}>{EXECUTION_LIFECYCLE.map((stage) => <span key={stage} className={`lifecycle-stage ${e.reachedStages.includes(stage) ? "reached" : "not-reached"}`}><b>{stage}</b><small>{e.timestamps[stage] ?? "NOT REACHED"}</small></span>)}</div><div className="execution-detail"><dl><dt>Decision</dt><dd><Unknown value={e.decisionId} /></dd><dt>Client order</dt><dd><Unknown value={e.clientOrderId} /></dd><dt>Correlation</dt><dd><Unknown value={e.correlationId} /></dd><dt>Strategy</dt><dd><Unknown value={e.strategyId} /> v<Unknown value={e.strategyVersion} /></dd><dt>Instrument</dt><dd><Unknown value={e.symbol} /> · conId <Unknown value={e.conId} /></dd><dt>Order</dt><dd><Unknown value={e.side} /> <Unknown value={e.quantity} /> · <Unknown value={e.orderType} /> @ <Unknown value={e.entryPrice} /></dd><dt>Protective stop</dt><dd><Unknown value={e.protectiveStopPrice} /></dd></dl><dl><dt>Risk</dt><dd><Unknown value={e.riskOutcome} /></dd><dt>Authorization</dt><dd><Unknown value={e.authorizationOutcome} /></dd><dt>Broker refs</dt><dd>{e.brokerOrderReferences.length ? e.brokerOrderReferences.join(", ") : <span className="unknown-value">UNKNOWN</span>}</dd><dt>Blocking reason</dt><dd><Unknown value={e.blockingReason} /></dd><dt>Failure reason</dt><dd><Unknown value={e.failureReason} /></dd><dt>Reconciliation</dt><dd><span className={stateClass(e.reconciliationState)}>{e.reconciliationState}</span></dd></dl></div></article>)}</section></Shell>;
}

export function EmergencyReadOnlyScreen({ operator, role, snapshot }: { operator: string; role: string; snapshot: OperationsSnapshot }) { const e = snapshot.emergency; return <Shell current="emergency" title="Emergency & Safety" operator={operator} role={role} snapshot={snapshot}>{e.halt === "ACTIVE" ? <div className="halt-banner" role="alert">HALT ACTIVE — {displayValue(e.scope)} · {displayValue(e.reason)}</div> : null}<div className="callout callout-info" role="note"><span className="callout-body"><strong>Read-only safety state</strong>No kill switch, resume, flatten, or other trading-state action is available from this console.</span></div><article className="panel"><dl><dt>Halt</dt><dd><span className={stateClass(e.halt)}>{e.halt}</span></dd><dt>Scope</dt><dd><Unknown value={e.scope} /></dd><dt>Reason</dt><dd><Unknown value={e.reason} /></dd><dt>Issued</dt><dd><Unknown value={e.issuedAt} /></dd><dt>Issuer/reference</dt><dd><Unknown value={e.issuerReference} /></dd><dt>New entries blocked?</dt><dd><Unknown value={e.newEntriesBlocked} /></dd><dt>Exposure-reducing actions allowed?</dt><dd><Unknown value={e.exposureReducingActionsAllowed} /></dd><dt>Reconciliation</dt><dd><span className={stateClass(e.reconciliation)}>{e.reconciliation}</span></dd><dt>Authorization</dt><dd><span className={stateClass(e.authorizationMode)}>{e.authorizationMode}</span></dd></dl></article></Shell>; }

export function HealthScreen({ operator, role, snapshot }: { operator: string; role: string; snapshot: OperationsSnapshot }) { return <Shell current="health" title="System Health & Runtime" operator={operator} role={role} snapshot={snapshot}><section className="health-grid">{snapshot.health.map((h) => <article className={`panel health-card ${danger.has(h.state) ? "status-card-danger" : ""}`} key={h.component}><h2>{h.component} <span className={stateClass(h.state)}>{h.state}</span></h2><dl><dt>Last check</dt><dd><Unknown value={h.lastCheck} /></dd><dt>Last success</dt><dd><Unknown value={h.lastSuccess} /></dd><dt>Details</dt><dd><Unknown value={h.details} /></dd></dl></article>)}</section></Shell>; }

export function orderedReachedStages(stages: readonly ExecutionStage[]): readonly ExecutionStage[] { return EXECUTION_LIFECYCLE.filter((stage) => stages.includes(stage)); }
