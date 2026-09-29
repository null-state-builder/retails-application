/** Store task checklists (store operations ticket 49, ST-OPS-4): the words and
 *  links the Task Checklists page, Today and the checklist-missed alert share.
 *  The server decides what is due, what was missed and who may tick; this only
 *  says it. */

export const CHECKLIST_PATH = "/checklist";

export const EVERY_CHOICES = [
  { value: "day", label: "Every day" },
  { value: "week", label: "Every week" },
  { value: "month", label: "Every month" },
] as const;

export const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
] as const;

export interface EveryRule {
  every: string;
  weekday: number | null;
  day_of_month: number | null;
  due_by: string | null;
}

/** "Every Wednesday by 18:00", "Every month on the 31st (the last day of a
 *  shorter month)", "Every day by the end of the day". */
export function everyText(rule: EveryRule): string {
  let when: string;
  if (rule.every === "week") {
    when = `Every ${WEEKDAYS[rule.weekday ?? 0] ?? "week"}`;
  } else if (rule.every === "month") {
    const day = rule.day_of_month ?? 1;
    when = `Every month on day ${day}`;
    if (day > 28) when += " (or the month's last day)";
  } else if (rule.every === "day") {
    when = "Every day";
  } else {
    when = rule.every;
  }
  return `${when}, ${dueByText(rule.due_by)}`;
}

export function dueByText(dueBy: string | null): string {
  return dueBy ? `by ${dueBy}` : "by the end of the day";
}

/** The existing screen an item opens, if it names one. */
const OPENS_PATH: Record<string, string> = {
  count_schedule: "/inventory?tab=schedule",
  cash_count: "/sell/cash-count",
};

export function opensPath(opens: string): string | null {
  return OPENS_PATH[opens] ?? null;
}

/** "2 of 5 ticked". */
export function progressText(items: { tick: unknown }[]): string {
  const done = items.filter((item) => item.tick).length;
  return `${done} of ${items.length} ticked`;
}

/** Where a checklist-missed alert opens: the store's checklist on Today. */
export function alertChecklistPath(storeCode: string): string {
  return storeCode ? `${CHECKLIST_PATH}?store=${encodeURIComponent(storeCode)}` : CHECKLIST_PATH;
}

/** One tick's key while it is on its way, so a retry after a dropped
 *  connection reuses the same id and the server saves one tick. */
export function tickKey(templateId: string, itemId: string, dueOn: string): string {
  return `${templateId}:${itemId}:${dueOn}`;
}
