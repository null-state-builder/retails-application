import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import "./Reservations.css";

import { useAuth, allowedUnits } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { formatINR, StatusChip } from "../lib/format";
import { TENDERS, dayText } from "../lib/reservations";
import type { Tender } from "../lib/reservations";
import {
  SPECIAL_ORDER_OFFLINE,
  SPECIAL_ORDERS_API,
  STEPS,
  advanceText,
  draftProblem,
  emptyDraft,
  isOpen,
  itemText,
  routeText,
  specialOrderRequest,
  statusChip,
  toldText,
} from "../lib/specialOrders";
import type { SpecialOrder, SpecialOrderDraft } from "../lib/specialOrders";

type Listing = ApiRead<ApiSchemas["SpecialOrderList"]>;
type Send = (
  order: SpecialOrder,
  step: string,
  body: Record<string, unknown>,
  said: (after: SpecialOrder) => string,
) => Promise<void>;

/** Customer Orders > Special orders (store operations ticket 21, ST-ORD-2).
 *
 *  For an item no store has: staff record the customer, brand, style, size and
 *  colour, with an optional advance in cash, card or UPI and a receipt voucher
 *  (RV series, no GST). The order becomes a transfer request or a booking line
 *  and moves asked, ordered, arrived, customer told, collected - or cancelled.
 *  The customer collects at Billing, where the advance pays towards the bill.
 *  Online only: offline the page says so and keeps what was typed. The server
 *  decides everything; this page shows it. */
export function SpecialOrdersPage() {
  const { user, activeStore } = useAuth();
  const stores = useMemo(
    () => (user ? allowedUnits(user).filter((s) => s.store_type === "store") : []),
    [user],
  );
  const [picked, setPicked] = useState("");
  const store =
    picked ||
    (activeStore?.store_type === "store" ? activeStore.code : "") ||
    (stores.length === 1 ? stores[0].code : "");

  const [listing, setListing] = useState<Listing | null>(null);
  const [draft, setDraft] = useState<SpecialOrderDraft>(emptyDraft);
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState<SpecialOrder | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  // One order's identity across retries: a dropped connection after the server
  // saved it replays the same order instead of taking a second.
  const [commandId, setCommandId] = useState(() => crypto.randomUUID());
  // The same for each step tap, per order: kept only while the answer is lost,
  // so a later, separate attempt is judged afresh.
  const attempts = useRef(new Map<string, string>());
  const request = useRef(0);

  const load = useCallback(
    async (q: string) => {
      if (!store) return;
      if (!navigator.onLine) {
        setOnline(false);
        return;
      }
      const mine = ++request.current;
      try {
        const l = await api.get<Listing>(SPECIAL_ORDERS_API, { params: q ? { store, q } : { store } });
        if (mine !== request.current) return;
        setLost(false);
        setListing(l.data);
      } catch (reason) {
        if (mine !== request.current) return;
        if (isConnectionLost(reason)) setLost(true);
        else setError(apiErrorMessage(reason));
      }
    },
    [store],
  );

  useEffect(() => {
    void load("");
  }, [load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(query);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load, query]);

  /** One write. A dropped connection keeps everything as typed and the command's
   *  identity; any answer from the server - saved or refused - ends that command. */
  async function post<T>(path: string, body: Record<string, unknown>, answered: () => void): Promise<T | null> {
    if (!navigator.onLine) {
      setOnline(false);
      return null;
    }
    setBusy(true);
    setError("");
    setDone("");
    try {
      const response = await api.post<T>(path, body);
      answered();
      return response.data;
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
      } else {
        answered();
        setError(apiErrorMessage(reason));
      }
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function take() {
    const problem = draftProblem(draft);
    if (problem) {
      setError(problem);
      return;
    }
    const made = await post<SpecialOrder>(
      SPECIAL_ORDERS_API,
      specialOrderRequest(commandId, store, draft),
      () => setCommandId(crypto.randomUUID()),
    );
    if (!made) return;
    setDraft(emptyDraft());
    setShown(made.voucher ? made : null);
    setDone(`Taken as ${made.ref}. Order it as a transfer request or a booking line below.`);
    await load(query);
  }

  const step: Send = async (order, name, body, said) => {
    const key = `${name}:${order.id}`;
    const held = attempts.current.get(key) ?? crypto.randomUUID();
    attempts.current.set(key, held);
    const after = await post<SpecialOrder>(
      `${SPECIAL_ORDERS_API}/${order.id}/${name}`,
      { store, command_id: held, ...body },
      () => attempts.current.delete(key),
    );
    if (!after) return;
    setDone(said(after));
    await load(query);
  };

  const offline = !online || lost;
  const writable = !offline && !busy;
  const withAdvance = draft.advance.trim() !== "";

  return (
    <div className="page-pad">
      <PageHeader
        title="Special orders"
        lead="For an item no store has: record what the customer wants and follow it until they collect it at Billing."
      />
      {stores.length > 1 && (
        <label className="field">
          <span>Store</span>
          <select
            className="input"
            data-testid="special-orders-store"
            value={store}
            onChange={(e) => {
              setPicked(e.target.value);
              setListing(null);
            }}
          >
            <option value="">Choose a store</option>
            {stores.map((s) => (
              <option key={s.code} value={s.code}>
                {s.code} · {s.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {offline && (
        <p className="warn-note" data-testid="special-orders-offline" role="status">
          {SPECIAL_ORDER_OFFLINE}
          {online && (
            <>
              {" "}
              <button
                type="button"
                className="btn"
                data-testid="special-orders-retry"
                onClick={() => void load(query)}
              >
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="special-orders-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="special-orders-done">
          {done}
        </p>
      )}
      {!store && <p>Choose a store to see its special orders.</p>}
      {store && !listing && !offline && !error && <p>Loading special orders…</p>}

      {shown?.voucher && <Voucher order={shown} onClose={() => setShown(null)} />}

      {listing && (
        <>
          {!listing.switched_on ? (
            <p className="warn-note" data-testid="special-orders-switched-off">
              Special orders are switched off at {listing.store}. No new order can be taken; the
              ones below can still be moved on, collected, cancelled and refunded.
            </p>
          ) : (
            <section className="card section-card" data-testid="special-order-form">
              <h2 className="h3">New special order</h2>
              <div className="form-grid">
                <label className="field">
                  <span>Customer name</span>
                  <input
                    className="input"
                    data-testid="special-order-name"
                    value={draft.customer_name}
                    onChange={(e) => setDraft({ ...draft, customer_name: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Mobile</span>
                  <input
                    className="input"
                    inputMode="tel"
                    data-testid="special-order-mobile"
                    value={draft.customer_mobile}
                    onChange={(e) => setDraft({ ...draft, customer_mobile: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Brand</span>
                  <select
                    className="input"
                    data-testid="special-order-brand"
                    value={draft.brand}
                    onChange={(e) => setDraft({ ...draft, brand: e.target.value })}
                  >
                    <option value="">Choose a brand</option>
                    {listing.brands.map((b) => (
                      <option key={b.id} value={String(b.id)}>
                        {b.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Style</span>
                  <input
                    className="input"
                    data-testid="special-order-style"
                    value={draft.style_code}
                    onChange={(e) => setDraft({ ...draft, style_code: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Size</span>
                  <input
                    className="input"
                    data-testid="special-order-size"
                    value={draft.size}
                    onChange={(e) => setDraft({ ...draft, size: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Colour</span>
                  <input
                    className="input"
                    data-testid="special-order-colour"
                    value={draft.colour}
                    onChange={(e) => setDraft({ ...draft, colour: e.target.value })}
                  />
                </label>
              </div>
              <label className="field">
                <span>Note (optional)</span>
                <input
                  className="input"
                  data-testid="special-order-note"
                  value={draft.note}
                  onChange={(e) => setDraft({ ...draft, note: e.target.value })}
                />
              </label>
              <div className="form-grid">
                <label className="field">
                  <span>Advance in rupees (optional)</span>
                  <input
                    className="input"
                    inputMode="decimal"
                    data-testid="special-order-advance"
                    value={draft.advance}
                    onChange={(e) => setDraft({ ...draft, advance: e.target.value })}
                  />
                </label>
                {withAdvance && (
                  <fieldset className="field">
                    <legend>Paid by</legend>
                    {TENDERS.map((t) => (
                      <label key={t.value} className="radio">
                        <input
                          type="radio"
                          name="special-order-mode"
                          data-testid={`special-order-mode-${t.value}`}
                          checked={draft.mode === t.value}
                          onChange={() => setDraft({ ...draft, mode: t.value as Tender })}
                        />{" "}
                        {t.label}
                      </label>
                    ))}
                  </fieldset>
                )}
                {withAdvance && draft.mode !== "cash" && (
                  <label className="field">
                    <span>Card or UPI reference (optional)</span>
                    <input
                      className="input"
                      data-testid="special-order-reference"
                      value={draft.reference}
                      onChange={(e) => setDraft({ ...draft, reference: e.target.value })}
                    />
                  </label>
                )}
              </div>
              <p className="muted-cell" data-testid="special-order-terms">
                {withAdvance ? listing.terms_with_advance : listing.terms_without_advance}
              </p>
              <button
                type="button"
                className="btn btn-primary"
                data-testid="special-order-save"
                disabled={!writable}
                onClick={() => void take()}
              >
                Take the order
              </button>
            </section>
          )}

          <section className="card section-card" data-testid="special-order-list">
            <h2 className="h3">Special orders at {listing.store}</h2>
            <p className="muted-cell">
              To collect: at Billing, press Special order collection and type the order number.
            </p>
            <div className="toolbar">
              <input
                className="input"
                aria-label="Find a special order"
                placeholder="Order number, voucher number or mobile"
                data-testid="special-order-search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void load(query.trim());
                }}
              />
              <button
                type="button"
                className="btn"
                disabled={offline}
                onClick={() => void load(query.trim())}
              >
                Find
              </button>
            </div>
            {listing.special_orders.length === 0 ? (
              <p data-testid="special-order-none">No special orders to show.</p>
            ) : (
              // One card per order, stacked: a real layout at 375 px on the shop
              // floor (PRD §5.2 rule 2), not a squeezed table.
              <ul className="reservation-cards" data-testid="special-order-cards">
                {listing.special_orders.map((o) => (
                  <OrderCard
                    key={o.id}
                    order={o}
                    sources={listing.sources}
                    writable={writable}
                    onVoucher={() => setShown(o)}
                    send={step}
                  />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}

/** One order, with the one next step it can take. */
function OrderCard({
  order: o,
  sources,
  writable,
  onVoucher,
  send,
}: {
  order: SpecialOrder;
  sources: { code: string; name: string }[];
  writable: boolean;
  onVoucher: () => void;
  send: Send;
}) {
  const chip = statusChip(o.status);
  const [route, setRoute] = useState<"transfer" | "booking">(sources.length ? "transfer" : "booking");
  const [source, setSource] = useState(sources[0]?.code ?? "");
  const [barcode, setBarcode] = useState("");
  const [booking, setBooking] = useState("");
  // Null until typed: the barcode asked for on the transfer request stands in.
  const [arrived, setArrived] = useState<string | null>(null);
  const piece = arrived ?? o.ordered_barcode;
  const reached = STEPS.indexOf(o.status as (typeof STEPS)[number]);

  return (
    <li className="reservation-card" data-testid={`special-order-${o.ref}`}>
      <div className="reservation-card-head">
        <strong>{o.ref}</strong> <StatusChip status={chip.label} tone={chip.tone} />
      </div>
      <p>
        {o.customer_name} · <span className="muted-cell">{o.customer_mobile}</span>
      </p>
      <p data-testid={`special-order-item-${o.ref}`}>{itemText(o)}</p>
      {o.note && <p className="muted-cell">{o.note}</p>}
      <p className="muted-cell" data-testid={`special-order-steps-${o.ref}`}>
        {o.status === "cancelled"
          ? "Cancelled"
          : STEPS.map((s, i) => (i <= reached ? statusChip(s).label : null))
              .filter(Boolean)
              .join(" → ")}
      </p>
      <p className="muted-cell">
        Asked {dayText(o.asked_on)}
        {o.route ? ` · ${routeText(o)}` : ""}
        {o.arrived_barcode ? ` · Piece ${o.arrived_barcode}` : ""}
        {o.told_how ? ` · Told ${toldText(o.told_how)}` : ""}
        {o.sale_doc_number ? ` · Bill ${o.sale_doc_number}` : ""}
      </p>
      <p data-testid={`special-order-advance-${o.ref}`}>{advanceText(o)}</p>

      {o.status === "asked" && (
        <fieldset className="field" data-testid={`special-order-order-${o.ref}`}>
          <legend>Order it as</legend>
          <label className="radio">
            <input
              type="radio"
              name={`route-${o.id}`}
              data-testid={`special-order-route-transfer-${o.ref}`}
              checked={route === "transfer"}
              disabled={!sources.length}
              onChange={() => setRoute("transfer")}
            />{" "}
            A transfer request
          </label>
          <label className="radio">
            <input
              type="radio"
              name={`route-${o.id}`}
              data-testid={`special-order-route-booking-${o.ref}`}
              checked={route === "booking"}
              onChange={() => setRoute("booking")}
            />{" "}
            A line on a booking
          </label>
          {route === "transfer" ? (
            <>
              <label className="field">
                <span>From</span>
                <select
                  className="input"
                  data-testid={`special-order-source-${o.ref}`}
                  value={source}
                  onChange={(e) => setSource(e.target.value)}
                >
                  {sources.map((s) => (
                    <option key={s.code} value={s.code}>
                      {s.code} · {s.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>The item's barcode</span>
                <input
                  className="input"
                  data-testid={`special-order-barcode-${o.ref}`}
                  value={barcode}
                  onChange={(e) => setBarcode(e.target.value)}
                />
              </label>
            </>
          ) : (
            <label className="field">
              <span>Booking number (from the buyer)</span>
              <input
                className="input"
                data-testid={`special-order-booking-${o.ref}`}
                value={booking}
                onChange={(e) => setBooking(e.target.value)}
              />
            </label>
          )}
          <button
            type="button"
            className="btn btn-primary"
            data-testid={`special-order-place-${o.ref}`}
            disabled={!writable}
            onClick={() =>
              void send(
                o,
                "order",
                route === "transfer"
                  ? { route, source_site: source, barcode: barcode.trim() }
                  : { route, booking: booking.trim() },
                (after) => `${after.ref} ordered: ${routeText(after)}.`,
              )
            }
          >
            Order
          </button>
        </fieldset>
      )}

      {o.status === "ordered" && (
        <div className="toolbar">
          <input
            className="input"
            aria-label="Barcode of the piece that arrived"
            placeholder="Scan the piece that arrived"
            data-testid={`special-order-arrived-${o.ref}`}
            value={piece}
            onChange={(e) => setArrived(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-primary"
            data-testid={`special-order-arrive-${o.ref}`}
            disabled={!writable}
            onClick={() =>
              void send(o, "arrive", { barcode: piece.trim() }, (after) => `${after.ref} has arrived. Tell the customer.`)
            }
          >
            Arrived
          </button>
        </div>
      )}

      {o.status === "arrived" && (
        <div className="toolbar" data-testid={`special-order-tell-${o.ref}`}>
          <span className="muted-cell">Customer told by</span>
          {(["call", "message", "in_person"] as const).map((how) => (
            <button
              key={how}
              type="button"
              className="btn"
              data-testid={`special-order-told-${how}-${o.ref}`}
              disabled={!writable}
              onClick={() =>
                void send(o, "tell", { told_how: how }, (after) => `${after.ref}: customer told ${toldText(how)}.`)
              }
            >
              {toldText(how).replace(/^by /, "").replace(/^./, (c) => c.toUpperCase())}
            </button>
          ))}
        </div>
      )}

      <div className="toolbar">
        {o.voucher && (
          <button type="button" className="btn" onClick={onVoucher}>
            Voucher
          </button>
        )}
        {isOpen(o) && (
          <>
            <button
              type="button"
              className="btn"
              data-testid={`special-order-cancel-customer-${o.ref}`}
              disabled={!writable}
              onClick={() =>
                void send(
                  o,
                  "cancel",
                  { by: "customer" },
                  (after) => `${after.ref} cancelled. Advance: ${advanceText(after)}.`,
                )
              }
            >
              Customer cancels
            </button>
            <button
              type="button"
              className="btn"
              data-testid={`special-order-cancel-store-${o.ref}`}
              disabled={!writable}
              onClick={() =>
                void send(
                  o,
                  "cancel",
                  { by: "store" },
                  (after) => `${after.ref} cancelled. Advance: ${advanceText(after)}.`,
                )
              }
            >
              Store cancels
            </button>
          </>
        )}
        {o.advance_outcome === "refund_due" && (
          <button
            type="button"
            className="btn btn-primary"
            data-testid={`special-order-refund-${o.ref}`}
            disabled={!writable}
            onClick={() =>
              void send(
                o,
                "refund",
                {},
                (after) => `${after.ref}: ${advanceText(after)} by ${after.voucher?.mode.toUpperCase() ?? "cash"}.`,
              )
            }
          >
            Refund {formatINR(o.advance_balance_paise)}
          </button>
        )}
      </div>
    </li>
  );
}

/** The receipt voucher as the customer takes it away. */
function Voucher({ order, onClose }: { order: SpecialOrder; onClose: () => void }) {
  const v = order.voucher;
  if (!v) return null;
  return (
    <section className="card section-card" data-testid="special-order-voucher">
      <h2 className="h3">Receipt voucher {v.number}</h2>
      <p>
        {v.store_name} · GSTIN {v.store_gstin}
        <br />
        Date {dayText(v.issued_on)} · Special order {order.ref}
      </p>
      <p>
        Received from {order.customer_name} ({order.customer_mobile}):{" "}
        <strong>{formatINR(v.amount_paise)}</strong> by {v.mode.toUpperCase()}
        {v.reference ? ` (${v.reference})` : ""}.
      </p>
      <p>For: {itemText(order)}</p>
      <p data-testid="special-order-voucher-terms">{order.terms}</p>
      <div className="toolbar">
        <button type="button" className="btn" onClick={() => window.print()}>
          Print
        </button>
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
    </section>
  );
}
