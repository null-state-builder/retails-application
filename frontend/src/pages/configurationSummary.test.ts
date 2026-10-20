import { describe, expect, it } from "vitest";

import { TENANT_SCOPE } from "../lib/goodsConfig";
import {
  humanise,
  scopeLabel,
  summarizeConfiguration,
  weekdaysLabel,
} from "./configurationSummary";

describe("configuration line summary", () => {
  it("names the working week the way a person would", () => {
    expect(weekdaysLabel([1, 2, 3, 4, 5, 6])).toBe("Mon to Sat");
    expect(weekdaysLabel([7, 1, 2, 3, 4, 5, 6])).toBe("Every day");
    expect(weekdaysLabel([1, 3, 5])).toBe("Mon, Wed, Fri");
    expect(weekdaysLabel([])).toBe("None");
  });

  it("summarises a working calendar", () => {
    const rows = summarizeConfiguration("working_calendar", {
      timezone: "Asia/Kolkata",
      excluded_dates: ["2026-12-25"],
      working_weekdays: [1, 2, 3, 4, 5, 6],
    });
    expect(rows.map((r) => r.label)).toEqual(["Working days", "Closed dates", "Timezone"]);
    expect(rows[0]?.value).toBe("Mon to Sat");
    expect(rows[1]?.value).toContain("2026");
    expect(rows[2]?.value).toBe("Asia/Kolkata");
  });

  it("counts several closed dates from the earliest", () => {
    const [, closed] = summarizeConfiguration("working_calendar", {
      working_weekdays: [1],
      excluded_dates: ["2027-01-26", "2026-12-25", "2027-03-14"],
    });
    expect(closed?.value).toMatch(/^3 dates from .*2026$/);
  });

  it("summarises a selling policy", () => {
    expect(
      summarizeConfiguration("sell_policy", {
        manual_discount_cap_percent: "10.00",
        manual_discount_on_offer_lines: false,
        return_window_days: 1,
      }),
    ).toEqual([
      { label: "Manual discount cap", value: "10.00%" },
      { label: "Return window", value: "1 day" },
      { label: "Discount on offer lines", value: "No" },
    ]);
  });

  it("falls back to the first readable fields, at most four", () => {
    const rows = summarizeConfiguration("non_trading", {
      reason_code: "closed",
      empty: "",
      nested: { a: 1 },
      notes_given: true,
      days: [1, 2],
      a: 1,
      b: 2,
    });
    expect(rows).toEqual([
      { label: "Reason code", value: "closed" },
      { label: "Notes given", value: "Yes" },
      { label: "Days", value: "1, 2" },
      { label: "A", value: "1" },
    ]);
    expect(humanise("pt.approve.opening")).toBe("Pt approve opening");
  });

  it("says who a line covers", () => {
    expect(scopeLabel(TENANT_SCOPE)).toBe("Whole business");
    expect(scopeLabel({ ...TENANT_SCOPE, scope_kind: "sites", site_ids: ["a", "b"] })).toBe(
      "2 stores",
    );
    expect(scopeLabel({ ...TENANT_SCOPE, scope_kind: "brands", brand_ids: ["a"] })).toBe("1 brand");
  });
});
