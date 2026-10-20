// What a configuration line says, in a few plain rows — so its list card tells
// a reader what the change is without opening it. Each kind picks the handful
// of values someone would check first; anything unknown falls back to the
// payload's first readable fields.
import type { ConfigScope } from "../lib/goodsConfig";
import { roleLabel } from "../lib/goodsExceptions";

export interface SummaryRow {
  label: string;
  value: string;
}

export const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

/** An ISO date (YYYY-MM-DD) as "Fri, 25 Dec 2026" (or without the weekday). */
export function longDate(iso: string, weekday = true): string {
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, {
        ...(weekday ? { weekday: "short" as const } : {}),
        day: "numeric",
        month: "short",
        year: "numeric",
      });
}

const MAX_ROWS = 4;
const list = (value: unknown): string[] =>
  Array.isArray(value) ? value.map(String).filter((v) => v.trim() !== "") : [];
const yesNo = (value: unknown) => (value ? "Yes" : "No");
const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** `snake_case` or `dotted.code` as a sentence-case phrase. */
export function humanise(code: string): string {
  const words = code.replace(/[._]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : code;
}

function roles(value: unknown): string {
  const names = list(value).map((code) => {
    const known = roleLabel(code);
    return known === code ? humanise(code) : known;
  });
  return names.join(", ");
}

function firstFew(values: string[], shown = 3): string {
  if (values.length <= shown) return values.join(", ");
  return `${values.slice(0, shown).join(", ")} +${values.length - shown} more`;
}

/** ISO weekdays (1 = Monday) as "Mon to Sat", "Every day" or "Mon, Wed, Fri". */
export function weekdaysLabel(days: number[]): string {
  const sorted = [...new Set(days)].filter((d) => d >= 1 && d <= 7).sort((a, b) => a - b);
  if (sorted.length === 0) return "None";
  if (sorted.length === 7) return "Every day";
  const short = (d: number) => (WEEKDAYS[d - 1] ?? "").slice(0, 3);
  const first = sorted[0] ?? 1;
  const last = sorted[sorted.length - 1] ?? 1;
  const run = last - first === sorted.length - 1;
  if (run && sorted.length >= 3) return `${short(first)} to ${short(last)}`;
  return sorted.map(short).join(", ");
}

function closedDates(value: unknown): string {
  const dates = list(value).sort();
  const first = dates[0];
  if (first === undefined) return "None";
  if (dates.length === 1) return longDate(first, false);
  return `${dates.length} dates from ${longDate(first, false)}`;
}

const percent = (value: unknown) => (value === undefined || value === "" ? "" : `${value}%`);

function fallback(payload: Record<string, unknown>): SummaryRow[] {
  const rows: SummaryRow[] = [];
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      if (value.length === 0) continue;
      const scalars = value.every((v) => typeof v !== "object");
      rows.push({
        label: humanise(key),
        value: scalars ? firstFew(value.map(String)) : count(value.length, "entry", "entries"),
      });
    } else if (typeof value === "boolean") {
      rows.push({ label: humanise(key), value: yesNo(value) });
    } else if (typeof value !== "object") {
      rows.push({ label: humanise(key), value: String(value) });
    }
  }
  return rows;
}

export function summarizeConfiguration(
  kind: string,
  payload: Record<string, unknown> | null | undefined,
): SummaryRow[] {
  const p = payload ?? {};
  const rows: SummaryRow[] = (() => {
    switch (kind) {
      case "working_calendar":
        return [
          {
            label: "Working days",
            value: weekdaysLabel(list(p.working_weekdays).map(Number)),
          },
          { label: "Closed dates", value: closedDates(p.excluded_dates) },
          { label: "Timezone", value: String(p.timezone ?? "") },
        ];
      case "sell_policy":
        return [
          { label: "Manual discount cap", value: percent(p.manual_discount_cap_percent) },
          {
            label: "Return window",
            value:
              p.return_window_days === undefined ? "" : count(Number(p.return_window_days), "day"),
          },
          {
            label: "Discount on offer lines",
            value: yesNo(p.manual_discount_on_offer_lines),
          },
        ];
      case "approval":
        return [
          { label: "Action", value: String(p.action ?? "") },
          { label: "Approved by", value: roles(p.roles) },
          { label: "Different approver", value: yesNo(p.require_distinct) },
          { label: "Extra sign-in", value: yesNo(p.step_up) },
        ];
      case "workflow":
        return [
          { label: "Operation", value: String(p.operation ?? "") },
          { label: "Status", value: p.enabled ? "On" : "Off" },
          {
            label: "Needs first",
            value: list(p.prerequisites).length ? firstFew(list(p.prerequisites), 2) : "Nothing",
          },
        ];
      case "notifications": {
        const unit = String(p.sla_unit ?? "").replaceAll("_", " ");
        const n = Number(p.sla_value);
        return [
          { label: "Event", value: String(p.event ?? "") },
          { label: "Tells", value: roles(p.roles) },
          { label: "Email", value: yesNo(p.email) },
          {
            label: "Deadline",
            value:
              Number.isFinite(n) && unit ? `${n} ${n === 1 ? unit.replace(/s$/, "") : unit}` : "",
          },
        ];
      }
      case "rates":
        return [
          { label: "Transport", value: percent(p.transport_pct) },
          { label: "Pricing margin", value: percent(p.pricing_margin_pct) },
        ];
      case "tax_rates":
        return [
          { label: "Currency", value: String(p.currency ?? "") },
          {
            label: "HSN rules",
            value: Array.isArray(p.hsn_rules) ? String(p.hsn_rules.length) : "",
          },
        ];
      case "vocabulary":
        return [
          { label: "Dimension", value: String(p.dimension ?? "") },
          { label: "Values", value: firstFew(list(p.values)) },
        ];
      case "identity_profile":
        return [
          { label: "Family", value: String(p.family ?? "") },
          { label: "Tells items apart by", value: firstFew(list(p.distinguishing_dimensions)) },
          { label: "Sizes", value: firstFew(list(p.allowed_size_values)) },
        ];
      case "reasons":
        return [
          { label: "Action", value: String(p.action ?? "") },
          {
            label: "Reasons",
            value: Array.isArray(p.codes) ? String(p.codes.length) : "",
          },
        ];
      case "series":
        return [
          { label: "Document type", value: String(p.type ?? "") },
          { label: "Financial year", value: String(p.fy ?? "") },
          { label: "Block size", value: p.ceiling_block_size ? String(p.ceiling_block_size) : "" },
        ];
      case "label":
        return [
          {
            label: "Size",
            value: p.width_mm && p.height_mm ? `${p.width_mm} × ${p.height_mm} mm` : "",
          },
          { label: "Barcode", value: String(p.symbology ?? "") },
          { label: "Prints", value: list(p.fields).map(humanise).join(", ") },
        ];
      case "barcode_range":
        return [
          { label: "Prefix", value: String(p.prefix ?? "") },
          {
            label: "Range",
            value:
              p.start !== undefined && p.end !== undefined
                ? `${Number(p.start).toLocaleString()} – ${Number(p.end).toLocaleString()}`
                : "",
          },
          { label: "Barcode", value: String(p.symbology ?? "") },
        ];
      case "business_profile":
        return [
          { label: "Categories", value: firstFew(list(p.categories), 2) },
          { label: "Expected SKUs", value: p.expected_skus ? String(p.expected_skus) : "" },
          { label: "Sites", value: p.sites ? String(p.sites) : "" },
          { label: "Staff", value: p.staff ? String(p.staff) : "" },
        ];
      default:
        return fallback(p);
    }
  })();
  return rows.filter((row) => row.value.trim() !== "").slice(0, MAX_ROWS);
}

/** Who a configuration line covers, in a reader's words. */
export function scopeLabel(scope: ConfigScope | null | undefined): string {
  if (!scope) return "Whole business";
  switch (scope.scope_kind) {
    case "sites":
      return count(scope.site_ids.length, "store");
    case "brands":
      return count(scope.brand_ids.length, "brand");
    case "sbus":
      return count(scope.sbu_ids.length, "business unit");
    case "entity":
      return "One legal entity";
    default:
      return "Whole business";
  }
}
