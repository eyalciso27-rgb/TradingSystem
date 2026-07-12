/**
 * Positions / orders / reconciliation model + fixtures (IMP-1105).
 *
 * Scope (task): stops, targets, fills, order states and mismatches.
 * DoD: the UI mirrors CANONICAL state, never broker raw status.
 *
 * `CANONICAL_ORDER_STATES` is the Decision_Contract §12.5 state machine;
 * a test cross-checks it verbatim against the generated contract sources
 * (packages/schemas/json/enums/order_state.schema.json and the generated
 * TS enum), so this list can never drift from the canonical enum. Raw
 * broker strings (e.g. the IBKR wire statuses) are mapped by the adapter
 * layer (IMP-1002) long before the UI — anything unmapped is UNKNOWN, and
 * UNKNOWN is never rendered as a positive state: it demands
 * reconciliation. Read-only fixtures: no broker, no network, no secrets;
 * account aliases masked.
 */

export const CANONICAL_ORDER_STATES = [
  "CREATED",
  "VALIDATED",
  "QUEUED",
  "SUBMITTING",
  "ACKNOWLEDGED",
  "PARTIALLY_FILLED",
  "FILLED",
  "CANCEL_PENDING",
  "CANCELLED",
  "CANCELLED_BY_POLICY",
  "REJECTED",
  "EXPIRED",
  "UNKNOWN",
] as const;

export type CanonicalOrderState = (typeof CANONICAL_ORDER_STATES)[number];

export function isCanonicalOrderState(value: string): value is CanonicalOrderState {
  return (CANONICAL_ORDER_STATES as readonly string[]).includes(value);
}

/** States that require operator attention before anything else. */
export function needsReconcile(state: CanonicalOrderState): boolean {
  return state === "UNKNOWN";
}

export interface PositionRow {
  readonly positionRef: string;
  readonly symbol: string;
  readonly direction: "LONG" | "SHORT";
  readonly quantity: string;
  readonly averagePrice: string;
  readonly stopPrice: string | null;
  readonly targetPrice: string | null;
  readonly protectedQuantity: string;
  readonly accountAliasMasked: string;
}

export interface OrderRow {
  readonly clientOrderId: string;
  readonly symbol: string;
  readonly role: string; // ENTRY / PROTECTIVE_STOP / TAKE_PROFIT ...
  readonly state: CanonicalOrderState;
  readonly quantity: string;
  readonly filledQuantity: string;
  readonly remainingQuantity: string;
}

export interface FillRow {
  readonly fillId: string;
  readonly clientOrderId: string;
  readonly symbol: string;
  readonly quantity: string;
  readonly price: string;
  readonly filledAt: string;
}

export interface MismatchRow {
  readonly kind: string; // IMP-1006 FindingKind vocabulary
  readonly subjectRef: string;
  readonly expected: string;
  readonly observed: string;
  readonly recommendation: "NEEDS_REVIEW" | "RESYNC_STATE" | "BLOCK_NEW_ENTRIES";
}

// -- deterministic fixtures ----------------------------------------------------

export const POSITION_ROWS: readonly PositionRow[] = [
  {
    positionRef: "pos-msft-1",
    symbol: "MSFT",
    direction: "LONG",
    quantity: "10",
    averagePrice: "400.00",
    stopPrice: "392.00",
    targetPrice: "424.00",
    protectedQuantity: "10",
    accountAliasMasked: "DU*****67",
  },
  {
    positionRef: "pos-aapl-2",
    symbol: "AAPL",
    direction: "LONG",
    quantity: "40",
    averagePrice: "232.40",
    stopPrice: "228.00",
    targetPrice: null,
    protectedQuantity: "40",
    accountAliasMasked: "DU*****67",
  },
];

export const ORDER_ROWS: readonly OrderRow[] = [
  {
    clientOrderId: "ord-entry-1",
    symbol: "AAPL",
    role: "ENTRY",
    state: "PARTIALLY_FILLED",
    quantity: "60",
    filledQuantity: "40",
    remainingQuantity: "20",
  },
  {
    clientOrderId: "ord-stop-1",
    symbol: "AAPL",
    role: "PROTECTIVE_STOP",
    state: "ACKNOWLEDGED",
    quantity: "40",
    filledQuantity: "0",
    remainingQuantity: "40",
  },
  {
    clientOrderId: "ord-tp-1",
    symbol: "MSFT",
    role: "TAKE_PROFIT",
    state: "CANCEL_PENDING",
    quantity: "10",
    filledQuantity: "0",
    remainingQuantity: "10",
  },
  {
    // Mapped from an unrecognized broker status by the adapter layer:
    // the UI only ever sees canonical UNKNOWN and treats it as blocking.
    clientOrderId: "ord-ghost-9",
    symbol: "TSLA",
    role: "ENTRY",
    state: "UNKNOWN",
    quantity: "5",
    filledQuantity: "0",
    remainingQuantity: "5",
  },
];

export const FILL_ROWS: readonly FillRow[] = [
  {
    fillId: "fill-1",
    clientOrderId: "ord-entry-1",
    symbol: "AAPL",
    quantity: "25",
    price: "232.35",
    filledAt: "2026-07-10T16:52:00.000Z",
  },
  {
    fillId: "fill-2",
    clientOrderId: "ord-entry-1",
    symbol: "AAPL",
    quantity: "15",
    price: "232.48",
    filledAt: "2026-07-10T16:53:30.000Z",
  },
];

export const MISMATCH_ROWS: readonly MismatchRow[] = [
  {
    kind: "POSITION_MISMATCH",
    subjectRef: "position:TSLA",
    expected: "0",
    observed: "5",
    recommendation: "BLOCK_NEW_ENTRIES",
  },
  {
    kind: "UNRESOLVED_UNKNOWN_ORDER",
    subjectRef: "order:ord-ghost-9",
    expected: "resolved state",
    observed: "UNKNOWN pending reconciliation",
    recommendation: "NEEDS_REVIEW",
  },
];
