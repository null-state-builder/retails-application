import { Field, useResourceList } from "../lib/goodsScreen";
import { useConfigurations, TENANT_SCOPE, type ConfigScope } from "../lib/goodsConfig";
import type { EditorProps, Payload } from "./configurationPayloads";
import { paiseStringToRupees, rupeesToPaise } from "../lib/format";

const INITIAL_ROLES = [
  "owner",
  "store_person",
  "warehouse",
  "brand_manager",
  "accounts",
  "it_admin",
];
const list = (value: unknown): string[] => (Array.isArray(value) ? value.map(String) : []);

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
      <div data-testid="working-calendar-editor">
        {text("timezone", "Business timezone", "An IANA timezone, for example Asia/Kolkata.")}
        <fieldset>
          <legend>Working days</legend>
          {["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map(
            (day, i) => (
              <Flag
                key={day}
                label={day}
                value={list(value.working_weekdays).includes(String(i + 1))}
                change={(checked) =>
                  set(
                    "working_weekdays",
                    checked
                      ? [...list(value.working_weekdays).map(Number), i + 1].sort()
                      : list(value.working_weekdays)
                          .map(Number)
                          .filter((n) => n !== i + 1),
                  )
                }
              />
            ),
          )}
        </fieldset>
        <Lines
          name="excluded_dates"
          label="Holidays and excluded dates"
          value={value.excluded_dates}
          change={(v) => set("excluded_dates", v)}
          hint="One date per line, YYYY-MM-DD."
        />
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
