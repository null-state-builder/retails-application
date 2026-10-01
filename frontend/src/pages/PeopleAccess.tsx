// People and access: staff and login history share one workspace with the
// canonical role-policy and scoped-assignment editors. Server decisions govern
// every read and write; session display hints only shape the controls.
//
import { useEffect, useState } from "react";
import {
  Ban,
  CheckCircle2,
  KeyRound,
  Pencil,
  Save,
  ShieldCheck,
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
import { AssignmentReconciliationPanel } from "./AssignmentReconciliation";
import { AdministrativeHistory } from "../components/AdministrativeHistory";
import { privilegedReviewLink } from "../lib/administrativeHistory";
import {
  UnifiedAssignmentsTab,
  UnifiedRolePolicyPanel,
  UnifiedWorkflowPolicyPanel,
} from "./UnifiedAccess";
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
  paths["/api/auth/admin/staff"]["post"]["requestBody"]
>["content"]["application/json"];

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

interface AdminMeta {
  sites: AdminMetaSite[];
}

// --------------------------------------------------------------------------
// Small shared bits local to this screen
// --------------------------------------------------------------------------

function useAdminMeta() {
  const { value } = useResourceListRaw<AdminMeta>("/auth/admin/meta");
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

function siteName(meta: AdminMeta | null, siteId: string | null): string {
  if (!siteId) return "No primary site assigned";
  return meta?.sites.find((s) => s.id === siteId)?.name ?? siteId;
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
  registration_email: "",
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
  const { items, canCreateUnplaced, loading, denied, failure, reload } =
    useStaffList("/auth/admin/staff");
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
        ...(form.registration_email ? { registration_email: form.registration_email } : {}),
        display_name: form.display_name,
        mobile: form.mobile || null,
        ...(form.site_id
          ? {
              site_id: form.site_id,
              effective_from: new Date(form.effective_from).toISOString(),
            }
          : {}),
      };
      const created = await api.post<ResourceDTO<StaffData>>("/auth/admin/staff", body);
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
              <label htmlFor="person-registration-email">Signup email (reserved codes only)</label>
              <input
                id="person-registration-email"
                type="email"
                className="input"
                value={form.registration_email}
                onChange={(e) => setForm({ ...form, registration_email: e.target.value })}
              />
              <small>For a proposed signup person, use their saved code and email.</small>
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
      await api.patch(`/auth/admin/staff/${doc.id}`, {
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
        api.post(`/auth/admin/staff/${doc.id}/retire`, {
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
      await api.post(`/auth/admin/staff/${doc.id}/assign`, {
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
  const list = useResourceList<UserData>("/auth/admin/users");
  const summary = list.items.find((u) => u.data.human_id === humanId) ?? null;
  const detail = useResourceDoc<UserData>(summary ? `/auth/admin/users/${summary.id}` : null);
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
        api.post("/auth/admin/users", {
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
        api.patch(`/auth/admin/users/${login.summary!.id}`, {
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

// Access assignments and role policy are edited through the canonical unified access API.

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

const PERSON_TABS = ["person", "login", "assignment", "grants", "history"] as const;
type PersonTab = (typeof PERSON_TABS)[number];

const PERSON_TAB_LABEL: Record<PersonTab, string> = {
  person: "Person",
  login: "Login",
  assignment: "Assignment",
  grants: "Access assignments",
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
    `/auth/admin/staff/${staffId}`,
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
        {tab === "grants" && (
          <UnifiedAssignmentsTab
            key={login.summary?.id ?? staffId}
            userId={login.summary?.id ?? null}
            personName={doc.data.display_name}
          />
        )}
        {tab === "history" && <HistoryTab staff={doc} login={login} />}
      </div>
    </div>
  );
}

// Role policy is edited through the canonical unified access API.

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
      ? `/auth/admin/privileged-changes?${new URLSearchParams({ id: changeId })}`
      : "/auth/admin/privileged-changes",
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
        api.post(`/auth/admin/privileged-changes/${id}/review`, {
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

type Panel = "people" | "roles" | "workflow" | "privileged" | "reconciliation";

export function PeopleAccessPage() {
  const [params, setParams] = useSearchParams();
  const panel = (params.get("panel") as Panel | null) ?? "people";
  const personId = params.get("person");
  const requestedTab = params.get("tab");
  const tab: PersonTab =
    requestedTab === "authority"
      ? "grants"
      : (PERSON_TABS.find((value) => value === requestedTab) ?? "person");
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
              {hold(session, "access.manage") && (
                <li>
                  <button
                    className={`org-nav-item ${panel === "workflow" ? "active" : ""}`}
                    onClick={() => selectPanel("workflow")}
                    data-testid="pa-nav-workflow"
                  >
                    <Pencil size={15} /> Workflow thresholds
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
              {hold(session, "access.manage") && (
                <li>
                  <button
                    className={`org-nav-item ${panel === "reconciliation" ? "active" : ""}`}
                    onClick={() => selectPanel("reconciliation")}
                    data-testid="pa-nav-reconciliation"
                  >
                    <ShieldCheck size={15} /> Migration review
                  </button>
                </li>
              )}
            </ul>
          </div>
        </nav>
        <div className="org-detail">
          {panel === "reconciliation" && <AssignmentReconciliationPanel />}
          {panel === "people" && !personId && <PeopleListPanel onOpen={openPerson} />}
          {panel === "people" && personId && (
            <PersonDetailView staffId={personId} tab={tab} onTab={selectTab} onBack={backToList} />
          )}
          {panel === "roles" &&
            (hold(session, "access.manage") ? (
              <UnifiedRolePolicyPanel />
            ) : (
              <Denied what="role policy" />
            ))}
          {panel === "workflow" &&
            (hold(session, "access.manage") ? (
              <UnifiedWorkflowPolicyPanel />
            ) : (
              <Denied what="workflow policy" />
            ))}
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
