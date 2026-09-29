import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  FileX2,
  Plus,
  Send,
  Trash2,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useDoc, useList } from "../lib/hooks";
import { Money } from "../lib/format";
import { canWriteStockCount } from "../lib/outbound-rbac";
import { ApprovalPill, ApprovalTrail, isCleared, type ApprovalT } from "../components/approval";
import { ListSearchBar } from "../components/SearchBox";
import "./Booking.css";
import { PageHeader } from "../components/PageHeader";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

const DS_TONE: Record<number, string> = { 0: "grey", 1: "green", 2: "red" };
const DS_LABEL: Record<number, string> = { 0: "Draft", 1: "Submitted", 2: "Cancelled" };
function DocPill({ ds }: { ds: number }) {
  return <span className={`chip chip-${DS_TONE[ds] ?? "grey"} status-pill`}>{DS_LABEL[ds] ?? ds}</span>;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface WOLineT {
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
  unit_cost_paise: number;
}

interface WOT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  store: number;
  store_code: string;
  store_name: string;
  reason: string;
  approved_by: number | null;
  approved_by_name: string;
  created_by: number | null;
  created_by_name: string;
  approval: ApprovalT | null;
  approval_history: ApprovalT[];
  created_at: string;
  updated_at: string;
  lines: WOLineT[];
}

interface StoreT { id: number; code: string; name: string; store_type: string; }

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export function WriteOffListPage() {
  const { user } = useAuth();
  const [q, setQ] = useState("");
  const { data, loading } = useList<WOT>("/outbound/writeoffs", { q });
  const writable = canWriteStockCount(user);

  return (
    <div className="page-pad">
      <PageHeader
        actions={
          writable && (
            <Link className="btn btn-cta" to="/stock-count/writeoffs/new" data-testid="new-writeoff-btn">
              <Plus size={16} /> New write-off
            </Link>
          )
        }
      />

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search write-offs — doc number, reason, store"
        label="Search write-offs"
        testId="writeoff-search"
        noun="write-off"
        count={data.length}
        loading={loading}
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : data.length === 0 ? (
        <div className="card section-card" data-testid="writeoff-empty">
          {q
            ? `No write-off matches “${q}”.`
            : "No write-offs yet. Create one for dead stock or refused defectives."}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="writeoff-table">
            <thead>
              <tr>
                <th>Doc #</th>
                <th>Store</th>
                <th>Reason</th>
                <th className="num">Lines</th>
                <th className="num">Total qty</th>
                <th>Status</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {data.map((w) => {
                const totalQty = w.lines.reduce((s, l) => s + l.qty, 0);
                return (
                  <tr key={w.id} data-testid={`wo-row-${w.id}`}>
                    <td>
                      <Link to={`/stock-count/writeoffs/${w.id}`} className="link-cell mono" data-testid={`wo-link-${w.id}`}>
                        <b>{w.doc_number || `Draft #${w.id}`}</b>
                      </Link>
                    </td>
                    <td><b className="mono">{w.store_code}</b></td>
                    <td>{w.reason || "—"}</td>
                    <td className="num">{w.lines.length}</td>
                    <td className="num">{totalQty}</td>
                    <td><DocPill ds={w.docstatus} /></td>
                    <td>{fmtDate(w.created_at)}</td>
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
// Create
// ---------------------------------------------------------------------------

interface DraftWOLine {
  sku_code: string;
  design: string;
  size: string;
  color: string;
  qty: number | string;
}
const emptyLine = (): DraftWOLine => ({
  sku_code: "", design: "", size: "", color: "", qty: "",
});

export function WriteOffNewPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: stores } = useList<StoreT>("/masters/stores");

  const storeLocked = user?.scope_type === "store" && (user?.stores?.length ?? 0) >= 1;
  const lockedStore = storeLocked ? user!.stores[0] : null;

  const [storeId, setStoreId] = useState<string>(lockedStore ? String(lockedStore.id) : "");
  const [reason, setReason] = useState("");
  const [lines, setLines] = useState<DraftWOLine[]>([emptyLine()]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  function setLine(i: number, key: keyof DraftWOLine, val: string) {
    setLines((ls) => ls.map((l, idx) => (idx === i ? { ...l, [key]: val } : l)));
  }

  async function save() {
    setError("");
    if (!storeId) { setError("Select a store."); return; }
    if (!reason.trim()) { setError("Provide a reason for the write-off."); return; }
    const payloadLines = lines
      .filter((l) => l.sku_code && Number(l.qty) > 0)
      .map((l) => ({
        sku_code: l.sku_code,
        design: l.design,
        size: l.size,
        color: l.color,
        qty: Number(l.qty),
      }));
    if (!payloadLines.length) { setError("Add at least one line with a SKU and quantity."); return; }
    setSaving(true);
    try {
      // No approver in the payload: the server refuses to let the maker be the
      // checker, and stamps the approver from the approvals inbox (#70).
      const { data } = await api.post("/outbound/writeoffs", {
        store: Number(storeId),
        reason,
        lines: payloadLines,
      });
      navigate(`/stock-count/writeoffs/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/stock-count/writeoffs" className="btn" style={{ marginBottom: 16 }} data-testid="wo-back-link">
        <ArrowLeft size={15} /> Write-offs
      </Link>
      <h1 className="h1 h2-rust" style={{ marginBottom: 18 }}>New Write-off</h1>

      <div className="card section-card">
        <p className="eyebrow">Step 1 · Write-off details</p>
        <div className="form-row" style={{ marginTop: 10 }}>
          <div className="field">
            <label>Store</label>
            {storeLocked && lockedStore ? (
              <div className="store-lock" data-testid="wo-store-locked">{lockedStore.code} · {lockedStore.name}</div>
            ) : (
              <select className="select" value={storeId} onChange={(e) => setStoreId(e.target.value)} data-testid="wo-store-select">
                <option value="">Select store…</option>
                {stores.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
              </select>
            )}
          </div>
          <div className="field">
            <label>Reason</label>
            <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Dead stock clearance, refused defectives" data-testid="wo-reason" />
          </div>
          <div className="field">
            <label>Approval</label>
            <div className="store-lock" data-testid="wo-approved-by">
              Goes to Action Needed — someone other than you must approve it
              before it can post.
            </div>
          </div>
        </div>
      </div>

      <div className="card section-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div>
            <p className="eyebrow">Step 2 · Items to write off</p>
            <h3 className="h3">Write-off lines</h3>
          </div>
          <button type="button" className="btn" onClick={() => setLines((l) => [...l, emptyLine()])} data-testid="add-wo-line">
            <Plus size={15} /> Add line
          </button>
        </div>
        <table className="lines-table" data-testid="wo-lines">
          <thead>
            <tr>
              <th style={{ width: "24%" }}>SKU code</th>
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
                <td><input value={l.sku_code} onChange={(e) => setLine(i, "sku_code", e.target.value)} data-testid={`wo-sku-${i}`} /></td>
                <td><input value={l.design} onChange={(e) => setLine(i, "design", e.target.value)} data-testid={`wo-design-${i}`} /></td>
                <td><input value={l.size} onChange={(e) => setLine(i, "size", e.target.value)} data-testid={`wo-size-${i}`} /></td>
                <td><input value={l.color} onChange={(e) => setLine(i, "color", e.target.value)} data-testid={`wo-color-${i}`} /></td>
                <td><input className="num" value={l.qty} onChange={(e) => setLine(i, "qty", e.target.value)} data-testid={`wo-qty-${i}`} /></td>
                <td><button type="button" className="line-del" onClick={() => setLines((ls) => ls.filter((_, idx) => idx !== i))} data-testid={`delete-wo-line-${i}`}><Trash2 size={15} /></button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="wo-create-error">{error}</div>}
      <button className="btn btn-primary btn-lg" disabled={saving} onClick={save} data-testid="save-wo-btn">
        <FileX2 size={16} /> {saving ? "Saving…" : "Create write-off (draft)"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

export function WriteOffDetailPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const { data: w, loading } = useDoc<WOT>(`/outbound/writeoffs/${id}`);
  const writable = canWriteStockCount(user);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  if (loading || !w) return <div className="page-pad"><p className="lead">Loading…</p></div>;

  const totalQty = w.lines.reduce((s, l) => s + l.qty, 0);
  const totalValue = w.lines.reduce((s, l) => s + l.qty * l.unit_cost_paise, 0);
  // A draft cannot post until a second person has approved it — the server
  // enforces this; the button only reflects it honestly.
  const approved = isCleared(w.approval);
  const canSubmit = w.docstatus === 0 && writable && approved;

  async function handleSubmit() {
    setError("");
    setSubmitting(true);
    try {
      await api.post(`/outbound/writeoffs/${w!.id}/submit`);
      window.location.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/stock-count/writeoffs" className="btn" style={{ marginBottom: 16 }} data-testid="wo-detail-back">
        <ArrowLeft size={15} /> Write-offs
      </Link>
      <div className="toolbar">
        <div>
          <p className="eyebrow">{w.doc_number || `Draft #${w.id}`}</p>
          <h1 className="h1">Write-off — {w.store_code}</h1>
          <p className="lead">{w.store_name} · {w.reason || "No reason"}</p>
        </div>
        <div className="spacer" />
        <DocPill ds={w.docstatus} />
        {w.docstatus === 0 && <ApprovalPill status={w.approval?.status ?? "pending"} />}
        {canSubmit && (
          <button
            type="button"
            className="btn btn-cta"
            disabled={submitting}
            onClick={handleSubmit}
            data-testid="submit-wo-btn"
          >
            <Send size={15} /> {submitting ? "Submitting…" : "Submit write-off"}
          </button>
        )}
      </div>

      <div className="form-row" style={{ marginBottom: 18 }}>
        <div className="card section-card">
          <p className="eyebrow">Total qty</p>
          <h3 className="h3">{totalQty} pcs</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Est. value</p>
          <h3 className="h3">{totalValue ? <Money paise={totalValue} /> : "—"}</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Lines</p>
          <h3 className="h3">{w.lines.length}</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Date</p>
          <h3 className="h3">{fmtDate(w.created_at)}</h3>
        </div>
      </div>

      <div style={{ marginBottom: 18 }}>
        <ApprovalTrail
          createdByName={w.created_by_name}
          createdAt={w.created_at}
          approval={w.approval}
          history={w.approval_history}
          askAgainPath={writable ? `/outbound/writeoffs/${w.id}/request-approval` : undefined}
        />
      </div>

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="wo-detail-error">{error}</div>}

      <div className="table-wrap">
        <table className="data" data-testid="wo-detail-lines">
          <thead>
            <tr>
              <th>SKU</th>
              <th>Design</th>
              <th>Size</th>
              <th>Colour</th>
              <th>Brand</th>
              <th className="num">Qty</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {w.lines.map((l) => (
              <tr key={l.id}>
                <td><b className="mono">{l.sku_code}</b></td>
                <td>{l.design || "—"}</td>
                <td>{l.size || "—"}</td>
                <td>{l.color || "—"}</td>
                <td>{l.brand || "—"}</td>
                <td className="num">{l.qty}</td>
                <td className="num">{l.unit_cost_paise ? <Money paise={l.unit_cost_paise} /> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
