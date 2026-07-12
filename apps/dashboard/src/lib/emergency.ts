/**
 * Emergency-controls model + drill logic (IMP-1106).
 *
 * Scope (task): global halt and a SEPARATE flatten preview/reauth flow.
 * DoD: the halt drill passes; flatten remains DISABLED in the initial
 * release unless explicitly enabled — and it is not enabled:
 * `FLATTEN_ENABLED = false as const`, and `executeFlatten` ALWAYS returns
 * `REFUSED_DISABLED`, even with a re-auth token.
 *
 * Everything here is a DRILL over local fixtures: activating the halt only
 * flips UI state and records a fixtureOnly audit entry. It never calls the
 * real emergency service in the backend, never touches a broker and never
 * sends anything to be cancelled or executed. Protective orders are always kept
 * (keep_protection=true — Security §24.1). Resume follows System
 * Functional §42.2: it is fail-closed until BOTH the health gate and the
 * reconciliation gate are PASS, and there is no automatic resume.
 */

export type GateResult = "PASS" | "FAIL";

export interface EmergencyAuditRecord {
  readonly action: string;
  readonly operator: string;
  readonly at: string;
  readonly detail: string;
  /** Always true: recording is the ONLY effect of any drill action. */
  readonly fixtureOnly: true;
}

export interface EmergencyState {
  readonly haltActive: boolean;
  readonly scope: "GLOBAL_ENTRY_HALT" | null;
  readonly reason: string;
  readonly keepProtection: true;
  readonly auditTrail: readonly EmergencyAuditRecord[];
}

export const INITIAL_EMERGENCY_STATE: EmergencyState = {
  haltActive: false,
  scope: null,
  reason: "",
  keepProtection: true,
  auditTrail: [],
};

function record(
  action: string,
  operator: string,
  at: string,
  detail: string,
): EmergencyAuditRecord {
  return { action, operator, at, detail, fixtureOnly: true };
}

/** Halt drill: flips local fixture state + audit. Nothing else happens. */
export function activateHaltDrill(
  state: EmergencyState,
  operator: string,
  nowIso: string,
  reason: string,
): EmergencyState {
  if (state.haltActive) {
    return {
      ...state,
      auditTrail: [
        ...state.auditTrail,
        record("HALT_DRILL_NOOP", operator, nowIso, "halt already active; nothing changed"),
      ],
    };
  }
  return {
    haltActive: true,
    scope: "GLOBAL_ENTRY_HALT",
    reason: reason.trim() || "OPERATOR_DRILL",
    keepProtection: true, // protective orders are never cancelled by a halt
    auditTrail: [
      ...state.auditTrail,
      record(
        "HALT_DRILL_ACTIVATED",
        operator,
        nowIso,
        "GLOBAL_ENTRY_HALT drill (fixture only): new entries blocked in the drill;" +
          " protective orders kept; no broker call, no real service",
      ),
    ],
  };
}

export type ResumeResult =
  | { readonly ok: true; readonly state: EmergencyState }
  | { readonly ok: false; readonly reason: string };

/**
 * Resume drill (§42.2): fail-closed unless health AND reconciliation are
 * both PASS. There is no automatic resume — this is only callable by an
 * explicit operator action, and even then only when both gates pass.
 */
export function resumeDrill(
  state: EmergencyState,
  operator: string,
  nowIso: string,
  gates: { readonly health: GateResult; readonly reconciliation: GateResult },
): ResumeResult {
  if (!state.haltActive) {
    return { ok: false, reason: "no halt is active" };
  }
  if (gates.health !== "PASS" || gates.reconciliation !== "PASS") {
    const failing = [
      gates.health !== "PASS" ? "health" : null,
      gates.reconciliation !== "PASS" ? "reconciliation" : null,
    ]
      .filter((gate) => gate !== null)
      .join(" + ");
    return {
      ok: false,
      reason: `RESUME_BLOCKED: ${failing} gate(s) not PASS — resume stays fail-closed`,
    };
  }
  return {
    ok: true,
    state: {
      haltActive: false,
      scope: null,
      reason: "",
      keepProtection: true,
      auditTrail: [
        ...state.auditTrail,
        record(
          "RESUME_DRILL",
          operator,
          nowIso,
          "resume drill accepted: health PASS + reconciliation PASS (fixture gates)",
        ),
      ],
    },
  };
}

// -- flatten: separate flow, HARD-disabled ----------------------------------

/** Initial release: flatten is hard-disabled. Not a config value. */
export const FLATTEN_ENABLED = false as const;

export const FLATTEN_DISABLED_REASON =
  "Flatten is disabled in the initial release (FLATTEN_ENABLED=false)." +
  " Enabling it requires an explicit release decision plus the emergency" +
  " runbook with re-authentication — nothing on this screen can turn it on.";

export interface FlattenPreviewRow {
  readonly positionRef: string;
  readonly symbol: string;
  readonly quantity: string;
  readonly note: string;
}

/**
 * Preview only: returns what WOULD be closed if flatten were ever enabled.
 * Pure — takes no state, changes no state, performs no action.
 */
export function previewFlatten(): readonly FlattenPreviewRow[] {
  return [
    {
      positionRef: "pos-msft-1",
      symbol: "MSFT",
      quantity: "10",
      note: "would close via protective path; preview only",
    },
    {
      positionRef: "pos-aapl-2",
      symbol: "AAPL",
      quantity: "40",
      note: "would close via protective path; preview only",
    },
  ];
}

export interface FlattenRefusal {
  readonly executed: false;
  readonly result: "REFUSED_DISABLED";
  readonly reason: string;
}

/**
 * ALWAYS refuses. The reauth token is deliberately ignored: re-auth is a
 * necessary-but-never-sufficient condition while FLATTEN_ENABLED is false,
 * so there is no code path from this function to any action.
 */
export function executeFlatten(reauthToken?: string): FlattenRefusal {
  // The token is read and discarded on purpose: while FLATTEN_ENABLED is
  // false there is no condition under which execution proceeds.
  void reauthToken;
  return {
    executed: false,
    result: "REFUSED_DISABLED",
    reason: FLATTEN_DISABLED_REASON,
  };
}
