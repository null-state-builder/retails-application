import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorCode, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  KIND_WORDS,
  changed,
  count,
  lastMonth,
  moved,
  newColumn,
  reportFileName,
  withField,
  without,
} from "../lib/brandReports";
import type { LayoutBody, LayoutColumn, LayoutField, ReportKind } from "../lib/brandReports";
import { blobErrorMessage } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import { pendingCommand, type PendingCommand } from "../lib/sorAgeing";
import "./Shared.css";

type Month = ApiRead<ApiSchemas["BrandMonth"]>;
type Layouts = ApiRead<ApiSchemas["BrandLayouts"]>;
type SavedLayout = ApiRead<ApiSchemas["BrandLayout"]>;

const MONTH_API = "/reports/brand-reports";
const LAYOUTS_API = "/reports/brand-layouts";
const OFFLINE =
  "Brand reports need a connection. Reconnect to see the month, make a report or save a layout; a layout you are editing stays here.";
const KINDS: ReportKind[] = ["sale", "soh"];

/** Save the file the server sent under `name`. */
function saveBlob(data: Blob, name: string) {
  const href = URL.createObjectURL(data);
  const link = document.createElement("a");
  link.href = href;
  link.download = name;
  link.click();
  URL.revokeObjectURL(href);
}

/** Brands > Reports (store operations PRD ST-BRD-2, ticket 29).
 *
 *  Each brand's Sale and SOH report for one store and month, laid out the way
 *  that brand wants it - columns, order, headers, formats and file type - and
 *  made here instead of by hand. The KDPS Sale and SOH sheets are the first
 *  layouts; a brand with none of its own uses them. Last month's reports are
 *  made by themselves once the month has ended and kept exactly as made; any
 *  report can also be made now. Accounts saves the layouts.
 *
 *  The server decides which stores, which brands' lines are whose, and what a
 *  file says about its basis and as-of time. Everything is live: offline the
 *  page says it needs a connection, and a layout being edited is kept. */
export function BrandReportsPage() {
  const [store, setStore] = useState("");
  const [month, setMonth] = useState(() => lastMonth());
  const [data, setData] = useState<Month | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [making, setMaking] = useState("");
  const [otherBrand, setOtherBrand] = useState("");
  const [otherKind, setOtherKind] = useState<ReportKind>("sale");
  const request = useRef(0);

  const load = useCallback(async (wantedStore: string, wantedMonth: string) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    setBusy(true);
    setError("");
    try {
      const params: Record<string, string> = { month: wantedMonth };
      if (wantedStore) params.store = wantedStore;
      const response = await api.get<Month>(MONTH_API, { params });
      if (mine !== request.current) return;
      setLost(false);
      setData(response.data);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      if (mine === request.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load(store, month);
  }, [store, month, load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(store, month);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [store, month, load]);

  async function make(brand: { id: number; code: string }, kind: ReportKind, fileType?: string) {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    if (!data?.store) return;
    const key = `${brand.id}:${kind}`;
    setMaking(key);
    setError("");
    try {
      const response = await api.get<Blob>(`${MONTH_API}/make`, {
        params: { store: data.store, brand: brand.id, kind, month: data.month },
        responseType: "blob",
      });
      const isCsv = fileType ? fileType === "csv" : response.data.type.startsWith("text/csv");
      saveBlob(response.data, reportFileName(brand.code, kind, data.store, data.month, isCsv));
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(await blobErrorMessage(reason));
    } finally {
      setMaking("");
    }
  }

  async function takeKept(file: Month["files"][number]) {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>(`${MONTH_API}/files/${file.id}`, { responseType: "blob" });
      saveBlob(response.data, file.file_name);
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(await blobErrorMessage(reason));
    }
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="brand-reports-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="brand-reports-retry"
            disabled={busy}
            onClick={() => void load(store, month)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const lead =
    "Each brand's Sale and SOH report for a store and month, in the brand's own layout. Last month's are made by themselves; any can be made now.";

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Brand Reports" lead={lead} />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="brand-reports-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the brand reports…</p>
        )}
      </div>
    );
  }

  const onePicked = Boolean(data.store);
  return (
    <div className="page-pad">
      <PageHeader title="Brand Reports" lead={lead} />
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="brand-reports-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="brand-reports-store"
              value={store}
              disabled={disabled}
              onChange={(event) => setStore(event.target.value)}
            >
              <option value="">All my stores</option>
              {data.store_options.map((s) => (
                <option key={s.id} value={s.code}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Month</span>
            <input
              className="input"
              type="month"
              data-testid="brand-reports-month"
              value={month}
              disabled={disabled}
              onChange={(event) => event.target.value && setMonth(event.target.value)}
            />
          </label>
        </div>
        <p className="muted-cell" data-testid="brand-reports-as-of">
          {`Sales: ${asOfText(data.sale_as_of)} · Stock: ${asOfText(data.soh_as_of)}`}
          {` · the copies are brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
          {data.running && " (the month is still running)"}
        </p>
        {error && (
          <p className="warn-note" data-testid="brand-reports-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="brand-reports-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`brand-reports-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {!onePicked && (
          <p className="muted-cell" data-testid="brand-reports-pick-store">
            A report is for one store. Choose a store to make one.
          </p>
        )}
        {data.brands.length === 0 ? (
          <p className="muted-cell" data-testid="brand-reports-empty">
            {busy ? "Loading…" : "No brand sold or held stock at these stores in this month."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="brand-reports-table">
              <thead>
                <tr>
                  <th>Brand</th>
                  <th className="num">Bill lines</th>
                  <th className="num">Pieces sold</th>
                  <th className="num">Pieces in stock</th>
                  <th>Layouts</th>
                  <th>Make now</th>
                </tr>
              </thead>
              <tbody>
                {data.brands.map((b) => (
                  <tr key={b.id} data-testid="brand-reports-row" data-brand={b.code}>
                    <td>
                      {b.name} ({b.code})
                    </td>
                    <td className="num tabular" data-testid="brand-reports-lines">
                      {count(b.bill_lines)}
                    </td>
                    <td className="num tabular" data-testid="brand-reports-sold">
                      {count(b.pieces_sold)}
                    </td>
                    <td className="num tabular" data-testid="brand-reports-stock">
                      {count(b.pieces_in_stock)}
                    </td>
                    <td className="muted-cell" data-testid="brand-reports-layouts">
                      {KINDS.map((kind) => {
                        const ref = b.layouts[kind];
                        return (
                          <div key={kind}>
                            {KIND_WORDS[kind]}: {ref.own ? "own layout" : "standard"} (.{ref.file_type})
                          </div>
                        );
                      })}
                    </td>
                    <td>
                      {KINDS.map((kind) => (
                        <button
                          key={kind}
                          type="button"
                          className="btn"
                          data-testid={`brand-reports-make-${kind}`}
                          disabled={disabled || !onePicked || making !== ""}
                          onClick={() => void make(b, kind, b.layouts[kind].file_type)}
                        >
                          {making === `${b.id}:${kind}` ? "Making…" : KIND_WORDS[kind]}
                        </button>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="toolbar" data-testid="brand-reports-any">
          <label className="field">
            <span>Any brand</span>
            <select
              className="input"
              data-testid="brand-reports-any-brand"
              value={otherBrand}
              disabled={disabled}
              onChange={(event) => setOtherBrand(event.target.value)}
            >
              <option value="">Choose a brand</option>
              {data.brand_options.map((b) => (
                <option key={b.id} value={String(b.id)}>
                  {b.name} ({b.code})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Report</span>
            <select
              className="input"
              data-testid="brand-reports-any-kind"
              value={otherKind}
              disabled={disabled}
              onChange={(event) => setOtherKind(event.target.value as ReportKind)}
            >
              {KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {KIND_WORDS[kind]}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="brand-reports-any-make"
            disabled={disabled || !onePicked || !otherBrand || making !== ""}
            onClick={() => {
              const brand = data.brand_options.find((b) => String(b.id) === otherBrand);
              if (brand) void make(brand, otherKind);
            }}
          >
            Make it now
          </button>
        </div>
        <details className="muted-cell" data-testid="brand-reports-basis">
          <summary>What each report holds</summary>
          <strong>Sale</strong>
          <ul>
            {data.sale_basis.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <strong>SOH</strong>
          <ul>
            {data.soh_basis.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <p>Each Excel file also has an "About this report" sheet stating its basis, as-of time and missing data.</p>
        </details>
      </section>

      <section className="card section-card" data-testid="brand-reports-kept">
        <h2>Made at month end</h2>
        {data.runs.length === 0 && data.files.length === 0 ? (
          <p className="muted-cell" data-testid="brand-reports-kept-none">
            {data.running
              ? "This month's reports are made by themselves once it has ended."
              : "Nothing was made for this month at these stores."}
          </p>
        ) : (
          <>
            <p className="muted-cell">
              {data.runs.map((r) => `${r.store.code}: ${r.files} file(s), ${asOfText(r.made_at)}`).join(" · ")}
            </p>
            <div className="table-wrap">
              <table className="data" data-testid="brand-reports-kept-table">
                <thead>
                  <tr>
                    <th>Store</th>
                    <th>Brand</th>
                    <th>Report</th>
                    <th className="num">Rows</th>
                    <th>File</th>
                  </tr>
                </thead>
                <tbody>
                  {data.files.map((f) => (
                    <tr key={f.id} data-testid="brand-reports-kept-row">
                      <td>{f.store.code}</td>
                      <td>{f.brand.name}</td>
                      <td>
                        {KIND_WORDS[f.kind as ReportKind]}
                        {f.missing.length > 0 && (
                          <span className="muted-cell" title={f.missing.map((m) => m.text).join(" ")}>
                            {" "}
                            (with notes)
                          </span>
                        )}
                      </td>
                      <td className="num tabular">{count(f.rows)}</td>
                      <td>
                        <button
                          type="button"
                          className="btn"
                          data-testid="brand-reports-kept-take"
                          disabled={disabled}
                          onClick={() => void takeKept(f)}
                        >
                          {f.file_name}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <LayoutEditor online={online} onLost={() => setLost(true)} onSaved={() => void load(store, month)} />
    </div>
  );
}

interface Target {
  brandId: number | null;
  kind: ReportKind;
}

/** The layouts: the company's standard ones and each brand's own. Accounts saves. */
function LayoutEditor({
  online,
  onLost,
  onSaved,
}: {
  online: boolean;
  onLost: () => void;
  onSaved: () => void;
}) {
  const [layouts, setLayouts] = useState<Layouts | null>(null);
  const [target, setTarget] = useState<Target>({ brandId: null, kind: "sale" });
  const [draft, setDraft] = useState<LayoutBody | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState<{ text: string; ok: boolean } | null>(null);
  const [saving, setSaving] = useState(false);
  const pending = useRef<PendingCommand | null>(null);

  const load = useCallback(async () => {
    if (!navigator.onLine) return;
    try {
      const response = await api.get<Layouts>(LAYOUTS_API);
      setLayouts(response.data);
      setError("");
    } catch (reason) {
      if (isConnectionLost(reason)) onLost();
      else setError(apiErrorMessage(reason));
    }
  }, [onLost]);

  useEffect(() => {
    void load();
  }, [load]);

  const saved: SavedLayout | null = useMemo(() => {
    if (!layouts) return null;
    if (target.brandId === null) return layouts.standard[target.kind];
    const brand = layouts.brands.find((b) => b.id === target.brandId);
    return (brand?.[target.kind] as SavedLayout | null) ?? null;
  }, [layouts, target]);
  const startingPoint: LayoutBody | null = useMemo(() => {
    if (!layouts) return null;
    return (saved ?? layouts.standard[target.kind]).layout as LayoutBody;
  }, [layouts, saved, target.kind]);

  // A new target starts the draft from what is saved for it; a reload after a
  // save or a lost answer keeps what is being typed.
  const draftKey = `${target.brandId ?? "standard"}:${target.kind}:${layouts ? "ready" : "none"}`;
  useEffect(() => {
    setDraft(startingPoint ? structuredClone(startingPoint) : null);
    setNote(null);
    pending.current = null;
  }, [draftKey]); // only a new target (or the first load) resets the draft

  if (!layouts || !draft || !startingPoint) {
    return (
      <section className="card section-card" data-testid="brand-layouts">
        <h2>Layouts</h2>
        {error ? (
          <p className="warn-note" data-testid="brand-layouts-error">
            {error}
          </p>
        ) : (
          <p className="muted-cell">Loading the layouts…</p>
        )}
      </section>
    );
  }

  const fields = layouts.field_catalogue[target.kind] as LayoutField[];
  const fieldOf = (key: string) => fields.find((f) => f.key === key);
  const formatWords = new Map(layouts.formats.map((f) => [f.key, f.label]));
  const editable = layouts.can_edit;
  const dirty = changed(draft, startingPoint);
  const brandName =
    target.brandId === null
      ? "Company standard"
      : (layouts.brands.find((b) => b.id === target.brandId)?.name ?? "");

  function patch(next: Partial<LayoutBody>) {
    setDraft((current) => (current ? { ...current, ...next } : current));
    setNote(null);
  }
  function setColumn(index: number, column: LayoutColumn) {
    if (!draft) return;
    patch({ columns: draft.columns.map((c, i) => (i === index ? column : c)) });
  }

  async function save() {
    if (!draft) return;
    if (!navigator.onLine) {
      onLost();
      setNote({
        text: "Not saved: there is no connection. Your layout stays here; press Save again when back.",
        ok: false,
      });
      return;
    }
    const body = {
      brand_id: target.brandId,
      kind: target.kind,
      // A layout never saved (a brand's first, or the built-in KDPS sheet) has none.
      expected_revision: saved && saved.id !== null ? saved.revision : null,
      layout: draft,
    };
    // The very same save again, after a lost answer, replays the same command.
    pending.current = pendingCommand(pending.current, JSON.stringify(body), () => crypto.randomUUID());
    setSaving(true);
    setError("");
    try {
      await api.post(LAYOUTS_API, { command_id: pending.current.commandId, ...body });
      pending.current = null;
      const response = await api.get<Layouts>(LAYOUTS_API);
      setLayouts(response.data);
      setNote({ text: `Saved. ${brandName} ${KIND_WORDS[target.kind]} reports use it from now on.`, ok: true });
      onSaved();
    } catch (reason) {
      if (isConnectionLost(reason) || apiErrorCode(reason) === "OUTCOME_UNKNOWN") {
        onLost();
        setNote({
          text: "We could not tell whether it was saved. Press Save again: it is saved once.",
          ok: false,
        });
      } else {
        pending.current = null;
        setError(apiErrorMessage(reason));
      }
    } finally {
      setSaving(false);
    }
  }

  const locked = !editable || saving;
  return (
    <section className="card section-card" data-testid="brand-layouts">
      <h2>Layouts</h2>
      <p className="muted-cell">
        A brand with no layout of its own uses the company standard: the KDPS Sale and SOH sheets.
        {!editable && " Only Accounts can change a layout."}
      </p>
      <div className="toolbar">
        <label className="field">
          <span>Brand</span>
          <select
            className="input"
            data-testid="brand-layouts-brand"
            value={target.brandId === null ? "" : String(target.brandId)}
            onChange={(event) =>
              setTarget((t) => ({ ...t, brandId: event.target.value ? Number(event.target.value) : null }))
            }
          >
            <option value="">Company standard</option>
            {layouts.brands.map((b) => (
              <option key={b.id} value={String(b.id)}>
                {b.name} ({b.code}){b.sale || b.soh ? " - own layout" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Report</span>
          <select
            className="input"
            data-testid="brand-layouts-kind"
            value={target.kind}
            onChange={(event) => setTarget((t) => ({ ...t, kind: event.target.value as ReportKind }))}
          >
            {KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {KIND_WORDS[kind]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="muted-cell" data-testid="brand-layouts-state">
        {!saved
          ? "No layout of its own yet: this starts from the company standard, and saving gives the brand its own."
          : saved.id === null
            ? `${saved.name}, built in: your company has not saved its own yet.`
            : `${saved.name}, revision ${saved.revision}.`}
      </p>

      <div className="toolbar">
        <label className="field">
          <span>File type</span>
          <select
            className="input"
            data-testid="brand-layouts-file-type"
            value={draft.file_type}
            disabled={locked}
            onChange={(event) => patch({ file_type: event.target.value as LayoutBody["file_type"] })}
          >
            {layouts.file_types.map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Sheet name</span>
          <input
            className="input"
            data-testid="brand-layouts-sheet"
            value={draft.sheet_name}
            disabled={locked}
            onChange={(event) => patch({ sheet_name: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Font</span>
          <input
            className="input"
            data-testid="brand-layouts-font"
            value={draft.font}
            disabled={locked}
            onChange={(event) => patch({ font: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Header border</span>
          <select
            className="input"
            data-testid="brand-layouts-header-border"
            value={draft.header_border ?? "grid"}
            disabled={locked}
            onChange={(event) => patch({ header_border: event.target.value as LayoutBody["header_border"] })}
          >
            {layouts.header_borders.map((b) => (
              <option key={b.key} value={b.key}>
                {b.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Header size</span>
          <input
            className="input"
            type="number"
            min={8}
            max={24}
            data-testid="brand-layouts-header-size"
            value={draft.header_size}
            disabled={locked}
            onChange={(event) => patch({ header_size: Number(event.target.value) })}
          />
        </label>
      </div>

      <h3>Title lines</h3>
      <p className="muted-cell">
        Can name {layouts.placeholders.map((p) => `{${p.key}}`).join(", ")}; in capitals ({"{BRAND}"}) the
        value is written in capitals.
      </p>
      {draft.title_lines.map((line, index) => (
        <div className="toolbar" key={index} data-testid="brand-layouts-title">
          <input
            className="input"
            style={{ flex: 1 }}
            aria-label={`Title line ${index + 1}`}
            value={line.text}
            disabled={locked}
            onChange={(event) =>
              patch({
                title_lines: draft.title_lines.map((l, i) => (i === index ? { ...l, text: event.target.value } : l)),
              })
            }
          />
          <input
            className="input"
            type="number"
            min={8}
            max={24}
            aria-label={`Title line ${index + 1} size`}
            value={line.size}
            disabled={locked}
            onChange={(event) =>
              patch({
                title_lines: draft.title_lines.map((l, i) =>
                  i === index ? { ...l, size: Number(event.target.value) } : l,
                ),
              })
            }
          />
          <button
            type="button"
            className="btn"
            disabled={locked}
            onClick={() => patch({ title_lines: without(draft.title_lines, index) })}
          >
            Remove
          </button>
        </div>
      ))}
      {editable && draft.title_lines.length < 5 && (
        <button
          type="button"
          className="btn"
          data-testid="brand-layouts-add-title"
          disabled={locked}
          onClick={() => patch({ title_lines: [...draft.title_lines, { text: "", size: 12 }] })}
        >
          Add a title line
        </button>
      )}

      <h3>Columns, in order</h3>
      <div className="table-wrap">
        <table className="data" data-testid="brand-layouts-columns">
          <thead>
            <tr>
              <th>#</th>
              <th>Shows</th>
              <th>Header</th>
              <th>Format</th>
              <th>Total</th>
              <th>Move</th>
            </tr>
          </thead>
          <tbody>
            {draft.columns.map((column, index) => {
              const field = fieldOf(column.field);
              return (
                <tr key={index} data-testid="brand-layouts-column">
                  <td className="tabular">{index + 1}</td>
                  <td>
                    <select
                      className="input"
                      aria-label={`Column ${index + 1} shows`}
                      data-testid="brand-layouts-column-field"
                      value={column.field}
                      disabled={locked}
                      onChange={(event) => {
                        const next = fieldOf(event.target.value);
                        if (next) setColumn(index, withField(column, next));
                      }}
                    >
                      {fields.map((f) => (
                        <option key={f.key} value={f.key}>
                          {f.label}
                        </option>
                      ))}
                    </select>
                    {column.field === "fixed" && (
                      <input
                        className="input"
                        aria-label={`Column ${index + 1} fixed text`}
                        data-testid="brand-layouts-column-value"
                        value={column.value ?? ""}
                        disabled={locked}
                        onChange={(event) => setColumn(index, { ...column, value: event.target.value })}
                      />
                    )}
                  </td>
                  <td>
                    <input
                      className="input"
                      aria-label={`Column ${index + 1} header`}
                      data-testid="brand-layouts-column-header"
                      value={column.header}
                      disabled={locked}
                      onChange={(event) => setColumn(index, { ...column, header: event.target.value })}
                    />
                  </td>
                  <td>
                    <select
                      className="input"
                      aria-label={`Column ${index + 1} format`}
                      value={column.format}
                      disabled={locked}
                      onChange={(event) => setColumn(index, { ...column, format: event.target.value })}
                    >
                      {(field?.formats ?? [column.format]).map((f) => (
                        <option key={f} value={f}>
                          {formatWords.get(f) ?? f}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Column ${index + 1} totalled`}
                      checked={column.total}
                      disabled={locked || !field?.can_total}
                      onChange={(event) => setColumn(index, { ...column, total: event.target.checked })}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      aria-label={`Move column ${index + 1} up`}
                      disabled={locked || index === 0}
                      onClick={() => patch({ columns: moved(draft.columns, index, -1) })}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className="btn"
                      aria-label={`Move column ${index + 1} down`}
                      disabled={locked || index === draft.columns.length - 1}
                      onClick={() => patch({ columns: moved(draft.columns, index, 1) })}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className="btn"
                      aria-label={`Remove column ${index + 1}`}
                      data-testid="brand-layouts-column-remove"
                      disabled={locked || draft.columns.length === 1}
                      onClick={() => patch({ columns: without(draft.columns, index) })}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {editable && (
        <div className="toolbar">
          <button
            type="button"
            className="btn"
            data-testid="brand-layouts-add-column"
            disabled={locked || draft.columns.length >= 40}
            onClick={() => patch({ columns: [...draft.columns, newColumn(fields[0])] })}
          >
            Add a column
          </button>
          <button
            type="button"
            className="btn"
            data-testid="brand-layouts-undo"
            disabled={locked || !dirty}
            onClick={() => {
              setDraft(structuredClone(startingPoint));
              setNote(null);
              pending.current = null;
            }}
          >
            Undo changes
          </button>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="brand-layouts-save"
            disabled={locked || !online || (!dirty && saved !== null)}
            onClick={() => void save()}
          >
            {saving ? "Saving…" : "Save layout"}
          </button>
        </div>
      )}
      {error && (
        <p className="warn-note" data-testid="brand-layouts-error">
          {error}
        </p>
      )}
      {note && (
        <p className={note.ok ? "ok-note" : "warn-note"} data-testid="brand-layouts-note" role="status">
          {note.text}
        </p>
      )}
    </section>
  );
}
