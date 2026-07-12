"use client";

/**
 * Emergency controls (IMP-1106): a global-halt DRILL and a separate,
 * hard-disabled flatten preview/reauth flow. Everything is a fixture drill:
 * no broker, no real emergency service, no order/cancel/flatten is ever
 * sent. UX goal: within five seconds the operator can tell whether a halt
 * is active and why, whether resume is possible, and why flatten is
 * blocked.
 */

import { useState } from "react";

import { AppNav } from "./app-nav";
import {
  FLATTEN_DISABLED_REASON,
  FLATTEN_ENABLED,
  type EmergencyState,
  type FlattenPreviewRow,
  type GateResult,
  INITIAL_EMERGENCY_STATE,
  activateHaltDrill,
  executeFlatten,
  previewFlatten,
  resumeDrill,
} from "../lib/emergency";

function nowIso(): string {
  return new Date().toISOString();
}

export function EmergencyControls({ operator, role }: { operator: string; role: string }) {
  const [state, setState] = useState<EmergencyState>(INITIAL_EMERGENCY_STATE);
  const [healthGate, setHealthGate] = useState<GateResult>("PASS");
  const [reconciliationGate, setReconciliationGate] = useState<GateResult>("FAIL");
  const [resumeMessage, setResumeMessage] = useState<string | null>(null);
  const [preview, setPreview] = useState<readonly FlattenPreviewRow[] | null>(null);
  const [flattenMessage, setFlattenMessage] = useState<string | null>(null);
  const [reauth, setReauth] = useState("");

  const resumeReady = healthGate === "PASS" && reconciliationGate === "PASS";

  function onHalt(): void {
    setResumeMessage(null);
    setState((current) => activateHaltDrill(current, operator, nowIso(), "OPERATOR_DRILL"));
  }

  function onResume(): void {
    const result = resumeDrill(state, operator, nowIso(), {
      health: healthGate,
      reconciliation: reconciliationGate,
    });
    if (result.ok) {
      setState(result.state);
      setResumeMessage("Resume drill accepted — halt cleared.");
    } else {
      setResumeMessage(result.reason);
    }
  }

  function onFlattenAttempt(): void {
    // Unreachable from the UI (the button is hard-disabled); kept only so
    // the refusal wording has a single source of truth.
    setFlattenMessage(executeFlatten(reauth).reason);
  }

  return (
    <>
      <AppNav current="emergency" operator={operator} role={role} />
      <main className="console">
        {state.haltActive ? (
          <div className="halt-banner" role="alert">
            DRILL: HALT ACTIVE — {state.scope} ({state.reason}). New entries blocked in this
            drill; protective stop orders are KEPT.
          </div>
        ) : null}

        <header className="console-header">
          <h1>Emergency Controls</h1>
        </header>
        <div className="callout callout-info" role="note">
          <span className="callout-body">
            <strong>Drill mode</strong>
            <small>
              Everything on this screen changes fixtures on this page only. No
              broker is contacted and no order is ever cancelled or sent.
            </small>
          </span>
        </div>

      <section className="grid">
        {/* -- halt card -------------------------------------------------- */}
        <article className="panel">
          <h2>
            Global halt{" "}
            <span className={state.haltActive ? "runtime-bad" : "runtime-ok"}>
              {state.haltActive ? "ACTIVE (drill)" : "inactive"}
            </span>
          </h2>
          <p className="muted">
            Blocks NEW entries only. Protective stop orders are always kept
            (keep_protection={String(state.keepProtection)}).
          </p>
          {state.haltActive ? (
            <p>
              Reason: <strong>{state.reason}</strong>
            </p>
          ) : (
            <p className="muted">No halt is active. You can run a halt drill safely.</p>
          )}
          <ul className="actions">
            <li>
              <button type="button" onClick={onHalt} disabled={state.haltActive}>
                {state.haltActive ? "HALT ALREADY ACTIVE" : "ACTIVATE HALT (DRILL)"}
              </button>
              <span className="action-reason">
                drill only — flips this page&apos;s fixture state and records an audit entry
              </span>
            </li>
          </ul>
        </article>

        {/* -- resume card ------------------------------------------------ */}
        <article className="panel">
          <h2>
            Resume{" "}
            <span className={resumeReady && state.haltActive ? "runtime-ok" : "runtime-bad"}>
              {state.haltActive ? (resumeReady ? "ready" : "BLOCKED") : "n/a (no halt)"}
            </span>
          </h2>
          <p className="muted">
            Resume is never automatic. Both gates must be PASS before the
            resume button unlocks (fail-closed).
          </p>
          <ul className="actions">
            <li>
              <label>
                <input
                  type="checkbox"
                  checked={healthGate === "PASS"}
                  onChange={(event) => setHealthGate(event.target.checked ? "PASS" : "FAIL")}
                />{" "}
                Health gate (drill fixture): <strong>{healthGate}</strong>
              </label>
            </li>
            <li>
              <label>
                <input
                  type="checkbox"
                  checked={reconciliationGate === "PASS"}
                  onChange={(event) =>
                    setReconciliationGate(event.target.checked ? "PASS" : "FAIL")
                  }
                />{" "}
                Reconciliation gate (drill fixture): <strong>{reconciliationGate}</strong>
              </label>
            </li>
            <li>
              <button
                type="button"
                disabled={!state.haltActive || !resumeReady}
                onClick={onResume}
              >
                RESUME (DRILL)
              </button>
              <span className="action-reason">
                {state.haltActive
                  ? resumeReady
                    ? "both gates PASS — resume drill allowed"
                    : "blocked: every gate must be PASS first"
                  : "nothing to resume"}
              </span>
            </li>
          </ul>
          {resumeMessage === null ? null : (
            <p className={resumeMessage.startsWith("RESUME_BLOCKED") ? "runtime-bad" : "runtime-ok"} role="alert">
              {resumeMessage}
            </p>
          )}
        </article>

        {/* -- flatten card: separate, dangerous-looking, hard-disabled ---- */}
        <article className="panel panel-wide danger-card">
          <h2>
            Flatten — close ALL positions{" "}
            <span className="runtime-bad">DISABLED (initial release)</span>
          </h2>
          <p>
            <strong>This is the most dangerous action in the system.</strong>{" "}
            {FLATTEN_DISABLED_REASON}
          </p>
          <ul className="actions">
            <li>
              <button type="button" onClick={() => setPreview(previewFlatten())}>
                PREVIEW ONLY — what would be closed
              </button>
              <span className="action-reason">safe: shows a list, changes nothing</span>
            </li>
            {preview === null ? null : (
              <li>
                <table>
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Qty</th>
                      <th>Note</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.map((row) => (
                      <tr key={row.positionRef}>
                        <td>{row.symbol}</td>
                        <td>{row.quantity}</td>
                        <td className="muted">{row.note}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </li>
            )}
            <li>
              <input
                aria-label="re-authentication token (not sufficient)"
                placeholder="re-auth token (required but NOT sufficient)"
                value={reauth}
                onChange={(event) => setReauth(event.target.value)}
              />
              <button
                type="button"
                disabled={!FLATTEN_ENABLED}
                title={FLATTEN_DISABLED_REASON}
                onClick={onFlattenAttempt}
              >
                EXECUTE FLATTEN
              </button>
              <span className="action-reason">
                hard-disabled: FLATTEN_ENABLED=false — a re-auth token does not
                unlock it
              </span>
            </li>
          </ul>
          {flattenMessage === null ? null : (
            <p className="runtime-bad" role="alert">
              {flattenMessage}
            </p>
          )}
        </article>

        {/* -- audit ------------------------------------------------------- */}
        <article className="panel panel-wide">
          <h2>Drill audit trail (fixture records only)</h2>
          {state.auditTrail.length === 0 ? (
            <p className="muted">no drill actions recorded yet</p>
          ) : (
            <ul>
              {state.auditTrail.map((entry, index) => (
                <li key={`${entry.action}-${index}`}>
                  [{entry.at}] {entry.operator}: {entry.action} · {entry.detail}
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
