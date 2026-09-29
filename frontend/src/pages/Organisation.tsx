// Organisation setup (ticket 02): legal entities, registrations, sites, SBUs
// and location trees, plus a site's readiness and closure. One area, a left
// list of entities/registrations/sites, a detail panel on the right — the
// owner does this once, then rarely (orchestrator UX brief).
//
// Enforcement lives on the server (design §4.2): every screen here is a thin
// client over `/api/goods-v1/masters/*`. `session.actions` only steers which controls
// this build shows — a person who lacks the grant still gets the uniform
// hidden-object 404/403 from the server if they reach for it anyway, and a
// wrong-site read answers the same "not found" as a record that never
// existed (ADR-0003).
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  Building2,
  History,
  Landmark,
  Lock,
  Pencil,
  Plus,
  Save,
  ShieldCheck,
  Store as StoreIcon,
  Warehouse,
  X,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api, apiErrorCode, apiErrorMessage, goodsMeta, type ApiRead } from "../lib/api";
import type { paths } from "../lib/api-schema";
import { openHistoryAfterRetire, parseMasterHistory } from "../lib/administrativeHistory";
import { AdministrativeHistory } from "../components/AdministrativeHistory";
import { apiErrorIssues, type ApiIssue } from "../lib/goodsAcceptance";
import {
  Denied,
  Feedback,
  Field,
  hold,
  PickerField,
  usePagedPicker,
  useResourceDoc,
  useResourceList,
  useStepUp,
  type ResourceDTO,
} from "../lib/goodsScreen";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import "./Organisation.css";

// --------------------------------------------------------------------------
// Wire shapes (design §6.1 ResourceDTO<T> / Page<T>, shared in
// `lib/goodsScreen.tsx`). Hand-typed: these views hand-build dict responses
// (not DRF serializers), so most of them are still undocumented in the
// OpenAPI schema this app generates from — see the backend's
// `masters/goods_views.py` module docstring additions from this ticket for
// the ones that now are.
// --------------------------------------------------------------------------

interface EntityData {
  code: string;
  name: string;
  pan?: string | null;
  address?: unknown;
  books_code?: string | null;
}

interface RegistrationData {
  entity_id: string;
  gstin: string;
  state_code: string;
  state_name: string;
  effective_from?: string | null;
}

const SITE_TYPES = ["warehouse", "store", "DC", "concession", "virtual"] as const;
const OPERATIONS = ["receive", "hold", "transfer", "sell"] as const;

interface SiteData {
  code: string;
  name: string;
  aliases?: string[];
  city?: string | null;
  state?: string | null;
  country?: string;
  type: (typeof SITE_TYPES)[number];
  entity_id: string | null;
  registration_id: string | null;
  counter_count?: number;
  partner_ref?: string | null;
  opening_date?: string | null;
  linked_warehouse_id?: string | null;
  permitted_operations: string[];
  brand_ids?: number[];
}

interface LocationData {
  site_id: string;
  parent_id: string | null;
  name: string;
  kind: string;
  system: boolean;
}

// E218/E246 (ticket 02A): the generated client carries the SBU shapes.
type SbuRetireOperation = paths["/api/goods-v1/masters/stores/{site_id}/sbus/{id}/retire"]["post"];
type SbuResource = ApiRead<SbuRetireOperation["responses"][200]["content"]["application/json"]>;
type SbuData = ApiRead<NonNullable<SbuResource["data"]>>;
type SbuRetireBody = NonNullable<SbuRetireOperation["requestBody"]>["content"]["application/json"];

interface CheckItem {
  key: string;
  passed: boolean;
  required: boolean;
  overridable: boolean;
  reason: string | null;
}

interface Residuals {
  physical_qty: number | null;
  transit_qty: number | null;
  unvalued_qty: number | null;
  reserved_qty: number | null;
  open_exception_ids: string[];
  completeness: "complete" | "partial" | "unknown";
  reasons: { code: string; message?: string }[];
  cash_assessment: "not_assessed";
}

interface ReadinessData {
  site_id: string;
  lifecycle: "planned" | "opening" | "active" | "closing" | "closed";
  opening_setup_ready: boolean;
  goods_ready: boolean;
  sell_ready: boolean;
  non_trading_confirmed: boolean;
  checks: CheckItem[];
  residuals: Residuals;
}

// The non-system kinds a person may create by hand. The six protected system
// locations (receiving, quarantine, excess_hold, rtv_hold, transit_out,
// custody) are created once by site setup and never offered here — posting
// one of their kinds by hand would not be *that* system location (it would
// not carry `system: true`), so offering the kind at all would only invite a
// confusing duplicate name clash.
const CREATABLE_LOCATION_KINDS = ["zone", "rack", "bin", "floor", "backstore", "fixture"] as const;

const LOCATION_KIND_LABEL: Record<string, string> = {
  floor: "Floor",
  backstore: "Backstore",
  receiving: "Receiving",
  quarantine: "Quarantine",
  excess_hold: "Excess hold",
  rtv_hold: "RTV hold",
  transit_out: "Transit out",
  custody: "Custody",
  zone: "Zone",
  rack: "Rack",
  bin: "Bin",
  fixture: "Fixture",
};

// --------------------------------------------------------------------------
// Legal entities (E006-E010)
// --------------------------------------------------------------------------

const blankEntity = { code: "", name: "", pan: "", books_code: "" };

function EntitiesPanel({
  historySubject,
  historyRefresh,
  onHistory,
}: {
  historySubject: string | null;
  historyRefresh: string | null;
  onHistory: (id: string) => void;
}) {
  const { session } = useAuth();
  const canEdit = hold(session, "org.entity.manage");
  const canReadHistory = hold(session, "audit.view");
  const [showRetired, setShowRetired] = useState(false);
  const { items, loading, denied, failure, reload } = useResourceList<EntityData>(
    showRetired ? "/goods-v1/masters/entities?status=retired" : "/goods-v1/masters/entities",
  );
  const [form, setForm] = useState(blankEntity);
  const [editing, setEditing] = useState<ResourceDTO<EntityData> | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  function add() {
    setForm(blankEntity);
    setEditing(null);
    setOpen(true);
    setOk("");
    setError("");
  }

  function edit(row: ResourceDTO<EntityData>) {
    setForm({
      code: row.data.code,
      name: row.data.name,
      pan: row.data.pan ?? "",
      books_code: row.data.books_code ?? "",
    });
    setEditing(row);
    setOpen(true);
    setOk("");
    setError("");
  }

  async function save() {
    setError("");
    setOk("");
    const payload = {
      code: form.code,
      name: form.name,
      pan: form.pan,
      books_code: form.books_code,
    };
    try {
      await stepUp.guarded(() =>
        editing
          ? api.patch(`/goods-v1/masters/entities/${editing.id}`, {
              ...payload,
              ...goodsMeta(editing.revision),
            })
          : api.post("/goods-v1/masters/entities", { ...payload, ...goodsMeta() }),
      );
      setOpen(false);
      setOk(editing ? "Legal entity saved." : "Legal entity created.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function retire(row: ResourceDTO<EntityData>) {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/entities/${row.id}/retire`, {
          reason_code: "NO_LONGER_TRADING",
          ...goodsMeta(row.revision),
        }),
      );
      openHistoryAfterRetire(canReadHistory, row.id, onHistory);
      setOk(`${row.data.name} retired.`);
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (denied) return <Denied what="organisation" />;

  return (
    <div data-testid="org-entities-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Legal entities</h3>
        <div className="spacer" />
        {canReadHistory && (
          <button
            className="btn btn-sm"
            onClick={() => setShowRetired((shown) => !shown)}
            data-testid="entities-retired-toggle"
          >
            {showRetired ? "Show active" : "Show retired"}
          </button>
        )}
        {canEdit && (
          <button className="btn btn-cta" onClick={add} data-testid="entity-new-button">
            <Plus size={15} /> New legal entity
          </button>
        )}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {historySubject && (
        <AdministrativeHistory
          key={`entity:${historySubject}:${historyRefresh ?? ""}`}
          subjectKind="master"
          subjectId={`entity:${historySubject}`}
          refreshKey={historyRefresh}
          testId="entity-history"
        />
      )}
      {canEdit && open && (
        <div className="card section-card" data-testid="entity-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">{editing ? "Edit legal entity" : "Create legal entity"}</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="entity-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Code"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value })}
              data-testid="entity-code-input"
            />
            <input
              className="input"
              placeholder="Registered name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="entity-name-input"
            />
            <input
              className="input"
              placeholder="PAN (optional)"
              value={form.pan}
              onChange={(e) => setForm({ ...form, pan: e.target.value.toUpperCase() })}
              data-testid="entity-pan-input"
            />
            <input
              className="input"
              placeholder="Books code (optional)"
              value={form.books_code}
              onChange={(e) => setForm({ ...form, books_code: e.target.value })}
              data-testid="entity-books-code-input"
            />
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={!form.code || !form.name}
              data-testid="entity-save-button"
            >
              <Save size={15} /> Save legal entity
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="entities-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              <th>PAN</th>
              <th>Status</th>
              {(canEdit || canReadHistory) && <th />}
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
              <tr data-testid="entities-empty">
                <td colSpan={5}>No legal entities yet.</td>
              </tr>
            ) : (
              items.map((row) => (
                <tr key={row.id} data-testid={`entity-row-${row.data.code}`}>
                  <td>
                    <b className="mono">{row.data.code}</b>
                  </td>
                  <td>{row.data.name}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {row.data.pan || "—"}
                  </td>
                  <td>
                    <span className={`chip chip-${row.state === "active" ? "green" : "red"}`}>
                      {row.state}
                    </span>
                  </td>
                  {(canEdit || canReadHistory) && (
                    <td>
                      {canEdit && (
                        <>
                          <button
                            className="btn btn-sm"
                            onClick={() => edit(row)}
                            data-testid={`edit-entity-${row.data.code}`}
                          >
                            <Pencil size={13} /> Edit
                          </button>
                          {row.state === "active" && (
                            <button
                              className="btn btn-sm"
                              onClick={() => retire(row)}
                              data-testid={`retire-entity-${row.data.code}`}
                            >
                              Retire
                            </button>
                          )}
                        </>
                      )}
                      {canReadHistory && (
                        <button
                          className="btn btn-sm"
                          onClick={() => onHistory(row.id)}
                          data-testid={`history-entity-${row.data.code}`}
                        >
                          <History size={13} /> History
                        </button>
                      )}
                    </td>
                  )}
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
// Registrations / GSTINs (E011-E015)
// --------------------------------------------------------------------------

const blankRegistration = { entity_id: "", gstin: "", state_code: "", state_name: "" };

function RegistrationsPanel({
  historySubject,
  historyRefresh,
  onHistory,
}: {
  historySubject: string | null;
  historyRefresh: string | null;
  onHistory: (id: string) => void;
}) {
  const { session } = useAuth();
  const canEdit = hold(session, "org.entity.manage");
  const canReadHistory = hold(session, "audit.view");
  const [showRetired, setShowRetired] = useState(false);
  // The table below names each row's entity from this one default first page
  // — good enough while a tenant's entities fit on it, and never claimed as
  // more (`nameOf` below falls back to "—", not a guess).
  const entities = useResourceList<EntityData>("/goods-v1/masters/entities");
  const { items, loading, denied, failure, reload } = useResourceList<RegistrationData>(
    showRetired ? "/goods-v1/masters/gstins?status=retired" : "/goods-v1/masters/gstins",
  );
  const [form, setForm] = useState(blankRegistration);
  // The create/edit form's own entity choice searches and pages (ticket 02D)
  // rather than trusting the table's full first page to be everything. An
  // edit's starting selection borrows the same first page's name when it
  // happens to be on it; otherwise it is read through the entity's own detail.
  const entityPicker = usePagedPicker<"/goods-v1/masters/entities", ResourceDTO<EntityData>>(
    "/goods-v1/masters/entities",
    {},
    form.entity_id,
    (row) => row.data.name,
    {
      ...(entities.items.find((e) => e.id === form.entity_id)?.data.name
        ? { knownLabel: entities.items.find((e) => e.id === form.entity_id)?.data.name }
        : {}),
      detailPath: (id) => `/goods-v1/masters/entities/${id}`,
    },
  );
  const [editing, setEditing] = useState<ResourceDTO<RegistrationData> | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  function nameOf(entityId: string | null): string {
    return entities.items.find((e) => e.id === entityId)?.data.name ?? "—";
  }

  function add() {
    setForm({ ...blankRegistration, entity_id: entities.items[0]?.id ?? "" });
    setEditing(null);
    setOpen(true);
    setOk("");
    setError("");
  }

  function edit(row: ResourceDTO<RegistrationData>) {
    setForm({
      entity_id: row.data.entity_id,
      gstin: row.data.gstin,
      state_code: row.data.state_code,
      state_name: row.data.state_name,
    });
    setEditing(row);
    setOpen(true);
    setOk("");
    setError("");
  }

  async function save() {
    setError("");
    setOk("");
    const payload = {
      entity_id: form.entity_id,
      gstin: form.gstin,
      state_code: form.state_code,
      state_name: form.state_name,
      effective_from: new Date().toISOString(),
    };
    try {
      await stepUp.guarded(() =>
        editing
          ? api.patch(`/goods-v1/masters/gstins/${editing.id}`, {
              ...payload,
              ...goodsMeta(editing.revision),
            })
          : api.post("/goods-v1/masters/gstins", { ...payload, ...goodsMeta() }),
      );
      setOpen(false);
      setOk(editing ? "Registration saved." : "Registration created.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function retire(row: ResourceDTO<RegistrationData>) {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/gstins/${row.id}/retire`, {
          reason_code: "NO_LONGER_TRADING",
          ...goodsMeta(row.revision),
        }),
      );
      openHistoryAfterRetire(canReadHistory, row.id, onHistory);
      setOk(`${row.data.gstin} retired.`);
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (denied) return <Denied what="organisation" />;

  return (
    <div data-testid="org-registrations-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Registrations</h3>
        <div className="spacer" />
        {canReadHistory && (
          <button
            className="btn btn-sm"
            onClick={() => setShowRetired((shown) => !shown)}
            data-testid="registrations-retired-toggle"
          >
            {showRetired ? "Show active" : "Show retired"}
          </button>
        )}
        {canEdit && (
          <button
            className="btn btn-cta"
            onClick={add}
            disabled={!entities.items.length}
            data-testid="registration-new-button"
          >
            <Plus size={15} /> New registration
          </button>
        )}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {historySubject && (
        <AdministrativeHistory
          key={`registration:${historySubject}:${historyRefresh ?? ""}`}
          subjectKind="master"
          subjectId={`registration:${historySubject}`}
          refreshKey={historyRefresh}
          testId="registration-history"
        />
      )}
      {canEdit && open && (
        <div className="card section-card" data-testid="registration-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">{editing ? "Edit registration" : "Create registration"}</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="registration-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <PickerField
              id="registration-entity-select"
              label="Legal entity"
              noun="legal entity"
              placeholder="Select legal entity…"
              value={form.entity_id}
              onChange={(id) => setForm({ ...form, entity_id: id })}
              picker={entityPicker}
            />
            <input
              className="input"
              placeholder="GSTIN (15 characters)"
              value={form.gstin}
              onChange={(e) => setForm({ ...form, gstin: e.target.value.toUpperCase() })}
              data-testid="registration-gstin-input"
            />
            <input
              className="input"
              placeholder="State code (e.g. 10)"
              value={form.state_code}
              onChange={(e) => setForm({ ...form, state_code: e.target.value })}
              data-testid="registration-state-code-input"
            />
            <input
              className="input"
              placeholder="State name"
              value={form.state_name}
              onChange={(e) => setForm({ ...form, state_name: e.target.value })}
              data-testid="registration-state-name-input"
            />
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={!form.entity_id || !form.gstin || !form.state_code || !form.state_name}
              data-testid="registration-save-button"
            >
              <Save size={15} /> Save registration
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="registrations-table">
          <thead>
            <tr>
              <th>GSTIN</th>
              <th>State</th>
              <th>Legal entity</th>
              <th>Status</th>
              {(canEdit || canReadHistory) && <th />}
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
              <tr data-testid="registrations-empty">
                <td colSpan={5}>No registrations yet.</td>
              </tr>
            ) : (
              items.map((row) => (
                <tr key={row.id} data-testid={`registration-row-${row.data.gstin}`}>
                  <td className="mono">
                    <b>{row.data.gstin}</b>
                  </td>
                  <td>{row.data.state_name}</td>
                  <td>{nameOf(row.data.entity_id)}</td>
                  <td>
                    <span className={`chip chip-${row.state === "active" ? "green" : "red"}`}>
                      {row.state}
                    </span>
                  </td>
                  {(canEdit || canReadHistory) && (
                    <td>
                      {canEdit && (
                        <>
                          <button
                            className="btn btn-sm"
                            onClick={() => edit(row)}
                            data-testid={`edit-registration-${row.data.gstin}`}
                          >
                            <Pencil size={13} /> Edit
                          </button>
                          {row.state === "active" && (
                            <button
                              className="btn btn-sm"
                              onClick={() => retire(row)}
                              data-testid={`retire-registration-${row.data.gstin}`}
                            >
                              Retire
                            </button>
                          )}
                        </>
                      )}
                      {canReadHistory && (
                        <button
                          className="btn btn-sm"
                          onClick={() => onHistory(row.id)}
                          data-testid={`history-registration-${row.data.gstin}`}
                        >
                          <History size={13} /> History
                        </button>
                      )}
                    </td>
                  )}
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
// Sites: list (E016-E017) and detail (E018-E020, E066-E069, E215-E221)
// --------------------------------------------------------------------------

const blankSite = {
  code: "",
  name: "",
  city: "",
  state: "",
  country: "IN",
  type: "warehouse" as SiteData["type"],
  entity_id: "",
  registration_id: "",
  counter_count: 0,
  permitted_operations: [] as string[],
};

function operationsFor(type: string): readonly string[] {
  // Warehouses (and DCs) never see a selling capability at all — never mind
  // the cashier/till/printer fields the site payload does not even have.
  return type === "store" || type === "concession"
    ? OPERATIONS
    : OPERATIONS.filter((o) => o !== "sell");
}

function SiteListView({ onOpen }: { onOpen: (id: string) => void }) {
  const { session } = useAuth();
  const canEdit = hold(session, "org.site.manage");
  const canReadHistory = hold(session, "audit.view");
  // A retired site leaves the active list but keeps its history (ticket 02C).
  const [showRetired, setShowRetired] = useState(false);
  const { items, loading, denied, failure, reload } = useResourceList<SiteData>(
    showRetired ? "/goods-v1/masters/stores?status=retired" : "/goods-v1/masters/stores",
  );
  const [form, setForm] = useState(blankSite);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  // Both search and page (ticket 02D) rather than trusting a fixed first page
  // to hold everything; the registration choice is further narrowed to the
  // entity picked above it, at the server, so its own search and paging stay
  // correct within that subset.
  const entities = usePagedPicker<"/goods-v1/masters/entities", ResourceDTO<EntityData>>(
    "/goods-v1/masters/entities",
    {},
    form.entity_id,
    (row) => row.data.name,
    { detailPath: (id) => `/goods-v1/masters/entities/${id}` },
  );
  const registrations = usePagedPicker<"/goods-v1/masters/gstins", ResourceDTO<RegistrationData>>(
    "/goods-v1/masters/gstins",
    form.entity_id ? { entity_id: Number(form.entity_id) } : null,
    form.registration_id,
    (row) => `${row.data.state_name} · ${row.data.gstin}`,
    { detailPath: (id) => `/goods-v1/masters/gstins/${id}` },
  );

  function add() {
    setForm(blankSite);
    setOpen(true);
    setOk("");
    setError("");
  }

  function toggleOp(op: string) {
    setForm((f) => ({
      ...f,
      permitted_operations: f.permitted_operations.includes(op)
        ? f.permitted_operations.filter((o) => o !== op)
        : [...f.permitted_operations, op],
    }));
  }

  async function save() {
    setError("");
    setOk("");
    const payload = {
      code: form.code,
      name: form.name,
      aliases: [],
      city: form.city,
      state: form.state,
      country: form.country,
      type: form.type,
      entity_id: form.entity_id,
      registration_id: form.registration_id,
      counter_count: form.type === "store" || form.type === "concession" ? form.counter_count : 0,
      permitted_operations: form.permitted_operations,
      brand_ids: [],
    };
    try {
      const resp = await stepUp.guarded(() =>
        api.post("/goods-v1/masters/stores", { ...payload, ...goodsMeta() }),
      );
      setOpen(false);
      setOk("Site created — planned, not yet goods-ready. See its Readiness tab.");
      reload();
      onOpen((resp as { data: { id: string } }).data.id);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (denied) return <Denied what="organisation" />;

  return (
    <div data-testid="org-sites-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Sites</h3>
        <div className="spacer" />
        {canReadHistory && (
          <button
            className="btn btn-sm"
            onClick={() => setShowRetired((shown) => !shown)}
            data-testid="sites-retired-toggle"
          >
            {showRetired ? "Show active" : "Show retired"}
          </button>
        )}
        {canEdit && (
          <button className="btn btn-cta" onClick={add} data-testid="site-new-button">
            <Plus size={15} /> New site
          </button>
        )}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {canEdit && open && (
        <div className="card section-card" data-testid="site-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">Create site</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="site-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Code"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value })}
              data-testid="site-code-input"
            />
            <input
              className="input"
              placeholder="Name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="site-name-input"
            />
            <select
              className="select"
              value={form.type}
              onChange={(e) =>
                setForm({
                  ...form,
                  type: e.target.value as SiteData["type"],
                  permitted_operations: [],
                })
              }
              data-testid="site-type-select"
            >
              {SITE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <input
              className="input"
              placeholder="City"
              value={form.city}
              onChange={(e) => setForm({ ...form, city: e.target.value })}
              data-testid="site-city-input"
            />
            <PickerField
              id="site-entity-select"
              label="Legal entity"
              noun="legal entity"
              placeholder="Select legal entity…"
              value={form.entity_id}
              onChange={(id) => setForm({ ...form, entity_id: id, registration_id: "" })}
              picker={entities}
            />
            <PickerField
              id="site-registration-select"
              label="Registration"
              noun="registration"
              placeholder="Select registration…"
              value={form.registration_id}
              onChange={(id) => setForm({ ...form, registration_id: id })}
              picker={registrations}
              disabled={!form.entity_id}
            />
            {(form.type === "store" || form.type === "concession") && (
              <input
                className="input"
                type="number"
                min={0}
                placeholder="Counter count"
                value={form.counter_count}
                onChange={(e) => setForm({ ...form, counter_count: Number(e.target.value) })}
                data-testid="site-counter-count-input"
              />
            )}
          </div>
          <div style={{ marginTop: 12 }}>
            <p className="eyebrow" style={{ marginBottom: 6 }}>
              Permitted operations — a preset never makes this site ready; see its Readiness tab
            </p>
            <div className="chip-picker" data-testid="site-operations-picker">
              {operationsFor(form.type).map((op) => (
                <button
                  key={op}
                  type="button"
                  className={`chip chip-pick ${form.permitted_operations.includes(op) ? "chip-green" : ""}`}
                  onClick={() => toggleOp(op)}
                  data-testid={`site-operation-${op}`}
                >
                  {op}
                </button>
              ))}
            </div>
          </div>
          <button
            className="btn btn-cta"
            style={{ marginTop: 12 }}
            onClick={save}
            disabled={!form.code || !form.name || !form.entity_id || !form.registration_id}
            data-testid="site-save-button"
          >
            <Save size={15} /> Save site
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="sites-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              <th>Type</th>
              <th>City</th>
              <th>Status</th>
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
              <tr data-testid="sites-empty">
                <td colSpan={5}>{showRetired ? "No retired sites." : "No sites yet."}</td>
              </tr>
            ) : (
              items.map((row) => (
                <tr
                  key={row.id}
                  data-testid={`site-row-${row.data.code}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => onOpen(row.id)}
                >
                  <td>
                    <b className="mono">{row.data.code}</b>
                  </td>
                  <td>{row.data.name}</td>
                  <td>
                    <span
                      className={`chip chip-${row.data.type === "warehouse" ? "navy" : "green"}`}
                    >
                      {row.data.type}
                    </span>
                  </td>
                  <td>{row.data.city || "—"}</td>
                  <td>
                    <span className={`chip chip-${row.state === "active" ? "green" : "amber"}`}>
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

const SITE_TABS = ["overview", "sbus", "locations", "readiness", "closure", "history"] as const;
type SiteTab = (typeof SITE_TABS)[number];

const TAB_LABEL: Record<SiteTab, string> = {
  overview: "Overview",
  sbus: "SBUs",
  locations: "Locations",
  readiness: "Readiness",
  closure: "Closure",
  history: "History",
};

function SiteDetailView({
  siteId,
  tab,
  onTab,
  onBack,
}: {
  siteId: string;
  tab: SiteTab;
  onTab: (t: SiteTab) => void;
  onBack: () => void;
}) {
  const { doc, loading, denied, failure, reload } = useResourceDoc<SiteData>(
    `/goods-v1/masters/stores/${siteId}`,
  );

  if (loading) return <p className="lead">Loading…</p>;
  if (denied) return <Denied what="site" />;
  if (failure || !doc)
    return <div className="warn-note">{failure || "This site could not be loaded."}</div>;

  return (
    <div data-testid="org-site-detail">
      <div className="toolbar" style={{ marginBottom: 8 }}>
        <button className="btn btn-sm" onClick={onBack} data-testid="site-back-button">
          ← All sites
        </button>
        <div className="spacer" />
      </div>
      <h3 className="h3" data-testid="site-detail-title">
        {doc.data.name} <span className="mono muted-cell">({doc.data.code})</span>
      </h3>
      <div className="org-tabs" role="tablist" aria-label="Site sections">
        {SITE_TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            className={`org-tab ${tab === t ? "active" : ""}`}
            onClick={() => onTab(t)}
            data-testid={`site-tab-${t}`}
          >
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>
      {tab === "overview" && <SiteOverviewTab siteId={siteId} doc={doc} reload={reload} />}
      {tab === "sbus" && <SiteSbusTab siteId={siteId} />}
      {tab === "locations" && <SiteLocationsTab siteId={siteId} siteType={doc.data.type} />}
      {tab === "readiness" && <SiteReadinessTab siteId={siteId} />}
      {tab === "closure" && <SiteClosureTab siteId={siteId} />}
      {tab === "history" && <SiteHistoryTab siteId={siteId} />}
    </div>
  );
}

function SiteOverviewTab({
  siteId,
  doc,
  reload,
}: {
  siteId: string;
  doc: ResourceDTO<SiteData>;
  reload: () => void;
}) {
  const { session } = useAuth();
  const canEdit = hold(session, "org.site.manage");
  const [form, setForm] = useState(() => ({
    code: doc.data.code,
    name: doc.data.name,
    city: doc.data.city ?? "",
    type: doc.data.type,
    entity_id: doc.data.entity_id ?? "",
    registration_id: doc.data.registration_id ?? "",
    counter_count: doc.data.counter_count ?? 0,
    permitted_operations: doc.data.permitted_operations ?? [],
  }));
  // Both search and page (ticket 02D) rather than trusting a fixed first page
  // to hold everything.
  const entities = usePagedPicker<"/goods-v1/masters/entities", ResourceDTO<EntityData>>(
    "/goods-v1/masters/entities",
    {},
    form.entity_id,
    (row) => row.data.name,
    { detailPath: (id) => `/goods-v1/masters/entities/${id}` },
  );
  const registrations = usePagedPicker<"/goods-v1/masters/gstins", ResourceDTO<RegistrationData>>(
    "/goods-v1/masters/gstins",
    form.entity_id ? { entity_id: Number(form.entity_id) } : null,
    form.registration_id,
    (row) => `${row.data.state_name} · ${row.data.gstin}`,
    { detailPath: (id) => `/goods-v1/masters/gstins/${id}` },
  );
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  function toggleOp(op: string) {
    setForm((f) => ({
      ...f,
      permitted_operations: f.permitted_operations.includes(op)
        ? f.permitted_operations.filter((o) => o !== op)
        : [...f.permitted_operations, op],
    }));
  }

  async function save() {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.patch(`/goods-v1/masters/stores/${siteId}`, {
          code: form.code,
          name: form.name,
          city: form.city,
          type: form.type,
          entity_id: form.entity_id,
          registration_id: form.registration_id,
          counter_count: form.counter_count,
          permitted_operations: form.permitted_operations,
          ...goodsMeta(doc.revision),
        }),
      );
      setEditing(false);
      setOk("Site saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function retireSite() {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/stores/${siteId}/retire`, {
          reason_code: "NO_LONGER_TRADING",
          ...goodsMeta(doc.revision),
        }),
      );
      setOk("Site retired.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  return (
    <div data-testid="site-overview-tab">
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {!editing ? (
        <div className="card section-card">
          <dl className="form-grid wide-form" style={{ rowGap: 6 }}>
            <div>
              <b>Type</b>
              <div>{doc.data.type}</div>
            </div>
            <div>
              <b>City</b>
              <div>{doc.data.city || "—"}</div>
            </div>
            <div>
              <b>Permitted operations</b>
              <div>{(doc.data.permitted_operations || []).join(", ") || "None yet"}</div>
            </div>
            <div>
              <b>Status</b>
              <div>{doc.state}</div>
            </div>
          </dl>
          {canEdit && (
            <div className="toolbar">
              <button
                className="btn btn-sm"
                onClick={() => setEditing(true)}
                data-testid="site-overview-edit"
              >
                <Pencil size={13} /> Edit
              </button>
              {doc.state === "active" && (
                <button
                  className="btn btn-sm"
                  onClick={retireSite}
                  data-testid="site-overview-retire"
                >
                  Retire site
                </button>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="card section-card" data-testid="site-overview-editor">
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Code"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value })}
              data-testid="site-overview-code-input"
            />
            <input
              className="input"
              placeholder="Name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="site-overview-name-input"
            />
            <input
              className="input"
              placeholder="City"
              value={form.city}
              onChange={(e) => setForm({ ...form, city: e.target.value })}
              data-testid="site-overview-city-input"
            />
            <PickerField
              id="site-overview-entity-select"
              label="Legal entity"
              noun="legal entity"
              placeholder="Select legal entity…"
              value={form.entity_id}
              onChange={(id) => setForm({ ...form, entity_id: id, registration_id: "" })}
              picker={entities}
            />
            <PickerField
              id="site-overview-registration-select"
              label="Registration"
              noun="registration"
              placeholder="Select registration…"
              value={form.registration_id}
              onChange={(id) => setForm({ ...form, registration_id: id })}
              picker={registrations}
              disabled={!form.entity_id}
            />
            {(form.type === "store" || form.type === "concession") && (
              <input
                className="input"
                type="number"
                min={0}
                value={form.counter_count}
                onChange={(e) => setForm({ ...form, counter_count: Number(e.target.value) })}
                data-testid="site-overview-counter-input"
              />
            )}
          </div>
          <div style={{ marginTop: 12 }}>
            <p className="eyebrow" style={{ marginBottom: 6 }}>
              Permitted operations
            </p>
            <div className="chip-picker">
              {operationsFor(form.type).map((op) => (
                <button
                  key={op}
                  type="button"
                  className={`chip chip-pick ${form.permitted_operations.includes(op) ? "chip-green" : ""}`}
                  onClick={() => toggleOp(op)}
                  data-testid={`site-overview-operation-${op}`}
                >
                  {op}
                </button>
              ))}
            </div>
          </div>
          <div className="toolbar" style={{ marginTop: 12 }}>
            <button className="btn btn-cta" onClick={save} data-testid="site-overview-save">
              <Save size={15} /> Save
            </button>
            <button
              className="btn btn-sm"
              onClick={() => setEditing(false)}
              data-testid="site-overview-cancel"
            >
              <X size={14} /> Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SiteSbusTab({ siteId }: { siteId: string }) {
  const {
    items: sbus,
    loading,
    denied,
    failure,
    reload,
  } = useResourceList<SbuData>(`/goods-v1/masters/stores/${siteId}/sbus`);
  const [retiring, setRetiring] = useState<string | null>(null);

  if (denied) return <Denied what="site" />;
  return (
    <div data-testid="site-sbus-tab">
      <p className="lead">
        Each brand at this site has its own business unit; a site with no brands configured has one
        fallback unit. A unit is retired, never deleted, and only once nothing still refers to it:
        no stock (including quarantined, unvalued, reserved or in-transit goods), no open document
        and no unresolved exception. There is no override — each of those is handled through its own
        step first.
      </p>
      <div className="table-wrap">
        <table className="data" data-testid="sbus-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Brand</th>
              <th>Status</th>
              <th></th>
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
            ) : sbus.length === 0 ? (
              <tr data-testid="sbus-empty">
                <td colSpan={4}>No business units yet.</td>
              </tr>
            ) : (
              sbus.map((s) => (
                <tr key={s.id} data-testid={`sbu-row-${s.data.code}`}>
                  <td className="mono">{s.data.code}</td>
                  <td>
                    {s.data.brand_id ? (
                      s.data.brand_id
                    ) : (
                      <span className="muted-cell">Fallback (no brand)</span>
                    )}
                  </td>
                  <td>
                    <span className={`chip chip-${s.state === "retired" ? "red" : "green"}`}>
                      {s.state === "retired" ? "Retired" : "Active"}
                    </span>
                  </td>
                  <td>
                    {s.allowed_actions.includes("retire") && retiring !== s.id && (
                      <button
                        className="btn btn-sm"
                        onClick={() => setRetiring(s.id)}
                        data-testid={`sbu-retire-${s.data.code}`}
                      >
                        Retire…
                      </button>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {sbus
        .filter((s) => s.id === retiring)
        .map((s) => (
          <SbuRetirePanel
            key={s.id}
            siteId={siteId}
            sbu={s}
            onClose={() => setRetiring(null)}
            onRetired={() => {
              setRetiring(null);
              reload();
            }}
          />
        ))}
    </div>
  );
}

/** E246: the owner names a reason and confirms the exact unit they reviewed; the
 *  server rechecks every residual and, if any remains, lists each one here. */
function SbuRetirePanel({
  siteId,
  sbu,
  onClose,
  onRetired,
}: {
  siteId: string;
  sbu: ResourceDTO<SbuData>;
  onClose: () => void;
  onRetired: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [blockers, setBlockers] = useState<ApiIssue[]>([]);
  const stepUp = useStepUp();

  async function retire() {
    setError("");
    setBlockers([]);
    setBusy(true);
    try {
      await stepUp.guarded(() => {
        const body: SbuRetireBody = {
          reason_code: reason.trim(),
          reviewed_hash: sbu.content_hash,
          ...goodsMeta(sbu.revision),
          expected_revision: sbu.revision,
        };
        return api.post(`/goods-v1/masters/stores/${siteId}/sbus/${sbu.id}/retire`, body);
      });
      onRetired();
    } catch (e) {
      setError(apiErrorMessage(e));
      if (apiErrorCode(e) === "SBU_RETIREMENT_BLOCKED") setBlockers(apiErrorIssues(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card section-card" data-testid="sbu-retire-panel">
      <h3 className="h3">Retire business unit {sbu.data.code}</h3>
      <p className="lead">
        The unit and its history stay on record. You will be asked to confirm your password.
      </p>
      {stepUp.dialog}
      <Field id="sbu-retire-reason" label="Reason">
        <input
          id="sbu-retire-reason"
          maxLength={60}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. BRAND_EXITED"
          data-testid="sbu-retire-reason"
        />
      </Field>
      {error && (
        <div className="warn-note" role="alert" data-testid="sbu-retire-error">
          {error}
        </div>
      )}
      {blockers.length > 0 && (
        <ul className="org-blockers" data-testid="sbu-retire-blockers">
          {blockers.map((b, i) => (
            <li key={`${b.code}-${b.field ?? i}`} data-testid={`sbu-blocker-${b.code}`}>
              {b.message}
            </li>
          ))}
        </ul>
      )}
      <div className="toolbar">
        <button
          className="btn btn-cta"
          onClick={retire}
          disabled={busy || !reason.trim()}
          data-testid="sbu-retire-confirm"
        >
          Retire unit
        </button>
        <button
          className="btn btn-sm"
          onClick={onClose}
          disabled={busy}
          data-testid="sbu-retire-cancel"
        >
          <X size={14} /> Cancel
        </button>
      </div>
    </div>
  );
}

interface LocNode extends ResourceDTO<LocationData> {
  children: LocNode[];
}

function buildTree(rows: ResourceDTO<LocationData>[]): LocNode[] {
  const byId = new Map<string, LocNode>();
  for (const row of rows) byId.set(row.id, { ...row, children: [] });
  const roots: LocNode[] = [];
  for (const node of byId.values()) {
    const parentId = node.data.parent_id;
    if (parentId && byId.has(parentId)) byId.get(parentId)!.children.push(node);
    else roots.push(node);
  }
  return roots;
}

function LocationRow({
  node,
  siteId,
  canEdit,
  onChanged,
  depth,
}: {
  node: LocNode;
  siteId: string;
  canEdit: boolean;
  onChanged: () => void;
  depth: number;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(node.data.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const stepUp = useStepUp();

  async function rename() {
    setError("");
    setBusy(true);
    try {
      await stepUp.guarded(() =>
        api.patch(`/goods-v1/masters/stores/${siteId}/locations/${node.id}`, {
          name,
          ...goodsMeta(node.revision),
        }),
      );
      setRenaming(false);
      onChanged();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function retire() {
    setError("");
    setBusy(true);
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/stores/${siteId}/locations/${node.id}/retire`, {
          reason_code: "NO_LONGER_USED",
          ...goodsMeta(node.revision),
        }),
      );
      onChanged();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li>
      <div
        className="org-loc-row"
        style={{ paddingLeft: depth * 6 }}
        data-testid={`location-row-${node.id}`}
      >
        {node.data.system && (
          <span
            className="org-loc-lock"
            title="System location — cannot be moved, retired or deleted"
          >
            <Lock size={13} />
          </span>
        )}
        {renaming ? (
          <>
            <input
              className="input"
              style={{ maxWidth: 180 }}
              value={name}
              onChange={(e) => setName(e.target.value)}
              data-testid={`location-rename-input-${node.id}`}
            />
            <button
              className="btn btn-sm"
              onClick={rename}
              disabled={busy || !name}
              data-testid={`location-rename-save-${node.id}`}
            >
              Save
            </button>
            <button
              className="btn btn-sm"
              onClick={() => setRenaming(false)}
              data-testid={`location-rename-cancel-${node.id}`}
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <span>{node.data.name}</span>
            <span className="muted-cell">
              {LOCATION_KIND_LABEL[node.data.kind] ?? node.data.kind}
            </span>
            {node.state === "retired" && <span className="chip chip-red">Retired</span>}
            {canEdit && node.state !== "retired" && (
              <>
                <button
                  className="btn btn-sm"
                  onClick={() => setRenaming(true)}
                  data-testid={`location-rename-${node.id}`}
                >
                  <Pencil size={12} /> Rename
                </button>
                {!node.data.system && (
                  <button
                    className="btn btn-sm"
                    onClick={retire}
                    disabled={busy}
                    data-testid={`location-retire-${node.id}`}
                  >
                    Retire
                  </button>
                )}
              </>
            )}
          </>
        )}
      </div>
      {error && <div className="warn-note">{error}</div>}
      {stepUp.dialog}
      {node.children.length > 0 && (
        <ul className="org-loc-tree">
          {node.children.map((child) => (
            <LocationRow
              key={child.id}
              node={child}
              siteId={siteId}
              canEdit={canEdit}
              onChanged={onChanged}
              depth={depth + 1}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

function SiteLocationsTab({ siteId, siteType }: { siteId: string; siteType: string }) {
  const { session } = useAuth();
  const canEdit =
    hold(session, "org.location.manage") ||
    hold(session, "org.location.store.manage") ||
    hold(session, "org.location.warehouse_bin.manage");
  const { items, loading, denied, failure, reload } = useResourceList<LocationData>(
    `/goods-v1/masters/stores/${siteId}/locations`,
  );
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<string>(CREATABLE_LOCATION_KINDS[0]);
  const [parentId, setParentId] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  if (denied) return <Denied what="site" />;

  const tree = buildTree(items);

  async function create() {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/stores/${siteId}/locations`, {
          name,
          kind,
          parent_id: parentId || null,
          ...goodsMeta(1),
        }),
      );
      setName("");
      setOpen(false);
      setOk("Location added.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  return (
    <div data-testid="site-locations-tab">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <p className="lead" style={{ margin: 0 }}>
          <Lock size={12} style={{ verticalAlign: -1 }} /> marks a protected system location — its
          kind cannot change and it cannot be moved, retired or deleted, only renamed.
        </p>
        <div className="spacer" />
        {canEdit && (
          <button
            className="btn btn-cta"
            onClick={() => setOpen((o) => !o)}
            data-testid="location-new-button"
          >
            <Plus size={15} /> Add location
          </button>
        )}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {canEdit && open && (
        <div className="card section-card" data-testid="location-editor">
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              data-testid="location-name-input"
            />
            <select
              className="select"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              data-testid="location-kind-select"
            >
              {CREATABLE_LOCATION_KINDS.map((k) => (
                <option key={k} value={k}>
                  {LOCATION_KIND_LABEL[k]}
                </option>
              ))}
            </select>
            <select
              className="select"
              value={parentId}
              onChange={(e) => setParentId(e.target.value)}
              data-testid="location-parent-select"
            >
              <option value="">No parent (top level)</option>
              {items.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.data.name}
                </option>
              ))}
            </select>
            <button
              className="btn btn-cta"
              onClick={create}
              disabled={!name}
              data-testid="location-create-button"
            >
              <Save size={15} /> Add
            </button>
          </div>
          {siteType === "warehouse" && (
            <p className="muted-cell" style={{ marginTop: 6 }}>
              Zone → rack → bin is a typical warehouse layout, not a compulsory tree.
            </p>
          )}
        </div>
      )}
      {loading ? (
        <p className="lead">Loading…</p>
      ) : failure ? (
        <div className="warn-note">{failure}</div>
      ) : tree.length === 0 ? (
        <p className="lead" data-testid="locations-empty">
          No locations yet.
        </p>
      ) : (
        <ul className="org-loc-tree top">
          {tree.map((node) => (
            <LocationRow
              key={node.id}
              node={node}
              siteId={siteId}
              canEdit={canEdit}
              onChanged={reload}
              depth={0}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function ReadinessCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="card section-card">
      <h3 className="h3">{title}</h3>
      {children}
    </div>
  );
}

function SiteReadinessTab({ siteId }: { siteId: string }) {
  const { session } = useAuth();
  const canRun = hold(session, "org.site.lifecycle.run");
  const canApprove = hold(session, "org.site.lifecycle.approve");
  const { doc, loading, denied, failure, reload } = useResourceDoc<ReadinessData>(
    `/goods-v1/masters/stores/${siteId}/readiness`,
  );
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  async function act(action: string) {
    if (!doc) return;
    setError("");
    setOk("");
    setBusy(true);
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/stores/${siteId}/readiness`, {
          action,
          ...goodsMeta(doc.revision),
        }),
      );
      setOk(action === "check" ? "Readiness re-checked." : "Saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="lead">Loading…</p>;
  if (denied) return <Denied what="site" />;
  if (failure || !doc)
    return <div className="warn-note">{failure || "Readiness could not be loaded."}</div>;

  const checks = doc.data.checks;
  const failing = checks.filter((c) => !c.passed);

  return (
    <div data-testid="site-readiness-tab">
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <span className="chip chip-navy" data-testid="site-lifecycle">
          {doc.data.lifecycle}
        </span>
        {canRun && (
          <button
            className="btn btn-sm"
            onClick={() => act("check")}
            disabled={busy}
            data-testid="readiness-check-button"
          >
            Re-check
          </button>
        )}
        <div className="spacer" />
        {canApprove && !doc.data.goods_ready && failing.length === 0 && (
          <button
            className="btn btn-cta"
            onClick={() => act("approve_goods")}
            disabled={busy}
            data-testid="readiness-approve-goods-button"
          >
            <ShieldCheck size={15} /> Approve goods
          </button>
        )}
        {canApprove && doc.data.goods_ready && (
          <button
            className="btn btn-sm"
            onClick={() => act("revoke_goods")}
            disabled={busy}
            data-testid="readiness-revoke-goods-button"
          >
            Revoke goods
          </button>
        )}
      </div>
      <div className="org-readiness-grid">
        <ReadinessCard title="Goods readiness">
          <p className="lead" data-testid="goods-ready-summary">
            {doc.data.goods_ready ? "Ready to receive, hold and transfer stock." : "Not ready yet."}
          </p>
          {checks.map((c) => (
            <div className="org-check-row" key={c.key} data-testid={`readiness-check-${c.key}`}>
              <span className={`chip chip-${c.passed ? "green" : "red"}`}>
                {c.passed ? "OK" : "Missing"}
              </span>
              <span>
                <b>{c.key.replace(/_/g, " ")}</b>
                {!c.passed && c.reason && <div className="muted-cell">{c.reason}</div>}
              </span>
            </div>
          ))}
        </ReadinessCard>
        <ReadinessCard title="Selling readiness">
          <p className="lead" data-testid="selling-ready-summary">
            {doc.data.sell_ready
              ? "Sell-ready."
              : "Not ready—selling activation is not available in this stage."}
          </p>
          <p className="muted-cell">
            This checklist is read-only in stage 1: there is no approve-sell action yet, whatever
            else this site satisfies.
          </p>
        </ReadinessCard>
      </div>
    </div>
  );
}

function residualLabel(qty: number | null): string {
  return qty === null ? "Unknown" : String(qty);
}

function SiteClosureTab({ siteId }: { siteId: string }) {
  const { session } = useAuth();
  const canApprove = hold(session, "org.site.lifecycle.approve");
  const { doc, loading, denied, failure, reload } = useResourceDoc<ReadinessData>(
    `/goods-v1/masters/stores/${siteId}/readiness`,
  );
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  if (loading) return <p className="lead">Loading…</p>;
  if (denied) return <Denied what="site" />;
  if (failure || !doc)
    return <div className="warn-note">{failure || "Closure detail could not be loaded."}</div>;

  const residuals = doc.data.residuals;
  const items: { code: string; label: string; qty: number | null; field?: string }[] = [
    { code: "physical_qty", label: "Physical stock", qty: residuals.physical_qty },
    { code: "transit_qty", label: "In transit", qty: residuals.transit_qty },
    { code: "unvalued_qty", label: "Unvalued stock", qty: residuals.unvalued_qty },
    { code: "reserved_qty", label: "Reserved", qty: residuals.reserved_qty },
    ...residuals.open_exception_ids.map((id) => ({
      code: "open_exception",
      label: `Open exception ${id}`,
      qty: null,
      field: id,
    })),
  ];

  async function startClosing() {
    if (!doc) return;
    setError("");
    setOk("");
    setBusy(true);
    const decisions = items
      .filter((i) => i.qty !== 0)
      .map((i) => ({
        code: i.code,
        ...(i.field ? { field: i.field } : {}),
        ...(i.qty !== null && i.code !== "open_exception" ? { quantity: i.qty } : {}),
        message: reasons[i.field ?? i.code] || "",
      }));
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/stores/${siteId}/readiness`, {
          action: "start_closing",
          residual_decisions: decisions,
          ...goodsMeta(doc.revision),
        }),
      );
      setOk("Closing started.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div data-testid="site-closure-tab">
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      <p className="lead">
        Cash: <b data-testid="closure-cash">Not assessed in this stage.</b>
      </p>
      <div className="table-wrap">
        <table className="data" data-testid="closure-table">
          <thead>
            <tr>
              <th>Residual</th>
              <th>Quantity</th>
              <th>Reason for the decision</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i) => (
              <tr key={i.field ?? i.code} data-testid={`closure-row-${i.field ?? i.code}`}>
                <td>{i.label}</td>
                <td>{residualLabel(i.qty)}</td>
                <td>
                  {canApprove && doc.data.lifecycle === "active" ? (
                    <input
                      className="input"
                      placeholder="Why this is safe to close"
                      value={reasons[i.field ?? i.code] ?? ""}
                      onChange={(e) =>
                        setReasons({ ...reasons, [i.field ?? i.code]: e.target.value })
                      }
                      data-testid={`closure-reason-${i.field ?? i.code}`}
                    />
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted-cell" data-testid="closure-completeness">
        Measurement:{" "}
        <span className={`chip chip-${residuals.completeness === "complete" ? "green" : "amber"}`}>
          {residuals.completeness}
        </span>
        {residuals.completeness !== "complete" &&
          " — closing is blocked until this site's stock can be fully measured."}
      </p>
      {canApprove && doc.data.lifecycle === "active" && (
        <button
          className="btn btn-cta"
          onClick={startClosing}
          disabled={busy || residuals.completeness !== "complete"}
          data-testid="closure-start-button"
        >
          Start closing
        </button>
      )}
      {doc.data.lifecycle === "closing" && (
        <p className="lead" data-testid="closure-in-progress">
          This site is closing. New inward work is blocked.
        </p>
      )}
      {doc.data.lifecycle === "closed" && (
        <p className="lead" data-testid="closure-done">
          This site is closed.
        </p>
      )}
    </div>
  );
}

function SiteHistoryTab({ siteId }: { siteId: string }) {
  return <AdministrativeHistory subjectKind="site" subjectId={siteId} testId="site-history-tab" />;
}

// --------------------------------------------------------------------------
// Tenant profile (E216-E217)
// --------------------------------------------------------------------------

function TenantPanel() {
  const { session } = useAuth();
  const canEdit = hold(session, "org.tenant.manage");
  const { doc, loading, denied, failure, reload } = useResourceDoc<{
    code: string;
    name: string;
    timezone: string;
    currency: string;
    locale: string;
    business_profile_version_id: string | null;
  }>("/goods-v1/masters/tenant");
  const [form, setForm] = useState({ name: "", timezone: "", currency: "", locale: "" });
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  useEffect(() => {
    if (doc)
      setForm({
        name: doc.data.name,
        timezone: doc.data.timezone,
        currency: doc.data.currency,
        locale: doc.data.locale,
      });
  }, [doc]);

  async function save() {
    if (!doc) return;
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.patch("/goods-v1/masters/tenant", {
          code: doc.data.code,
          ...form,
          ...goodsMeta(doc.revision),
        }),
      );
      setEditing(false);
      setOk("Tenant profile saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (loading) return <p className="lead">Loading…</p>;
  if (denied) return <Denied what="tenant profile" />;
  if (failure || !doc)
    return <div className="warn-note">{failure || "The tenant profile could not be loaded."}</div>;

  return (
    <div data-testid="org-tenant-panel">
      <h3 className="h3">Tenant profile</h3>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {!editing ? (
        <div className="card section-card">
          <dl className="form-grid wide-form" style={{ rowGap: 6 }}>
            <div>
              <b>Code</b>
              <div className="mono">{doc.data.code}</div>
            </div>
            <div>
              <b>Name</b>
              <div>{doc.data.name}</div>
            </div>
            <div>
              <b>Timezone</b>
              <div>{doc.data.timezone}</div>
            </div>
            <div>
              <b>Currency</b>
              <div>{doc.data.currency}</div>
            </div>
            <div>
              <b>Locale</b>
              <div>{doc.data.locale}</div>
            </div>
            <div>
              <b>Business profile</b>
              <div data-testid="tenant-business-profile">
                {doc.data.business_profile_version_id ? (
                  <span className="chip chip-green">Approved reference set</span>
                ) : (
                  <span className="chip chip-amber">
                    No approved business profile yet — prepared and approved in ticket 04
                  </span>
                )}
              </div>
            </div>
          </dl>
          {canEdit && (
            <button
              className="btn btn-sm"
              onClick={() => setEditing(true)}
              data-testid="tenant-edit-button"
            >
              <Pencil size={13} /> Edit
            </button>
          )}
          {hold(session, "audit.view") && (
            <AdministrativeHistory
              subjectKind="master"
              subjectId={`tenant:${doc.id}`}
              testId="tenant-history"
            />
          )}
        </div>
      ) : (
        <div className="card section-card" data-testid="tenant-editor">
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="tenant-name-input"
            />
            <input
              className="input"
              placeholder="Timezone"
              value={form.timezone}
              onChange={(e) => setForm({ ...form, timezone: e.target.value })}
              data-testid="tenant-timezone-input"
            />
            <input
              className="input"
              placeholder="Currency"
              value={form.currency}
              onChange={(e) => setForm({ ...form, currency: e.target.value })}
              data-testid="tenant-currency-input"
            />
            <input
              className="input"
              placeholder="Locale"
              value={form.locale}
              onChange={(e) => setForm({ ...form, locale: e.target.value })}
              data-testid="tenant-locale-input"
            />
          </div>
          <div className="toolbar" style={{ marginTop: 12 }}>
            <button className="btn btn-cta" onClick={save} data-testid="tenant-save-button">
              <Save size={15} /> Save
            </button>
            <button
              className="btn btn-sm"
              onClick={() => setEditing(false)}
              data-testid="tenant-cancel-button"
            >
              <X size={14} /> Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// The page: left nav + right detail panel
// --------------------------------------------------------------------------

type Panel = "entities" | "registrations" | "sites" | "tenant";

export function OrganisationPage() {
  const [params, setParams] = useSearchParams();
  const panel = (params.get("panel") as Panel | null) ?? "sites";
  const siteId = params.get("site");
  const tab = (params.get("tab") as SiteTab | null) ?? "overview";
  const history = params.get("history");
  const historyRefresh = params.get("history_refresh");
  // Only a well-formed entity/registration id reaches the history request.
  const historySubject = parseMasterHistory(history);
  const entityHistory = historySubject?.family === "entity" ? historySubject.id : null;
  const registrationHistory = historySubject?.family === "registration" ? historySubject.id : null;
  const { session } = useAuth();
  const sitesNav = useResourceList<SiteData>("/goods-v1/masters/stores");

  function selectPanel(next: Panel) {
    setParams({ panel: next });
  }

  function openSite(id: string) {
    setParams({ panel: "sites", site: id, tab: "overview" });
  }

  function selectTab(next: SiteTab) {
    setParams((prev) => {
      const merged = new URLSearchParams(prev);
      merged.set("tab", next);
      return merged;
    });
  }

  function backToSiteList() {
    setParams({ panel: "sites" });
  }

  function openMasterHistory(family: "entity" | "registration", id: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("history", `${family}:${id}`);
      // A retirement can keep this same subject selected.  This stable route
      // marker also tells the history reader to fetch the newly appended event.
      next.set("history_refresh", String(Date.now()));
      return next;
    });
  }

  return (
    <div className="page-pad">
      <PageHeader />
      <div className="org-layout">
        <nav className="org-nav" aria-label="Organisation">
          <div className="org-nav-group">
            <h4>Organisation</h4>
            <ul className="org-nav-list">
              <li>
                <button
                  className={`org-nav-item ${panel === "entities" ? "active" : ""}`}
                  onClick={() => selectPanel("entities")}
                  data-testid="org-nav-entities"
                >
                  <Landmark size={15} /> Legal entities
                </button>
              </li>
              <li>
                <button
                  className={`org-nav-item ${panel === "registrations" ? "active" : ""}`}
                  onClick={() => selectPanel("registrations")}
                  data-testid="org-nav-registrations"
                >
                  <Building2 size={15} /> Registrations
                </button>
              </li>
              {hold(session, "org.tenant.manage") && (
                <li>
                  <button
                    className={`org-nav-item ${panel === "tenant" ? "active" : ""}`}
                    onClick={() => selectPanel("tenant")}
                    data-testid="org-nav-tenant"
                  >
                    <ShieldCheck size={15} /> Tenant profile
                  </button>
                </li>
              )}
            </ul>
          </div>
          <div className="org-nav-group">
            <h4>Sites</h4>
            <ul className="org-nav-list">
              <li>
                <button
                  className={`org-nav-item ${panel === "sites" && !siteId ? "active" : ""}`}
                  onClick={() => setParams({ panel: "sites" })}
                  data-testid="org-nav-all-sites"
                >
                  All sites
                </button>
              </li>
              {sitesNav.items.map((s) => (
                <li key={s.id}>
                  <button
                    className={`org-nav-item ${panel === "sites" && siteId === s.id ? "active" : ""}`}
                    onClick={() => openSite(s.id)}
                    data-testid={`org-nav-site-${s.data.code}`}
                  >
                    {s.data.type === "warehouse" ? (
                      <Warehouse size={15} />
                    ) : (
                      <StoreIcon size={15} />
                    )}
                    {s.data.name}
                    <span className="org-nav-sub">{s.data.code}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </nav>
        <div className="org-detail">
          {panel === "entities" && (
            <EntitiesPanel
              historySubject={entityHistory}
              historyRefresh={historyRefresh}
              onHistory={(id) => openMasterHistory("entity", id)}
            />
          )}
          {panel === "registrations" && (
            <RegistrationsPanel
              historySubject={registrationHistory}
              historyRefresh={historyRefresh}
              onHistory={(id) => openMasterHistory("registration", id)}
            />
          )}
          {panel === "tenant" && <TenantPanel />}
          {panel === "sites" && !siteId && <SiteListView onOpen={openSite} />}
          {panel === "sites" && siteId && (
            <SiteDetailView siteId={siteId} tab={tab} onTab={selectTab} onBack={backToSiteList} />
          )}
        </div>
      </div>
    </div>
  );
}
