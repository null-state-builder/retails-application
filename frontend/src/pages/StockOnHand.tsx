import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Boxes,
  CheckCircle2,
  IndianRupee,
  Layers,
  Minus,
  Plus,
  Repeat,
  ScrollText,
  ShieldAlert,
  X,
} from "lucide-react";

import { fmtApprovalWhen } from "../components/approval";
import type { ApprovalT } from "../components/approval";
import { SearchBox } from "../components/SearchBox";
import { api, apiErrorMessage } from "../lib/api";
import { Money } from "../lib/format";
import { withQuery } from "../lib/query";
import "./Booking.css";
import "./Shared.css";
import { PageHeader } from "../components/PageHeader";
import { OperationsPage } from "../components/OperationsPage";
import { stockParams } from "../lib/stockWorkspace";
import type { StockWorkspaceFilter } from "./StockWorkspace";

type Group = "sku" | "brand" | "store" | "quarantine";

interface RowT {
  store_id?: number;
  store_code: string;
  store_name: string;
  brand: string;
  design: string;
  color: string;
  size: string;
  item: string;
  season: string;
  sku_code: string;
  net_qty: number;
  skus: number;
  net_value_paise?: number;
  record_contract?: "goods-v1" | "legacy";
}

interface OnHandT {
  group_by: Group;
  summary: {
    units_on_hand: number;
    value_paise?: number;
    identity_complete?: boolean;
    value_complete?: boolean;
    scope?: "current_access";
    lines: number;
    displayed?: number;
    truncated?: boolean;
  };
  rows: RowT[];
}

interface QuarRowT {
  store_code: string;
  store_name: string;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  item: string;
  season: string;
  brand: string;
  qty: number;
  value_paise?: number;
  marked_by: string | null;
  marked_at: string | null;
}

interface QuarT {
  summary: { units_quarantined: number; value_paise?: number; lines: number };
  rows: QuarRowT[];
}

/** A damage report and where it has got to (#138). A store's report is a flag:
 *  the pieces stay sellable until a warehouse or HO person confirms it. */
interface DamageFlagT {
  id: number;
  flag_status: "flagged" | "confirmed" | "rejected";
  store_code: string;
  created_by_name: string;
  approval: ApprovalT | null;
  created_at: string;
  lines: {
    sku_code: string;
    design: string;
    color: string;
    size: string;
    brand: string;
    qty: number;
  }[];
}

const TABS: { key: Group; label: string }[] = [
  { key: "sku", label: "By SKU" },
  { key: "brand", label: "By Brand" },
  { key: "store", label: "By Store" },
  { key: "quarantine", label: "Quarantine" },
];

export function StockValue({ paise }: { paise: number | undefined }) {
  return typeof paise === "number" ? (
    <Money paise={paise} />
  ) : (
    <span className="muted">Unavailable</span>
  );
}

export function stockQuantity(value: number | undefined, loading: boolean): number | string {
  return value !== undefined ? value : loading ? "Loading…" : "Unavailable";
}

/** Which half of this screen to draw when it is hosted on the Inventory page
 *  (#170), where Stock on Hand and Damage & Quarantine are two separate tabs -
 *  so the panel must not also offer the other one as a fourth grouping. */
export type StockView = "stock" | "quarantine";

/** The groupings that belong to one half. Standing alone at /stock all four are
 *  one strip and Quarantine is the fourth; hosted, the host has already split
 *  them across two tabs. */
function groupingsFor(view: StockView | undefined): typeof TABS {
  if (!view) return TABS;
  return TABS.filter((t) => (t.key === "quarantine") === (view === "quarantine"));
}

export default function StockOnHand({
  view,
  workspace,
}: { view?: StockView; workspace?: StockWorkspaceFilter } = {}) {
  // Where a global-search result lands (#86): one barcode, or one brand, with
  // its stock wherever the caller may see it. Both filters are the server's, so
  // the answer survives the on-hand line cap.
  const [params, setParams] = useSearchParams();
  const skuFilter = params.get("sku") ?? "";
  const brandFilter = params.get("brand") ?? "";
  const deepFilter = skuFilter || brandFilter;
  // Standing alone at /stock the four groupings are one strip, and
  // `?view=quarantine` is the deep link the Return to Brand section uses for
  // Damage / Quarantine — the screen lives here, the menu entry lives there.
  // Hosted, the host has already chosen which half this is.
  const wantedGroup = params.get("group") ?? params.get("group_by");
  const group: Group =
    view === "quarantine" || (!view && params.get("view") === "quarantine")
      ? "quarantine"
      : wantedGroup === "brand" || wantedGroup === "store"
        ? wantedGroup
        : "sku";
  const setGroup = (value: Group) =>
    setParams(stockParams(params, { group: value === "sku" ? "" : value }));
  const [data, setData] = useState<OnHandT | null>(null);
  const [quar, setQuar] = useState<QuarT | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  // The screen's own search (#102) — style, barcode or brand. A wedge scan lands
  // here too, so a store person can check a tag without going up to the top bar.
  const q = workspace?.query ?? params.get("q") ?? "";
  const setQ = (value: string) => setParams(stockParams(params, { q: value }), { replace: true });
  const storeCode = workspace?.storeCode ?? "";
  const storeId = workspace?.storeId;

  // Quarantine filters (the backend accepts ?store=&brand=; this exposes them).
  const [qStore, setQStore] = useState("");
  const [qBrand, setQBrand] = useState("");
  // Option lists come from an unfiltered snapshot so a chosen filter never
  // empties the other dropdown.
  const [quarOpts, setQuarOpts] = useState<{ stores: [string, string][]; brands: string[] }>({
    stores: [],
    brands: [],
  });

  // Damage reports that have not become quarantine yet — the reporting store's
  // own view of "I said so, and it hasn't been actioned" (#138).
  const [openFlags, setOpenFlags] = useState<DamageFlagT[]>([]);
  const [flagsErr, setFlagsErr] = useState("");

  // Mark-damaged modal state (pre-commit adjustment before posting).
  const [dmgRow, setDmgRow] = useState<RowT | null>(null);
  const [dmgQty, setDmgQty] = useState(1);
  const [dmgErr, setDmgErr] = useState("");
  const [dmgBusy, setDmgBusy] = useState(false);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError("");
    setData(null);
    setQuar(null);
    if (group === "quarantine") {
      api
        .get(withQuery("/stockledger/quarantine", { store: qStore, brand: qBrand }))
        .then((r) => live && setQuar(r.data))
        .catch((e) => live && setError(apiErrorMessage(e)))
        .finally(() => live && setLoading(false));
    } else {
      // Deep link (from a global-search result) and typed term compose: the term
      // narrows inside the link, it does not replace it.
      api
        .get(
          withQuery("/stockledger/on-hand", {
            group_by: group,
            sku: skuFilter,
            brand: brandFilter,
            q,
            store: storeCode,
          }),
          storeId === undefined ? undefined : { headers: { "X-KDPS-Unit": storeId } },
        )
        .then((r) => live && setData(r.data))
        .catch((e) => live && setError(apiErrorMessage(e)))
        .finally(() => live && setLoading(false));
    }
    return () => {
      live = false;
    };
  }, [group, qStore, qBrand, reloadKey, skuFilter, brandFilter, q, storeCode, storeId]);

  // The reports still waiting on someone — drafts, which is flagged and
  // rejected both; a confirmed one has posted and shows up as quarantine
  // instead. Asked for by docstatus so the confirmed history never travels.
  // Only the store's own are visible to it (the list is store-scoped
  // server-side), so this is exactly "what I reported that has not been
  // actioned yet".
  // A failure here is said out loud rather than swallowed: an empty list and a
  // list that would not load look identical on screen, and the store would read
  // the silence as "my report was actioned".
  useEffect(() => {
    if (group !== "quarantine") return;
    setFlagsErr("");
    api
      .get("/outbound/mark-damaged?docstatus=0")
      .then((r) => setOpenFlags(r.data as DamageFlagT[]))
      .catch((e) => {
        setOpenFlags([]);
        setFlagsErr(apiErrorMessage(e));
      });
  }, [group, reloadKey]);

  // Refresh the filter option lists from the full (unfiltered) quarantine set
  // whenever we enter the tab or the data changes.
  useEffect(() => {
    if (group !== "quarantine") return;
    api
      .get("/stockledger/quarantine")
      .then((r) => {
        const rows: QuarRowT[] = r.data.rows ?? [];
        const stores = new Map<string, string>();
        const brands = new Set<string>();
        for (const row of rows) {
          stores.set(row.store_code, row.store_name);
          if (row.brand) brands.add(row.brand);
        }
        setQuarOpts({
          stores: [...stores.entries()].sort((a, b) => a[0].localeCompare(b[0])),
          brands: [...brands].sort(),
        });
      })
      .catch(() => {
        /* option lists are best-effort */
      });
  }, [group, reloadKey]);

  // "Mark damaged" is a global action wherever stock is visible. Clicking it
  // opens a stepper so the exact count is set BEFORE anything posts; on confirm
  // the piece moves from free-to-sell into quarantine there and then.
  function openDamage(row: RowT) {
    if (row.store_id == null) return;
    setDmgRow(row);
    setDmgQty(1);
    setDmgErr("");
    setDmgBusy(false);
  }

  // Clamp any requested count into [1, sellable] — a piece can't be under- or
  // over-quarantined against what's free-to-sell.
  function clampQty(n: number): number {
    if (!dmgRow) return 1;
    return Math.min(dmgRow.net_qty, Math.max(1, Math.trunc(n)));
  }

  function bumpQty(delta: number) {
    if (!dmgRow) return;
    setDmgQty((q) => clampQty(q + delta));
  }

  async function confirmDamage() {
    if (!dmgRow || dmgRow.store_id == null) return;
    const qty = clampQty(dmgQty);
    setDmgErr("");
    setDmgBusy(true);
    try {
      const { data: mark } = await api.post<DamageFlagT>("/outbound/mark-damaged", {
        store: dmgRow.store_id,
        scans: [{ barcode: dmgRow.sku_code, qty }],
      });
      setFlash(
        mark.flag_status === "confirmed"
          ? `Moved ${qty} × ${dmgRow.sku_code} into quarantine at ${dmgRow.store_code}.`
          : `Reported ${qty} × ${dmgRow.sku_code} as damaged at ${dmgRow.store_code}. ` +
              "They stay sellable until the warehouse confirms it.",
      );
      setDmgRow(null);
      setReloadKey((k) => k + 1);
    } catch (e) {
      setDmgErr(apiErrorMessage(e));
    } finally {
      setDmgBusy(false);
    }
  }

  const isQuar = group === "quarantine";
  const groupings = groupingsFor(view);
  // Hosted, this panel is only ever one half, so its header can speak for that
  // half. Standing alone it is one screen with a strip you switch on, and the
  // header must stay the screen's - unchanged from before the fold.
  const hostedQuar = view === "quarantine";
  const cards = isQuar
    ? [
        {
          icon: ShieldAlert,
          label: "Units quarantined",
          value: stockQuantity(quar?.summary.units_quarantined, loading),
        },
        {
          icon: IndianRupee,
          label: "Quarantine value",
          value: <StockValue paise={quar?.summary.value_paise} />,
        },
        {
          icon: Layers,
          label: "Quarantine lines",
          value: stockQuantity(quar?.summary.lines, loading),
        },
      ]
    : [
        {
          icon: Boxes,
          label: "Units on hand",
          value: stockQuantity(data?.summary.units_on_hand, loading),
        },
        {
          icon: IndianRupee,
          label: "Stock value",
          value: <StockValue paise={data?.summary.value_paise} />,
        },
        {
          icon: Layers,
          label: group === "store" ? "Stores" : group === "brand" ? "Brands" : "SKU lines",
          value: stockQuantity(data?.summary.lines, loading),
        },
      ];

  const emptyQuar = !quar || quar.rows.length === 0;
  const emptyOnHand = !data || data.rows.length === 0;
  const dmgValue = useMemo(() => {
    if (!dmgRow) return null;
    return dmgQty === dmgRow.net_qty ? "all remaining" : `${dmgQty} of ${dmgRow.net_qty}`;
  }, [dmgRow, dmgQty]);
  const Frame = workspace ? "div" : OperationsPage;

  return (
    <Frame>
      {!workspace && (
        <PageHeader
          lead={
            hostedQuar
              ? "Pieces held back from sale, and the damage reports still waiting on somebody."
              : "The live net position, from the stock ledger."
          }
          actions={
            !hostedQuar && (
              <>
                {/* V-flip is an ownership correction, not a daily job - so it is an
                  action here inside Stock rather than a line in the sidebar (#87). */}
                <Link className="btn" to="/stock/vflips" data-testid="vflip-link">
                  <Repeat size={16} /> V-Flip
                </Link>
                <Link className="btn" to="/stock/history" data-testid="stock-ledger-link">
                  <ScrollText size={16} /> Movement History
                </Link>
              </>
            )
          }
        />
      )}

      {/* Hosted on Inventory, Quarantine is a tab of its own and the strip is
          only the three groupings - or nothing at all, on the damage tab. */}
      {groupings.length > 1 && (
        <div className="seg" data-testid="onhand-tabs">
          {groupings.map((t) => (
            <button
              key={t.key}
              className={`seg-btn ${group === t.key ? "active" : ""}`}
              onClick={() => setGroup(t.key)}
              data-testid={`onhand-tab-${t.key}`}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}

      {flash && (
        <div className="ok-note" data-testid="onhand-flash">
          <CheckCircle2 size={16} /> {flash}
          <span className="spacer" />
          <button onClick={() => setFlash("")} aria-label="Dismiss">
            <X size={15} />
          </button>
        </div>
      )}

      <div className="stat-grid" data-testid="onhand-summary">
        {cards.map((c) => (
          <div className="card stat-card" key={c.label}>
            <c.icon size={18} style={{ color: "var(--rust)" }} />
            <div className="stat-value mono">{c.value}</div>
            <div className="stat-label">{c.label}</div>
          </div>
        ))}
      </div>

      {!isQuar && data?.summary.scope === "current_access" && (
        <p className="muted" data-testid="onhand-scope-note">
          Totals cover the stock you may currently access.
          {data.summary.identity_complete === false &&
            " Some stock has unresolved item identities."}
          {data.summary.value_complete === false &&
            " A complete valuation is unavailable for this selection."}
        </p>
      )}

      {!isQuar && !workspace && (
        <div className="filter-bar" data-testid="onhand-search-bar">
          <SearchBox
            value={q}
            onChange={setQ}
            placeholder="Search or scan — style, barcode, brand"
            label="Search stock"
            testId="onhand-search"
          />
        </div>
      )}

      {!isQuar && deepFilter && (
        <div className="filter-bar" data-testid="onhand-deep-filter">
          <span className={`chip chip-navy ${skuFilter ? "mono" : ""}`}>
            {skuFilter ? `Barcode ${skuFilter}` : `Brand ${brandFilter}`}
          </span>
          <button
            className="btn btn-sm"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("sku");
              next.delete("brand");
              setParams(next, { replace: true });
            }}
            data-testid="onhand-deep-filter-clear"
          >
            <X size={14} /> Show all stock
          </button>
        </div>
      )}

      {isQuar && (
        <div className="filter-bar" data-testid="quarantine-filters">
          <select
            className="select"
            value={qStore}
            onChange={(e) => setQStore(e.target.value)}
            data-testid="quarantine-filter-store"
          >
            <option value="">All stores</option>
            {quarOpts.stores.map(([code, name]) => (
              <option key={code} value={code}>
                {code} — {name}
              </option>
            ))}
          </select>
          <select
            className="select"
            value={qBrand}
            onChange={(e) => setQBrand(e.target.value)}
            data-testid="quarantine-filter-brand"
          >
            <option value="">All brands</option>
            {quarOpts.brands.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
          {(qStore || qBrand) && (
            <button
              className="btn btn-sm"
              onClick={() => {
                setQStore("");
                setQBrand("");
              }}
              data-testid="quarantine-filter-clear"
            >
              Clear filters
            </button>
          )}
        </div>
      )}

      {isQuar && !loading && !error && flagsErr && (
        <div className="warn-note" style={{ marginTop: 16 }} data-testid="damage-flags-error">
          Could not load the damage reports waiting to be confirmed — {flagsErr}
        </div>
      )}

      {/* A sibling of the quarantine table, not one of its states: a store can
          have reports waiting whether or not anything has reached quarantine. */}
      {isQuar && !loading && !error && openFlags.length > 0 && (
        <div className="card section-card" style={{ marginTop: 16 }} data-testid="damage-flags">
          <h3 className="h3" style={{ marginBottom: 4 }}>
            <ShieldAlert
              size={16}
              style={{ color: "var(--rust)", verticalAlign: "-2px", marginRight: 6 }}
            />
            Damage reported, not in quarantine
          </h3>
          <p className="stat-label" style={{ marginBottom: 12 }}>
            These pieces are still sellable — either waiting for a warehouse or HO person to confirm
            the report, or looked at and sent back as sellable.
          </p>
          <div
            className="table-wrap kdps-scroll"
            role="region"
            aria-label="Earlier damage reports"
            tabIndex={0}
          >
            <table className="data kdps-table" data-testid="damage-flags-table">
              <thead>
                <tr>
                  <th>Barcode (SKU)</th>
                  <th>Brand</th>
                  <th>Design</th>
                  <th>Colour</th>
                  <th>Size</th>
                  <th>Store</th>
                  <th className="num">Units</th>
                  <th>Reported by</th>
                  <th>When</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {openFlags.flatMap((f) =>
                  f.lines.map((l, j) => (
                    <tr key={`${f.id}-${j}`} data-testid={`damage-flag-${f.id}-${j}`}>
                      <td className="mono">{l.sku_code}</td>
                      <td>{l.brand}</td>
                      <td>{l.design}</td>
                      <td>{l.color}</td>
                      <td>{l.size}</td>
                      <td>{f.store_code}</td>
                      <td className="num" style={{ fontWeight: 700 }}>
                        {l.qty}
                      </td>
                      <td>{f.created_by_name || "—"}</td>
                      <td>{fmtApprovalWhen(f.created_at)}</td>
                      <td>
                        {f.flag_status === "rejected"
                          ? `Not damaged — ${f.approval?.reason ?? ""}`
                          : "Waiting to be confirmed"}
                      </td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {loading ? (
        <p className="lead">Loading…</p>
      ) : error ? (
        <div className="warn-note" data-testid="onhand-error">
          {error}
        </div>
      ) : isQuar ? (
        emptyQuar ? (
          <div className="card section-card" data-testid="quarantine-empty">
            {qStore || qBrand
              ? "No quarantined stock matches these filters."
              : "Nothing in quarantine. “Mark damaged” on any SKU reports a piece; a warehouse or HO person's confirmation moves it here."}
          </div>
        ) : (
          <div
            className="table-wrap kdps-scroll"
            style={{ marginTop: 16 }}
            role="region"
            aria-label="Earlier quarantined stock"
            tabIndex={0}
          >
            <table className="data kdps-table" data-testid="quarantine-table">
              <thead>
                <tr>
                  <th>Barcode (SKU)</th>
                  <th>Brand</th>
                  <th>Design</th>
                  <th>Colour</th>
                  <th>Size</th>
                  <th>Season</th>
                  <th>Store</th>
                  <th className="num">Units</th>
                  <th className="num">Value ₹</th>
                  <th>Marked by</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {quar!.rows.map((r, i) => (
                  <tr key={i} data-testid={`quarantine-row-${i}`}>
                    <td className="mono">{r.sku_code}</td>
                    <td>{r.brand}</td>
                    <td>{r.design}</td>
                    <td>{r.color}</td>
                    <td>{r.size}</td>
                    <td>{r.season}</td>
                    <td>{r.store_code}</td>
                    <td className="num" style={{ fontWeight: 700 }}>
                      {r.qty}
                    </td>
                    <td className="num mono">
                      <StockValue paise={r.value_paise} />
                    </td>
                    <td>{r.marked_by ?? "—"}</td>
                    <td>{r.marked_at ? new Date(r.marked_at).toLocaleString("en-IN") : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : emptyOnHand ? (
        <div className="card section-card" data-testid="onhand-empty">
          {q
            ? `Nothing matching “${q}” in any location you can see.`
            : deepFilter
              ? `No stock of ${deepFilter} in any location you can see.`
              : "No stock on hand in your current scope. Reviewed receipts become inventory after physical acceptance."}
        </div>
      ) : (
        <div
          className="table-wrap kdps-scroll"
          style={{ marginTop: 16 }}
          role="region"
          aria-label="Stock on hand"
          tabIndex={0}
        >
          {data!.summary.truncated && (
            <div
              className="warn-note"
              data-testid="onhand-truncated-banner"
              style={{ marginBottom: 10 }}
            >
              Showing the first {data!.summary.displayed ?? data!.rows.length} of{" "}
              {data!.summary.lines} lines. Filter by store or brand to narrow the view.
            </div>
          )}
          <table className="data kdps-table" data-testid="onhand-table">
            <thead>
              <tr>
                {group === "sku" && (
                  <>
                    <th>Barcode (SKU)</th>
                    <th>Brand</th>
                    <th>Design</th>
                    <th>Colour</th>
                    <th>Size</th>
                    <th>Item</th>
                    <th>Season</th>
                    <th>Store</th>
                  </>
                )}
                {group === "brand" && (
                  <>
                    <th>Brand</th>
                    <th>Store</th>
                    <th className="num">SKUs</th>
                  </>
                )}
                {group === "store" && (
                  <>
                    <th>Store</th>
                    <th>Name</th>
                    <th className="num">SKUs</th>
                  </>
                )}
                <th className="num">Units</th>
                <th className="num">Value ₹</th>
                {group === "sku" && <th />}
              </tr>
            </thead>
            <tbody>
              {data!.rows.map((r, i) => (
                <tr key={i} data-testid={`onhand-row-${i}`}>
                  {group === "sku" && (
                    <>
                      <td className="mono">{r.sku_code}</td>
                      <td>{r.brand}</td>
                      <td>{r.design}</td>
                      <td>{r.color}</td>
                      <td>{r.size}</td>
                      <td>{r.item}</td>
                      <td>{r.season}</td>
                      <td>{r.store_code}</td>
                    </>
                  )}
                  {group === "brand" && (
                    <>
                      <td>
                        <b>{r.brand}</b>
                      </td>
                      <td>{r.store_code}</td>
                      <td className="num">{r.skus}</td>
                    </>
                  )}
                  {group === "store" && (
                    <>
                      <td className="mono">{r.store_code}</td>
                      <td>{r.store_name}</td>
                      <td className="num">{r.skus}</td>
                    </>
                  )}
                  <td className="num" style={{ fontWeight: 700 }}>
                    {r.net_qty}
                  </td>
                  <td className="num mono">
                    <StockValue paise={r.net_value_paise} />
                  </td>
                  {group === "sku" && (
                    <td>
                      {r.record_contract === "goods-v1" ? (
                        <Link
                          className="btn btn-sm"
                          to={withQuery("/goods/stock", { site_id: r.store_id })}
                          data-testid={`canonical-stock-${i}`}
                        >
                          Review stock
                        </Link>
                      ) : (
                        <button
                          className="btn btn-sm"
                          onClick={() => openDamage(r)}
                          data-testid={`mark-damaged-${i}`}
                          title="Report a piece as damaged"
                        >
                          <ShieldAlert size={14} /> Mark damaged
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Mark-damaged confirm dialog — set the exact count, then post. */}
      {dmgRow && (
        <div
          className="modal-backdrop"
          data-testid="mark-damaged-modal"
          onClick={() => !dmgBusy && setDmgRow(null)}
        >
          <div className="modal" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h3 className="h3">
                <ShieldAlert
                  size={17}
                  style={{ color: "var(--rust)", verticalAlign: "-3px", marginRight: 6 }}
                />
                Mark damaged
              </h3>
              <button
                type="button"
                className="btn"
                onClick={() => setDmgRow(null)}
                disabled={dmgBusy}
              >
                Cancel
              </button>
            </div>

            <p className="lead" style={{ marginBottom: 6 }}>
              <b className="mono">{dmgRow.sku_code}</b> · {dmgRow.brand}
            </p>
            <p className="stat-label" style={{ marginBottom: 18 }}>
              {[dmgRow.design, dmgRow.color, dmgRow.size].filter(Boolean).join(" · ")} — at{" "}
              <b>{dmgRow.store_code}</b>, {dmgRow.net_qty} sellable
            </p>

            <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 8 }}>
              <span className="stat-label">Pieces damaged</span>
              <div className="qty-stepper" data-testid="mark-damaged-stepper">
                <button
                  type="button"
                  onClick={() => bumpQty(-1)}
                  disabled={dmgBusy || dmgQty <= 1}
                  data-testid="mark-damaged-dec"
                  aria-label="Decrease"
                >
                  <Minus size={16} />
                </button>
                <input
                  type="number"
                  min={1}
                  max={dmgRow.net_qty}
                  value={dmgQty}
                  data-testid="mark-damaged-qty"
                  onChange={(e) => {
                    const n = Number(e.target.value);
                    if (Number.isFinite(n)) setDmgQty(clampQty(n));
                  }}
                />
                <button
                  type="button"
                  onClick={() => bumpQty(1)}
                  disabled={dmgBusy || dmgQty >= dmgRow.net_qty}
                  data-testid="mark-damaged-inc"
                  aria-label="Increase"
                >
                  <Plus size={16} />
                </button>
              </div>
              <span className="stat-label">{dmgValue}</span>
            </div>

            <p className="stat-label" style={{ marginBottom: 18 }}>
              These pieces stay owned and at the store. A store's report is checked by the warehouse
              before they stop being free-to-sell; a warehouse or HO person's takes them out of
              sellable stock at once.
            </p>

            {dmgErr && (
              <div className="warn-note" data-testid="mark-damaged-error">
                {dmgErr}
              </div>
            )}

            <button
              className="btn btn-cta btn-lg"
              style={{ marginTop: 16 }}
              disabled={dmgBusy}
              onClick={confirmDamage}
              data-testid="mark-damaged-confirm"
            >
              <ShieldAlert size={16} /> {dmgBusy ? "Moving…" : `Mark ${dmgQty} damaged`}
            </button>
          </div>
        </div>
      )}
    </Frame>
  );
}
