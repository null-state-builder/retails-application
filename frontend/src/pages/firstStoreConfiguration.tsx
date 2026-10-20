import { useRef, useState } from "react";

import { Field, useResourceList } from "../lib/goodsScreen";
import { useConfigurations, TENANT_SCOPE, type ConfigScope } from "../lib/goodsConfig";
import type { EditorProps, Payload } from "./configurationPayloads";
import { paiseStringToRupees, rupeesToPaise } from "../lib/format";
import { longDate, WEEKDAYS } from "./configurationSummary";

const INITIAL_ROLES = [
  "owner",
  "store_person",
  "warehouse",
  "brand_manager",
  "accounts",
  "it_admin",
];
const list = (value: unknown): string[] => (Array.isArray(value) ? value.map(String) : []);

function timezones(): string[] {
  try {
    return (Intl as unknown as { supportedValuesOf(k: string): string[] }).supportedValuesOf(
      "timeZone",
    );
  } catch {
    return ["Asia/Kolkata"];
  }
}

/** ISO weekdays (1 = Monday .. 7 = Sunday), as the backend calendar reads them. */
function WorkingDays({ value, change }: { value: number[]; change: (days: number[]) => void }) {
  const on = new Set(value);
  const toggle = (day: number) => {
    const next = new Set(on);
    if (next.has(day)) next.delete(day);
    else next.add(day);
    change([...next].sort((a, b) => a - b));
  };
  const PRESETS = [
    { label: "Mon to Sat", days: [1, 2, 3, 4, 5, 6] },
    { label: "Every day", days: [1, 2, 3, 4, 5, 6, 7] },
  ];
  const matches = (days: number[]) => days.length === on.size && days.every((d) => on.has(d));
  return (
    <fieldset className="cfg-week cfg-sect" aria-describedby="cfg-week-hint">
      <legend className="sr-only">Working days</legend>
      <div className="cfg-sect-head" aria-hidden="true">
        <span className="cfg-sect-label">Working days</span>
        <span className="cfg-sect-count" data-testid="cfg-week-count">
          {on.size === 0 ? "None chosen" : `${on.size} a week`}
        </span>
      </div>
      <p className="goods-hint" id="cfg-week-hint">
        {on.size === 0
          ? "Choose at least one day the business works."
          : "Tap a day to switch it on or off."}
      </p>
      <div className="cfg-week-days">
        {WEEKDAYS.map((day, i) => (
          <label key={day} className="cfg-day">
            <input
              type="checkbox"
              checked={on.has(i + 1)}
              onChange={() => toggle(i + 1)}
              data-testid={`cfg-weekday-${i + 1}`}
            />
            <span aria-hidden="true">{day.slice(0, 3)}</span>
            <span className="sr-only">{day}</span>
          </label>
        ))}
      </div>
      <div className="cfg-presets">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className={`btn ${matches(preset.days) ? "btn-active" : ""}`}
            aria-pressed={matches(preset.days)}
            onClick={() => change(preset.days)}
          >
            {preset.label}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

/** Holidays as ISO dates (YYYY-MM-DD), kept sorted and unique. A date that is
 *  already a non-working weekday is flagged, since it changes nothing. */
function HolidayDates({
  value,
  workingDays,
  change,
}: {
  value: string[];
  workingDays: number[];
  change: (dates: string[]) => void;
}) {
  const [pick, setPick] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const add = () => {
    if (!pick) {
      input.current?.focus();
      input.current?.showPicker?.();
      return;
    }
    change([...new Set([...value, pick])].sort());
    setPick("");
  };
  const isoWeekday = (iso: string) => ((new Date(`${iso}T00:00:00`).getDay() + 6) % 7) + 1;
  return (
    <div className="cfg-holidays cfg-sect">
      <div className="cfg-sect-head">
        <label className="cfg-sect-label" htmlFor="cfg-excluded-date">
          Closed dates
        </label>
        <span className="cfg-sect-count">
          {value.length === 0 ? "None" : `${value.length} ${value.length === 1 ? "date" : "dates"}`}
        </span>
      </div>
      <p className="goods-hint" id="cfg-excluded-date-hint">
        Holidays and other days the business is shut.
      </p>
      <div className="cfg-holiday-add">
        <input
          ref={input}
          id="cfg-excluded-date"
          className="input"
          type="date"
          value={pick}
          onChange={(e) => setPick(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          aria-describedby="cfg-excluded-date-hint"
          data-testid="cfg-excluded_dates"
        />
        <button type="button" className="btn" onClick={add}>
          Add date
        </button>
      </div>
      <ul className="cfg-holiday-list" aria-label="Closed dates">
        {value.length === 0 && <li className="cfg-holiday-empty">No closed dates yet.</li>}
        {value.map((iso) => {
          const offDay = workingDays.length > 0 && !workingDays.includes(isoWeekday(iso));
          return (
            <li key={iso}>
              <span className="cfg-holiday-when">
                <time dateTime={iso}>{longDate(iso)}</time>
                {offDay && <span className="goods-hint">Already a non-working day</span>}
              </span>
              <button
                type="button"
                className="cfg-holiday-remove"
                onClick={() => change(value.filter((d) => d !== iso))}
                aria-label={`Remove ${longDate(iso)}`}
              >
                Remove
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function FirstStoreConfigScope({
  value,
  onChange,
  locked,
}: {
  value: ConfigScope;
  onChange: (next: ConfigScope) => void;
  locked: boolean;
}) {
  const sites = useResourceList<{ name: string }>("/goods-v1/masters/stores");
  const scoped = value.scope_kind !== "tenant";
  return (
    <fieldset disabled={locked}>
      <legend>Where this policy applies</legend>
      <select
        className="input"
        aria-label="Policy scope"
        value={scoped ? "sites" : "tenant"}
        onChange={(e) =>
          onChange(
            e.target.value === "tenant"
              ? { ...TENANT_SCOPE }
              : { ...TENANT_SCOPE, scope_kind: "sites" },
          )
        }
      >
        <option value="tenant">Entire company (explicit)</option>
        <option value="sites">Selected stores</option>
      </select>
      {scoped && (
        <div>
          {sites.items.map((site) => (
            <Flag
              key={site.id}
              label={site.data.name}
              value={value.site_ids.includes(site.id)}
              change={(checked) =>
                onChange({
                  ...value,
                  site_ids: checked
                    ? [...value.site_ids, site.id]
                    : value.site_ids.filter((id) => id !== site.id),
                })
              }
            />
          ))}
        </div>
      )}
      <p className="muted-cell">
        A policy's scope never grants access. Each person still needs one assignment covering the
        action, fields and complete resource.
      </p>
    </fieldset>
  );
}

function Text({
  name,
  label,
  value,
  change,
  type = "text",
  hint,
}: {
  name: string;
  label: string;
  value: unknown;
  change: (next: string) => void;
  type?: string;
  hint?: string;
}) {
  return (
    <Field id={`cfg-${name}`} label={label} hint={hint}>
      <input
        id={`cfg-${name}`}
        className="input"
        type={type}
        value={String(value ?? "")}
        onChange={(e) => change(e.target.value)}
        data-testid={`cfg-${name}`}
      />
    </Field>
  );
}

function Flag({
  label,
  value,
  change,
}: {
  label: string;
  value: unknown;
  change: (next: boolean) => void;
}) {
  return (
    <label className="check-label">
      <input type="checkbox" checked={value === true} onChange={(e) => change(e.target.checked)} />{" "}
      {label}
    </label>
  );
}

function Lines({
  name,
  label,
  value,
  change,
  hint,
}: {
  name: string;
  label: string;
  value: unknown;
  change: (next: string[]) => void;
  hint?: string;
}) {
  return (
    <Field id={`cfg-${name}`} label={label} hint={hint ?? "One entry per line."}>
      <textarea
        id={`cfg-${name}`}
        className="input"
        value={list(value).join("\n")}
        onChange={(e) => change(e.target.value.split("\n"))}
        data-testid={`cfg-${name}`}
      />
    </Field>
  );
}

function Roles({ value, change }: { value: unknown; change: (next: string[]) => void }) {
  const selected = list(value);
  return (
    <fieldset>
      <legend>Authorised roles</legend>
      {INITIAL_ROLES.map((code) => (
        <Flag
          key={code}
          label={code.replaceAll("_", " ")}
          value={selected.includes(code)}
          change={(checked) =>
            change(checked ? [...selected, code] : selected.filter((c) => c !== code))
          }
        />
      ))}
    </fieldset>
  );
}

function Version({
  kind,
  name,
  label,
  value,
  change,
}: {
  kind: string;
  name: string;
  label: string;
  value: unknown;
  change: (next: string) => void;
}) {
  const configs = useConfigurations(kind);
  const versions = configs.items.flatMap((row) =>
    (row.data.versions ?? [])
      .filter((v) => v.state === "effective" || v.state === "scheduled")
      .map((v) => ({
        id: v.id,
        label: `${row.data.payload.family ?? kind} · version ${v.version} (${v.state})`,
      })),
  );
  return (
    <Field id={`cfg-${name}`} label={label} hint={configs.failure || "Choose an approved version."}>
      <select
        id={`cfg-${name}`}
        className="input"
        value={String(value ?? "")}
        onChange={(e) => change(e.target.value)}
      >
        <option value="">Choose…</option>
        {versions.map((v) => (
          <option key={v.id} value={v.id}>
            {v.label}
          </option>
        ))}
      </select>
    </Field>
  );
}

export function FirstStorePolicyEditor({ kind, value, onChange }: EditorProps & { kind: string }) {
  const set = (key: string, next: unknown) => onChange({ ...value, [key]: next });
  const text = (key: string, label: string, hint?: string) => (
    <Text
      key={key}
      name={key}
      label={label}
      value={value[key]}
      change={(v) => set(key, v)}
      {...(hint ? { hint } : {})}
    />
  );
  const number = (key: string, label: string) => (
    <Text
      key={key}
      name={key}
      label={label}
      type="number"
      value={value[key]}
      change={(v) => set(key, v === "" ? null : Number(v))}
    />
  );
  if (kind === "sell_policy")
    return (
      <div className="form-grid" data-testid="sell-policy-editor">
        {text(
          "manual_discount_cap_percent",
          "Maximum manual discount (%)",
          "0 to 100; this does not override item discount restrictions.",
        )}
        {number("return_window_days", "Return window (days)")}
        <Flag
          label="Allow manual discount on offer lines"
          value={value.manual_discount_on_offer_lines}
          change={(v) => set("manual_discount_on_offer_lines", v)}
        />
      </div>
    );
  if (kind === "working_calendar")
    return (
      <div className="cfg-calendar" data-testid="working-calendar-editor">
        <div className="cfg-two">
          <WorkingDays
            value={list(value.working_weekdays).map(Number)}
            change={(days) => set("working_weekdays", days)}
          />
          <HolidayDates
            value={list(value.excluded_dates).filter(Boolean)}
            workingDays={list(value.working_weekdays).map(Number)}
            change={(dates) => set("excluded_dates", dates)}
          />
        </div>
        <div className="cfg-two cfg-calendar-foot">
          <div className="cfg-tz">
            <label htmlFor="cfg-timezone">Timezone</label>
            <input
              id="cfg-timezone"
              className="input"
              list="cfg-timezone-options"
              value={String(value.timezone ?? "")}
              onChange={(e) => set("timezone", e.target.value)}
              aria-describedby="cfg-timezone-hint"
              data-testid="cfg-timezone"
            />
            <datalist id="cfg-timezone-options">
              {timezones().map((tz) => (
                <option key={tz} value={tz} />
              ))}
            </datalist>
          </div>
          <span className="goods-hint" id="cfg-timezone-hint">
            Deadlines are counted in this timezone.
          </span>
        </div>
      </div>
    );
  if (kind === "approval")
    return (
      <div data-testid="approval-policy-editor">
        {text(
          "action",
          "Approval responsibility",
          "Use the exact workflow action, for example pt.approve.opening.",
        )}
        <Roles value={value.roles} change={(v) => set("roles", v)} />
        <p className="muted-cell">
          The maker and each reviewer must be different people. Routes never shorten after
          submission.
        </p>
        <Flag
          label="Require different people"
          value={value.require_distinct}
          change={(v) => set("require_distinct", v)}
        />
        <Flag
          label="Require password confirmation"
          value={value.step_up}
          change={(v) => set("step_up", v)}
        />
        {number("qty_max", "Maximum quantity (optional)")}
        <Text
          name="value_max"
          label="Maximum value in rupees (optional)"
          value={value.value_max == null ? "" : paiseStringToRupees(String(value.value_max))}
          change={(v) => set("value_max", v ? rupeesToPaise(v) : null)}
        />
        <Field id="cfg-unknown-value" label="When a protected value is unknown">
          <select
            id="cfg-unknown-value"
            className="input"
            value={String(value.unknown_value ?? "refuse")}
            onChange={(e) => set("unknown_value", e.target.value)}
          >
            <option value="refuse">Refuse approval</option>
            <option value="quantity_only">Quantity-only responsibility</option>
          </select>
        </Field>
        <p>Additional review steps</p>
        {(Array.isArray(value.steps) ? (value.steps as Payload[]) : []).map((step, index) => (
          <div className="card section-card" key={index}>
            <Text
              name={`step-${index}`}
              label="Step name"
              value={step.label}
              change={(label) =>
                set(
                  "steps",
                  (value.steps as Payload[]).map((s, i) => (i === index ? { ...s, label } : s)),
                )
              }
            />
            <Roles
              value={step.roles}
              change={(roles) =>
                set(
                  "steps",
                  (value.steps as Payload[]).map((s, i) => (i === index ? { ...s, roles } : s)),
                )
              }
            />
            <button
              className="btn btn-sm"
              onClick={() =>
                set(
                  "steps",
                  (value.steps as Payload[]).filter((_, i) => i !== index),
                )
              }
            >
              Remove step
            </button>
          </div>
        ))}
        <button
          className="btn btn-sm"
          onClick={() =>
            set("steps", [
              ...(Array.isArray(value.steps) ? value.steps : []),
              { label: "", roles: [] },
            ])
          }
        >
          Add review step
        </button>
      </div>
    );
  if (kind === "workflow")
    return (
      <div data-testid="workflow-editor">
        {text("operation", "Workflow operation")}
        <Flag label="Enabled" value={value.enabled} change={(v) => set("enabled", v)} />
        <Lines
          name="prerequisites"
          label="Required prerequisites"
          value={value.prerequisites}
          change={(v) => set("prerequisites", v)}
        />
      </div>
    );
  if (kind === "notifications")
    return (
      <div data-testid="notification-policy-editor">
        {text("event", "Notification event")}
        <Roles value={value.roles} change={(v) => set("roles", v)} />
        <Flag label="Email notifications" value={value.email} change={(v) => set("email", v)} />
        {number("sla_value", "Due after")}
        <Field id="cfg-sla-unit" label="Deadline unit">
          <select
            id="cfg-sla-unit"
            className="input"
            value={String(value.sla_unit ?? "working_days")}
            onChange={(e) => set("sla_unit", e.target.value)}
          >
            {["working_days", "days", "hours"].map((s) => (
              <option key={s} value={s}>
                {s.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </Field>
        <Version
          kind="working_calendar"
          name="calendar_version_id"
          label="Approved working calendar"
          value={value.calendar_version_id}
          change={(v) => set("calendar_version_id", v)}
        />
      </div>
    );
  return <BusinessProfileEditor value={value} onChange={onChange} />;
}

function BusinessProfileEditor({ value, onChange }: EditorProps) {
  const sites = useResourceList<{ name: string }>("/goods-v1/masters/stores");
  const set = (key: string, next: unknown) => onChange({ ...value, [key]: next });
  return (
    <div data-testid="business-profile-editor">
      <Version
        kind="identity_profile"
        name="identity_profile_id"
        label="Approved SKU identity profile"
        value={value.identity_profile_id}
        change={(v) => set("identity_profile_id", v)}
      />
      {["categories", "commercial_labels"].map((key) => (
        <Lines
          key={key}
          name={key}
          label={key.replaceAll("_", " ")}
          value={value[key]}
          change={(v) => set(key, v)}
        />
      ))}
      {[
        "expected_skus",
        "brands",
        "sites",
        "sbus",
        "staff",
        "documents_per_day",
        "evidence_bytes_per_year",
      ].map((key) => (
        <Text
          key={key}
          name={key}
          label={key.replaceAll("_", " ")}
          type="number"
          value={value[key]}
          change={(v) => set(key, Number(v))}
        />
      ))}
      {["workforce_scope", "accounting_interface"].map((key) => (
        <Text
          key={key}
          name={key}
          label={key.replaceAll("_", " ")}
          value={value[key]}
          change={(v) => set(key, v)}
        />
      ))}
      <fieldset>
        <legend>Counters per store</legend>
        {sites.items.map((site) => {
          const rows = (
            Array.isArray(value.tills_per_site) ? value.tills_per_site : []
          ) as Payload[];
          return (
            <Text
              key={site.id}
              name={`counter-${site.id}`}
              label={site.data.name}
              type="number"
              value={rows.find((row) => String(row.site_id) === site.id)?.count ?? ""}
              change={(v) =>
                set("tills_per_site", [
                  ...rows.filter((row) => String(row.site_id) !== site.id),
                  { site_id: site.id, count: Number(v) },
                ])
              }
            />
          );
        })}
      </fieldset>
    </div>
  );
}
