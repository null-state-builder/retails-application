// Today's checklist (store operations ticket 49, ST-OPS-4). The store's lists
// due today, each item ticked or not, and - below them - the lists of the last
// few days whose time passed with items still open. Staff tick an item, with a
// photo if they want one. The server decides what is due, what was missed and
// who may tick; this only shows it and sends the tick.
//
// Everything here is live data, so it needs a connection: offline the page says
// so and ticks nothing. A tick whose answer was lost keeps its own id, so the
// retry the person makes saves one tick, not two.

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Camera, Check, ClipboardCheck } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, apiUrl } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { dayText } from "../lib/stockAgeing";
import { dueByText, opensPath, progressText, tickKey } from "../lib/storeChecklists";
import { newUuid } from "../till/uuid";
import "./Shared.css";
import "./StoreChecklist.css";

type Payload = ApiRead<ApiSchemas["ChecklistToday"]>;
type List = Payload["lists"][number];
type Item = List["items"][number];

const URL = "/store/checklists";
const OFFLINE = "The checklist needs a connection. Nothing was ticked; reconnect and tick again.";

function useChecklist(store: string) {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const request = useRef(0);

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const response = await api.get<Payload>(URL, { params: store ? { store } : {} });
      if (mine !== request.current) return;
      setLost(false);
      setError("");
      setData(response.data);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, [store]);

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

  return { data, error, setError, online, setOnline, lost, setLost, load };
}

/** The lists themselves, as Today's card and the full page both draw them. */
export function StoreChecklist({
  store = "",
  compact = false,
}: {
  store?: string;
  compact?: boolean;
}) {
  const { data, error, setError, online, setOnline, lost, setLost, load } = useChecklist(store);
  const [busy, setBusy] = useState("");
  const [saved, setSaved] = useState("");
  // One id per tick on its way, kept until the server has it.
  const pending = useRef(new Map<string, string>());

  async function tick(list: List, item: Item, photo: File | null) {
    if (!data) return;
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const key = tickKey(list.template_id, item.id, list.due_on);
    const clientId = pending.current.get(key) ?? newUuid();
    pending.current.set(key, clientId);
    const form = new FormData();
    form.append("client_id", clientId);
    form.append("store", data.store);
    form.append("template_id", list.template_id);
    form.append("item_id", item.id);
    form.append("due_on", list.due_on);
    if (photo) form.append("photo", photo);
    setBusy(key);
    setError("");
    setSaved("");
    try {
      await api.post(`${URL}/ticks`, form);
      pending.current.delete(key);
      setLost(false);
      setSaved(`Ticked: ${item.text}${photo ? ", with a photo" : ""}.`);
      await load();
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else {
        pending.current.delete(key);
        setError(apiErrorMessage(reason));
        await load();
      }
    } finally {
      setBusy("");
    }
  }

  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="checklist-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="checklist-retry"
            onClick={() => void load()}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const errorNote = error && (
    <p className="warn-note" data-testid="checklist-error">
      {error}
    </p>
  );

  if (!data) {
    return (
      <>
        {offlineNote}
        {errorNote || (online && !lost && <p className="lead">Loading…</p>)}
      </>
    );
  }
  if (!data.on) {
    return compact ? null : (
      <p className="muted-cell" data-testid="checklist-off">
        Task checklists are not switched on at {data.store}.
      </p>
    );
  }

  const disabled = !online || busy !== "" || !data.can_tick;
  const body = (
    <>
      {offlineNote}
      {errorNote}
      {saved && (
        <p className="ok-note" data-testid="checklist-saved">
          {saved}
        </p>
      )}
      {data.lists.length === 0 ? (
        <p className="muted-cell" data-testid="checklist-none">
          No checklist is due today.
        </p>
      ) : (
        data.lists.map((list) => (
          <ChecklistBlock
            key={`${list.template_id}-${list.due_on}`}
            list={list}
            disabled={disabled}
            busy={busy}
            onTick={tick}
          />
        ))
      )}
      {data.missed.length > 0 && (
        <div className="cl-missed" data-testid="checklist-missed">
          <p className="cl-missed-head">Missed in the last {data.missed_days} days, still to do</p>
          {data.missed.map((list) => (
            <ChecklistBlock
              key={`${list.template_id}-${list.due_on}`}
              list={list}
              disabled={disabled}
              busy={busy}
              onTick={tick}
              missed
            />
          ))}
        </div>
      )}
      {!data.can_tick && (
        <p className="muted-cell" data-testid="checklist-read-only">
          Only the store's own staff tick this list.
        </p>
      )}
    </>
  );
  return body;
}

function ChecklistBlock({
  list,
  disabled,
  busy,
  onTick,
  missed = false,
}: {
  list: List;
  disabled: boolean;
  busy: string;
  onTick: (list: List, item: Item, photo: File | null) => Promise<void>;
  missed?: boolean;
}) {
  return (
    <section className="cl-list" data-testid={`checklist-list-${list.name}`} data-due={list.due_on}>
      <div className="cl-list-head">
        <b>{list.name}</b>
        <span className="cl-when">
          {missed
            ? `Was due ${dayText(list.due_on)}, ${dueByText(list.due_by)}`
            : `Due ${dueByText(list.due_by)}`}
          {!missed && <> · {progressText(list.items)}</>}
        </span>
      </div>
      <ul className="cl-items">
        {list.items.map((item) => (
          <ChecklistItem
            key={item.id}
            list={list}
            item={item}
            disabled={disabled}
            sending={busy === tickKey(list.template_id, item.id, list.due_on)}
            onTick={onTick}
            missed={missed || item.missed}
          />
        ))}
      </ul>
    </section>
  );
}

function ChecklistItem({
  list,
  item,
  disabled,
  sending,
  onTick,
  missed,
}: {
  list: List;
  item: Item;
  disabled: boolean;
  sending: boolean;
  onTick: (list: List, item: Item, photo: File | null) => Promise<void>;
  missed: boolean;
}) {
  const [fileKey, setFileKey] = useState(0);
  const to = opensPath(item.opens);
  const done = item.tick;
  return (
    <li
      className={`cl-item ${done ? "cl-done" : ""} ${missed && !done ? "cl-late" : ""}`}
      data-testid="checklist-item"
      data-item={item.text}
      data-state={done ? "ticked" : missed ? "missed" : "open"}
    >
      <span className="cl-mark" aria-hidden>
        {done ? <Check size={16} /> : null}
      </span>
      <span className="cl-text">
        {item.text}
        {to && (
          <>
            {" "}
            <Link to={to} className="cl-open" data-testid="checklist-open">
              Open
            </Link>
          </>
        )}
        {done ? (
          <span className="cl-meta" data-testid="checklist-ticked">
            {done.by} ·{" "}
            {new Date(done.at).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}
            {done.late && " · late"}
            {done.has_photo && (
              <>
                {" · "}
                <a
                  href={apiUrl(`/store/checklists/ticks/${done.id}/photo`)}
                  target="_blank"
                  rel="noreferrer"
                >
                  Photo
                </a>
              </>
            )}
          </span>
        ) : (
          missed && (
            <span className="cl-meta cl-meta-late" data-testid="checklist-item-missed">
              Missed
            </span>
          )
        )}
      </span>
      {!done && (
        <span className="cl-actions">
          <button
            type="button"
            className="btn btn-sm btn-cta"
            data-testid="checklist-tick"
            disabled={disabled}
            onClick={() => void onTick(list, item, null)}
          >
            {sending ? "Ticking…" : "Tick"}
          </button>
          <label
            className={`btn btn-sm cl-photo ${disabled ? "cl-photo-off" : ""}`}
            title="Tick with a photo"
          >
            <Camera size={14} /> Photo
            <input
              key={fileKey}
              type="file"
              accept="image/jpeg,image/png"
              capture="environment"
              data-testid="checklist-photo"
              disabled={disabled}
              onChange={(e) => {
                const file = e.target.files?.[0] ?? null;
                setFileKey((k) => k + 1);
                if (file) void onTick(list, item, file);
              }}
            />
          </label>
        </span>
      )}
    </li>
  );
}

/** Today's card on the store's Dashboard. Drawn only where the switch is on. */
export function StoreChecklistCard({ store }: { store: string }) {
  return (
    <div className="card panel dash-block" data-testid="checklist-card">
      <div className="panel-head">
        <p className="eyebrow">Today's checklist</p>
        <h3 className="h3">
          <ClipboardCheck size={16} /> Tasks for today
        </h3>
      </div>
      <StoreChecklist store={store} compact />
    </div>
  );
}

/** Home > Checklist: the same lists, full width, for the store in `?store=`
 *  (the checklist-missed alert names it) or the person's own. */
export function StoreChecklistPage() {
  const [params] = useSearchParams();
  const store = (params.get("store") ?? "").trim();
  return (
    <div className="page-pad">
      <PageHeader
        title="Today's checklist"
        lead="The store's lists due today, and anything from the last few days whose time passed before it was ticked. Tick each item when it is done, with a photo if you want one."
      />
      <div className="card section-card" data-testid="checklist-page">
        <StoreChecklist store={store} />
      </div>
    </div>
  );
}
