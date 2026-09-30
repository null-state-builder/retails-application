import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Pencil, Plus, Save, X } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import {
  CommercialBadge,
  StatusChip,
  formatINR,
  paiseToRupees,
  rupeesToPaise,
} from "../lib/format";
import { PageHeader } from "../components/PageHeader";
import { OperationsPage } from "../components/OperationsPage";
import { SearchBox } from "../components/SearchBox";
import { financialYear, financialYearChoices, financialYearMonths } from "../lib/fiscal";
import type { FiscalMonth } from "../lib/fiscal";
import { userCan } from "../shell/navConfig";
import { withQuery, type QueryParams } from "../lib/query";

function useSteward(): boolean {
  const { user } = useAuth();
  return userCan(user, "setup", "manage");
}

// `failure` is not decoration. Without it a refused or broken fetch left `data`
// at `[]` and said nothing at all, so "the server would not tell me" rendered
// exactly like "there is nothing here" - survivable on a list of seasons, not on
// a grid of editable money cells, where a blank cell reads as "no target set" and
// the next save would overwrite a number that was there all along.
function useList<T>(url: string, params?: QueryParams) {
  const [data, setData] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState("");
  const [tick, setTick] = useState(0);
  const full = withQuery(url, params);
  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .get(full)
      .then((r) => {
        if (!live) return;
        setData(r.data);
        setFailure("");
      })
      .catch((e) => {
        if (!live) return;
        setData([]);
        setFailure(apiErrorMessage(e));
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [full, tick]);
  return { data, loading, failure, reload: () => setTick((t) => t + 1) };
}

function Screen({
  title,
  count,
  action,
  children,
}: {
  title: string;
  count: number;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="page-pad">
      <PageHeader
        title={title}
        lead={`${count} record${count === 1 ? "" : "s"}`}
        actions={action}
      />
      {children}
    </div>
  );
}

function Feedback({ error, ok }: { error: string; ok: string }) {
  return (
    <>
      {error && (
        <div className="warn-note" data-testid="master-error">
          {error}
        </div>
      )}
      {ok && (
        <div className="ok-note" data-testid="master-ok">
          {ok}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------- Stores

// The masters read-shapes come from the generated client, never from a hand copy
// (#192). A hand-written twin agrees with the backend only until somebody changes
// the backend, and then it agrees with nothing and says so to nobody - which is
// how `store_type: string` here sat opposite a two-value enum there. Taking them
// from `ApiSchemas` makes the next divergence a build failure. `ApiRead<>` is the
// one adjustment; `lib/api.ts` says why.
type Store = ApiRead<ApiSchemas["Store"]>;

const blankStore = {
  id: 0,
  code: "",
  name: "",
  store_type: "store",
  city: "",
  gstin: 0,
  is_active: true,
  is_partner: false,
};

export function StoresPage() {
  const canEdit = useSteward();
  const [q, setQ] = useState("");
  const { data, loading, reload } = useList<Store>("/masters/stores", { q });
  const { data: gstins } = useList<Gstin>("/masters/gstins");
  const [form, setForm] = useState(blankStore);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  async function save() {
    setError("");
    setOk("");
    const payload = {
      code: form.code,
      name: form.name,
      store_type: form.store_type,
      city: form.city,
      gstin: form.gstin || null,
      is_active: form.is_active,
      is_partner: form.is_partner,
    };
    try {
      if (form.id) await api.patch(`/masters/stores/${form.id}`, payload);
      else await api.post("/masters/stores", payload);
      setForm(blankStore);
      setOpen(false);
      setOk("Store saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }
  function edit(s: Store) {
    setOpen(true);
    setOk("");
    setError("");
    setForm({
      id: s.id,
      code: s.code,
      name: s.name,
      store_type: s.store_type,
      city: s.city || "",
      gstin: s.gstin,
      is_active: s.is_active,
      is_partner: s.is_partner,
    });
  }
  function add() {
    setForm(blankStore);
    setOpen(true);
    setOk("");
    setError("");
  }

  return (
    <Screen
      title="Stores"
      count={data.length}
      action={
        canEdit && (
          <button className="btn btn-cta" onClick={add} data-testid="store-new-button">
            <Plus size={15} /> New store
          </button>
        )
      }
    >
      <Feedback error={error} ok={ok} />
      <SearchBox
        value={q}
        onChange={setQ}
        placeholder="Search stores — name, code"
        label="Search stores"
        testId="store-search"
      />
      {canEdit && open && (
        <div className="card section-card" data-testid="store-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">{form.id ? "Edit store" : "Create store"}</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="store-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Code (e.g. DEO)"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value })}
              data-testid="store-code-input"
            />
            <input
              className="input"
              placeholder="Name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="store-name-input"
            />
            <select
              className="select"
              value={form.store_type}
              onChange={(e) => setForm({ ...form, store_type: e.target.value })}
              data-testid="store-type-select"
            >
              <option value="store">Store</option>
              <option value="warehouse">Warehouse</option>
            </select>
            <input
              className="input"
              placeholder="City"
              value={form.city}
              onChange={(e) => setForm({ ...form, city: e.target.value })}
              data-testid="store-city-input"
            />
            <select
              className="select"
              value={form.gstin}
              onChange={(e) => setForm({ ...form, gstin: Number(e.target.value) })}
              data-testid="store-gstin-select"
            >
              <option value={0}>Select GSTIN / state…</option>
              {gstins.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.state_name} · {g.gstin}
                </option>
              ))}
            </select>
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.is_active}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                data-testid="store-active-checkbox"
              />{" "}
              Active
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.is_partner}
                onChange={(e) => setForm({ ...form, is_partner: e.target.checked })}
                data-testid="store-partner-checkbox"
              />
              Partner store (franchisee — billed at Purchase Price on every transfer)
            </label>
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={!form.code || !form.name || !form.gstin}
              data-testid="store-save-button"
            >
              <Save size={15} /> Save store
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="stores-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              <th>Type</th>
              <th>City</th>
              <th>State</th>
              <th>GSTIN</th>
              <th>Status</th>
              <th>Partner</th>
              {canEdit && <th />}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={9}>Loading…</td>
              </tr>
            ) : data.length === 0 ? (
              <tr data-testid="stores-empty">
                <td colSpan={9}>{q ? `No store matches “${q}”.` : "No stores yet."}</td>
              </tr>
            ) : (
              data.map((s) => (
                <tr key={s.id} data-testid={`store-row-${s.code}`}>
                  <td>
                    <b className="mono">{s.code}</b>
                  </td>
                  <td>{s.name}</td>
                  <td>
                    <StatusChip
                      status={s.store_type}
                      tone={s.store_type === "warehouse" ? "navy" : "green"}
                    />
                  </td>
                  <td>{s.city || "—"}</td>
                  <td>
                    <span className={`chip chip-${s.state_name === "Bihar" ? "amber" : "blue"}`}>
                      {s.state_name}
                    </span>
                  </td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {s.gstin_number}
                  </td>
                  <td>
                    <span className={`chip chip-${s.is_active ? "green" : "red"}`}>
                      {s.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td data-testid={`store-partner-${s.code}`}>
                    {s.is_partner ? <span className="chip chip-amber">Partner</span> : "—"}
                  </td>
                  {canEdit && (
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={() => edit(s)}
                        data-testid={`edit-store-${s.code}`}
                      >
                        <Pencil size={13} /> Edit
                      </button>
                    </td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Screen>
  );
}

// -------------------------------------------------------- Store targets

interface StoreTarget {
  store: string;
  month: string;
  target_paise: number;
}

interface TargetEdit {
  store: string;
  month: string;
  label: string;
  rupees: string;
}

/** How a cell is addressed, in the one place that spells it. Three call sites
 *  build this key, and three hand-written template strings are three chances for
 *  the lookup to stop matching the fill. */
const cellKey = (store: string, monthIso: string) => `${store}|${monthIso}`;

/** `GET | PUT /masters/store-targets` - the monthly rupee number each store is
 *  asked to sell, which the store Dashboard shows month-to-date against (#171).
 *
 *  Gated on `money: manage`, the same rung the server's PUT requires, not on the
 *  master-data steward rule the rest of this file uses: setting what a store must
 *  sell is a Money act, and the D10 grill made *which* role holds that cell
 *  admin-editable data rather than code. Reading stays at whatever holds the Money
 *  section at all - a store may see the number it is judged against.
 *
 *  Twelve columns are drawn from the financial year, not from the rows that came
 *  back: a month nobody has set yet is a blank cell to fill, and dropping it would
 *  hide exactly the work this screen exists for. */
export function StoreTargetsPage() {
  const { user } = useAuth();
  const canEdit = userCan(user, "money", "manage");
  const [fy, setFy] = useState(() => financialYear());
  const storeList = useList<Pick<Store, "id" | "code" | "name">>(
    "/masters/store-targets/locations",
  );
  const { data: stores, loading: storesLoading, failure: storesFailure } = storeList;
  const { data, loading, failure, reload } = useList<StoreTarget>("/masters/store-targets", { fy });
  const [edit, setEdit] = useState<TargetEdit | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  // A fetch that failed must not leave editable cells behind: a blank cell would
  // read as "no target set", and saving over it would overwrite a number the
  // screen never managed to load.
  const stale = Boolean(failure || storesFailure);
  const editable = canEdit && !stale;

  const months = useMemo(() => financialYearMonths(fy), [fy]);
  // cellKey -> paise. One pass, because a 50-store year is 600 cells and scanning
  // the list per cell would be 600 scans of it.
  const byCell = useMemo(() => {
    const map = new Map<string, number>();
    for (const row of data) map.set(cellKey(row.store, row.month), row.target_paise);
    return map;
  }, [data]);
  const fyTotal = useMemo(() => data.reduce((sum, row) => sum + row.target_paise, 0), [data]);

  // What the total is a total *of*, in the words that are true for this reader.
  // A store manager sees one store, so "across every store" would be a claim
  // about the network from a page showing one row of it.
  const totalOf =
    stores.length === 1 && stores[0] ? `at ${stores[0].code}` : `across ${stores.length} stores`;

  /** Dismiss whatever the last save said. A banner that outlives the editor it
   *  belonged to gets read against the next cell, or against the next year. */
  function clearFeedback() {
    setError("");
    setOk("");
  }

  function closeEditor() {
    setEdit(null);
    clearFeedback();
  }

  async function save() {
    if (!edit) return;
    const paise = rupeesToPaise(edit.rupees);
    if (paise === null) {
      setError("Enter the target in rupees - digits, and at most two decimal places.");
      return;
    }
    setBusy(true);
    setError("");
    setOk("");
    try {
      await api.put("/masters/store-targets", {
        store: edit.store,
        month: edit.month,
        target_paise: paise,
      });
      setOk(`${edit.store} · ${edit.label} target set to ${formatINR(paise)}.`);
      setEdit(null);
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function open(store: string, month: FiscalMonth) {
    const current = byCell.get(cellKey(store, month.iso));
    clearFeedback();
    setEdit({
      store,
      month: month.iso,
      label: month.label,
      // Pre-filled with what is already there so a correction is an edit, not a
      // retype - and blank when there is nothing, so nought stays a deliberate
      // answer rather than the default one.
      rupees: current === undefined ? "" : paiseToRupees(current),
    });
  }

  // Not the shared `Screen` wrapper: its lead counts records, and "600 records"
  // says nothing true about a grid. What a reader wants off the top of this one is
  // the year's committed total.
  return (
    <OperationsPage>
      <PageHeader
        lead={
          <span data-testid="target-fy-total">
            {canEdit
              ? "Pick a cell to set that store's month for the year. "
              : "Set at head office; shown here for reference. "}
            {/* No total until there is something to total. "committed across 0
                stores" while the grid loads is a number nobody asked for. */}
            {stores.length > 0 && !loading && (
              <>
                FY {fy} committed {totalOf}: <b>{formatINR(fyTotal)}</b>.
              </>
            )}
          </span>
        }
        actions={
          <select
            className="select"
            value={fy}
            onChange={(e) => {
              setFy(e.target.value);
              closeEditor();
            }}
            aria-label="Financial year"
            data-testid="target-fy-select"
          >
            {financialYearChoices().map((choice) => (
              <option key={choice} value={choice}>
                FY {choice}
              </option>
            ))}
          </select>
        }
      />
      <Feedback error={error || failure || storesFailure} ok={ok} />
      {stale && (
        <div className="warn-note" data-testid="target-stale-note">
          The grid could not be loaded, so nothing here can be edited - what you would see as an
          empty cell might be a target that is already set. Refresh, or check you still have access
          to this section.
        </div>
      )}
      {editable && edit && (
        <div className="card section-card" data-testid="target-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">
              {edit.store} · {edit.label}
            </h3>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={closeEditor} data-testid="target-editor-close">
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <input
              className="input"
              inputMode="decimal"
              placeholder="Target for the month (₹)"
              value={edit.rupees}
              onChange={(e) => setEdit({ ...edit, rupees: e.target.value })}
              aria-label={`Target for ${edit.store}, ${edit.label}, in rupees`}
              data-testid="target-amount-input"
            />
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={busy || !edit.rupees.trim()}
              data-testid="target-save-button"
            >
              <Save size={15} /> {busy ? "Saving…" : "Save target"}
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap" role="region" aria-label="Store targets" tabIndex={0}>
        <table className="data" data-testid="store-targets-table">
          <thead>
            <tr>
              <th>Store</th>
              {months.map((month) => (
                <th key={month.iso} className="tabular">
                  {month.label}
                </th>
              ))}
              <th className="tabular">FY total</th>
            </tr>
          </thead>
          <tbody>
            {storesLoading || loading ? (
              <tr>
                <td colSpan={months.length + 2}>Loading…</td>
              </tr>
            ) : stores.length === 0 ? (
              <tr data-testid="store-targets-empty">
                <td colSpan={months.length + 2}>
                  {stale
                    ? "Store targets could not be loaded. Refresh or check your access."
                    : "No active stores are available in your target scope."}
                </td>
              </tr>
            ) : (
              stores.map((store) => {
                const cells = months.map((month) => byCell.get(cellKey(store.code, month.iso)));
                const total = cells.reduce<number>((sum, paise) => sum + (paise ?? 0), 0);
                return (
                  <tr key={store.code} data-testid={`target-row-${store.code}`}>
                    <td>
                      <b className="mono">{store.code}</b> {store.name}
                    </td>
                    {months.map((month, index) => {
                      const paise = cells[index];
                      // The em dash is this table's "nothing here" glyph, the same
                      // one the store and vendor lists use for a blank column.
                      const shown = paise === undefined ? "—" : formatINR(paise, { short: true });
                      const testId = `target-cell-${store.code}-${month.iso}`;
                      return (
                        <td key={month.iso} className="tabular">
                          {editable ? (
                            <button
                              className="btn btn-sm"
                              onClick={() => open(store.code, month)}
                              aria-label={`Set ${store.name} target for ${month.label}`}
                              data-testid={testId}
                            >
                              {shown}
                            </button>
                          ) : (
                            <span data-testid={testId}>{shown}</span>
                          )}
                        </td>
                      );
                    })}
                    <td className="tabular">
                      <b>{total ? formatINR(total, { short: true }) : "—"}</b>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </OperationsPage>
  );
}

// ---------------------------------------------------------------- Brands

type Brand = ApiRead<ApiSchemas["Brand"]>;

// The two numbers a brand negotiates on returns (#75): how long the window is
// and, for a Correction brand, what share of what they delivered may go back.
// Zero means nobody has agreed one yet, which the return screen says out loud
// rather than inventing a default nobody shook hands on.
const blankBrand = {
  id: 0,
  code: "",
  name: "",
  ownership: "owned",
  return_terms: "none",
  return_window_days: "0",
  return_cap_percent: "0",
  is_active: true,
};

export function BrandsPage() {
  const canEdit = useSteward();
  const [q, setQ] = useState("");
  const { data, loading, reload } = useList<Brand>("/masters/brands", { q });
  const [form, setForm] = useState(blankBrand);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  async function save() {
    setError("");
    setOk("");
    const payload = {
      code: form.code,
      name: form.name,
      ownership: form.ownership,
      return_terms: form.return_terms,
      return_window_days: Number(form.return_window_days) || 0,
      return_cap_percent: form.return_cap_percent || "0",
      is_active: form.is_active,
    };
    try {
      if (form.id) await api.patch(`/masters/brands/${form.id}`, payload);
      else await api.post("/masters/brands", payload);
      setForm(blankBrand);
      setOpen(false);
      setOk("Brand saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }
  function edit(b: Brand) {
    setOpen(true);
    setOk("");
    setError("");
    setForm({
      id: b.id,
      code: b.code,
      name: b.name,
      ownership: b.ownership,
      return_terms: b.return_terms,
      return_window_days: String(b.return_window_days ?? 0),
      return_cap_percent: String(b.return_cap_percent ?? "0"),
      is_active: b.is_active,
    });
  }
  function add() {
    setForm(blankBrand);
    setOpen(true);
    setOk("");
    setError("");
  }

  return (
    <Screen
      title="Brands"
      count={data.length}
      action={
        canEdit && (
          <button className="btn btn-cta" onClick={add} data-testid="brand-new-button">
            <Plus size={15} /> New brand
          </button>
        )
      }
    >
      <Feedback error={error} ok={ok} />
      <SearchBox
        value={q}
        onChange={setQ}
        placeholder="Search brands — name, code"
        label="Search brands"
        testId="brand-search"
      />
      {canEdit && open && (
        <div className="card section-card" data-testid="brand-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">{form.id ? "Edit brand" : "Create brand"}</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="brand-editor-close"
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
              data-testid="brand-code-input"
            />
            <input
              className="input"
              placeholder="Brand name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="brand-name-input"
            />
            <select
              className="select"
              value={form.ownership}
              onChange={(e) => setForm({ ...form, ownership: e.target.value })}
              data-testid="brand-ownership-select"
            >
              <option value="owned">KDPS-owned</option>
              <option value="brand_owned">Brand-owned</option>
            </select>
            <select
              className="select"
              value={form.return_terms}
              onChange={(e) => setForm({ ...form, return_terms: e.target.value })}
              data-testid="brand-return-select"
            >
              <option value="none">No returns</option>
              <option value="capped">Capped allowance</option>
              <option value="uncapped">Uncapped</option>
              <option value="rolling">Uncapped + rolling top-up</option>
            </select>
            <input
              className="input"
              type="number"
              min={0}
              placeholder="Return window (days)"
              value={form.return_window_days}
              onChange={(e) => setForm({ ...form, return_window_days: e.target.value })}
              data-testid="brand-window-days-input"
            />
            <input
              className="input"
              type="number"
              min={0}
              max={100}
              step="0.01"
              placeholder="Allowed return %"
              value={form.return_cap_percent}
              onChange={(e) => setForm({ ...form, return_cap_percent: e.target.value })}
              data-testid="brand-cap-percent-input"
              disabled={form.return_terms !== "capped"}
              title={
                form.return_terms === "capped"
                  ? "The negotiated share of what this brand delivered that may go back"
                  : "Only a capped brand has an allowance"
              }
            />
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.is_active}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                data-testid="brand-active-checkbox"
              />{" "}
              Active
            </label>
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={!form.code || !form.name}
              data-testid="brand-save-button"
            >
              <Save size={15} /> Save brand
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="brands-table">
          <thead>
            <tr>
              <th>Brand</th>
              <th>Ownership</th>
              <th>Return terms</th>
              <th>Commercial model</th>
              <th>Returns</th>
              <th>Status</th>
              {canEdit && <th />}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7}>Loading…</td>
              </tr>
            ) : data.length === 0 ? (
              <tr data-testid="brands-empty">
                <td colSpan={7}>{q ? `No brand matches “${q}”.` : "No brands yet."}</td>
              </tr>
            ) : (
              data.map((b) => (
                <tr key={b.id} data-testid={`brand-row-${b.code}`}>
                  <td>
                    <b>{b.name}</b>
                  </td>
                  <td>{b.ownership === "owned" ? "KDPS-owned" : "Brand-owned"}</td>
                  <td style={{ textTransform: "capitalize" }}>{b.return_terms}</td>
                  <td>
                    <CommercialBadge label={b.commercial_label} />
                  </td>
                  <td data-testid={`brand-returns-${b.code}`}>
                    {b.takes_returns ? (
                      <>
                        {b.return_window_days > 0
                          ? `${b.return_window_days}-day window`
                          : "No window agreed"}
                        {b.cap_applies && ` · ${b.return_cap_percent}% allowed`}
                      </>
                    ) : (
                      "Nothing goes back"
                    )}
                  </td>
                  <td>
                    <span className={`chip chip-${b.is_active ? "green" : "red"}`}>
                      {b.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  {canEdit && (
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={() => edit(b)}
                        data-testid={`edit-brand-${b.code}`}
                      >
                        <Pencil size={13} /> Edit
                      </button>
                    </td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Screen>
  );
}

// ---------------------------------------------------------------- Seasons

type Season = ApiRead<ApiSchemas["Season"]>;

const blankSeason = { id: 0, code: "", name: "", status: "open", sort_order: 0 };

export function SeasonsPage() {
  const canEdit = useSteward();
  const [q, setQ] = useState("");
  const { data, loading, reload } = useList<Season>("/masters/seasons", { q });
  const [form, setForm] = useState(blankSeason);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  async function save() {
    setError("");
    setOk("");
    const payload = {
      code: form.code,
      name: form.name,
      status: form.status,
      sort_order: Number(form.sort_order) || 0,
    };
    try {
      if (form.id) await api.patch(`/masters/seasons/${form.id}`, payload);
      else await api.post("/masters/seasons", payload);
      setForm(blankSeason);
      setOpen(false);
      setOk("Season saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }
  function edit(s: Season) {
    setOpen(true);
    setOk("");
    setError("");
    setForm({ id: s.id, code: s.code, name: s.name, status: s.status, sort_order: s.sort_order });
  }
  function add() {
    setForm(blankSeason);
    setOpen(true);
    setOk("");
    setError("");
  }

  return (
    <Screen
      title="Seasons"
      count={data.length}
      action={
        canEdit && (
          <button className="btn btn-cta" onClick={add} data-testid="season-new-button">
            <Plus size={15} /> New season
          </button>
        )
      }
    >
      <Feedback error={error} ok={ok} />
      <SearchBox
        value={q}
        onChange={setQ}
        placeholder="Search seasons — name, code"
        label="Search seasons"
        testId="season-search"
      />
      {canEdit && open && (
        <div className="card section-card" data-testid="season-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">{form.id ? "Edit season" : "Create season"}</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="season-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Code (e.g. SS26)"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value })}
              data-testid="season-code-input"
            />
            <input
              className="input"
              placeholder="Name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="season-name-input"
            />
            <select
              className="select"
              value={form.status}
              onChange={(e) => setForm({ ...form, status: e.target.value })}
              data-testid="season-status-select"
            >
              <option value="open">Open</option>
              <option value="eoss">EOSS</option>
              <option value="closed">Closed</option>
            </select>
            <input
              className="input"
              type="number"
              placeholder="Sort order"
              value={form.sort_order}
              onChange={(e) => setForm({ ...form, sort_order: Number(e.target.value) })}
              data-testid="season-sort-input"
            />
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={!form.code || !form.name}
              data-testid="season-save-button"
            >
              <Save size={15} /> Save season
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="seasons-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              <th>Status</th>
              {canEdit && <th />}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={4}>Loading…</td>
              </tr>
            ) : data.length === 0 ? (
              <tr data-testid="seasons-empty">
                <td colSpan={4}>{q ? `No season matches “${q}”.` : "No seasons yet."}</td>
              </tr>
            ) : (
              data.map((s) => (
                <tr key={s.id} data-testid={`season-row-${s.code}`}>
                  <td>
                    <b className="mono">{s.code}</b>
                  </td>
                  <td>{s.name}</td>
                  <td>
                    <StatusChip status={s.status} />
                  </td>
                  {canEdit && (
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={() => edit(s)}
                        data-testid={`edit-season-${s.code}`}
                      >
                        <Pencil size={13} /> Edit
                      </button>
                    </td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Screen>
  );
}

// ---------------------------------------------------------------- GSTINs

type Gstin = ApiRead<ApiSchemas["Gstin"]>;
interface EntityOpt {
  id: number;
  code: string;
  name: string;
}

const blankGstin = {
  id: 0,
  gstin: "",
  state_code: "",
  state_name: "",
  legal_entity: 0,
  is_active: true,
};

export function GstinsPage() {
  const canEdit = useSteward();
  const [q, setQ] = useState("");
  const { data, loading, reload } = useList<Gstin>("/masters/gstins", { q });
  const { data: entities } = useList<EntityOpt>("/masters/entities");
  const [form, setForm] = useState(blankGstin);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  async function save() {
    setError("");
    setOk("");
    const payload = {
      gstin: form.gstin,
      state_code: form.state_code,
      state_name: form.state_name,
      legal_entity: form.legal_entity || null,
      is_active: form.is_active,
    };
    try {
      if (form.id) await api.patch(`/masters/gstins/${form.id}`, payload);
      else await api.post("/masters/gstins", payload);
      setForm(blankGstin);
      setOpen(false);
      setOk("GSTIN saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }
  function edit(g: Gstin) {
    setOpen(true);
    setOk("");
    setError("");
    setForm({
      id: g.id,
      gstin: g.gstin,
      state_code: g.state_code,
      state_name: g.state_name,
      legal_entity: g.legal_entity,
      is_active: g.is_active,
    });
  }
  function add() {
    setForm({ ...blankGstin, legal_entity: entities[0]?.id ?? 0 });
    setOpen(true);
    setOk("");
    setError("");
  }

  return (
    <Screen
      title="GSTINs"
      count={data.length}
      action={
        canEdit && (
          <button className="btn btn-cta" onClick={add} data-testid="gstin-new-button">
            <Plus size={15} /> New GSTIN
          </button>
        )
      }
    >
      <Feedback error={error} ok={ok} />
      <SearchBox
        value={q}
        onChange={setQ}
        placeholder="Search GSTINs — number, state, legal entity"
        label="Search GSTINs"
        testId="gstin-search"
      />
      {canEdit && open && (
        <div className="card section-card" data-testid="gstin-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">{form.id ? "Edit GSTIN" : "Create GSTIN"}</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="gstin-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="GSTIN (15 chars)"
              value={form.gstin}
              onChange={(e) => setForm({ ...form, gstin: e.target.value.toUpperCase() })}
              data-testid="gstin-number-input"
            />
            <input
              className="input"
              placeholder="State code (e.g. 10)"
              value={form.state_code}
              onChange={(e) => setForm({ ...form, state_code: e.target.value })}
              data-testid="gstin-state-code-input"
            />
            <input
              className="input"
              placeholder="State name"
              value={form.state_name}
              onChange={(e) => setForm({ ...form, state_name: e.target.value })}
              data-testid="gstin-state-name-input"
            />
            <select
              className="select"
              value={form.legal_entity}
              onChange={(e) => setForm({ ...form, legal_entity: Number(e.target.value) })}
              data-testid="gstin-entity-select"
            >
              <option value={0}>Select legal entity…</option>
              {entities.map((en) => (
                <option key={en.id} value={en.id}>
                  {en.name}
                </option>
              ))}
            </select>
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.is_active}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                data-testid="gstin-active-checkbox"
              />{" "}
              Active
            </label>
            <button
              className="btn btn-cta"
              onClick={save}
              disabled={!form.gstin || !form.state_code || !form.state_name || !form.legal_entity}
              data-testid="gstin-save-button"
            >
              <Save size={15} /> Save GSTIN
            </button>
          </div>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="gstins-table">
          <thead>
            <tr>
              <th>GSTIN</th>
              <th>State</th>
              <th>State code</th>
              <th>Legal entity</th>
              <th>Status</th>
              {canEdit && <th />}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={6}>Loading…</td>
              </tr>
            ) : data.length === 0 ? (
              <tr data-testid="gstins-empty">
                <td colSpan={6}>{q ? `No GSTIN matches “${q}”.` : "No GSTINs yet."}</td>
              </tr>
            ) : (
              data.map((g) => (
                <tr key={g.id} data-testid={`gstin-row-${g.state_code}`}>
                  <td className="mono">
                    <b>{g.gstin}</b>
                  </td>
                  <td>
                    <span className={`chip chip-${g.state_name === "Bihar" ? "amber" : "blue"}`}>
                      {g.state_name}
                    </span>
                  </td>
                  <td className="mono">{g.state_code}</td>
                  <td>{g.legal_entity_name}</td>
                  <td>
                    <span className={`chip chip-${g.is_active ? "green" : "red"}`}>
                      {g.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  {canEdit && (
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={() => edit(g)}
                        data-testid={`edit-gstin-${g.state_code}`}
                      >
                        <Pencil size={13} /> Edit
                      </button>
                    </td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Screen>
  );
}

// ---------------------------------------------------------------- Vendors

// The supplier master. It existed in the database and in `/api/vendors` from the
// first migration, and had no screen: vendors arrived through the seed or a raw
// POST (which, until this review, any logged-in person could send), and a typo
// in a name or a GSTIN could never be corrected. Bookings, GRNs, PTs and every
// rupee of payable hang off this row.

interface VendorRow {
  id: number;
  code: string;
  name: string;
  city: string;
  gstin: string;
  state_code: string;
  state_name: string;
  pan: string;
  payment_terms: string;
  brands: number[];
  brand_names: string[];
  is_active: boolean;
}

const blankVendor = {
  id: 0,
  code: "",
  name: "",
  city: "",
  gstin: "",
  state_code: "",
  state_name: "",
  pan: "",
  payment_terms: "",
  brands: [] as number[],
  is_active: true,
};

export function VendorsPage() {
  const canEdit = useSteward();
  const [showRetired, setShowRetired] = useState(false);
  const { data, loading, reload } = useList<VendorRow>(
    showRetired ? "/vendors?include_inactive=1" : "/vendors",
  );
  const { data: brands } = useList<{ id: number; code: string; name: string }>("/masters/brands");
  const [form, setForm] = useState(blankVendor);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  async function save() {
    setError("");
    setOk("");
    const payload = {
      code: form.code,
      name: form.name,
      city: form.city,
      gstin: form.gstin,
      state_code: form.state_code,
      state_name: form.state_name,
      pan: form.pan,
      payment_terms: form.payment_terms,
      brands: form.brands,
      is_active: form.is_active,
    };
    try {
      if (form.id) await api.patch(`/vendors/${form.id}`, payload);
      else await api.post("/vendors", payload);
      setForm(blankVendor);
      setOpen(false);
      setOk("Vendor saved.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }
  function edit(v: VendorRow) {
    setOpen(true);
    setOk("");
    setError("");
    setForm({
      id: v.id,
      code: v.code,
      name: v.name,
      city: v.city || "",
      gstin: v.gstin || "",
      state_code: v.state_code || "",
      state_name: v.state_name || "",
      pan: v.pan || "",
      payment_terms: v.payment_terms || "",
      brands: v.brands ?? [],
      is_active: v.is_active,
    });
  }
  function add() {
    setForm(blankVendor);
    setOpen(true);
    setOk("");
    setError("");
  }
  function toggleBrand(id: number) {
    setForm((f) => ({
      ...f,
      brands: f.brands.includes(id) ? f.brands.filter((b) => b !== id) : [...f.brands, id],
    }));
  }

  return (
    <Screen
      title="Vendors"
      count={data.length}
      action={
        <>
          <label className="check-row" style={{ marginRight: 10 }}>
            <input
              type="checkbox"
              checked={showRetired}
              onChange={(e) => setShowRetired(e.target.checked)}
              data-testid="vendor-show-retired"
            />
            Show retired
          </label>
          {canEdit && (
            <button className="btn btn-cta" onClick={add} data-testid="vendor-new-button">
              <Plus size={15} /> New vendor
            </button>
          )}
        </>
      }
    >
      <Feedback error={error} ok={ok} />
      {canEdit && open && (
        <div className="card section-card" data-testid="vendor-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">{form.id ? "Edit vendor" : "Create vendor"}</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="vendor-editor-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <input
              className="input"
              placeholder="Code (e.g. acme)"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value })}
              data-testid="vendor-code-input"
            />
            <input
              className="input"
              placeholder="Vendor name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              data-testid="vendor-name-input"
            />
            <input
              className="input"
              placeholder="City"
              value={form.city}
              onChange={(e) => setForm({ ...form, city: e.target.value })}
              data-testid="vendor-city-input"
            />
            <input
              className="input"
              placeholder="GSTIN (15 chars)"
              value={form.gstin}
              onChange={(e) => setForm({ ...form, gstin: e.target.value.toUpperCase() })}
              data-testid="vendor-gstin-input"
            />
            <input
              className="input"
              placeholder="State name"
              value={form.state_name}
              onChange={(e) => setForm({ ...form, state_name: e.target.value })}
              data-testid="vendor-state-input"
            />
            <input
              className="input"
              placeholder="PAN"
              value={form.pan}
              onChange={(e) => setForm({ ...form, pan: e.target.value.toUpperCase() })}
              data-testid="vendor-pan-input"
            />
            <input
              className="input"
              placeholder="Payment terms (e.g. 30 days from invoice)"
              value={form.payment_terms}
              onChange={(e) => setForm({ ...form, payment_terms: e.target.value })}
              data-testid="vendor-terms-input"
            />
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.is_active}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                data-testid="vendor-active-checkbox"
              />{" "}
              Active
            </label>
          </div>
          <div style={{ marginTop: 12 }}>
            <p className="eyebrow" style={{ marginBottom: 6 }}>
              Brands this vendor supplies
            </p>
            <div className="chip-picker" data-testid="vendor-brand-picker">
              {brands.map((b) => (
                <button
                  key={b.id}
                  type="button"
                  className={`chip chip-pick ${form.brands.includes(b.id) ? "chip-green" : ""}`}
                  onClick={() => toggleBrand(b.id)}
                  data-testid={`vendor-brand-${b.code}`}
                >
                  {b.name}
                </button>
              ))}
            </div>
          </div>
          <button
            className="btn btn-cta"
            style={{ marginTop: 12 }}
            onClick={save}
            disabled={!form.code || !form.name}
            data-testid="vendor-save-button"
          >
            <Save size={15} /> Save vendor
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="vendors-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Vendor</th>
              <th>City</th>
              <th>GSTIN</th>
              <th>Brands</th>
              <th>Payment terms</th>
              <th>Status</th>
              {canEdit && <th />}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={8}>Loading…</td>
              </tr>
            ) : data.length === 0 ? (
              <tr>
                <td colSpan={8}>
                  No vendors yet. A booking needs one — create the supplier first.
                </td>
              </tr>
            ) : (
              data.map((v) => (
                <tr key={v.id} data-testid={`vendor-row-${v.code}`}>
                  <td>
                    <b className="mono">{v.code}</b>
                  </td>
                  <td>{v.name}</td>
                  <td>{v.city || "—"}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {v.gstin || "—"}
                  </td>
                  <td>
                    {v.brand_names.length ? (
                      v.brand_names.join(", ")
                    ) : (
                      <span className="muted-cell">Any brand</span>
                    )}
                  </td>
                  <td>{v.payment_terms || "—"}</td>
                  <td>
                    <span className={`chip chip-${v.is_active ? "green" : "red"}`}>
                      {v.is_active ? "Active" : "Retired"}
                    </span>
                  </td>
                  {canEdit && (
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={() => edit(v)}
                        data-testid={`edit-vendor-${v.code}`}
                      >
                        <Pencil size={13} /> Edit
                      </button>
                    </td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Screen>
  );
}
