import { useCallback, useEffect, useRef, useState } from "react";

import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { alignsRight, gstCell } from "../lib/gstReport";
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
    <p className="warn-note" data-testid="staff-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="staff-retry"
            disabled={busy}
            onClick={() => void load(filters)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const lead =
    "Each salesperson's sales, bills, units per bill, average bill value, returns against their sales and target achievement, with shared bills split by each person's share.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Staff Performance" lead={lead} />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="staff-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the staff performance report…</p>
        )}
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = data.rows as unknown as GstRow[];
  const total = data.total as unknown as GstRow;

  return (
    <OperationsPage>
      <PageHeader title="Staff Performance" lead={lead} />
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="staff-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
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
          <label className="field">
            <span>From</span>
            <input
              className="input"
              type="date"
              data-testid="staff-filter-from"
              value={filters.date_from}
              disabled={disabled}
              onChange={(event) => set("date_from", event.target.value)}
            />
          </label>
          <label className="field">
            <span>To</span>
            <input
              className="input"
              type="date"
              data-testid="staff-filter-to"
              value={filters.date_to}
              disabled={disabled}
              onChange={(event) => set("date_to", event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="staff-export"
            disabled={disabled}
            onClick={() => void exportWorkbook()}
          >
            Export to Excel
          </button>
        </div>
        <p className="muted-cell" data-testid="staff-view">
          {data.view === "own"
            ? "Your own results."
            : "Your team: every salesperson at the stores shown."}
        </p>
        <p className="muted-cell" data-testid="staff-as-of">
          {asOfText(data.as_of)}
          {data.as_of &&
            ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {error && (
          <p className="warn-note" data-testid="staff-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="staff-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`staff-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {rows.length === 0 ? (
          <p className="muted-cell" data-testid="staff-empty">
            {busy ? "Loading…" : "No sales in this period at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="staff-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={alignsRight(c.kind) ? "num" : undefined}
                      data-testid={`staff-col-${c.key}`}
                    >
                      {c.label}
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
      </section>
      {data.can_set_targets && data.store_options.length > 0 && (
        <TargetEditor
          stores={data.store_options}
          initialStore={data.store ?? data.store_options[0]?.code ?? ""}
          month={targetMonth(filters)}
          online={online}
          onSaved={() => void load(filters)}
        />
      )}
    </OperationsPage>
  );
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
            className={alignsRight(c.kind) ? "num tabular" : undefined}
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
    <section className="card section-card" data-testid="staff-targets">
      <h2>Monthly targets</h2>
      <p className="muted-cell">
        Each salesperson's net sales target for {month} at one store. Changes are recorded in the
        audit log.
      </p>
      <div className="toolbar">
        <label className="field">
          <span>Store</span>
          <select
            className="input"
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
      </div>
      {error && (
        <p className="warn-note" data-testid="staff-targets-error">
          {error}
        </p>
      )}
      {targets && !targets.switched_on && (
        <p className="warn-note" data-testid="staff-targets-off">
          The staff performance report is switched off at this store, so no target can be set here.
        </p>
      )}
      {targets && targets.people.length === 0 && (
        <p className="muted-cell">No salespeople on this store's staff list.</p>
      )}
      {targets && targets.people.length > 0 && (
        <div className="table-wrap">
          <table className="data" data-testid="staff-targets-table">
            <thead>
              <tr>
                <th>Salesperson</th>
                <th className="num">Target (Rs)</th>
                <th />
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
                  <td>
                    <button
                      type="button"
                      className="btn"
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
    </section>
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
