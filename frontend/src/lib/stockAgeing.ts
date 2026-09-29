// What the Stock Ageing page says (store operations ticket 33, ST-INV-2). The
// server decides every group, age and flag (`stockledger.goods_ageing`); these
// only put them into words, so the page and its tests say the same thing.

export type AgeingGroup = "in_season" | "season_ended" | "unknown_season" | "season_unclear";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-01-10" as "10 Jan 2026", read as the calendar day it is (no clock). */
export function dayText(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  const monthName = month === undefined ? undefined : MONTHS[month - 1];
  if (!monthName || !Number.isInteger(year) || !Number.isInteger(day)) return iso;
  return `${day} ${monthName} ${year}`;
}

function days(n: number): string {
  return `${n} ${n === 1 ? "day" : "days"}`;
}

export function groupHeading(group: string, idleDays?: number | null): string {
  switch (group) {
    case "in_season":
      return idleDays == null
        ? "In season (no flag days set)"
        : `In season: flagged after ${idleDays} days with no sale`;
    case "season_ended":
      return "Season ended: aged from the day it ended";
    case "unknown_season":
      return "Unknown historical season: not aged";
    default:
      return "Season end not recorded: not aged until its end date is recorded";
  }
}

export function ageText(row: {
  group: string;
  age_days: number | null;
  last_sale_on: string | null;
}): string {
  if (row.age_days === null) return "Not aged";
  if (row.group === "season_ended") {
    return row.age_days === 0
      ? "Its season ended today"
      : `${days(row.age_days)} since its season ended`;
  }
  return row.last_sale_on
    ? `${days(row.age_days)} since its last sale here`
    : `${days(row.age_days)} with no sale here`;
}

/** Days in the company. Opening stock whose older arrival nobody recorded says
 *  so: its cutover day is when the system met it, not when it arrived. */
export function firstArrivalText(row: {
  first_arrived_on: string | null;
  days_in_company: number | null;
  in_company_before: string | null;
}): string {
  if (row.first_arrived_on && row.days_in_company !== null) {
    return `${days(row.days_in_company)} (since ${dayText(row.first_arrived_on)})`;
  }
  return row.in_company_before
    ? `Unknown (here before ${dayText(row.in_company_before)})`
    : "Unknown";
}

/** Where an ageing alert opens: its store's page. */
export function alertAgeingPath(storeId: number): string {
  return `/stock/ageing?site_id=${storeId}`;
}
