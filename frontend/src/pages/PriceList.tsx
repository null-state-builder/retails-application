// Price List — the half of the module that did not exist (D11 §4, G3).
//
// The MRP was a column that whichever PT last touched a barcode overwrote in
// place, so "what was this piece priced at on the day that bill was made" had no
// answer. This screen is the answer: the ticket, what it cost to land, the margin
// between them, whether the piece may be discounted at all — and, behind each
// barcode, every movement of its ticket with the person who moved it.
//
// Two things are deliberate:
//
//   · **As-of, not just now.** Pick a past date and the list answers as of that
//     day, from the trail rather than from the column.
//   · **Re-ticketing asks for a reason and refuses to go below cost.** The refusal
//     names the cost, because "violates constraint" is not a sentence anybody can
//     act on.

import { useState } from "react";
import { History, IndianRupee, Printer, Search, Tag } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import { useDoc } from "../lib/hooks";
import { TagPrintModal } from "./TagPrint";
import "./Booking.css";
import "./OffersPrice.css";

interface Row {
  barcode: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  item: string;
  mrp_paise: number | null;
  mrp_then_paise: number | null;
  moved_since: boolean;
  cost_paise: number | null;
  season: string;
  margin_pct: string | null;
  no_discount: boolean;
}

interface Change {
  id: number;
  effective_from: string;
  from_paise: number | null;
  to_paise: number;
  source: string;
  source_label: string;
  doc_number: string;
  reason: string;
  changed_by_name: string;
}

interface Detail extends Row {
  cohorts: { season: string; unit_cost_paise: number; mrp_paise: number | null; last_doc_number: string }[];
  history: Change[];
}

function money(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "—";
  return `₹${(paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export function PriceListPage() {
  const [term, setTerm] = useState("");
  const [asOf, setAsOf] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [printingRows, setPrintingRows] = useState<Row[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const query = `/offers/price-list?q=${encodeURIComponent(term)}${asOf ? `&as_of=${asOf}` : ""}`;
  const { data, loading, reload } = useDoc<{
    as_of: string;
    count: number;
    truncated: boolean;
    rows: Row[];
  }>(query);

  function toggleSelected(barcode: string) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(barcode)) next.delete(barcode);
      else next.add(barcode);
      return next;
    });
  }

  return (
    <div className="page-pad">
      <PageHeader lead="What every piece is ticketed at, what it cost to land, and what the margin between them is. Pick a date to read the list as it stood then - a bill from last month can only be explained by last month's ticket." />

      <div className="toolbar">
        <label className="field" style={{ maxWidth: 320 }}>
          <span className="eyebrow">
            <Search size={13} /> Barcode, style, item or brand
          </span>
          <input
            className="input"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="e.g. PE or 8901"
            data-testid="price-search"
          />
        </label>
        <label className="field" style={{ maxWidth: 200 }}>
          <span className="eyebrow">Priced as on</span>
          <input
            type="date"
            className="input"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            data-testid="price-as-of"
          />
        </label>
        <span className="spacer" />
        {selected.size > 0 && (
          <button
            type="button"
            className="btn btn-cta"
            onClick={() => setPrintingRows((data?.rows || []).filter((r) => selected.has(r.barcode)))}
            data-testid="price-print-selected"
          >
            <Printer size={14} /> Print {selected.size} selected tag{selected.size === 1 ? "" : "s"}
          </button>
        )}
        {data && (
          <span className="chip chip-grey" data-testid="price-count">
            {data.count} piece{data.count === 1 ? "" : "s"}
            {data.truncated ? " (first page)" : ""}
          </span>
        )}
      </div>

      {loading ? (
        <p className="lead">Loading…</p>
      ) : !data || data.rows.length === 0 ? (
        <div className="card section-card" data-testid="price-empty">
          <p className="eyebrow">
            <Tag size={15} /> Nothing matches
          </p>
          No piece answers to that. Try a barcode, a style code or a brand.
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="price-table">
            <thead>
              <tr>
                <th />
                <th>Piece</th>
                <th>Season</th>
                <th className="num">Cost</th>
                <th className="num">Ticket</th>
                <th className="num">Margin</th>
                <th>Discountable</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.barcode} data-testid={`price-row-${row.barcode}`}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(row.barcode)}
                      onChange={() => toggleSelected(row.barcode)}
                      data-testid={`price-select-${row.barcode}`}
                    />
                  </td>
                  <td>
                    <p className="eyebrow">{row.brand || "(unbranded)"}</p>
                    {row.design} {row.color} {row.size}
                    <span className="muted-cell mono" style={{ display: "block" }}>
                      {row.barcode}
                    </span>
                  </td>
                  <td>{row.season || "—"}</td>
                  <td className="num tabular">{money(row.cost_paise)}</td>
                  <td className="num tabular">
                    {money(row.mrp_then_paise ?? row.mrp_paise)}
                    {row.moved_since && (
                      <span className="muted-cell" style={{ display: "block" }}>
                        now {money(row.mrp_paise)}
                      </span>
                    )}
                  </td>
                  <td className="num tabular">{row.margin_pct ? `${row.margin_pct}%` : "—"}</td>
                  <td>
                    {row.no_discount ? (
                      <span className="chip chip-red" data-testid={`price-nod-${row.barcode}`}>
                        never discounted
                      </span>
                    ) : (
                      <span className="muted-cell">yes</span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => setOpen(row.barcode)}
                      data-testid={`price-open-${row.barcode}`}
                    >
                      <History size={14} /> Ticket &amp; trail
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm"
                      style={{ marginLeft: 6 }}
                      onClick={() => setPrintingRows([row])}
                      data-testid={`price-print-tag-${row.barcode}`}
                    >
                      <Printer size={13} /> Print tag
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <PriceDetail
          barcode={open}
          onClose={() => setOpen(null)}
          onChanged={() => {
            reload();
          }}
        />
      )}
      {printingRows && <TagPrintModal rows={printingRows} onClose={() => setPrintingRows(null)} />}
    </div>
  );
}

function PriceDetail({
  barcode,
  onClose,
  onChanged,
}: {
  barcode: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { data, loading, reload } = useDoc<Detail>(`/offers/price-list/${barcode}`);
  const [next, setNext] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  async function reprice() {
    setBusy(true);
    setError("");
    setDone("");
    try {
      await api.post(`/offers/price-list/${barcode}/reprice`, {
        mrp: next,
        reason,
      });
      setDone("Ticket moved, and the trail below says who moved it and why.");
      setNext("");
      setReason("");
      reload();
      onChanged();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} data-testid="price-detail">
        <div className="modal-head">
          <div>
            <p className="eyebrow">
              <Tag size={14} /> {data?.brand || "Piece"} · {barcode}
            </p>
            <h2 className="h2">
              {data ? `${data.design} ${data.color} ${data.size}`.trim() : "Loading…"}
            </h2>
          </div>
          <button type="button" className="btn" onClick={onClose} data-testid="price-close">
            Close
          </button>
        </div>

        {loading || !data ? (
          <p className="lead">Loading…</p>
        ) : (
          <>
            <div className="price-head">
              <div>
                <p className="eyebrow">Ticket now</p>
                <p className="price-figure" data-testid="price-figure">
                  {money(data.mrp_paise)}
                </p>
                <p className="price-sub">
                  landed at {money(data.cost_paise)}
                  {data.margin_pct ? ` · ${data.margin_pct}% on the ticket, before GST` : ""}
                </p>
              </div>
              <div>
                <p className="eyebrow">Cohorts holding it</p>
                {data.cohorts.length === 0 ? (
                  <p className="price-sub">None inwarded yet.</p>
                ) : (
                  data.cohorts.map((c, i) => (
                    <p className="price-sub" key={i}>
                      {c.season || "—"} · cost {money(c.unit_cost_paise)} · {c.last_doc_number || "no doc"}
                    </p>
                  ))
                )}
              </div>
            </div>

            <div className="card section-card" style={{ marginTop: 18 }}>
              <p className="eyebrow">
                <IndianRupee size={13} /> Move the ticket
              </p>
              <div className="form-row">
                <label className="field">
                  New ticket ₹
                  <input
                    className="input num"
                    value={next}
                    onChange={(e) => setNext(e.target.value)}
                    data-testid="reprice-value"
                  />
                </label>
                <label className="field">
                  Why it is moving
                  <input
                    className="input"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="AW26 revised tag from the brand"
                    data-testid="reprice-reason"
                  />
                </label>
              </div>
              <button
                type="button"
                className="btn btn-primary"
                style={{ marginTop: 12 }}
                disabled={busy || !next || reason.trim().length < 3}
                onClick={reprice}
                data-testid="reprice-submit"
              >
                Re-ticket this piece
              </button>
              <p className="hint">
                Never below what it cost to land, and never without a reason - both are refused, and
                the refusal says the figure it is refusing against.
              </p>
              {error && (
                <div className="warn-note" data-testid="reprice-error">
                  {error}
                </div>
              )}
              {done && (
                <div className="ai-note" data-testid="reprice-done">
                  {done}
                </div>
              )}
            </div>

            <p className="eyebrow" style={{ marginTop: 18 }}>
              <History size={13} /> Every movement of this ticket
            </p>
            {data.history.length === 0 ? (
              <p className="hint" data-testid="price-trail-empty">
                Nothing recorded yet. The trail starts the first time this ticket moves — a PT that
                re-states the same figure is not a price change.
              </p>
            ) : (
              <div className="table-wrap">
                <table className="data" data-testid="price-trail">
                  <thead>
                    <tr>
                      <th>From</th>
                      <th>Was → became</th>
                      <th>Why</th>
                      <th>Who</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.history.map((change) => (
                      <tr key={change.id}>
                        <td>{change.effective_from}</td>
                        <td className="tabular">
                          <span className="trail-from">{money(change.from_paise)}</span> →{" "}
                          <b>{money(change.to_paise)}</b>
                        </td>
                        <td>
                          {change.source_label}
                          {change.doc_number ? ` · ${change.doc_number}` : ""}
                          {change.reason ? (
                            <span className="muted-cell" style={{ display: "block" }}>
                              {change.reason}
                            </span>
                          ) : null}
                        </td>
                        <td>{change.changed_by_name || "system"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default PriceListPage;
