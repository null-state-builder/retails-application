import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  AlertTriangle,
  Boxes,
  Download,
  PackageCheck,
  Plus,
  Printer,
  Send,
  Trash2,
  Truck,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useDoc, useList } from "../lib/hooks";
import { Money } from "../lib/format";
import { canCloseTransferGap, canWriteTransfer } from "../lib/outbound-rbac";
import { destinationOptions, isCrossState, type LocationT } from "../lib/transfer-locations";
import { ScanScreen, type ScanResult, type ScanTarget } from "../components/ScanScreen";
import { ListSearchBar } from "../components/SearchBox";
import { ApprovalPill, ApprovalTrail, isCleared, type ApprovalT } from "../components/approval";
import "./Booking.css";
import { PageHeader } from "../components/PageHeader";

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

export function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

const DS_TONE: Record<number, string> = { 0: "grey", 1: "green", 2: "red" };
const DS_LABEL: Record<number, string> = { 0: "Draft", 1: "Submitted", 2: "Cancelled" };

function DocPill({ ds }: { ds: number }) {
  return <span className={`chip chip-${DS_TONE[ds] ?? "grey"} status-pill`}>{DS_LABEL[ds] ?? ds}</span>;
}

const TRANSFER_TYPE_LABEL: Record<string, string> = {
  store_split: "Store split",
  inter_store: "Inter-store",
};

const REASON_OPTIONS = [
  { value: "sister_store_request", label: "Sister store request" },
  { value: "slow_mover", label: "Slow mover" },
  { value: "seasonal_swap", label: "Seasonal swap" },
  { value: "free_floor_space", label: "Free floor space" },
  { value: "customer_waiting", label: "Customer waiting" },
  { value: "other", label: "Other" },
];

const TRANSPORT_OPTIONS = [
  { value: "public_bus", label: "Public bus" },
  { value: "courier", label: "Courier" },
  { value: "own_vehicle", label: "Own vehicle" },
  { value: "hand_carried", label: "Hand-carried" },
];

const RECEIPT_TONE: Record<string, string> = {
  pending: "amber",
  complete: "green",
  shortfall: "red",
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface StoreT {
  id: number;
  code: string;
  name: string;
  store_type: string;
  state_name: string;
}

interface TransferLineT {
  id: number;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  season: string;
  item: string;
  hsn: string;
  qty_planned: number | null;
  qty_dispatched: number;
  qty_received: number;
  qty_resolved: number;
  qty_in_transit: number;
  unit_cost_paise: number;
}

export interface ReceiptExceptionT {
  id: number;
  kind: "short" | "extra" | "damaged";
  kind_label: string;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  qty: number;
  unit_cost_paise: number;
  note: string;
}

interface ReceiptT {
  id: number;
  received_by: number | null;
  received_by_name: string;
  receipt_date: string;
  receipt_status: string;
  shortfall_notes: string;
  exceptions: ReceiptExceptionT[];
}

export interface GapClosureT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  transfer: number;
  transfer_doc_number: string | null;
  reason: string;
  reason_label: string;
  note: string;
  approved_by_name: string;
  approval: ApprovalT | null;
  approval_history: ApprovalT[];
  created_by_name: string;
  created_at: string;
  lines: { id: number; sku_code: string; qty: number; unit_cost_paise: number }[];
}

export interface TransferT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  transfer_type: string;
  is_cross_state: boolean;
  source_store: number;
  source_store_code: string;
  source_store_name: string;
  destination_store: number;
  destination_store_code: string;
  destination_store_name: string;
  reason: string;
  transport_mode: string;
  transport_ref: string;
  dispatcher_name: string;
  expected_arrival_note: string;
  eway_bill_number: string;
  dispatch_date: string | null;
  dispatched_by: number | null;
  dispatched_by_name: string;
  created_by: number | null;
  created_by_name: string;
  /** Stamped at dispatch from the approval — never typed (#137). */
  approved_by: number | null;
  approved_by_name: string;
  /** The live request. No stock leaves until this is cleared (#137). */
  approval: ApprovalT | null;
  approval_history: ApprovalT[];
  created_at: string;
  updated_at: string;
  dispatch_mismatch: boolean;
  qty_in_transit: number;
  /** Derived server-side: "" (draft) · in_transit · received · gap · closed. */
  gap_state: string;
  /** When the transfer's PT was cut. Null on a draft — nothing scanned yet (#72). */
  pt_generated_at: string | null;
  lines: TransferLineT[];
  receipt: ReceiptT | null;
  gap_closure: GapClosureT | null;
  /** Set at dispatch only when the destination is a partner store: qty × the
   *  books' Purchase Price (Rule 12). Null for every ordinary transfer. */
  partner_billing_value_paise: number | null;
}

export const GAP_STATE_LABEL: Record<string, string> = {
  in_transit: "In transit",
  received: "Received",
  gap: "Gap — sent ≠ received",
  closed: "Gap closed",
};

const GAP_STATE_TONE: Record<string, string> = {
  in_transit: "amber",
  received: "green",
  gap: "red",
  closed: "grey",
};

export function GapStatePill({ state }: { state: string }) {
  if (!state) return null;
  return (
    <span className={`chip chip-${GAP_STATE_TONE[state] ?? "grey"}`} data-testid="gap-state-pill">
      {GAP_STATE_LABEL[state] ?? state}
    </span>
  );
}

const EXCEPTION_TONE: Record<string, string> = {
  short: "red",
  extra: "amber",
  damaged: "amber",
};

/** The three receive outcomes, as a table anyone can read months later (#71). */
export function ReceiptExceptions({ rows }: { rows: ReceiptExceptionT[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="table-wrap" data-testid="receipt-exceptions">
      <table className="data">
        <thead>
          <tr>
            <th>What happened</th>
            <th>SKU</th>
            <th>Item</th>
            <th className="num">Pieces</th>
            <th>Note</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr key={e.id} data-testid={`exception-${e.kind}-${e.sku_code}`}>
              <td>
                <span className={`chip chip-${EXCEPTION_TONE[e.kind] ?? "grey"}`}>{e.kind_label}</span>
              </td>
              <td><b className="mono">{e.sku_code}</b></td>
              <td>{[e.design, e.color, e.size].filter(Boolean).join(" · ") || e.brand || "—"}</td>
              <td className="num"><b>{e.qty}</b></td>
              <td>{e.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export function TransferListPage() {
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const tab = params.get("type") === "store_split" ? "store_split" : "inter_store";
  const writable = canWriteTransfer(user);
  const [q, setQ] = useState("");

  const { data, loading } = useList<TransferT>("/outbound/transfers", { type: tab, q });

  function setTab(next: string) {
    setParams((p) => { p.set("type", next); return p; });
  }

  return (
    <div className="page-pad">
      <PageHeader
        actions={
          writable && (
            <Link className="btn btn-cta" to="/transfer/new" data-testid="new-transfer-btn">
              <Plus size={16} /> New transfer
            </Link>
          )
        }
      />

      <div className="mode-toggle" data-testid="transfer-type-toggle" style={{ maxWidth: 520, marginBottom: 18 }}>
        <button
          type="button"
          className={`mode-btn ${tab === "inter_store" ? "active" : ""}`}
          onClick={() => setTab("inter_store")}
          data-testid="transfer-tab-inter"
        >
          <ArrowRight size={16} /> Inter-store
        </button>
        <button
          type="button"
          className={`mode-btn ${tab === "store_split" ? "active" : ""}`}
          onClick={() => setTab("store_split")}
          data-testid="transfer-tab-split"
        >
          <Boxes size={16} /> Store split (WH → store)
        </button>
      </div>

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search transfers — doc number, from, to"
        label="Search transfers"
        testId="transfers-search"
        noun="transfer"
        count={data.length}
        loading={loading}
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : data.length === 0 ? (
        <div className="card section-card" data-testid="transfer-empty">
          {q
            ? `No ${tab === "store_split" ? "store split" : "inter-store"} transfer matches “${q}”. `
              + "Try the document number, or the store it came from or went to."
            : `No ${tab === "store_split" ? "store split" : "inter-store"} transfers yet.`}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="transfer-table">
            <thead>
              <tr>
                <th>Doc #</th>
                <th>From</th>
                <th>To</th>
                <th>Reason</th>
                <th className="num">Lines</th>
                <th>Cross-state</th>
                <th>Status</th>
                <th>Receipt</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {data.map((t) => (
                <tr key={t.id} data-testid={`transfer-row-${t.id}`}>
                  <td>
                    <Link to={`/transfer/${t.id}`} className="link-cell mono" data-testid={`transfer-link-${t.id}`}>
                      <b>{t.doc_number || `Draft #${t.id}`}</b>
                    </Link>
                  </td>
                  <td><b className="mono">{t.source_store_code}</b></td>
                  <td><b className="mono">{t.destination_store_code}</b></td>
                  <td>{REASON_OPTIONS.find((r) => r.value === t.reason)?.label || t.reason || "—"}</td>
                  <td className="num">{t.lines.length}</td>
                  <td>
                    {t.is_cross_state ? (
                      <span className="chip chip-amber">Cross-state</span>
                    ) : (
                      <span className="chip chip-grey">Same state</span>
                    )}
                  </td>
                  <td>
                    <DocPill ds={t.docstatus} />
                    {/* A draft's real state has two halves since #137: it is a
                        draft, and it is either cleared to go or it is not. */}
                    {t.docstatus === 0 && <ApprovalPill status={t.approval?.status ?? "pending"} />}
                  </td>
                  <td>
                    {t.receipt ? (
                      <span className={`chip chip-${RECEIPT_TONE[t.receipt.receipt_status] ?? "grey"}`}>
                        {t.receipt.receipt_status}
                      </span>
                    ) : t.docstatus === 1 ? (
                      <span className="chip chip-amber">In transit</span>
                    ) : (
                      "—"
                    )}
                  </td>
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

// ---------------------------------------------------------------------------
// Create
// ---------------------------------------------------------------------------

interface DraftPlanLine {
  sku_code: string;
  qty_planned: number | string;
}
const emptyLine = (): DraftPlanLine => ({ sku_code: "", qty_planned: "" });

export function TransferNewPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  // Two lists on purpose (#147): the source is what this person may operate on,
  // the destination is anywhere in the network. See `lib/transfer-locations`.
  const { data: stores } = useList<StoreT>("/masters/stores");
  const { data: locations } = useList<LocationT>("/masters/locations");

  const storeLocked = user?.scope_type === "store" && (user?.stores?.length ?? 0) >= 1;
  const lockedStore = storeLocked ? user!.stores[0] : null;

  const [sourceId, setSourceId] = useState<string>(lockedStore ? String(lockedStore.id) : "");
  const [destId, setDestId] = useState("");
  const [transferType, setTransferType] = useState("inter_store");
  const [reason, setReason] = useState("");
  const [transportMode, setTransportMode] = useState("");
  const [transportRef, setTransportRef] = useState("");
  const [dispatcherName, setDispatcherName] = useState("");
  const [expectedArrival, setExpectedArrival] = useState("");
  const [ewayBill, setEwayBill] = useState("");
  const [lines, setLines] = useState<DraftPlanLine[]>([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  // Cross-state is read off the network list, since that is the only one that
  // holds both ends for a store-scoped user.
  const crossState = useMemo(
    () => isCrossState(locations, sourceId, destId),
    [locations, sourceId, destId],
  );
  const destinations = useMemo(
    () => destinationOptions(locations, sourceId),
    [locations, sourceId],
  );

  function setLine(i: number, key: keyof DraftPlanLine, val: string) {
    setLines((ls) => ls.map((l, idx) => (idx === i ? { ...l, [key]: val } : l)));
  }

  async function save() {
    setError("");
    if (!sourceId) { setError("Select a source store."); return; }
    if (!destId) { setError("Select a destination store."); return; }
    if (sourceId === destId) { setError("Source and destination must differ."); return; }
    if (crossState && !ewayBill.trim()) { setError("E-way bill number is required for cross-state transfers."); return; }
    const payloadLines = lines
      .filter((l) => l.sku_code && Number(l.qty_planned) > 0)
      .map((l) => ({ sku_code: l.sku_code.trim(), qty_planned: Number(l.qty_planned) }));
    if (lines.some((l) => l.sku_code && !(Number(l.qty_planned) > 0))) {
      setError("Every plan line needs a quantity of at least 1.");
      return;
    }
    setSaving(true);
    try {
      const { data } = await api.post("/outbound/transfers", {
        source_store: Number(sourceId),
        destination_store: Number(destId),
        transfer_type: transferType,
        reason,
        transport_mode: transportMode,
        transport_ref: transportRef,
        dispatcher_name: dispatcherName,
        expected_arrival_note: expectedArrival,
        eway_bill_number: ewayBill,
        lines: payloadLines,
      });
      navigate(`/transfer/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/transfer" className="btn" style={{ marginBottom: 16 }} data-testid="transfer-back-link">
        <ArrowLeft size={15} /> Transfers
      </Link>
      <h1 className="h1 h2-rust" style={{ marginBottom: 18 }}>New transfer</h1>

      <div className="card section-card">
        <p className="eyebrow">Step 1 · Locations</p>
        <div className="form-row" style={{ marginTop: 10 }}>
          <div className="field">
            <label>Source store / warehouse</label>
            {storeLocked && lockedStore ? (
              <div className="store-lock" data-testid="transfer-source-locked">
                {lockedStore.code} · {lockedStore.name}
              </div>
            ) : (
              <select className="select" value={sourceId} onChange={(e) => setSourceId(e.target.value)} data-testid="transfer-source-select">
                <option value="">Select source…</option>
                {stores.map((s) => (
                  <option key={s.id} value={s.id}>{s.code} · {s.name} {s.store_type === "warehouse" ? "(WH)" : ""}</option>
                ))}
              </select>
            )}
          </div>
          <div className="field">
            <label>Destination store / warehouse</label>
            <select className="select" value={destId} onChange={(e) => setDestId(e.target.value)} data-testid="transfer-dest-select">
              <option value="">Select destination…</option>
              {destinations.map((l) => (
                <option key={l.id} value={l.id}>{l.code} · {l.name} {l.store_type === "warehouse" ? "(WH)" : ""}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Transfer type</label>
            <select className="select" value={transferType} onChange={(e) => setTransferType(e.target.value)} data-testid="transfer-type-select">
              <option value="inter_store">Inter-store</option>
              <option value="store_split">Store split (WH → store)</option>
            </select>
          </div>
          <div className="field">
            <label>Reason</label>
            <select className="select" value={reason} onChange={(e) => setReason(e.target.value)} data-testid="transfer-reason-select">
              <option value="">Select reason…</option>
              {REASON_OPTIONS.map((r) => (
                <option key={r.value} value={r.value}>{r.label}</option>
              ))}
            </select>
          </div>
        </div>
        {crossState && (
          <div className="warn-note" style={{ marginTop: 14 }} data-testid="cross-state-warning">
            <AlertTriangle size={14} style={{ verticalAlign: "middle", marginRight: 6 }} />
            Cross-state transfer (Bihar ↔ Jharkhand) — <b>E-way bill is required</b>.
          </div>
        )}
      </div>

      <div className="card section-card">
        <p className="eyebrow">Step 2 · Transport details</p>
        <div className="form-row" style={{ marginTop: 10 }}>
          <div className="field">
            <label>Transport mode</label>
            <select className="select" value={transportMode} onChange={(e) => setTransportMode(e.target.value)} data-testid="transfer-transport-mode">
              <option value="">Select mode…</option>
              {TRANSPORT_OPTIONS.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Bus / courier AWB / vehicle plate</label>
            <input className="input" value={transportRef} onChange={(e) => setTransportRef(e.target.value)} placeholder="Transport ID" data-testid="transfer-transport-ref" />
          </div>
          <div className="field">
            <label>Dispatcher name</label>
            <input className="input" value={dispatcherName} onChange={(e) => setDispatcherName(e.target.value)} placeholder="Who is dispatching?" data-testid="transfer-dispatcher" />
          </div>
          <div className="field">
            <label>Expected arrival</label>
            <input className="input" value={expectedArrival} onChange={(e) => setExpectedArrival(e.target.value)} placeholder="e.g. Tomorrow by 2 PM" data-testid="transfer-expected-arrival" />
          </div>
          {crossState && (
            <div className="field">
              <label>E-way bill number *</label>
              <input className="input" value={ewayBill} onChange={(e) => setEwayBill(e.target.value)} placeholder="Required for cross-state" data-testid="transfer-eway-bill" />
            </div>
          )}
        </div>
      </div>

      <div className="card section-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div>
            <p className="eyebrow">Step 3 · Plan (optional)</p>
            <h3 className="h3">Planned lines</h3>
          </div>
          <button type="button" className="btn" onClick={() => setLines((l) => [...l, emptyLine()])} data-testid="add-transfer-line">
            <Plus size={15} /> Add plan line
          </button>
        </div>
        <p className="lead" style={{ marginBottom: 10 }}>
          The plan is what dispatch scans <b>against</b> — the scanned pieces are the only
          quantities that move stock. Leave it empty to build the transfer by scanning the
          carton at dispatch (store → store).
        </p>
        {lines.length > 0 && (
          <table className="lines-table" data-testid="transfer-lines">
            <thead>
              <tr>
                <th style={{ width: "50%" }}>Barcode / SKU</th>
                <th className="num">Planned qty</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {lines.map((l, i) => (
                <tr key={i}>
                  <td><input value={l.sku_code} onChange={(e) => setLine(i, "sku_code", e.target.value)} data-testid={`tl-sku-${i}`} /></td>
                  <td><input className="num" value={l.qty_planned} onChange={(e) => setLine(i, "qty_planned", e.target.value)} data-testid={`tl-qty-${i}`} /></td>
                  <td><button type="button" className="line-del" onClick={() => setLines((ls) => ls.filter((_, idx) => idx !== i))} data-testid={`delete-tl-${i}`}><Trash2 size={15} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {error && <div className="login-error" style={{ maxWidth: 540 }} data-testid="transfer-create-error">{error}</div>}
      <button className="btn btn-primary btn-lg" disabled={saving} onClick={save} data-testid="save-transfer-btn">
        <Truck size={16} /> {saving ? "Saving…" : "Create transfer (draft)"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The PT that travels with the carton (#72)
// ---------------------------------------------------------------------------

export interface TransferPtT {
  id: number;
  transfer: number;
  doc_number: string | null;
  source_store_code: string;
  source_store_name: string;
  destination_store_code: string;
  destination_store_name: string;
  dispatch_date: string | null;
  generated_at: string;
  generated_by_name: string;
  columns: string[];
  rows: Record<string, string | number>[];
}

/** Pull the PT down as a file. Same blob dance as the PT-mapper export - the
 *  API needs the auth header, so a plain link cannot fetch it.
 *
 *  The filename comes off the response: the server already names the PT after
 *  its voucher, and restating that rule here would be one rule in two languages. */
async function downloadPt(transferId: number | string, kind: "csv" | "xlsx") {
  const res = await api.get(`/outbound/transfers/${transferId}/pt.${kind}`, { responseType: "blob" });
  const named = /filename="([^"]+)"/.exec(res.headers["content-disposition"] ?? "");
  const href = URL.createObjectURL(res.data);
  const a = document.createElement("a");
  a.href = href;
  a.download = named?.[1] ?? `KDPS-PT-${transferId}.${kind}`;
  a.click();
  URL.revokeObjectURL(href);
}

/** The PT panel on a dispatched transfer: what it is, and the three ways out
 *  of it. Never an editor — the PT is regenerated from the scanned lines or it
 *  does not change. */
function TransferPtCard({ t }: { t: TransferT }) {
  if (!t.pt_generated_at) return null;
  return (
    <div className="card section-card" data-testid="transfer-pt-card">
      <p className="eyebrow">PT file</p>
      <h3 className="h3">Generated {fmtDate(t.pt_generated_at)}</h3>
      <p className="lead" style={{ marginTop: 6 }}>
        Built from the scanned lines, so the document and the carton say the same thing.
        It is never typed and cannot be edited - send it with the goods.
      </p>
      <div className="toolbar" style={{ marginTop: 14, marginBottom: 0 }}>
        <Link className="btn btn-cta" to={`/transfer/${t.id}/pt`} data-testid="transfer-pt-open">
          <Printer size={15} /> View &amp; print
        </Link>
        <button
          type="button"
          className="btn"
          onClick={() => void downloadPt(t.id, "xlsx")}
          data-testid="transfer-pt-xlsx"
        >
          <Download size={15} /> Excel (KDPS)
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => void downloadPt(t.id, "csv")}
          data-testid="transfer-pt-csv"
        >
          <Download size={15} /> CSV
        </button>
      </div>
    </div>
  );
}

/** The printable PT — one transfer, one carton, one document. */
export function TransferPtPage() {
  const { id } = useParams();
  const { data: pt, loading } = useDoc<TransferPtT>(`/outbound/transfers/${id}/pt`);

  if (loading) return <div className="page-pad"><p className="lead">Loading…</p></div>;
  if (!pt) {
    return (
      <div className="page-pad" data-testid="transfer-pt-missing">
        <Link to={`/transfer/${id}`} className="btn" style={{ marginBottom: 16 }}>
          <ArrowLeft size={15} /> Transfer
        </Link>
        <p className="lead">
          This transfer has no PT yet. One is generated the moment the carton is scanned and
          dispatched.
        </p>
      </div>
    );
  }

  return (
    <div className="page-pad pt-print">
      <div className="pt-print-hide">
        <Link to={`/transfer/${pt.transfer}`} className="btn" style={{ marginBottom: 16 }} data-testid="transfer-pt-back">
          <ArrowLeft size={15} /> Transfer
        </Link>
      </div>

      <div className="toolbar">
        <div>
          <p className="eyebrow">PT file · {pt.doc_number || `Draft #${pt.transfer}`}</p>
          <h1 className="h1">{pt.source_store_code} → {pt.destination_store_code}</h1>
          <p className="lead">
            {pt.source_store_name} → {pt.destination_store_name} ·{" "}
            {pt.rows.length} line(s) · generated {fmtDate(pt.generated_at)}
            {pt.generated_by_name ? ` by ${pt.generated_by_name}` : ""}
          </p>
        </div>
        <div className="spacer" />
        <div className="toolbar pt-print-hide" style={{ marginBottom: 0 }}>
          <button
            type="button"
            className="btn btn-cta"
            onClick={() => window.print()}
            data-testid="transfer-pt-print"
          >
            <Printer size={15} /> Print
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => void downloadPt(pt.transfer, "xlsx")}
            data-testid="transfer-pt-page-xlsx"
          >
            <Download size={15} /> Excel (KDPS)
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => void downloadPt(pt.transfer, "csv")}
            data-testid="transfer-pt-page-csv"
          >
            <Download size={15} /> CSV
          </button>
        </div>
      </div>

      <div className="table-wrap">
        <table className="data" data-testid="transfer-pt-table">
          <thead>
            <tr>{pt.columns.map((c) => <th key={c}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {pt.rows.map((row, i) => (
              <tr key={i} data-testid={`transfer-pt-row-${i}`}>
                {pt.columns.map((c) => (
                  <td key={c} className={c === "QTY" || c === "NAG" ? "num" : undefined}>
                    {row[c] === "" || row[c] == null ? "—" : String(row[c])}
                  </td>
                ))}
              </tr>
            ))}
            {pt.rows.length === 0 && (
              <tr>
                <td colSpan={pt.columns.length} style={{ textAlign: "center", opacity: 0.7 }}>
                  This PT has no lines.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

function lineLabel(l: TransferLineT): string {
  return [l.design, l.color, l.size].filter(Boolean).join(" · ") || l.brand || "—";
}

/** A scan tally as the API's ``[{barcode, qty}]`` shape. */
function asPairs(counts: Record<string, number>) {
  return Object.entries(counts).map(([barcode, qty]) => ({ barcode, qty }));
}

export function TransferDetailPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const { data: t, loading } = useDoc<TransferT>(`/outbound/transfers/${id}`);
  const writable = canWriteTransfer(user);
  const [scanMode, setScanMode] = useState<"" | "dispatch" | "receive">("");
  const [posting, setPosting] = useState(false);
  const [scanError, setScanError] = useState("");

  if (loading || !t) return <div className="page-pad"><p className="lead">Loading…</p></div>;

  // No stock leaves until the Operations Head has approved (#137). The server
  // refuses either way — the button only reflects that honestly, so nobody is
  // walked through a scan the API will throw away.
  const approved = isCleared(t.approval);
  const canDispatch = t.docstatus === 0 && writable && approved;
  const canReceive = t.docstatus === 1 && !t.receipt && writable;
  const hasPlan = t.lines.some((l) => l.qty_planned != null);

  // Dispatch scans against the plan (or builds the lines when there is none);
  // receive scans against what was sent. The scans are the only quantities.
  const dispatchTargets: ScanTarget[] = t.lines
    .filter((l) => l.qty_planned != null)
    .map((l) => ({ barcode: l.sku_code, label: lineLabel(l), expected: l.qty_planned }));
  const receiveTargets: ScanTarget[] = t.lines
    .filter((l) => l.qty_dispatched > 0)
    .map((l) => ({ barcode: l.sku_code, label: lineLabel(l), expected: l.qty_dispatched }));

  async function lookupAtSource(barcode: string): Promise<ScanTarget | null> {
    try {
      const { data } = await api.get(
        `/outbound/scan-lookup?store=${t!.source_store}&barcode=${encodeURIComponent(barcode)}`,
      );
      return {
        barcode: data.barcode,
        label: [data.design, data.color, data.size].filter(Boolean).join(" · ") || data.brand,
        expected: null,
        available: data.available_qty,
      };
    } catch {
      return null;
    }
  }

  async function postScans(action: "dispatch" | "receive", result: ScanResult) {
    setScanError("");
    setPosting(true);
    try {
      await api.post(`/outbound/transfers/${t!.id}/${action}`, {
        scans: asPairs(result.scans),
        ...(action === "receive"
          ? {
              damaged: asPairs(result.damaged),
              extras: asPairs(result.extras),
              notes: result.notes,
            }
          : {}),
      });
      window.location.reload();
    } catch (e) {
      setScanError(apiErrorMessage(e));
      setPosting(false);
    }
  }

  const totalDispatched = t.lines.reduce((s, l) => s + l.qty_dispatched, 0);
  const totalReceived = t.lines.reduce((s, l) => s + l.qty_received, 0);

  return (
    <div className="page-pad">
      <Link to="/transfer" className="btn" style={{ marginBottom: 16 }} data-testid="transfer-detail-back">
        <ArrowLeft size={15} /> Transfers
      </Link>
      <div className="toolbar">
        <div>
          <p className="eyebrow">{t.doc_number || `Draft #${t.id}`}</p>
          <h1 className="h1">{t.source_store_code} → {t.destination_store_code}</h1>
          <p className="lead">
            {t.source_store_name} → {t.destination_store_name}
            {t.reason ? ` · ${REASON_OPTIONS.find((r) => r.value === t.reason)?.label || t.reason}` : ""}
          </p>
        </div>
        <div className="spacer" />
        <DocPill ds={t.docstatus} />
        {t.docstatus === 0 && <ApprovalPill status={t.approval?.status ?? "pending"} />}
        <GapStatePill state={t.gap_state} />
        {t.is_cross_state && <span className="chip chip-amber">Cross-state</span>}
        <span className="chip chip-navy">{TRANSFER_TYPE_LABEL[t.transfer_type] ?? t.transfer_type}</span>
        {t.docstatus === 1 && t.dispatch_mismatch && (
          <span className="chip chip-amber" data-testid="dispatch-mismatch-chip">Plan mismatch</span>
        )}
        {canDispatch && (
          <button
            type="button"
            className="btn btn-cta"
            onClick={() => { setScanError(""); setScanMode("dispatch"); }}
            data-testid="dispatch-transfer-btn"
          >
            <Send size={15} /> Scan &amp; dispatch
          </button>
        )}
        {canReceive && (
          <button
            type="button"
            className="btn btn-cta"
            onClick={() => { setScanError(""); setScanMode("receive"); }}
            data-testid="receive-transfer-toggle"
          >
            <PackageCheck size={15} /> Scan &amp; receive
          </button>
        )}
      </div>

      {scanMode === "dispatch" && (
        <ScanScreen
          mode="DISPATCH"
          docLabel={t.doc_number || `Draft #${t.id}`}
          routeLabel={`${t.source_store_code} → ${t.destination_store_code}`}
          targets={dispatchTargets}
          lookup={hasPlan ? undefined : lookupAtSource}
          confirmLabel="Confirm dispatch"
          busy={posting}
          error={scanError}
          onConfirm={(result) => void postScans("dispatch", result)}
          onClose={() => setScanMode("")}
        />
      )}
      {scanMode === "receive" && (
        <ScanScreen
          mode="RECEIVE"
          docLabel={t.doc_number || `Draft #${t.id}`}
          routeLabel={`${t.source_store_code} → ${t.destination_store_code}`}
          targets={receiveTargets}
          strictExpected
          exceptions
          confirmLabel="Confirm receipt"
          busy={posting}
          error={scanError}
          onConfirm={(result) => void postScans("receive", result)}
          onClose={() => setScanMode("")}
        />
      )}

      {/* Stats row */}
      <div className="form-row" style={{ marginBottom: 18 }}>
        <div className="card section-card">
          <p className="eyebrow">Transport</p>
          <h3 className="h3">{TRANSPORT_OPTIONS.find((o) => o.value === t.transport_mode)?.label || t.transport_mode || "—"}</h3>
          {t.transport_ref && <p className="lead" style={{ marginTop: 4 }}>{t.transport_ref}</p>}
        </div>
        <div className="card section-card">
          <p className="eyebrow">Dispatcher</p>
          <h3 className="h3">{t.dispatcher_name || "—"}</h3>
          {t.expected_arrival_note && <p className="lead" style={{ marginTop: 4 }}>{t.expected_arrival_note}</p>}
        </div>
        <div className="card section-card">
          <p className="eyebrow">Dispatched / Received</p>
          <h3 className="h3">{totalDispatched} / {totalReceived} pcs</h3>
          {t.qty_in_transit > 0 && (
            <p className="lead" style={{ marginTop: 4 }} data-testid="in-transit-count">
              <b>{t.qty_in_transit} pcs in transit</b>
            </p>
          )}
        </div>
        <div className="card section-card">
          <p className="eyebrow">Raised</p>
          <h3 className="h3">{fmtDate(t.created_at)}</h3>
          {/* Who actually let the stock go, and when — the other half of the
              trail. The "Dispatcher" card above is the free-text person carrying
              the carton, which is not the same question (#137). */}
          {t.dispatch_date && (
            <p className="lead" style={{ marginTop: 4 }} data-testid="dispatched-by">
              Sent by <b>{t.dispatched_by_name || "—"}</b> on {fmtDate(t.dispatch_date)}
            </p>
          )}
        </div>
      </div>

      {t.eway_bill_number && (
        <div className="card section-card" style={{ marginBottom: 18 }}>
          <p className="eyebrow">E-way bill</p>
          <h3 className="h3 mono">{t.eway_bill_number}</h3>
        </div>
      )}

      {/* Only ever set when the destination is a partner store (Rule 12) —
          every ordinary transfer leaves this null, exactly as before. */}
      {t.partner_billing_value_paise != null && (
        <div className="card section-card" style={{ marginBottom: 18 }} data-testid="partner-billing-card">
          <p className="eyebrow">Partner billing</p>
          <h3 className="h3"><Money paise={t.partner_billing_value_paise} /> at Purchase Price</h3>
          <p className="lead" style={{ marginTop: 4 }}>
            {t.destination_store_code} is a partner store — this is what it owes for this transfer.
          </p>
        </div>
      )}

      {/* Who made this transfer and who let the stock go (#137) */}
      <div style={{ marginBottom: 18 }} data-testid="transfer-approval">
        <ApprovalTrail
          createdByName={t.created_by_name}
          createdAt={t.created_at}
          approval={t.approval}
          history={t.approval_history}
          askAgainPath={writable ? `/outbound/transfers/${t.id}/request-approval` : undefined}
        />
      </div>

      {/* The PT that went in the box (#72) */}
      <TransferPtCard t={t} />

      {/* Receipt — and everything that went wrong at it (#71) */}
      {t.receipt && (
        <div className="card section-card" style={{ marginBottom: 18 }} data-testid="transfer-receipt-info">
          <p className="eyebrow">Receipt</p>
          <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
            <span className={`chip chip-${RECEIPT_TONE[t.receipt.receipt_status] ?? "grey"}`}>
              {t.receipt.receipt_status}
            </span>
            <span className="lead">{fmtDate(t.receipt.receipt_date)}</span>
            {t.receipt.received_by_name && (
              <span className="lead">by <b>{t.receipt.received_by_name}</b></span>
            )}
          </div>
          {t.receipt.shortfall_notes && (
            <p className="lead" style={{ marginTop: 10 }} data-testid="shortfall-notes">
              “{t.receipt.shortfall_notes}”
            </p>
          )}
          {t.receipt.exceptions.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <ReceiptExceptions rows={t.receipt.exceptions} />
            </div>
          )}
        </div>
      )}

      {/* The gap and how it was closed */}
      {t.gap_state === "gap" && (
        <div className="card section-card" style={{ marginBottom: 18 }} data-testid="transfer-open-gap">
          <p className="eyebrow">Open gap</p>
          <p className="lead">
            <b>{t.qty_in_transit} piece(s)</b> were sent but never scanned in. They stay in the
            in-transit bucket on this transfer — {t.source_store_code} is answerable for them — until
            the Operations Head closes the gap with a reason. The receiving store cannot close
            it.{" "}
            {t.gap_closure ? (
              <>A closure is already waiting for approval.</>
            ) : (
              // Only offered to the people who can act on it — for anyone else
              // the gaps list is scoped away and the link would go nowhere.
              canCloseTransferGap(user) && (
                <Link to="/transfer/in-transit">Open the gaps list</Link>
              )
            )}
          </p>
        </div>
      )}
      {t.gap_closure && (
        <div className="card section-card" style={{ marginBottom: 18 }} data-testid="transfer-gap-closure">
          <p className="eyebrow">Gap closure {t.gap_closure.doc_number ? `· ${t.gap_closure.doc_number}` : "(draft)"}</p>
          <h3 className="h3">{t.gap_closure.reason_label}</h3>
          {t.gap_closure.note && <p className="lead" style={{ marginTop: 6 }}>{t.gap_closure.note}</p>}
          <div style={{ marginTop: 12 }}>
            <ApprovalTrail
              createdByName={t.gap_closure.created_by_name}
              createdAt={t.gap_closure.created_at}
              approval={t.gap_closure.approval}
              history={t.gap_closure.approval_history}
            />
          </div>
        </div>
      )}

      {/* Lines table — quantities are scan-derived, never typed */}
      <div className="table-wrap">
        <table className="data" data-testid="transfer-detail-lines">
          <thead>
            <tr>
              <th>SKU</th>
              <th>Design</th>
              <th>Size</th>
              <th>Colour</th>
              <th>Brand</th>
              <th className="num">Planned</th>
              <th className="num">Dispatched</th>
              <th className="num">Received</th>
              <th className="num">Gap closed</th>
              <th className="num">In transit</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {t.lines.map((l) => (
              <tr key={l.id}>
                <td><b className="mono">{l.sku_code}</b></td>
                <td>{l.design || "—"}</td>
                <td>{l.size || "—"}</td>
                <td>{l.color || "—"}</td>
                <td>{l.brand || "—"}</td>
                <td className="num">{l.qty_planned ?? "—"}</td>
                <td className="num">
                  {l.qty_dispatched}
                  {t.docstatus === 1 && l.qty_planned != null && l.qty_planned !== l.qty_dispatched && (
                    <span className="chip chip-amber" style={{ marginLeft: 6 }}>≠ plan</span>
                  )}
                </td>
                <td className="num">{l.qty_received}</td>
                <td className="num">{l.qty_resolved || "—"}</td>
                <td className="num">
                  {t.docstatus === 1 && l.qty_in_transit > 0 ? (
                    <b data-testid={`line-in-transit-${l.id}`}>{l.qty_in_transit}</b>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="num">{l.unit_cost_paise ? <Money paise={l.unit_cost_paise} /> : "—"}</td>
              </tr>
            ))}
            {t.lines.length === 0 && (
              <tr>
                <td colSpan={11} style={{ textAlign: "center", opacity: 0.7 }}>
                  No lines yet — this transfer builds its lines by scanning at dispatch.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
