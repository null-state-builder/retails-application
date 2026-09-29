// Offers & Pricing - the rulebook, as the shop floor reads it (#183, D5).
//
// Read-only, deliberately and for now. Head office authors rules through
// `POST`/`PUT /api/offers`; a store sees what is running, what is coming and
// what a bill will do about it. Two things follow from that being a *store's*
// screen rather than an authoring one:
//
//   · It says the rule in a phrase, not in its dials. The phrase comes from the
//     server (`one_liner`), generated off the rule itself, because a summary
//     typed beside a rule drifts away from it and the summary is what a shop
//     floor believes.
//   · It says out loud which rules stack, because "can I give the bank discount
//     on top of the brand one?" is the question a counter actually asks, and the
//     answer is one column rather than a phone call to head office.
//
// The authoring screens are their own tickets; this is the surface the pilot
// store needs on day one.

import { CalendarClock, Layers, Plus, Tag } from "lucide-react";

import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { useAuth } from "../auth/AuthContext";
import { meetsCapability } from "../shell/navConfig";
import { useList } from "../lib/hooks";

interface OfferRow {
  id: number;
  name: string;
  brand: string;
  layer: string;
  one_liner: string;
  starts_on: string;
  ends_on: string | null;
  combinable: boolean;
  priority: number;
  is_fallback: boolean;
  status: string;
  stores: string[];
  awaiting_approval?: boolean;
}

const LAYER_LABEL: Record<string, string> = {
  brand: "Brand offer",
  storewide: "Storewide",
  bank: "Bank / tender",
};

/** Live is green, ended is grey, anything on its way is amber. */
const STATUS_TONE: Record<string, string> = {
  live: "green",
  draft: "amber",
  approved: "amber",
  ended: "grey",
};

function when(row: OfferRow): string {
  const from = fmtDay(row.starts_on);
  // "No end date" is a real and common state, not a missing value: a brand's
  // default offer rolls until somebody stops it (D5 Q5).
  return row.ends_on ? `${from} → ${fmtDay(row.ends_on)}` : `${from} → until stopped`;
}

function fmtDay(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export function OffersPage() {
  const { data, loading } = useList<OfferRow>("/offers/");
  const { user } = useAuth();
  // The author's own entrance, shown only to the rung that can actually write
  // one - a button that 403s is worse than no button.
  const mayAuthor = meetsCapability(user?.capabilities?.offers_price, "operate");

  return (
    <div className="page-pad">
      <PageHeader lead="Every rule your counter is pricing with, and the ones on their way. The till applies these itself as each piece is scanned, offline included - nothing here needs a decision, and nothing here is edited at a store." />

      {mayAuthor && (
        <div className="toolbar">
          <span className="spacer" />
          <Link className="btn btn-primary" to="/offers/new" data-testid="offer-new-cta">
            <Plus size={15} /> Write an offer
          </Link>
        </div>
      )}

      {loading ? (
        <p className="lead">Loading…</p>
      ) : data.length === 0 ? (
        <div className="card section-card" data-testid="offers-empty">
          <p className="eyebrow">
            <Tag size={15} /> Nothing running
          </p>
          No offers apply at this store today.
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="offers-table">
            <thead>
              <tr>
                <th>Offer</th>
                <th>Layer</th>
                <th>Running</th>
                <th>Stacks?</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <tr key={row.id} data-testid={`offer-row-${row.id}`}>
                  <td>
                    <p className="eyebrow">
                      <Tag size={13} /> {row.brand || "Storewide"}
                      {row.is_fallback && " · default"}
                    </p>
                    {mayAuthor ? (
                      <Link className="link-cell" to={`/offers/${row.id}`}>
                        {row.one_liner}
                      </Link>
                    ) : (
                      row.one_liner
                    )}
                    <span className="muted-cell" style={{ display: "block" }}>
                      {row.name}
                    </span>
                  </td>
                  <td>
                    <span className="chip chip-grey">
                      <Layers size={12} /> {LAYER_LABEL[row.layer] ?? row.layer}
                    </span>
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <CalendarClock size={12} style={{ marginRight: 4 }} />
                    {when(row)}
                  </td>
                  <td data-testid={`offer-stacks-${row.id}`}>
                    {row.combinable ? "Stacks on top" : "On its own"}
                  </td>
                  <td>
                    <span className={`chip chip-${STATUS_TONE[row.status] ?? "grey"}`}>
                      {row.awaiting_approval ? "waiting for approval" : row.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default OffersPage;
