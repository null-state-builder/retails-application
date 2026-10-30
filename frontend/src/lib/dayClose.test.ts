import { describe, expect, it } from "vitest";

import { canClose, dayWords, deriveSteps, openHolds, whyNotYet } from "./dayClose";
import type { CounterView, DayCloseState } from "./dayClose";

const bills = { bills: 0, net_paise: 0, tenders: {}, frontier: 0 };

function server(overrides: Partial<DayCloseState> = {}): DayCloseState {
  const steps = ["synced", "held", "cash", "summary"].map((key) => ({
    key,
    done: true,
    code: "",
    message: "",
  }));
  return {
    store: "WRR",
    business_day: "2026-10-29",
    next_business_day: "2026-10-30",
    closed: false,
    can_close: true,
    steps: [...steps, { key: "close", done: false, code: "", message: "" }],
    counter_id: "T1",
    hole_count: 0,
    last_doc_number: "WRR/26-27/147",
    held_open: 0,
    held_kept: 0,
    switched_on: true,
    count: {
      id: "c1",
      counted_at: "2026-10-29T16:01:00Z",
      counted_paise: 1459100,
      expected_paise: 1459100,
      variance_paise: 0,
      counted_by_name: "Cashier 1",
      approved_by_name: "",
    },
    bills_after_count: 0,
    summary: { ...bills, bills: 64 },
    summary_changed: false,
    carried_in: bills,
    after_close: bills,
    record: {
      id: "r1",
      summary_checked_at: "2026-10-29T16:14:00Z",
      summary_checked_by_name: "Cashier 1",
      closed_at: null,
      closed_by_name: "",
      confirmed_by_name: "",
      facts: {},
    },
    last_closed: "2026-10-28",
    still_open: {
      reservations_holding: 0,
      reservations_past_date: 0,
      alterations_ready: 0,
      special_orders_arrived: 0,
    },
    ...overrides,
  };
}

const counter: CounterView = {
  ready: true,
  counterId: "T1",
  pending: 0,
  halt: null,
  onlinePending: false,
  heldOpen: 0,
};

function withStep(state: DayCloseState, key: string, message: string): DayCloseState {
  return {
    ...state,
    can_close: false,
    steps: state.steps.map((step) =>
      step.key === key ? { ...step, done: false, code: "X", message } : step,
    ),
  };
}

describe("day close steps", () => {
  it("offers Close the day only when every step above it is done", () => {
    const state = server();
    const steps = deriveSteps(state, counter);
    expect(steps.map((step) => step.status)).toEqual(["done", "done", "done", "done", "now"]);
    expect(canClose(steps, state, true)).toBe(true);
    expect(canClose(steps, state, false)).toBe(false);
    expect(steps[2]?.text).toBe(
      "₹14,591 counted at 21:31 by Cashier 1. Matches the expected cash.",
    );
    expect(steps[3]?.text).toContain("Ticked by Cashier 1 at 21:44");
  });

  it("names the counter and its waiting bills when the queue is not empty", () => {
    const state = server();
    const steps = deriveSteps(state, { ...counter, counterId: "T2", pending: 2 });
    expect(steps[0]).toMatchObject({
      status: "now",
      text: "Counter T2 has 2 bills waiting. Sync T2, then come back.",
    });
    expect(canClose(steps, state, true)).toBe(false);
    expect(whyNotYet(steps, state, true)).toBe(
      "Counter T2 has 2 bills waiting. Sync T2, then come back.",
    );
  });

  it("keeps the first open step as the one to do now, and the rest waiting", () => {
    const state = withStep(
      server({ count: null }),
      "cash",
      "The drawer is not counted yet. Count it on Cash count, then come back.",
    );
    const steps = deriveSteps(state, { ...counter, heldOpen: 1 });
    expect(steps.map((step) => step.status)).toEqual(["done", "now", "open", "done", "open"]);
    expect(steps[1]?.text).toBe(
      "1 bill is on hold. Resume or let go of each on Billing, then come back.",
    );
    expect(steps[2]?.text).toContain("not counted yet");
  });

  it("takes the server's word for what only head office can see", () => {
    const state = withStep(
      server(),
      "summary",
      "Bills changed since the Day summary was checked. Read it again and tick it again.",
    );
    const steps = deriveSteps(state, counter);
    expect(steps[3]).toMatchObject({
      status: "now",
      text: expect.stringContaining("Read it again"),
    });
    expect(canClose(steps, state, true)).toBe(false);
  });

  it("waits for the counter's own queue before calling the bills synced", () => {
    const steps = deriveSteps(server(), { ...counter, ready: false });
    expect(steps[0]?.status).toBe("now");
  });

  it("reads a closed day as five done steps, and offers nothing", () => {
    const state = server({
      closed: true,
      record: {
        ...server().record!,
        closed_at: "2026-10-29T16:22:00Z",
        closed_by_name: "Cashier 1",
        confirmed_by_name: "Debanjan",
      },
    });
    const steps = deriveSteps(state, { ...counter, pending: 3 });
    expect(steps.every((step) => step.status === "done")).toBe(true);
    expect(steps[4]?.text).toContain("Counters now bill into Fri 30 Oct 2026");
    expect(canClose(steps, state, true)).toBe(false);
  });

  it("counts only holds the store has not kept", () => {
    expect(openHolds([{ expires_policy: "today" }, { expires_policy: "kept" }])).toBe(1);
    expect(dayWords("2026-10-29")).toBe("Thu 29 Oct 2026");
  });
});
