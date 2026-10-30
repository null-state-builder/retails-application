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
import { FilterChip, FilterChips } from "../components/FilterChip";
import { KpiStrip } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { SearchBox } from "../components/SearchBox";
import { Section } from "../components/Section";
import { EmptyState } from "../components/States";
import { Status, type StatusTone } from "../components/Status";
import { INVENTORY_FOLD, foldTabsFor } from "../shell/navConfig";
import { useWideScreen } from "../lib/useWideScreen";
import "./Booking.css";
import { PageHeader } from "../components/PageHeader";
import { SegmentedControl } from "../components/SegmentedControl";

// ---------------------------------------------------------------------------
// Helpers & controlled vocab
// ---------------------------------------------------------------------------

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

const DS_TONE: Record<number, StatusTone> = { 0: "grey", 1: "green", 2: "red" };
const DS_LABEL: Record<number, string> = { 0: "Draft", 1: "Submitted", 2: "Cancelled" };
function DocPill({ ds }: { ds: number }) {
  return <Status tone={DS_TONE[ds] ?? "grey"}>{DS_LABEL[ds] ?? ds}</Status>;
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

function windowTone(days: number | null): StatusTone {
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

interface StoreT {
  id: number;
  code: string;
  name: string;
  store_type: string;
}
interface VendorT {
  id: number;
  name: string;
}
interface BrandT {
  id: number;
  name: string;
}
interface TransferT {
  id: number;
  doc_number: string | null;
  destination_store_code: string;
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

/** Where a return stands, for the list's chips (mockup Return to Brand): a
 *  draft is still in progress, a posted return waits for the brand's credit
 *  note, and one with its credit note is done. */
type RtvStage = "progress" | "owed" | "done" | "cancelled";

function rtvStage(r: Pick<RTVT, "docstatus" | "credit_note">): RtvStage {
  if (r.docstatus === 2) return "cancelled";
  if (r.docstatus === 0) return "progress";
  return r.credit_note ? "done" : "owed";
}

const RTV_FILTERS: { key: RtvStage | "all"; label: string }[] = [
  { key: "all", label: "All" },
  { key: "progress", label: "In progress" },
  { key: "owed", label: "Credit note owed" },
  { key: "done", label: "Done" },
];

/** How an empty chip says so: "No defective return is in progress." */
const STAGE_EMPTY: Record<RtvStage, string> = {
  progress: "is in progress",
  owed: "is waiting for its credit note",
  done: "has its credit note yet",
  cancelled: "is cancelled",
};

const RETURN_TYPES = [
  { key: "defective", label: "Defective / GR", icon: PackageMinus },
  { key: "seasonal", label: "Season-end", icon: RotateCcw },
] as const;

export function RTVListPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { user, session, featuresOn } = useAuth();
  const tab = params.get("type") === "seasonal" ? "seasonal" : "defective";
  const [q, setQ] = useState("");
  const [stage, setStage] = useState<RtvStage | "all">("all");
  const { data, loading } = useList<RTVT>("/outbound/rtvs", { return_type: tab, q });
  // Reaching this section is not the same as being allowed to send stock back:
  // a store person opens the list and marks damage, and the warehouse is the
  // one who raises the return (#75).
  const mayCreate = canCreateReturnToBrand(user);
  // The earlier damage records stay readable from here (desktop redesign Q4),
  // for whoever the Stock page draws that tab for.
  const earlierDamage =
    !!user &&
    foldTabsFor(INVENTORY_FOLD, user, session?.display_actions ?? [], featuresOn).some(
      (t) => t.slug === "damage-history",
    );

  function setTab(next: string) {
    setParams((p) => {
      p.set("type", next);
      return p;
    });
  }

  const counts = {
    all: data.length,
    progress: data.filter((r) => rtvStage(r) === "progress").length,
    owed: data.filter((r) => rtvStage(r) === "owed").length,
    done: data.filter((r) => rtvStage(r) === "done").length,
    cancelled: 0,
  };
  const rows = stage === "all" ? data : data.filter((r) => rtvStage(r) === stage);
  // The kind as a word inside a sentence: "No defective returns yet."
  const kind = tab === "seasonal" ? "season-end" : "defective";

  return (
    <div className="page-pad">
      <PageHeader
        title="Returns to brands"
        lead={
          <>
            {/* Said only once the list has answered: no guessed count. */}
            {!loading && data.length > 0 && (
              <b data-testid="rtv-count">
                {counts.progress} in progress · {counts.owed} credit{" "}
                {counts.owed === 1 ? "note" : "notes"} owed.{" "}
              </b>
            )}
            Pieces going back to a brand, within its return allowance, until the brand&apos;s credit
            note arrives.
          </>
        }
        actions={
          <>
            {earlierDamage && (
              <Link className="btn" to="/inventory?tab=damage-history" data-testid="rtv-earlier">
                Earlier damage records
              </Link>
            )}
            {mayCreate && (
              <Link className="btn btn-primary" to="/return-to-brand/new" data-testid="new-rtv-btn">
                <Plus size={16} /> New return
              </Link>
            )}
          </>
        }
      />

      <div className="toolbar">
        {/* Two kinds of return, both visible: a switch, not two big buttons. */}
        <SegmentedControl
          label="Kind of return"
          testId="rtv-type-toggle"
          value={tab}
          options={RETURN_TYPES.map(({ key, label, icon: Icon }) => ({
            value: key,
            label: (
              <>
                <Icon size={14} aria-hidden="true" /> {label}
              </>
            ),
            testId: `rtv-tab-${key}`,
          }))}
          onChange={setTab}
        />
        <FilterChips label="Where it stands">
          {RTV_FILTERS.map(({ key, label }) => (
            <FilterChip
              key={key}
              pressed={stage === key}
              onClick={() => setStage(key)}
              count={loading ? "—" : counts[key]}
              testId={`rtv-filter-${key}`}
            >
              {label}
            </FilterChip>
          ))}
        </FilterChips>
        <div className="spacer" />
        <SearchBox
          value={q}
          onChange={setQ}
          placeholder="Return number or brand"
          label="Search returns to brand"
          testId="rtv-search"
        />
      </div>

      {loading ? (
        <p className="lead">Loading…</p>
      ) : rows.length === 0 ? (
        <EmptyState
          testId="rtv-empty"
          action={
            mayCreate && !q && stage === "all" ? (
              <Link className="btn" to="/return-to-brand/new">
                <Plus size={14} /> New return
              </Link>
            ) : undefined
          }
        >
          {q
            ? `No ${kind} return matches “${q}”.`
            : stage === "all"
              ? `No ${kind} returns yet.`
              : `No ${kind} return ${STAGE_EMPTY[stage as RtvStage]}.`}
        </EmptyState>
      ) : (
        <div className="table-wrap capped">
          <table className="data" data-testid="rtv-table">
            <caption className="sr-only">
              Returns to brands, each with its pieces, value, window, credit note and status.
            </caption>
            <thead>
              <tr>
                <th>Return</th>
                <th>Brand</th>
                <th>Store</th>
                <th>Logistics</th>
                <th className="num">Pieces</th>
                <th className="num">Value</th>
                <th>Window</th>
                <th>Credit note</th>
                <th>Status</th>
                <th>Raised</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  data-testid={`rtv-row-${r.id}`}
                  onClick={() => navigate(`/return-to-brand/${r.id}`)}
                  style={{ cursor: "pointer" }}
                >
                  <td>
                    <Link
                      to={`/return-to-brand/${r.id}`}
                      className="link-cell mono"
                      onClick={(e) => e.stopPropagation()}
                      data-testid={`rtv-link-${r.id}`}
                    >
                      <b>{r.doc_number || `Draft #${r.id}`}</b>
                    </Link>
                  </td>
                  <td>
                    {r.brand_name || "—"}
                    {r.commercial_label && (
                      <span className="chip" style={{ marginLeft: 6 }}>
                        {r.commercial_label}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="mono">{r.store_code}</span>
                  </td>
                  <td>{r.logistics_route_label || routeLabel(r.logistics_route)}</td>
                  <td className="num">
                    {r.lines.reduce((s, l) => s + l.qty, 0).toLocaleString("en-IN")}
                  </td>
                  <td className="num">{r.value_paise ? <Money paise={r.value_paise} /> : "—"}</td>
                  <td>
                    {r.return_window_date ? (
                      <Status tone={windowTone(r.days_to_window)}>
                        {windowText(r.days_to_window)}
                      </Status>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {r.credit_note ? (
                      <Status tone="green">Received {fmtDate(r.credit_note.received_on)}</Status>
                    ) : r.docstatus === 1 ? (
                      <Status tone="amber">Owed</Status>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    <DocPill ds={r.docstatus} />
                  </td>
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

/** The returns still moving, beside the one being read (layout L): drafts and
 *  posted returns still owed a credit note, newest first. The same scoped read
 *  the list makes, both kinds together. */
function RTVQueue({ currentId }: { currentId: string }) {
  const { data, loading } = useList<RTVT>("/outbound/rtvs");
  const open = data.filter((r) => ["progress", "owed"].includes(rtvStage(r)));
  if (loading || open.length === 0) return null;
  return (
    <nav
      className="box side-queue"
      style={{ padding: "var(--space-8) 0" }}
      aria-label="Open returns"
      data-testid="rtv-queue"
    >
      <p className="side-queue-head">
        Open returns <span className="filter-chip-n">{open.length}</span>
      </p>
      <ul>
        {open.map((r) => (
          <li key={r.id}>
            <Link
              to={`/return-to-brand/${r.id}`}
              className={`side-queue-item${String(r.id) === currentId ? " active" : ""}`}
              aria-current={String(r.id) === currentId ? "page" : undefined}
            >
              <span className="side-queue-ref">{r.brand_name || "Return"}</span>
              <span className="side-queue-meta">
                <span className="mono">{r.doc_number || `Draft #${r.id}`}</span>
                {rtvStage(r) === "owed" ? "Credit note owed" : "In progress"}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// The cap meter — the same numbers the server snapshots onto the return
// ---------------------------------------------------------------------------

function CapMeter({ cap, thisReturnPaise }: { cap: CapT; thisReturnPaise: number }) {
  const used = cap.used_paise + thisReturnPaise;
  const pct =
    cap.allowance_paise > 0 ? Math.min(100, Math.round((used / cap.allowance_paise) * 100)) : 100;
  const over = cap.allowance_paise > 0 && used > cap.allowance_paise;
  // The threshold is the server's (`CAP_WARN_AT`), sent down as a value rather
  // than a percentage — recomputing it here would be a second copy that a
  // retune on the server would silently leave behind.
  const warn = !over && cap.allowance_paise > 0 && used >= cap.warn_at_paise;
  const tone = over ? "red" : warn ? "amber" : "green";

  return (
    <Section
      heading={`Allowed return — ${cap.percent}% of what this brand delivered`}
      ruled
      testId="rtv-cap-meter"
    >
      <p>
        <b>
          <Money paise={used} /> of <Money paise={cap.allowance_paise} />
        </b>
      </p>
      <div
        style={{
          height: 10,
          borderRadius: "var(--radius-control)",
          background: "var(--line)",
          overflow: "hidden",
        }}
      >
        <div
          data-testid="rtv-cap-bar"
          style={{
            width: `${pct}%`,
            height: "100%",
            background: `var(--${tone})`,
          }}
        />
      </div>
      <p className="lead" style={{ fontSize: 13 }}>
        Already returned <Money paise={cap.used_paise} /> · this return{" "}
        <Money paise={thisReturnPaise} /> · delivered <Money paise={cap.delivered_paise} />
      </p>
      {over ? (
        <p className="lead" data-testid="rtv-cap-exceeded">
          <Status tone="red">
            Over the allowance by <Money paise={used - cap.allowance_paise} />
          </Status>{" "}
          Flagged for the approver — the return is not blocked.
        </p>
      ) : warn ? (
        <p className="lead" data-testid="rtv-cap-warning">
          <Status tone="amber">Close to the allowance</Status>{" "}
          <Money paise={cap.allowance_paise - used} /> left this season.
        </p>
      ) : (
        <p className="lead" data-testid="rtv-cap-room">
          <Money paise={Math.max(0, cap.allowance_paise - used)} /> of room left.
        </p>
      )}
    </Section>
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
    brandId
      ? `/outbound/returnable-pool?brand=${brandId}${storeId ? `&store=${storeId}` : ""}`
      : null,
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
    if (!storeId) {
      setError("Select a store.");
      return;
    }
    if (!vendorId) {
      setError("Select the vendor this brand is billed through.");
      return;
    }
    if (!brandId) {
      setError("Select a brand — the pool is a brand's stock.");
      return;
    }
    if (!logisticsRoute) {
      setError("Say how the stock gets back to the brand.");
      return;
    }
    if (logisticsRoute === "warehouse" && !viaTransferId) {
      setError("A consolidated return has to name the transfer that brought the pieces in.");
      return;
    }
    if (!scannedPieces) {
      setError("Scan the pieces going back.");
      return;
    }
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
        <Link
          to="/return-to-brand"
          className="btn"
          style={{ marginBottom: 16 }}
          data-testid="rtv-back-link"
        >
          <ArrowLeft size={15} /> Returns
        </Link>
        <div data-testid="rtv-new-denied">
          <h1 className="h1">New Return to Brand</h1>
          <Note tone="warn">
            A store may only mark damage; the warehouse creates and executes returns.
          </Note>
        </div>
      </div>
    );
  }

  return (
    <div className="page-pad">
      <Link
        to="/return-to-brand"
        className="btn"
        style={{ marginBottom: 16 }}
        data-testid="rtv-back-link"
      >
        <ArrowLeft size={15} /> Returns
      </Link>
      <h1 className="h1" style={{ marginBottom: 18 }}>
        New Return to Brand
      </h1>

      <div className="page">
        <Section heading="Step 1 · Which brand, and how it travels">
          <div className="form-row">
            <div className="field">
              <label>Store</label>
              {storeLocked && lockedStore ? (
                <div className="store-lock" data-testid="rtv-store-locked">
                  {lockedStore.code} · {lockedStore.name}
                </div>
              ) : (
                <select
                  className="select"
                  value={storeId}
                  onChange={(e) => {
                    setStoreId(e.target.value);
                    setScans({});
                  }}
                  data-testid="rtv-store-select"
                >
                  <option value="">Select store…</option>
                  {stores.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.code} · {s.name}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <div className="field">
              <label>Brand</label>
              <select
                className="select"
                value={brandId}
                onChange={(e) => {
                  setBrandId(e.target.value);
                  setScans({});
                }}
                data-testid="rtv-brand-select"
              >
                <option value="">Select brand…</option>
                {brands.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Vendor</label>
              <select
                className="select"
                value={vendorId}
                onChange={(e) => setVendorId(e.target.value)}
                data-testid="rtv-vendor-select"
              >
                <option value="">Select vendor…</option>
                {vendors.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Return type</label>
              <select
                className="select"
                value={returnType}
                onChange={(e) => {
                  setReturnType(e.target.value);
                  setScans({});
                }}
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
              <select
                className="select"
                value={logisticsRoute}
                onChange={(e) => setLogisticsRoute(e.target.value)}
                data-testid="rtv-logistics-select"
              >
                <option value="">Select route…</option>
                {LOGISTICS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            {logisticsRoute === "warehouse" && (
              <div className="field">
                <label>Transfer that brought the pieces</label>
                <select
                  className="select"
                  value={viaTransferId}
                  onChange={(e) => setViaTransferId(e.target.value)}
                  data-testid="rtv-via-transfer-select"
                >
                  <option value="">Select transfer…</option>
                  {transfers
                    .filter((t) => t.doc_number)
                    .map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.doc_number} → {t.destination_store_code}
                      </option>
                    ))}
                </select>
              </div>
            )}
            <div className="field">
              <label>Notes</label>
              <input
                className="input"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                placeholder="Optional notes"
                data-testid="rtv-notes"
              />
            </div>
          </div>
        </Section>

        {/* Step 2 — the pool. Nothing here is typed: it is what the brand will take. */}
        {brandId && (
          <Section heading="Step 2 · What this brand will take back" ruled testId="rtv-pool">
            {poolLoading ? (
              <p className="lead">Loading the returnable pool…</p>
            ) : !pool ? (
              <p className="lead">Could not read the pool for this brand.</p>
            ) : pool.excluded_reason ? (
              <p className="lead" data-testid="rtv-pool-excluded">
                {pool.excluded_reason}
              </p>
            ) : (
              <>
                <h3 className="heading">
                  {pool.brand.name}
                  <span className="chip" style={{ marginLeft: 8 }}>
                    {pool.brand.commercial_label}
                  </span>
                  {pool.brand.return_window_days > 0 && (
                    <span className="chip" style={{ marginLeft: 6 }}>
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
                  <div className="table-wrap">
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
                          <tr
                            key={`${r.store}-${r.sku_code}`}
                            data-testid={`rtv-pool-row-${r.sku_code}`}
                          >
                            <td>
                              <b className="mono">{r.sku_code}</b>
                            </td>
                            <td>{r.design || "—"}</td>
                            <td>{r.size || "—"}</td>
                            <td>{r.color || "—"}</td>
                            <td>
                              <b className="mono">{r.store_code}</b>
                            </td>
                            <td className="num">{r.qty}</td>
                            <td className="num">
                              <Money paise={r.unit_cost_paise} />
                            </td>
                            <td>
                              <Status tone={windowTone(r.days_left)}>
                                {windowText(r.days_left)}
                              </Status>
                            </td>
                            <td className="num">
                              <b>{scans[r.sku_code] ?? 0}</b>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {expiredCount > 0 && (
                  <p className="lead" style={{ fontSize: 13 }} data-testid="rtv-pool-expired">
                    {expiredCount} more {expiredCount === 1 ? "holding is" : "holdings are"} past
                    the return window and cannot go back. The window starts at the piece's first
                    arrival here.
                  </p>
                )}

                {rows.length > 0 && (
                  <button
                    type="button"
                    className="btn btn-primary"
                    style={{ alignSelf: "flex-start" }}
                    onClick={() => setScanning(true)}
                    data-testid="rtv-scan-btn"
                  >
                    <ScanLine size={16} /> {scannedPieces ? "Scan again" : "Scan the pieces"}
                  </button>
                )}
              </>
            )}
          </Section>
        )}

        {/* Step 3 — the allowance, live, against what this return uses. */}
        {pool?.cap.applies && !pool.excluded_reason && (
          <CapMeter cap={pool.cap} thisReturnPaise={scannedValue} />
        )}

        {scannedPieces > 0 && (
          <Section heading="Ready to send back" ruled testId="rtv-scanned-summary">
            <p>
              <b>
                {scannedPieces} pcs · <Money paise={scannedValue} />
              </b>
            </p>
            <p className="lead" style={{ fontSize: 13 }}>
              Valued from the books, not from this screen. It goes to the approvals inbox — a second
              person clears it before any stock moves.
            </p>
          </Section>
        )}

        {scanning && pool && (
          <ScanScreen
            mode="RETURN"
            docLabel={`${pool.brand.name} · ${RETURN_TYPE_LABEL[returnType]}`}
            routeLabel={routeLabel(logisticsRoute)}
            targets={targets}
            strictExpected={false}
            rejectReason="Not in this brand's returnable pool — it cannot go back on this return."
            availableNoun={
              returnType === "defective" ? "confirmed damaged here" : "returnable here"
            }
            confirmLabel="Use these pieces"
            onConfirm={onScanned}
            onClose={() => setScanning(false)}
          />
        )}

        {error && (
          <Note tone="error" testId="rtv-create-error">
            {error}
          </Note>
        )}
        <div>
          <button
            className="btn btn-primary btn-lg"
            disabled={saving}
            onClick={save}
            data-testid="save-rtv-btn"
          >
            <RotateCcw size={16} /> {saving ? "Saving…" : "Create return (draft)"}
          </button>
        </div>
      </div>
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
    if (!receivedOn) {
      setError("Date the brand's credit note.");
      return;
    }
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
      <Section heading="Credit note" ruled testId="rtv-credit-note">
        <p>
          <Status tone="green">Received {fmtDate(rtv.credit_note.received_on)}</Status>
        </p>
        <p className="lead" style={{ fontSize: 13 }}>
          {rtv.credit_note.reference || "No reference"} · recorded by{" "}
          {rtv.credit_note.recorded_by_name || "—"}
        </p>
      </Section>
    );
  }

  // A credit note answers a return that has actually gone back, so there is
  // nothing to record against a draft. Money already moved when the return
  // posted — this is status, not a payment.
  if (rtv.docstatus !== 1 || !writable) {
    return (
      <Section heading="Credit note" ruled testId="rtv-credit-note">
        <p>
          <Status tone="amber">Pending</Status>
        </p>
      </Section>
    );
  }

  return (
    <Section heading="Credit note" ruled testId="rtv-credit-note-form">
      <p>
        <Status tone="amber">Not received yet</Status>
      </p>
      <div className="form-row">
        <div className="field">
          <label>Received on</label>
          <input
            className="input"
            type="date"
            value={receivedOn}
            onChange={(e) => setReceivedOn(e.target.value)}
            data-testid="rtv-cn-date"
          />
        </div>
        <div className="field">
          <label>Brand's reference</label>
          <input
            className="input"
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            placeholder="e.g. CN/2026/0042"
            data-testid="rtv-cn-ref"
          />
        </div>
      </div>
      {error && (
        <Note tone="error" testId="rtv-cn-error">
          {error}
        </Note>
      )}
      <button
        type="button"
        className="btn"
        disabled={saving}
        onClick={record}
        data-testid="rtv-cn-save"
      >
        {saving ? "Recording…" : "Record credit note"}
      </button>
    </Section>
  );
}

/** `/return-to-brand/<id>`, with the open returns beside it on a wide screen
 *  (layout L). */
export function RTVDetailPage() {
  const { id = "" } = useParams();
  const wide = useWideScreen();
  if (!wide) return <RTVDetail id={id} />;
  return (
    <div className="page-pad queue-split">
      <RTVQueue currentId={id} />
      <div className="queue-work">
        <RTVDetail id={id} />
      </div>
    </div>
  );
}

function RTVDetail({ id }: { id: string }) {
  const { user } = useAuth();
  const { data: r, loading } = useDoc<RTVT>(`/outbound/rtvs/${id}`);
  const writable = canWriteReturnToBrand(user);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  if (loading || !r)
    return (
      <div className="page-pad">
        <p className="lead">Loading…</p>
      </div>
    );

  const totalQty = r.lines.reduce((s, l) => s + l.qty, 0);
  // A draft cannot post until a second person has cleared it — the server
  // enforces it, the button only reflects it honestly.
  const canSubmit = r.docstatus === 0 && canCreateReturnToBrand(user) && isCleared(r.approval);

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
      <Link
        to="/return-to-brand"
        className="btn"
        style={{ marginBottom: 16 }}
        data-testid="rtv-detail-back"
      >
        <ArrowLeft size={15} /> Returns
      </Link>
      <div className="toolbar">
        <div>
          <h1 className="h1">
            {r.brand_name || "Return"} — {r.store_code}
          </h1>
          <p className="lead">
            <span className="mono">{r.doc_number || `Draft #${r.id}`}</span> · {r.store_name}
            {r.season ? ` · ${r.season}` : ""}
            {r.notes ? ` · ${r.notes}` : ""}
          </p>
        </div>
        <div className="spacer" />
        <DocPill ds={r.docstatus} />
        {r.docstatus === 0 && <ApprovalPill status={r.approval?.status ?? "pending"} />}
        <span className="chip">{RETURN_TYPE_LABEL[r.return_type] ?? r.return_type}</span>
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

      <div className="page">
        <KpiStrip
          label="Return"
          items={[
            {
              label: "Logistics",
              value: r.logistics_route_label || routeLabel(r.logistics_route),
              detail: r.via_transfer_number ? `via ${r.via_transfer_number}` : undefined,
            },
            {
              label: "Going back",
              value: `${totalQty} pcs`,
              detail: r.value_paise ? <Money paise={r.value_paise} /> : "—",
            },
            ...(r.return_window_date
              ? [
                  {
                    label: "Return window",
                    value: fmtDate(r.return_window_date),
                    detail: (
                      <Status tone={windowTone(r.days_to_window)} testId="rtv-window-countdown">
                        {windowText(r.days_to_window)}
                      </Status>
                    ),
                  },
                ]
              : []),
            { label: "Date", value: fmtDate(r.created_at) },
          ]}
        />

        {/* The allowance as it stood when this return was raised (Rule 2) — not
          recomputed, because the next arrival would move it under the approver. */}
        {r.cap_allowance_paise > 0 && (
          <Section
            heading={`Allowed return when this was raised — ${r.cap_percent}%`}
            testId="rtv-cap-snapshot"
          >
            <p>
              <b>
                <Money paise={r.cap_used_before_paise + r.value_paise} /> of{" "}
                <Money paise={r.cap_allowance_paise} />
              </b>
            </p>
            {r.cap_exceeded_by_paise > 0 && (
              <p className="lead" data-testid="rtv-cap-flag">
                <Status tone="red">
                  Over by <Money paise={r.cap_exceeded_by_paise} />
                </Status>{" "}
                Flagged, not blocked — the approver decided with this on screen.
              </p>
            )}
          </Section>
        )}

        <CreditNoteCard rtv={r} writable={writable} />

        <div>
          <ApprovalTrail
            createdByName={r.created_by_name}
            createdAt={r.created_at}
            approval={r.approval}
            history={r.approval_history}
            askAgainPath={writable ? `/outbound/rtvs/${r.id}/request-approval` : undefined}
          />
        </div>

        {error && (
          <Note tone="error" testId="rtv-detail-error">
            {error}
          </Note>
        )}

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
                  <td>
                    <b className="mono">{l.sku_code}</b>
                  </td>
                  <td>{l.design || "—"}</td>
                  <td>{l.size || "—"}</td>
                  <td>{l.color || "—"}</td>
                  <td>{l.brand || "—"}</td>
                  <td>{l.source === "quarantine" ? "Quarantine" : "Season-end"}</td>
                  <td className="num">{l.qty}</td>
                  <td className="num">
                    {l.unit_cost_paise ? <Money paise={l.unit_cost_paise} /> : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
