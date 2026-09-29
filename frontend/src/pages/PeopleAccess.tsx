// People and access (ticket 03): an owner manages people separately from
// logins, gives each person an assignment, roles and grants, reviews
// privileged changes with password confirmation, and sees a revoked grant
// take effect at once. One area, a left people list, a person detail panel
// on the right with tabs — plus a separate Privileged changes screen
// (orchestrator UX brief).
//
// Enforcement lives on the server (design §4.2): every screen here is a thin
// client over `/api/goods-v1/auth/admin/*`. `session.actions` only steers which
// controls this build shows — a person who lacks the grant still gets the
// uniform hidden-object 404/403 from the server if they reach for it anyway
// (ADR-0003).
//
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  Ban,
  CheckCircle2,
  KeyRound,
  Pencil,
  Plus,
  Save,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
  X,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";

import type { paths } from "../lib/api-schema";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  hold,
  useGoodsFetch,
  useResourceDoc,
  useResourceList,
  useStepUp,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { CounterPinCard } from "../components/CounterPinCard";
import { AdministrativeHistory } from "../components/AdministrativeHistory";
import { privilegedReviewLink } from "../lib/administrativeHistory";
import "./PeopleAccess.css";

// --------------------------------------------------------------------------
// Wire shapes (design §6, E061-E084/E214/E237). Hand-typed like Organisation's
// — these are goods-v1 dict responses, not DRF serializers.
// --------------------------------------------------------------------------

interface StaffData {
  human_id: string;
  staff_code: string;
  display_name: string;
  salesperson: boolean;
  site_id: string | null;
  effective_from: string | null;
  mobile?: string | null;
}

// E062 (ticket 03B): the generated client carries the create contract, and with
// it the rule that `site_id` and `effective_from` are both optional - so
// omitting them to create a head-office person type-checks here.
type StaffCreateBody = NonNullable<
  paths["/api/goods-v1/auth/admin/staff"]["post"]["requestBody"]
>["content"]["application/json"];

// E077/E078/E080/E079 (ticket 03C): the role requests and the effective role
// maxima come from the generated client, so a contract change breaks the build.
type RoleCreateBody = NonNullable<
  paths["/api/goods-v1/auth/admin/roles"]["post"]["requestBody"]
>["content"]["application/json"];
type RoleUpdateBody = NonNullable<
  paths["/api/goods-v1/auth/admin/roles/{id}"]["patch"]["requestBody"]
>["content"]["application/json"];
type RoleAccessBody = NonNullable<
  paths["/api/goods-v1/auth/admin/roles/{code}/access"]["put"]["requestBody"]
>["content"]["application/json"];
type AccessMatrix =
  paths["/api/goods-v1/auth/admin/access-matrix"]["get"]["responses"][200]["content"]["application/json"];
type RoleMaximum = NonNullable<AccessMatrix["role_maxima"]>[number];

interface RoleData {
  code: string;
  name: string;
  description: string | null;
  active: boolean;
}

const SCOPE_KINDS = ["tenant", "entity", "site", "sbu", "brand"] as const;
type ScopeKind = (typeof SCOPE_KINDS)[number];

interface GrantScope {
  scope_kind: ScopeKind;
  entity_id: string | null;
  site_id: string | null;
  sbu_id: string | null;
  brand_id: string | null;
}

interface UserGrant {
  id: string;
  human_id: string;
  role_id: string;
  role_code: string;
  scope: GrantScope;
  actions: { actions: string[]; scope_kind: string };
  fields: string[];
  effective_from: string | null;
  effective_to: string | null;
}

interface UserData {
  human_id: string | null;
  email: string;
  display_name: string;
  active: boolean;
  /** GSA-T03/ticket 03A: true from a create or assisted reset that issued a
   *  temporary password, until that login's own E239 change-password succeeds. */
  must_change_password: boolean;
  /** Store operations ticket 06, on the login's own read only: whether this
   *  login has a counter PIN, and whether it may hold one - never the PIN. */
  has_till_pin?: boolean;
  may_hold_till_pin?: boolean;
  /** Whether the person reading may clear it (Admin only). */
  may_reset_till_pin?: boolean;
  grants?: UserGrant[];
}

interface PrivilegedValue {
  field: string;
  redacted: boolean;
  value: string | null;
}

interface PrivilegedChangeData {
  action: string;
  subject_key: string;
  actor_id: string | null;
  actor_name: string | null;
  service_code: string | null;
  outcome: string;
  reason_code: string | null;
  site_id: string | null;
  before: PrivilegedValue[] | null;
  after: PrivilegedValue[] | null;
  event_at: string | null;
  recorded_at: string | null;
  reviews: {
    reviewer_id: string;
    reviewer_name: string | null;
    note: string;
    reviewed_at: string | null;
  }[];
}

interface AdminMetaSite {
  id: string;
  code: string;
  name: string;
}

interface AdminMetaSbu {
  id: string;
  code: string;
  site_id: string;
  brand_id: string | null;
}

interface AdminMetaRole {
  id: string;
  code: string;
  name: string;
  active: boolean;
}

interface AdminMetaRoleTemplate {
  code: string;
  name: string;
  actions: string[];
  fields: string[];
  scope_kinds: string[];
}

interface AdminMeta {
  roles: AdminMetaRole[];
  sites: AdminMetaSite[];
  sbus: AdminMetaSbu[];
  actions: { code: string; label: string }[];
  fields: string[];
  scope_kinds: string[];
  role_templates: AdminMetaRoleTemplate[];
}

// A narrower duplicate of Organisation.tsx's own `EntityData` (this screen
// only ever reads `code`/`name` off an entity, for the grant scope picker) —
// left as its own local type rather than importing across pages for one field
// subset; worth folding into a shared masters-types module if a third screen
// needs entities.
interface EntityData {
  code: string;
  name: string;
}

// --------------------------------------------------------------------------
// Small shared bits local to this screen
// --------------------------------------------------------------------------

function useAdminMeta() {
  const { value } = useResourceListRaw<AdminMeta>("/goods-v1/auth/admin/meta");
  return value;
}

// `admin/meta` (E214) answers one plain object, not a `ResourceDTO`/`Page` —
// its own tiny fetch rather than stretching `useGoodsFetch`'s resource shape
// to fit.
function useResourceListRaw<T>(url: string) {
  const [value, setValue] = useState<T | null>(null);
  useEffect(() => {
    let live = true;
    api
      .get<T>(url)
      .then((r) => live && setValue(r.data))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [url]);
  return { value };
}

// E079's `role_maxima`: each role's effective ceiling - its template narrowed by
// E080. A role with no registered template has no row, and so no ceiling.
// `maxima` is null while a read is in flight, so nothing edits from a stale
// ceiling; `version` counts completed reads so an editor can re-seed from each.
function useRoleMaxima(refresh: number) {
  const [state, setState] = useState<{
    maxima: RoleMaximum[] | null;
    error: string;
    version: number;
  }>({
    maxima: null,
    error: "",
    version: 0,
  });
  useEffect(() => {
    let live = true;
    setState((prev) => ({ ...prev, maxima: null, error: "" }));
    api
      .get<AccessMatrix>("/goods-v1/auth/admin/access-matrix")
      .then((r) => {
        if (!live) return;
        const maxima = r.data.role_maxima ?? [];
        setState((prev) => ({ maxima, error: "", version: prev.version + 1 }));
      })
      .catch((e) => {
        if (live) setState((prev) => ({ ...prev, maxima: null, error: apiErrorMessage(e) }));
      });
    return () => {
      live = false;
    };
  }, [refresh]);
  return state;
}

function siteName(meta: AdminMeta | null, siteId: string | null): string {
  if (!siteId) return "No primary site assigned";
  return meta?.sites.find((s) => s.id === siteId)?.name ?? siteId;
}

function scopeLabel(meta: AdminMeta | null, scope: GrantScope): string {
  switch (scope.scope_kind) {
    case "tenant":
      return "Whole tenant";
    case "site":
      return `Site: ${meta?.sites.find((s) => s.id === scope.site_id)?.name ?? scope.site_id}`;
    case "sbu":
      return `SBU: ${meta?.sbus.find((s) => s.id === scope.sbu_id)?.code ?? scope.sbu_id}`;
    case "entity":
      return `Entity #${scope.entity_id}`;
    case "brand":
      return `Brand #${scope.brand_id}`;
    default:
      return scope.scope_kind;
  }
}

function fmtDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

// --------------------------------------------------------------------------
// People list (E061)
// --------------------------------------------------------------------------

const blankPerson = {
  staff_code: "",
  display_name: "",
  mobile: "",
  site_id: "",
  effective_from: "",
};

function nowLocalInput(): string {
  const d = new Date();
  d.setSeconds(0, 0);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

// E061's own list answers `can_create_unplaced` (ticket 03B) alongside its
// rows - the same tenant-wide `staff.manage` check E062 makes of the site-less
// create. `useResourceList` throws that extra field away (it only keeps
// `items`), so this screen reads the envelope itself rather than guess the
// answer from `session.sites`, which does not say what is scoped where.
function useStaffList(url: string) {
  const { value, ...rest } = useGoodsFetch<
    Page<ResourceDTO<StaffData>> & { can_create_unplaced?: boolean },
    { items: ResourceDTO<StaffData>[]; canCreateUnplaced: boolean }
  >(url, (r) => ({ items: r.items ?? [], canCreateUnplaced: Boolean(r.can_create_unplaced) }), {
    items: [],
    canCreateUnplaced: false,
  });
  return { items: value.items, canCreateUnplaced: value.canCreateUnplaced, ...rest };
}

function PeopleListPanel({ onOpen }: { onOpen: (staffId: string) => void }) {
  const { session } = useAuth();
  const meta = useAdminMeta();
  const { items, canCreateUnplaced, loading, denied, failure, reload } = useStaffList(
    "/goods-v1/auth/admin/staff",
  );
  const canCreate = hold(session, "staff.manage");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  // `effective_from` starts empty: the default is a head-office person with no
  // site, and the site dropdown fills the start date when a site is picked - so
  // the date is the moment the site was chosen, not the moment the form opened.
  const [form, setForm] = useState({ ...blankPerson });
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  const filtered = items.filter((row) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return `${row.data.staff_code} ${row.data.display_name}`.toLowerCase().includes(q);
  });

  function startCreate() {
    setForm({ ...blankPerson });
    setOpen(true);
    setError("");
    setOk("");
  }

  async function save() {
    setError("");
    setOk("");
    if (!form.staff_code || !form.display_name) {
      setError("Staff code and name are both required.");
      return;
    }
    // E062 takes the site and its start date together or not at all. Head-office
    // staff are created with neither and get no assignment (GSA-T03).
    if (form.site_id && !form.effective_from) {
      setError("A site needs the date the person starts there.");
      return;
    }
    // Mirrors E062 step 8 (`GoodsStaffListCreateView.post`): only tenant-wide
    // staff.manage may create a person with no site.
    if (!form.site_id && !canCreateUnplaced) {
      setError("Choose the site this person works at.");
      return;
    }
    try {
      // Staff create is an ordinary staff edit (change PRD §14.5 P1) — the
      // backend never asks for step-up here (no `access.require_step_up()`
      // in `GoodsStaffListCreateView.post`), so this is a plain call, not
      // `stepUp.guarded(...)`.
      // `StaffCreateBody` now carries `command_id`/`contract_version` too (03B-P3)
      // - folding `goodsMeta()` into this same typed object, rather than
      // spreading it on afterwards, is what lets the generated type actually
      // catch a caller that forgets it.
      const body: StaffCreateBody = {
        ...goodsMeta(),
        staff_code: form.staff_code,
        display_name: form.display_name,
        mobile: form.mobile || null,
        ...(form.site_id
          ? {
              site_id: form.site_id,
              effective_from: new Date(form.effective_from).toISOString(),
            }
          : {}),
      };
      const created = await api.post<ResourceDTO<StaffData>>("/goods-v1/auth/admin/staff", body);
      setOpen(false);
      setOk(`${form.display_name} added.`);
      reload();
      onOpen(created.data.id);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (denied) return <Denied what="people" />;

  return (
    <div data-testid="pa-people-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">People</h3>
        <div className="spacer" />
        {canCreate && (
          <button className="btn btn-cta" onClick={startCreate} data-testid="person-new-button">
            <UserPlus size={15} /> New person
          </button>
        )}
      </div>
      <input
        className="input"
        placeholder="Search by staff code or name"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        data-testid="person-search-input"
        style={{ marginBottom: 12 }}
        aria-label="Search people"
      />
      <Feedback error={error} ok={ok} />
      {canCreate && open && (
        <div className="card section-card" data-testid="person-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">Add a person</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="person-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <div className="field">
              <label htmlFor="person-staff-code-input">Staff code</label>
              <input
                id="person-staff-code-input"
                className="input"
                value={form.staff_code}
                onChange={(e) => setForm({ ...form, staff_code: e.target.value })}
                data-testid="person-staff-code-input"
              />
            </div>
            <div className="field">
              <label htmlFor="person-name-input">Name</label>
              <input
                id="person-name-input"
                className="input"
                value={form.display_name}
                onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                data-testid="person-name-input"
              />
            </div>
            <div className="field">
              <label htmlFor="person-mobile-input">Mobile (optional)</label>
              <input
                id="person-mobile-input"
                className="input"
                value={form.mobile}
                onChange={(e) => setForm({ ...form, mobile: e.target.value })}
                data-testid="person-mobile-input"
              />
            </div>
            <div className="field">
              <label htmlFor="person-site-select">Site</label>
              <select
                id="person-site-select"
                className="input"
                value={form.site_id}
                onChange={(e) =>
                  setForm({
                    ...form,
                    site_id: e.target.value,
                    // A site needs a start date, so offer one to change rather
                    // than an empty box the server would refuse. Going back to
                    // head office clears it, so the pair is never half-set.
                    effective_from: e.target.value ? form.effective_from || nowLocalInput() : "",
                  })
                }
                data-testid="person-site-select"
              >
                <option value="" disabled={!canCreateUnplaced}>
                  {canCreateUnplaced ? "Head office — no primary site" : "Choose a site…"}
                </option>
                {(meta?.sites ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </div>
            {form.site_id && (
              <div className="field">
                <label htmlFor="person-effective-from-input">Starts</label>
                <input
                  id="person-effective-from-input"
                  className="input"
                  type="datetime-local"
                  value={form.effective_from}
                  onChange={(e) => setForm({ ...form, effective_from: e.target.value })}
                  data-testid="person-effective-from-input"
                />
              </div>
            )}
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={!form.site_id && !canCreateUnplaced}
              data-testid="person-save-button"
            >
              <Save size={15} /> Save person
            </button>
          </div>
          <p className="lead" style={{ marginTop: 8 }} data-testid="person-site-help">
            {canCreateUnplaced ? (
              <>
                Leave the site as "Head office" for someone who works for the company rather than at
                one place: they are created with no assignment, shown as "No primary site assigned",
                and can be assigned to a real site later. Either way, what this person may do is
                decided by their role grants, never by the site shown here.
              </>
            ) : (
              <>
                Choose the site this person works at - what they may do there is decided by their
                role grants, never by the site shown here.
              </>
            )}
          </p>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="people-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              <th>Site</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={4}>Loading…</td>
              </tr>
            ) : failure ? (
              <tr>
                <td colSpan={4} className="warn-note">
                  {failure}
                </td>
              </tr>
            ) : filtered.length === 0 ? (
              <tr data-testid="people-empty">
                <td colSpan={4}>No people found.</td>
              </tr>
            ) : (
              filtered.map((row) => (
                <tr
                  key={row.id}
                  className="pa-row"
                  onClick={() => onOpen(row.id)}
                  data-testid={`person-row-${row.data.staff_code}`}
                >
                  <td>
                    <b className="mono">{row.data.staff_code}</b>
                  </td>
                  <td>{row.data.display_name}</td>
                  <td>{siteName(meta, row.data.site_id)}</td>
                  <td>
                    <span className={`chip chip-${row.state === "active" ? "green" : "red"}`}>
                      {row.state}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Person tab (E063-E065)
// --------------------------------------------------------------------------

function PersonTab({ doc, reload }: { doc: ResourceDTO<StaffData>; reload: () => void }) {
  const { session } = useAuth();
  const canEdit = doc.allowed_actions.includes("update");
  const canRetire = doc.allowed_actions.includes("retire");
  const meta = useAdminMeta();
  const [form, setForm] = useState({
    staff_code: doc.data.staff_code,
    display_name: doc.data.display_name,
    mobile: doc.data.mobile ?? "",
    salesperson: doc.data.salesperson,
  });
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [retiring, setRetiring] = useState(false);
  const [reason, setReason] = useState("");
  const stepUp = useStepUp();

  useEffect(() => {
    setForm({
      staff_code: doc.data.staff_code,
      display_name: doc.data.display_name,
      mobile: doc.data.mobile ?? "",
      salesperson: doc.data.salesperson,
    });
  }, [doc]);

  async function save() {
    setError("");
    setOk("");
    try {
      await api.patch(`/goods-v1/auth/admin/staff/${doc.id}`, {
        staff_code: form.staff_code,
        display_name: form.display_name,
        mobile: form.mobile || null,
        salesperson: form.salesperson,
        ...goodsMeta(doc.revision),
      });
      setOk("Saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function retire() {
    setError("");
    setOk("");
    if (!reason.trim()) {
      setError("Say why this person is retiring.");
      return;
    }
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/auth/admin/staff/${doc.id}/retire`, {
          reason_code: reason.trim(),
          effective_at: new Date().toISOString(),
          ...goodsMeta(doc.revision),
        }),
      );
      setOk(`${doc.data.display_name} retired.`);
      setRetiring(false);
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  const showMobile = "mobile" in doc.data || hold(session, "staff.manage");

  return (
    <div data-testid="pa-person-tab">
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      <div className="card section-card">
        <div className="form-grid wide-form">
          <div className="field">
            <label htmlFor="pa-staff-code">Staff code</label>
            <input
              id="pa-staff-code"
              className="input"
              value={form.staff_code}
              disabled={!canEdit}
              onChange={(e) => setForm({ ...form, staff_code: e.target.value })}
              data-testid="pa-staff-code-input"
            />
          </div>
          <div className="field">
            <label htmlFor="pa-name">Name</label>
            <input
              id="pa-name"
              className="input"
              value={form.display_name}
              disabled={!canEdit}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              data-testid="pa-name-input"
            />
          </div>
          {showMobile && (
            <div className="field">
              <label htmlFor="pa-mobile">Mobile</label>
              <input
                id="pa-mobile"
                className="input"
                value={form.mobile}
                disabled={!canEdit}
                onChange={(e) => setForm({ ...form, mobile: e.target.value })}
                data-testid="pa-mobile-input"
              />
            </div>
          )}
          <label htmlFor="pa-salesperson" className="check-row">
            <input
              id="pa-salesperson"
              type="checkbox"
              checked={form.salesperson}
              disabled={!canEdit}
              onChange={(e) => setForm({ ...form, salesperson: e.target.checked })}
              data-testid="pa-salesperson-input"
            />{" "}
            Counts as a salesperson
          </label>
          {canEdit && (
            <button className="btn btn-cta" onClick={save} data-testid="pa-person-save-button">
              <Save size={15} /> Save
            </button>
          )}
        </div>
        <p className="lead" style={{ marginTop: 8 }}>
          Site: {siteName(meta, doc.data.site_id)}
          {doc.data.site_id === null && (
            <span data-testid="pa-no-primary-site">
              {" "}
              — grants alone still decide this person's authority.
            </span>
          )}
        </p>
      </div>

      {doc.state === "active" && canRetire && (
        <div className="card section-card" style={{ marginTop: 12 }}>
          <h3 className="h3">Retire this person</h3>
          <p className="lead">Needs your password again — retiring staff is a privileged change.</p>
          {!retiring ? (
            <button
              className="btn btn-sm"
              onClick={() => setRetiring(true)}
              data-testid="pa-retire-start-button"
            >
              <Ban size={14} /> Retire
            </button>
          ) : (
            <div className="form-grid">
              <input
                className="input"
                placeholder="Reason (e.g. LEFT_TEAM)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                data-testid="pa-retire-reason-input"
              />
              <button
                className="btn btn-cta"
                onClick={retire}
                data-testid="pa-retire-confirm-button"
              >
                Confirm retirement
              </button>
              <button
                className="btn btn-sm"
                onClick={() => setRetiring(false)}
                data-testid="pa-retire-cancel-button"
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
      {doc.state === "retired" && (
        <div className="card section-card" style={{ marginTop: 12 }} data-testid="pa-retired-note">
          Retired.
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Assignment tab (E070)
// --------------------------------------------------------------------------

function AssignmentTab({ doc, reload }: { doc: ResourceDTO<StaffData>; reload: () => void }) {
  const meta = useAdminMeta();
  const [siteId, setSiteId] = useState("");
  const [startsAt, setStartsAt] = useState(nowLocalInput());
  const [reasonCode, setReasonCode] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const canAssign = doc.allowed_actions.includes("assign");

  async function assign() {
    setError("");
    setOk("");
    if (!siteId || !startsAt || !reasonCode.trim()) {
      setError("Site, start date and reason are all required.");
      return;
    }
    try {
      await api.post(`/goods-v1/auth/admin/staff/${doc.id}/assign`, {
        site_id: siteId,
        effective_from: new Date(startsAt).toISOString(),
        reason_code: reasonCode.trim(),
        ...goodsMeta(doc.revision),
      });
      setOk("Assignment saved.");
      setSiteId("");
      setReasonCode("");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  return (
    <div data-testid="pa-assignment-tab">
      <Feedback error={error} ok={ok} />
      <div className="card section-card">
        <p className="lead">
          Current site: <b>{siteName(meta, doc.data.site_id)}</b>
          {doc.data.effective_from && ` (since ${fmtDate(doc.data.effective_from)})`}
        </p>
      </div>
      {canAssign && doc.state === "active" && (
        <div className="card section-card" style={{ marginTop: 12 }}>
          <h3 className="h3">
            {doc.data.site_id === null ? "Assign to a site" : "Move to a new site"}
          </h3>
          <div className="form-grid wide-form">
            <div className="field">
              <label htmlFor="pa-assign-site">New site</label>
              <select
                id="pa-assign-site"
                className="input"
                value={siteId}
                onChange={(e) => setSiteId(e.target.value)}
                data-testid="pa-assign-site-select"
              >
                <option value="">Choose a site…</option>
                {(meta?.sites ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="pa-assign-from">Starts</label>
              <input
                id="pa-assign-from"
                className="input"
                type="datetime-local"
                value={startsAt}
                onChange={(e) => setStartsAt(e.target.value)}
                data-testid="pa-assign-from-input"
              />
            </div>
            <div className="field">
              <label htmlFor="pa-assign-reason">Reason</label>
              <input
                id="pa-assign-reason"
                className="input"
                value={reasonCode}
                onChange={(e) => setReasonCode(e.target.value)}
                data-testid="pa-assign-reason-input"
              />
            </div>
            <button className="btn btn-cta" onClick={assign} data-testid="pa-assign-button">
              Move
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Login tab (E071-E074) and the grants/effective-authority tabs that need it
// --------------------------------------------------------------------------

/** The login (if any) whose `human_id` matches this person, found from the
 *  logins list — there is no "get a login by human_id" read, only by the
 *  login's own id, so the match happens client-side against the list this
 *  screen already needs for the "Has login"/"No login" marker. */
function useLoginForPerson(humanId: string) {
  const list = useResourceList<UserData>("/goods-v1/auth/admin/users");
  const summary = list.items.find((u) => u.data.human_id === humanId) ?? null;
  const detail = useResourceDoc<UserData>(
    summary ? `/goods-v1/auth/admin/users/${summary.id}` : null,
  );
  return {
    summary,
    detail,
    reloadList: list.reload,
    listLoading: list.loading,
    listDenied: list.denied,
  };
}

const blankLogin = { email: "", display_name: "", password: "", identityConfirmed: false };

function LoginTab({
  staff,
  login,
}: {
  staff: ResourceDTO<StaffData>;
  login: ReturnType<typeof useLoginForPerson>;
}) {
  const { session } = useAuth();
  const canManage = hold(session, "access.manage");
  const [form, setForm] = useState({ ...blankLogin, display_name: staff.data.display_name });
  const [editing, setEditing] = useState(false);
  const [active, setActive] = useState(true);
  const [newPassword, setNewPassword] = useState("");
  const [resetIdentityConfirmed, setResetIdentityConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  useEffect(() => {
    if (login.detail.doc) setActive(login.detail.doc.data.active);
  }, [login.detail.doc]);

  async function createLogin() {
    setError("");
    setOk("");
    if (!form.email || !form.display_name || !form.password) {
      setError("Email, name and a temporary password are all required.");
      return;
    }
    // GSA-T03/ticket 03A: an administrator explicitly confirms identity and
    // email ownership before a temporary password can be issued.
    if (!form.identityConfirmed) {
      setError("Confirm this person's identity and ownership of the login email first.");
      return;
    }
    try {
      await stepUp.guarded(() =>
        api.post("/goods-v1/auth/admin/users", {
          human_id: staff.data.human_id,
          email: form.email,
          display_name: form.display_name,
          password: form.password,
          identity_email_confirmed: true,
          ...goodsMeta(),
        }),
      );
      setOk(
        "Login created. Share the temporary password with them directly, never in writing here.",
      );
      setEditing(false);
      login.reloadList();
      login.detail.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function saveLogin() {
    if (!login.summary) return;
    setError("");
    setOk("");
    // Assisted reset: the same identity/email-ownership confirmation a create
    // needs, since GSA-T03 treats confirming the person and issuing them a
    // fresh temporary password as one bundled act.
    if (newPassword && !resetIdentityConfirmed) {
      setError("Confirm this person's identity and ownership of the login email first.");
      return;
    }
    try {
      await stepUp.guarded(() =>
        api.patch(`/goods-v1/auth/admin/users/${login.summary!.id}`, {
          email: form.email || undefined,
          display_name: form.display_name || undefined,
          active,
          ...(newPassword ? { password: newPassword, identity_email_confirmed: true } : {}),
          ...goodsMeta(login.detail.doc?.revision),
        }),
      );
      setOk("Login saved.");
      setNewPassword("");
      setResetIdentityConfirmed(false);
      login.detail.reload();
      login.reloadList();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (login.listLoading) return <div data-testid="pa-login-tab">Loading…</div>;
  if (login.listDenied) return <Denied what="login" />;

  return (
    <div data-testid="pa-login-tab">
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {!login.summary ? (
        <div className="card section-card">
          <p className="lead" data-testid="pa-no-login">
            This person has no login yet.
          </p>
          {canManage && (
            <div className="form-grid wide-form">
              <div className="field">
                <label htmlFor="pa-login-email">Email</label>
                <input
                  id="pa-login-email"
                  className="input"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  data-testid="pa-login-email-input"
                />
              </div>
              <div className="field">
                <label htmlFor="pa-login-name">Name</label>
                <input
                  id="pa-login-name"
                  className="input"
                  value={form.display_name}
                  onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                  data-testid="pa-login-name-input"
                />
              </div>
              <div className="field">
                <label htmlFor="pa-login-password">Temporary password</label>
                <input
                  id="pa-login-password"
                  className="input"
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                  data-testid="pa-login-password-input"
                />
              </div>
              <label htmlFor="pa-login-identity-confirmed" className="check-row">
                <input
                  id="pa-login-identity-confirmed"
                  type="checkbox"
                  checked={form.identityConfirmed}
                  onChange={(e) => setForm({ ...form, identityConfirmed: e.target.checked })}
                  data-testid="pa-login-identity-confirmed-input"
                />{" "}
                I confirmed this person's identity and ownership of this login email
              </label>
              <button
                className="btn btn-cta"
                onClick={createLogin}
                data-testid="pa-login-create-button"
              >
                <KeyRound size={15} /> Create login
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="card section-card" data-testid="pa-login-existing">
          <p className="lead">
            <b>{login.detail.doc?.data.email ?? login.summary.data.email}</b>{" "}
            <span className={`chip chip-${login.summary.data.active ? "green" : "red"}`}>
              {login.summary.data.active ? "active" : "inactive"}
            </span>{" "}
            {/* GSA-T03/ticket 03A: true until this login's own change-password
                (E239) succeeds - a restricted session server-side, not merely a
                hint here (`accounts.authentication.enforce_password_change_restriction`). */}
            {(login.detail.doc?.data.must_change_password ??
              login.summary.data.must_change_password) && (
              <span className="chip chip-amber" data-testid="pa-login-must-change-password">
                must replace temporary password
              </span>
            )}
          </p>
          {canManage && !editing && (
            <button
              className="btn btn-sm"
              onClick={() => {
                setForm({
                  email: login.detail.doc?.data.email ?? "",
                  display_name: login.detail.doc?.data.display_name ?? "",
                  password: "",
                  identityConfirmed: false,
                });
                setNewPassword("");
                setResetIdentityConfirmed(false);
                setEditing(true);
              }}
              data-testid="pa-login-edit-button"
            >
              <Pencil size={13} /> Edit login
            </button>
          )}
          {canManage && editing && (
            <div className="form-grid wide-form">
              <div className="field">
                <label htmlFor="pa-login-edit-email">Email</label>
                <input
                  id="pa-login-edit-email"
                  className="input"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  data-testid="pa-login-edit-email-input"
                />
              </div>
              <div className="field">
                <label htmlFor="pa-login-edit-name">Name</label>
                <input
                  id="pa-login-edit-name"
                  className="input"
                  value={form.display_name}
                  onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                  data-testid="pa-login-edit-name-input"
                />
              </div>
              <label htmlFor="pa-login-edit-active" className="check-row">
                <input
                  id="pa-login-edit-active"
                  type="checkbox"
                  checked={active}
                  onChange={(e) => setActive(e.target.checked)}
                  data-testid="pa-login-edit-active-input"
                />{" "}
                Active
              </label>
              <div className="field">
                <label htmlFor="pa-login-edit-password">
                  New temporary password (assisted reset, optional)
                </label>
                <input
                  id="pa-login-edit-password"
                  className="input"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  data-testid="pa-login-edit-password-input"
                />
              </div>
              {newPassword && (
                <label htmlFor="pa-login-reset-identity-confirmed" className="check-row">
                  <input
                    id="pa-login-reset-identity-confirmed"
                    type="checkbox"
                    checked={resetIdentityConfirmed}
                    onChange={(e) => setResetIdentityConfirmed(e.target.checked)}
                    data-testid="pa-login-reset-identity-confirmed-input"
                  />{" "}
                  I confirmed this person's identity and ownership of this login email
                </label>
              )}
              <button
                className="btn btn-cta"
                onClick={saveLogin}
                data-testid="pa-login-save-button"
              >
                <Save size={15} /> Save login
              </button>
              <button
                className="btn btn-sm"
                onClick={() => {
                  setNewPassword("");
                  setResetIdentityConfirmed(false);
                  setEditing(false);
                }}
                data-testid="pa-login-edit-cancel-button"
              >
                Cancel
              </button>
            </div>
          )}
          {/* Store operations ticket 06 (B76): Admin sets or clears it here. */}
          {login.summary && <CounterPinCard key={login.summary.id} userId={login.summary.id} />}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Roles and grants + Effective authority (E081, grants inside E072)
// --------------------------------------------------------------------------

function actionsForRole(
  meta: AdminMeta | null,
  roleCode: string,
): AdminMetaRoleTemplate | undefined {
  return meta?.role_templates.find((t) => t.code === roleCode);
}

function GrantEditor({
  meta,
  userId,
  revision,
  onDone,
  stepUp,
}: {
  meta: AdminMeta | null;
  userId: string;
  revision: number | undefined;
  onDone: () => void;
  stepUp: ReturnType<typeof useStepUp>;
}) {
  const [roleId, setRoleId] = useState("");
  const [scopeKind, setScopeKind] = useState<ScopeKind>("site");
  const [siteId, setSiteId] = useState("");
  const [sbuId, setSbuId] = useState("");
  const [entityId, setEntityId] = useState("");
  const [brandId, setBrandId] = useState("");
  const [actions, setActions] = useState<string[]>([]);
  const [fields, setFields] = useState<string[]>([]);
  const [effectiveFrom, setEffectiveFrom] = useState(nowLocalInput());
  const [reasonCode, setReasonCode] = useState("");
  const [error, setError] = useState("");
  const entities = useResourceList<EntityData>("/goods-v1/masters/entities");

  const { maxima, error: maximaError } = useRoleMaxima(0);

  const role = meta?.roles.find((r) => r.id === roleId);
  const template = role ? actionsForRole(meta, role.code) : undefined;
  // What this role may carry now (ticket 03C): its template as narrowed by the
  // role's own E080 setting. The server refuses anything beyond it regardless.
  const maximum = role ? maxima?.find((m) => m.role_code === role.code) : undefined;
  const scopeKinds = role
    ? SCOPE_KINDS.filter((k) => maximum?.scope_kinds.includes(k))
    : SCOPE_KINDS;

  function toggle(list: string[], set: (v: string[]) => void, value: string) {
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  }

  async function submit() {
    setError("");
    if (!roleId || actions.length === 0 || !effectiveFrom || !reasonCode.trim()) {
      setError("Role, at least one action, a start date and a reason are all required.");
      return;
    }
    const scope: Record<string, unknown> = { scope_kind: scopeKind };
    if (scopeKind === "site") scope.site_id = siteId;
    if (scopeKind === "sbu") scope.sbu_id = sbuId;
    if (scopeKind === "entity") scope.entity_id = entityId;
    if (scopeKind === "brand") scope.brand_id = brandId;
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/auth/admin/users/${userId}/grants`, {
          grants: [
            {
              role_id: roleId,
              scope,
              actions: { actions },
              fields,
              effective_from: new Date(effectiveFrom).toISOString(),
            },
          ],
          reason_code: reasonCode.trim(),
          ...goodsMeta(revision),
        }),
      );
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  return (
    <div className="card section-card" data-testid="pa-grant-editor">
      <h3 className="h3">Add a grant</h3>
      <Feedback error={error} ok="" />
      <div className="form-grid wide-form">
        <div className="field">
          <label htmlFor="pa-grant-role">Role</label>
          <select
            id="pa-grant-role"
            className="input"
            value={roleId}
            onChange={(e) => {
              setRoleId(e.target.value);
              setActions([]);
              setFields([]);
              const nextCode = meta?.roles.find((r) => r.id === e.target.value)?.code;
              const next = maxima?.find((m) => m.role_code === nextCode);
              const allowed = next ? SCOPE_KINDS.filter((k) => next.scope_kinds.includes(k)) : [];
              if (allowed[0] && !allowed.includes(scopeKind)) setScopeKind(allowed[0]);
            }}
            data-testid="pa-grant-role-select"
          >
            <option value="">Choose a role…</option>
            {(meta?.roles ?? [])
              .filter((r) => r.active)
              .map((r) => (
                <option key={r.id} value={r.id}>
                  {r.code} — {r.name}
                </option>
              ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="pa-grant-scope-kind">Scope</label>
          <select
            id="pa-grant-scope-kind"
            className="input"
            value={scopeKind}
            onChange={(e) => setScopeKind(e.target.value as ScopeKind)}
            data-testid="pa-grant-scope-kind-select"
          >
            {scopeKinds.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>

        {scopeKind === "site" && (
          <div className="field">
            <label htmlFor="pa-grant-site">Site</label>
            <select
              id="pa-grant-site"
              className="input"
              value={siteId}
              onChange={(e) => setSiteId(e.target.value)}
              data-testid="pa-grant-site-select"
            >
              <option value="">Choose a site…</option>
              {(meta?.sites ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
          </div>
        )}
        {scopeKind === "sbu" && (
          <div className="field">
            <label htmlFor="pa-grant-sbu">SBU</label>
            <select
              id="pa-grant-sbu"
              className="input"
              value={sbuId}
              onChange={(e) => setSbuId(e.target.value)}
              data-testid="pa-grant-sbu-select"
            >
              <option value="">Choose an SBU…</option>
              {(meta?.sbus ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.code}
                </option>
              ))}
            </select>
          </div>
        )}
        {scopeKind === "entity" && (
          <div className="field">
            <label htmlFor="pa-grant-entity">Entity</label>
            <select
              id="pa-grant-entity"
              className="input"
              value={entityId}
              onChange={(e) => setEntityId(e.target.value)}
              data-testid="pa-grant-entity-select"
            >
              <option value="">Choose an entity…</option>
              {entities.items.map((en) => (
                <option key={en.id} value={en.id}>
                  {en.data.name}
                </option>
              ))}
            </select>
          </div>
        )}
        {scopeKind === "brand" && (
          <div className="field">
            <label htmlFor="pa-grant-brand">Brand ID</label>
            <input
              id="pa-grant-brand"
              className="input"
              value={brandId}
              onChange={(e) => setBrandId(e.target.value)}
              data-testid="pa-grant-brand-input"
            />
          </div>
        )}

        <fieldset className="pa-check-group">
          <legend>Actions{template ? ` (within ${template.code}'s maximum)` : ""}</legend>
          {role && maximaError && <p className="warn-note">{maximaError}</p>}
          {role && maxima !== null && !maximum && (
            <p className="lead" data-testid="pa-grant-no-maximum">
              This role has no registered maximum, so it cannot carry goods actions.
            </p>
          )}
          {(role ? (maximum?.actions ?? []) : (meta?.actions.map((a) => a.code) ?? [])).map(
            (code) => (
              <label key={code} className="pa-check">
                <input
                  type="checkbox"
                  checked={actions.includes(code)}
                  onChange={() => toggle(actions, setActions, code)}
                  data-testid={`pa-grant-action-${code}`}
                />
                {meta?.actions.find((a) => a.code === code)?.label ?? code}
              </label>
            ),
          )}
        </fieldset>

        <fieldset className="pa-check-group">
          <legend>Fields</legend>
          {(role ? (maximum?.fields ?? []) : (meta?.fields ?? [])).map((code) => (
            <label key={code} className="pa-check">
              <input
                type="checkbox"
                checked={fields.includes(code)}
                onChange={() => toggle(fields, setFields, code)}
                data-testid={`pa-grant-field-${code}`}
              />
              {code}
            </label>
          ))}
        </fieldset>

        <div className="field">
          <label htmlFor="pa-grant-from">Starts</label>
          <input
            id="pa-grant-from"
            className="input"
            type="datetime-local"
            value={effectiveFrom}
            onChange={(e) => setEffectiveFrom(e.target.value)}
            data-testid="pa-grant-from-input"
          />
        </div>
        <div className="field">
          <label htmlFor="pa-grant-reason">Reason</label>
          <input
            id="pa-grant-reason"
            className="input"
            value={reasonCode}
            onChange={(e) => setReasonCode(e.target.value)}
            data-testid="pa-grant-reason-input"
          />
        </div>
        <button className="btn btn-cta" onClick={submit} data-testid="pa-grant-submit-button">
          <ShieldCheck size={15} /> Grant
        </button>
      </div>
    </div>
  );
}

function GrantsTable({
  meta,
  grants,
  renderActions,
}: {
  meta: AdminMeta | null;
  grants: UserGrant[];
  renderActions?: ((grant: UserGrant) => ReactNode) | undefined;
}) {
  return (
    <div className="table-wrap">
      <table className="data" data-testid="pa-grants-table">
        <thead>
          <tr>
            <th>Role</th>
            <th>Scope</th>
            <th>Actions</th>
            <th>Fields</th>
            <th>Since</th>
            {renderActions && <th />}
          </tr>
        </thead>
        <tbody>
          {grants.length === 0 ? (
            <tr data-testid="pa-grants-empty">
              <td colSpan={renderActions ? 6 : 5}>No grants.</td>
            </tr>
          ) : (
            grants.map((g) => (
              <tr key={g.id} data-testid={`pa-grant-row-${g.id}`}>
                <td>{g.role_code}</td>
                <td>{scopeLabel(meta, g.scope)}</td>
                <td>{g.actions.actions.join(", ") || "—"}</td>
                <td>{g.fields.join(", ") || "—"}</td>
                <td>{fmtDate(g.effective_from)}</td>
                {renderActions && <td style={{ textAlign: "right" }}>{renderActions(g)}</td>}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function GrantsTab({
  staff,
  login,
}: {
  staff: ResourceDTO<StaffData>;
  login: ReturnType<typeof useLoginForPerson>;
}) {
  const { session } = useAuth();
  const canManage = hold(session, "access.manage");
  const meta = useAdminMeta();
  const [adding, setAdding] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  if (!login.summary) {
    return (
      <div data-testid="pa-grants-tab">
        <p className="lead" data-testid="pa-grants-needs-login">
          {staff.data.display_name} has no login yet — create one on the Login tab before granting
          roles.
        </p>
      </div>
    );
  }

  const grants = login.detail.doc?.data.grants ?? [];

  async function revoke(grantId: string) {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/auth/admin/users/${login.summary!.id}/grants`, {
          grants: [{ revokes: grantId }],
          reason_code: "GRANT_REMOVED",
          ...goodsMeta(login.detail.doc?.revision),
        }),
      );
      setOk("Grant removed.");
      setConfirmRevoke(null);
      login.detail.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  return (
    <div data-testid="pa-grants-tab">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Roles and grants</h3>
        <div className="spacer" />
        {canManage && !adding && (
          <button
            className="btn btn-cta"
            onClick={() => setAdding(true)}
            data-testid="pa-grant-add-button"
          >
            <Plus size={15} /> Add grant
          </button>
        )}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {adding && meta && (
        <GrantEditor
          meta={meta}
          userId={login.summary.id}
          revision={login.detail.doc?.revision}
          stepUp={stepUp}
          onDone={() => {
            setAdding(false);
            setOk("Grant added.");
            login.detail.reload();
          }}
        />
      )}
      <GrantsTable
        meta={meta}
        grants={grants}
        renderActions={
          canManage
            ? (g) =>
                confirmRevoke === g.id ? (
                  <>
                    <span style={{ marginRight: 8 }}>Remove this grant?</span>
                    <button
                      className="btn btn-sm"
                      onClick={() => revoke(g.id)}
                      data-testid={`pa-grant-revoke-confirm-${g.id}`}
                    >
                      Yes, remove
                    </button>
                    <button className="btn btn-sm" onClick={() => setConfirmRevoke(null)}>
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    className="btn btn-sm"
                    onClick={() => setConfirmRevoke(g.id)}
                    data-testid={`pa-grant-revoke-${g.id}`}
                  >
                    <Trash2 size={13} /> Remove
                  </button>
                )
            : undefined
        }
      />
    </div>
  );
}

function EffectiveAuthorityTab({
  staff,
  login,
}: {
  staff: ResourceDTO<StaffData>;
  login: ReturnType<typeof useLoginForPerson>;
}) {
  const meta = useAdminMeta();
  if (!login.summary) {
    return (
      <div data-testid="pa-authority-tab">
        <p className="lead">
          {staff.data.display_name} has no login, so nothing here can be exercised.
        </p>
      </div>
    );
  }
  const grants = login.detail.doc?.data.grants ?? [];
  const rows = grants.flatMap((g) =>
    (g.actions.actions.length ? g.actions.actions : ["—"]).map((action) => ({ grant: g, action })),
  );
  return (
    <div data-testid="pa-authority-tab">
      <p className="lead">What this login may do right now, and which grant gives it.</p>
      <div className="table-wrap">
        <table className="data" data-testid="pa-authority-table">
          <thead>
            <tr>
              <th>Site</th>
              <th>SBU</th>
              <th>Brand</th>
              <th>Action</th>
              <th>Fields</th>
              <th>Granted by</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr data-testid="pa-authority-empty">
                <td colSpan={6}>No live authority.</td>
              </tr>
            ) : (
              rows.map(({ grant, action }, i) => (
                <tr key={`${grant.id}-${action}-${i}`}>
                  <td>
                    {grant.scope.scope_kind === "site" ? siteName(meta, grant.scope.site_id) : "—"}
                  </td>
                  <td>
                    {grant.scope.scope_kind === "sbu"
                      ? (meta?.sbus.find((s) => s.id === grant.scope.sbu_id)?.code ??
                        grant.scope.sbu_id)
                      : "—"}
                  </td>
                  <td>{grant.scope.scope_kind === "brand" ? grant.scope.brand_id : "—"}</td>
                  <td>{action}</td>
                  <td>{grant.fields.join(", ") || "—"}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {grant.role_code} · {grant.id.slice(0, 8)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// E247 (ticket 03D): the person's own history, then their login's - which
// carries every grant change and each privileged change's acknowledgement.
// Reading history never acknowledges or closes anything.
function HistoryTab({
  staff,
  login,
}: {
  staff: ResourceDTO<StaffData>;
  login: ReturnType<typeof useLoginForPerson>;
}) {
  const { session } = useAuth();
  const canReview = hold(session, "access.review");
  if (!hold(session, "audit.view")) {
    return (
      <div className="card section-card" data-testid="pa-history-tab">
        <p className="lead" data-testid="pa-history-no-access">
          You cannot read history.
        </p>
      </div>
    );
  }
  return (
    <div data-testid="pa-history-tab">
      <AdministrativeHistory
        subjectKind="staff"
        subjectId={staff.id}
        testId="pa-person-history"
        title="Person history"
      />
      {login.summary && (
        <AdministrativeHistory
          subjectKind="user"
          subjectId={login.summary.id}
          testId="pa-login-history"
          title="Login and grant history"
          eventLink={(event) => privilegedReviewLink(event, canReview)}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Person detail (tabs: Person, Login, Assignment, Roles and grants,
// Effective authority, History)
// --------------------------------------------------------------------------

const PERSON_TABS = ["person", "login", "assignment", "grants", "authority", "history"] as const;
type PersonTab = (typeof PERSON_TABS)[number];

const PERSON_TAB_LABEL: Record<PersonTab, string> = {
  person: "Person",
  login: "Login",
  assignment: "Assignment",
  grants: "Roles and grants",
  authority: "Effective authority",
  history: "History",
};

function PersonDetailView({
  staffId,
  tab,
  onTab,
  onBack,
}: {
  staffId: string;
  tab: PersonTab;
  onTab: (t: PersonTab) => void;
  onBack: () => void;
}) {
  const { doc, loading, denied, failure, reload } = useResourceDoc<StaffData>(
    `/goods-v1/auth/admin/staff/${staffId}`,
  );
  const login = useLoginForPerson(doc?.data.human_id ?? "");

  if (denied) return <Denied what="person" />;
  if (loading) return <div data-testid="pa-person-loading">Loading…</div>;
  if (failure) return <div className="warn-note">{failure}</div>;
  if (!doc) return null;

  return (
    <div data-testid="pa-person-detail">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <button className="btn btn-sm" onClick={onBack} data-testid="person-back-button">
          ← People
        </button>
        <h3 className="h3" style={{ marginLeft: 8 }}>
          {doc.data.display_name}
        </h3>
        <span className="chip chip-blue" data-testid="pa-has-login-badge" style={{ marginLeft: 8 }}>
          {login.summary ? "Has login" : "No login"}
        </span>
      </div>
      <div className="pa-tabs" role="tablist" aria-label="Person">
        {PERSON_TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            className={`pa-tab ${tab === t ? "active" : ""}`}
            onClick={() => onTab(t)}
            data-testid={`person-tab-${t}`}
          >
            {PERSON_TAB_LABEL[t]}
          </button>
        ))}
      </div>
      <div className="pa-tab-panel">
        {tab === "person" && <PersonTab doc={doc} reload={reload} />}
        {tab === "login" && <LoginTab staff={doc} login={login} />}
        {tab === "assignment" && <AssignmentTab doc={doc} reload={reload} />}
        {tab === "grants" && <GrantsTab staff={doc} login={login} />}
        {tab === "authority" && <EffectiveAuthorityTab staff={doc} login={login} />}
        {tab === "history" && <HistoryTab staff={doc} login={login} />}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Roles (E075-E080, ticket 03C): a tenant-wide administrator creates and edits
// roles and sets each role's maximum - the actions, fields and scope kinds any
// grant of it may carry, never beyond its registered template. Every change
// needs a fresh password confirmation and is a privileged change; the server
// refuses a stale revision and changes nothing when any part is refused.
// --------------------------------------------------------------------------

const blankRole = { code: "", name: "", description: "" };

function RolesPanel() {
  const meta = useAdminMeta();
  const { items, loading, denied, failure, reload } = useResourceList<RoleData>(
    "/goods-v1/auth/admin/roles",
  );
  const [refresh, setRefresh] = useState(0);
  const { maxima, error: maximaError, version: maximaVersion } = useRoleMaxima(refresh);
  const [form, setForm] = useState(blankRole);
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  if (denied) return <Denied what="roles" />;

  function changed(message: string) {
    setOk(message);
    reload();
    setRefresh((n) => n + 1);
  }

  async function create() {
    setError("");
    setOk("");
    const code = form.code.trim();
    const name = form.name.trim();
    if (!code || !name) {
      setError("A role needs a code and a name.");
      return;
    }
    const body: RoleCreateBody = {
      ...goodsMeta(),
      code,
      name,
      description: form.description.trim() || null,
    };
    try {
      await stepUp.guarded(() =>
        api.post<ResourceDTO<RoleData>>("/goods-v1/auth/admin/roles", body),
      );
      const known = meta?.role_templates.some((t) => t.code === code);
      setForm(blankRole);
      setAdding(false);
      changed(
        known
          ? `Role ${code} created.`
          : `Role ${code} created. It has no registered maximum, so it cannot carry goods actions yet.`,
      );
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  const open = items.find((r) => r.id === openId);
  // Role writes need a tenant-wide administrator; the server says so per row.
  const canCreate = items.some((r) => r.allowed_actions.includes("update"));

  return (
    <div data-testid="pa-roles-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Roles</h3>
        <div className="spacer" />
        {!adding && canCreate && (
          <button
            className="btn btn-cta"
            onClick={() => setAdding(true)}
            data-testid="pa-role-add-button"
          >
            <Plus size={13} /> New role
          </button>
        )}
      </div>
      <Feedback error={error || maximaError} ok={ok} />
      {stepUp.dialog}
      {adding && (
        <div className="card section-card" data-testid="pa-role-create-form">
          <div className="form-grid wide-form">
            <div className="field">
              <label htmlFor="pa-role-code">Code</label>
              <input
                id="pa-role-code"
                className="input"
                maxLength={40}
                value={form.code}
                onChange={(e) => setForm({ ...form, code: e.target.value })}
                data-testid="pa-role-code-input"
              />
            </div>
            <div className="field">
              <label htmlFor="pa-role-name">Name</label>
              <input
                id="pa-role-name"
                className="input"
                maxLength={80}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                data-testid="pa-role-name-input"
              />
            </div>
            <div className="field">
              <label htmlFor="pa-role-description">Description</label>
              <input
                id="pa-role-description"
                className="input"
                maxLength={240}
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                data-testid="pa-role-description-input"
              />
            </div>
            <p className="lead">
              A role can carry goods actions only when its code has a registered maximum. The code
              never changes after the role is created.
            </p>
            <div>
              <button className="btn btn-cta" onClick={create} data-testid="pa-role-create-submit">
                <Save size={15} /> Create role
              </button>{" "}
              <button
                className="btn"
                onClick={() => {
                  setAdding(false);
                  setForm(blankRole);
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="pa-roles-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              <th>Status</th>
              <th>Maximum</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={5}>Loading…</td>
              </tr>
            ) : failure ? (
              <tr>
                <td colSpan={5} className="warn-note">
                  {failure}
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td colSpan={5}>No roles yet.</td>
              </tr>
            ) : (
              items.map((row) => {
                const maximum = maxima?.find((m) => m.role_code === row.data.code);
                return (
                  <tr key={row.id} data-testid={`pa-role-row-${row.data.code}`}>
                    <td className="mono">{row.data.code}</td>
                    <td>{row.data.name}</td>
                    <td>
                      <span className={`chip chip-${row.data.active ? "green" : "amber"}`}>
                        {row.data.active ? "active" : "inactive"}
                      </span>
                    </td>
                    <td>
                      {maxima === null
                        ? "…"
                        : maximum
                          ? `${maximum.actions.length} actions, ${maximum.fields.length} fields`
                          : "No registered maximum"}
                    </td>
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={() => setOpenId(openId === row.id ? null : row.id)}
                        data-testid={`pa-role-open-${row.data.code}`}
                      >
                        <Pencil size={13} /> {openId === row.id ? "Close" : "Open"}
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      {/* Shown only once this refresh's maxima have loaded, and re-seeded from
          each load and each new revision, so it never edits from a stale ceiling. */}
      {open && maxima !== null && (
        <RoleDetail
          key={`${open.id}:${open.revision}:${maximaVersion}`}
          role={open}
          meta={meta}
          maximum={maxima?.find((m) => m.role_code === open.data.code)}
          stepUp={stepUp}
          onChanged={(message) => {
            setError("");
            changed(message);
          }}
        />
      )}
    </div>
  );
}

function RoleDetail({
  role,
  meta,
  maximum,
  stepUp,
  onChanged,
}: {
  role: ResourceDTO<RoleData>;
  meta: AdminMeta | null;
  maximum: RoleMaximum | undefined;
  stepUp: ReturnType<typeof useStepUp>;
  onChanged: (message: string) => void;
}) {
  const template = actionsForRole(meta, role.data.code);
  const [name, setName] = useState(role.data.name);
  const [description, setDescription] = useState(role.data.description ?? "");
  const [active, setActive] = useState(role.data.active);
  const [kinds, setKinds] = useState<string[]>(maximum?.scope_kinds ?? []);
  const [actions, setActions] = useState<string[]>(maximum?.actions ?? []);
  const [fields, setFields] = useState<string[]>(maximum?.fields ?? []);
  const [error, setError] = useState("");
  const url = `/goods-v1/auth/admin/roles`;
  const canEdit = role.allowed_actions.includes("update");
  const canSetMaximum = role.allowed_actions.includes("access") && template !== undefined;
  const { session } = useAuth();
  const canReadHistory = hold(session, "audit.view");
  const canReview = hold(session, "access.review");

  function toggle(list: string[], set: (v: string[]) => void, value: string) {
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  }

  async function saveDetails() {
    setError("");
    if (!name.trim()) {
      setError("A role needs a name.");
      return;
    }
    const body: RoleUpdateBody = {
      ...goodsMeta(),
      expected_revision: role.revision,
      name: name.trim(),
      description: description.trim() || null,
      active,
    };
    try {
      await stepUp.guarded(() => api.patch(`${url}/${role.id}`, body));
      onChanged(`Role ${role.data.code} saved.`);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function saveMaximum() {
    setError("");
    if ((actions.length > 0 || fields.length > 0) && kinds.length === 0) {
      setError("Choose at least one scope kind for these actions and fields.");
      return;
    }
    const body: RoleAccessBody = {
      ...goodsMeta(),
      expected_revision: role.revision,
      grants: kinds.map((kind) => ({
        scope: { scope_kind: kind as ScopeKind },
        actions: { actions: [...actions].sort() },
        fields: [...fields].sort(),
      })),
    };
    try {
      await stepUp.guarded(() =>
        api.put(`${url}/${encodeURIComponent(role.data.code)}/access`, body),
      );
      onChanged(`Maximum for ${role.data.code} saved.`);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  const label = (code: string) => meta?.actions.find((a) => a.code === code)?.label ?? code;

  return (
    <div className="card section-card" data-testid={`pa-role-detail-${role.data.code}`}>
      <h3 className="h3">
        {role.data.code} — {role.data.name}
      </h3>
      <Feedback error={error} ok="" />
      <div className="form-grid wide-form">
        <div className="field">
          <label htmlFor="pa-role-edit-name">Name</label>
          <input
            id="pa-role-edit-name"
            className="input"
            maxLength={80}
            value={name}
            disabled={!canEdit}
            onChange={(e) => setName(e.target.value)}
            data-testid="pa-role-edit-name-input"
          />
        </div>
        <div className="field">
          <label htmlFor="pa-role-edit-description">Description</label>
          <input
            id="pa-role-edit-description"
            className="input"
            maxLength={240}
            value={description}
            disabled={!canEdit}
            onChange={(e) => setDescription(e.target.value)}
            data-testid="pa-role-edit-description-input"
          />
        </div>
        <label className="pa-check">
          <input
            type="checkbox"
            checked={active}
            disabled={!canEdit}
            onChange={(e) => setActive(e.target.checked)}
            data-testid="pa-role-edit-active"
          />
          Active (an inactive role gives its holders nothing)
        </label>
        {canEdit && (
          <div>
            <button className="btn btn-cta" onClick={saveDetails} data-testid="pa-role-edit-submit">
              <Save size={15} /> Save role
            </button>
          </div>
        )}
      </div>

      <h3 className="h3">Maximum</h3>
      {template === undefined ? (
        <p className="lead" data-testid="pa-role-no-maximum">
          This role has no registered maximum, so it cannot carry goods actions and cannot be
          granted to anyone.
        </p>
      ) : (
        <div className="form-grid wide-form" data-testid="pa-role-maximum">
          <p className="lead">
            Anyone granted {role.data.code} gets at most what is ticked here. Only what the role's
            registered maximum allows is offered. Saving ends the open sessions of everyone who
            holds this role, so the new limit applies at once.
          </p>
          <fieldset className="pa-check-group">
            <legend>Scope kinds</legend>
            {template.scope_kinds.map((kind) => (
              <label key={kind} className="pa-check">
                <input
                  type="checkbox"
                  checked={kinds.includes(kind)}
                  disabled={!canSetMaximum}
                  onChange={() => toggle(kinds, setKinds, kind)}
                  data-testid={`pa-role-kind-${kind}`}
                />
                {kind}
              </label>
            ))}
          </fieldset>
          <fieldset className="pa-check-group">
            <legend>Actions</legend>
            {template.actions.map((code) => (
              <label key={code} className="pa-check">
                <input
                  type="checkbox"
                  checked={actions.includes(code)}
                  disabled={!canSetMaximum}
                  onChange={() => toggle(actions, setActions, code)}
                  data-testid={`pa-role-action-${code}`}
                />
                {label(code)}
              </label>
            ))}
          </fieldset>
          <fieldset className="pa-check-group">
            <legend>Fields</legend>
            {template.fields.length === 0 ? (
              <span className="lead">This role's maximum allows no protected fields.</span>
            ) : (
              template.fields.map((code) => (
                <label key={code} className="pa-check">
                  <input
                    type="checkbox"
                    checked={fields.includes(code)}
                    disabled={!canSetMaximum}
                    onChange={() => toggle(fields, setFields, code)}
                    data-testid={`pa-role-field-${code}`}
                  />
                  {code}
                </label>
              ))
            )}
          </fieldset>
          {canSetMaximum && (
            <div>
              <button
                className="btn btn-cta"
                onClick={saveMaximum}
                data-testid="pa-role-maximum-submit"
              >
                <ShieldCheck size={15} /> Save maximum
              </button>
            </div>
          )}
        </div>
      )}
      {canReadHistory && (
        <AdministrativeHistory
          subjectKind="role"
          subjectId={role.id}
          testId="pa-role-history"
          title="Role history"
          eventLink={(event) => privilegedReviewLink(event, canReview)}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Privileged changes (E084, E237)
// --------------------------------------------------------------------------

function PrivilegedChangesPanel({
  changeId,
  onShowAll,
}: {
  changeId: string | null;
  onShowAll: () => void;
}) {
  const { session } = useAuth();
  const canReview = hold(session, "access.review");
  // The owned follow-up and a history entry open one change by id (ticket 03D);
  // the server still applies the reader's own review scope to it.
  const { items, loading, denied, failure, reload } = useResourceList<PrivilegedChangeData>(
    changeId
      ? `/goods-v1/auth/admin/privileged-changes?${new URLSearchParams({ id: changeId })}`
      : "/goods-v1/auth/admin/privileged-changes",
  );
  const [noteFor, setNoteFor] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  if (denied) return <Denied what="privileged changes" />;
  if (!canReview) {
    return (
      <div className="card section-card" data-testid="pa-privileged-no-access">
        You cannot review privileged changes.
      </div>
    );
  }

  async function acknowledge(id: string) {
    setError("");
    setOk("");
    if (!note.trim()) {
      setError("Add a note before acknowledging.");
      return;
    }
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/auth/admin/privileged-changes/${id}/review`, {
          note: note.trim(),
          ...goodsMeta(),
        }),
      );
      setOk("Acknowledged.");
      setNoteFor(null);
      setNote("");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  return (
    <div data-testid="pa-privileged-panel">
      <h3 className="h3">Privileged changes</h3>
      {changeId && (
        <p className="lead" data-testid="pa-privileged-one">
          Showing one change.{" "}
          <button className="btn btn-sm" onClick={onShowAll} data-testid="pa-privileged-show-all">
            Show all
          </button>
        </p>
      )}
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      <div className="table-wrap">
        <table className="data" data-testid="privileged-changes-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>What</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={5}>Loading…</td>
              </tr>
            ) : failure ? (
              <tr>
                <td colSpan={5} className="warn-note">
                  {failure}
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr data-testid="privileged-changes-empty">
                <td colSpan={5}>
                  {changeId ? "That change was not found." : "No privileged changes yet."}
                </td>
              </tr>
            ) : (
              items.map((row) => (
                <tr key={row.id} data-testid={`privileged-row-${row.id}`}>
                  <td>{fmtDate(row.data.event_at)}</td>
                  <td>{row.data.actor_name ?? row.data.service_code ?? "—"}</td>
                  <td>
                    {row.data.action}{" "}
                    <span className="mono" style={{ fontSize: 12 }}>
                      {row.data.subject_key}
                    </span>
                  </td>
                  <td>
                    <span className={`chip chip-${row.state === "reviewed" ? "green" : "amber"}`}>
                      {row.state}
                    </span>
                    {row.data.reviews.length > 0 && (
                      <div className="lead" style={{ fontSize: 12 }}>
                        {row.data.reviews
                          .map((r) => `${r.reviewer_name ?? r.reviewer_id}: ${r.note}`)
                          .join("; ")}
                      </div>
                    )}
                  </td>
                  <td>
                    {row.allowed_actions.includes("review") &&
                      (noteFor === row.id ? (
                        <div className="form-grid">
                          <input
                            className="input"
                            placeholder="Acknowledgement note"
                            value={note}
                            onChange={(e) => setNote(e.target.value)}
                            data-testid={`privileged-note-input-${row.id}`}
                          />
                          <button
                            className="btn btn-sm"
                            onClick={() => acknowledge(row.id)}
                            data-testid={`privileged-acknowledge-confirm-${row.id}`}
                          >
                            <CheckCircle2 size={13} /> Acknowledge
                          </button>
                          <button className="btn btn-sm" onClick={() => setNoteFor(null)}>
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          className="btn btn-sm"
                          onClick={() => {
                            setNoteFor(row.id);
                            setNote("");
                          }}
                          data-testid={`privileged-acknowledge-${row.id}`}
                        >
                          Acknowledge
                        </button>
                      ))}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

type Panel = "people" | "roles" | "privileged";

export function PeopleAccessPage() {
  const [params, setParams] = useSearchParams();
  const panel = (params.get("panel") as Panel | null) ?? "people";
  const personId = params.get("person");
  const tab = (params.get("tab") as PersonTab | null) ?? "person";
  const { session } = useAuth();

  function selectPanel(next: Panel) {
    setParams({ panel: next });
  }

  function openPerson(id: string) {
    setParams({ panel: "people", person: id, tab: "person" });
  }

  function selectTab(next: PersonTab) {
    setParams((prev) => {
      const merged = new URLSearchParams(prev);
      merged.set("tab", next);
      return merged;
    });
  }

  function backToList() {
    setParams({ panel: "people" });
  }

  return (
    <div className="page-pad">
      <PageHeader />
      <div className="org-layout pa-layout">
        <nav className="org-nav" aria-label="People and access">
          <div className="org-nav-group">
            <h4>People and access</h4>
            <ul className="org-nav-list">
              <li>
                <button
                  className={`org-nav-item ${panel === "people" ? "active" : ""}`}
                  onClick={() => selectPanel("people")}
                  data-testid="pa-nav-people"
                >
                  <Users size={15} /> People
                </button>
              </li>
              {hold(session, "access.manage") && (
                <li>
                  <button
                    className={`org-nav-item ${panel === "roles" ? "active" : ""}`}
                    onClick={() => selectPanel("roles")}
                    data-testid="pa-nav-roles"
                  >
                    <KeyRound size={15} /> Roles
                  </button>
                </li>
              )}
              {hold(session, "access.review") && (
                <li>
                  <button
                    className={`org-nav-item ${panel === "privileged" ? "active" : ""}`}
                    onClick={() => selectPanel("privileged")}
                    data-testid="pa-nav-privileged"
                  >
                    <ShieldCheck size={15} /> Privileged changes
                  </button>
                </li>
              )}
            </ul>
          </div>
        </nav>
        <div className="org-detail">
          {panel === "people" && !personId && <PeopleListPanel onOpen={openPerson} />}
          {panel === "people" && personId && (
            <PersonDetailView staffId={personId} tab={tab} onTab={selectTab} onBack={backToList} />
          )}
          {panel === "roles" && <RolesPanel />}
          {panel === "privileged" && (
            <PrivilegedChangesPanel
              changeId={params.get("change")}
              onShowAll={() => selectPanel("privileged")}
            />
          )}
        </div>
      </div>
    </div>
  );
}
