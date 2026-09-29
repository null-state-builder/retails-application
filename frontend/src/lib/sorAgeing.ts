// What the SOR Ageing page says (store operations ticket 24, ST-BRD-5). The
// server decides every state and date (`stockledger.sor_ageing`); these only put
// them into words, so the page and its tests say the same thing.

import { dayText } from "./stockAgeing";

export type SorState = "overdue" | "due" | "no_dispatch_date" | "ageing" | "invoiced";

function days(n: number): string {
  return `${n} ${n === 1 ? "day" : "days"}`;
}

export function stateHeading(state: string, alertMonths: number, invoiceMonths: number): string {
  switch (state) {
    case "overdue":
      return `Past ${invoiceMonths} months from dispatch with no brand invoice`;
    case "due":
      return `At ${alertMonths} months from dispatch: sell, return or get the brand's invoice`;
    case "no_dispatch_date":
      return "No dispatch date recorded: not aged until it is";
    case "ageing":
      return `Under ${alertMonths} months from dispatch`;
    default:
      return "Settled by the brand's invoice";
  }
}

export function deadlineText(row: {
  state: string;
  alert_on: string | null;
  invoice_by: string | null;
  days_left: number | null;
  brand_invoice: { number: string; invoice_date: string } | null;
}): string {
  if (row.state === "invoiced" && row.brand_invoice) {
    return `Brand invoice ${row.brand_invoice.number} of ${dayText(row.brand_invoice.invoice_date)}`;
  }
  if (!row.invoice_by || row.days_left === null) {
    return "Unknown until the dispatch date is recorded";
  }
  if (row.state === "overdue") {
    const when = row.days_left === 0 ? "today" : `${days(-row.days_left)} ago`;
    return `Brand invoice was due ${dayText(row.invoice_by)} (${when})`;
  }
  if (row.state === "ageing" && row.alert_on) return `Flagged on ${dayText(row.alert_on)}`;
  return `Settle by ${dayText(row.invoice_by)} (${days(row.days_left)} left)`;
}

/** The delivery a piece came on: where, when, and the vendor's invoice. */
export function deliveryText(row: {
  arrival_id: string | null;
  arrival_site: string;
  arrived_on: string | null;
  vendor_invoice: string;
}): string {
  if (!row.arrival_id || !row.arrived_on) return "Opening stock: no delivery to date it from";
  const invoice = row.vendor_invoice ? `, invoice ${row.vendor_invoice}` : "";
  return `${row.arrival_site}, ${dayText(row.arrived_on)}${invoice}`;
}

/** Where an SOR ageing alert opens: its site's page. */
export function alertSorPath(siteId: number): string {
  return `/brands/sor-ageing?site_id=${siteId}`;
}

export interface PendingCommand {
  /** What was pressed: the step, the delivery and what was typed. */
  key: string;
  commandId: string;
}

/** The command for pressing `key`. The very same press again, after a dropped
 *  connection, replays the command it was sent under, so the server records it
 *  once; anything else is a new command. */
export function pendingCommand(
  pending: PendingCommand | null,
  key: string,
  fresh: () => string,
): PendingCommand {
  return pending !== null && pending.key === key ? pending : { key, commandId: fresh() };
}
