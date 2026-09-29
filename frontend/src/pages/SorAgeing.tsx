import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  deadlineText,
  deliveryText,
  pendingCommand,
  stateHeading,
  type PendingCommand,
} from "../lib/sorAgeing";
import { dayText } from "../lib/stockAgeing";

type Payload = ApiRead<ApiSchemas["SorAgeing"]>;
type Row = Payload["rows"][number];

const PAGE_API = "/goods-v1/stock/sor-ageing";
const OFFLINE =
  "SOR ageing needs a connection. Nothing can be recorded until it is back; what you typed stays here.";
/** Where a flagged piece is sent back to its brand. */
const RETURN_TO_BRAND = "/return-to-brand/new";

interface Delivery {
  arrivalId: string;
  text: string;
  pieces: number;
  dispatchDate: string | null;
  brandInvoice: Row["brand_invoice"];
}

interface Draft {
  dispatch: string;
  dispatchReason: string;
  number: string;
  invoiceDate: string;
  invoiceReason: string;
}

const EMPTY: Draft = {
  dispatch: "",
  dispatchReason: "",
  number: "",
  invoiceDate: "",
  invoiceReason: "",
};

/** The deliveries whose SOR pieces stand here, once each, in the page's order. */
function deliveriesOf(rows: Row[]): Delivery[] {
  const out = new Map<string, Delivery>();
  for (const row of rows) {
    if (!row.arrival_id) continue;
    const seen = out.get(row.arrival_id);
    if (seen) seen.pieces += row.qty;
    else
      out.set(row.arrival_id, {
        arrivalId: row.arrival_id,
        text: deliveryText(row),
        pieces: row.qty,
        dispatchDate: row.dispatch_date,
        brandInvoice: row.brand_invoice,
      });
  }
  return [...out.values()];
}

/** Brands > SOR Ageing (store operations ticket 24, ST-BRD-5).
 *
 *  Goods on sale or return are the brand's until it invoices them, and it must
 *  invoice at 6 months from dispatch at the latest (CGST Act s.31(7)). Each SOR
 *  piece is flagged at 5 months from the brand's dispatch date, leaving a month
 *  to sell it, return it to the brand, or get the brand's invoice. Pieces with no
 *  dispatch date, and pieces of brands whose model is unknown, are listed and
 *  never guessed. The server decides every state and date; Accounts records a
 *  missing dispatch date and the brand's invoice here, each in the audit log.
 *  Everything is live, so offline the page says it needs a connection. */
export function SorAgeingPage() {
  const [params, setParams] = useSearchParams();
  const siteId = Number(params.get("site_id")) || null;
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [refused, setRefused] = useState("");
  const [saved, setSaved] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  /** The press whose answer was lost, if any (`pendingCommand`). */
  const pending = useRef<PendingCommand | null>(null);
  const request = useRef(0);

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
        const response = await api.get<Payload>(PAGE_API, {
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

  const deliveries = useMemo(() => deliveriesOf(data?.rows ?? []), [data]);

  function draftOf(arrivalId: string): Draft {
    return drafts[arrivalId] ?? EMPTY;
  }

  function edit(arrivalId: string, change: Partial<Draft>) {
    setDrafts((current) => ({
      ...current,
      [arrivalId]: { ...(current[arrivalId] ?? EMPTY), ...change },
    }));
  }

  /** Send one record. A dropped connection keeps what was typed and the
   *  command's identity, so pressing again records it once. */
  async function send(
    url: string,
    step: string,
    arrivalId: string,
    body: Record<string, unknown>,
    said: string,
    clear: Partial<Draft>,
  ) {
    if (!navigator.onLine || !data?.site_id) {
      setOnline(navigator.onLine);
      return;
    }
    setBusy(true);
    setError("");
    setSaved("");
    const full = { site_id: data.site_id, arrival_id: arrivalId, ...body };
    const command = pendingCommand(pending.current, `${step}:${JSON.stringify(full)}`, () =>
      crypto.randomUUID(),
    );
    pending.current = command;
    try {
      await api.post(url, { ...goodsMeta(undefined, command.commandId), ...full });
      pending.current = null;
      setSaved(said);
      edit(arrivalId, clear);
      await load(siteId);
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else {
        pending.current = null;
        setError(apiErrorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="sor-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="sor-retry"
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
      title="SOR Ageing"
      lead="Stock on sale or return, aged from the brand's dispatch date. Each piece is flagged a month before the brand must invoice it, so it can be sold, returned to the brand, or invoiced by the brand in time. Nothing is guessed: pieces with no dispatch date, and brands with no approved model, are listed apart."
    />
  );
  const errorNote = (error || refused) && (
    <p className="warn-note" data-testid="sor-error">
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
        <p className="muted-cell" data-testid="sor-none">
          SOR ageing is not switched on at any site you work at.
        </p>
      </div>
    );
  }

  const flagged = data.rows.some((row) => row.state === "due" || row.state === "overdue");

  return (
    <div className="page-pad">
      {header}
      {offlineNote}
      {errorNote}
      {saved && (
        <p className="ok-note" data-testid="sor-saved">
          {saved}
        </p>
      )}
      <section className="card section-card">
        <div className="toolbar">
          <label className="field">
            <span>Site</span>
            <select
              className="select"
              data-testid="sor-store"
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
          <span className="muted-cell">
            As of {dayText(data.today)}. Flagged at {data.alert_months} months from dispatch; the
            brand must invoice by {data.invoice_months}.
          </span>
        </div>
        {flagged && (
          <div className="warn-note" data-testid="sor-ways">
            <strong>Settle each flagged piece one of three ways:</strong>
            <ul>
              <li>Sell it before its date.</li>
              <li>
                Return it to the brand: <Link to={RETURN_TO_BRAND}>Return to Brand</Link>.
              </li>
              <li>
                Get the brand's invoice for its delivery
                {data.can_record
                  ? " and record it under Deliveries below."
                  : "; Accounts records it here."}
              </li>
            </ul>
          </div>
        )}
        {!data.goods_records ? (
          <p className="warn-note" data-testid="sor-legacy">
            This site's stock is not on the goods records yet, so no piece can be traced to its
            delivery.
          </p>
        ) : data.rows.length === 0 ? (
          <p className="ok-note" data-testid="sor-empty">
            No SOR stock at this site.
          </p>
        ) : (
          data.groups
            .filter((group) => group.qty > 0)
            .map((group) => (
              <div key={group.group} data-testid={`sor-group-${group.group}`}>
                <h3 className="section-title">
                  {stateHeading(group.group, data.alert_months, data.invoice_months)}
                </h3>
                <p className="muted-cell" data-testid={`sor-total-${group.group}`}>
                  {group.qty} piece(s) of {group.items} item(s) from {group.deliveries} deliver
                  {group.deliveries === 1 ? "y" : "ies"}
                </p>
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Barcode</th>
                        <th>Brand</th>
                        <th>Design</th>
                        <th>Size</th>
                        <th>Colour</th>
                        <th>Season</th>
                        <th className="num">Pieces</th>
                        <th>Delivery</th>
                        <th>Dispatched</th>
                        <th>Settle</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.rows
                        .filter((row) => row.state === group.group)
                        .map((row) => (
                          <tr
                            key={row.origin_id}
                            data-testid={`sor-row-${row.barcode || row.sku_id}`}
                          >
                            <td className="mono">{row.barcode || "No barcode"}</td>
                            <td>{row.brand}</td>
                            <td>{row.design}</td>
                            <td>{row.size}</td>
                            <td>{row.colour}</td>
                            <td>{row.season_code || "None"}</td>
                            <td className="num">{row.qty}</td>
                            <td>{deliveryText(row)}</td>
                            <td>
                              {row.dispatch_date ? dayText(row.dispatch_date) : "Not recorded"}
                            </td>
                            <td data-testid="sor-deadline">
                              {row.state === "overdue" && (
                                <span className="chip chip-red">Past due</span>
                              )}
                              {row.state === "due" && (
                                <span className="chip chip-amber">Settle now</span>
                              )}{" "}
                              {deadlineText(row)}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))
        )}
      </section>

      {deliveries.length > 0 && (
        <section className="card section-card" data-testid="sor-deliveries">
          <h3 className="section-title">Deliveries</h3>
          <p className="muted-cell">
            The brand's dispatch date and its invoice belong to the delivery the pieces came on.
            {data.can_record
              ? " Changing a recorded value needs a reason; every record is in the audit log."
              : " Accounts records these."}
          </p>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Delivery</th>
                  <th className="num">SOR pieces here</th>
                  <th>Brand's dispatch date</th>
                  <th>Brand's invoice</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.map((delivery) => {
                  const draft = draftOf(delivery.arrivalId);
                  const dispatch = draft.dispatch || delivery.dispatchDate || "";
                  const dispatchChanged =
                    !!draft.dispatch && draft.dispatch !== delivery.dispatchDate;
                  const number = draft.number || delivery.brandInvoice?.number || "";
                  const invoiceDate =
                    draft.invoiceDate || delivery.brandInvoice?.invoice_date || "";
                  const invoiceChanged =
                    (!!draft.number && draft.number !== delivery.brandInvoice?.number) ||
                    (!!draft.invoiceDate &&
                      draft.invoiceDate !== delivery.brandInvoice?.invoice_date);
                  return (
                    <tr key={delivery.arrivalId} data-testid={`sor-delivery-${delivery.arrivalId}`}>
                      <td>{delivery.text}</td>
                      <td className="num">{delivery.pieces}</td>
                      <td>
                        {data.can_record ? (
                          <div className="stack">
                            <input
                              type="date"
                              className="input"
                              aria-label="Brand's dispatch date"
                              data-testid="sor-dispatch-date"
                              max={data.today}
                              value={dispatch}
                              disabled={disabled}
                              onChange={(event) =>
                                edit(delivery.arrivalId, { dispatch: event.target.value })
                              }
                            />
                            {delivery.dispatchDate && dispatchChanged && (
                              <input
                                className="input"
                                aria-label="Why change the dispatch date"
                                placeholder="Why change it?"
                                data-testid="sor-dispatch-reason"
                                value={draft.dispatchReason}
                                disabled={disabled}
                                onChange={(event) =>
                                  edit(delivery.arrivalId, { dispatchReason: event.target.value })
                                }
                              />
                            )}
                            <button
                              type="button"
                              className="btn"
                              data-testid="sor-dispatch-save"
                              disabled={
                                disabled ||
                                !dispatchChanged ||
                                (!!delivery.dispatchDate && !draft.dispatchReason.trim())
                              }
                              onClick={() =>
                                void send(
                                  `${PAGE_API}/dispatch-date`,
                                  "dispatch",
                                  delivery.arrivalId,
                                  {
                                    dispatch_date: draft.dispatch,
                                    ...(delivery.dispatchDate
                                      ? { reason: draft.dispatchReason.trim() }
                                      : {}),
                                  },
                                  `Dispatch date ${dayText(draft.dispatch)} recorded.`,
                                  { dispatch: "", dispatchReason: "" },
                                )
                              }
                            >
                              Save
                            </button>
                          </div>
                        ) : delivery.dispatchDate ? (
                          dayText(delivery.dispatchDate)
                        ) : (
                          "Not recorded"
                        )}
                      </td>
                      <td>
                        {data.can_record ? (
                          <div className="stack">
                            <input
                              className="input"
                              aria-label="Brand's invoice number"
                              placeholder="Invoice number"
                              data-testid="sor-invoice-number"
                              value={number}
                              disabled={disabled}
                              onChange={(event) =>
                                edit(delivery.arrivalId, { number: event.target.value })
                              }
                            />
                            <input
                              type="date"
                              className="input"
                              aria-label="Brand's invoice date"
                              data-testid="sor-invoice-date"
                              max={data.today}
                              value={invoiceDate}
                              disabled={disabled}
                              onChange={(event) =>
                                edit(delivery.arrivalId, { invoiceDate: event.target.value })
                              }
                            />
                            {delivery.brandInvoice && invoiceChanged && (
                              <input
                                className="input"
                                aria-label="Why replace the invoice"
                                placeholder="Why replace it?"
                                data-testid="sor-invoice-reason"
                                value={draft.invoiceReason}
                                disabled={disabled}
                                onChange={(event) =>
                                  edit(delivery.arrivalId, { invoiceReason: event.target.value })
                                }
                              />
                            )}
                            <button
                              type="button"
                              className="btn"
                              data-testid="sor-invoice-save"
                              disabled={
                                disabled ||
                                !invoiceChanged ||
                                !number.trim() ||
                                !invoiceDate ||
                                (!!delivery.brandInvoice && !draft.invoiceReason.trim())
                              }
                              onClick={() =>
                                void send(
                                  `${PAGE_API}/brand-invoice`,
                                  "invoice",
                                  delivery.arrivalId,
                                  {
                                    invoice_number: number.trim(),
                                    invoice_date: invoiceDate,
                                    ...(delivery.brandInvoice
                                      ? { reason: draft.invoiceReason.trim() }
                                      : {}),
                                  },
                                  `Brand invoice ${number.trim()} recorded.`,
                                  { number: "", invoiceDate: "", invoiceReason: "" },
                                )
                              }
                            >
                              Save
                            </button>
                          </div>
                        ) : delivery.brandInvoice ? (
                          `${delivery.brandInvoice.number} of ${dayText(delivery.brandInvoice.invoice_date)}`
                        ) : (
                          "Not recorded"
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {data.unknown_models.length > 0 && (
        <section className="card section-card" data-testid="sor-unknown-models">
          <h3 className="section-title">Brands with no approved model</h3>
          <p className="muted-cell">
            These pieces are not treated as SOR, or as anything else, until the brand's terms for
            the season are approved (Brands, Terms).
          </p>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Brand</th>
                  <th>Season</th>
                  <th className="num">Pieces</th>
                  <th className="num">Items</th>
                </tr>
              </thead>
              <tbody>
                {data.unknown_models.map((entry) => (
                  <tr key={`${entry.brand}-${entry.season_code}`} data-testid="sor-unknown-row">
                    <td>{entry.brand || "No brand"}</td>
                    <td>{entry.season_label || entry.season_code || "None"}</td>
                    <td className="num">{entry.qty}</td>
                    <td className="num">{entry.items}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
