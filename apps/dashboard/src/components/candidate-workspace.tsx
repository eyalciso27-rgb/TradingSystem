"use client";

/**
 * Candidate workspace (IMP-1103): a read-only review surface where every
 * decision is explainable from the UI (DoD). The left list selects a
 * candidate; the detail panel shows the full chain — evidence, per-model
 * cards, fusion, vetoes, the proposed plan and freshness. Approve/submit/
 * auto-trade are disabled: any real action must traverse Decision → Risk →
 * Allocation → Pre-submit → Authorization → Execution Agent.
 */

import { useState } from "react";

import {
  CANDIDATE_ACTIONS,
  CANDIDATE_IDEAS,
  type CandidateIdea,
  candidateById,
  isStatusPositive,
} from "../lib/candidates";

function statusClass(candidate: CandidateIdea): string {
  return isStatusPositive(candidate.status) ? "runtime-ok" : "runtime-bad";
}

function CandidateDetail({ candidate }: { candidate: CandidateIdea }) {
  return (
    <div className="candidate-detail">
      <h2>
        {candidate.symbol} · {candidate.direction}{" "}
        <span className={statusClass(candidate)}>{candidate.status}</span>
      </h2>
      <p className="muted">
        {candidate.instrument} · provenance {candidate.provenance} · confidence{" "}
        {candidate.confidence} · updated {candidate.updatedAt}
      </p>
      <p>{candidate.strategySummary}</p>
      {candidate.riskFlags.length > 0 ? (
        <p className="runtime-bad">Risk flags: {candidate.riskFlags.join(", ")}</p>
      ) : null}
      {candidate.blockedReasons.length > 0 ? (
        <p className="runtime-bad">Blocked / missing: {candidate.blockedReasons.join(", ")}</p>
      ) : null}

      <h3>Evidence</h3>
      <ul>
        {candidate.evidence.map((item) => (
          <li key={item.ref}>
            <code>{item.ref}</code> [{item.sourceKind}] {item.summary} · obs {item.observedAt} ·
            avail {item.availableAt}
          </li>
        ))}
      </ul>

      <h3>Model cards</h3>
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Family</th>
            <th>Score</th>
            <th>Calibration</th>
            <th>Status</th>
            <th>Verified</th>
            <th>Assessment</th>
          </tr>
        </thead>
        <tbody>
          {candidate.modelCards.map((card) => (
            <tr key={card.model}>
              <td>{card.model}</td>
              <td>{card.family}</td>
              <td>{card.score}</td>
              <td>{card.calibration}</td>
              <td className={card.status === "VETO" ? "runtime-bad" : ""}>{card.status}</td>
              <td>{card.verified ? "yes" : "UNVERIFIED"}</td>
              <td>{card.assessment}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Fusion</h3>
      <p>
        Fused score {candidate.fusion.fusedScore} · action tier{" "}
        <strong>{candidate.fusion.actionTier}</strong> · weights{" "}
        {candidate.fusion.familyWeights.map((w) => `${w.family}:${w.weight}`).join(", ")}
      </p>
      <ul>
        {candidate.fusion.contributions.map((c) => (
          <li key={c.model}>
            {c.model}: {c.weightedScore}
            {c.deduped ? " (deduped)" : ""}
          </li>
        ))}
      </ul>

      <h3>Vetoes</h3>
      {candidate.vetoes.length === 0 ? (
        <p className="muted">none</p>
      ) : (
        <ul>
          {candidate.vetoes.map((veto) => (
            <li key={veto.reasonCode} className="runtime-bad">
              {veto.source}: {veto.reasonCode}
              {veto.blocking ? " (blocking)" : ""}
            </li>
          ))}
        </ul>
      )}

      <h3>Proposed plan</h3>
      <dl className="plan">
        <dt>Entry</dt>
        <dd>
          {candidate.plan.entryType} · qty {candidate.plan.proposedQuantity} @{" "}
          {candidate.plan.entryPrice}
        </dd>
        <dt>Stop / target</dt>
        <dd>
          {candidate.plan.stopPrice} / {candidate.plan.targetPrice ?? "—"}
        </dd>
        <dt>Risk</dt>
        <dd>{candidate.plan.riskAmount}</dd>
        <dt>Allocation</dt>
        <dd>{candidate.plan.allocationStatus}</dd>
        <dt>Pre-submit</dt>
        <dd>{candidate.plan.preSubmitAction}</dd>
      </dl>

      <h3>Freshness</h3>
      <p className={candidate.freshness.stale ? "runtime-bad" : "runtime-ok"}>
        as of {candidate.freshness.asOf} · market-data age{" "}
        {candidate.freshness.marketDataAgeSeconds}s ·{" "}
        {candidate.freshness.stale ? "STALE" : "fresh"} · lookahead guard{" "}
        {candidate.freshness.lookaheadGuard ? "on" : "OFF"}
      </p>

      <h3>Actions (disabled by policy)</h3>
      <ul className="actions">
        {CANDIDATE_ACTIONS.map((entry) => (
          <li key={entry.action}>
            <button type="button" disabled title={entry.reason}>
              {entry.action}
            </button>
            <span className="action-reason">{entry.reason}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function CandidateWorkspace({ operator, role }: { operator: string; role: string }) {
  const first = CANDIDATE_IDEAS[0];
  const [selectedId, setSelectedId] = useState<string>(first ? first.candidateId : "");
  const selected = candidateById(selectedId);

  return (
    <main className="console">
      <header className="console-header">
        <h1>Candidate Workspace</h1>
        <a className="nav-link" href="/">
          ← Dashboard
        </a>
        <span className="operator">
          {operator} ({role}, read-only)
        </span>
      </header>
      <p className="env-tag">
        Review only. Every candidate is explainable end to end. Nothing here
        trades — approve/submit require the full pipeline.
      </p>

      <section className="candidate-layout">
        <ul className="candidate-list">
          {CANDIDATE_IDEAS.map((candidate) => (
            <li key={candidate.candidateId}>
              <button
                type="button"
                className={candidate.candidateId === selectedId ? "selected" : ""}
                onClick={() => setSelectedId(candidate.candidateId)}
              >
                <strong>{candidate.symbol}</strong> {candidate.direction} ·{" "}
                <span className={statusClass(candidate)}>{candidate.status}</span>
                <br />
                <span className="muted">conf {candidate.confidence}</span>
              </button>
            </li>
          ))}
        </ul>
        <article className="panel">
          {selected ? (
            <CandidateDetail candidate={selected} />
          ) : (
            <p className="muted">no candidate selected</p>
          )}
        </article>
      </section>
    </main>
  );
}
