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

import { AppNav } from "./app-nav";
import { chipClass, explain } from "../lib/explain";
import {
  CANDIDATE_ACTIONS,
  CANDIDATE_IDEAS,
  type CandidateIdea,
  candidateById,
} from "../lib/candidates";

function CandidateDetail({ candidate }: { candidate: CandidateIdea }) {
  const blockedNote =
    explain(candidate.status) ||
    (candidate.blockedReasons.length > 0 ? candidate.blockedReasons.join(", ") : "");
  const isBlocked = candidate.status !== "WATCH" && candidate.status !== "APPROVED";

  return (
    <div className="candidate-detail">
      <h2>
        {candidate.symbol} · {candidate.direction}{" "}
        <span className={chipClass(candidate.status)}>{candidate.status}</span>
      </h2>
      <p className="muted">
        {candidate.instrument} · from {candidate.provenance} · confidence {candidate.confidence} ·
        updated {candidate.updatedAt}
      </p>
      <p>{candidate.strategySummary}</p>

      {isBlocked && blockedNote ? (
        <div className="callout callout-warn" role="status">
          <span className="callout-body">
            <strong>Why this can&apos;t be traded</strong>
            {blockedNote}
          </span>
        </div>
      ) : null}
      {candidate.riskFlags.length > 0 ? (
        <p>
          <span className="chip chip-warn">risk</span> {candidate.riskFlags.join(", ")}
        </p>
      ) : null}

      <h3>Evidence</h3>
      <ul>
        {candidate.evidence.map((item) => (
          <li key={item.ref}>
            <span className="chip chip-muted">{item.sourceKind}</span> {item.summary}{" "}
            <span className="muted">
              (<code>{item.ref}</code> · obs {item.observedAt})
            </span>
          </li>
        ))}
      </ul>

      <h3>Model cards</h3>
      <div className="table-wrap">
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
                <td>
                  <span className={chipClass(card.status === "VETO" ? "VETOED" : "OK")}>
                    {card.status}
                  </span>
                </td>
                <td>
                  {card.verified ? (
                    <span className="chip chip-ok">verified</span>
                  ) : (
                    <span className="chip chip-warn">unverified</span>
                  )}
                </td>
                <td>{card.assessment}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>Fusion</h3>
      <p>
        Fused score {candidate.fusion.fusedScore} · action tier{" "}
        <span className="chip chip-info">{candidate.fusion.actionTier}</span> · weights{" "}
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
        <p className="empty">None.</p>
      ) : (
        <ul className="actions">
          {candidate.vetoes.map((veto) => (
            <li key={veto.reasonCode}>
              <span className="chip chip-danger">{veto.blocking ? "blocking" : "veto"}</span>
              <span>
                {veto.source}: {veto.reasonCode}
              </span>
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
      <p>
        <span className={chipClass(candidate.freshness.stale ? "STALE" : "OK")}>
          {candidate.freshness.stale ? "stale" : "fresh"}
        </span>{" "}
        market-data age {candidate.freshness.marketDataAgeSeconds}s · lookahead guard{" "}
        {candidate.freshness.lookaheadGuard ? "on" : "off"} ·{" "}
        <span className="muted">as of {candidate.freshness.asOf}</span>
      </p>

      <h3>Actions</h3>
      <p className="page-intro">
        Disabled by design — approving here would still require the full pipeline before anything
        could be sent.
      </p>
      <ul className="actions">
        {CANDIDATE_ACTIONS.map((entry) => (
          <li key={entry.action}>
            <button type="button" disabled title={entry.reason}>
              {entry.action}
            </button>
            <span className="action-reason">disabled — {entry.reason}</span>
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
    <>
      <AppNav current="candidates" operator={operator} role={role} />
      <main className="console">
        <header className="console-header">
          <h1>Candidate Workspace</h1>
        </header>
        <p className="page-intro">
          Review only. Every candidate is explainable end to end — nothing here trades.
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
                  <span>
                    <strong>{candidate.symbol}</strong> {candidate.direction}
                  </span>
                  <span>
                    <span className={chipClass(candidate.status)}>{candidate.status}</span>{" "}
                    <span className="muted">conf {candidate.confidence}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <article className="panel">
            {selected ? (
              <CandidateDetail candidate={selected} />
            ) : (
              <p className="empty">No candidate selected.</p>
            )}
          </article>
        </section>
      </main>
    </>
  );
}
