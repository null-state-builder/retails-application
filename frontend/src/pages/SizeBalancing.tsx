import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { formatDateTime } from "../lib/format";
import {
  REQUESTS_PATH,
  itemText,
  mrpText,
  ruleText,
  stateText,
  totalText,
} from "../lib/sizeBalancing";

type Payload = ApiRead<ApiSchemas["SizeBalancing"]>;
type Suggestion = Payload["pending"][number];
type Decision = "approve" | "reject";

const OFFLINE = "Size balancing needs a connection. Reconnect to see the suggestions.";

/** Stock > Size Balancing (store operations ticket 34, ST-TRF-1).
 *
 *  The daily check suggests transfers that fill this store's broken sizes from
 *  another store's surplus: what it holds beyond 8 weeks of its own sales, and
 *  only if the transfer moves at least 3 pieces or Rs 3,000 at MRP. The server
 *  works all of it out; a person here approves a suggestion, which raises an
 *  ordinary transfer request, or rejects it with a reason. MRP only, never cost.
 *
 *  Everything is live data, so offline the page says it needs a connection
 *  instead of showing a stale copy as current. A decision sent as the connection
 *  drops keeps its command id, so pressing again replays it rather than doing it
 *  twice. Only stores in the person's own scope, and only where the switch is on. */
export function SizeBalancingPage() {
  const [params, setParams] = useSearchParams();
  const siteId = Number(params.get("site_id")) || null;
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [refused, setRefused] = useState("");
  const [saved, setSaved] = useState<{ text: string; request: boolean } | null>(null);
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);
  /** A decision's command id, kept until the server answers it. */
  const sent = useRef(new Map<string, string>());

  const load = useCallback(
    async (site: number | null) => {
      if (!navigator.onLine) {
        setOnline(false);
        return;
      }
      const mine = ++request.current;
      setBusy(true);
      setError("");
      try {
        const response = await api.get<Payload>("/goods-v1/outbound/size-balancing", {
          params: site ? { site_id: site } : {},
        });
        if (mine !== request.current) return;
        setLost(false);
        setData(response.data);
      } catch (reason) {
        if (mine !== request.current) return;
        if (isConnectionLost(reason)) setLost(true);
        else if (site && apiErrorCode(reason) === "NOT_FOUND") {
          setRefused(apiErrorMessage(reason));
          setParams(
            (current) => {
              const next = new URLSearchParams(current);
              next.delete("site_id");
              return next;
            },
            { replace: true },
          );
        } else setError(apiErrorMessage(reason));
      } finally {
        if (mine === request.current) setBusy(false);
      }
    },
    [setParams],
  );

  useEffect(() => {
    void load(siteId);
  }, [load, siteId]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(siteId);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load, siteId]);

  /** Approve or reject one suggestion; true once the server has it. */
  async function decide(row: Suggestion, decision: Decision, reason: string): Promise<boolean> {
    if (!navigator.onLine) {
      setOnline(false);
      return false;
    }
    // The reason is part of what a rejection says, so a changed reason is a new command.
    const key = `${decision}:${row.id}:${decision === "reject" ? reason.trim() : ""}`;
    const commandId = sent.current.get(key) ?? crypto.randomUUID();
    sent.current.set(key, commandId);
    setError("");
    setSaved(null);
    setBusy(true);
    let ok = false;
    let refusal = "";
    try {
      await api.post(`/goods-v1/outbound/size-balancing/${decision}`, {
        ...goodsMeta(undefined, commandId),
        suggestion_id: row.id,
        ...(decision === "reject" ? { reason } : {}),
      });
      sent.current.delete(key);
      setSaved(
        decision === "approve"
          ? { text: `From ${row.sending.code}: approved. A transfer request was raised.`, request: true }
          : { text: `From ${row.sending.code}: rejected.`, request: false },
      );
      ok = true;
    } catch (reason) {
      if (isConnectionLost(reason)) {
        // No answer at all: say so and leave the page as it was. The same
        // command id goes again next time, so it is never done twice.
        setLost(true);
        return false;
      }
      sent.current.delete(key);
      refusal = apiErrorMessage(reason);
    } finally {
      setBusy(false);
    }
    await load(siteId);
    // After the reload, which starts by clearing the last error.
    if (refusal) setError(refusal);
    return ok;
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="balance-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="balance-retry"
            disabled={busy}
            onClick={() => void load(siteId)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const header = (
    <PageHeader
      title="Size Balancing"
      lead="Transfers suggested to fill this store's missing sizes from another store that has more than it sells. Approve one to ask for the stock, or reject it and say why."
    />
  );
  const errorNote = (error || refused) && (
    <p className="warn-note" data-testid="balance-error">
      {error || refused}
    </p>
  );

  if (!data) {
    return (
      <div className="page-pad">
        {header}
        {offlineNote}
        {errorNote || (online && !lost && <p>Loading…</p>)}
      </div>
    );
  }

  if (data.stores.length === 0) {
    return (
      <div className="page-pad">
        {header}
        {offlineNote}
        {errorNote}
        <p className="muted-cell" data-testid="balance-none">
          Size balancing is not switched on at any store you work at.
        </p>
      </div>
    );
  }

  return (
    <div className="page-pad">
      {header}
      {offlineNote}
      {errorNote}
      {saved && (
        <p className="ok-note" data-testid="balance-saved">
          {saved.text}
          {saved.request && (
            <>
              {" "}
              <Link to={REQUESTS_PATH} data-testid="balance-requests">
                See the requests
              </Link>
            </>
          )}
        </p>
      )}
      <section className="card section-card">
        <div className="toolbar">
          <label className="field">
            <span>Store</span>
            <select
              className="select"
              data-testid="balance-store"
              value={data.site_id ?? ""}
              disabled={disabled}
              onChange={(event) => {
                setRefused("");
                setParams((current) => {
                  const next = new URLSearchParams(current);
                  next.set("site_id", event.target.value);
                  return next;
                });
              }}
            >
              {data.stores.map((store) => (
                <option key={store.id} value={store.id}>
                  {store.name} ({store.code})
                </option>
              ))}
            </select>
          </label>
          <span className="muted-cell" data-testid="balance-checked">
            {data.checked_at ? `Last checked ${formatDateTime(data.checked_at)}` : "Checked daily"}
          </span>
        </div>
        <p className="muted-cell" data-testid="balance-rule">
          {ruleText(data.settings)}
        </p>
        {data.pending.length === 0 ? (
          <p className="ok-note" data-testid="balance-empty">
            Nothing suggested for this store now.
          </p>
        ) : (
          data.pending.map((row) => (
            <PendingSuggestion
              key={row.id}
              row={row}
              canDecide={data.can_decide}
              weeks={data.settings.weeks}
              disabled={disabled}
              onDecide={decide}
            />
          ))
        )}
      </section>
      {data.decided.length > 0 && <Decided rows={data.decided} />}
    </div>
  );
}

function PendingSuggestion({
  row,
  canDecide,
  weeks,
  disabled,
  onDecide,
}: {
  row: Suggestion;
  canDecide: boolean;
  weeks: number;
  disabled: boolean;
  onDecide: (row: Suggestion, decision: Decision, reason: string) => Promise<boolean>;
}) {
  const [reason, setReason] = useState("");
  return (
    <div className="card section-card" data-testid={`balance-row-${row.sending.code}`}>
      <h3 className="section-title" data-testid="balance-from">
        From {row.sending.name} ({row.sending.code})
      </h3>
      <p data-testid="balance-total">
        {totalText(row.pieces, row.mrp_paise, row.mrp_unknown_pieces)}. Suggested{" "}
        {formatDateTime(row.made_at)}.
      </p>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Brand</th>
              <th>Item</th>
              <th>Pieces</th>
              <th>MRP each</th>
              <th>You sold ({weeks} weeks)</th>
            </tr>
          </thead>
          <tbody>
            {row.lines.map((line) => (
              <tr key={`${line.alert_id}:${line.sku_id}`} data-testid="balance-line">
                <td>{line.brand}</td>
                <td>{itemText(line)}</td>
                <td>{line.qty}</td>
                <td>{mrpText(line.mrp_paise)}</td>
                <td>{line.destination_sold}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {canDecide ? (
        <div className="toolbar">
          <button
            type="button"
            className="btn btn-primary"
            data-testid="balance-approve"
            disabled={disabled}
            onClick={() => void onDecide(row, "approve", "")}
          >
            Approve: ask {row.sending.code} for these
          </button>
          <input
            className="input"
            data-testid="balance-reason"
            placeholder="Why not (needed to reject)"
            maxLength={240}
            value={reason}
            disabled={disabled}
            onChange={(event) => setReason(event.target.value)}
          />
          <button
            type="button"
            className="btn"
            data-testid="balance-reject"
            disabled={disabled || !reason.trim()}
            onClick={() => void onDecide(row, "reject", reason)}
          >
            Reject
          </button>
        </div>
      ) : (
        <p className="muted-cell" data-testid="balance-cannot">
          Someone who can ask for stock at this store decides.
        </p>
      )}
    </div>
  );
}

function Decided({ rows }: { rows: Suggestion[] }) {
  return (
    <section className="card section-card" data-testid="balance-decided">
      <h3 className="section-title">Recently decided</h3>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>From</th>
              <th>Items</th>
              <th>Total</th>
              <th>Outcome</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} data-testid={`balance-decided-${row.id}`}>
                <td>{row.sending.code}</td>
                <td>{row.lines.map((line) => `${itemText(line)} × ${line.qty}`).join("; ")}</td>
                <td>{totalText(row.pieces, row.mrp_paise, row.mrp_unknown_pieces)}</td>
                <td data-testid="balance-outcome">
                  {stateText(row.state, row.withdrawn_reason)}
                  {row.reason && `: ${row.reason}`}
                  {row.decided_by && <span className="muted-cell"> ({row.decided_by})</span>}
                </td>
                <td>{row.decided_at ? formatDateTime(row.decided_at) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
