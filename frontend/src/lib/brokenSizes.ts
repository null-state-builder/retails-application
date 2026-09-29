// What the Broken Sizes page says (store operations ticket 32, ST-INV-1). The
// server decides which style-colours are broken, when an alert opens and closes,
// and where it stands in the 7-day measure (`stockledger.broken_size`); these
// only put that into words, so the page and its tests say the same thing.

export interface MeasureT {
  counted: number;
  on_time: number;
  waiting: number;
  closed_first: number;
  percent: number | null;
}

export function categoryText(category: string): string {
  return category || "No category";
}

export function heldText(held: { size: string; qty: number }[]): string {
  return held.length ? held.map((h) => `${h.size} ${h.qty}`).join(", ") : "None";
}

export function actionText(action: string, note: string): string {
  switch (action) {
    case "transfer":
      return "Asked for a transfer";
    case "markdown":
      return "Asked for a markdown";
    default:
      return note || "Something else";
  }
}

export function closedText(reason: string): string {
  switch (reason) {
    case "fixed":
      return "Enough core sizes are back";
    case "rule_changed":
      return "Its category's core sizes changed";
    case "not_checked":
      return "The store is no longer checked";
    case "sold_out":
      return "No stock of it here now";
    case "no_rule":
      return "Its category has no rule now";
    case "switched_off":
      return "Switched off at this store";
    default:
      return "Closed";
  }
}

export function outcomeText(outcome: string): string {
  switch (outcome) {
    case "on_time":
      return "Acted on within 7 days";
    case "missed":
      return "Not acted on within 7 days";
    case "waiting":
      return "Inside its 7 days";
    default:
      return "Closed before anybody acted";
  }
}

/** The success measure: broken-size alerts acted on within 7 days (target 80% or more). */
export function measureText(m: MeasureT): string {
  const parts = [
    m.counted && m.percent !== null
      ? `Acted on within 7 days: ${m.on_time} of ${m.counted} (${m.percent}%).`
      : "Acted on within 7 days: none to count yet.",
  ];
  if (m.waiting)
    parts.push(`${m.waiting} still inside ${m.waiting === 1 ? "its" : "their"} 7 days.`);
  if (m.closed_first) parts.push(`${m.closed_first} closed before anybody acted.`);
  return parts.join(" ");
}

/** "S, M, L" as the list of sizes it names; blanks are dropped. */
export function parseSizes(text: string): string[] {
  return text
    .split(",")
    .map((size) => size.split(/\s+/).filter(Boolean).join(" "))
    .filter(Boolean);
}

/** Where a broken-size alert opens: its store's page. */
export function alertBrokenSizesPath(storeId: number): string {
  return `/stock/broken-sizes?site_id=${storeId}`;
}
