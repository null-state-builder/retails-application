/**
 * Return to Brand (#75).
 *
 * The screen does not decide what may go back. Pick the brand and the server
 * answers with the returnable pool — confirmed-damaged pieces, and for the
 * models that take stock back, season-end stock still inside its window. The
 * return is then built by scanning against that pool, so a piece that is not
 * the brand's to take beeps wrong at the scanner and is refused again at the
 * server. No price is ever sent: the unit cost is read off the books (#103).
 *
 * For Correction brands the negotiated allowance is on screen while the return
 * is being built — filling, warning, or crossed — because a cap nobody can see
 * until after they have packed the carton is not a cap.
 */

import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  CalendarClock,
  PackageMinus,
  Plus,
  RotateCcw,
  ScanLine,
  Send,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useDoc, useList } from "../lib/hooks";
import { Money } from "../lib/format";
import { canCreateReturnToBrand, canWriteReturnToBrand } from "../lib/outbound-rbac";
import { ScanScreen, type ScanResult, type ScanTarget } from "../components/ScanScreen";
import { ApprovalPill, ApprovalTrail, isCleared, type ApprovalT } from "../components/approval";
import { ListSearchBar } from "../components/SearchBox";
import "./Booking.css";
import { PageHeader } from "../components/PageHeader";

// ---------------------------------------------------------------------------
// Helpers & controlled vocab
// ---------------------------------------------------------------------------

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

const DS_TONE: Record<number, string> = { 0: "grey", 1: "green", 2: "red" };
const DS_LABEL: Record<number, string> = { 0: "Draft", 1: "Submitted", 2: "Cancelled" };
function DocPill({ ds }: { ds: number }) {
  return <span className={`chip chip-${DS_TONE[ds] ?? "grey"} status-pill`}>{DS_LABEL[ds] ?? ds}</span>;
}

const RETURN_TYPE_LABEL: Record<string, string> = {
  defective: "Defective / GR return",
  seasonal: "Season-end return",
};

/** The three ways stock physically gets back to the brand. The consolidated one
 *  rides a store-to-warehouse transfer first, so it has to name it. */
const LOGISTICS_OPTIONS = [
  { value: "store_pickup", label: "Brand collects from store" },
  { value: "store_dispatch", label: "Store sends to brand" },
  { value: "warehouse", label: "Consolidated at warehouse" },
];

function routeLabel(value: string): string {
  return LOGISTICS_OPTIONS.find((o) => o.value === value)?.label || value || "—";
}

/** The window countdown, in the words a warehouse person would use. */
function windowText(days: number | null): string {
  if (days === null) return "No deadline";
  if (days < 0) return `${Math.abs(days)} days past the window`;
  if (days === 0) return "Last day of the window";
  return `${days} days left`;
}

function windowTone(days: number | null): string {
  if (days === null) return "grey";
  if (days < 0) return "red";
  if (days <= 14) return "amber";
  return "green";
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface RTVLineT {
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
  source: string;
}

interface CreditNoteT {
  id: number;
  received_on: string;
  reference: string;
  recorded_by_name: string;
}

interface RTVT {
  id: number;
  doc_number: string | null;
  docstatus: number;
  store: number;
  store_code: string;
  store_name: string;
  vendor: number;
  brand: number | null;
  brand_name: string;
  commercial_label: string;
  return_type: string;
  logistics_route: string;
  logistics_route_label: string;
  via_transfer: number | null;
  via_transfer_number: string;
  season: string;
  return_window_date: string | null;
  days_to_window: number | null;
  value_paise: number;
  cap_percent: string;
  cap_allowance_paise: number;
  cap_used_before_paise: number;
  cap_exceeded_by_paise: number;
  credit_note: CreditNoteT | null;
  notes: string;
  created_by: number | null;
  created_by_name: string;
  approved_by: number | null;
  approved_by_name: string;
  approval: ApprovalT | null;
  approval_history: ApprovalT[];
  created_at: string;
  updated_at: string;
  lines: RTVLineT[];
}

interface PoolRowT {
  source: string;
  store: number;
  store_code: string;
  store_name: string;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  season: string;
  qty: number;
  unit_cost_paise: number;
  value_paise: number;
  arrived_on: string | null;
  window_date: string | null;
  days_left: number | null;
  /** Past its window. Sent so the screen can say so; never scannable. */
  expired: boolean;
}

interface CapT {
  applies: boolean;
  percent: string;
  delivered_paise: number;
  allowance_paise: number;
  used_paise: number;
  remaining_paise: number;
  /** Used-value at which to start warning. The server owns the threshold; this
   *  screen must never carry its own copy of the percentage (Rule 12). */
  warn_at_paise: number;
  this_return_paise: number;
  exceeded_by_paise: number;
  warn: boolean;
}

interface PoolT {
  brand: {
    id: number;
    name: string;
    commercial_label: string;
    takes_returns: boolean;
    return_window_days: number;
  };
  excluded_reason: string;
  cap: CapT;
  rows: PoolRowT[];
}

interface StoreT { id: number; code: string; name: string; store_type: string; }
interface VendorT { id: number; name: string; }
interface BrandT { id: number; name: string; }
interface TransferT { id: number; doc_number: string | null; destination_store_code: string }

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export function RTVListPage() {
  const [params, setParams] = useSearchParams();
  const { user } = useAuth();
  const tab = params.get("type") === "seasonal" ? "seasonal" : "defective";
  const [q, setQ] = useState("");
  const { data, loading } = useList<RTVT>("/outbound/rtvs", { return_type: tab, q });
  // Reaching this section is not the same as being allowed to send stock back:
  // a store person opens the list and marks damage, and the warehouse is the
  // one who raises the return (#75).
  const mayCreate = canCreateReturnToBrand(user);

  function setTab(next: string) {
    setParams((p) => { p.set("type", next); return p; });
  }

  return (
    <div className="page-pad">
      <PageHeader
        actions={
          mayCreate && (
            <Link className="btn btn-cta" to="/return-to-brand/new" data-testid="new-rtv-btn">
              <Plus size={16} /> New return
            </Link>
          )
        }
      />

      <div className="mode-toggle" data-testid="rtv-type-toggle" style={{ maxWidth: 520, marginBottom: 18 }}>
        <button
          type="button"
          className={`mode-btn ${tab === "defective" ? "active" : ""}`}
          onClick={() => setTab("defective")}
          data-testid="rtv-tab-defective"
        >
          <PackageMinus size={16} /> Defective / GR
        </button>
        <button
          type="button"
          className={`mode-btn ${tab === "seasonal" ? "active" : ""}`}
          onClick={() => setTab("seasonal")}
          data-testid="rtv-tab-seasonal"
        >
          <RotateCcw size={16} /> Season-end
        </button>
      </div>

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search returns — doc number, brand"
        label="Search returns to brand"
        testId="rtv-search"
        noun="return"
        count={data.length}
        loading={loading}
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : data.length === 0 ? (
        <div className="card section-card" data-testid="rtv-empty">
          {q
            ? `No ${RETURN_TYPE_LABEL[tab]?.toLowerCase() || tab} matches “${q}”.`
            : `No ${RETURN_TYPE_LABEL[tab]?.toLowerCase() || tab} returns yet.`}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="rtv-table">
            <thead>
              <tr>
                <th>Doc #</th>
                <th>Brand</th>
                <th>Store</th>
                <th>Logistics</th>
                <th className="num">Pcs</th>
                <th className="num">Value</th>
                <th>Window</th>
                <th>Credit note</th>
                <th>Status</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.id} data-testid={`rtv-row-${r.id}`}>
                  <td>
                    <Link to={`/return-to-brand/${r.id}`} className="link-cell mono" data-testid={`rtv-link-${r.id}`}>
                      <b>{r.doc_number || `Draft #${r.id}`}</b>
                    </Link>
                  </td>
                  <td>
                    {r.brand_name || "—"}
                    {r.commercial_label && (
                      <span className="chip chip-grey" style={{ marginLeft: 6 }}>{r.commercial_label}</span>
                    )}
                  </td>
                  <td><b className="mono">{r.store_code}</b></td>
                  <td>{r.logistics_route_label || routeLabel(r.logistics_route)}</td>
                  <td className="num">{r.lines.reduce((s, l) => s + l.qty, 0)}</td>
                  <td className="num">{r.value_paise ? <Money paise={r.value_paise} /> : "—"}</td>
                  <td>
                    {r.return_window_date ? (
                      <span className={`chip chip-${windowTone(r.days_to_window)}`}>
                        {windowText(r.days_to_window)}
                      </span>
                    ) : "—"}
                  </td>
                  <td>
                    {r.credit_note ? (
                      <span className="chip chip-green">Received {fmtDate(r.credit_note.received_on)}</span>
                    ) : (
                      <span className="chip chip-grey">Pending</span>
                    )}
                  </td>
                  <td><DocPill ds={r.docstatus} /></td>
                  <td>{fmtDate(r.created_at)}</td>
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
// The cap meter — the same numbers the server snapshots onto the return
// ---------------------------------------------------------------------------

function CapMeter({ cap, thisReturnPaise }: { cap: CapT; thisReturnPaise: number }) {
  const used = cap.used_paise + thisReturnPaise;
  const pct = cap.allowance_paise > 0 ? Math.min(100, Math.round((used / cap.allowance_paise) * 100)) : 100;
  const over = cap.allowance_paise > 0 && used > cap.allowance_paise;
  // The threshold is the server's (`CAP_WARN_AT`), sent down as a value rather
  // than a percentage — recomputing it here would be a second copy that a
  // retune on the server would silently leave behind.
  const warn = !over && cap.allowance_paise > 0 && used >= cap.warn_at_paise;
  const tone = over ? "red" : warn ? "amber" : "green";

  return (
    <div className="card section-card" data-testid="rtv-cap-meter">
      <p className="eyebrow">Allowed return — {cap.percent}% of what this brand delivered</p>
      <h3 className="h3">
        <Money paise={used} /> of <Money paise={cap.allowance_paise} />
      </h3>
      <div
        style={{
          height: 10,
          borderRadius: 6,
          background: "var(--line)",
          overflow: "hidden",
          margin: "10px 0 8px",
        }}
      >
        <div
          data-testid="rtv-cap-bar"
          style={{ width: `${pct}%`, height: "100%", background: `var(--chip-${tone}-fg, currentColor)` }}
        />
      </div>
      <p className="lead" style={{ fontSize: 13 }}>
        Already returned <Money paise={cap.used_paise} /> · this return{" "}
        <Money paise={thisReturnPaise} /> · delivered <Money paise={cap.delivered_paise} />
      </p>
      {over ? (
        <p className="lead" data-testid="rtv-cap-exceeded">
          <span className="chip chip-red">
            <AlertTriangle size={13} /> Over the allowance by <Money paise={used - cap.allowance_paise} />
          </span>{" "}
          Flagged for the approver — the return is not blocked.
        </p>
      ) : warn ? (
        <p className="lead" data-testid="rtv-cap-warning">
          <span className="chip chip-amber">Close to the allowance</span>{" "}
          <Money paise={cap.allowance_paise - used} /> left this season.
        </p>
      ) : (
        <p className="lead" data-testid="rtv-cap-room">
          <Money paise={Math.max(0, cap.allowance_paise - used)} /> of room left.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create — pool first, then scan
// ---------------------------------------------------------------------------

export function RTVNewPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: stores } = useList<StoreT>("/masters/stores");
  const { data: vendors } = useList<VendorT>("/vendors");
  const { data: brands } = useList<BrandT>("/masters/brands");

  const storeLocked = user?.scope_type === "store" && (user?.stores?.length ?? 0) >= 1;
  const lockedStore = storeLocked ? user!.stores[0] : null;

  const [storeId, setStoreId] = useState<string>(lockedStore ? String(lockedStore.id) : "");
  const [vendorId, setVendorId] = useState("");
  const [brandId, setBrandId] = useState("");
  const [returnType, setReturnType] = useState("defective");
  const [logisticsRoute, setLogisticsRoute] = useState("");
  const [viaTransferId, setViaTransferId] = useState("");
  const [notes, setNotes] = useState("");
  const [scans, setScans] = useState<Record<string, number>>({});
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  // The pool is the brand's answer, refetched whenever the brand or the store
  // changes. Asked only once both are known — "everything, everywhere" is not a
  // question this endpoint takes.
  const { data: pool, loading: poolLoading } = useDoc<PoolT>(
    brandId ? `/outbound/returnable-pool?brand=${brandId}${storeId ? `&store=${storeId}` : ""}` : null,
  );
  const { data: transfers } = useList<TransferT>(
    logisticsRoute === "warehouse" && storeId ? "/outbound/transfers" : null,
  );

  // The return type narrows the pool before a barcode is matched — a defect
  // claim reaches quarantine, a season-end sweep reaches the shelf.
  const wanted = returnType === "defective" ? "quarantine" : "season_end";
  const rows = useMemo(
    () => (pool?.rows ?? []).filter((r) => r.source === wanted && !r.expired),
    [pool, wanted],
  );
  // Shown as a count rather than dropped in silence: the window starts at the
  // barcode's first arrival here, so a piece delivered again this season can be
  // past a deadline set last season, and "it is simply not in the list" reads as
  // missing stock instead of a closed window.
  const expiredCount = useMemo(
    () => (pool?.rows ?? []).filter((r) => r.source === wanted && r.expired).length,
    [pool, wanted],
  );

  const targets: ScanTarget[] = useMemo(
    () =>
      rows.map((r) => ({
        barcode: r.sku_code,
        label: [r.design, r.color, r.size].filter(Boolean).join(" · ") || r.sku_code,
        expected: null,
        available: r.qty,
      })),
    [rows],
  );

  const scannedValue = useMemo(
    () =>
      Object.entries(scans).reduce((sum, [barcode, qty]) => {
        const row = rows.find((r) => r.sku_code === barcode);
        return sum + (row ? row.unit_cost_paise * qty : 0);
      }, 0),
    [scans, rows],
  );
  const scannedPieces = Object.values(scans).reduce((a, b) => a + b, 0);

  function onScanned(result: ScanResult) {
    setScans(result.scans);
    setScanning(false);
  }

  async function save() {
    setError("");
    if (!storeId) { setError("Select a store."); return; }
    if (!vendorId) { setError("Select the vendor this brand is billed through."); return; }
    if (!brandId) { setError("Select a brand — the pool is a brand's stock."); return; }
    if (!logisticsRoute) { setError("Say how the stock gets back to the brand."); return; }
    if (logisticsRoute === "warehouse" && !viaTransferId) {
      setError("A consolidated return has to name the transfer that brought the pieces in.");
      return;
    }
    if (!scannedPieces) { setError("Scan the pieces going back."); return; }
    setSaving(true);
    try {
      // Barcodes and quantities only. What each piece is worth, which bucket it
      // comes out of and whether the brand will take it are the server's
      // answers, read off the same pool this screen is showing.
      const { data } = await api.post("/outbound/rtvs", {
        store: Number(storeId),
        vendor: Number(vendorId),
        brand: Number(brandId),
        return_type: returnType,
        logistics_route: logisticsRoute,
        via_transfer: logisticsRoute === "warehouse" ? Number(viaTransferId) : null,
        scans: Object.entries(scans).map(([barcode, qty]) => ({ barcode, qty })),
        notes,
      });
      navigate(`/return-to-brand/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  // Reached by typing the address as well as by the button, so the refusal is
  // said here too rather than only after a whole return has been scanned.
  if (!canCreateReturnToBrand(user)) {
    return (
      <div className="page-pad">
        <Link to="/return-to-brand" className="btn" style={{ marginBottom: 16 }} data-testid="rtv-back-link">
          <ArrowLeft size={15} /> Returns
        </Link>
        <div className="card section-card" data-testid="rtv-new-denied">
          <h1 className="h1 h2-rust">New Return to Brand</h1>
          <p className="lead">
            A store may only mark damage; the warehouse creates and executes returns.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="page-pad">
      <Link to="/return-to-brand" className="btn" style={{ marginBottom: 16 }} data-testid="rtv-back-link">
        <ArrowLeft size={15} /> Returns
      </Link>
      <h1 className="h1 h2-rust" style={{ marginBottom: 18 }}>New Return to Brand</h1>

      <div className="card section-card">
        <p className="eyebrow">Step 1 · Which brand, and how it travels</p>
        <div className="form-row" style={{ marginTop: 10 }}>
          <div className="field">
            <label>Store</label>
            {storeLocked && lockedStore ? (
              <div className="store-lock" data-testid="rtv-store-locked">{lockedStore.code} · {lockedStore.name}</div>
            ) : (
              <select
                className="select"
                value={storeId}
                onChange={(e) => { setStoreId(e.target.value); setScans({}); }}
                data-testid="rtv-store-select"
              >
                <option value="">Select store…</option>
                {stores.map((s) => <option key={s.id} value={s.id}>{s.code} · {s.name}</option>)}
              </select>
            )}
          </div>
          <div className="field">
            <label>Brand</label>
            <select
              className="select"
              value={brandId}
              onChange={(e) => { setBrandId(e.target.value); setScans({}); }}
              data-testid="rtv-brand-select"
            >
              <option value="">Select brand…</option>
              {brands.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Vendor</label>
            <select className="select" value={vendorId} onChange={(e) => setVendorId(e.target.value)} data-testid="rtv-vendor-select">
              <option value="">Select vendor…</option>
              {vendors.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Return type</label>
            <select
              className="select"
              value={returnType}
              onChange={(e) => { setReturnType(e.target.value); setScans({}); }}
              data-testid="rtv-type-select"
            >
              <option value="defective">Defective / GR return</option>
              <option value="seasonal">Season-end return</option>
            </select>
          </div>
        </div>
        <div className="form-row" style={{ marginTop: 14 }}>
          <div className="field">
            <label>How it gets back</label>
            <select className="select" value={logisticsRoute} onChange={(e) => setLogisticsRoute(e.target.value)} data-testid="rtv-logistics-select">
              <option value="">Select route…</option>
              {LOGISTICS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          {logisticsRoute === "warehouse" && (
            <div className="field">
              <label>Transfer that brought the pieces</label>
              <select className="select" value={viaTransferId} onChange={(e) => setViaTransferId(e.target.value)} data-testid="rtv-via-transfer-select">
                <option value="">Select transfer…</option>
                {transfers
                  .filter((t) => t.doc_number)
                  .map((t) => <option key={t.id} value={t.id}>{t.doc_number} → {t.destination_store_code}</option>)}
              </select>
            </div>
          )}
          <div className="field">
            <label>Notes</label>
            <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional notes" data-testid="rtv-notes" />
          </div>
        </div>
      </div>

      {/* Step 2 — the pool. Nothing here is typed: it is what the brand will take. */}
      {brandId && (
        <div className="card section-card" data-testid="rtv-pool">
          <p className="eyebrow">Step 2 · What this brand will take back</p>
          {poolLoading ? (
            <p className="lead">Loading the returnable pool…</p>
          ) : !pool ? (
            <p className="lead">Could not read the pool for this brand.</p>
          ) : pool.excluded_reason ? (
            <p className="lead" data-testid="rtv-pool-excluded">{pool.excluded_reason}</p>
          ) : (
            <>
              <h3 className="h3">
                {pool.brand.name}
                <span className="chip chip-grey" style={{ marginLeft: 8 }}>{pool.brand.commercial_label}</span>
                {pool.brand.return_window_days > 0 && (
                  <span className="chip chip-grey" style={{ marginLeft: 6 }}>
                    <CalendarClock size={13} /> {pool.brand.return_window_days}-day window
                  </span>
                )}
              </h3>
              {rows.length === 0 ? (
                <p className="lead" data-testid="rtv-pool-empty">
                  {returnType === "defective"
                    ? "Nothing confirmed damaged for this brand here. A store's damage flag has to be confirmed by the warehouse before the piece can go back."
                    : "No season-end stock inside the return window for this brand here."}
                </p>
              ) : (
                <div className="table-wrap" style={{ marginTop: 10 }}>
                  <table className="data" data-testid="rtv-pool-table">
                    <thead>
                      <tr>
                        <th>Barcode</th>
                        <th>Design</th>
                        <th>Size</th>
                        <th>Colour</th>
                        <th>Store</th>
                        <th className="num">Returnable</th>
                        <th className="num">Cost</th>
                        <th>Window</th>
                        <th className="num">Scanned</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r) => (
                        <tr key={`${r.store}-${r.sku_code}`} data-testid={`rtv-pool-row-${r.sku_code}`}>
                          <td><b className="mono">{r.sku_code}</b></td>
                          <td>{r.design || "—"}</td>
                          <td>{r.size || "—"}</td>
                          <td>{r.color || "—"}</td>
                          <td><b className="mono">{r.store_code}</b></td>
                          <td className="num">{r.qty}</td>
                          <td className="num"><Money paise={r.unit_cost_paise} /></td>
                          <td>
                            <span className={`chip chip-${windowTone(r.days_left)}`}>{windowText(r.days_left)}</span>
                          </td>
                          <td className="num"><b>{scans[r.sku_code] ?? 0}</b></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {expiredCount > 0 && (
                <p className="lead" style={{ fontSize: 13 }} data-testid="rtv-pool-expired">
                  {expiredCount} more {expiredCount === 1 ? "holding is" : "holdings are"} past the
                  return window and cannot go back. The window starts at the piece's first arrival
                  here.
                </p>
              )}

              {rows.length > 0 && (
                <button
                  type="button"
                  className="btn btn-cta"
                  style={{ marginTop: 14 }}
                  onClick={() => setScanning(true)}
                  data-testid="rtv-scan-btn"
                >
                  <ScanLine size={16} /> {scannedPieces ? "Scan again" : "Scan the pieces"}
                </button>
              )}
            </>
          )}
        </div>
      )}

      {/* Step 3 — the allowance, live, against what this return uses. */}
      {pool?.cap.applies && !pool.excluded_reason && (
        <CapMeter cap={pool.cap} thisReturnPaise={scannedValue} />
      )}

      {scannedPieces > 0 && (
        <div className="card section-card" data-testid="rtv-scanned-summary">
          <p className="eyebrow">Ready to send back</p>
          <h3 className="h3">{scannedPieces} pcs · <Money paise={scannedValue} /></h3>
          <p className="lead" style={{ fontSize: 13 }}>
            Valued from the books, not from this screen. It goes to the approvals
            inbox — a second person clears it before any stock moves.
          </p>
        </div>
      )}

      {scanning && pool && (
        <ScanScreen
          mode="RETURN"
          docLabel={`${pool.brand.name} · ${RETURN_TYPE_LABEL[returnType]}`}
          routeLabel={routeLabel(logisticsRoute)}
          targets={targets}
          strictExpected={false}
          rejectReason="Not in this brand's returnable pool — it cannot go back on this return."
          availableNoun={returnType === "defective" ? "confirmed damaged here" : "returnable here"}
          confirmLabel="Use these pieces"
          onConfirm={onScanned}
          onClose={() => setScanning(false)}
        />
      )}

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="rtv-create-error">{error}</div>}
      <button className="btn btn-primary btn-lg" disabled={saving} onClick={save} data-testid="save-rtv-btn">
        <RotateCcw size={16} /> {saving ? "Saving…" : "Create return (draft)"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

function CreditNoteCard({ rtv, writable }: { rtv: RTVT; writable: boolean }) {
  const [receivedOn, setReceivedOn] = useState("");
  const [reference, setReference] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function record() {
    setError("");
    if (!receivedOn) { setError("Date the brand's credit note."); return; }
    setSaving(true);
    try {
      await api.post(`/outbound/rtvs/${rtv.id}/credit-note`, {
        received_on: receivedOn,
        reference,
      });
      window.location.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
      setSaving(false);
    }
  }

  if (rtv.credit_note) {
    return (
      <div className="card section-card" data-testid="rtv-credit-note">
        <p className="eyebrow">Credit note</p>
        <h3 className="h3">Received {fmtDate(rtv.credit_note.received_on)}</h3>
        <p className="lead" style={{ fontSize: 13 }}>
          {rtv.credit_note.reference || "No reference"} · recorded by{" "}
          {rtv.credit_note.recorded_by_name || "—"}
        </p>
      </div>
    );
  }

  // A credit note answers a return that has actually gone back, so there is
  // nothing to record against a draft. Money already moved when the return
  // posted — this is status, not a payment.
  if (rtv.docstatus !== 1 || !writable) {
    return (
      <div className="card section-card" data-testid="rtv-credit-note">
        <p className="eyebrow">Credit note</p>
        <h3 className="h3">Pending</h3>
      </div>
    );
  }

  return (
    <div className="card section-card" data-testid="rtv-credit-note-form">
      <p className="eyebrow">Credit note</p>
      <h3 className="h3">Not received yet</h3>
      <div className="form-row" style={{ marginTop: 10 }}>
        <div className="field">
          <label>Received on</label>
          <input className="input" type="date" value={receivedOn} onChange={(e) => setReceivedOn(e.target.value)} data-testid="rtv-cn-date" />
        </div>
        <div className="field">
          <label>Brand's reference</label>
          <input className="input" value={reference} onChange={(e) => setReference(e.target.value)} placeholder="e.g. CN/2026/0042" data-testid="rtv-cn-ref" />
        </div>
      </div>
      {error && <div className="login-error" data-testid="rtv-cn-error">{error}</div>}
      <button type="button" className="btn" style={{ marginTop: 10 }} disabled={saving} onClick={record} data-testid="rtv-cn-save">
        {saving ? "Recording…" : "Record credit note"}
      </button>
    </div>
  );
}

export function RTVDetailPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const { data: r, loading } = useDoc<RTVT>(`/outbound/rtvs/${id}`);
  const writable = canWriteReturnToBrand(user);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  if (loading || !r) return <div className="page-pad"><p className="lead">Loading…</p></div>;

  const totalQty = r.lines.reduce((s, l) => s + l.qty, 0);
  // A draft cannot post until a second person has cleared it — the server
  // enforces it, the button only reflects it honestly.
  const canSubmit =
    r.docstatus === 0 && canCreateReturnToBrand(user) && isCleared(r.approval);

  async function handleSubmit() {
    setError("");
    setSubmitting(true);
    try {
      await api.post(`/outbound/rtvs/${r!.id}/submit`);
      window.location.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="page-pad">
      <Link to="/return-to-brand" className="btn" style={{ marginBottom: 16 }} data-testid="rtv-detail-back">
        <ArrowLeft size={15} /> Returns
      </Link>
      <div className="toolbar">
        <div>
          <p className="eyebrow">{r.doc_number || `Draft #${r.id}`}</p>
          <h1 className="h1">{r.brand_name || "Return"} — {r.store_code}</h1>
          <p className="lead">
            {r.store_name}
            {r.season ? ` · ${r.season}` : ""}
            {r.notes ? ` · ${r.notes}` : ""}
          </p>
        </div>
        <div className="spacer" />
        <DocPill ds={r.docstatus} />
        {r.docstatus === 0 && <ApprovalPill status={r.approval?.status ?? "pending"} />}
        <span className={`chip chip-${r.return_type === "defective" ? "red" : "amber"}`}>
          {RETURN_TYPE_LABEL[r.return_type] ?? r.return_type}
        </span>
        {canSubmit && (
          <button
            type="button"
            className="btn btn-cta"
            disabled={submitting}
            onClick={handleSubmit}
            data-testid="submit-rtv-btn"
          >
            <Send size={15} /> {submitting ? "Submitting…" : "Send back to brand"}
          </button>
        )}
      </div>

      <div className="form-row" style={{ marginBottom: 18 }}>
        <div className="card section-card">
          <p className="eyebrow">Logistics</p>
          <h3 className="h3">{r.logistics_route_label || routeLabel(r.logistics_route)}</h3>
          {r.via_transfer_number && (
            <p className="lead" style={{ marginTop: 4 }}>via {r.via_transfer_number}</p>
          )}
        </div>
        <div className="card section-card">
          <p className="eyebrow">Going back</p>
          <h3 className="h3">{totalQty} pcs</h3>
          <p className="lead" style={{ marginTop: 4 }}>{r.value_paise ? <Money paise={r.value_paise} /> : "—"}</p>
        </div>
        {r.return_window_date && (
          <div className="card section-card">
            <p className="eyebrow">Return window</p>
            <h3 className="h3">{fmtDate(r.return_window_date)}</h3>
            <p className="lead" style={{ marginTop: 4 }}>
              <span className={`chip chip-${windowTone(r.days_to_window)}`} data-testid="rtv-window-countdown">
                {windowText(r.days_to_window)}
              </span>
            </p>
          </div>
        )}
        <div className="card section-card">
          <p className="eyebrow">Date</p>
          <h3 className="h3">{fmtDate(r.created_at)}</h3>
        </div>
      </div>

      {/* The allowance as it stood when this return was raised (Rule 2) — not
          recomputed, because the next arrival would move it under the approver. */}
      {r.cap_allowance_paise > 0 && (
        <div className="card section-card" data-testid="rtv-cap-snapshot">
          <p className="eyebrow">Allowed return when this was raised — {r.cap_percent}%</p>
          <h3 className="h3">
            <Money paise={r.cap_used_before_paise + r.value_paise} /> of{" "}
            <Money paise={r.cap_allowance_paise} />
          </h3>
          {r.cap_exceeded_by_paise > 0 && (
            <p className="lead" data-testid="rtv-cap-flag">
              <span className="chip chip-red">
                <AlertTriangle size={13} /> Over by <Money paise={r.cap_exceeded_by_paise} />
              </span>{" "}
              Flagged, not blocked — the approver decided with this on screen.
            </p>
          )}
        </div>
      )}

      <div className="form-row" style={{ marginBottom: 18 }}>
        <CreditNoteCard rtv={r} writable={writable} />
      </div>

      <div style={{ marginBottom: 18 }}>
        <ApprovalTrail
          createdByName={r.created_by_name}
          createdAt={r.created_at}
          approval={r.approval}
          history={r.approval_history}
          askAgainPath={writable ? `/outbound/rtvs/${r.id}/request-approval` : undefined}
        />
      </div>

      {error && <div className="login-error" style={{ maxWidth: 480 }} data-testid="rtv-detail-error">{error}</div>}

      <div className="table-wrap">
        <table className="data" data-testid="rtv-detail-lines">
          <thead>
            <tr>
              <th>Barcode</th>
              <th>Design</th>
              <th>Size</th>
              <th>Colour</th>
              <th>Brand</th>
              <th>From</th>
              <th className="num">Qty</th>
              <th className="num">Cost</th>
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
                <td>
                  <span className={`chip chip-${l.source === "quarantine" ? "red" : "amber"}`}>
                    {l.source === "quarantine" ? "Quarantine" : "Season-end"}
                  </span>
                </td>
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
