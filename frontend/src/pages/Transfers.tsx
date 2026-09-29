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
import { useNavigate } from "react-router-dom";
import { ArrowDownLeft, ArrowUpRight, Plus } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
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

function siteLabel(
  sites: { id: string; code: string; name: string }[],
  id: string | null,
): string {
  if (!id) return "—";
  const found = sites.find((site) => String(site.id) === String(id));
  return found ? `${found.name} (${found.code})` : `#${id}`;
}

export function TransfersPage() {
  const { session } = useAuth();
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

  const rows = list.items
    .filter((row) => matchesFilter(row, filter))
    .filter((row) => direction === "both" || directionOf(row, site) === direction)
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
    <div className="page-pad">
      <PageHeader
        title="Transfers"
        lead="Stock moving between this site and another, in both directions, with the stage each movement has reached."
        actions={
          <button
            className="btn btn-cta"
            onClick={() => setDrafting((open) => !open)}
            data-testid="transfers-new"
          >
            <Plus size={14} /> New transfer
          </button>
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
        <Field id="transfers-direction" label="Direction">
          <select
            id="transfers-direction"
            className="select"
            value={direction}
            onChange={(e) => setDirection(e.target.value as DirectionKey)}
            data-testid="transfers-direction"
          >
            {DIRECTIONS.map((d) => (
              <option key={d.key} value={d.key}>
                {d.label}
              </option>
            ))}
          </select>
        </Field>
        <div className="spacer" />
        <SearchBox
          value={term}
          onChange={setTerm}
          placeholder="Transfer number"
          label="Search these transfers"
          testId="transfers-search"
        />
      </div>

      <div className="toolbar" data-testid="transfers-chips">
        {TRANSFER_FILTERS.map((chip) => (
          <button
            key={chip.key}
            className={`btn btn-sm${filter === chip.key ? " btn-cta" : ""}`}
            onClick={() => setFilter(chip.key)}
            aria-pressed={filter === chip.key}
            data-testid={`transfers-chip-${chip.key}`}
          >
            {chip.label}
          </button>
        ))}
      </div>

      {list.denied ? (
        <div className="card section-card" data-testid="transfers-denied">
          <p className="eyebrow">Not found</p>
          <h3 className="h3">There are no transfers here for you</h3>
          <p className="lead">
            Either this site does not exist, or it is outside what you may see.
          </p>
        </div>
      ) : (
        state ?? (
          <div className="table-wrap">
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
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
}

export default TransfersPage;
