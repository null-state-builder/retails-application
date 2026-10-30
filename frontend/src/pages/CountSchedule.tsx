import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Note } from "../components/Note";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { Box, Section } from "../components/Section";
import { EmptyState } from "../components/States";
import { Status } from "../components/Status";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { useCachedGet } from "../lib/cachedQuery";
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
  // Read through the shared cache: a revisit shows the last schedule at once
  // and refreshes it behind it (and again on reconnect).
  const read = useCachedGet<Payload>(URL);
  const data = read.data ?? null;
  const [writeError, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // When a write last got no answer at all; cleared by the next good read.
  const [lostAt, setLostAt] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Draft>({
    site_id: "",
    brand_id: "",
    every: "month",
    first_due_on: "",
  });
  const readLost = read.error != null && isConnectionLost(read.error);
  const lost = readLost || (lostAt !== null && (read.updatedAt ?? 0) < lostAt);
  const error = writeError || (read.error != null && !readLost ? apiErrorMessage(read.error) : "");
  const setLost = (value: boolean) => setLostAt(value ? Date.now() : null);
  const { reload } = read;
  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    reload();
  }, [reload]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      load();
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
    if (ok) load();
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
    <Note
      tone="warn"
      testId="sched-offline"
      role="status"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="sched-retry"
            onClick={() => void load()}
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
    <Note tone="error" testId="sched-error">
      {error}
    </Note>
  );

  if (!data) {
    return (
      <OperationsPage className="page">
        {header}
        {offlineNote}
        {errorNote || (online && !lost && <p className="lead">Loading…</p>)}
      </OperationsPage>
    );
  }
  if (data.sites.length === 0) {
    return (
      <OperationsPage className="page">
        {header}
        {offlineNote}
        {errorNote}
        <EmptyState testId="sched-none">
          Scheduled counts are not switched on at any store you work at.
        </EmptyState>
      </OperationsPage>
    );
  }

  const siteOf = (id: number) => data.sites.find((s) => s.id === id);
  const shown = <T extends { site_id: number }>(rows: T[]) =>
    siteFilter && siteOf(siteFilter) ? rows.filter((r) => r.site_id === siteFilter) : rows;

  // Where each schedule stands is the server's answer (its due-today and missed
  // lists), never worked out here from the dates.
  const missedIds = new Set(data.missed.map((r) => r.schedule_id));
  const dueIds = new Set(data.due_today.map((r) => r.schedule_id));
  const standing = (id: string): Standing =>
    missedIds.has(id) ? "missed" : dueIds.has(id) ? "due" : "on-time";

  return (
    <OperationsPage className="page">
      {header}
      {offlineNote}
      {errorNote}
      {saved && (
        <Note tone="success" role="status" testId="sched-saved">
          {saved}
        </Note>
      )}

      <DueList
        title="Due today"
        testId="sched-due"
        rows={shown(data.due_today)}
        empty="No count is due today."
        siteOf={siteOf}
        lead
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

      <Section heading="Schedules" ruled testId="sched-list">
        {data.schedules.length === 0 ? (
          <p className="lead" data-testid="sched-empty">
            No count is scheduled yet.
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>What to count</th>
                  <th>Store</th>
                  <th>How often</th>
                  <th className="p2">First count on</th>
                  <th>Next due</th>
                  <th>Last counted</th>
                  <th>State</th>
                  {data.can_edit && (
                    <th className="act">
                      <span className="sr-only">Actions</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {shown(data.schedules).map((row) => (
                  <ScheduleRow
                    key={row.id}
                    row={row}
                    standing={standing(row.id)}
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
      </Section>

      {data.can_edit && (
        <Section heading="Set a schedule" ruled testId="sched-new">
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
              className="btn btn-primary"
              data-testid="sched-save"
              disabled={disabled || !draft.site_id || !draft.first_due_on}
              onClick={() => void create()}
            >
              Save schedule
            </button>
          </div>
        </Section>
      )}
    </OperationsPage>
  );
}

/** The counts due today, or missed, as one tile each: what to count, where,
 *  and the way into that store's blind count. */
function DueList({
  title,
  testId,
  rows,
  empty,
  siteOf,
  lead = false,
}: {
  title: string;
  testId: string;
  rows: DueCount[];
  empty: string;
  siteOf: (id: number) => { id: number; goods_v1: boolean } | undefined;
  /** The list the page leads with: its way in is the page's primary action. */
  lead?: boolean;
}) {
  return (
    <Section heading={title} testId={testId}>
      {rows.length === 0 ? (
        <p className="lead">{empty}</p>
      ) : (
        <div className="tiles">
          {rows.map((row) => {
            const site = siteOf(row.site_id);
            return (
              <Box key={`${row.schedule_id}-${row.due_on}`} data-testid={`${testId}-row`}>
                <p className="lead">
                  {lead ? "Due" : "Was due"} {dayText(row.due_on)} · {row.site_code}
                </p>
                <h3 className="heading">{scopeText(row.brand_name)}</h3>
                {site && (
                  <Link
                    className={lead ? "btn btn-primary" : "btn"}
                    to={countScreenPath(site)}
                    data-testid={`${testId}-count`}
                  >
                    Count now
                  </Link>
                )}
              </Box>
            );
          })}
        </div>
      )}
    </Section>
  );
}

type Standing = "missed" | "due" | "on-time";

function ScheduleRow({
  row,
  standing,
  canEdit,
  today,
  disabled,
  send,
}: {
  row: Row;
  standing: Standing;
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
      <td>
        <b>{what}</b>
      </td>
      <td>{row.site_code}</td>
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
      <td className="p2">{dayText(row.first_due_on)}</td>
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
      <td>
        {standing === "missed" ? (
          <Status tone="red">Missed</Status>
        ) : standing === "due" ? (
          <Status tone="amber">Due today</Status>
        ) : (
          <Status tone="green">On time</Status>
        )}
      </td>
      {canEdit && (
        <td className="act">
          {editing ? (
            <>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                data-testid="sched-edit-save"
                disabled={disabled || !first}
                onClick={() => void change()}
              >
                Save
              </button>{" "}
              <button type="button" className="btn btn-sm" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-sm"
                data-testid="sched-edit"
                disabled={disabled}
                onClick={() => setEditing(true)}
              >
                Change
              </button>{" "}
              <button
                type="button"
                className="btn btn-sm btn-danger"
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
