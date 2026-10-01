// Product lists from the KDPS master sheet (SO-04 C01).
//
// One uploaded master sheet is one import package: the uploader reviews what it
// would change, adjusts it, and submits it; a different person approves the
// whole package once. These are the package's types (taken from the API's own
// declared bodies) and the pure helpers the screen builds its choices with.
import type { paths } from "./api-schema";

export const MASTER_SHEET_BASE = "/goods-v1/masters/master-sheet-imports";

export type MasterSheetImport =
  paths["/api/goods-v1/masters/master-sheet-imports/{id}"]["get"]["responses"][200]["content"]["application/json"];
export type MasterSheetPlan = MasterSheetImport["data"]["plan"];
export type MasterSheetSelections = MasterSheetImport["data"]["selections"];
export type MasterSheetDimension = MasterSheetPlan["dimensions"][number];
export type MasterSheetRule = MasterSheetPlan["item_rules"]["new"][number];
export type MasterSheetSummary =
  paths["/api/goods-v1/masters/master-sheet-imports/summary"]["get"]["responses"][200]["content"]["application/json"];

/** How the sheet's state reads to a person. */
export const STATE_LABEL: Record<MasterSheetImport["state"], string> = {
  review: "Being reviewed",
  submitted: "Waiting for approval",
  approved: "Approved",
  withdrawn: "Withdrawn",
};

export const STATE_TONE: Record<MasterSheetImport["state"], string> = {
  review: "chip-amber",
  submitted: "chip-blue",
  approved: "chip-green",
  withdrawn: "",
};

/** The headline numbers of a plan, in the order the summary strip shows them. */
export function planTotals(plan: MasterSheetPlan) {
  const sum = (key: string) =>
    plan.dimensions.reduce((total, d) => total + (d.counts[key] ?? 0), 0);
  return {
    newValues: sum("added"),
    backInUse: sum("back_in_use"),
    toRetire: sum("retire"),
    notInSheet: sum("not_in_sheet"),
    brands: plan.brands.to_create.filter((b) => b.include).length,
    seasons: plan.seasons.to_create.filter((s) => s.include).length,
    rules: plan.item_rules.new.length + plan.item_rules.changed.length,
    problems:
      plan.dimensions.reduce((total, d) => total + d.problems.length, 0) +
      plan.brands.problems.length +
      plan.item_rules.problems.length,
    warningsOpen: plan.warnings.filter((w) => !w.acknowledged).length,
  };
}

function toggled(list: readonly string[] | undefined, value: string, on: boolean): string[] {
  const set = new Set(list ?? []);
  if (on) set.add(value);
  else set.delete(value);
  return [...set].sort();
}

/** Tick or untick a value that is not in the sheet for retirement. */
export function withRetire(
  selections: MasterSheetSelections,
  valueId: string,
  retire: boolean,
): MasterSheetSelections {
  return { ...selections, retire: toggled(selections.retire, valueId, retire) };
}

/** Leave a new value out of the import (a typo), or put it back. */
export function withLeftOut(
  selections: MasterSheetSelections,
  dimension: string,
  text: string,
  leaveOut: boolean,
): MasterSheetSelections {
  const skip = { ...(selections.skip_values ?? {}) };
  const next = toggled(skip[dimension], text, leaveOut);
  if (next.length) skip[dimension] = next;
  else delete skip[dimension];
  return { ...selections, skip_values: skip };
}

/** Include or skip a brand or season the sheet would create. */
export function withSkipped(
  selections: MasterSheetSelections,
  key: "skip_brands" | "skip_seasons",
  name: string,
  skip: boolean,
): MasterSheetSelections {
  return { ...selections, [key]: toggled(selections[key], name, skip) };
}

/** Pick one of an ITEM's suggested values where the sheet names several. */
export function withRuleChoice(
  selections: MasterSheetSelections,
  item: string,
  dimension: "sub_category" | "type",
  value: string,
): MasterSheetSelections {
  const key = item.normalize("NFC").split(/\s+/).filter(Boolean).join(" ").toLowerCase();
  const choices = { ...(selections.rule_choices ?? {}) };
  choices[key] = { ...(choices[key] ?? {}), [dimension]: value };
  return { ...selections, rule_choices: choices };
}

export function withAcknowledged(
  selections: MasterSheetSelections,
  warningId: string,
  acknowledged: boolean,
): MasterSheetSelections {
  return {
    ...selections,
    acknowledged: toggled(selections.acknowledged, warningId, acknowledged),
  };
}

/** What still stops the uploader from submitting, in plain words (empty: ready). */
export function submitBlockers(item: MasterSheetImport): string[] {
  const out: string[] = [];
  if (item.data.stale)
    out.push("The lists changed since you uploaded this sheet. Refresh it first.");
  if (item.data.plan.change_count === 0) out.push("The lists already match this sheet.");
  const open = item.data.plan.warnings.filter((w) => !w.acknowledged).length;
  if (open) out.push(`Tick ${open === 1 ? "the warning" : `all ${open} warnings`} below.`);
  return out;
}
