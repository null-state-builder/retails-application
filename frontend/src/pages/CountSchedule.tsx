import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { countScreenPath, EVERY_CHOICES, everyText, scopeText } from "../lib/countSchedule";
import { dayText } from "../lib/stockAgeing";

type Payload = ApiRead<ApiSchemas["CountSchedulePage"]>;
type Row = Payload["schedules"][number];
type DueCount = Payload["due_today"][number];

const URL = "/goods-v1/outbound/count-schedules";
const OFFLINE = "The count schedule needs a connection. What you typed is kept; reconnect to save.";

interface Draft {
  site_id: string;
  brand_id: string;
  every: string;
  first_due_on: string;
}

/** Stock Count > Count Schedule (store operations ticket 35, ST-INV-3).
 *
 *  The Owner sets how often each brand, or the whole store, is counted. The
 *  counts due today and the ones missed are listed first, each opening the
 *  store's existing blind count. The server decides what is due, what was
 *  missed and who may change it; this page only shows it. Everything is live
 *  data: offline the page says it needs a connection, keeps what was typed, and
 *  saves nothing until the connection is back. */
export function CountSchedulePage() {
  const [params] = useSearchParams();
  const siteFilter = Number(params.get("site_id")) || null;
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Draft>({
    site_id: "",
    brand_id: "",
    every: "month",
    first_due_on: "",
  });
  const request = useRef(0);

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const response = await api.get<Payload>(URL);
      if (mine !== request.current) return;
      setLost(false);
      setData(response.data);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load();
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  /** Send one write; true once the server has it. A dropped connection keeps
   *  the page as it is (and so what was typed), and says so. */
  async function send(path: string, body: object, done: string): Promise<boolean> {
    if (!navigator.onLine) {
      setOnline(false);
      return false;
    }
    setError("");
    setSaved("");
    setBusy(true);
    let ok = false;
    try {
      await api.post(path, body);
      setSaved(done);
      ok = true;
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
    if (ok) await load();
    return ok;
  }

  async function create() {
    const brand = data?.brands.find((b) => String(b.id) === draft.brand_id);
    const ok = await send(
      URL,
      {
        ...goodsMeta(),
        site_id: Number(draft.site_id),
        brand_id: draft.brand_id ? Number(draft.brand_id) : null,
        every: draft.every,
        first_due_on: draft.first_due_on,
      },
      `Schedule set: ${scopeText(brand?.name)}, ${everyText(draft.every).toLowerCase()}.`,
    );
    if (ok) setDraft((current) => ({ ...current, brand_id: "", first_due_on: "" }));
  }

  const disabled = !online || busy;
  const header = (
    <PageHeader
      title="Count Schedule"
      lead="When each brand, or the whole store, is counted. The count due today opens the store's blind count; a count not done by its day is raised as an exception and a count-due alert."
    />
  );
  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="sched-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button type="button" className="btn" data-testid="sched-retry" onClick={() => void load()}>
            Try again
          </button>
        </>
      )}
    </p>
  );
  const errorNote = error && (
    <p className="warn-note" data-testid="sched-error">
      {error}
    </p>
  );

  if (!data) {
    return (
      <div className="page-pad">
        {header}
        {offlineNote}
        {errorNote || (online && !lost && <p>Loading…</p>)}
      </div>
    );
  }
  if (data.sites.length === 0) {
    return (
      <div className="page-pad">
        {header}
        {offlineNote}
        {errorNote}
        <p className="muted-cell" data-testid="sched-none">
          Scheduled counts are not switched on at any store you work at.
        </p>
      </div>
    );
  }

  const siteOf = (id: number) => data.sites.find((s) => s.id === id);
  const shown = <T extends { site_id: number }>(rows: T[]) =>
    siteFilter && siteOf(siteFilter) ? rows.filter((r) => r.site_id === siteFilter) : rows;

  return (
    <div className="page-pad">
      {header}
      {offlineNote}
      {errorNote}
      {saved && (
        <p className="ok-note" data-testid="sched-saved">
          {saved}
        </p>
      )}

      <DueList
        title="Due today"
        testId="sched-due"
        rows={shown(data.due_today)}
        empty="No count is due today."
        siteOf={siteOf}
      />
      {data.missed.length > 0 && (
        <DueList
          title="Missed"
          testId="sched-missed"
          rows={shown(data.missed)}
          empty="Nothing missed."
          siteOf={siteOf}
        />
      )}

      <section className="card section-card" data-testid="sched-list">
        <h3 className="section-title">Schedules</h3>
        {data.schedules.length === 0 ? (
          <p className="muted-cell" data-testid="sched-empty">
            No count is scheduled yet.
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Store</th>
                  <th>What</th>
                  <th>How often</th>
                  <th>Next due</th>
                  <th>Last counted</th>
                  {data.can_edit && <th />}
                </tr>
              </thead>
              <tbody>
                {shown(data.schedules).map((row) => (
                  <ScheduleRow
                    key={row.id}
                    row={row}
                    canEdit={data.can_edit}
                    today={data.today}
                    disabled={disabled}
                    send={send}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {data.can_edit && (
        <section className="card section-card" data-testid="sched-new">
          <h3 className="section-title">Set a schedule</h3>
          <div className="form-grid">
            <label className="field">
              <span>Store</span>
              <select
                className="select"
                data-testid="sched-site"
                value={draft.site_id}
                disabled={disabled}
                onChange={(e) => setDraft({ ...draft, site_id: e.target.value })}
              >
                <option value="">Choose a store</option>
                {data.sites.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>What to count</span>
              <select
                className="select"
                data-testid="sched-brand"
                value={draft.brand_id}
                disabled={disabled}
                onChange={(e) => setDraft({ ...draft, brand_id: e.target.value })}
              >
                <option value="">Whole store</option>
                {data.brands.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>How often</span>
              <select
                className="select"
                data-testid="sched-every"
                value={draft.every}
                disabled={disabled}
                onChange={(e) => setDraft({ ...draft, every: e.target.value })}
              >
                {EVERY_CHOICES.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>First count on</span>
              <input
                type="date"
                className="input"
                data-testid="sched-first"
                min={data.today}
                value={draft.first_due_on}
                disabled={disabled}
                onChange={(e) => setDraft({ ...draft, first_due_on: e.target.value })}
              />
            </label>
            <button
              type="button"
              className="btn btn-cta"
              data-testid="sched-save"
              disabled={disabled || !draft.site_id || !draft.first_due_on}
              onClick={() => void create()}
            >
              Save schedule
            </button>
          </div>
        </section>
      )}
    </div>
  );
}

function DueList({
  title,
  testId,
  rows,
  empty,
  siteOf,
}: {
  title: string;
  testId: string;
  rows: DueCount[];
  empty: string;
  siteOf: (id: number) => { id: number; goods_v1: boolean } | undefined;
}) {
  return (
    <section className="card section-card" data-testid={testId}>
      <h3 className="section-title">{title}</h3>
      {rows.length === 0 ? (
        <p className="muted-cell">{empty}</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>What</th>
                <th>Store</th>
                <th>Due</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const site = siteOf(row.site_id);
                return (
                  <tr key={`${row.schedule_id}-${row.due_on}`} data-testid={`${testId}-row`}>
                    <td>{scopeText(row.brand_name)}</td>
                    <td>{row.site_code}</td>
                    <td>{dayText(row.due_on)}</td>
                    <td>
                      {site && (
                        <Link
                          className="btn"
                          to={countScreenPath(site)}
                          data-testid={`${testId}-count`}
                        >
                          Count now
                        </Link>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ScheduleRow({
  row,
  canEdit,
  today,
  disabled,
  send,
}: {
  row: Row;
  canEdit: boolean;
  today: string;
  disabled: boolean;
  send: (path: string, body: object, done: string) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  const [every, setEvery] = useState(row.every);
  const [first, setFirst] = useState(row.next_due_on);
  const what = scopeText(row.brand_name);

  async function change() {
    const ok = await send(
      `${URL}/${row.id}`,
      { ...goodsMeta(row.revision), every, first_due_on: first },
      `Schedule changed: ${what} at ${row.site_code}.`,
    );
    if (ok) setEditing(false);
  }

  return (
    <tr data-testid={`sched-row-${row.site_code}-${what}`}>
      <td>{row.site_code}</td>
      <td>{what}</td>
      <td>
        {editing ? (
          <select
            className="select"
            data-testid="sched-edit-every"
            value={every}
            disabled={disabled}
            onChange={(e) => setEvery(e.target.value as Row["every"])}
          >
            {EVERY_CHOICES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        ) : (
          everyText(row.every)
        )}
      </td>
      <td>
        {editing ? (
          <input
            type="date"
            className="input"
            data-testid="sched-edit-first"
            min={today}
            value={first}
            disabled={disabled}
            onChange={(e) => setFirst(e.target.value)}
          />
        ) : (
          <span data-testid="sched-next">{dayText(row.next_due_on)}</span>
        )}
      </td>
      <td>{row.last_counted_on ? dayText(row.last_counted_on) : "Not yet"}</td>
      {canEdit && (
        <td>
          {editing ? (
            <>
              <button
                type="button"
                className="btn btn-cta"
                data-testid="sched-edit-save"
                disabled={disabled || !first}
                onClick={() => void change()}
              >
                Save
              </button>{" "}
              <button type="button" className="btn" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn"
                data-testid="sched-edit"
                disabled={disabled}
                onClick={() => setEditing(true)}
              >
                Change
              </button>{" "}
              <button
                type="button"
                className="btn"
                data-testid="sched-stop"
                disabled={disabled}
                onClick={() =>
                  void send(
                    `${URL}/${row.id}/stop`,
                    goodsMeta(row.revision),
                    `Schedule stopped: ${what} at ${row.site_code}.`,
                  )
                }
              >
                Stop
              </button>
            </>
          )}
        </td>
      )}
    </tr>
  );
}
