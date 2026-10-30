import { useCallback, useEffect, useRef, useState } from "react";

import { Download } from "lucide-react";

import { KpiStrip } from "../components/KpiStrip";
import type { Kpi } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { Section } from "../components/Section";
import { EmptyState, Loading } from "../components/States";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { alignsRight, columnHeading, gstCell, maskUnknownFigures } from "../lib/gstReport";
import type { GstColumn, GstRow } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import {
  defaultStaffFilters,
  paiseToRupeeText,
  rupeesToPaise,
  staffParams,
  targetMonth,
} from "../lib/staffReport";
import type { StaffFilters } from "../lib/staffReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["StaffReport"]>;
type Targets = ApiRead<ApiSchemas["StaffTargets"]>;

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Staff Performance (store operations PRD ST-RPT-4, ticket 46).
 *
 *  Each salesperson's sales, bills, units per bill, average bill value,
 *  returns against their sales and target achievement, using split shares. A
 *  salesperson is sent only their own row; the manager, the team - the server
 *  decides which, and which stores. Whoever sets store targets also sets each
 *  person's monthly target here. It reads live data only: offline it says it
 *  needs a connection, and comes back by itself when the line does. */
export function StaffPerformanceReportPage() {
  const [filters, setFilters] = useState<StaffFilters>(() => defaultStaffFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: StaffFilters) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    if (wanted.date_from && wanted.date_to && wanted.date_to < wanted.date_from) {
      setError("The period ends before it starts. Choose a To date on or after the From date.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await api.get<Payload>("/reports/staff", {
        params: staffParams(wanted),
      });
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
    void load(filters);
  }, [filters, load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(filters);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [filters, load]);

  function set<K extends keyof StaffFilters>(key: K, value: StaffFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/staff/export.xlsx", {
        params: staffParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `staff-performance-${filters.date_from}-${filters.date_to}.xlsx`;
      link.click();
      URL.revokeObjectURL(href);
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
        return;
      }
      setError(await blobErrorMessage(reason));
    }
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <Note
      tone="warn"
      role="status"
      testId="staff-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="staff-retry"
            disabled={busy}
            onClick={() => void load(filters)}
          >
            Try again
          </button>
        )
      }
    >
      {OFFLINE}
    </Note>
  );
  const errorNote = error && (
    <Note tone="error" testId="staff-error">
      {error}
    </Note>
  );
  const lead =
    "Each salesperson's sales, bills, units per bill, average bill value, returns against their sales and target achievement, with shared bills split by each person's share.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Staff Performance" lead={lead} />
        <div className="page">
          {offlineNote}
          {error
            ? errorNote
            : online && !lost && <Loading label="Loading the staff performance report" />}
        </div>
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = (data.rows as unknown as GstRow[]).map((row) =>
    maskUnknownFigures(row, columns, data.missing),
  );
  const total = maskUnknownFigures(data.total as unknown as GstRow, columns, data.missing);
  const kpis = totalKpis(columns, total, KPI_KEYS, "staff");

  // The period and the store, in the header where the page's own controls go:
  // the filters choose what every figure below is about.
  const filterBar = (
    <div className="page-head-actions" data-testid="staff-filters">
      <label>
        <span className="sr-only">Store</span>
        <select
          className="input"
          style={{ width: "auto" }}
          data-testid="staff-filter-store"
          value={filters.store}
          disabled={disabled}
          onChange={(event) => set("store", event.target.value)}
        >
          <option value="">All my stores</option>
          {data.store_options.map((s) => (
            <option key={s.id} value={s.code}>
              {s.name} ({s.code})
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="sr-only">From</span>
        <input
          className="input"
          type="date"
          style={{ width: "auto" }}
          data-testid="staff-filter-from"
          value={filters.date_from}
          disabled={disabled}
          onChange={(event) => set("date_from", event.target.value)}
        />
      </label>
      <span className="muted-cell" aria-hidden="true">
        to
      </span>
      <label>
        <span className="sr-only">To</span>
        <input
          className="input"
          type="date"
          style={{ width: "auto" }}
          data-testid="staff-filter-to"
          value={filters.date_to}
          disabled={disabled}
          onChange={(event) => set("date_to", event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn"
        data-testid="staff-export"
        disabled={disabled}
        onClick={() => void exportWorkbook()}
      >
        <Download size={16} aria-hidden="true" /> Export to Excel
      </button>
    </div>
  );

  return (
    <OperationsPage>
      <PageHeader title="Staff Performance" lead={lead} actions={filterBar} />
      <div className="page">
        {offlineNote}
        {errorNote}
        {data.missing.length > 0 && (
          <Note tone="warn" testId="staff-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`staff-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </Note>
        )}
        {rows.length > 0 && kpis.length > 0 && (
          <KpiStrip items={kpis} label="The period in figures" />
        )}

        <Section
          heading={data.view === "own" ? "Your results" : "By salesperson"}
          ruled={rows.length > 0 && kpis.length > 0}
          aside={
            <p className="muted-cell" data-testid="staff-as-of" style={{ margin: 0 }}>
              {asOfText(data.as_of)}
              {data.as_of &&
                ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
              {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
            </p>
          }
        >
          <p className="muted-cell" data-testid="staff-view">
            {data.view === "own"
              ? "Your own results."
              : "Your team: every salesperson at the stores shown."}
          </p>
          {rows.length === 0 ? (
            <EmptyState testId="staff-empty">
              {busy ? "Loading…" : "No sales in this period at these stores."}
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="data freeze-first" data-testid="staff-table">
                <thead>
                  <tr>
                    {columns.map((c, index) => (
                      <th
                        key={c.key}
                        scope="col"
                        className={cellClass(c, index) || undefined}
                        data-testid={`staff-col-${c.key}`}
                      >
                        {columnHeading(c.label)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <StaffTableRow key={String(row.key ?? index)} row={row} columns={columns} />
                  ))}
                </tbody>
                <tfoot>
                  <StaffTableRow row={total} columns={columns} total />
                </tfoot>
              </table>
            </div>
          )}
          <details className="muted-cell" data-testid="staff-basis">
            <summary>How these figures are worked out (formula {data.formula_version})</summary>
            <ul>
              {data.basis.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </details>
        </Section>
        {data.can_set_targets && data.store_options.length > 0 && (
          <TargetEditor
            stores={data.store_options}
            initialStore={data.store ?? data.store_options[0]?.code ?? ""}
            month={targetMonth(filters)}
            online={online}
            onSaved={() => void load(filters)}
          />
        )}
      </div>
    </OperationsPage>
  );
}

/** The totals the strip states, in this order, when the view has them. Target
 *  achievement is sent only where targets are shown. */
const KPI_KEYS = ["value_paise", "bills", "upt", "abv_paise", "target_pct"];

/** Column priority (v2 Table): net sales and the ratios always show; the
 *  pieces and the target amount behind them come in on wider screens (the
 *  spreadsheet always has every column). */
const PRIORITY: Partial<Record<string, "p2" | "p3">> = {
  sold_pieces: "p2",
  returned_pieces: "p2",
  target_paise: "p2",
};

/** A cell's classes: figures right-aligned; the first column never hides. */
function cellClass(c: GstColumn, index: number, figure = "num"): string {
  return [alignsRight(c.kind) ? figure : "", index > 0 ? (PRIORITY[c.key] ?? "") : ""]
    .filter(Boolean)
    .join(" ");
}

/** The total row's figures as a KpiStrip, from what the table already holds. */
function totalKpis(
  columns: GstColumn[],
  total: GstRow,
  keys: readonly string[],
  prefix: string,
): Kpi[] {
  return keys
    .map((key) => columns.find((c) => c.key === key))
    .filter((c): c is GstColumn => c !== undefined && total[c.key] !== undefined)
    .map((c) => ({
      label: columnHeading(c.label),
      value: gstCell(total[c.key], c.kind),
      testId: `${prefix}-kpi-${c.key}`,
    }));
}

function StaffTableRow({
  row,
  columns,
  total,
}: {
  row: GstRow;
  columns: GstColumn[];
  total?: boolean;
}) {
  return (
    <tr data-testid={total ? "staff-total" : "staff-row"} data-key={String(row.key ?? "")}>
      {columns.map((c, index) => {
        const text = gstCell(row[c.key], c.kind);
        return (
          <td
            key={c.key}
            className={cellClass(c, index, "num tabular") || undefined}
            data-testid={`staff-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
    </tr>
  );
}

type Person = Targets["people"][number];

/** Set each salesperson's target for one month at one store. Each save is one
 *  audited change on the server; a save whose answer was lost is sent again
 *  with the same command id, so it is saved once. */
function TargetEditor({
  stores,
  initialStore,
  month,
  online,
  onSaved,
}: {
  stores: Payload["store_options"];
  initialStore: string;
  month: string;
  online: boolean;
  onSaved: () => void;
}) {
  const [store, setStore] = useState(initialStore);
  const [targets, setTargets] = useState<Targets | null>(null);
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [saving, setSaving] = useState("");
  // A save whose answer never came keeps its command id for the resend.
  const pending = useRef<Record<string, string>>({});

  // Only the newest load may draw the list, so a save never pairs one store's
  // people with another store or month.
  const loads = useRef(0);

  const load = useCallback(async () => {
    const mine = ++loads.current;
    setTargets(null);
    setTyped({});
    if (!navigator.onLine) return;
    setError("");
    try {
      const response = await api.get<Targets>("/reports/staff/targets", {
        params: { store, month },
      });
      if (mine !== loads.current) return;
      setTargets(response.data);
      setTyped(
        Object.fromEntries(
          response.data.people.map((p) => [p.staff_id, paiseToRupeeText(p.target_paise)]),
        ),
      );
    } catch (reason) {
      if (mine !== loads.current) return;
      setError(
        isConnectionLost(reason)
          ? "The targets could not be loaded: the connection dropped."
          : apiErrorMessage(reason),
      );
    }
  }, [store, month]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(list: Targets, person: Person) {
    const paise = rupeesToPaise(typed[person.staff_id] ?? "");
    if (paise === null) {
      setNotes((n) => ({
        ...n,
        [person.staff_id]: "Type the target in rupees, such as 150000.",
      }));
      return;
    }
    if (!navigator.onLine) {
      setNotes((n) => ({
        ...n,
        [person.staff_id]: "Not saved: you are offline. Save again once reconnected.",
      }));
      return;
    }
    // The store and month the list on screen was loaded for, never the picker's.
    const key = `${list.store}:${list.month}:${person.staff_id}:${paise}:${person.revision}`;
    const commandId = pending.current[key] ?? crypto.randomUUID();
    pending.current[key] = commandId;
    setSaving(person.staff_id);
    setNotes((n) => ({ ...n, [person.staff_id]: "" }));
    try {
      await api.put("/reports/staff/targets", {
        command_id: commandId,
        store: list.store,
        staff_id: person.staff_id,
        month: list.month.slice(0, 7),
        target_paise: paise,
        revision: person.revision,
      });
      delete pending.current[key];
      setNotes((n) => ({ ...n, [person.staff_id]: "Saved." }));
      await load();
      onSaved();
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setNotes((n) => ({
          ...n,
          [person.staff_id]:
            "Not saved yet: the connection dropped. Save again - it is only ever saved once.",
        }));
      } else {
        delete pending.current[key];
        setNotes((n) => ({ ...n, [person.staff_id]: apiErrorMessage(reason) }));
      }
    } finally {
      setSaving("");
    }
  }

  return (
    <Section
      heading="Monthly targets"
      ruled
      testId="staff-targets"
      aside={
        <label>
          <span className="sr-only">Store</span>
          <select
            className="input"
            style={{ width: "auto" }}
            data-testid="staff-targets-store"
            value={store}
            onChange={(event) => setStore(event.target.value)}
          >
            {stores.map((s) => (
              <option key={s.id} value={s.code}>
                {s.name} ({s.code})
              </option>
            ))}
          </select>
        </label>
      }
    >
      <p className="muted-cell">
        Each salesperson's net sales target for {month} at one store. Changes are recorded in the
        audit log.
      </p>
      {error && (
        <Note tone="error" testId="staff-targets-error">
          {error}
        </Note>
      )}
      {targets && !targets.switched_on && (
        <Note tone="warn" testId="staff-targets-off">
          The staff performance report is switched off at this store, so no target can be set here.
        </Note>
      )}
      {targets && targets.people.length === 0 && (
        <p className="muted-cell">No salespeople on this store's staff list.</p>
      )}
      {targets && targets.people.length > 0 && (
        <div className="table-wrap">
          <table className="data freeze-first" data-testid="staff-targets-table">
            <thead>
              <tr>
                <th scope="col">Salesperson</th>
                <th scope="col" className="num">
                  Target (₹)
                </th>
                <th scope="col" className="act">
                  <span className="sr-only">Save</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {targets.people.map((p) => (
                <tr key={p.staff_id} data-testid="staff-target-row" data-code={p.code}>
                  <td>
                    {p.name} ({p.code}){p.active ? "" : " - no longer here"}
                  </td>
                  <td className="num">
                    <input
                      className="input"
                      inputMode="decimal"
                      aria-label={`Target for ${p.name}`}
                      data-testid="staff-target-input"
                      value={typed[p.staff_id] ?? ""}
                      onChange={(event) =>
                        setTyped((t) => ({ ...t, [p.staff_id]: event.target.value }))
                      }
                    />
                  </td>
                  <td className="act">
                    <button
                      type="button"
                      className="btn btn-sm"
                      data-testid="staff-target-save"
                      disabled={!online || !targets.switched_on || saving === p.staff_id}
                      onClick={() => void save(targets, p)}
                    >
                      Save
                    </button>{" "}
                    <span className="muted-cell" data-testid="staff-target-note" role="status">
                      {notes[p.staff_id]}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

/** A refused download arrives as a Blob; read the server's sentence out of it. */
async function blobErrorMessage(reason: unknown): Promise<string> {
  const data = (reason as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text()) as unknown;
      return apiErrorMessage({ response: { data: parsed } });
    } catch {
      return "The export could not be made. Please try again.";
    }
  }
  return apiErrorMessage(reason);
}
