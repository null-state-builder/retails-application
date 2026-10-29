// Transfers (OPS-06; store and warehouse operations PRD §7 and §12).
//
// One list of every movement touching this site, in or out, with chips for the
// stage it has reached. *In transit* is one of those chips and not a screen of
// its own, and *New transfer* is a button on this list rather than a "Send
// stock" page - both exactly as the PRD writes them.
//
// Nothing here is a second opinion about anything. Which movements exist, what
// is reserved, what is on the road and what each person may do are the
// server's answers; this screen shows them and opens the record where the work
// actually happens.
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowDownLeft, ArrowUpRight, Plus } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { canAccess } from "../auth/routeAccess";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { SearchBox } from "../components/SearchBox";
import { formatDateTime } from "../lib/format";
import { Field, listState, useAllPages } from "../lib/goodsScreen";
import {
  TRANSFER_FILTERS,
  TRANSFER_STATE_HELP,
  TRANSFER_STATE_LABEL,
  directionOf,
  matchesFilter,
  type TransferFilterKey,
  type TransferSummary,
} from "../lib/goodsTransfers";
import { NewTransferPanel } from "./TransferDetail";

const DIRECTIONS = [
  { key: "both", label: "Both ways" },
  { key: "out", label: "Sending" },
  { key: "in", label: "Receiving" },
] as const;

type DirectionKey = (typeof DIRECTIONS)[number]["key"];

/** The next step on a movement, in words, from where this site stands. Words
 *  rather than a button: the list carries no per-row grant, so it says what
 *  happens next and the record decides who may do it. */
function nextStep(state: string, way: "in" | "out"): string {
  switch (state) {
    case "draft":
      return way === "out" ? "Finish and submit" : "Being drafted";
    case "submitted":
      return "Waiting for approval";
    case "approved":
      return way === "out" ? "Pack and dispatch" : "Waiting for dispatch";
    case "dispatching":
      return way === "in" ? "Receive" : "On the road";
    default:
      return "—";
  }
}

function siteLabel(sites: { id: string; code: string; name: string }[], id: string | null): string {
  if (!id) return "—";
  const found = sites.find((site) => String(site.id) === String(id));
  return found ? `${found.name} (${found.code})` : `#${id}`;
}

export function TransfersPage() {
  const { user, session, featuresOn } = useAuth();
  const navigate = useNavigate();
  const sites = useMemo(() => session?.sites ?? [], [session]);
  const [siteId, setSiteId] = useState<string>(sites[0]?.id ?? "");
  const [filter, setFilter] = useState<TransferFilterKey>("all");
  const [direction, setDirection] = useState<DirectionKey>("both");
  const [drafting, setDrafting] = useState(false);
  const [term, setTerm] = useState("");

  const site = siteId || sites[0]?.id || "";
  // Every page, not the first: a movement waiting on somebody is a decision
  // they never make if it sits on page two. The chips then cut the whole list,
  // which is why they can cover two server states at once.
  const list = useAllPages<TransferSummary>(
    site ? `/goods-v1/outbound/transfers?site=${site}&limit=100` : null,
  );

  const thisWay = list.items.filter(
    (row) => direction === "both" || directionOf(row, site) === direction,
  );
  // How many of this direction's movements each stage chip holds. Only once the
  // whole list is in: a count of a list still loading, or one that failed, is
  // not a zero, so the chips then show no number at all.
  const counted = !list.loading && !list.failure && !list.denied;
  const rows = thisWay
    .filter((row) => matchesFilter(row, filter))
    .filter((row) => {
      if (!term.trim()) return true;
      const hay = [row.number ?? "", TRANSFER_STATE_LABEL[row.state] ?? row.state]
        .join(" ")
        .toLowerCase();
      return hay.includes(term.trim().toLowerCase());
    });

  const state = listState(
    { loading: list.loading, failure: list.failure, empty: rows.length === 0 },
    "No transfer here matches this filter.",
  );

  return (
    <OperationsPage>
      <PageHeader
        title="Transfers"
        lead="Stock moving between this site and another, in both directions, with the stage each movement has reached."
        actions={
          <>
            {/* The legacy records are off the menu (desktop redesign Q4) but
                stay readable until they retire (SO-03 C07): this is their way
                in, for whoever their own gate opens. */}
            {user && canAccess("/transfer", user, session?.display_actions ?? [], featuresOn) && (
              <Link className="btn" to="/transfer" data-testid="transfers-earlier">
                Earlier transfer records
              </Link>
            )}
            <button
              className="btn btn-cta"
              onClick={() => setDrafting((open) => !open)}
              data-testid="transfers-new"
            >
              <Plus size={14} /> New transfer
            </button>
          </>
        }
      />

      {drafting && site ? (
        <NewTransferPanel
          sourceSiteId={site}
          onDone={(id) => {
            setDrafting(false);
            navigate(`/goods/transfers/${id}`);
          }}
          onCancel={() => setDrafting(false)}
        />
      ) : null}

      <div className="toolbar">
        {sites.length > 1 && (
          <Field id="transfers-site" label="Site">
            <select
              id="transfers-site"
              className="select"
              value={site}
              onChange={(e) => setSiteId(e.target.value)}
              data-testid="transfers-site"
            >
              {sites.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
          </Field>
        )}
        {/* Three choices, all visible: a switch, not a dropdown to open. */}
        <div
          className="seg transfers-direction"
          role="group"
          aria-label="Direction"
          data-testid="transfers-direction"
        >
          {DIRECTIONS.map((d) => (
            <button
              key={d.key}
              type="button"
              className={`seg-btn ${direction === d.key ? "active" : ""}`}
              aria-pressed={direction === d.key}
              onClick={() => setDirection(d.key)}
              data-testid={`transfers-direction-${d.key}`}
            >
              {d.label}
            </button>
          ))}
        </div>
        <SearchBox
          value={term}
          onChange={setTerm}
          placeholder="Transfer number"
          label="Search these transfers"
          testId="transfers-search"
        />
      </div>

      {/* The stage counts are the filter (mockup Transfers): each tile's number
          is exactly what pressing it shows. Never the page's red: the one red
          here is New transfer. A count is shown only once the whole list is in. */}
      <div
        className="transfers-stages"
        role="group"
        aria-label="Stage"
        data-testid="transfers-chips"
      >
        {TRANSFER_FILTERS.map((chip) => (
          <button
            key={chip.key}
            type="button"
            className="transfers-stage"
            onClick={() => setFilter(chip.key)}
            aria-pressed={filter === chip.key}
            data-testid={`transfers-chip-${chip.key}`}
          >
            <span className="transfers-stage-label">{chip.label}</span>
            <span className="transfers-stage-n">
              {counted ? thisWay.filter((row) => matchesFilter(row, chip.key)).length : "—"}
            </span>
          </button>
        ))}
      </div>

      {list.denied ? (
        <div className="card section-card" data-testid="transfers-denied">
          <h3 className="h3">There are no transfers here for you</h3>
          <p className="lead">
            Either this site does not exist, or it is outside what you may see.
          </p>
        </div>
      ) : (
        (state ?? (
          <div className="table-wrap capped">
            <table className="data" data-testid="transfers-table">
              <caption className="sr-only">
                Transfers touching this site, each with its direction, stage and what is on the
                road.
              </caption>
              <thead>
                <tr>
                  <th>Way</th>
                  <th>Number</th>
                  <th>Other site</th>
                  <th>Stage</th>
                  <th className="num">Shipments</th>
                  <th className="num">On the road</th>
                  <th>Started</th>
                  <th>Next step</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const way = directionOf(row, site);
                  const other = way === "out" ? row.destination_site_id : row.source_site_id;
                  return (
                    <tr
                      key={row.id}
                      data-testid={`transfer-row-${row.id}`}
                      data-state={row.state}
                      onClick={() => navigate(`/goods/transfers/${row.id}`)}
                      style={{ cursor: "pointer" }}
                    >
                      <td>
                        <span className="chip chip-navy">
                          {way === "out" ? (
                            <>
                              <ArrowUpRight size={13} /> Sending
                            </>
                          ) : (
                            <>
                              <ArrowDownLeft size={13} /> Receiving
                            </>
                          )}
                        </span>
                      </td>
                      <td>
                        <button
                          className="btn btn-sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            navigate(`/goods/transfers/${row.id}`);
                          }}
                          data-testid={`transfer-open-${row.id}`}
                        >
                          {row.number ?? "Not numbered yet"}
                        </button>
                        {row.custody === "quarantine" ? (
                          <span
                            className="chip chip-amber"
                            title="Held goods that stay in quarantine at both sites"
                            data-testid={`transfer-custody-${row.id}`}
                          >
                            Quarantine
                          </span>
                        ) : row.custody === "pre_pt" ? (
                          <span
                            className="chip chip-amber"
                            title="Damaged goods not yet on a PT: held at both sites, value unknown"
                            data-testid={`transfer-custody-${row.id}`}
                          >
                            Pre-PT damaged
                          </span>
                        ) : null}
                      </td>
                      <td>{siteLabel([...sites, row.source_site, row.destination_site], other)}</td>
                      <td data-testid={`transfer-state-${row.id}`}>
                        <span
                          className={`chip chip-${row.state === "completed" ? "green" : "amber"}`}
                        >
                          {TRANSFER_STATE_LABEL[row.state] ?? row.state}
                        </span>
                        <span className="muted"> {TRANSFER_STATE_HELP[row.state]}</span>
                      </td>
                      <td className="num">{row.dispatch_count}</td>
                      <td className="num">{row.in_transit_qty}</td>
                      <td>{formatDateTime(row.created_at)}</td>
                      <td className="transfers-next" data-testid={`transfer-next-${row.id}`}>
                        {nextStep(row.state, way === "out" ? "out" : "in")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))
      )}
    </OperationsPage>
  );
}

export default TransfersPage;
