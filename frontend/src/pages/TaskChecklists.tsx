import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { EVERY_CHOICES, WEEKDAYS, everyText } from "../lib/storeChecklists";
import { newUuid } from "../till/uuid";
import "./Shared.css";
import "./StoreChecklist.css";

type Payload = ApiRead<ApiSchemas["ChecklistTemplatesPage"]>;
type Template = Payload["templates"][number];

const URL = "/store/checklist-templates";
const OFFLINE = "Task checklists need a connection. What you typed is kept; reconnect to save.";

interface DraftItem {
  id: string;
  text: string;
  opens: string;
}

interface Draft {
  name: string;
  every: string;
  weekday: string;
  day_of_month: string;
  due_by: string;
  items: DraftItem[];
}

const EMPTY: Draft = {
  name: "",
  every: "day",
  weekday: "0",
  day_of_month: "1",
  due_by: "",
  items: [{ id: "", text: "", opens: "" }],
};

function draftOf(t: Template): Draft {
  return {
    name: t.name,
    every: t.every,
    weekday: String(t.weekday ?? 0),
    day_of_month: String(t.day_of_month ?? 1),
    due_by: t.due_by ?? "",
    items: t.items.map((i) => ({ id: i.id ?? "", text: i.text, opens: i.opens ?? "" })),
  };
}

function bodyOf(draft: Draft) {
  return {
    name: draft.name.trim(),
    every: draft.every,
    weekday: draft.every === "week" ? Number(draft.weekday) : null,
    day_of_month: draft.every === "month" ? Number(draft.day_of_month) : null,
    due_by: draft.due_by || null,
    items: draft.items
      .filter((i) => i.text.trim())
      .map((i) => ({ ...(i.id ? { id: i.id } : {}), text: i.text.trim(), opens: i.opens })),
  };
}

/** Setup > Task Checklists (store operations ticket 49, ST-OPS-4).
 *
 *  Admin sets each checklist every store gets where the switch is on: its
 *  items, how often it is due and by what time. The site-readiness checklist
 *  (Setup > Organisation) is a different thing and is not here. Everyone with
 *  Setup reads this page; only Admin changes it, which the server enforces.
 *  Offline the page says it needs a connection and keeps what was typed. */
export function TaskChecklistsPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  // One command id per save, kept until the server has it, so a retry after a
  // dropped connection is the same save. Keyed by what is saved; changing the
  // draft makes it a new save.
  const pending = useRef(new Map<string, string>());
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

  function edit(next: Draft) {
    pending.current.delete(URL);
    if (editing) pending.current.delete(`${URL}/${editing}`);
    setDraft(next);
  }

  async function send(path: string, body: object, done: string): Promise<boolean> {
    if (!navigator.onLine) {
      setOnline(false);
      return false;
    }
    setError("");
    setSaved("");
    setBusy(true);
    let ok = false;
    const commandId = pending.current.get(path) ?? newUuid();
    pending.current.set(path, commandId);
    try {
      await api.post(path, { command_id: commandId, ...body });
      pending.current.delete(path);
      setSaved(done);
      ok = true;
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else {
        pending.current.delete(path);
        setError(apiErrorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
    if (ok) await load();
    return ok;
  }

  async function save() {
    const body = bodyOf(draft);
    const current = data?.templates.find((t) => t.id === editing);
    const ok = current
      ? await send(
          `${URL}/${current.id}`,
          { ...body, expected_revision: current.revision },
          `Checklist changed: ${body.name}.`,
        )
      : await send(URL, body, `Checklist set: ${body.name}.`);
    if (ok) {
      setEditing(null);
      setDraft(EMPTY);
    }
  }

  const disabled = !online || busy;
  const header = (
    <PageHeader
      title="Task Checklists"
      lead="The lists each store gets on Today where task checklists are switched on: opening, closing, a weekly display check, the monthly count. Staff tick each item, with a photo if they want one; an item not ticked by its time is missed. The site-readiness checklist is separate."
    />
  );
  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="tcl-offline" role="status">
      {OFFLINE}
    </p>
  );
  const errorNote = error && (
    <p className="warn-note" data-testid="tcl-error">
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

  const canSave =
    !disabled && draft.name.trim() !== "" && draft.items.some((i) => i.text.trim() !== "");

  return (
    <div className="page-pad">
      {header}
      {offlineNote}
      {errorNote}
      {saved && (
        <p className="ok-note" data-testid="tcl-saved">
          {saved}
        </p>
      )}
      {data.stores_on.length === 0 && (
        <p className="muted-cell" data-testid="tcl-off">
          Task checklists are not switched on at any store you work at.
        </p>
      )}

      <section className="card section-card" data-testid="tcl-list">
        <h3 className="section-title">Checklists</h3>
        {data.templates.length === 0 ? (
          <p className="muted-cell" data-testid="tcl-empty">
            No checklist is set yet.
          </p>
        ) : (
          data.templates.map((t) => (
            <div className="cl-list" key={t.id} data-testid={`tcl-row-${t.name}`}>
              <div className="cl-list-head">
                <b>{t.name}</b>
                <span className="cl-when">{everyText(t)}</span>
              </div>
              <ul className="cl-items">
                {t.items.map((item) => (
                  <li className="cl-item" key={item.id}>
                    <span className="cl-text">
                      {item.text}
                      {item.opens && (
                        <span className="cl-meta">
                          Opens: {data.opens.find((o) => o.key === item.opens)?.name ?? item.opens}
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
              {data.can_edit && (
                <p className="cl-actions" style={{ marginTop: 8 }}>
                  <button
                    type="button"
                    className="btn btn-sm"
                    data-testid="tcl-edit"
                    disabled={disabled}
                    onClick={() => {
                      pending.current.delete(`${URL}/${t.id}`);
                      setEditing(t.id);
                      setDraft(draftOf(t));
                    }}
                  >
                    Change
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    data-testid="tcl-stop"
                    disabled={disabled}
                    onClick={() =>
                      void send(
                        `${URL}/${t.id}/stop`,
                        { expected_revision: t.revision },
                        `Checklist stopped: ${t.name}.`,
                      )
                    }
                  >
                    Stop
                  </button>
                </p>
              )}
            </div>
          ))
        )}
      </section>

      {data.can_edit && (
        <section className="card section-card" data-testid="tcl-form">
          <h3 className="section-title">{editing ? "Change the checklist" : "Set a checklist"}</h3>
          <div className="form-grid">
            <label className="field">
              <span>Name</span>
              <input
                className="input"
                data-testid="tcl-name"
                maxLength={80}
                value={draft.name}
                disabled={disabled}
                onChange={(e) => edit({ ...draft, name: e.target.value })}
              />
            </label>
            <label className="field">
              <span>How often</span>
              <select
                className="select"
                data-testid="tcl-every"
                value={draft.every}
                disabled={disabled}
                onChange={(e) => edit({ ...draft, every: e.target.value })}
              >
                {EVERY_CHOICES.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
            {draft.every === "week" && (
              <label className="field">
                <span>On</span>
                <select
                  className="select"
                  data-testid="tcl-weekday"
                  value={draft.weekday}
                  disabled={disabled}
                  onChange={(e) => edit({ ...draft, weekday: e.target.value })}
                >
                  {WEEKDAYS.map((day, index) => (
                    <option key={day} value={index}>
                      {day}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {draft.every === "month" && (
              <label className="field">
                <span>Day of the month</span>
                <input
                  type="number"
                  className="input"
                  data-testid="tcl-day"
                  min={1}
                  max={31}
                  value={draft.day_of_month}
                  disabled={disabled}
                  onChange={(e) => edit({ ...draft, day_of_month: e.target.value })}
                />
              </label>
            )}
            <label className="field">
              <span>Due by (empty: the end of the day)</span>
              <input
                type="time"
                className="input"
                data-testid="tcl-due-by"
                value={draft.due_by}
                disabled={disabled}
                onChange={(e) => edit({ ...draft, due_by: e.target.value })}
              />
            </label>
          </div>

          <p className="cl-missed-head" style={{ color: "var(--caption)", marginTop: 14 }}>
            Items
          </p>
          <ul className="cl-items" data-testid="tcl-items">
            {draft.items.map((item, index) => (
              <li className="cl-item" key={index}>
                <input
                  className="input cl-text"
                  data-testid="tcl-item-text"
                  maxLength={200}
                  placeholder="What to do"
                  value={item.text}
                  disabled={disabled}
                  onChange={(e) => {
                    const items = [...draft.items];
                    items[index] = { ...item, text: e.target.value };
                    edit({ ...draft, items });
                  }}
                />
                <select
                  className="select"
                  data-testid="tcl-item-opens"
                  value={item.opens}
                  disabled={disabled}
                  onChange={(e) => {
                    const items = [...draft.items];
                    items[index] = { ...item, opens: e.target.value };
                    edit({ ...draft, items });
                  }}
                >
                  {data.opens.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.key ? `Opens: ${o.name}` : "Opens nothing"}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn-sm"
                  data-testid="tcl-item-remove"
                  disabled={disabled || draft.items.length === 1}
                  onClick={() =>
                    edit({ ...draft, items: draft.items.filter((_, i) => i !== index) })
                  }
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <p className="cl-actions" style={{ marginTop: 10, justifyContent: "flex-start" }}>
            <button
              type="button"
              className="btn btn-sm"
              data-testid="tcl-item-add"
              disabled={disabled || draft.items.length >= 30}
              onClick={() =>
                edit({ ...draft, items: [...draft.items, { id: "", text: "", opens: "" }] })
              }
            >
              Add an item
            </button>
            <button
              type="button"
              className="btn btn-cta"
              data-testid="tcl-save"
              disabled={!canSave}
              onClick={() => void save()}
            >
              {editing ? "Save changes" : "Save checklist"}
            </button>
            {editing && (
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setEditing(null);
                  setDraft(EMPTY);
                }}
              >
                Cancel
              </button>
            )}
          </p>
        </section>
      )}
    </div>
  );
}
