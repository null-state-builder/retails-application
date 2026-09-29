// Distribution grid — Ops Head enters qty-per-store for stock that has arrived
// at a warehouse, and this bulk-creates one draft `store_split` transfer per
// destination store. Nothing new posts underneath it: every draft goes through
// the same approval → scan-and-dispatch → receive pipeline as a transfer raised
// one at a time on "Send Stock" — this screen only saves the Ops Head from
// doing that N times for one arrived batch.
//
// Rows are styles (`design`) — a style's size × colour breakup opens inside
// its row, one line per barcode, each with its own per-store cells and a
// warehouse **buffer** cell for stock explicitly held back rather than sent.
// The pre-fill (#73's v1: last split for the brand, weighted by each store's
// own size mix) comes from `/outbound/distribution/suggested-split`; every
// cell stays hand-editable afterward, and re-split re-asks the server rather
// than reusing a stale answer.
import { Fragment, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  XCircle,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useList } from "../lib/hooks";
import { PageHeader } from "../components/PageHeader";
import { destinationOptions as filterDestinations, type LocationT } from "../lib/transfer-locations";

interface StoreT {
  id: number;
  code: string;
  name: string;
  store_type: string;
  gstin: number;
  is_partner: boolean;
  is_active: boolean;
}

interface StockRow {
  store_code: string;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  qty: number;
  is_own: boolean;
}

interface SkuLine {
  sku_code: string;
  color: string;
  size: string;
  available: number;
  qtyByDest: Record<string, string>;
  buffer: string;
}

interface StyleGroup {
  /** `brand::design` — two brands can share a design string, and the id is
   *  what every handler matches on, never `design` alone. */
  id: string;
  design: string;
  brand: string;
  lines: SkuLine[];
  expanded: boolean;
}

function styleId(brand: string, design: string): string {
  return `${brand}::${design}`;
}

/** Coerces a typed cell to the non-negative integer it must always resolve
 *  to — a blank, a negative, or a decimal all read as "nothing typed here"
 *  rather than skewing a total or slipping a fractional qty to the API. */
function toQty(val: string): number {
  const n = Math.trunc(Number(val));
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/** size (or `_default`) → destination store id → fraction of that size's
 *  qty the store should get. Mirrors `outbound.distribution.suggest_split`'s
 *  return shape exactly — see that module for what "last split" means. */
type SplitWeights = Record<string, Record<string, number>>;
const DEFAULT_WEIGHT_KEY = "_default";

function lineTotal(line: SkuLine): number {
  return (
    Object.values(line.qtyByDest).reduce((s, v) => s + toQty(v), 0) + toQty(line.buffer)
  );
}

function styleTotals(group: StyleGroup, destIds: string[]) {
  const available = group.lines.reduce((s, l) => s + l.available, 0);
  const buffer = group.lines.reduce((s, l) => s + toQty(l.buffer), 0);
  const byDest: Record<string, number> = {};
  for (const d of destIds) {
    byDest[d] = group.lines.reduce((s, l) => s + toQty(l.qtyByDest[d] || ""), 0);
  }
  const allocated = group.lines.reduce((s, l) => s + lineTotal(l), 0);
  const over = group.lines.some((l) => lineTotal(l) > l.available);
  return { available, buffer, byDest, allocated, over };
}

/** A starting point, not a decision: split what's available as evenly as the
 *  arithmetic allows across the stores picked so far, so the grid opens with a
 *  sane guess instead of a wall of blanks — Ops Head edits any cell freely
 *  from there. The remainder (available % stores) goes to the earliest-picked
 *  stores, which is as good a tie-break as any and at least a stable one. */
function equalSplit(available: number, destIds: string[]): Record<string, string> {
  if (destIds.length === 0 || available <= 0) return {};
  const base = Math.floor(available / destIds.length);
  const remainder = available % destIds.length;
  const out: Record<string, string> = {};
  destIds.forEach((d, i) => {
    const qty = base + (i < remainder ? 1 : 0);
    if (qty > 0) out[d] = String(qty);
  });
  return out;
}

/** The suggested fill for one line: the size's weight if the server has ever
 *  seen this brand dispatched to these stores, else `_default`'s (that
 *  store's overall share across every size), else an even split. Whatever
 *  floor() leaves unallocated goes to the warehouse buffer — rounding never
 *  invents or drops a piece. */
function suggestLine(available: number, size: string, destIds: string[], weights: SplitWeights) {
  const sizeWeights = weights[size] || weights[DEFAULT_WEIGHT_KEY];
  if (!sizeWeights || destIds.length === 0 || available <= 0) {
    return { qtyByDest: equalSplit(available, destIds), buffer: "" };
  }
  const qtyByDest: Record<string, string> = {};
  let allocated = 0;
  for (const d of destIds) {
    const qty = Math.floor(available * (sizeWeights[d] ?? 0));
    if (qty > 0) {
      qtyByDest[d] = String(qty);
      allocated += qty;
    }
  }
  const remainder = available - allocated;
  return { qtyByDest, buffer: remainder > 0 ? String(remainder) : "" };
}

export function DistributionGridPage() {
  const navigate = useNavigate();
  // The source picker asks "which warehouse may I operate from" — the scoped
  // list. The destination picker asks "where in the network may stock go" —
  // deliberately the *unscoped* one (`/masters/locations`, #147): a warehouse
  // person scoped to their own store alone would otherwise see zero
  // destinations and the grid would be unusable for exactly the role it was
  // built for. Never collapse these back onto one list (test_masters_locations.py).
  const { data: stores } = useList<StoreT>("/masters/stores");
  const { data: locations } = useList<LocationT>("/masters/locations");
  const warehouses = stores.filter((s) => s.store_type === "warehouse");

  const [sourceId, setSourceId] = useState("");
  const [destIds, setDestIds] = useState<string[]>([]);
  const [ewayBills, setEwayBills] = useState<Record<string, string>>({});
  const [term, setTerm] = useState("");
  const [results, setResults] = useState<StockRow[]>([]);
  const [searching, setSearching] = useState(false);
  const [styles, setStyles] = useState<StyleGroup[]>([]);
  const [weightsCache, setWeightsCache] = useState<Record<string, SplitWeights>>({});
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState<{ dest: string; ok: boolean; detail: string }[] | null>(null);

  const source = stores.find((s) => String(s.id) === sourceId) || null;
  // Every other store in the network is a candidate destination — a partner
  // franchisee is not excluded, that is the whole point of the flag — and a
  // warehouse is never itself a distribution target.
  const destinationOptions = filterDestinations(locations, sourceId).filter(
    (l) => l.store_type === "store",
  );

  function toggleDest(id: string) {
    const removing = destIds.includes(id);
    setDestIds((ds) => (removing ? ds.filter((d) => d !== id) : [...ds, id]));
    // A de-selected store's cells would otherwise stay in `qtyByDest`,
    // invisible but still counted in every total and the over-stock check.
    if (removing) {
      setStyles((gs) =>
        gs.map((g) => ({
          ...g,
          lines: g.lines.map((l) => {
            if (!(id in l.qtyByDest)) return l;
            const rest = { ...l.qtyByDest };
            delete rest[id];
            return { ...l, qtyByDest: rest };
          }),
        })),
      );
    }
  }

  function crossState(dest: LocationT): boolean {
    return !!source && source.gstin !== dest.gstin;
  }

  async function search() {
    if (!source || !term.trim()) { setResults([]); return; }
    setSearching(true);
    try {
      const { data } = await api.get<{ rows: StockRow[] }>(
        `/outbound/stock-search?store=${source.code}&q=${encodeURIComponent(term.trim())}`,
      );
      setResults(data.rows.filter((r) => r.qty > 0));
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  }

  function weightsKey(brand: string): string {
    return `${brand}::${[...destIds].sort().join(",")}`;
  }

  /** The last-known weights for `brand` at the currently-picked destinations,
   *  fetched once and cached — a re-split asks fresh rather than trusting the
   *  cache, since that is the one moment staleness would matter. */
  async function weightsFor(brand: string): Promise<SplitWeights> {
    const key = weightsKey(brand);
    if (weightsCache[key]) return weightsCache[key];
    if (!source || destIds.length === 0 || !brand) return {};
    try {
      const { data } = await api.get<{ weights: SplitWeights }>(
        `/outbound/distribution/suggested-split?warehouse=${source.id}&brand=${encodeURIComponent(brand)}&stores=${destIds.join(",")}`,
      );
      setWeightsCache((c) => ({ ...c, [key]: data.weights || {} }));
      return data.weights || {};
    } catch {
      return {};
    }
  }

  async function addRow(r: StockRow) {
    if (styles.some((g) => g.lines.some((l) => l.sku_code === r.sku_code))) return;
    const design = r.design || r.brand || r.sku_code;
    const id = styleId(r.brand, design);
    const weights = await weightsFor(r.brand);
    const line: SkuLine = {
      sku_code: r.sku_code,
      color: r.color,
      size: r.size,
      available: r.qty,
      ...suggestLine(r.qty, r.size, destIds, weights),
    };
    setStyles((gs) => {
      const idx = gs.findIndex((g) => g.id === id);
      if (idx === -1) return [...gs, { id, design, brand: r.brand, lines: [line], expanded: true }];
      const next = [...gs];
      next[idx] = { ...next[idx], lines: [...next[idx].lines, line], expanded: true };
      return next;
    });
    setResults([]);
    setTerm("");
  }

  function removeLine(id: string, sku: string) {
    setStyles((gs) =>
      gs
        .map((g) => (g.id === id ? { ...g, lines: g.lines.filter((l) => l.sku_code !== sku) } : g))
        .filter((g) => g.lines.length > 0),
    );
  }

  function removeStyle(id: string) {
    setStyles((gs) => gs.filter((g) => g.id !== id));
  }

  function toggleStyle(id: string) {
    setStyles((gs) => gs.map((g) => (g.id === id ? { ...g, expanded: !g.expanded } : g)));
  }

  /** Re-split asks the server fresh rather than trusting the cache — the one
   *  moment staleness would matter, e.g. after a newer allocation landed
   *  elsewhere since this screen was opened. A failed fetch leaves both the
   *  cache and the rows untouched rather than quietly resolving to an even
   *  split that looks like a real answer. */
  async function resplitStyle(id: string) {
    const group = styles.find((g) => g.id === id);
    if (!group || !source || !group.brand) return;
    let fresh: SplitWeights;
    try {
      const { data } = await api.get<{ weights: SplitWeights }>(
        `/outbound/distribution/suggested-split?warehouse=${source.id}&brand=${encodeURIComponent(group.brand)}&stores=${destIds.join(",")}`,
      );
      fresh = data.weights || {};
    } catch (e) {
      setError(`Couldn't refresh the suggestion for ${group.design}: ${apiErrorMessage(e)}`);
      return;
    }
    setWeightsCache((c) => ({ ...c, [weightsKey(group.brand)]: fresh }));
    setStyles((gs) =>
      gs.map((g) =>
        g.id === id
          ? { ...g, lines: g.lines.map((l) => ({ ...l, ...suggestLine(l.available, l.size, destIds, fresh) })) }
          : g,
      ),
    );
  }

  function setQty(id: string, sku: string, dest: string, val: string) {
    setStyles((gs) =>
      gs.map((g) =>
        g.id === id
          ? {
              ...g,
              lines: g.lines.map((l) =>
                l.sku_code === sku ? { ...l, qtyByDest: { ...l.qtyByDest, [dest]: val } } : l,
              ),
            }
          : g,
      ),
    );
  }

  function setBuffer(id: string, sku: string, val: string) {
    setStyles((gs) =>
      gs.map((g) =>
        g.id === id
          ? { ...g, lines: g.lines.map((l) => (l.sku_code === sku ? { ...l, buffer: val } : l)) }
          : g,
      ),
    );
  }

  async function submit() {
    setError("");
    setOutcome(null);
    if (!source) { setError("Select a source warehouse."); return; }
    if (destIds.length === 0) { setError("Select at least one destination store."); return; }
    const allLines = styles.flatMap((g) => g.lines);
    for (const line of allLines) {
      if (lineTotal(line) > line.available) {
        setError(`${line.sku_code}: allocated (${lineTotal(line)}) exceeds available (${line.available}).`);
        return;
      }
    }
    const activeDests = destIds.filter((d) => allLines.some((l) => toQty(l.qtyByDest[d] || "") > 0));
    if (activeDests.length === 0) { setError("Enter at least one quantity for a selected store."); return; }
    for (const d of activeDests) {
      const dest = destinationOptions.find((s) => String(s.id) === d);
      if (dest && crossState(dest) && !(ewayBills[d] || "").trim()) {
        setError(`${dest.code} is cross-state — an e-way bill number is required for it.`);
        return;
      }
    }

    setSaving(true);
    const results = await Promise.allSettled(
      activeDests.map((d) => {
        const dest = destinationOptions.find((s) => String(s.id) === d)!;
        const lines = allLines
          .map((l) => ({ sku_code: l.sku_code, qty_planned: toQty(l.qtyByDest[d] || "") }))
          .filter((l) => l.qty_planned > 0);
        return api.post("/outbound/transfers", {
          source_store: source.id,
          destination_store: dest.id,
          transfer_type: source.store_type === "warehouse" ? "store_split" : "inter_store",
          reason: "warehouse_allocation",
          eway_bill_number: ewayBills[d] || "",
          lines,
        }).then((r) => ({ dest: dest.code, ok: true, detail: r.data.doc_number || `Draft #${r.data.id}` }));
      }),
    );
    setOutcome(
      results.map((r, i) => {
        const dest = destinationOptions.find((s) => String(s.id) === activeDests[i])!;
        return r.status === "fulfilled"
          ? r.value
          : { dest: dest.code, ok: false, detail: apiErrorMessage((r as PromiseRejectedResult).reason) };
      }),
    );
    setSaving(false);
  }

  return (
    <div className="page-pad">
      <Link to="/transfer" className="btn" style={{ marginBottom: 16 }} data-testid="distribution-back-link">
        <ArrowLeft size={15} /> Transfers
      </Link>
      <PageHeader
        title="Distribution grid"
        lead="Split one arrived batch across many stores in one pass — enter qty per store, and each store gets its own draft transfer to approve, dispatch and receive as usual."
      />

      {outcome && (
        <div className="card section-card" style={{ marginBottom: 18 }} data-testid="distribution-outcome">
          <p className="eyebrow">Drafts created</p>
          {outcome.map((o) => (
            <div key={o.dest} style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6 }} data-testid={`distribution-outcome-${o.dest}`}>
              {o.ok ? <CheckCircle2 size={15} color="var(--green)" /> : <XCircle size={15} color="var(--red)" />}
              <b className="mono">{o.dest}</b> — {o.detail}
            </div>
          ))}
          <div className="toolbar" style={{ marginTop: 14 }}>
            <button className="btn btn-cta" onClick={() => navigate("/transfer")} data-testid="distribution-view-transfers">
              View transfers
            </button>
          </div>
        </div>
      )}

      <div className="card section-card">
        <p className="eyebrow">Step 1 · Source warehouse</p>
        <select
          className="select"
          style={{ marginTop: 10, maxWidth: 320 }}
          value={sourceId}
          onChange={(e) => { setSourceId(e.target.value); setDestIds([]); setStyles([]); setWeightsCache({}); setOutcome(null); }}
          data-testid="distribution-source-select"
        >
          <option value="">Select warehouse…</option>
          {warehouses.map((w) => <option key={w.id} value={w.id}>{w.code} · {w.name}</option>)}
        </select>
      </div>

      {source && (
        <div className="card section-card">
          <p className="eyebrow">Step 2 · Destination stores</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 10 }}>
            {destinationOptions.map((s) => (
              <label key={s.id} className="check-row" data-testid={`distribution-dest-${s.code}`}>
                <input type="checkbox" checked={destIds.includes(String(s.id))} onChange={() => toggleDest(String(s.id))} />
                {s.code}
              </label>
            ))}
          </div>
          {destIds.some((d) => { const s = destinationOptions.find((x) => String(x.id) === d); return s && crossState(s); }) && (
            <div style={{ marginTop: 14 }}>
              <p className="eyebrow">E-way bill (cross-state destinations)</p>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 8 }}>
                {destIds.map((d) => {
                  const s = destinationOptions.find((x) => String(x.id) === d);
                  if (!s || !crossState(s)) return null;
                  return (
                    <input
                      key={d}
                      className="input"
                      placeholder={`${s.code} e-way bill no.`}
                      value={ewayBills[d] || ""}
                      onChange={(e) => setEwayBills({ ...ewayBills, [d]: e.target.value })}
                      data-testid={`distribution-ewaybill-${s.code}`}
                    />
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {source && destIds.length > 0 && (
        <div className="card section-card">
          <p className="eyebrow">Step 3 · Add arrived SKUs</p>
          <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
            <input
              className="input"
              placeholder="Search barcode, design, brand…"
              value={term}
              onChange={(e) => setTerm(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void search()}
              data-testid="distribution-sku-search"
            />
            <button className="btn" onClick={() => void search()} disabled={searching} data-testid="distribution-sku-search-btn">
              {searching ? "Searching…" : "Search"}
            </button>
          </div>
          {results.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 10 }}>
              <table className="data">
                <tbody>
                  {results.map((r) => (
                    <tr key={r.sku_code} data-testid={`distribution-result-${r.sku_code}`}>
                      <td><b className="mono">{r.sku_code}</b></td>
                      <td>{[r.design, r.color, r.size].filter(Boolean).join(" · ") || r.brand}</td>
                      <td className="num">{r.qty} available</td>
                      <td><button className="btn btn-sm" onClick={() => void addRow(r)} data-testid={`distribution-add-${r.sku_code}`}><Plus size={13} /> Add</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {styles.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 18 }}>
              <p className="muted-cell" style={{ marginBottom: 8 }}>
                Every style opens with the suggested split — last time this brand went to these stores,
                weighted by each store's own size mix — pre-filled, with whatever is left over held in
                the Buffer column. Edit any cell, or
                <RefreshCw size={11} style={{ verticalAlign: "-1px", margin: "0 3px" }} />
                re-split a style back to the suggestion. Click a style to open its size · colour breakup.
              </p>
              <table className="data" data-testid="distribution-grid-table">
                <thead>
                  <tr>
                    <th>Style / SKU</th>
                    <th className="num">Available</th>
                    {destIds.map((d) => {
                      const s = destinationOptions.find((x) => String(x.id) === d);
                      return <th key={d} className="num">{s?.code}</th>;
                    })}
                    <th className="num">Buffer</th>
                    <th className="num">Allocated</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {styles.map((group) => {
                    const totals = styleTotals(group, destIds);
                    return (
                      <Fragment key={group.id}>
                        <tr data-testid={`distribution-style-${group.design}`}>
                          <td>
                            <button
                              type="button"
                              className="btn btn-sm"
                              onClick={() => toggleStyle(group.id)}
                              aria-expanded={group.expanded}
                              data-testid={`distribution-style-toggle-${group.design}`}
                            >
                              {group.expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                            </button>{" "}
                            <b>{group.design}</b>{" "}
                            <span className="muted-cell">
                              ({group.lines.length} {group.lines.length === 1 ? "line" : "lines"})
                            </span>
                          </td>
                          <td className="num">{totals.available}</td>
                          {destIds.map((d) => <td key={d} className="num">{totals.byDest[d] || 0}</td>)}
                          <td className="num">{totals.buffer}</td>
                          <td className="num">
                            <b style={totals.over ? { color: "var(--red)" } : undefined}>{totals.allocated}</b>
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn btn-sm"
                              title="Re-split this style to the suggestion"
                              onClick={() => void resplitStyle(group.id)}
                              data-testid={`distribution-resplit-${group.design}`}
                            >
                              <RefreshCw size={13} />
                            </button>
                            <button
                              type="button"
                              className="btn btn-sm"
                              onClick={() => removeStyle(group.id)}
                              data-testid={`distribution-remove-style-${group.design}`}
                            >
                              <Trash2 size={13} />
                            </button>
                          </td>
                        </tr>
                        {group.expanded && group.lines.map((line) => {
                          const total = lineTotal(line);
                          const over = total > line.available;
                          return (
                            <tr key={line.sku_code} data-testid={`distribution-line-${line.sku_code}`}>
                              <td style={{ paddingLeft: 28 }}>
                                <b className="mono">{line.sku_code}</b>{" "}
                                <span className="muted-cell">{[line.color, line.size].filter(Boolean).join(" · ")}</span>
                              </td>
                              <td className="num">{line.available}</td>
                              {destIds.map((d) => (
                                <td key={d} className="num">
                                  <input
                                    className="input"
                                    style={{ width: 64, textAlign: "right" }}
                                    inputMode="numeric"
                                    value={line.qtyByDest[d] || ""}
                                    onChange={(e) => setQty(group.id, line.sku_code, d, e.target.value)}
                                    data-testid={`distribution-qty-${line.sku_code}-${d}`}
                                  />
                                </td>
                              ))}
                              <td className="num">
                                <input
                                  className="input"
                                  style={{ width: 64, textAlign: "right" }}
                                  inputMode="numeric"
                                  value={line.buffer}
                                  onChange={(e) => setBuffer(group.id, line.sku_code, e.target.value)}
                                  data-testid={`distribution-buffer-${line.sku_code}`}
                                />
                              </td>
                              <td className="num">
                                <b style={over ? { color: "var(--red)" } : undefined}>{total}</b>
                              </td>
                              <td>
                                <button
                                  className="btn btn-sm"
                                  onClick={() => removeLine(group.id, line.sku_code)}
                                  data-testid={`distribution-remove-${line.sku_code}`}
                                >
                                  <Trash2 size={13} />
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {error && <p className="warn-note" style={{ marginTop: 14 }} data-testid="distribution-error">{error}</p>}

          <div className="toolbar" style={{ marginTop: 18 }}>
            <span className="spacer" />
            <button
              type="button"
              className="btn btn-cta"
              disabled={saving || styles.length === 0}
              onClick={() => void submit()}
              data-testid="distribution-submit"
            >
              <Send size={15} /> {saving ? "Creating drafts…" : "Create transfers"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default DistributionGridPage;
