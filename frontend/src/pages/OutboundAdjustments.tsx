import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  ClipboardCheck,
  Send,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useDoc, useList } from "../lib/hooks";
import { Money } from "../lib/format";
import { canWriteStockCount } from "../lib/outbound-rbac";
import { adjustmentReasonLabel } from "../lib/adjustment-reasons";
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

interface AdjLineT {
  id: number;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  season: string;
  item: string;
  hsn: string;
  book_qty: number;
  counted_qty: number;
  adj_qty: number;
  unit_cost_paise: number;
}

interface AdjT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  store: number;
  store_code: string;
  store_name: string;
  reason: string;
  approved_by: number | null;
  approved_by_name: string;
  approval: ApprovalT | null;
  approval_history: ApprovalT[];
  notes: string;
  created_by: number | null;
  created_by_name: string;
  created_at: string;
  updated_at: string;
  lines: AdjLineT[];
}


// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export function AdjustmentListPage() {
  const { user } = useAuth();
  const [q, setQ] = useState("");
  const { data, loading } = useList<AdjT>("/outbound/adjustments", { q });
  const writable = canWriteStockCount(user);

  return (
    <div className="page-pad">
      <PageHeader
        actions={
          /* An adjustment is what a count produces, not something typed. The
             book-against-counted form this screen used to carry is gone (#76):
             the variance is computed server-side from a count session. */
          writable && (
            <Link className="btn btn-cta" to="/stock-count" data-testid="count-stock-btn">
              <ClipboardCheck size={16} /> Count stock
            </Link>
          )
        }
      />

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search adjustments — doc number, reason, store"
        label="Search adjustments"
        testId="adjustment-search"
        noun="adjustment"
        count={data.length}
        loading={loading}
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : data.length === 0 ? (
        <div className="card section-card" data-testid="adjustment-empty">
          {q
            ? `No adjustment matches “${q}”.`
            : "No stock adjustments yet. Create one after a physical count."}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="adjustment-table">
            <thead>
              <tr>
                <th>Doc #</th>
                <th>Store</th>
                <th>Reason</th>
                <th className="num">Lines</th>
                <th className="num">Net variance</th>
                <th>Status</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {data.map((a) => {
                const netAdj = a.lines.reduce((s, l) => s + l.adj_qty, 0);
                return (
                  <tr key={a.id} data-testid={`adj-row-${a.id}`}>
                    <td>
                      <Link to={`/stock-count/adjustments/${a.id}`} className="link-cell mono" data-testid={`adj-link-${a.id}`}>
                        <b>{a.doc_number || `Draft #${a.id}`}</b>
                      </Link>
                    </td>
                    <td><b className="mono">{a.store_code}</b></td>
                    <td>{adjustmentReasonLabel(a.reason)}</td>
                    <td className="num">{a.lines.length}</td>
                    <td className="num">
                      <span style={{ color: netAdj < 0 ? "var(--red)" : netAdj > 0 ? "var(--green)" : undefined }}>
                        {netAdj > 0 ? "+" : ""}{netAdj}
                      </span>
                    </td>
                    <td><DocPill ds={a.docstatus} /></td>
                    <td>{fmtDate(a.created_at)}</td>
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
// Detail
// ---------------------------------------------------------------------------

export function AdjustmentDetailPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const { data: a, loading } = useDoc<AdjT>(`/outbound/adjustments/${id}`);
  const writable = canWriteStockCount(user);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  if (loading || !a) return <div className="page-pad"><p className="lead">Loading…</p></div>;

  const netAdj = a.lines.reduce((s, l) => s + l.adj_qty, 0);
  // An adjustment cannot post until a second person has approved it (#70).
  const canSubmit = a.docstatus === 0 && writable && isCleared(a.approval);

  async function handleSubmit() {
    setError("");
    setSubmitting(true);
    try {
      await api.post(`/outbound/adjustments/${a!.id}/submit`);
      window.location.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/stock-count/adjustments" className="btn" style={{ marginBottom: 16 }} data-testid="adj-detail-back">
        <ArrowLeft size={15} /> Adjustments
      </Link>
      <div className="toolbar">
        <div>
          <p className="eyebrow">{a.doc_number || `Draft #${a.id}`}</p>
          <h1 className="h1">Stock Adjustment — {a.store_code}</h1>
          <p className="lead">
            {a.store_name} · {adjustmentReasonLabel(a.reason)}
            {a.notes ? ` · ${a.notes}` : ""}
          </p>
        </div>
        <div className="spacer" />
        <DocPill ds={a.docstatus} />
        {a.docstatus === 0 && <ApprovalPill status={a.approval?.status ?? "pending"} />}
        {canSubmit && (
          <button
            type="button"
            className="btn btn-cta"
            disabled={submitting}
            onClick={handleSubmit}
            data-testid="submit-adj-btn"
          >
            <Send size={15} /> {submitting ? "Submitting…" : "Submit adjustment"}
          </button>
        )}
      </div>

      <div className="form-row" style={{ marginBottom: 18 }}>
        <div className="card section-card">
          <p className="eyebrow">Reason</p>
          <h3 className="h3">{adjustmentReasonLabel(a.reason)}</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Net variance</p>
          <h3 className="h3" style={{ color: netAdj < 0 ? "var(--red)" : netAdj > 0 ? "var(--green)" : undefined }}>
            {netAdj > 0 ? "+" : ""}{netAdj} pcs
          </h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Lines</p>
          <h3 className="h3">{a.lines.length}</h3>
        </div>
        <div className="card section-card">
          <p className="eyebrow">Date</p>
          <h3 className="h3">{fmtDate(a.created_at)}</h3>
        </div>
      </div>

      <div style={{ marginBottom: 18 }}>
        <ApprovalTrail
          createdByName={a.created_by_name}
          createdAt={a.created_at}
          approval={a.approval}
          history={a.approval_history}
          askAgainPath={writable ? `/outbound/adjustments/${a.id}/request-approval` : undefined}
        />
      </div>

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="adj-detail-error">{error}</div>}

      <div className="table-wrap">
        <table className="data" data-testid="adj-detail-lines">
          <thead>
            <tr>
              <th>SKU</th>
              <th>Design</th>
              <th>Size</th>
              <th>Colour</th>
              <th>Brand</th>
              <th className="num">Book</th>
              <th className="num">Counted</th>
              <th className="num">Adj</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {a.lines.map((l) => (
              <tr key={l.id}>
                <td><b className="mono">{l.sku_code}</b></td>
                <td>{l.design || "—"}</td>
                <td>{l.size || "—"}</td>
                <td>{l.color || "—"}</td>
                <td>{l.brand || "—"}</td>
                <td className="num">{l.book_qty}</td>
                <td className="num">{l.counted_qty}</td>
                <td className="num" style={{ fontWeight: 700, color: l.adj_qty < 0 ? "var(--red)" : l.adj_qty > 0 ? "var(--green)" : undefined }}>
                  {l.adj_qty > 0 ? "+" : ""}{l.adj_qty}
                </td>
                <td className="num">{l.unit_cost_paise ? <Money paise={l.unit_cost_paise} /> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
