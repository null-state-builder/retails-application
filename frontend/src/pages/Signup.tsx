import { useEffect, useState } from "react";
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
const COMPANY_LABELS: Record<string, string> = {
  code: "Company code",
  name: "Company name",
  legal_name: "Registered legal name",
  pan: "PAN",
  gstin: "GSTIN",
  state_code: "GST state code",
  state_name: "State name",
  billing_address: "Company billing address",
  country: "Country code",
  timezone: "Timezone",
  currency: "Currency",
  locale: "Language and number format",
};
const blankPerson = (): Person => ({ name: "", email: "", staff_code: "", temporary_password: "" });

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

function Input({
  label,
  value,
  onChange,
  name,
  type = "text",
  required = true,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  name: string;
  type?: string;
  required?: boolean;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        className="input"
        type={type}
        autoComplete={type === "password" ? "new-password" : "off"}
        value={value}
        required={required}
        onChange={(e) => onChange(e.target.value)}
        data-testid={`signup-${name}`}
      />
    </label>
  );
}

function PersonFields({
  who,
  value,
  onChange,
}: {
  who: string;
  value: Person;
  onChange: (p: Person) => void;
}) {
  return (
    <div className="signup-grid">
      <Input
        label="Full name"
        name={`${who}-name`}
        value={value.name}
        onChange={(name) => onChange({ ...value, name })}
      />
      <Input
        label="Individual email"
        type="email"
        name={`${who}-email`}
        value={value.email}
        onChange={(email) => onChange({ ...value, email })}
      />
      <Input
        label="Staff code"
        name={`${who}-staff-code`}
        value={value.staff_code}
        onChange={(staff_code) => onChange({ ...value, staff_code })}
      />
      <Input
        label="Separate temporary password"
        type="password"
        name={`${who}-password`}
        value={value.temporary_password}
        onChange={(temporary_password) => onChange({ ...value, temporary_password })}
      />
    </div>
  );
}

export function RegistrationSummaryView({ summary }: { summary: RegistrationSummary }) {
  return (
    <div className="signup-summary" data-testid="signup-summary">
      <h3>
        {summary.company.name} ({summary.company.code})
      </h3>
      <dl className="signup-grid">
        <div>
          <dt>Legal entity</dt>
          <dd>
            {summary.company.legal_name} · {summary.company.pan}
          </dd>
        </div>
        <div>
          <dt>Registration</dt>
          <dd>
            {summary.company.gstin} · {summary.company.state_name} ({summary.company.state_code})
          </dd>
        </div>
        <div>
          <dt>Billing address</dt>
          <dd>{summary.company.billing_address}</dd>
        </div>
        <div>
          <dt>Defaults</dt>
          <dd>
            {summary.company.country} · {summary.company.currency} · {summary.company.timezone} ·{" "}
            {summary.company.locale}
          </dd>
        </div>
        <div>
          <dt>First store</dt>
          <dd>
            {summary.store.name} ({summary.store.code}) · {summary.store.city}
            <br />
            {summary.store.address}
          </dd>
        </div>
        <div>
          <dt>Setup path</dt>
          <dd>
            {summary.store.setup_kind === "existing"
              ? `Existing store switching from ${summary.store.source_system}`
              : "New store"}{" "}
            · one online counter
          </dd>
        </div>
        <div>
          <dt>Owner</dt>
          <dd>
            {summary.owner.name} · {summary.owner.email} · {summary.owner.staff_code}
            <br />
            Owner authority across the company’s sites and brands.
          </dd>
        </div>
        <div>
          <dt>Separate Admin</dt>
          <dd>
            {summary.admin.name} · {summary.admin.email} · {summary.admin.staff_code}
            <br />
            Administration across the company; protected business fields are not granted.
          </dd>
        </div>
      </dl>
      <h4>Proposed team</h4>
      {summary.proposed_team.length ? (
        <ul>
          {summary.proposed_team.map((p) => (
            <li key={p.email}>
              {p.name} · {p.email} · {p.staff_code} ·{" "}
              {ROLES.find(([code]) => code === p.role_code)?.[1] ?? p.role_code}
            </li>
          ))}
        </ul>
      ) : (
        <p>No other people proposed yet.</p>
      )}
      <p>
        These people receive no login or access from signup. Owner activates their scoped
        assignments through normal administration, with Admin’s independent review.
      </p>
      <p>
        Stock and selling remain inactive. Admin’s separate scoped Warehouse preparation assignment,
        business configuration, opening reconciliation and trading approval follow company
        registration.
      </p>
      {summary.initial_access?.policy_baseline && (
        <details className="signup-policy" data-testid="signup-policy-baseline">
          <summary>Review the exact initial role permissions and workflow responsibilities</summary>
          <p>
            Only the initial Owner and Admin receive assignments now. The six role policies below
            are available for later independently reviewed, scoped assignments. Changing this
            baseline before registration requires both people to confirm a new revision.
          </p>
          {Object.entries(summary.initial_access.policy_baseline.roles).map(([code, policy]) => (
            <div key={code}>
              <h4>{ROLES.find(([role]) => role === code)?.[1] ?? code}</h4>
              <p>
                Protected fields:{" "}
                {policy.field_access.length ? policy.field_access.join(", ") : "None"}.
              </p>
              <p>
                Responsibilities:{" "}
                {policy.step_actions.length ? policy.step_actions.join(", ") : "None"}.
              </p>
              <table className="data">
                <thead>
                  <tr>
                    <th>Section</th>
                    <th>Highest permitted level</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(policy.section_access).map(([section, access]) => (
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
          ))}
          <h4>Workflow action requirements</h4>
          <table className="data">
            <thead>
              <tr>
                <th>Action</th>
                <th>Section</th>
                <th>Minimum level</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(summary.initial_access.policy_baseline.action_levels).map(
                ([action, policy]) => (
                  <tr key={action}>
                    <td>{action}</td>
                    <td>{policy.section.replaceAll("_", " ")}</td>
                    <td>{policy.minimum}</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

export function Signup() {
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
  });
  const [store, setStore] = useState({
    code: "",
    name: "",
    city: "",
    address: "",
    setup_kind: "existing",
    source_system: "",
    counter_count: 1,
  });
  const [owner, setOwner] = useState(blankPerson);
  const [admin, setAdmin] = useState(blankPerson);
  const [team, setTeam] = useState<Proposed[]>([]);
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
  useEffect(() => {
    let live = true;
    Promise.all([api.get<RegistrationState>("/auth/registration"), api.get("/auth/csrf")])
      .then(([{ data }]) => {
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

  async function stage(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const body = { command_id: commandId, company, store, owner, admin, proposed_team: team };
      const { data } = revisionMode
        ? await api.patch<RegistrationResult>("/auth/registration", {
            ...body,
            current_owner_password: currentOwnerPassword,
          })
        : await api.post<RegistrationResult>("/auth/registration", body);
      setPending(data);
      setState((s) => (s ? { ...s, pending_confirmation: true } : s));
      setRevisionMode(false);
      setCurrentOwnerPassword("");
      setIdentity({ email: "", temporary_password: "" });
      setNotice(
        "Initial details saved. Owner and Admin must each authenticate and personally confirm the exact summary.",
      );
      setOwner((p) => ({ ...p, temporary_password: "" }));
      setAdmin((p) => ({ ...p, temporary_password: "" }));
    } catch (e) {
      setError(apiErrorMessage(e));
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
          : "Your confirmation is recorded. The other initial person must now review and confirm with their own credentials.",
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
  function beginRevision() {
    if (!pending?.summary || pending.confirming_role !== "owner") return;
    const summary = pending.summary;
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
    setCommandId(crypto.randomUUID());
    setRevisionMode(true);
    setCurrentOwnerPassword("");
    setError("");
  }

  return (
    <main className="signup">
      <header className="signup-header">
        <KdpsLogo height={38} />
        <Link to="/login">Sign in</Link>
      </header>
      <h1>Set up your company and first store</h1>
      <p>
        One company registers this installation once. Afterwards, authorised people add stores and
        staff inside the company.
      </p>
      {error && (
        <div role="alert" className="warn-note" data-testid="signup-error">
          {error}
        </div>
      )}
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
          <h2>Initial joint confirmation</h2>
          <p>
            Owner and the separate Admin each sign in here with their own initial credentials,
            review the complete summary and confirm it personally. No company or business authority
            is created until both confirm the same revision.
          </p>
          <form onSubmit={inspect} className="signup-grid" data-testid="signup-confirm-credentials">
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
            <button className="btn" disabled={busy}>
              Review my initial summary
            </button>
          </form>
          {pending?.summary && <RegistrationSummaryView summary={pending.summary} />}
          {pending?.confirmed && (
            <p>
              Owner: {pending.confirmed.owner ? "confirmed" : "awaiting confirmation"} · Admin:{" "}
              {pending.confirmed.admin ? "confirmed" : "awaiting confirmation"}
            </p>
          )}
          {pending?.confirming_role === "owner" && (
            <button type="button" className="btn" disabled={busy} onClick={beginRevision}>
              Revise initial details and request both confirmations again
            </button>
          )}
          {pending?.confirming_role && identity.temporary_password && (
            <>
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
            </>
          )}
        </section>
      ) : (
        <form onSubmit={stage} data-testid="signup-form">
          {revisionMode && (
            <section className="card signup-section">
              <h2>Revise the pending initial registration</h2>
              <p>
                Changing any initial details clears both confirmations. Supply both new temporary
                passwords and personally authenticate with the current proposed Owner password.
              </p>
              <Input
                label="Current proposed Owner password"
                type="password"
                name="revision-owner-password"
                value={currentOwnerPassword}
                onChange={setCurrentOwnerPassword}
              />
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setRevisionMode(false);
                  setCurrentOwnerPassword("");
                  setOwner((p) => ({ ...p, temporary_password: "" }));
                  setAdmin((p) => ({ ...p, temporary_password: "" }));
                }}
              >
                Cancel revision
              </button>
            </section>
          )}
          <section className="card signup-section">
            <h2>Company and legal registration</h2>
            <div className="signup-grid">
              {Object.entries(company).map(([key, value]) => (
                <Input
                  key={key}
                  label={COMPANY_LABELS[key] ?? key}
                  name={key}
                  value={value}
                  onChange={(next) => setCompany({ ...company, [key]: next })}
                />
              ))}
            </div>
            <p className="caption">
              India and INR are the supported alpha configuration. Confirm your real legal and
              invoice details; signup does not approve tax rates.
            </p>
          </section>
          <section className="card signup-section">
            <h2>First store</h2>
            <div className="signup-grid">
              <Input
                label="Store code"
                name="store-code"
                value={store.code}
                onChange={(code) => setStore({ ...store, code })}
              />
              <Input
                label="Store name"
                name="store-name"
                value={store.name}
                onChange={(name) => setStore({ ...store, name })}
              />
              <Input
                label="City"
                name="store-city"
                value={store.city}
                onChange={(city) => setStore({ ...store, city })}
              />
              <Input
                label="Store address"
                name="store-address"
                value={store.address}
                onChange={(address) => setStore({ ...store, address })}
              />
              <label className="field">
                <span>How is this store starting?</span>
                <select
                  className="input"
                  value={store.setup_kind}
                  onChange={(e) => setStore({ ...store, setup_kind: e.target.value })}
                  data-testid="signup-store-kind"
                >
                  <option value="existing">Existing store switching software</option>
                  <option value="new">New store</option>
                </select>
              </label>
              {store.setup_kind === "existing" && (
                <Input
                  label="Current software / opening source"
                  name="source-system"
                  value={store.source_system}
                  onChange={(source_system) => setStore({ ...store, source_system })}
                />
              )}
            </div>
            <p>
              One online counter starts after approved setup. Existing stores need a fresh stock
              export and physical verification; new stores receive evidenced stock through normal
              receiving.
            </p>
          </section>
          <section className="card signup-section">
            <h2>Initial Owner</h2>
            <PersonFields who="owner" value={owner} onChange={setOwner} />
          </section>
          <section className="card signup-section">
            <h2>Separate initial Admin</h2>
            <PersonFields who="admin" value={admin} onChange={setAdmin} />
            <p>
              Owner and Admin are two people with separate credentials. Both replace their temporary
              password at first sign-in. Share temporary credentials directly with the named person.
            </p>
          </section>
          <section className="card signup-section">
            <h2>Proposed manager and team</h2>
            <p>
              These details are retained for normal company administration. No other person receives
              access during signup.
            </p>
            {team.map((p, index) => (
              <div className="signup-team-row" key={index}>
                <div className="signup-grid">
                  <Input
                    label="Full name"
                    name={`team-${index}-name`}
                    value={p.name}
                    onChange={(name) =>
                      setTeam(team.map((row, i) => (i === index ? { ...row, name } : row)))
                    }
                  />
                  <Input
                    label="Email"
                    type="email"
                    name={`team-${index}-email`}
                    value={p.email}
                    onChange={(email) =>
                      setTeam(team.map((row, i) => (i === index ? { ...row, email } : row)))
                    }
                  />
                  <Input
                    label="Staff code"
                    name={`team-${index}-code`}
                    value={p.staff_code}
                    onChange={(staff_code) =>
                      setTeam(team.map((row, i) => (i === index ? { ...row, staff_code } : row)))
                    }
                  />
                  <label className="field">
                    <span>Proposed role</span>
                    <select
                      className="input"
                      value={p.role_code}
                      onChange={(e) =>
                        setTeam(
                          team.map((row, i) =>
                            i === index ? { ...row, role_code: e.target.value } : row,
                          ),
                        )
                      }
                    >
                      {ROLES.map(([code, label]) => (
                        <option key={code} value={code}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setTeam(team.filter((_, i) => i !== index))}
                >
                  Remove proposed person
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn"
              onClick={() =>
                setTeam([
                  ...team,
                  { name: "", email: "", staff_code: "", role_code: "store_person" },
                ])
              }
              disabled={team.length >= 50}
            >
              Add proposed manager or team member
            </button>
          </section>
          <button className="btn btn-cta btn-lg" disabled={busy} data-testid="signup-submit">
            {busy ? "Saving initial details…" : "Save and request joint confirmation"}
          </button>
        </form>
      )}
    </main>
  );
}
