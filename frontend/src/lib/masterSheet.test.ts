import { describe, expect, it } from "vitest";

import { goodsApprovalView } from "./actionCatalogue";
import {
  planTotals,
  submitBlockers,
  withAcknowledged,
  withLeftOut,
  withRetire,
  withRuleChoice,
  withSkipped,
  type MasterSheetImport,
  type MasterSheetPlan,
} from "./masterSheet";

function plan(overrides: Partial<MasterSheetPlan> = {}): MasterSheetPlan {
  return {
    dimensions: [
      {
        dimension: "colour",
        column: "COLOR",
        current_version_id: null,
        changed: true,
        added: [{ text: "GREEN", row: 2 }],
        left_out: [],
        present: [],
        back_in_use: [],
        not_in_sheet: [{ value_id: "v1", label: "OLD", retire: true }],
        problems: [
          {
            column: "colour",
            row: 3,
            text: "GREE",
            code: "LOOKS_LIKE",
            message: "",
            blocking: false,
          },
        ],
        counts: { added: 1, present: 0, back_in_use: 0, not_in_sheet: 1, retire: 1, left_out: 0 },
      },
    ],
    brands: {
      to_create: [
        { name: "A", code: "a", include: true },
        { name: "B", code: "b", include: false },
      ],
      present_count: 0,
      not_in_sheet: [],
      terms_note: "",
      problems: [],
    },
    seasons: { to_create: [] },
    item_rules: { new: [], changed: [], same_count: 0, problems: [] },
    ignored_columns: [],
    warnings: [
      {
        id: "PINNED_PROFILE:size",
        code: "PINNED_PROFILE",
        dimension: "size",
        message: "",
        acknowledged: false,
      },
    ],
    change_count: 3,
    retires: true,
    ready: false,
    ...overrides,
  };
}

describe("master sheet plan", () => {
  it("totals only what will actually be written", () => {
    expect(planTotals(plan())).toMatchObject({
      newValues: 1,
      toRetire: 1,
      brands: 1,
      problems: 1,
      warningsOpen: 1,
    });
  });

  it("builds choices without dropping earlier ones", () => {
    let s = withRetire({}, "v1", true);
    s = withLeftOut(s, "colour", "GREE", true);
    s = withSkipped(s, "skip_brands", "B", true);
    s = withRuleChoice(s, "  Shirt ", "sub_category", "CASUAL WEAR");
    s = withAcknowledged(s, "PINNED_PROFILE:size", true);
    expect(s).toEqual({
      retire: ["v1"],
      skip_values: { colour: ["GREE"] },
      skip_brands: ["B"],
      rule_choices: { shirt: { sub_category: "CASUAL WEAR" } },
      acknowledged: ["PINNED_PROFILE:size"],
    });
    expect(withLeftOut(s, "colour", "GREE", false).skip_values).toEqual({});
    expect(withRetire(s, "v1", false).retire).toEqual([]);
  });

  it("says plainly what stops a submit", () => {
    const item = {
      data: { stale: true, plan: plan() },
    } as unknown as MasterSheetImport;
    expect(submitBlockers(item)).toEqual([
      "The lists changed since you uploaded this sheet. Refresh it first.",
      "Tick the warning below.",
    ]);
  });
});

describe("approval link", () => {
  it("opens a master sheet upload on Product lists", () => {
    expect(
      goodsApprovalView({
        requested_action: "config.approve",
        subject_kind: "master_sheet_import",
        subject_id: "abc",
        parent_document: null,
      }),
    ).toEqual({
      topic: "setup",
      label: "Product lists update",
      to: "/setup/products?import=abc",
      number: null,
    });
  });
});
