// Setup > Audit Log (store operations PRD ST-OPS-3, ticket 02): the plain
// words for one entry. Pure helpers, so the wording is tested without a screen.
// What a reader may see is the server's decision; nothing here hides a value.

export interface AuditValue {
  field: string;
  redacted: boolean;
  value?: unknown;
}

export interface AuditFilters {
  person: string;
  store: string;
  record_type: string;
  date_from: string;
  date_to: string;
}

export const NO_FILTERS: AuditFilters = {
  person: "",
  store: "",
  record_type: "",
  date_from: "",
  date_to: "",
};

/** Only the filters that are set, as query parameters. */
export function filterParams(filters: AuditFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}

/** `applies_from` → "applies from"; `slabs.upto_1000` → "slabs, upto 1000". */
export function fieldLabel(field: string): string {
  return field
    .split(".")
    .map((part) => part.replace(/_/g, " "))
    .join(", ");
}

export function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "none";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** One before or after as lines of "field: value". Null means nothing was recorded. */
export function valueLines(values: readonly AuditValue[] | null | undefined): string[] {
  if (!values) return [];
  return values.map(
    (v) => `${fieldLabel(v.field)}: ${v.redacted ? "(hidden)" : valueText(v.value)}`,
  );
}

const DATE_KEYS = ["applies_from", "effective_from", "valid_from"];

/** A versioned setting's change, read from whatever the record holds: its new
 *  version and the date it applies from (ST-OPS-3). Null when it has neither. */
export function versionNote(after: readonly AuditValue[] | null | undefined): string | null {
  if (!after) return null;
  const find = (keys: string[]) => after.find((v) => keys.includes(v.field) && !v.redacted);
  const version = find(["version"]);
  const from = find(DATE_KEYS);
  if (!version && !from) return null;
  const parts = [];
  if (version) parts.push(`Version ${valueText(version.value)}`);
  if (from) parts.push(`applies from ${valueText(from.value)}`);
  return parts.join(", ");
}

/** Who made the change, in words the reader is allowed. */
export function whoText(entry: {
  actor_name?: string | null;
  actor_hidden?: boolean;
  service_code?: string | null;
}): string {
  if (entry.actor_name) return entry.actor_name;
  if (entry.actor_hidden) return "A person (not shown to you)";
  if (entry.service_code) return `System (${entry.service_code})`;
  return "Unknown";
}

export function whereText(entry: {
  store_code?: string | null;
  store_name?: string | null;
}): string {
  return entry.store_code
    ? `${entry.store_name ?? entry.store_code} (${entry.store_code})`
    : "No store";
}

/** `stock.transfer.create` → "Stock transfer create"; the code stays in a tooltip. */
export function actionText(action: string): string {
  const words = action.replace(/[._]/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : action;
}

/** A failed request with no answer at all is a lost connection, not a refusal. */
export function isConnectionLost(error: unknown): boolean {
  const e = error as {
    response?: unknown;
    code?: string;
    message?: string;
  } | null;
  if (!e || e.response) return false;
  return e.code === "ERR_NETWORK" || /network/i.test(e.message ?? "");
}
