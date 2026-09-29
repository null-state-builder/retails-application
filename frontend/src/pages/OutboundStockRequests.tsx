import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  PackageSearch,
  Plus,
  Search,
  Trash2,
  Truck,
  XCircle,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useDoc, useList } from "../lib/hooks";
import { Money, formatDateTime } from "../lib/format";
import { canWriteTransfer } from "../lib/outbound-rbac";
import { destinationOptions, type LocationT } from "../lib/transfer-locations";
import { ApprovalTrail, type ApprovalT } from "../components/approval";
import { ListSearchBar } from "../components/SearchBox";
import { PageHeader } from "../components/PageHeader";
import "./Booking.css";

// ---------------------------------------------------------------------------
// Helpers & types
// ---------------------------------------------------------------------------

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

// The six honest statuses a store sees on its ask (#74) — derived server-side,
// never typed here. The label comes from the server (`status_display`); the
// tone is the only thing this screen decides for itself.
const STATUS_TONE: Record<string, string> = {
  pending_approval: "amber",
  approved: "blue",
  declined: "red",
  being_fulfilled: "blue",
  partly_fulfilled: "amber",
  closed: "green",
};

function StatusPill({ status, label }: { status: string; label: string }) {
  return <span className={`chip chip-${STATUS_TONE[status] ?? "grey"} status-pill`}>{label}</span>;
}

interface StockRequestLineT {
  id: number;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  season: string;
  item: string;
  hsn: string;
  qty: number;
  qty_fulfilled: number;
  qty_committed: number;
}

interface FulfillingTransferT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  source_store_code: string;
  destination_store_code: string;
  dispatch_date: string | null;
  created_at: string;
}

interface StockRequestT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  requesting_store: number;
  requesting_store_code: string;
  requesting_store_name: string;
  fulfilling_store: number;
  fulfilling_store_code: string;
  fulfilling_store_name: string;
  notes: string;
  source: string;
  expected_arrival_at: string | null;
  status: string;
  status_display: string;
  decline_reason: string;
  created_by: number | null;
  created_by_name: string;
  approval: ApprovalT | null;
  approval_history: ApprovalT[];
  created_at: string;
  updated_at: string;
  lines: StockRequestLineT[];
  fulfilling_transfers: FulfillingTransferT[];
}

interface StoreT { id: number; code: string; name: string; store_type: string; }

interface StockSearchRowT {
  store_code: string;
  store_name: string;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  season: string;
  item: string;
  hsn: string;
  qty: number;
  is_own: boolean;
  unit_cost_paise?: number;
  landed_value_paise?: number;
  margin_paise?: number | null;
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export function StockRequestListPage() {
  const { user } = useAuth();
  const [q, setQ] = useState("");
  const { data, loading } = useList<StockRequestT>("/outbound/stock-requests", { q });
  const writable = canWriteTransfer(user);

  return (
    <div className="page-pad">
      <PageHeader
        actions={
          writable && (
            <Link className="btn btn-cta" to="/transfer/requests/new" data-testid="new-stock-request-btn">
              <Plus size={16} /> New request
            </Link>
          )
        }
      />

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search requests — doc number, store"
        label="Search stock requests"
        testId="stock-request-search"
        noun="request"
        count={data.length}
        loading={loading}
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : data.length === 0 ? (
        <div className="card section-card" data-testid="stock-request-empty">
          {q
            ? `No request matches “${q}”.`
            : "No stock requests yet. Search another location's stock and ask for it."}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="stock-request-table">
            <thead>
              <tr>
                <th>Doc #</th>
                <th>Requesting</th>
                <th>Fulfilling</th>
                <th className="num">Lines</th>
                <th className="num">Total qty</th>
                <th>Status</th>
                <th>Expected</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => {
                const totalQty = r.lines.reduce((s, l) => s + l.qty, 0);
                return (
                  <tr key={r.id} data-testid={`sr-row-${r.id}`}>
                    <td>
                      <Link to={`/transfer/requests/${r.id}`} className="link-cell mono" data-testid={`sr-link-${r.id}`}>
                        <b>{r.doc_number || `Draft #${r.id}`}</b>
                      </Link>
                    </td>
                    <td><b className="mono">{r.requesting_store_code}</b></td>
                    <td><b className="mono">{r.fulfilling_store_code}</b></td>
                    <td className="num">{r.lines.length}</td>
                    <td className="num">{totalQty}</td>
                    <td><StatusPill status={r.status} label={r.status_display} /></td>
                    <td data-testid={`sr-expected-${r.id}`}>
                      {r.expected_arrival_at ? formatDateTime(r.expected_arrival_at) : "—"}
                    </td>
                    <td>{fmtDate(r.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cross-location stock search (#74) — the way a request's lines get built
// ---------------------------------------------------------------------------

/** Searches stock across every location. Identity and quantity show for every
 *  row; cost, landed value and margin show only where `is_own` says so — the
 *  server already withheld them for anywhere else (Anand's ruling of 26 July). */
function CrossLocationSearch({ onAdd }: { onAdd: (row: StockSearchRowT) => void }) {
  const [term, setTerm] = useState("");
  const [rows, setRows] = useState<StockSearchRowT[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const q = term.trim();
    if (!q) { setRows([]); return; }
    let live = true;
    setLoading(true);
    api
      .get("/outbound/stock-search", { params: { q } })
      .then((r) => live && setRows(r.data.rows))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [term]);

  const data = rows;

  return (
    <div className="card section-card" data-testid="cross-location-search">
      <p className="eyebrow">Search stock across locations</p>
      <div className="form-row" style={{ marginTop: 10 }}>
        <div className="field" style={{ flex: 1 }}>
          <label>SKU, design or brand</label>
          <input
            className="input"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search everywhere this store cannot see cost…"
            data-testid="stock-search-term"
          />
        </div>
      </div>
      {term.trim() && (
        loading ? (
          <p className="lead">Searching…</p>
        ) : data.length === 0 ? (
          <p className="lead">No stock matches “{term}” anywhere.</p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="stock-search-results">
              <thead>
                <tr>
                  <th>Store</th>
                  <th>SKU</th>
                  <th>Design</th>
                  <th>Size</th>
                  <th>Colour</th>
                  <th className="num">Qty</th>
                  <th className="num">Cost</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((row, i) => (
                  <tr key={`${row.store_code}-${row.sku_code}-${i}`} data-testid={`stock-search-row-${i}`}>
                    <td><b className="mono">{row.store_code}</b></td>
                    <td className="mono">{row.sku_code}</td>
                    <td>{row.design || "—"}</td>
                    <td>{row.size || "—"}</td>
                    <td>{row.color || "—"}</td>
                    <td className="num">{row.qty}</td>
                    <td className="num">
                      {row.is_own
                        ? (row.unit_cost_paise ? <Money paise={row.unit_cost_paise} /> : "—")
                        : <span className="lead" style={{ color: "var(--muted)" }}>Not your store</span>}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => onAdd(row)}
                        data-testid={`stock-search-add-${i}`}
                      >
                        <Plus size={14} /> Add
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create
// ---------------------------------------------------------------------------

interface DraftLine {
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  season: string;
  item: string;
  hsn: string;
  qty: number | string;
}

export function StockRequestNewPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: stores } = useList<StoreT>("/masters/stores");
  const { data: locations } = useList<LocationT>("/masters/locations");

  const storeLocked = user?.scope_type === "store" && (user?.stores?.length ?? 0) >= 1;
  const lockedStore = storeLocked ? user!.stores[0] : null;

  const [requestingId, setRequestingId] = useState<string>(lockedStore ? String(lockedStore.id) : "");
  const [fulfillingId, setFulfillingId] = useState("");
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const fulfillingOptions = destinationOptions(locations, requestingId);

  function addFromSearch(row: StockSearchRowT) {
    setLines((ls) => [
      ...ls,
      {
        sku_code: row.sku_code,
        design: row.design,
        color: row.color,
        size: row.size,
        brand: row.brand,
        season: row.season,
        item: row.item,
        hsn: row.hsn,
        qty: 1,
      },
    ]);
  }

  function setLineQty(i: number, qty: string) {
    setLines((ls) => ls.map((l, idx) => (idx === i ? { ...l, qty } : l)));
  }

  async function save() {
    setError("");
    if (!requestingId) { setError("Select the store asking for stock."); return; }
    if (!fulfillingId) { setError("Select who this is asked of."); return; }
    const payloadLines = lines
      .filter((l) => l.sku_code && Number(l.qty) > 0)
      .map((l) => ({
        sku_code: l.sku_code,
        design: l.design,
        color: l.color,
        size: l.size,
        brand: l.brand,
        season: l.season,
        item: l.item,
        hsn: l.hsn,
        qty: Number(l.qty),
      }));
    if (!payloadLines.length) { setError("Add at least one line from the search above."); return; }
    setSaving(true);
    try {
      const { data } = await api.post("/outbound/stock-requests", {
        requesting_store: Number(requestingId),
        fulfilling_store: Number(fulfillingId),
        notes,
        lines: payloadLines,
      });
      navigate(`/transfer/requests/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/transfer/requests" className="btn" style={{ marginBottom: 16 }} data-testid="sr-back-link">
        <ArrowLeft size={15} /> Stock requests
      </Link>
      <h1 className="h1 h2-rust" style={{ marginBottom: 18 }}>New stock request</h1>

      <div className="card section-card">
        <p className="eyebrow">Step 1 · Who is asking, and of whom</p>
        <div className="form-row" style={{ marginTop: 10 }}>
          <div className="field">
            <label>Requesting store</label>
            {storeLocked && lockedStore ? (
              <div className="store-lock" data-testid="sr-requesting-locked">{lockedStore.code} · {lockedStore.name}</div>
            ) : (
              <select className="select" value={requestingId} onChange={(e) => setRequestingId(e.target.value)} data-testid="sr-requesting-select">
                <option value="">Select store…</option>
                {stores.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
              </select>
            )}
          </div>
          <div className="field">
            <label>Fulfilling store / warehouse</label>
            <select className="select" value={fulfillingId} onChange={(e) => setFulfillingId(e.target.value)} data-testid="sr-fulfilling-select">
              <option value="">Select warehouse or store…</option>
              {fulfillingOptions.map((l) => (
                <option key={l.id} value={l.id}>{l.code} · {l.name} {l.store_type === "warehouse" ? "(WH)" : ""}</option>
              ))}
            </select>
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Notes</label>
            <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Why this store needs it" data-testid="sr-notes" />
          </div>
        </div>
      </div>

      <CrossLocationSearch onAdd={addFromSearch} />

      <div className="card section-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div>
            <p className="eyebrow">Step 2 · What this request asks for</p>
            <h3 className="h3">Requested lines</h3>
          </div>
        </div>
        {lines.length === 0 ? (
          <p className="lead">Nothing added yet — search above and add lines to the ask.</p>
        ) : (
          <table className="lines-table" data-testid="sr-lines">
            <thead>
              <tr>
                <th>SKU</th>
                <th>Design</th>
                <th>Size</th>
                <th>Colour</th>
                <th className="num">Qty</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {lines.map((l, i) => (
                <tr key={i}>
                  <td className="mono">{l.sku_code}</td>
                  <td>{l.design || "—"}</td>
                  <td>{l.size || "—"}</td>
                  <td>{l.color || "—"}</td>
                  <td><input className="num" value={l.qty} onChange={(e) => setLineQty(i, e.target.value)} data-testid={`sr-qty-${i}`} /></td>
                  <td><button type="button" className="line-del" onClick={() => setLines((ls) => ls.filter((_, idx) => idx !== i))} data-testid={`delete-sr-line-${i}`}><Trash2 size={15} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="sr-create-error">{error}</div>}
      <button className="btn btn-primary btn-lg" disabled={saving} onClick={save} data-testid="save-stock-request-btn">
        <PackageSearch size={16} /> {saving ? "Saving…" : "Raise request"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

function FulfilPanel({ r, onDone }: { r: StockRequestT; onDone: () => void }) {
  // qty_committed, not qty_fulfilled: an earlier pass's transfer may still be
  // an unreceived draft, and its promise must count here too, or the form
  // would offer a quantity the server (correctly) refuses.
  const remaining = r.lines.map((l) => ({ ...l, left: l.qty - l.qty_committed })).filter((l) => l.left > 0);
  const [qtys, setQtys] = useState<Record<number, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function fulfil() {
    setError("");
    const payload = remaining
      .map((l) => ({ line_id: l.id, qty: Number(qtys[l.id] || 0) }))
      .filter((l) => l.qty > 0);
    if (!payload.length) { setError("Enter a quantity for at least one line."); return; }
    setBusy(true);
    try {
      const { data } = await api.post(`/outbound/stock-requests/${r.id}/fulfil`, { lines: payload });
      navigate(`/transfer/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
      setBusy(false);
    }
  }

  if (remaining.length === 0) return null;

  return (
    <div className="card section-card" data-testid="fulfil-panel">
      <p className="eyebrow">Fulfil this request</p>
      <p className="lead" style={{ marginBottom: 10 }}>
        Send some or all of what is left — this creates a draft transfer, pre-filled with what
        you enter below. It still needs the Operations Head's approval before anything dispatches.
      </p>
      <table className="lines-table" data-testid="fulfil-lines">
        <thead>
          <tr>
            <th>SKU</th>
            <th className="num">Left to send</th>
            <th className="num">Send now</th>
          </tr>
        </thead>
        <tbody>
          {remaining.map((l) => (
            <tr key={l.id}>
              <td className="mono">{l.sku_code}</td>
              <td className="num">{l.left}</td>
              <td>
                <input
                  className="num"
                  value={qtys[l.id] ?? ""}
                  onChange={(e) => setQtys((q) => ({ ...q, [l.id]: e.target.value }))}
                  placeholder="0"
                  data-testid={`fulfil-qty-${l.id}`}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <div className="login-error" style={{ marginTop: 10 }} data-testid="fulfil-error">{error}</div>}
      <button className="btn btn-cta" style={{ marginTop: 12 }} disabled={busy} onClick={fulfil} data-testid="fulfil-btn">
        <Truck size={15} /> {busy ? "Creating transfer…" : "Create pre-filled transfer"}
      </button>
      <ClosePanel r={r} onDone={onDone} />
    </div>
  );
}

function ClosePanel({ r, onDone }: { r: StockRequestT; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function close() {
    setError("");
    setBusy(true);
    try {
      await api.post(`/outbound/stock-requests/${r.id}/close`, { note });
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ marginTop: 16, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
      <p className="lead" style={{ marginBottom: 8 }}>
        Not sending any more? Close the request as it stands — it reads as partly fulfilled.
      </p>
      <input
        className="input"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Why nothing more is coming (optional)"
        style={{ maxWidth: 420, marginBottom: 8 }}
        data-testid="close-note"
      />
      {error && <div className="login-error" data-testid="close-error">{error}</div>}
      <div>
        <button type="button" className="btn" disabled={busy} onClick={close} data-testid="close-request-btn">
          <XCircle size={15} /> {busy ? "Closing…" : "Close request"}
        </button>
      </div>
    </div>
  );
}

export function StockRequestDetailPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const { data: r, loading, reload } = useDoc<StockRequestT>(`/outbound/stock-requests/${id}`);
  const writable = canWriteTransfer(user);

  if (loading || !r) return <div className="page-pad"><p className="lead">Loading…</p></div>;

  const totalQty = r.lines.reduce((s, l) => s + l.qty, 0);
  const totalFulfilled = r.lines.reduce((s, l) => s + l.qty_fulfilled, 0);
  const canAct = writable && ["approved", "being_fulfilled", "partly_fulfilled"].includes(r.status);

  return (
    <div className="page-pad">
      <Link to="/transfer/requests" className="btn" style={{ marginBottom: 16 }} data-testid="sr-detail-back">
        <ArrowLeft size={15} /> Stock requests
      </Link>
      <div className="toolbar">
        <div>
          <p className="eyebrow">{r.doc_number || `Draft #${r.id}`}</p>
          <h1 className="h1">{r.requesting_store_code} asks {r.fulfilling_store_code}</h1>
          <p className="lead">{r.requesting_store_name} → {r.fulfilling_store_name}{r.notes ? ` · ${r.notes}` : ""}</p>
        </div>
        <div className="spacer" />
        {r.source === "cross_store_search" && (
          <span className="chip chip-blue" data-testid="sr-source">
            From store search
          </span>
        )}
        <StatusPill status={r.status} label={r.status_display} />
      </div>

      {r.status === "declined" && r.decline_reason && (
        <div className="warn-note" style={{ marginBottom: 18 }} data-testid="sr-decline-reason">
          <b>Declined:</b> {r.decline_reason}
        </div>
      )}

      <div className="form-row" style={{ marginBottom: 18 }}>
        <div className="card section-card">
          <p className="eyebrow">Requested</p>
          <h3 className="h3">{totalQty} pcs</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Fulfilled</p>
          <h3 className="h3">{totalFulfilled} pcs</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Lines</p>
          <h3 className="h3">{r.lines.length}</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Date</p>
          <h3 className="h3">{fmtDate(r.created_at)}</h3>
        </div>
        {r.expected_arrival_at && (
          // The counter quoted this to a person who is coming back to ask about
          // it. A time nobody can read back is a time that was never carried.
          <div className="card section-card" data-testid="sr-expected-arrival">
            <p className="eyebrow">Customer told</p>
            <h3 className="h3">{formatDateTime(r.expected_arrival_at)}</h3>
            <p className="lead">No hold was placed on the piece.</p>
          </div>
        )}
      </div>

      <div style={{ marginBottom: 18 }}>
        <ApprovalTrail
          createdByName={r.created_by_name}
          createdAt={r.created_at}
          approval={r.approval}
          history={r.approval_history}
          askAgainPath={writable ? `/outbound/stock-requests/${r.id}/request-approval` : undefined}
        />
      </div>

      {canAct && <div style={{ marginBottom: 18 }}><FulfilPanel r={r} onDone={reload} /></div>}

      <div className="table-wrap" style={{ marginBottom: 18 }}>
        <table className="data" data-testid="sr-detail-lines">
          <thead>
            <tr>
              <th>SKU</th>
              <th>Design</th>
              <th>Size</th>
              <th>Colour</th>
              <th>Brand</th>
              <th className="num">Requested</th>
              <th className="num">Fulfilled</th>
            </tr>
          </thead>
          <tbody>
            {r.lines.map((l) => (
              <tr key={l.id}>
                <td><b className="mono">{l.sku_code}</b></td>
                <td>{l.design || "—"}</td>
                <td>{l.size || "—"}</td>
                <td>{l.color || "—"}</td>
                <td>{l.brand || "—"}</td>
                <td className="num">{l.qty}</td>
                <td className="num">{l.qty_fulfilled}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {r.fulfilling_transfers.length > 0 && (
        <div className="table-wrap">
          <p className="eyebrow" style={{ marginBottom: 8 }}>Transfers answering this request</p>
          <table className="data" data-testid="sr-fulfilling-transfers">
            <thead>
              <tr>
                <th>Doc #</th>
                <th>Route</th>
                <th>Status</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {r.fulfilling_transfers.map((t) => (
                <tr key={t.id}>
                  <td>
                    <Link to={`/transfer/${t.id}`} className="link-cell mono" data-testid={`sr-transfer-link-${t.id}`}>
                      <Search size={13} /> {t.doc_number || `Draft #${t.id}`}
                    </Link>
                  </td>
                  <td>{t.source_store_code} → {t.destination_store_code}</td>
                  <td>{t.docstatus === 1 ? "Dispatched" : "Draft"}</td>
                  <td>{fmtDate(t.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
