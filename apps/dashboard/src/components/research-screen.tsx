/**
 * Research / calibration screen (IMP-1107). Read-only fixtures: funnel KPI
 * tiles, starvation callouts with human explanations, champion-vs-challenger
 * table, backtest runs and model cards. DoD: no UI action auto-promotes a
 * policy/model — the only promotion surface is a permanently disabled
 * control registry with the governance reason. No client interactivity is
 * needed at all, so this is a server component with zero action handlers.
 */

import { AppNav } from "./app-nav";
import { chipClass } from "../lib/explain";
import {
  BACKTEST_RUNS,
  CHALLENGER_ROWS,
  FUNNEL_STAGES,
  MODEL_CARDS,
  PROMOTION_ACTIONS,
  RESEARCH_AS_OF,
  STARVATION_INDICATORS,
  type ModelStatus,
} from "../lib/research";

function modelStatusChip(status: ModelStatus): string {
  if (status === "ACTIVE") {
    return "chip chip-ok";
  }
  if (status === "SHADOW") {
    return "chip chip-info";
  }
  return "chip chip-muted";
}

export function ResearchScreen({ operator, role }: { operator: string; role: string }) {
  const starving = STARVATION_INDICATORS.filter((indicator) => indicator.starving);

  return (
    <>
      <AppNav current="research" operator={operator} role={role} />
      <main className="console">
        <header className="console-header">
          <h1>Research &amp; Calibration</h1>
          <span className="muted">as of {RESEARCH_AS_OF}</span>
        </header>
        <p className="page-intro">
          Read-only research view. Champions stay champions here: promoting a
          model or policy is an owner decision through the registry governance
          flow — nothing on this screen can do it.
        </p>

        {starving.length > 0 ? (
          <div className="callout callout-warn" role="alert">
            <span className="callout-body">
              <strong>
                {starving.length} feed{starving.length > 1 ? "s are" : " is"} starving
              </strong>
              {starving.map((indicator) => indicator.explanation).join(" ")}
            </span>
          </div>
        ) : (
          <div className="callout callout-info" role="status">
            <span className="callout-body">
              <strong>All feeds flowing</strong>
              <small>No source or pipeline stage is starving.</small>
            </span>
          </div>
        )}

        <div className="kpi-grid">
          {FUNNEL_STAGES.map((stage) => (
            <div className="kpi" key={stage.stage}>
              <p className="kpi-label">{stage.label}</p>
              <p className="kpi-value">{stage.count}</p>
              <p className="kpi-sub">{stage.windowLabel}</p>
            </div>
          ))}
        </div>

        <section className="grid">
          <article className="panel panel-wide">
            <h2>
              Feed &amp; stage freshness <span className="muted">starvation watch</span>
            </h2>
            <ul className="actions">
              {STARVATION_INDICATORS.map((indicator) => (
                <li key={indicator.subject}>
                  <span className={indicator.starving ? "chip chip-warn" : "chip chip-ok"}>
                    {indicator.starving ? "starving" : "flowing"}
                  </span>
                  <span>
                    <strong>{indicator.subject}</strong> ({indicator.kind.toLowerCase()}) ·{" "}
                    {indicator.minutesSinceLastItem}m since last item (threshold{" "}
                    {indicator.thresholdMinutes}m).{" "}
                    <span className="muted">{indicator.explanation}</span>
                  </span>
                </li>
              ))}
            </ul>
          </article>

          <article className="panel panel-wide">
            <h2>
              Champion vs challenger <span className="muted">calibration</span>
            </h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Family</th>
                    <th>Champion</th>
                    <th>Calib.</th>
                    <th>Challenger</th>
                    <th>Calib.</th>
                    <th>Δ</th>
                    <th>Status</th>
                    <th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {CHALLENGER_ROWS.map((row) => (
                    <tr key={row.family}>
                      <td>{row.family}</td>
                      <td>
                        {row.championModel} <span className="muted">{row.championVersion}</span>
                      </td>
                      <td>{row.championCalibration}</td>
                      <td>
                        {row.challengerModel}{" "}
                        <span className="muted">{row.challengerVersion}</span>
                      </td>
                      <td>{row.challengerCalibration}</td>
                      <td>
                        <span
                          className={
                            row.calibrationDelta.startsWith("+")
                              ? "chip chip-ok"
                              : "chip chip-warn"
                          }
                        >
                          {row.calibrationDelta}
                        </span>
                      </td>
                      <td>
                        <span className={modelStatusChip(row.challengerStatus)}>
                          {row.challengerStatus}
                        </span>
                      </td>
                      <td className="muted">{row.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>

          <article className="panel panel-wide">
            <h2>
              Backtest runs <span className="muted">deterministic replays</span>
            </h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Strategy</th>
                    <th>Policy</th>
                    <th>Period</th>
                    <th>Trades</th>
                    <th>Hit rate</th>
                    <th>PF</th>
                    <th>Max DD%</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {BACKTEST_RUNS.map((run) => (
                    <tr key={run.runId}>
                      <td>
                        {run.runId} <span className="muted">{run.deterministicSeed}</span>
                      </td>
                      <td>{run.strategy}</td>
                      <td>{run.policyVersion}</td>
                      <td>
                        {run.periodStart} → {run.periodEnd}
                      </td>
                      <td>{run.trades}</td>
                      <td>{run.hitRate}</td>
                      <td>{run.profitFactor}</td>
                      <td>{run.maxDrawdownPct}</td>
                      <td>
                        <span
                          className={
                            run.status === "COMPLETED"
                              ? chipClass("OK")
                              : run.status === "RUNNING"
                                ? chipClass("PENDING")
                                : "chip chip-danger"
                          }
                        >
                          {run.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>

          <article className="panel panel-wide">
            <h2>Model cards</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Family</th>
                    <th>Version</th>
                    <th>Status</th>
                    <th>Calibration</th>
                    <th>Last evaluated</th>
                    <th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {MODEL_CARDS.map((card) => (
                    <tr key={card.model}>
                      <td>{card.model}</td>
                      <td>{card.family}</td>
                      <td>{card.version}</td>
                      <td>
                        <span className={modelStatusChip(card.status)}>{card.status}</span>
                      </td>
                      <td>{card.calibration}</td>
                      <td className="muted">{card.lastEvaluatedAt}</td>
                      <td className="muted">{card.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>

          <article className="panel panel-wide">
            <h2>Promotion (governance only)</h2>
            <p className="page-intro">
              Nothing on this screen can change a model or policy status. These
              controls exist only to make that boundary visible.
            </p>
            <ul className="actions">
              {PROMOTION_ACTIONS.map((entry) => (
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
