// Running Offers - what is live at this site today, and which pieces it reaches
// (OPS-10, store and warehouse operations PRD §11).
//
// The Promotions list says what rules exist. This says what they *do to my
// stock*, which is the question a store manager actually opens the screen with:
// "the brand's 40% - which of my racks does it come off?"
//
// Three things it is careful about.
//
//   · **The items are the engine's answer, not the screen's.** The server runs
//     `covers()` - the same test checkout runs before it prices a line - over the
//     site's own working set. Nothing here filters or re-decides applicability;
//     the search box narrows what is *shown*, and says how many it narrowed from.
//   · **A card says what the rule asks for separately from what it gives.** A
//     spend ladder covers a piece without discounting a bill that never reached
//     the threshold, so "reaches these pieces" and "gives this" are two lines,
//     never one sentence that would read as a promise.
//   · **A cut list says it was cut.** A storewide rule at a real store reaches
//     the whole shelf; the server sends the first few hundred and the true count,
//     and this says so rather than letting a short list read as a complete one.
import { useMemo, useState } from "react";
import { CalendarClock, Layers, Search, Tag } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { useDoc } from "../lib/hooks";
import { formatINR } from "../lib/format";
import { withQuery } from "../lib/query";
import "./OffersPrice.css";

interface RunningItem {
  barcode: string;
  description: string;
  season: string;
  mrp_paise: number;
}

interface RunningOffer {
  id: number;
  name: string;
  /** `company`, or `brand:<code>` - who the rule belongs to. */
  source: string;
  brand: string;
  layer: string;
  starts_on: string;
  ends_on: string | null;
  combinable: boolean;
  priority: number;
  reward: string;
  trigger: string;
  items_count: number;
  items_truncated: boolean;
  items: RunningItem[];
}

interface Site {
  id: number;
  code: string;
  name: string;
  store_type: string;
}

interface Running {
  site: Site | null;
  sites: Site[];
  day: string;
  offers: RunningOffer[];
}

const LAYER_LABEL: Record<string, string> = {
  brand: "Brand offer",
  storewide: "Storewide",
  bank: "Bank / tender",
};

function fmtDay(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** "No end date" is a real and common state, not a missing value (D5 Q5). */
function when(row: RunningOffer): string {
  const from = fmtDay(row.starts_on);
  return row.ends_on ? `${from} → ${fmtDay(row.ends_on)}` : `${from} → until stopped`;
}

/** Who the rule belongs to, in the words a shop floor uses. */
function sourceLabel(row: RunningOffer): string {
  return row.source === "company" ? "Company offer" : row.brand || row.source;
}

export function RunningOffersPage() {
  const [site, setSite] = useState<number | null>(null);
  const url = withQuery("/offers/running", site === null ? {} : { site: String(site) });
  const { data, loading } = useDoc<Running>(url);

  const offers = data?.offers ?? [];
  const sites = data?.sites ?? [];

  return (
    <div className="page-pad">
      <PageHeader
        lead="Every rule running here today, and the pieces on this site's own shelf it applies to. The list is the counter's own answer: a piece here is a piece the till would discount under this rule."
        actions={
          // Only where there is a choice to make. One site is not a picker.
          sites.length > 1 && (
            <label className="field" style={{ maxWidth: 240 }}>
              <span className="eyebrow">Site</span>
              <select
                className="input"
                value={String(data?.site?.id ?? "")}
                onChange={(e) => setSite(Number(e.target.value))}
                data-testid="running-site"
              >
                {sites.map((s) => (
                  <option key={s.id} value={String(s.id)}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </label>
          )
        }
      />

      {loading ? (
        <p className="lead">Loading…</p>
      ) : offers.length === 0 ? (
        <div className="card section-card" data-testid="running-empty">
          <p className="eyebrow">
            <Tag size={15} /> Nothing running
          </p>
          No offer applies at this site on {data ? fmtDay(data.day) : "this day"}.
        </div>
      ) : (
        <div data-testid="running-offers">
          {offers.map((row) => (
            <OfferCard key={row.id} row={row} />
          ))}
        </div>
      )}
    </div>
  );
}

function OfferCard({ row }: { row: RunningOffer }) {
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");

  const shown = useMemo(() => {
    const needle = term.trim().toLowerCase();
    if (!needle) return row.items;
    return row.items.filter(
      (item) =>
        item.barcode.toLowerCase().includes(needle) ||
        item.description.toLowerCase().includes(needle) ||
        item.season.toLowerCase().includes(needle),
    );
  }, [row.items, term]);

  return (
    <div className="card section-card" data-testid={`running-offer-${row.id}`}>
      <div className="toolbar">
        <div>
          <p className="eyebrow">
            <Tag size={13} /> <span data-testid={`running-source-${row.id}`}>{sourceLabel(row)}</span>
          </p>
          <h3 className="h3">{row.name}</h3>
          <p className="lead" data-testid={`running-reward-${row.id}`}>
            Gives {row.reward}. Asks: {row.trigger}.
          </p>
        </div>
        <span className="spacer" />
        <div style={{ textAlign: "right" }}>
          <span className="chip chip-grey">
            <Layers size={12} /> {LAYER_LABEL[row.layer] ?? row.layer}
          </span>{" "}
          <span
            className={`chip chip-${row.combinable ? "blue" : "grey"}`}
            data-testid={`running-stacks-${row.id}`}
          >
            {row.combinable ? "Stacks on top" : "On its own"}
          </span>
          <p className="hint">
            <CalendarClock size={12} /> {when(row)}
          </p>
        </div>
      </div>

      <button
        type="button"
        className="btn btn-sm"
        onClick={() => setOpen((v) => !v)}
        data-testid={`running-toggle-${row.id}`}
      >
        {open ? "Hide" : "Show"} {row.items_count} piece{row.items_count === 1 ? "" : "s"}
      </button>

      {open && (
        <>
          <label className="field" style={{ maxWidth: 320, marginTop: 10 }}>
            <span className="eyebrow">
              <Search size={13} /> Barcode, description or season
            </span>
            <input
              className="input"
              value={term}
              onChange={(e) => setTerm(e.target.value)}
              data-testid={`running-search-${row.id}`}
            />
          </label>

          {row.items_truncated && (
            <p className="hint" data-testid={`running-truncated-${row.id}`}>
              This rule reaches {row.items_count} pieces; the first {row.items.length} are
              listed. Search for the one you are holding.
            </p>
          )}

          {shown.length === 0 ? (
            <p className="hint">Nothing here matches “{term}”.</p>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid={`running-items-${row.id}`}>
                <thead>
                  <tr>
                    <th>Barcode</th>
                    <th>Piece</th>
                    <th>Season</th>
                    <th className="num">MRP</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((item) => (
                    <tr key={`${item.barcode}-${item.season}`}>
                      <td>{item.barcode}</td>
                      <td>{item.description}</td>
                      <td>{item.season}</td>
                      <td className="num">{formatINR(item.mrp_paise)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default RunningOffersPage;
