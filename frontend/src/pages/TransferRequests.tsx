// Requests (OPS-06; store and warehouse operations PRD §7 and §12's second
// Transfer tab).
//
// A store asks the warehouse for stock. That is all this is: asking reserves
// nothing, moves nothing, and does not oblige the sending site to anything.
// What it does do is make the asking a record with an author, a time and a
// list, instead of a phone call - and give the draft that answers it something
// to point back at.
import { useMemo, useState } from "react";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { Feedback, Field, listState, useAllPages, useGoodsFetch } from "../lib/goodsScreen";
import { REQUEST_STATE_LABEL, type TransferRequestRow } from "../lib/goodsTransfers";

const REQUESTS = "/goods-v1/outbound/transfer-requests";

interface StockRow {
  sku_id: string | null;
  description: string;
  transferable_qty: number;
}

interface AskRow {
  sku_id: string;
  description: string;
  available: number;
  qty: string;
}

export function TransferRequestsPage() {
  const { session } = useAuth();
  const sites = useMemo(() => session?.sites ?? [], [session]);
  const [siteId, setSiteId] = useState<string>(sites[0]?.id ?? "");
  const [sourceId, setSourceId] = useState<string>("");
  const [note, setNote] = useState("");
  const [asks, setAsks] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  const site = siteId || sites[0]?.id || "";
  const others = sites.filter((s) => String(s.id) !== String(site));
  const source = sourceId || others[0]?.id || "";

  const list = useAllPages<TransferRequestRow>(site ? `${REQUESTS}?site=${site}&limit=100` : null);
  // What the other site could actually send. A request for something nobody
  // has is a request nobody can answer, so the list is the sending site's own
  // transferable stock rather than a free-text product search.
  const stock = useGoodsFetch<{ items: StockRow[] }, AskRow[]>(
    source ? `/goods-v1/outbound/stock-search?source_site_id=${source}` : null,
    (r) =>
      (r.items ?? [])
        .filter((row) => row.sku_id && row.transferable_qty > 0)
        .map((row) => ({
          sku_id: String(row.sku_id),
          description: row.description,
          available: row.transferable_qty,
          qty: "",
        })),
    [],
  );

  const chosen = stock.value.filter((row) => Number(asks[row.sku_id]) > 0);

  async function raise() {
    setBusy(true);
    setError("");
    setOk("");
    try {
      await api.post(REQUESTS, {
        source_site_id: String(source),
        destination_site_id: String(site),
        note: note || undefined,
        lines: chosen.map((row) => ({
          line_key: crypto.randomUUID(),
          sku_id: row.sku_id,
          qty: Number(asks[row.sku_id]),
        })),
        ...goodsMeta(),
      });
      setAsks({});
      setNote("");
      setOk(
        "Asked. Nothing is reserved until the sending site drafts it and somebody approves it.",
      );
      list.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const state = listState(
    { loading: list.loading, failure: list.failure, empty: list.items.length === 0 },
    "Nobody has asked this site for anything, and this site has asked for nothing.",
  );

  return (
    <div className="page-pad">
      <PageHeader
        title="Requests"
        lead="Ask another site for stock. Asking reserves nothing — the sending site drafts a transfer, and its approval is what reserves the pieces."
      />
      <Feedback error={error} ok={ok} />

      <section className="card section-card" data-testid="request-new">
        <h3 className="h3">Ask for stock</h3>
        <div className="form-grid">
          {sites.length > 1 && (
            <Field id="request-site" label="For this site">
              <select
                id="request-site"
                className="select"
                value={site}
                onChange={(e) => setSiteId(e.target.value)}
                data-testid="request-site"
              >
                {sites.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field id="request-source" label="Ask">
            <select
              id="request-source"
              className="select"
              value={source}
              onChange={(e) => setSourceId(e.target.value)}
              data-testid="request-source"
            >
              {others.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
          </Field>
          <Field id="request-note" label="Note">
            <input
              id="request-note"
              className="input"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              data-testid="request-note"
            />
          </Field>
        </div>
        {listState(
          { loading: stock.loading, failure: stock.failure, empty: stock.value.length === 0 },
          "That site has nothing it can send right now.",
        ) ?? (
          <table data-testid="request-lines">
            <thead>
              <tr>
                <th>Item</th>
                <th className="num">They can send</th>
                <th className="num">Ask for</th>
              </tr>
            </thead>
            <tbody>
              {stock.value.map((row) => (
                <tr key={row.sku_id}>
                  <td>{row.description}</td>
                  <td className="num">{row.available}</td>
                  <td className="num">
                    <input
                      className="input"
                      type="number"
                      min={0}
                      value={asks[row.sku_id] ?? ""}
                      aria-label={`Ask for how many of ${row.description}`}
                      onChange={(e) =>
                        setAsks((current) => ({ ...current, [row.sku_id]: e.target.value }))
                      }
                      data-testid={`request-qty-${row.sku_id}`}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <button
          className="btn btn-cta"
          disabled={busy || !source || chosen.length === 0}
          onClick={raise}
          data-testid="request-raise"
        >
          Send this request
        </button>
      </section>

      {list.denied ? (
        <div className="card section-card" data-testid="requests-denied">
          <p className="eyebrow">Not found</p>
          <h3 className="h3">There are no requests here for you</h3>
          <p className="lead">
            Either this site does not exist, or it is outside what you may see.
          </p>
        </div>
      ) : (
        (state ?? (
          <div className="table-wrap">
            <table className="data" data-testid="requests-table">
              <caption className="sr-only">
                Requests this site has raised and requests other sites have raised of it.
              </caption>
              <thead>
                <tr>
                  <th>Asked</th>
                  <th>Asked of</th>
                  <th>For</th>
                  <th className="num">Lines</th>
                  <th>By</th>
                  <th>Where it got to</th>
                </tr>
              </thead>
              <tbody>
                {list.items.map((row) => (
                  <tr key={row.id} data-testid={`request-row-${row.id}`} data-state={row.state}>
                    <td>{formatDateTime(row.requested_at)}</td>
                    <td>{siteName(sites, row.source_site_id)}</td>
                    <td>{siteName(sites, row.destination_site_id)}</td>
                    <td className="num">{row.lines.length}</td>
                    <td>{row.requested_by.name || row.requested_by.id}</td>
                    <td data-testid={`request-state-${row.id}`}>
                      {REQUEST_STATE_LABEL[row.state] ?? row.state}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
    </div>
  );
}

function siteName(sites: { id: string; code: string; name: string }[], id: string): string {
  const found = sites.find((site) => String(site.id) === String(id));
  return found ? `${found.name} (${found.code})` : `#${id}`;
}

export default TransferRequestsPage;
