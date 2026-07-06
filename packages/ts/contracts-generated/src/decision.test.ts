import { describe, expect, it } from "vitest";

import {
  isNormalizedBrokerEvent,
  isOrderIntent,
  isRiskDecision,
  isTradePlan,
  OPERATING_MODE_TO_ENVIRONMENT,
  OrderIntent,
} from "./decision";

const uuid = "0197a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b";

const validIntent = (): Record<string, unknown> => ({
  intent_id: uuid,
  decision_id: uuid,
  trade_plan_id: uuid,
  risk_decision_id: uuid,
  strategy_version: "1.0",
  instrument: { instrument_id: uuid, symbol: "AAPL", currency: "USD" },
  order_role: "ENTRY",
  side: "BUY",
  order_type: "STOP",
  quantity: "100",
  stop_price: "25.00",
  time_in_force: "DAY",
  outside_rth: false,
  environment: "PAPER",
  operating_mode: "PAPER_MANUAL",
  account_ref: "account-ref-1",
  idempotency_key: "idem-1",
});

const validPlan = (): Record<string, unknown> => ({
  trade_plan_id: uuid,
  decision_id: uuid,
  instrument: { instrument_id: uuid, symbol: "AAPL", currency: "USD" },
  direction: "LONG",
  strategy_version: "1.0",
  risk_policy_version: "1.0",
  status: "APPROVED",
  environment: "PAPER",
  thesis: "Breakout",
  invalidation_summary: "Close below 24.5",
  entry: { entry_type: "BUY_STOP_TRIGGER", order_type: "STOP", stop_trigger_price: "25.00" },
  protective_stop: { stop_price: "23.00", anchor: "ENTRY_FILL" },
  profit_targets: [{ price: "28.00", quantity_fraction: "0.5" }],
  max_loss_amount: "200.00",
  currency: "USD",
  valid_from: "2026-07-05T10:00:00+00:00",
});

const validRiskApprove = (): Record<string, unknown> => ({
  risk_decision_id: uuid,
  decision_id: uuid,
  trade_plan_id: uuid,
  outcome: "APPROVE",
  approved_quantity: "100",
  risk_policy_version: "1.0",
  evaluated_at: "2026-07-05T10:00:00+00:00",
});

const validBrokerEvent = (): Record<string, unknown> => ({
  intent_id: uuid,
  order_state: "PARTIALLY_FILLED",
  external_order_ref: "ext-1",
  filled_quantity: "40",
  remaining_quantity: "60",
});

describe("decision contracts (generated)", () => {
  it("validates a well-formed order intent", () => {
    expect(isOrderIntent(validIntent())).toBe(true);
  });

  it("validates nested trade plan (instrument + targets)", () => {
    expect(isTradePlan(validPlan())).toBe(true);
    expect(isTradePlan({ ...validPlan(), profit_targets: [{ price: "28.00" }] })).toBe(false);
  });

  it("rejects missing, null and extra fields", () => {
    const missing = validIntent();
    delete missing.quantity;
    expect(isOrderIntent(missing)).toBe(false);
    expect(isOrderIntent({ ...validIntent(), quantity: null })).toBe(false);
    expect(isOrderIntent({ ...validIntent(), conId: 42 })).toBe(false);
  });

  it("rejects an inconsistent environment/operating-mode pair", () => {
    expect(
      isOrderIntent({ ...validIntent(), environment: "LIVE", operating_mode: "PAPER_MANUAL" }),
    ).toBe(false);
  });

  it("has no LIVE_AUTO in the operating-mode mapping", () => {
    expect(Object.keys(OPERATING_MODE_TO_ENVIRONMENT)).not.toContain("LIVE_AUTO");
  });

  it("rejects non-decimal money strings", () => {
    expect(isOrderIntent({ ...validIntent(), quantity: "abc" })).toBe(false);
  });

  it("enforces the risk veto/approve cross-field rule", () => {
    expect(isRiskDecision(validRiskApprove())).toBe(true);
    expect(
      isRiskDecision({
        outcome: "VETO",
        risk_decision_id: uuid,
        decision_id: uuid,
        trade_plan_id: uuid,
        risk_policy_version: "1.0",
        evaluated_at: "2026-07-05T10:00:00+00:00",
        veto_reason_codes: ["RISK_LIMIT_REACHED"],
      }),
    ).toBe(true);
    // VETO must not carry approved_quantity
    expect(
      isRiskDecision({ ...validRiskApprove(), outcome: "VETO", veto_reason_codes: ["X"] }),
    ).toBe(false);
  });

  it("validates a normalized broker event with a canonical order state", () => {
    expect(isNormalizedBrokerEvent(validBrokerEvent())).toBe(true);
    expect(isNormalizedBrokerEvent({ ...validBrokerEvent(), order_state: "WEIRD" })).toBe(false);
  });

  it("rejects an incomplete intent at type level", () => {
    // @ts-expect-error missing required order intent fields is a compile error
    const incomplete: OrderIntent = { intent_id: uuid };
    expect(isOrderIntent(incomplete)).toBe(false);
  });
});
