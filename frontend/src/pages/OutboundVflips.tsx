import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useDoc, useList } from "../lib/hooks";
import { Money } from "../lib/format";
import { canFlipOwnership } from "../lib/outbound-rbac";
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

interface VFlipLineT {
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

interface VFlipT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  store: number;
  store_code: string;
  store_name: string;
  original_brand: number;
  original_brand_name: string;
  season: string;
  authorized_by: number | null;
  approved_by_name: string;
  created_by: number | null;
  created_by_name: string;
  approval: ApprovalT | null;
  approval_history: ApprovalT[];
  created_at: string;
  updated_at: string;
  lines: VFlipLineT[];
}

interface StoreT { id: number; code: string; name: string; store_type: string; }
interface BrandT { id: number; name: string; }

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export function VFlipListPage() {
  const { user } = useAuth();
  const [q, setQ] = useState("");
  const { data, loading } = useList<VFlipT>("/outbound/vflips", { q });
  const writable = canFlipOwnership(user);

  return (
    <div className="page-pad">
      <PageHeader
        lead="An ownership correction: SOR ⇄ owned, without moving a piece."
        actions={
          writable && (
            <Link className="btn btn-cta" to="/stock/vflips/new" data-testid="new-vflip-btn">
              <Plus size={16} /> New V-flip
            </Link>
          )
        }
      />

      <div className="card section-card" style={{ marginBottom: 18, padding: "14px 18px" }} data-testid="vflip-info-banner">
        <p className="lead" style={{ margin: 0 }}>
          <RefreshCw size={14} style={{ verticalAlign: "middle", marginRight: 6, color: "var(--rust)" }} />
          V-flip converts brand-owned SOR/consignment stock to KDPS-owned. Physical stock stays on shelf; brand display prefixed with "V ".
        </p>
      </div>

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search V-flips — doc number, brand, store"
        label="Search V-flips"
        testId="vflip-search"
        noun="V-flip"
        count={data.length}
        loading={loading}
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : data.length === 0 ? (
        <div className="card section-card" data-testid="vflip-empty">
          {q
            ? `No V-flip matches “${q}”.`
            : "No V-flips yet. Create one to convert brand-owned stock to KDPS-owned."}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="vflip-table">
            <thead>
              <tr>
                <th>Doc #</th>
                <th>Store</th>
                <th>Original brand</th>
                <th>Season</th>
                <th className="num">Lines</th>
                <th className="num">Total qty</th>
                <th>Status</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {data.map((v) => {
                const totalQty = v.lines.reduce((s, l) => s + l.qty, 0);
                return (
                  <tr key={v.id} data-testid={`vflip-row-${v.id}`}>
                    <td>
                      <Link to={`/stock/vflips/${v.id}`} className="link-cell mono" data-testid={`vflip-link-${v.id}`}>
                        <b>{v.doc_number || `Draft #${v.id}`}</b>
                      </Link>
                    </td>
                    <td><b className="mono">{v.store_code}</b></td>
                    <td>{v.original_brand_name}</td>
                    <td>{v.season || "—"}</td>
                    <td className="num">{v.lines.length}</td>
                    <td className="num">{totalQty}</td>
                    <td><DocPill ds={v.docstatus} /></td>
                    <td>{fmtDate(v.created_at)}</td>
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

interface DraftVFlipLine {
  sku_code: string;
  design: string;
  size: string;
  color: string;
  qty: number | string;
}
const emptyLine = (): DraftVFlipLine => ({
  sku_code: "", design: "", size: "", color: "", qty: "",
});

export function VFlipNewPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: stores } = useList<StoreT>("/masters/stores");
  const { data: brands } = useList<BrandT>("/masters/brands");

  const storeLocked = user?.scope_type === "store" && (user?.stores?.length ?? 0) >= 1;
  const lockedStore = storeLocked ? user!.stores[0] : null;

  const [storeId, setStoreId] = useState<string>(lockedStore ? String(lockedStore.id) : "");
  const [brandId, setBrandId] = useState("");
  const [season, setSeason] = useState("");
  const [lines, setLines] = useState<DraftVFlipLine[]>([emptyLine()]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  function setLine(i: number, key: keyof DraftVFlipLine, val: string) {
    setLines((ls) => ls.map((l, idx) => (idx === i ? { ...l, [key]: val } : l)));
  }

  async function save() {
    setError("");
    if (!storeId) { setError("Select a store."); return; }
    if (!brandId) { setError("Select the original brand."); return; }
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
      // No authoriser in the payload: the server refuses to let the maker be
      // the checker, and stamps it from the approvals inbox (#70).
      const { data } = await api.post("/outbound/vflips", {
        store: Number(storeId),
        original_brand: Number(brandId),
        season,
        lines: payloadLines,
      });
      navigate(`/stock/vflips/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/stock/vflips" className="btn" style={{ marginBottom: 16 }} data-testid="vflip-back-link">
        <ArrowLeft size={15} /> V-Flips
      </Link>
      <h1 className="h1 h2-rust" style={{ marginBottom: 18 }}>New V-Flip</h1>

      <div className="card section-card">
        <p className="eyebrow">Step 1 · Flip details</p>
        <div className="form-row" style={{ marginTop: 10 }}>
          <div className="field">
            <label>Store</label>
            {storeLocked && lockedStore ? (
              <div className="store-lock" data-testid="vflip-store-locked">{lockedStore.code} · {lockedStore.name}</div>
            ) : (
              <select className="select" value={storeId} onChange={(e) => setStoreId(e.target.value)} data-testid="vflip-store-select">
                <option value="">Select store…</option>
                {stores.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
              </select>
            )}
          </div>
          <div className="field">
            <label>Original brand (SOR/consignment)</label>
            <select className="select" value={brandId} onChange={(e) => setBrandId(e.target.value)} data-testid="vflip-brand-select">
              <option value="">Select brand…</option>
              {brands.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Season</label>
            <input className="input" value={season} onChange={(e) => setSeason(e.target.value)} placeholder="e.g. AW24" data-testid="vflip-season" />
          </div>
          <div className="field">
            <label>Authorized by</label>
            <div className="store-lock" data-testid="vflip-authorized-by">
              {user?.full_name || user?.username} (you)
            </div>
          </div>
        </div>
      </div>

      <div className="card section-card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div>
            <p className="eyebrow">Step 2 · Items to flip</p>
            <h3 className="h3">V-flip lines</h3>
          </div>
          <button type="button" className="btn" onClick={() => setLines((l) => [...l, emptyLine()])} data-testid="add-vflip-line">
            <Plus size={15} /> Add line
          </button>
        </div>
        <table className="lines-table" data-testid="vflip-lines">
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
                <td><input value={l.sku_code} onChange={(e) => setLine(i, "sku_code", e.target.value)} data-testid={`vf-sku-${i}`} /></td>
                <td><input value={l.design} onChange={(e) => setLine(i, "design", e.target.value)} data-testid={`vf-design-${i}`} /></td>
                <td><input value={l.size} onChange={(e) => setLine(i, "size", e.target.value)} data-testid={`vf-size-${i}`} /></td>
                <td><input value={l.color} onChange={(e) => setLine(i, "color", e.target.value)} data-testid={`vf-color-${i}`} /></td>
                <td><input className="num" value={l.qty} onChange={(e) => setLine(i, "qty", e.target.value)} data-testid={`vf-qty-${i}`} /></td>
                <td><button type="button" className="line-del" onClick={() => setLines((ls) => ls.filter((_, idx) => idx !== i))} data-testid={`delete-vf-line-${i}`}><Trash2 size={15} /></button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="vflip-create-error">{error}</div>}
      <button className="btn btn-primary btn-lg" disabled={saving} onClick={save} data-testid="save-vflip-btn">
        <RefreshCw size={16} /> {saving ? "Saving…" : "Create V-flip (draft)"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

export function VFlipDetailPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const { data: v, loading } = useDoc<VFlipT>(`/outbound/vflips/${id}`);
  const writable = canFlipOwnership(user);
  const [submitting, setSubmitting] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [error, setError] = useState("");

  if (loading || !v) return <div className="page-pad"><p className="lead">Loading…</p></div>;

  const totalQty = v.lines.reduce((s, l) => s + l.qty, 0);
  const totalValue = v.lines.reduce((s, l) => s + l.qty * l.unit_cost_paise, 0);
  // A V-flip cannot post until a second person has approved it (#70).
  const canSubmit = v.docstatus === 0 && writable && isCleared(v.approval);

  async function handleSubmit() {
    setError("");
    setSubmitting(true);
    try {
      await api.post(`/outbound/vflips/${v!.id}/submit`);
      window.location.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSubmitting(false);
      setShowConfirm(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/stock/vflips" className="btn" style={{ marginBottom: 16 }} data-testid="vflip-detail-back">
        <ArrowLeft size={15} /> V-Flips
      </Link>
      <div className="toolbar">
        <div>
          <p className="eyebrow">{v.doc_number || `Draft #${v.id}`}</p>
          <h1 className="h1">V-Flip — {v.store_code}</h1>
          <p className="lead">
            {v.store_name} · {v.original_brand_name}
            {v.season ? ` · ${v.season}` : ""}
          </p>
        </div>
        <div className="spacer" />
        <DocPill ds={v.docstatus} />
        {v.docstatus === 0 && <ApprovalPill status={v.approval?.status ?? "pending"} />}
        <span className="chip chip-purple">Ownership flip</span>
        {canSubmit && (
          <button
            type="button"
            className="btn btn-cta"
            onClick={() => setShowConfirm(true)}
            data-testid="flip-action-btn"
          >
            <RefreshCw size={15} /> Flip ownership
          </button>
        )}
      </div>

      {/* Confirm dialog */}
      {showConfirm && (
        <div className="modal-backdrop" data-testid="vflip-confirm-modal">
          <div className="modal">
            <div className="modal-head">
              <h3 className="h3">Confirm V-flip</h3>
              <button type="button" className="btn" onClick={() => setShowConfirm(false)}>Cancel</button>
            </div>
            <p className="lead" style={{ marginBottom: 14 }}>
              This will permanently convert <b>{totalQty} pcs</b> of <b>{v.original_brand_name}</b> stock
              at <b>{v.store_code}</b> from brand-owned (SOR/consignment) to KDPS-owned.
              The physical stock stays on the shelf. The brand display will be prefixed with "V ".
            </p>
            <p className="lead" style={{ marginBottom: 14 }}>
              Settlement claim tracking will be handled in Sprint 8 (Payments). This action <b>cannot be undone</b>.
            </p>
            <button
              className="btn btn-cta btn-lg"
              disabled={submitting}
              onClick={handleSubmit}
              data-testid="confirm-flip-btn"
            >
              <RefreshCw size={16} /> {submitting ? "Flipping…" : "Confirm flip"}
            </button>
          </div>
        </div>
      )}

      <div className="form-row" style={{ marginBottom: 18 }}>
        <div className="card section-card">
          <p className="eyebrow">Original brand</p>
          <h3 className="h3">{v.original_brand_name}</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Total qty</p>
          <h3 className="h3">{totalQty} pcs</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Est. value</p>
          <h3 className="h3">{totalValue ? <Money paise={totalValue} /> : "—"}</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Date</p>
          <h3 className="h3">{fmtDate(v.created_at)}</h3>
        </div>
      </div>

      <div style={{ marginBottom: 18 }}>
        <ApprovalTrail
          createdByName={v.created_by_name}
          createdAt={v.created_at}
          approval={v.approval}
          history={v.approval_history}
          askAgainPath={writable ? `/outbound/vflips/${v.id}/request-approval` : undefined}
        />
      </div>

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="vflip-detail-error">{error}</div>}

      <div className="table-wrap">
        <table className="data" data-testid="vflip-detail-lines">
          <thead>
            <tr>
              <th>SKU</th>
              <th>Design</th>
              <th>Size</th>
              <th>Colour</th>
              <th>Brand</th>
              <th>Season</th>
              <th className="num">Qty</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {v.lines.map((l) => (
              <tr key={l.id}>
                <td><b className="mono">{l.sku_code}</b></td>
                <td>{l.design || "—"}</td>
                <td>{l.size || "—"}</td>
                <td>{l.color || "—"}</td>
                <td>{l.brand || "—"}</td>
                <td>{l.season || "—"}</td>
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
