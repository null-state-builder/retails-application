// Discounts — what the chain gave away, and what it forgot to give (D11 §5, §8).
//
// Four questions, in the order they get asked:
//
//   1. how much came off the ticket this month, and under which rule;
//   2. how much of it a *person* decided rather than a rule — the counter's own
//      latitude, with the person named, because "₹40,000 of manual discount" is a
//      shrug and "₹31,000 of it on one salesman's bills" is a conversation;
//   3. which brands it landed on;
//   4. **what was owed and never given** — eligible discount the till did not
//      apply, read off the nightly continuity flags, on the true bill and the true
//      date. This is the honest half of the month-end conversation (D11 R4): the
//      report states sales exactly as billed, and this is how value is recovered
//      without restating one of them.
//
// Read from `/api/sell/discounts`: the facts are bills, and the rulebook is not
// allowed to import the counter. The gate is still `offers_price: view`.
//
// Scope is the reader's own: a store manager sees their shop, head office sees the
// network. That is the store-scope rule the whole system obeys, not a filter this
// screen invented.

import { useState } from "react";
import { AlertTriangle, BadgePercent, HandCoins, Receipt } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { useDoc } from "../lib/hooks";
import "./Booking.css";
import "./OffersPrice.css";

interface Report {
  from: string;
  to: string;
  totals: {
    gross_paise: number;
    disc_paise: number;
    net_paise: number;
    lines: number;
    bills: number;
    discount_pct: string | null;
  };
  by_brand: { brand: string; disc_paise: number; net_paise: number; lines: number }[];
  by_offer: {
    offer_id: number;
    name: string;
    funder: string;
    layer: string;
    disc_paise: number;
    lines: number;
  }[];
  keyed_in: {
    disc_paise: number;
    lines: number;
    by_person: { store_code: string; name: string; disc_paise: number; lines: number }[];
  };
  not_applied: {
    missed_paise: number;
    open_flags: number;
    rows: {
      flag_id: number;
      bill_number: string;
      billed_on: string;
      store_code: string;
      missed_paise: number;
      status: string;
    }[];
  };
}

function money(paise: number | null | undefined): string {
  return `₹${Math.round((paise ?? 0) / 100).toLocaleString("en-IN")}`;
}

function iso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

const TODAY = new Date();

/** The three windows people actually ask for. */
const PRESETS: { key: string; label: string; from: () => string; to: () => string }[] = [
  {
    key: "30",
    label: "Last 30 days",
    from: () => iso(new Date(TODAY.getTime() - 29 * 86400000)),
    to: () => iso(TODAY),
  },
  {
    key: "month",
    label: "This month",
    from: () => iso(new Date(TODAY.getFullYear(), TODAY.getMonth(), 1)),
    to: () => iso(TODAY),
  },
  {
    key: "last",
    label: "Last month",
    from: () => iso(new Date(TODAY.getFullYear(), TODAY.getMonth() - 1, 1)),
    to: () => iso(new Date(TODAY.getFullYear(), TODAY.getMonth(), 0)),
  },
];

export function DiscountsPage() {
  const [preset, setPreset] = useState(PRESETS[0]);
  const { data, loading } = useDoc<Report>(
    `/sell/discounts?from=${preset.from()}&to=${preset.to()}`,
  );

  return (
    <div className="page-pad">
      <PageHeader lead="Every rupee that came off a ticket, and every rupee that should have. The rules did most of it; where a person decided instead, they are named - and where the till missed an offer a piece was entitled to, it is listed on its own bill, on its own date." />

      <div className="filter-bar">
        {PRESETS.map((p) => (
          <button
            key={p.key}
            type="button"
            className={`chip chip-pick${preset.key === p.key ? " chip-navy" : ""}`}
            onClick={() => setPreset(p)}
            data-testid={`discounts-window-${p.key}`}
          >
            {p.label}
          </button>
        ))}
        {data && (
          <span className="hint">
            {data.from} → {data.to}
          </span>
        )}
      </div>

      {loading || !data ? (
        <p className="lead">Loading…</p>
      ) : (
        <>
          <div className="kpi-row" style={{ marginTop: 18 }}>
            <div className="card kpi" data-testid="kpi-discount">
              <p className="eyebrow">
                <BadgePercent size={13} /> Came off the ticket
              </p>
              <p className="kpi-value rust">{money(data.totals.disc_paise)}</p>
              <p className="kpi-note">
                {data.totals.discount_pct ?? "0"}% of {money(data.totals.gross_paise)} at MRP
              </p>
            </div>
            <div className="card kpi" data-testid="kpi-net">
              <p className="eyebrow">
                <Receipt size={13} /> Collected
              </p>
              <p className="kpi-value">{money(data.totals.net_paise)}</p>
              <p className="kpi-note">
                {data.totals.bills} bill{data.totals.bills === 1 ? "" : "s"} ·{" "}
                {data.totals.lines} line{data.totals.lines === 1 ? "" : "s"}
              </p>
            </div>
            <div className="card kpi" data-testid="kpi-keyed">
              <p className="eyebrow">
                <HandCoins size={13} /> A person decided
              </p>
              <p className="kpi-value">{money(data.keyed_in.disc_paise)}</p>
              <p className="kpi-note">
                keyed in at the counter, on {data.keyed_in.lines} line
                {data.keyed_in.lines === 1 ? "" : "s"}
              </p>
            </div>
            <div className="card kpi" data-testid="kpi-missed">
              <p className="eyebrow">
                <AlertTriangle size={13} /> Owed, not given
              </p>
              <p className="kpi-value rust">{money(data.not_applied.missed_paise)}</p>
              <p className="kpi-note">
                {data.not_applied.open_flags} bill
                {data.not_applied.open_flags === 1 ? "" : "s"} still open
              </p>
            </div>
          </div>

          <div className="card section-card" data-testid="discounts-by-offer">
            <p className="eyebrow">What each rule cost, and whose scheme it is</p>
            {data.by_offer.length === 0 ? (
              <p className="hint">No rule priced a line in this window.</p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Offer</th>
                      <th>Whose</th>
                      <th>Layer</th>
                      <th className="num">Lines</th>
                      <th className="num">Discount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_offer.map((row) => (
                      <tr key={row.offer_id} data-testid={`by-offer-${row.offer_id}`}>
                        <td>{row.name}</td>
                        <td>
                          <span
                            className={`chip chip-${row.funder === "brand" ? "blue" : "purple"}`}
                          >
                            {row.funder === "brand" ? "brand's scheme" : "KDPS markdown"}
                          </span>
                        </td>
                        <td>{row.layer}</td>
                        <td className="num tabular">{row.lines}</td>
                        <td className="num tabular">{money(row.disc_paise)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card section-card" data-testid="discounts-not-applied">
            <p className="eyebrow">
              <AlertTriangle size={13} /> Eligible, and not given
            </p>
            <p className="hint" style={{ marginTop: 0 }}>
              The rulebook priced these lines one way and the till charged another. Listed on the
              bill and the date they actually happened — nothing here is moved into a different
              month.
            </p>
            {data.not_applied.rows.length === 0 ? (
              <p className="hint" data-testid="not-applied-empty">
                Nothing missed in this window. Every eligible line got its offer.
              </p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Bill</th>
                      <th>Billed on</th>
                      <th>Store</th>
                      <th className="num">Not given</th>
                      <th>State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.not_applied.rows.map((row) => (
                      <tr key={row.flag_id} data-testid={`missed-${row.flag_id}`}>
                        <td className="mono">{row.bill_number || "—"}</td>
                        <td>{row.billed_on}</td>
                        <td>{row.store_code}</td>
                        <td className="num tabular">{money(row.missed_paise)}</td>
                        <td>
                          <span
                            className={`chip chip-${row.status === "open" ? "amber" : "green"}`}
                          >
                            {row.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card section-card" data-testid="discounts-keyed-in">
            <p className="eyebrow">
              <HandCoins size={13} /> Keyed in at the counter
            </p>
            {data.keyed_in.by_person.length === 0 ? (
              <p className="hint">Nobody keyed a discount in this window.</p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Store</th>
                      <th>Who sold it</th>
                      <th className="num">Lines</th>
                      <th className="num">Given</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.keyed_in.by_person.map((row, i) => (
                      <tr key={i} data-testid={`keyed-${i}`}>
                        <td>{row.store_code}</td>
                        <td>{row.name}</td>
                        <td className="num tabular">{row.lines}</td>
                        <td className="num tabular">{money(row.disc_paise)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card section-card" data-testid="discounts-by-brand">
            <p className="eyebrow">Where it landed, by brand</p>
            {data.by_brand.length === 0 ? (
              <p className="hint">No discount in this window.</p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Brand</th>
                      <th className="num">Lines</th>
                      <th className="num">Discount</th>
                      <th className="num">Collected</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_brand.map((row) => (
                      <tr key={row.brand} data-testid={`by-brand-${row.brand}`}>
                        <td>{row.brand}</td>
                        <td className="num tabular">{row.lines}</td>
                        <td className="num tabular">{money(row.disc_paise)}</td>
                        <td className="num tabular">{money(row.net_paise)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export default DiscountsPage;
