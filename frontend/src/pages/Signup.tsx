import { createContext, useContext, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { KdpsLogo } from "../components/KdpsLogo";
import { api, apiErrorMessage } from "../lib/api";
import "./Signup.css";

export interface RegistrationState {
  available: boolean;
  pending_confirmation: boolean;
  message: string;
  synthetic: boolean;
  options?: RegistrationOptions;
}

type Person = { name: string; email: string; staff_code: string; temporary_password: string };
type Proposed = Omit<Person, "temporary_password"> & { role_code: string };
export type RegistrationSummary = {
  company: Record<string, string>;
  store: Record<string, string | number>;
  owner: Omit<Person, "temporary_password">;
  admin: Omit<Person, "temporary_password">;
  proposed_team: Proposed[];
  initial_access?: {
    policy_baseline?: {
      roles: Record<
        string,
        {
          section_access: Record<string, { capability: string; label: string }>;
          field_access: string[];
          step_actions: string[];
        }
      >;
      action_levels: Record<string, { section: string; minimum: string }>;
    };
  };
};
type RegistrationResult = {
  state: "awaiting_confirmation" | "registered";
  summary?: RegistrationSummary;
  summary_hash?: string;
  revision?: number;
  confirming_role?: "owner" | "admin";
  confirmed?: { owner: boolean; admin: boolean };
};
const CLOSED = "This installation is already registered. New company registrations are closed.";
const ROLES = [
  ["store_person", "Store Person (pilot manager)"],
  ["warehouse", "Warehouse"],
  ["brand_manager", "Brand Manager"],
  ["accounts", "Accounts"],
  ["it_admin", "Admin"],
  ["owner", "Owner"],
] as const;
type Option = { value: string; label: string };
type RegistrationOptions = {
  countries: Option[];
  timezones: Option[];
  currencies: Option[];
  languages: Option[];
  formats: Option[];
  states: (Option & { cities: string[] })[];
  code_defaults: { company: string; store: string; staff_prefix: string };
};
const SETUP_STEPS = [
  { id: "company", label: "Company" },
  { id: "regional", label: "Regional settings" },
  { id: "store", label: "First store" },
  { id: "people", label: "People" },
  { id: "review", label: "Review" },
  { id: "confirmation", label: "Confirmation" },
] as const;
const FieldErrors = createContext<Record<string, string>>({});

const blankPerson = (): Person => ({ name: "", email: "", staff_code: "", temporary_password: "" });
type SignupDraft = Pick<
  RegistrationSummary,
  "company" | "store" | "owner" | "admin" | "proposed_team"
>;
const DRAFT_KEY = "kdps.signup.draft.v1";
function draftDetails(value: SignupDraft): SignupDraft {
  const person = (p: SignupDraft["owner"]) => ({
    name: p.name,
    email: p.email,
    staff_code: p.staff_code,
  });
  return {
    company: Object.fromEntries(
      Object.entries(value.company).filter(([key]) =>
        [
          "code",
          "name",
          "legal_name",
          "pan",
          "gstin",
          "state_code",
          "state_name",
          "billing_address",
          "country",
          "timezone",
          "currency",
          "locale",
        ].includes(key),
      ),
    ),
    store: Object.fromEntries(
      Object.entries(value.store).filter(([key]) =>
        [
          "code",
          "name",
          "city",
          "address",
          "setup_kind",
          "source_system",
          "counter_count",
        ].includes(key),
      ),
    ),
    owner: person(value.owner),
    admin: person(value.admin),
    proposed_team: value.proposed_team.map((p) => ({ ...person(p), role_code: p.role_code })),
  };
}
function readDraft(): SignupDraft | null {
  if (typeof window === "undefined") return null;
  try {
    const value = JSON.parse(sessionStorage.getItem(DRAFT_KEY) ?? "null") as SignupDraft | null;
    if (
      !value ||
      !value.company ||
      !value.store ||
      !value.owner ||
      !value.admin ||
      !Array.isArray(value.proposed_team)
    )
      return null;
    if (
      ![value.company, value.store, value.owner, value.admin, ...value.proposed_team].every(
        (record) =>
          Object.values(record).every((v) => typeof v === "string" || typeof v === "number"),
      )
    )
      return null;
    return draftDetails(value);
  } catch {
    return null;
  }
}

export function InstallationGate() {
  const { user, session, loading } = useAuth();
  const { pathname, key } = useLocation();
  const [state, setState] = useState<RegistrationState | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [setupResolution, setSetupResolution] = useState<{
    key: string;
    userId: number;
    required: boolean;
    error?: string;
  } | null>(null);
  useEffect(() => {
    let live = true;
    api
      .get<RegistrationState>("/auth/registration")
      .then(({ data }) => {
        if (live) {
          setState(data);
          setError("");
        }
      })
      .catch((e: unknown) => {
        if (live) setError(apiErrorMessage(e));
      });
    return () => {
      live = false;
    };
  }, [retry]);
  useEffect(() => {
    let live = true;
    if (
      pathname !== "/" ||
      !user?.display_actions.includes("access.manage") ||
      session?.user.must_change_password
    ) {
      return;
    }
    api
      .get<{ setup_complete: boolean }>("/auth/admin/registration")
      .then(({ data }) => {
        if (live) setSetupResolution({ key, userId: user.id, required: !data.setup_complete });
      })
      .catch((e: unknown) => {
        const status = (e as { response?: { status?: number } }).response?.status;
        if (live)
          setSetupResolution({
            key,
            userId: user.id,
            required: false,
            ...(status !== 404 && status !== 403 ? { error: apiErrorMessage(e) } : {}),
          });
      });
    return () => {
      live = false;
    };
  }, [pathname, key, user, session, retry]);
  if (pathname === "/" && !user) {
    if (loading || (!state && !error))
      return <div className="full-loader">Loading this installation…</div>;
    if (!state)
      return (
        <main className="signup">
          <p role="alert">{error}</p>
          <button className="btn" onClick={() => setRetry(retry + 1)}>
            Retry
          </button>
        </main>
      );
    return <Navigate to={state.available ? "/signup" : "/login"} replace />;
  }
  if (
    pathname === "/" &&
    user?.display_actions.includes("access.manage") &&
    !session?.user.must_change_password
  ) {
    if (!setupResolution || setupResolution.key !== key || setupResolution.userId !== user.id)
      return <div className="full-loader">Loading company setup…</div>;
    if (setupResolution.error)
      return (
        <main className="signup">
          <p role="alert">{setupResolution.error}</p>
          <button
            className="btn"
            onClick={() => {
              setSetupResolution(null);
              setRetry(retry + 1);
            }}
          >
            Retry
          </button>
        </main>
      );
    if (setupResolution.required) return <Navigate to="/setup/first-store" replace />;
  }
  return <Outlet />;
}

function describedBy(name: string, hint: string | undefined, error: string | undefined) {
  return [hint && `hint-${name}`, error && `error-${name}`].filter(Boolean).join(" ") || undefined;
}

function Hint({ name, text }: { name: string; text?: string | undefined }) {
  return text ? (
    <span className="signup-hint" id={`hint-${name}`}>
      {text}
    </span>
  ) : null;
}

function Input({
  label,
  value,
  onChange,
  name,
  type = "text",
  required = true,
  multiline = false,
  hint,
  wide = multiline,
  toggleName = label,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  name: string;
  type?: string;
  required?: boolean;
  multiline?: boolean;
  hint?: string | undefined;
  wide?: boolean;
  /** Distinguishes repeated labels, e.g. "Owner temporary password". */
  toggleName?: string;
}) {
  const errors = useContext(FieldErrors);
  const [visible, setVisible] = useState(false);
  const props = {
    id: `signup-${name}`,
    className: "input",
    value,
    required,
    "aria-invalid": Boolean(errors[name]),
    "aria-describedby": describedBy(name, hint, errors[name]),
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      onChange(e.target.value),
    "data-testid": `signup-${name}`,
  };
  return (
    <div className={`field ${wide ? "signup-wide" : ""}`}>
      <label htmlFor={props.id}>
        {label}
        {!required && " (optional)"}
      </label>
      {multiline ? (
        <textarea {...props} rows={3} />
      ) : (
        <div className={type === "password" ? "signup-password" : undefined}>
          <input
            {...props}
            type={type === "password" && visible ? "text" : type}
            autoComplete={type === "password" ? "new-password" : "off"}
          />
          {type === "password" && (
            <button
              type="button"
              className="btn"
              aria-label={`${visible ? "Hide" : "Show"} ${toggleName}`}
              aria-pressed={visible}
              onClick={() => setVisible(!visible)}
            >
              {visible ? "Hide" : "Show"}
            </button>
          )}
        </div>
      )}
      <Hint name={name} text={hint} />
      {errors[name] && (
        <span className="signup-field-error" id={`error-${name}`}>
          {errors[name]}
        </span>
      )}
    </div>
  );
}

function Select({
  label,
  name,
  value,
  onChange,
  options,
  placeholder,
  hint,
  disabled = false,
}: {
  label: string;
  name: string;
  value: string;
  onChange: (v: string) => void;
  options: Option[];
  placeholder?: string;
  hint?: string | undefined;
  disabled?: boolean;
}) {
  const errors = useContext(FieldErrors);
  return (
    <div className="field">
      <label htmlFor={`signup-${name}`}>{label}</label>
      <select
        className="input"
        id={`signup-${name}`}
        data-testid={`signup-${name}`}
        required
        disabled={disabled}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={Boolean(errors[name])}
        aria-describedby={describedBy(name, hint, errors[name])}
      >
        {placeholder && <option value="">{placeholder}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <Hint name={name} text={hint} />
      {errors[name] && (
        <span className="signup-field-error" id={`error-${name}`}>
          {errors[name]}
        </span>
      )}
    </div>
  );
}

function CodeField({
  label,
  name,
  value,
  preview,
  onChange,
}: {
  label: string;
  name: string;
  value: string;
  preview: string;
  onChange: (v: string) => void;
}) {
  const [custom, setCustom] = useState(Boolean(value));
  return (
    <div className="signup-code signup-wide">
      {!custom && (
        <p className="signup-code-preview">
          <span>{label}</span> <code>{preview}</code>{" "}
          <span className="signup-code-note">Final when saved</span>
        </p>
      )}
      <label className="signup-code-toggle">
        <input
          type="checkbox"
          checked={custom}
          aria-label={`Use my own ${label.toLowerCase()}`}
          onChange={(e) => {
            setCustom(e.target.checked);
            if (!e.target.checked) onChange("");
          }}
        />
        Use my own code
      </label>
      {custom && <Input label={label} name={name} value={value} onChange={onChange} wide />}
    </div>
  );
}

function PersonFields({
  who,
  value,
  preview,
  passwordHint,
  onChange,
}: {
  who: string;
  value: Person;
  preview: string;
  passwordHint: string;
  onChange: (p: Person) => void;
}) {
  return (
    <div className="signup-grid signup-grid-single">
      <Input
        label="Full name"
        name={`${who}-name`}
        value={value.name}
        onChange={(name) => onChange({ ...value, name })}
      />
      <Input
        label="Email"
        type="email"
        name={`${who}-email`}
        value={value.email}
        onChange={(email) => onChange({ ...value, email })}
      />
      <Input
        label="Temporary password"
        type="password"
        name={`${who}-password`}
        value={value.temporary_password}
        hint={passwordHint}
        toggleName={`${who === "owner" ? "Owner" : "Admin"} temporary password`}
        onChange={(temporary_password) => onChange({ ...value, temporary_password })}
      />
      <CodeField
        label="Staff code"
        name={`${who}-staff-code`}
        value={value.staff_code}
        preview={preview}
        onChange={(staff_code) => onChange({ ...value, staff_code })}
      />
    </div>
  );
}

function optionLabel(list: Option[] | undefined, value: string, fallback: string) {
  return list?.find((o) => o.value === value)?.label ?? fallback;
}

export function regionalLabels(company: Record<string, string>, options?: RegistrationOptions) {
  const indian = company.locale === "en-IN";
  const locale = company.locale ?? "";
  const language = optionLabel(options?.languages, locale, indian ? "English — India" : locale);
  // Format labels read "Indian — 1,23,456.78"; the sample alone is enough beside the language.
  const format = optionLabel(options?.formats, locale, indian ? "1,23,456.78" : "")
    .split(" — ")
    .pop();
  return [
    [
      "Country",
      optionLabel(
        options?.countries,
        company.country ?? "",
        company.country === "IN" ? "India — IN" : (company.country ?? ""),
      ),
    ],
    ["Time zone", optionLabel(options?.timezones, company.timezone ?? "", company.timezone ?? "")],
    ["Currency", optionLabel(options?.currencies, company.currency ?? "", company.currency ?? "")],
    ["Language and number format", format ? `${language} · ${format}` : language],
  ] as const;
}

export function previewStaffCodes(people: { staff_code: string }[]): string[] {
  const used = new Set(people.map((p) => p.staff_code.toLowerCase()).filter(Boolean));
  let next = 1;
  return people.map((p) => {
    if (p.staff_code) return p.staff_code;
    while (used.has(`emp-${String(next).padStart(4, "0")}`)) next++;
    const code = `EMP-${String(next++).padStart(4, "0")}`;
    used.add(code.toLowerCase());
    return code;
  });
}

export function registrationErrors(error: unknown): Record<string, string> {
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  const result: Record<string, string> = {};
  function walk(value: unknown, path: string[]) {
    if (typeof value === "string") {
      let key = path
        .join("-")
        .replace(/^company-/, "")
        .replace(/^proposed_team-/, "team-")
        .replace(/staff_code$/, "staff-code")
        .replace(/temporary_password$/, "password");
      if (key === "store-setup_kind") key = "store-kind";
      if (key === "store-source_system") key = "source-system";
      if (key === "state_code") key = "state";
      if (
        !path.includes("non_field_errors") &&
        path[0] &&
        ["company", "store", "owner", "admin", "proposed_team"].includes(path[0])
      )
        result[key] = result[key] ? `${result[key]} ${value}` : value;
    } else if (Array.isArray(value))
      value.forEach((v, i) => walk(v, typeof v === "string" ? path : [...path, String(i)]));
    else if (value && typeof value === "object")
      Object.entries(value).forEach(([k, v]) => walk(v, [...path, k]));
  }
  walk(data, []);
  return result;
}

function fieldLabel(name: string): string {
  const person = /^(owner|admin|team-\d+)-(.+)$/.exec(name);
  const labels: Record<string, string> = {
    name: "Company name",
    code: "Company code",
    legal_name: "Registered legal name",
    pan: "PAN",
    gstin: "GSTIN",
    state: "State",
    billing_address: "Billing address",
    "store-name": "Store name",
    "store-code": "Store code",
    "store-city": "City name",
    "store-city-choice": "City",
    "store-address": "Store address",
    "store-kind": "Store starting option",
    "source-system": "Current software name",
  };
  if (person) {
    const who = person[1]!.startsWith("team-")
      ? `Team member ${Number(person[1]!.slice(5)) + 1}`
      : person[1] === "owner"
        ? "Owner"
        : "Admin";
    const field =
      person[2] === "password" ? "temporary password" : person[2]!.replace(/[-_]/g, " ");
    return `${who} ${field}`;
  }
  return labels[name] ?? name.replace(/[-_]/g, " ");
}

export function RegistrationFeedback({
  errors,
  error,
  onSelect,
}: {
  errors: Record<string, string>;
  error: string;
  onSelect: (name: string) => void;
}) {
  if (Object.keys(errors).length) {
    return (
      <div className="warn-note" role="alert">
        <strong>Check these details</strong>
        <ul>
          {Object.entries(errors).map(([name, message]) => (
            <li key={name}>
              <a href={`#signup-${name}`} onClick={() => onSelect(name)}>
                {fieldLabel(name)}: {message}
              </a>
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return error ? (
    <div role="alert" className="warn-note" data-testid="signup-error">
      {error}
    </div>
  ) : null;
}

function SummaryGroup({
  id,
  title,
  onEdit,
  children,
}: {
  id: string;
  title: string;
  onEdit?: ((id: string) => void) | undefined;
  children: React.ReactNode;
}) {
  return (
    <section className="signup-summary-group" aria-labelledby={`summary-${id}`}>
      <div className="signup-summary-head">
        <h3 id={`summary-${id}`}>{title}</h3>
        {onEdit && (
          <button
            type="button"
            className="btn"
            aria-label={`Edit ${title}`}
            onClick={() => onEdit(id)}
          >
            Edit
          </button>
        )}
      </div>
      {children}
    </section>
  );
}

function Values({ rows }: { rows: [string, React.ReactNode, string?][] }) {
  return (
    <dl className="signup-values">
      {rows.map(([label, value, className]) => (
        <div key={label} className={className}>
          <dt>{label}</dt>
          <dd>{value || "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

function roleLabel(code: string) {
  return ROLES.find(([role]) => role === code)?.[1] ?? code;
}

export function RegistrationSummaryView({
  summary,
  options,
  onEdit,
}: {
  summary: RegistrationSummary;
  options?: RegistrationOptions | undefined;
  onEdit?: ((id: string) => void) | undefined;
}) {
  const { company, store } = summary;
  const people = [
    { ...summary.owner, role: "Owner", chip: "chip-navy" },
    { ...summary.admin, role: "Admin", chip: "chip-blue" },
    ...summary.proposed_team.map((p) => ({
      ...p,
      role: `${roleLabel(p.role_code)} (proposed)`,
      chip: "",
    })),
  ];
  const policy = summary.initial_access?.policy_baseline;
  return (
    <div className="signup-summary" data-testid="signup-summary">
      <SummaryGroup id="company" title="Company" onEdit={onEdit}>
        <Values
          rows={[
            ["Company name", company.name],
            ["Company code", <code key="code">{company.code}</code>],
            ["Registered legal name", company.legal_name, "signup-values-wide"],
            ["PAN", <code key="pan">{company.pan}</code>],
            ["GSTIN", <code key="gstin">{company.gstin}</code>],
            ["State", company.state_name],
            ["Billing address", company.billing_address, "signup-values-wide"],
          ]}
        />
      </SummaryGroup>
      <SummaryGroup id="regional" title="Regional settings">
        <Values rows={regionalLabels(company, options).map(([k, v]) => [k, v])} />
      </SummaryGroup>
      <SummaryGroup id="store" title="First store" onEdit={onEdit}>
        <Values
          rows={[
            ["Store name", String(store.name ?? "")],
            ["Store code", <code key="code">{store.code}</code>],
            ["City", String(store.city ?? "")],
            [
              "Starting as",
              store.setup_kind === "existing"
                ? `Existing store — from ${store.source_system}`
                : store.setup_kind === "new"
                  ? "New store"
                  : "",
            ],
            ["Store address", String(store.address ?? ""), "signup-values-wide"],
          ]}
        />
      </SummaryGroup>
      <SummaryGroup id="people" title="People" onEdit={onEdit}>
        <ul className="signup-people-list">
          {people.map((p) => (
            <li key={`${p.role}-${p.email}`}>
              <span className="signup-people-name">
                <strong>{p.name}</strong>
                <span className={`chip ${p.chip}`}>{p.role}</span>
              </span>
              <span className="signup-people-email">{p.email}</span>
              <code>{p.staff_code}</code>
            </li>
          ))}
        </ul>
        {summary.proposed_team.length === 0 && (
          <p className="signup-muted">No other people proposed yet.</p>
        )}
        <p className="signup-muted">
          Team members are proposed only. Their login and store access are set up after
          registration.
        </p>
      </SummaryGroup>
      <details
        className="signup-policy"
        data-testid={policy ? "signup-policy-baseline" : undefined}
      >
        <summary>Access and permissions detail</summary>
        <p>
          Owner has authority across the company’s sites and brands. Admin runs company
          administration; protected business fields are not granted. After registration, Owner and
          Admin finish company configuration, team access and store readiness. Stock setup and
          selling approval follow separately.
        </p>
        {policy && (
          <>
            <p>
              Only the initial Owner and Admin receive assignments now. The role policies below are
              available for later independently reviewed, scoped assignments. Changing this baseline
              before registration requires both people to confirm a new revision.
            </p>
            {Object.entries(policy.roles).map(([code, rolePolicy]) => (
              <div key={code}>
                <h4>{roleLabel(code)}</h4>
                <p>
                  Protected fields:{" "}
                  {rolePolicy.field_access.length ? rolePolicy.field_access.join(", ") : "None"}.
                </p>
                <p>
                  Responsibilities:{" "}
                  {rolePolicy.step_actions.length ? rolePolicy.step_actions.join(", ") : "None"}.
                </p>
                <div className="signup-table-scroll">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Section</th>
                        <th>Highest permitted level</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(rolePolicy.section_access).map(([section, access]) => (
                        <tr key={section}>
                          <td>{section.replaceAll("_", " ")}</td>
                          <td>
                            {access.capability} · {access.label}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
            <h4>Workflow action requirements</h4>
            <div className="signup-table-scroll">
              <table className="data">
                <thead>
                  <tr>
                    <th>Action</th>
                    <th>Section</th>
                    <th>Minimum level</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(policy.action_levels).map(([action, level]) => (
                    <tr key={action}>
                      <td>{action}</td>
                      <td>{level.section.replaceAll("_", " ")}</td>
                      <td>{level.minimum}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </details>
    </div>
  );
}

export function Signup() {
  const [initialDraft] = useState(readDraft);
  const [state, setState] = useState<RegistrationState | null>(null);
  const [company, setCompany] = useState({
    code: "",
    name: "",
    legal_name: "",
    pan: "",
    gstin: "",
    state_code: "",
    state_name: "",
    billing_address: "",
    country: "IN",
    timezone: "Asia/Kolkata",
    currency: "INR",
    locale: "en-IN",
    ...initialDraft?.company,
  });
  const [store, setStore] = useState({
    code: "",
    name: "",
    city: "",
    address: "",
    setup_kind: "",
    source_system: "",
    counter_count: 1,
    ...initialDraft?.store,
  });
  const [owner, setOwner] = useState(() => ({ ...blankPerson(), ...initialDraft?.owner }));
  const [admin, setAdmin] = useState(() => ({ ...blankPerson(), ...initialDraft?.admin }));
  const [team, setTeam] = useState<Proposed[]>(initialDraft?.proposed_team ?? []);
  const [commandId, setCommandId] = useState(() => crypto.randomUUID());
  const [revisionMode, setRevisionMode] = useState(false);
  const [currentOwnerPassword, setCurrentOwnerPassword] = useState("");
  const [pending, setPending] = useState<RegistrationResult | null>(null);
  const [identity, setIdentity] = useState({ email: "", temporary_password: "" });
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [retry, setRetry] = useState(0);
  const [reviewing, setReviewing] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [customCity, setCustomCity] = useState(false);
  const [activeSection, setActiveSection] = useState(0);
  const [detailsMissing, setDetailsMissing] = useState(false);
  const confirming = !revisionMode && Boolean(state?.pending_confirmation || pending);
  const activeStep = confirming ? 5 : reviewing ? 4 : activeSection;
  useEffect(() => {
    try {
      if (state?.available === false || pending?.state === "registered")
        sessionStorage.removeItem(DRAFT_KEY);
      else
        sessionStorage.setItem(
          DRAFT_KEY,
          JSON.stringify(draftDetails({ company, store, owner, admin, proposed_team: team })),
        );
    } catch {
      // Storage may be unavailable; the current page still keeps its draft.
    }
  }, [company, store, owner, admin, team, state?.available, pending?.state]);
  useEffect(() => {
    if (reviewing || confirming || !state?.available) return;
    const update = () => {
      const index = SETUP_STEPS.slice(0, 4).findIndex((step) => {
        const section = document.getElementById(`signup-section-${step.id}`);
        return section !== null && section.getBoundingClientRect().bottom > 180;
      });
      if (index >= 0) setActiveSection(index);
    };
    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [reviewing, confirming, state?.available]);
  const staffCodes = previewStaffCodes([owner, admin, ...team]);
  const filled = (...values: (string | number)[]) => values.every((v) => String(v).trim());
  const sectionComplete = [
    filled(
      company.name,
      company.legal_name,
      company.state_code,
      company.pan,
      company.gstin,
      company.billing_address,
    ),
    filled(company.country, company.timezone, company.currency, company.locale),
    filled(store.name, store.city, store.address, store.setup_kind) &&
      (store.setup_kind !== "existing" || filled(store.source_system)),
    filled(
      owner.name,
      owner.email,
      owner.temporary_password,
      admin.name,
      admin.email,
      admin.temporary_password,
    ) && team.every((p) => filled(p.name, p.email)),
    confirming,
    false,
  ];
  const passwordHint = revisionMode
    ? "New temporary password required. Different for each person."
    : "Different for each person. Changed at first sign-in.";
  const options = state?.options;
  useEffect(() => {
    if (options && store.city)
      setCustomCity(!options.states.flatMap((s) => s.cities).includes(store.city));
  }, [options, store.city]);
  function editSection(id: string) {
    setReviewing(false);
    requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView({ block: "start" }));
  }
  function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors: Record<string, string> = {};
    for (const element of Array.from(event.currentTarget.elements)) {
      if (
        element instanceof HTMLInputElement ||
        element instanceof HTMLSelectElement ||
        element instanceof HTMLTextAreaElement
      ) {
        if (!element.validity.valid)
          errors[element.id.replace("signup-", "")] = element.validationMessage;
      }
    }
    if (!/^[A-Z]{5}[0-9]{4}[A-Z]$/.test(company.pan))
      errors.pan = "Enter a valid PAN: five letters, four digits and one letter.";
    if (!/^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$/.test(company.gstin))
      errors.gstin = "Enter a valid 15-character GSTIN.";
    else if (
      company.gstin.slice(0, 2) !== company.state_code ||
      company.gstin.slice(2, 12) !== company.pan
    )
      errors.gstin = "GSTIN must match the selected state and PAN.";
    if (!store.setup_kind) errors["store-kind"] = "Choose how this store is starting.";
    const emails = new Set<string>();
    [owner, admin, ...team].forEach((p, i) => {
      const key = i === 0 ? "owner" : i === 1 ? "admin" : `team-${i - 2}`;
      if (emails.has(p.email.trim().toLowerCase()))
        errors[`${key}-email`] = "Each person needs a separate email.";
      emails.add(p.email.trim().toLowerCase());
      if (p.staff_code && !/^[-a-zA-Z0-9_]+$/.test(p.staff_code))
        errors[`${key}-staff-code`] = "Use letters, numbers, hyphens or underscores.";
      if (staffCodes.findIndex((c) => c.toLowerCase() === staffCodes[i]!.toLowerCase()) !== i)
        errors[`${key}-staff-code`] = "Each person needs a separate staff code.";
    });
    if (owner.temporary_password && owner.temporary_password === admin.temporary_password)
      errors["admin-password"] = "Owner and Admin need different temporary passwords.";
    setFieldErrors(errors);
    setError("");
    if (Object.keys(errors).length)
      requestAnimationFrame(() =>
        document.getElementById(`signup-${Object.keys(errors)[0]}`)?.focus(),
      );
    else {
      setReviewing(true);
      requestAnimationFrame(() => document.getElementById("signup-review")?.focus());
    }
  }
  useEffect(() => {
    let live = true;
    Promise.all([api.get<RegistrationState>("/auth/registration"), api.get("/auth/csrf")])
      .then(([{ data }]) => {
        if (live) {
          setState(data);
          setError("");
          setFieldErrors({});
        }
      })
      .catch((e: unknown) => {
        if (live) setError(apiErrorMessage(e));
      });
    return () => {
      live = false;
    };
  }, [retry]);

  async function stage() {
    if (revisionMode && !currentOwnerPassword) {
      setError("Enter the saved Owner password to save changes.");
      requestAnimationFrame(() =>
        document.getElementById("signup-revision-owner-password")?.focus(),
      );
      return;
    }
    setError("");
    setBusy(true);
    try {
      const autoCode = <T extends { code: string }>(value: T) => {
        const { code, ...rest } = value;
        return { ...rest, ...(code ? { code } : {}) };
      };
      const autoStaff = <T extends { staff_code: string }>(value: T) => {
        const { staff_code, ...rest } = value;
        return { ...rest, ...(staff_code ? { staff_code } : {}) };
      };
      const body = {
        command_id: commandId,
        company: autoCode(company),
        store: autoCode({
          ...store,
          source_system: store.setup_kind === "existing" ? store.source_system : "",
        }),
        owner: autoStaff(owner),
        admin: autoStaff(admin),
        proposed_team: team.map(autoStaff),
      };
      const { data } = revisionMode
        ? await api.patch<RegistrationResult>("/auth/registration", {
            ...body,
            current_owner_password: currentOwnerPassword,
          })
        : await api.post<RegistrationResult>("/auth/registration", body);
      setPending(data);
      if (data.summary) restoreDetails(data.summary);
      setState((s) => (s ? { ...s, pending_confirmation: true } : s));
      setRevisionMode(false);
      setReviewing(false);
      setCurrentOwnerPassword("");
      setIdentity({ email: "", temporary_password: "" });
      setNotice("Setup saved. Owner and Admin must each review and confirm.");
      setOwner((p) => ({ ...p, temporary_password: "" }));
      setAdmin((p) => ({ ...p, temporary_password: "" }));
    } catch (e) {
      const errors = registrationErrors(e);
      setError(Object.keys(errors).length ? "" : apiErrorMessage(e));
      setFieldErrors(errors);
      if (Object.keys(errors).length) {
        setReviewing(false);
        requestAnimationFrame(() =>
          document.getElementById(`signup-${Object.keys(errors)[0]}`)?.focus(),
        );
      }
    } finally {
      setBusy(false);
    }
  }
  async function inspect(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setAcknowledged(false);
    try {
      const { data } = await api.post<RegistrationResult>("/auth/registration/confirm", identity);
      setPending(data);
      setNotice("");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function confirm() {
    if (!pending?.summary_hash || !acknowledged) return;
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post<RegistrationResult>("/auth/registration/confirm", {
        ...identity,
        summary_hash: pending.summary_hash,
        acknowledged: true,
      });
      setPending(data);
      setIdentity({ email: "", temporary_password: "" });
      setAcknowledged(false);
      setNotice(
        data.state === "registered"
          ? "Company registered. Both initial people must sign in and replace their temporary password before business work."
          : `Your confirmation is recorded. ${data.summary?.[data.confirmed?.owner ? "admin" : "owner"]?.name ?? "The remaining person"} (${data.confirmed?.owner ? "Admin" : "Owner"}) must review and confirm here using their own credentials.`,
      );
      if (data.state === "registered")
        setState((s) =>
          s ? { ...s, available: false, pending_confirmation: false, message: CLOSED } : s,
        );
    } catch (e) {
      setError(apiErrorMessage(e));
      // After a lost response, status determines whether registration committed.
      try {
        const { data } = await api.get<RegistrationState>("/auth/registration");
        setState(data);
      } catch {
        /* Original typed error remains visible. */
      }
    } finally {
      setBusy(false);
    }
  }
  function restoreDetails(summary: SignupDraft) {
    setCompany((current) => ({ ...current, ...summary.company }));
    setStore({
      code: String(summary.store.code),
      name: String(summary.store.name),
      city: String(summary.store.city),
      address: String(summary.store.address),
      setup_kind: String(summary.store.setup_kind),
      source_system: String(summary.store.source_system),
      counter_count: 1,
    });
    setOwner({ ...summary.owner, temporary_password: "" });
    setAdmin({ ...summary.admin, temporary_password: "" });
    setTeam(summary.proposed_team.map((person) => ({ ...person })));
    setCustomCity(!options?.states.flatMap((s) => s.cities).includes(String(summary.store.city)));
  }
  function beginRevision() {
    setDetailsMissing(!company.name && !pending?.summary);
    if (!company.name && pending?.summary) restoreDetails(pending.summary);
    else {
      setOwner((p) => ({ ...p, temporary_password: "" }));
      setAdmin((p) => ({ ...p, temporary_password: "" }));
    }
    setCommandId(crypto.randomUUID());
    setReviewing(false);
    setRevisionMode(true);
    setCurrentOwnerPassword(
      pending?.confirming_role === "owner" ? identity.temporary_password : "",
    );
    setIdentity({ email: "", temporary_password: "" });
    setFieldErrors({});
    setError("");
    setNotice("");
    requestAnimationFrame(() =>
      document.getElementById("signup-section-company")?.scrollIntoView(),
    );
  }

  const showChecklist =
    state?.available &&
    !confirming &&
    !reviewing &&
    !revisionMode &&
    !sectionComplete.slice(0, 4).every(Boolean);
  const supportedStates = (options?.states ?? []).map((s) => s.label);
  const cities = options?.states.find((s) => s.value === company.state_code)?.cities ?? [];

  return (
    <main className="signup">
      <FieldErrors.Provider value={fieldErrors}>
        <header className="signup-header">
          <KdpsLogo height={38} />
          <Link to="/login">Sign in</Link>
        </header>
        <h1>Set up your company and first store</h1>
        <p className="signup-intro">Add your company, first store and initial team.</p>
        {showChecklist && (
          <aside className="signup-checklist" aria-labelledby="signup-checklist-title">
            <strong id="signup-checklist-title">Before you start, keep these ready</strong>
            <ul>
              <li>Company PAN and GSTIN</li>
              <li>Billing and store addresses</li>
              <li>Two people — Owner and Admin — with separate emails</li>
            </ul>
          </aside>
        )}
        {(state?.available || pending?.state === "registered") && (
          <nav className="signup-progress" aria-label="Setup progress">
            <p className="signup-progress-title">
              <strong>
                {pending?.state === "registered"
                  ? "Setup registered"
                  : `Step ${activeStep + 1} of ${SETUP_STEPS.length}`}
              </strong>
              <span>
                {pending?.state === "registered"
                  ? "Continue with store setup after signing in"
                  : SETUP_STEPS[activeStep]?.label}
              </span>
            </p>
            <ol>
              {SETUP_STEPS.map((step, index) => {
                const done =
                  pending?.state === "registered" ||
                  (index !== activeStep && sectionComplete[index]);
                return (
                  <li key={step.id}>
                    <button
                      type="button"
                      aria-current={index === activeStep ? "step" : undefined}
                      data-complete={done || undefined}
                      disabled={confirming || (index >= 4 && index !== activeStep)}
                      onClick={() => {
                        if (index < 4) {
                          setActiveSection(index);
                          editSection(`signup-section-${step.id}`);
                        }
                      }}
                    >
                      <span className="signup-step-number" aria-hidden="true">
                        {done ? "✓" : index + 1}
                      </span>
                      <span className="signup-step-label">{step.label}</span>
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>
        )}
        <RegistrationFeedback
          errors={fieldErrors}
          error={error}
          onSelect={(name) => {
            setReviewing(false);
            requestAnimationFrame(() => document.getElementById(`signup-${name}`)?.focus());
          }}
        />
        {notice && (
          <p role="status" className="ok-note">
            {notice}
          </p>
        )}
        {!state ? (
          <>
            <p>Checking this installation…</p>
            {error && (
              <button className="btn" onClick={() => setRetry(retry + 1)}>
                Retry
              </button>
            )}
          </>
        ) : pending?.state === "registered" ? (
          <section className="card signup-section">
            <h2>Company registered</h2>
            <p>Sign in and change your temporary password to continue store setup.</p>
            <Link className="btn btn-cta" to="/login">
              Sign in to continue setup
            </Link>
          </section>
        ) : !state.available ? (
          <section className="card signup-section" data-testid="signup-closed">
            <h2>Company signup is closed</h2>
            <p>{CLOSED}</p>
            <Link className="btn btn-cta" to="/login">
              Sign in
            </Link>
          </section>
        ) : !revisionMode && (state.pending_confirmation || pending) ? (
          <section className="card signup-section">
            <div className="signup-confirm-header">
              <h2>Confirm company setup</h2>
              <button type="button" className="btn" disabled={busy} onClick={beginRevision}>
                Back to setup
              </button>
            </div>
            <p className="signup-section-lead">
              The setup is saved. Owner and Admin each confirm it separately.
            </p>
            {pending?.confirmed && pending.summary && (
              <div className="signup-confirmation-status">
                {(["owner", "admin"] as const).map((role) => (
                  <div className="signup-person-status" key={role}>
                    <div>
                      <strong>
                        {role === "owner" ? "Owner" : "Admin"}: {pending.summary?.[role].name}
                      </strong>
                      <p>{pending.summary?.[role].email}</p>
                    </div>
                    <span
                      className={`chip ${pending.confirmed?.[role] ? "chip-green" : "chip-amber"}`}
                    >
                      {pending.confirmed?.[role] ? "Confirmed" : "Awaiting confirmation"}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <form
              onSubmit={inspect}
              className="signup-credentials"
              data-testid="signup-confirm-credentials"
              aria-labelledby="signup-credentials-title"
            >
              <h3 id="signup-credentials-title">Confirm as Owner or Admin</h3>
              <p className="signup-muted">
                Use your own email and the temporary password set for you.
              </p>
              <div className="signup-grid">
                <Input
                  label="Your individual email"
                  type="email"
                  name="confirm-email"
                  value={identity.email}
                  onChange={(email) => {
                    setIdentity({ ...identity, email });
                    setAcknowledged(false);
                  }}
                />
                <Input
                  label="Your temporary password"
                  type="password"
                  name="confirm-password"
                  value={identity.temporary_password}
                  onChange={(temporary_password) => {
                    setIdentity({ ...identity, temporary_password });
                    setAcknowledged(false);
                  }}
                />
              </div>
              <button className="btn btn-primary" disabled={busy}>
                Review my initial summary
              </button>
            </form>
            {pending?.summary && (
              <>
                <h3 className="signup-summary-title">Saved setup</h3>
                <RegistrationSummaryView summary={pending.summary} options={options} />
              </>
            )}
            {!pending?.summary && company.name && (
              <>
                <h3 className="signup-summary-title">Your entered details</h3>
                <RegistrationSummaryView
                  summary={draftDetails({ company, store, owner, admin, proposed_team: team })}
                  options={options}
                />
              </>
            )}
            {pending?.confirming_role && identity.temporary_password && (
              <div className="signup-acknowledge">
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    onChange={(e) => setAcknowledged(e.target.checked)}
                    data-testid="signup-acknowledge"
                  />
                  I am the named {pending.confirming_role === "owner" ? "Owner" : "Admin"}, a
                  different person from the other initial signatory. I personally confirm this
                  company, store, people and exact access summary.
                </label>
                <button
                  className="btn btn-cta"
                  onClick={() => void confirm()}
                  disabled={busy || !acknowledged}
                  data-testid="signup-confirm"
                >
                  Confirm as {pending.confirming_role === "owner" ? "Owner" : "Admin"}
                </button>
              </div>
            )}
          </section>
        ) : (
          <>
            {reviewing ? (
              <section className="card signup-section" id="signup-review" tabIndex={-1}>
                <h2>Review setup</h2>
                <p className="signup-section-lead">
                  Check each section. After saving, Owner and Admin each confirm this setup.
                </p>
                <RegistrationSummaryView
                  options={options}
                  onEdit={(id) => editSection(`signup-section-${id}`)}
                  summary={{
                    company: {
                      ...company,
                      code: company.code || options?.code_defaults.company || "CMP-0001",
                    },
                    store: {
                      ...store,
                      code: store.code || options?.code_defaults.store || "STR-0001",
                    },
                    owner: { name: owner.name, email: owner.email, staff_code: staffCodes[0]! },
                    admin: { name: admin.name, email: admin.email, staff_code: staffCodes[1]! },
                    proposed_team: team.map((p, i) => ({ ...p, staff_code: staffCodes[i + 2]! })),
                  }}
                />
                <div className="signup-save">
                  {revisionMode && (
                    <>
                      <p className="signup-callout">
                        Saving replaces the saved setup and clears both confirmations. Owner and
                        Admin confirm again.
                      </p>
                      <Input
                        label="Saved Owner password"
                        type="password"
                        name="revision-owner-password"
                        value={currentOwnerPassword}
                        onChange={setCurrentOwnerPassword}
                      />
                    </>
                  )}
                  <button
                    type="button"
                    className="btn btn-cta"
                    disabled={busy}
                    onClick={() => void stage()}
                  >
                    {busy ? "Saving setup…" : "Save and continue to confirmation"}
                  </button>
                </div>
              </section>
            ) : (
              <form
                onSubmit={review}
                onChange={(event) => {
                  const target = event.target;
                  if (!(target instanceof HTMLElement) || !target.id.startsWith("signup-")) return;
                  const key = target.id.slice(7);
                  setFieldErrors((previous) => {
                    const next = { ...previous };
                    delete next[key];
                    return next;
                  });
                  setError("");
                }}
                noValidate
                data-testid="signup-form"
              >
                {revisionMode && (
                  <section className="card signup-section signup-edit-banner">
                    <div>
                      <h2>Edit setup</h2>
                      <p>Editing the saved setup. Nothing changes until you save.</p>
                      {detailsMissing && (
                        <p className="signup-callout">
                          This browser doesn’t have the saved details. Go back to confirmation,
                          review as Owner, then return here to edit with the saved details filled
                          in.
                        </p>
                      )}
                    </div>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => {
                        setRevisionMode(false);
                        setDetailsMissing(false);
                        setCurrentOwnerPassword("");
                        setOwner((p) => ({ ...p, temporary_password: "" }));
                        setAdmin((p) => ({ ...p, temporary_password: "" }));
                      }}
                    >
                      Back to confirmation
                    </button>
                  </section>
                )}
                <section className="card signup-section" id="signup-section-company">
                  <h2>Company</h2>
                  <div className="signup-grid">
                    <Input
                      label="Company name"
                      name="name"
                      value={company.name}
                      onChange={(name) => setCompany({ ...company, name })}
                    />
                    <Select
                      label="State"
                      name="state"
                      value={company.state_code}
                      placeholder="Choose state"
                      options={options?.states ?? []}
                      hint={
                        supportedStates.length
                          ? `This release supports ${supportedStates.join(" and ")}.`
                          : undefined
                      }
                      onChange={(state_code) => {
                        setCompany({
                          ...company,
                          state_code,
                          state_name:
                            options?.states.find((s) => s.value === state_code)?.label || "",
                        });
                        setStore({ ...store, city: "" });
                        setCustomCity(false);
                      }}
                    />
                    <Input
                      label="Registered legal name"
                      name="legal_name"
                      wide
                      value={company.legal_name}
                      onChange={(legal_name) => setCompany({ ...company, legal_name })}
                    />
                    <Input
                      label="PAN"
                      name="pan"
                      value={company.pan}
                      hint="10 characters, for example ABCDE1234F."
                      onChange={(pan) => setCompany({ ...company, pan: pan.toUpperCase() })}
                    />
                    <Input
                      label="GSTIN"
                      name="gstin"
                      value={company.gstin}
                      hint="15 characters: the state code, then the PAN."
                      onChange={(gstin) => setCompany({ ...company, gstin: gstin.toUpperCase() })}
                    />
                    <Input
                      label="Company billing address"
                      name="billing_address"
                      value={company.billing_address}
                      multiline
                      onChange={(billing_address) => setCompany({ ...company, billing_address })}
                    />
                    <CodeField
                      label="Company code"
                      name="code"
                      value={company.code}
                      preview={options?.code_defaults.company || "CMP-0001"}
                      onChange={(code) => setCompany({ ...company, code })}
                    />
                  </div>
                </section>
                <section className="card signup-section" id="signup-section-regional">
                  <h2>Regional settings</h2>
                  <p className="signup-section-lead">Only option in this release.</p>
                  <dl className="signup-values signup-regional">
                    {regionalLabels(company, options).map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{value}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
                <section className="card signup-section" id="signup-section-store">
                  <h2>First store</h2>
                  <div className="signup-grid">
                    <Input
                      label="Store name"
                      name="store-name"
                      value={store.name}
                      onChange={(name) => setStore({ ...store, name })}
                    />
                    <Select
                      label="City"
                      name="store-city-choice"
                      value={customCity ? "__custom" : store.city}
                      placeholder={company.state_code ? "Choose city" : "Choose a state first"}
                      disabled={!company.state_code}
                      hint={company.state_code ? undefined : "Choose the company state first."}
                      options={[
                        ...cities.map((c) => ({ value: c, label: c })),
                        ...(company.state_code
                          ? [{ value: "__custom", label: "Enter another city" }]
                          : []),
                      ]}
                      onChange={(city) => {
                        setCustomCity(city === "__custom");
                        setStore({ ...store, city: city === "__custom" ? "" : city });
                      }}
                    />
                    {customCity && (
                      <Input
                        label="City name"
                        name="store-city"
                        value={store.city}
                        onChange={(city) => setStore({ ...store, city })}
                      />
                    )}
                    <Input
                      label="Store address"
                      name="store-address"
                      multiline
                      value={store.address}
                      onChange={(address) => setStore({ ...store, address })}
                    />
                  </div>
                  <fieldset
                    className="signup-start"
                    aria-describedby={fieldErrors["store-kind"] ? "error-store-kind" : undefined}
                  >
                    <legend>How is this store starting?</legend>
                    <div className="signup-grid">
                      {[
                        [
                          "new",
                          "New store",
                          "No stock yet.",
                          "Next: receive stock into the store after sign-in.",
                        ],
                        [
                          "existing",
                          "Existing store",
                          "Already trading on other software.",
                          "Next: import opening stock (SOH) from that software after sign-in.",
                        ],
                      ].map(([value, title, detail, next], i) => (
                        <label
                          className={`signup-choice ${store.setup_kind === value ? "selected" : ""}`}
                          key={value}
                        >
                          <input
                            type="radio"
                            name="store-start"
                            id={i === 0 ? "signup-store-kind" : "signup-store-kind-existing"}
                            required
                            checked={store.setup_kind === value}
                            onChange={() => setStore({ ...store, setup_kind: value! })}
                          />
                          <span>
                            <strong>{title}</strong>
                            <span>{detail}</span>
                            <span className="signup-choice-next">{next}</span>
                          </span>
                        </label>
                      ))}
                    </div>
                    {fieldErrors["store-kind"] && (
                      <p className="signup-field-error" id="error-store-kind">
                        {fieldErrors["store-kind"]}
                      </p>
                    )}
                  </fieldset>
                  {store.setup_kind === "existing" && (
                    <div className="signup-grid">
                      <Input
                        label="Current software name"
                        name="source-system"
                        value={store.source_system}
                        onChange={(source_system) => setStore({ ...store, source_system })}
                      />
                    </div>
                  )}
                  <div className="signup-grid">
                    <CodeField
                      label="Store code"
                      name="store-code"
                      value={store.code}
                      preview={options?.code_defaults.store || "STR-0001"}
                      onChange={(code) => setStore({ ...store, code })}
                    />
                  </div>
                </section>
                <section className="card signup-section" id="signup-section-people">
                  <h2>People</h2>
                  <p className="signup-section-lead">
                    Two different people. Each confirms separately on the next screen with their own
                    email and temporary password.
                  </p>
                  <div className="signup-people">
                    {(
                      [
                        [
                          "owner",
                          "Owner",
                          "chip-navy",
                          "Full authority across the company’s stores and brands.",
                          owner,
                          setOwner,
                          0,
                        ],
                        [
                          "admin",
                          "Admin",
                          "chip-blue",
                          "Company administration. No protected business fields.",
                          admin,
                          setAdmin,
                          1,
                        ],
                      ] as const
                    ).map(([who, title, chip, detail, value, set, index]) => (
                      <div className="signup-person" key={who}>
                        <h3>
                          <span className={`chip ${chip}`}>{title}</span>
                        </h3>
                        <p className="signup-muted">{detail}</p>
                        <PersonFields
                          who={who}
                          value={value}
                          preview={staffCodes[index]!}
                          passwordHint={passwordHint}
                          onChange={set}
                        />
                      </div>
                    ))}
                  </div>
                  <div className="signup-team">
                    <h3>
                      Manager or team members <span className="caption">(optional)</span>
                    </h3>
                    <p className="signup-muted">
                      Their login and store access are set up after registration.
                    </p>
                    {team.map((person, index) => (
                      <div className="signup-team-row" key={index}>
                        <div className="signup-team-head">
                          <h4>Team member {index + 1}</h4>
                          <button
                            type="button"
                            className="btn"
                            aria-label={`Remove team member ${index + 1}`}
                            onClick={() => setTeam(team.filter((_, i) => i !== index))}
                          >
                            Remove
                          </button>
                        </div>
                        <div className="signup-grid">
                          <Input
                            label="Full name"
                            name={`team-${index}-name`}
                            value={person.name}
                            onChange={(name) =>
                              setTeam(team.map((p, i) => (i === index ? { ...p, name } : p)))
                            }
                          />
                          <Input
                            label="Email"
                            type="email"
                            name={`team-${index}-email`}
                            value={person.email}
                            onChange={(email) =>
                              setTeam(team.map((p, i) => (i === index ? { ...p, email } : p)))
                            }
                          />
                          <Select
                            label="Proposed role"
                            name={`team-${index}-role`}
                            value={person.role_code}
                            options={ROLES.map(([value, label]) => ({ value, label }))}
                            onChange={(role_code) =>
                              setTeam(team.map((p, i) => (i === index ? { ...p, role_code } : p)))
                            }
                          />
                          <CodeField
                            label="Staff code"
                            name={`team-${index}-staff-code`}
                            value={person.staff_code}
                            preview={staffCodes[index + 2]!}
                            onChange={(staff_code) =>
                              setTeam(team.map((p, i) => (i === index ? { ...p, staff_code } : p)))
                            }
                          />
                        </div>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="btn"
                      disabled={team.length >= 50}
                      onClick={() =>
                        setTeam([
                          ...team,
                          { name: "", email: "", staff_code: "", role_code: "store_person" },
                        ])
                      }
                    >
                      Add team member
                    </button>
                  </div>
                </section>
                <div className="signup-submit">
                  <button
                    className="btn btn-cta"
                    disabled={busy || !options}
                    data-testid="signup-submit"
                  >
                    Review setup
                  </button>
                </div>
              </form>
            )}
          </>
        )}
      </FieldErrors.Provider>
    </main>
  );
}
