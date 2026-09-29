import { useCallback, useEffect, useRef, useState } from "react";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { useAuth } from "../auth/AuthContext";

interface Choice {
  id: number;
  code: string;
  name: string;
}

interface RoleChoice {
  code: string;
  name: string;
}

interface Assignment {
  id?: string;
  role_code: string;
  all_sites: boolean;
  site_ids: number[];
  all_brands: boolean;
  brand_ids: number[];
  effective_from: string;
  effective_to: string | null;
}

interface AssignmentAnswer {
  revision: number;
  items: Assignment[];
  choices?: { roles: RoleChoice[]; sites: Choice[]; brands: Choice[] };
}

interface RolePolicy {
  code: string;
  revision: number;
  section_access: Record<string, { capability: string; label: string }>;
  field_access: string[];
  step_actions: string[];
  initial_step_defaults?: string[];
}

const nowIso = () => new Date().toISOString();
const localDate = (value: string | null) => {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
};
const isoDate = (value: string) => (value ? new Date(value).toISOString() : null);

/** Each row keeps its own scope tuple. There are no independent action or field
 * grants on a person: those are configured once on the role policy. */
export function UnifiedAssignmentsTab({
  userId,
  personName,
}: {
  userId: string | null;
  personName: string;
}) {
  const { session } = useAuth();
  const [items, setItems] = useState<Assignment[]>([]);
  const [revision, setRevision] = useState<number | null>(null);
  const [choices, setChoices] = useState<AssignmentAnswer["choices"]>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const requestSequence = useRef(0);
  const url = userId ? `/auth/admin/users/${encodeURIComponent(userId)}/assignments` : "";

  const load = useCallback(() => {
    if (!url) return;
    const sequence = ++requestSequence.current;
    setLoading(true);
    setError("");
    api
      .get<AssignmentAnswer>(url)
      .then(({ data }) => {
        if (sequence !== requestSequence.current) return;
        setItems(data.items);
        setRevision(data.revision);
        setChoices(data.choices);
      })
      .catch((reason) => {
        if (sequence === requestSequence.current) setError(apiErrorMessage(reason));
      })
      .finally(() => {
        if (sequence === requestSequence.current) setLoading(false);
      });
  }, [url]);

  useEffect(load, [load]);

  if (!userId) {
    return <p className="lead">Create a login for {personName} before assigning access.</p>;
  }

  const roleChoices = choices?.roles ?? [];
  const siteChoices = choices?.sites ?? session?.context_choices.sites ?? [];
  const brandChoices = choices?.brands ?? session?.context_choices.brands ?? [];

  function edit(index: number, patch: Partial<Assignment>) {
    setItems((current) => current.map((item, at) => (at === index ? { ...item, ...patch } : item)));
  }

  function toggleId(index: number, kind: "site_ids" | "brand_ids", id: number) {
    const selected = items[index]?.[kind] ?? [];
    edit(index, {
      [kind]: selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id],
    });
  }

  async function save() {
    setError("");
    setMessage("");
    if (revision === null) {
      setError("Reload assignments before saving.");
      return;
    }
    if (!password) {
      setError("Enter your current password to confirm this access change.");
      return;
    }
    if (
      items.some(
        (item) =>
          !item.role_code ||
          (!item.all_sites && !item.site_ids.length) ||
          (!item.all_brands && !item.brand_ids.length),
      )
    ) {
      setError("Each assignment needs a role and a nonempty site and brand scope.");
      return;
    }
    if (
      items.some(
        (item) => item.effective_to && new Date(item.effective_to) <= new Date(item.effective_from),
      )
    ) {
      setError("An end date must be after its assignment's start date.");
      return;
    }
    setSaving(true);
    try {
      await api.put(url, {
        ...goodsMeta(revision),
        assignments: items.map(
          ({
            id,
            effective_from,
            role_code,
            all_sites,
            site_ids,
            all_brands,
            brand_ids,
            effective_to,
          }) => ({
            ...(id ? { id, effective_from } : {}),
            role_code,
            all_sites,
            site_ids: all_sites ? [] : site_ids,
            all_brands,
            brand_ids: all_brands ? [] : brand_ids,
            effective_to,
          }),
        ),
        current_password: password,
      });
      setPassword("");
      setMessage(
        "Assignments saved. A different authorised person must review this change within one working day.",
      );
      load();
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div data-testid="pa-assignments-tab">
      <h3 className="h3">Access assignments</h3>
      <p className="lead">
        Each role has its own site and brand scope. All includes future additions; selected sites
        and brands stay fixed.
      </p>
      {loading && <p>Loading assignments…</p>}
      {error && (
        <div className="warn-note" role="alert">
          {error}
        </div>
      )}
      {message && <div className="ok-note">{message}</div>}
      {items.map((item, index) => (
        <fieldset
          className="card section-card"
          key={item.id ?? `new-${index}`}
          data-testid={`pa-assignment-${index}`}
        >
          <legend>Assignment {index + 1}</legend>
          <div className="form-grid wide-form">
            <label className="field">
              Role
              <select
                className="input"
                value={item.role_code}
                onChange={(event) => edit(index, { role_code: event.target.value })}
              >
                <option value="">Choose a role</option>
                {roleChoices.map((role) => (
                  <option key={role.code} value={role.code}>
                    {role.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="pa-check">
              <input
                type="checkbox"
                checked={item.all_sites}
                onChange={(event) => edit(index, { all_sites: event.target.checked, site_ids: [] })}
              />{" "}
              All sites, including future sites
            </label>
            {!item.all_sites && (
              <div className="field">
                <span>Selected sites</span>
                {siteChoices.map((site) => (
                  <label className="pa-check" key={site.id}>
                    <input
                      type="checkbox"
                      checked={item.site_ids.includes(site.id)}
                      onChange={() => toggleId(index, "site_ids", site.id)}
                    />{" "}
                    {site.code} · {site.name}
                  </label>
                ))}
              </div>
            )}
            <label className="pa-check">
              <input
                type="checkbox"
                checked={item.all_brands}
                onChange={(event) =>
                  edit(index, { all_brands: event.target.checked, brand_ids: [] })
                }
              />{" "}
              All brands, including future brands
            </label>
            {!item.all_brands && (
              <div className="field">
                <span>Selected brands</span>
                {brandChoices.map((brand) => (
                  <label className="pa-check" key={brand.id}>
                    <input
                      type="checkbox"
                      checked={item.brand_ids.includes(brand.id)}
                      onChange={() => toggleId(index, "brand_ids", brand.id)}
                    />{" "}
                    {brand.code} · {brand.name}
                  </label>
                ))}
              </div>
            )}
            <div className="field">
              Effective from: {new Date(item.effective_from).toLocaleString()}
            </div>
            <label className="field">
              Effective until (optional)
              <input
                className="input"
                type="datetime-local"
                value={localDate(item.effective_to)}
                onChange={(event) => edit(index, { effective_to: isoDate(event.target.value) })}
              />
            </label>
          </div>
          <button
            className="btn btn-sm"
            type="button"
            onClick={() => setItems((current) => current.filter((_, at) => at !== index))}
          >
            Remove assignment
          </button>
        </fieldset>
      ))}
      <button
        className="btn btn-sm"
        type="button"
        onClick={() =>
          setItems((current) => [
            ...current,
            {
              role_code: "",
              all_sites: false,
              site_ids: [],
              all_brands: false,
              brand_ids: [],
              effective_from: nowIso(),
              effective_to: null,
            },
          ])
        }
        disabled={loading}
      >
        Add assignment
      </button>
      <label className="field">
        Current password
        <input
          className="input"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>
      <button
        className="btn btn-cta"
        type="button"
        onClick={save}
        disabled={loading || saving}
        data-testid="pa-assignments-save"
      >
        Save assignments
      </button>
    </div>
  );
}

/** One policy editor for section levels, protected fields and additive steps.
 * Its payload is versioned and validated by the server; the editor stays usable
 * when a new section or step is added without a frontend release. */
export function UnifiedRolePolicyPanel() {
  const [roles, setRoles] = useState<RoleChoice[]>([]);
  const [code, setCode] = useState("");
  const [draft, setDraft] = useState("");
  const [stepDefaults, setStepDefaults] = useState<string[]>([]);
  const [revision, setRevision] = useState<number | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const requestSequence = useRef(0);

  useEffect(() => {
    api
      .get<{ items: { data: { code: string; name: string } }[] }>("/auth/admin/roles")
      .then(({ data }) => setRoles(data.items.map(({ data: role }) => role)))
      .catch((reason) => setError(apiErrorMessage(reason)));
  }, []);

  const load = useCallback(() => {
    if (!code) return;
    const sequence = ++requestSequence.current;
    setLoading(true);
    setError("");
    api
      .get<RolePolicy>(`/auth/admin/roles/${encodeURIComponent(code)}/policy`)
      .then(({ data }) => {
        if (sequence !== requestSequence.current) return;
        setRevision(data.revision);
        setStepDefaults(data.initial_step_defaults ?? []);
        setDraft(
          JSON.stringify(
            {
              section_access: data.section_access,
              field_access: data.field_access,
              step_actions: data.step_actions,
            },
            null,
            2,
          ),
        );
      })
      .catch((reason) => {
        if (sequence === requestSequence.current) setError(apiErrorMessage(reason));
      })
      .finally(() => {
        if (sequence === requestSequence.current) setLoading(false);
      });
  }, [code]);
  useEffect(load, [load]);

  async function save() {
    setError("");
    setMessage("");
    if (revision === null) {
      setError("Reload this policy before saving.");
      return;
    }
    if (!password) {
      setError("Enter your current password to confirm this policy change.");
      return;
    }
    let policy: Pick<RolePolicy, "section_access" | "field_access" | "step_actions">;
    try {
      const parsed = JSON.parse(draft) as Partial<RolePolicy>;
      if (
        !parsed.section_access ||
        typeof parsed.section_access !== "object" ||
        !Array.isArray(parsed.field_access) ||
        !Array.isArray(parsed.step_actions)
      )
        throw new Error("Policy must contain section_access, field_access and step_actions.");
      policy = {
        section_access: parsed.section_access,
        field_access: parsed.field_access,
        step_actions: parsed.step_actions,
      };
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Invalid policy JSON");
      return;
    }
    setSaving(true);
    try {
      await api.put(`/auth/admin/roles/${encodeURIComponent(code)}/policy`, {
        ...goodsMeta(revision),
        ...policy,
        current_password: password,
      });
      setPassword("");
      setMessage(
        "Role policy saved. A different authorised person must review this change within one working day.",
      );
      load();
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div data-testid="pa-policy-panel">
      <h3 className="h3">Role policy</h3>
      <p className="lead">
        Set section levels, protected fields and workflow steps for each role. Approval separation
        and other fixed safeguards remain enforced by the server.
      </p>
      {error && (
        <div className="warn-note" role="alert">
          {error}
        </div>
      )}
      {message && <div className="ok-note">{message}</div>}
      <label className="field">
        Role
        <select
          className="input"
          value={code}
          onChange={(event) => {
            requestSequence.current += 1;
            setCode(event.target.value);
            setDraft("");
            setRevision(null);
          }}
        >
          <option value="">Choose a role</option>
          {roles.map((role) => (
            <option value={role.code} key={role.code}>
              {role.name}
            </option>
          ))}
        </select>
      </label>
      {loading && <p>Loading policy…</p>}
      {code && !loading && draft && (
        <>
          <button
            className="btn btn-sm"
            type="button"
            onClick={() => {
              try {
                const policy = JSON.parse(draft) as RolePolicy;
                policy.step_actions = [
                  ...new Set([...policy.step_actions, ...stepDefaults]),
                ].sort();
                setDraft(JSON.stringify(policy, null, 2));
                setMessage(
                  "Initial step defaults added to the draft. Review every addition before saving; previously removed permissions may be included.",
                );
              } catch {
                setError("Correct the policy JSON before adding defaults.");
              }
            }}
          >
            Review initial step defaults
          </button>
          <label className="field">
            Policy JSON
            <textarea
              className="input"
              rows={18}
              spellCheck={false}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              data-testid="pa-policy-json"
            />
          </label>
          <label className="field">
            Current password
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button
            className="btn btn-cta"
            type="button"
            onClick={save}
            disabled={saving}
            data-testid="pa-policy-save"
          >
            Save policy
          </button>
        </>
      )}
    </div>
  );
}

type WorkflowLevel = "none" | "view" | "operate" | "approve" | "manage";
interface WorkflowRule {
  section: string;
  minimum: WorkflowLevel;
}
interface WorkflowPolicyAnswer {
  revision: number;
  action_levels: Record<string, WorkflowRule>;
  upgrade_defaults: Record<string, WorkflowRule>;
}

/** The tenant's action-to-section thresholds. Fixed approval, identity and
 * document safeguards are still evaluated after these configurable levels. */
export function UnifiedWorkflowPolicyPanel() {
  const { refreshSession } = useAuth();
  const [levels, setLevels] = useState<Record<string, WorkflowRule>>({});
  const [upgradeDefaults, setUpgradeDefaults] = useState<Record<string, WorkflowRule>>({});
  const [revision, setRevision] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    api
      .get<WorkflowPolicyAnswer>("/auth/admin/workflow-policy")
      .then(({ data }) => {
        setLevels(data.action_levels);
        setUpgradeDefaults(data.upgrade_defaults ?? {});
        setRevision(data.revision);
      })
      .catch((reason) => setError(apiErrorMessage(reason)))
      .finally(() => setLoading(false));
  }, []);
  useEffect(load, [load]);

  async function save() {
    setError("");
    setMessage("");
    if (revision === null) {
      setError("Reload workflow policy before saving.");
      return;
    }
    if (!password) {
      setError("Enter your current password to confirm this policy change.");
      return;
    }
    setSaving(true);
    try {
      const { data } = await api.put<WorkflowPolicyAnswer>("/auth/admin/workflow-policy", {
        ...goodsMeta(revision),
        action_levels: levels,
        current_password: password,
      });
      setLevels(data.action_levels);
      setRevision(data.revision);
      setPassword("");
      setMessage(
        "Workflow policy saved. A different authorised person must review it within one working day.",
      );
      refreshSession(true);
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  const rows = Object.entries(levels)
    .filter(([action, rule]) =>
      `${action} ${rule.section}`.toLowerCase().includes(query.toLowerCase()),
    )
    .sort(
      ([leftAction, left], [rightAction, right]) =>
        left.section.localeCompare(right.section) || leftAction.localeCompare(rightAction),
    );

  return (
    <div data-testid="pa-workflow-policy-panel">
      <h3 className="h3">Workflow thresholds</h3>
      <p className="lead">
        Choose the section level needed for each action. Separate-person approvals, limits, document
        state and step-up remain fixed server checks.
      </p>
      {error && (
        <div className="warn-note" role="alert">
          {error}
        </div>
      )}
      {message && <div className="ok-note">{message}</div>}
      {loading ? (
        <p>Loading workflow policy…</p>
      ) : (
        <>
          {Object.keys(upgradeDefaults).length > 0 && (
            <div className="warn-note">
              <p>
                {Object.keys(upgradeDefaults).length} newly registered actions are disabled. Review
                their proposed defaults before saving this policy version.
              </p>
              <button
                className="btn"
                data-testid="pa-workflow-upgrade"
                onClick={() => {
                  setLevels((current) => ({ ...current, ...upgradeDefaults }));
                  setUpgradeDefaults({});
                }}
              >
                Review new-action defaults
              </button>
            </div>
          )}
          <label className="field">
            Find an action
            <input
              className="input"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Action</th>
                  <th>Section</th>
                  <th>Minimum level</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([action, rule]) => (
                  <tr key={action}>
                    <td>
                      <code>{action}</code>
                    </td>
                    <td>{rule.section.replaceAll("_", " ")}</td>
                    <td>
                      <select
                        className="input"
                        value={rule.minimum}
                        aria-label={`${action} minimum level`}
                        onChange={(event) =>
                          setLevels((current) => ({
                            ...current,
                            [action]: { ...rule, minimum: event.target.value as WorkflowLevel },
                          }))
                        }
                      >
                        {(["none", "view", "operate", "approve", "manage"] as const).map(
                          (level) => (
                            <option key={level} value={level}>
                              {level}
                            </option>
                          ),
                        )}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <label className="field">
            Current password
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button
            className="btn btn-cta"
            type="button"
            onClick={save}
            disabled={saving || revision === null}
            data-testid="pa-workflow-policy-save"
          >
            Save thresholds
          </button>
        </>
      )}
    </div>
  );
}
