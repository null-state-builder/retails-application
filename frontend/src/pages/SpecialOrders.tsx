import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, Plus, Printer, X } from "lucide-react";

import "./Reservations.css";

import { useAuth, allowedUnits } from "../auth/AuthContext";
import { FilterChip, FilterChips } from "../components/FilterChip";
import { Note } from "../components/Note";
import { PageHeader } from "../components/PageHeader";
import { SearchBox } from "../components/SearchBox";
import { Box, Section } from "../components/Section";
import { Pane, SplitView } from "../components/SplitView";
import { EmptyState, Loading } from "../components/States";
import { Status } from "../components/Status";
import type { StatusTone } from "../components/Status";
import { api, apiErrorMessage } from "../lib/api";
import { useCachedGet } from "../lib/cachedQuery";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { formatDateTime, formatINR } from "../lib/format";
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
    (stores.length === 1 ? (stores[0]?.code ?? "") : "");

  const [draft, setDraft] = useState<SpecialOrderDraft>(emptyDraft);
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState<SpecialOrder | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // When a request last went unanswered; a list read after it clears it.
  const [lostAt, setLostAt] = useState(0);
  const [busy, setBusy] = useState(false);
  // One order's identity across retries: a dropped connection after the server
  // saved it replays the same order instead of taking a second.
  const [commandId, setCommandId] = useState(() => crypto.randomUUID());
  // The same for each step tap, per order: kept only while the answer is lost,
  // so a later, separate attempt is judged afresh.
  const attempts = useRef(new Map<string, string>());
  // The open order is in the URL, so back, refresh and a shared link reopen
  // it; the form and the list's filter are this visit's.
  const [params] = useSearchParams();
  const openRef = params.get("open") ?? "";
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState<string>("open");

  // The store's list, from the shared cache: a revisit shows the last list
  // at once and refreshes it behind it. A search is part of the URL.
  const read = useCachedGet<Listing>(store ? SPECIAL_ORDERS_API : null, {
    params: { store, q: query.trim() },
  });
  const { reload } = read;
  // Only this store's list: another store's stays off screen while this one loads.
  const listing = read.data && read.data.store === store ? read.data : null;
  const readLost = read.error !== null && isConnectionLost(read.error);
  const lost = readLost || lostAt > (read.updatedAt ?? 0);
  const readError = read.error !== null && !readLost ? apiErrorMessage(read.error) : "";
  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    reload();
  }, [reload]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      load();
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  /** One write. A dropped connection keeps everything as typed and the command's
   *  identity; any answer from the server - saved or refused - ends that command. */
  async function post<T>(
    path: string,
    body: Record<string, unknown>,
    answered: () => void,
  ): Promise<T | null> {
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
        setLostAt(Date.now());
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
    load();
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
    load();
  };

  /** A search looks across every stage, so what it finds is on screen. */
  const search = useCallback((term: string) => {
    setQuery(term);
    if (term.trim()) setFilter("");
  }, []);

  const offline = !online || lost;
  const writable = !offline && !busy;
  const withAdvance = draft.advance.trim() !== "";
  const rows = listing?.special_orders ?? [];
  const open = rows.find((o) => o.ref === openRef) ?? null;
  const showing = rows.filter((o) => inView(o, filter));
  const refundsDue = rows.filter((o) => o.advance_outcome === "refund_due");

  return (
    <div className="page-pad">
      <PageHeader
        title="Special orders"
        lead="For an item no store has: record what the customer wants and follow it until they collect it at Billing."
        actions={
          listing?.switched_on && (
            <button
              type="button"
              className="btn btn-primary"
              aria-expanded={adding}
              data-testid="special-order-new"
              onClick={() => setAdding(!adding)}
            >
              <Plus size={16} aria-hidden="true" /> New special order
            </button>
          )
        }
      />
      <div className="page">
        {stores.length > 1 && (
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="special-orders-store"
              value={store}
              onChange={(e) => {
                setPicked(e.target.value);
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
          <Note
            tone="warn"
            role="status"
            testId="special-orders-offline"
            action={
              online && (
                <button
                  type="button"
                  className="btn btn-sm"
                  data-testid="special-orders-retry"
                  onClick={() => load()}
                >
                  Try again
                </button>
              )
            }
          >
            {SPECIAL_ORDER_OFFLINE}
          </Note>
        )}
        {(error || readError) && (
          <Note tone="error" testId="special-orders-error">
            {error || readError}
          </Note>
        )}
        {done && (
          <Note tone="success" role="status" testId="special-orders-done">
            {done}
          </Note>
        )}
        {!store && <EmptyState>Choose a store to see its special orders.</EmptyState>}
        {store && !listing && !offline && !error && !readError && (
          <Loading label="Loading special orders" />
        )}

        {shown?.voucher && <Voucher order={shown} onClose={() => setShown(null)} />}

        {listing && !listing.switched_on && (
          <Note tone="warn" testId="special-orders-switched-off">
            Special orders are switched off at {listing.store}. No new order can be taken; the ones
            below can still be moved on, collected, cancelled and refunded.
          </Note>
        )}

        {listing && listing.switched_on && adding && (
          <Section heading="New special order" testId="special-order-form">
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
            <div className="order-actions">
              <button
                type="button"
                className="btn btn-primary"
                data-testid="special-order-save"
                disabled={!writable}
                onClick={() => void take()}
              >
                Take the order
              </button>
            </div>
          </Section>
        )}

        {listing && (
          <Section heading={`Special orders at ${listing.store}`} testId="special-order-list">
            <div className="orders-bar">
              <FilterChips label="Stage">
                {VIEWS.map((v) => (
                  <FilterChip
                    key={v.key}
                    pressed={filter === v.key}
                    count={rows.filter((o) => inView(o, v.key)).length}
                    onClick={() => setFilter(filter === v.key ? "" : v.key)}
                    testId={`special-order-show-${v.key}`}
                  >
                    {v.label}
                  </FilterChip>
                ))}
              </FilterChips>
              <SearchBox
                label="Find a special order"
                placeholder="Order number, voucher number or mobile"
                testId="special-order-search"
                value={query}
                onChange={search}
              />
            </div>
            {refundsDue.length > 0 && (
              <Note tone="warn" testId="special-order-refunds-due">
                An advance is due back:{" "}
                {refundsDue.map((o, i) => (
                  <span key={o.id}>
                    {i > 0 && "; "}
                    <Link
                      className="link"
                      to={{ search: `?open=${encodeURIComponent(o.ref)}` }}
                      onClick={() => setFilter("")}
                    >
                      {o.ref}
                    </Link>
                    , {formatINR(o.advance_balance_paise)} to {o.customer_name}
                  </span>
                ))}
                .
              </Note>
            )}
            <SplitView
              pane={
                open && (
                  <OrderPane
                    key={open.id}
                    order={open}
                    sources={listing.sources}
                    writable={writable}
                    onVoucher={() => setShown(open)}
                    send={step}
                  />
                )
              }
            >
              {rows.length === 0 ? (
                <EmptyState testId="special-order-none">No special orders to show.</EmptyState>
              ) : showing.length === 0 ? (
                <EmptyState>Nothing at this stage.</EmptyState>
              ) : (
                <div className="table-wrap capped">
                  <table className="data" data-testid="special-order-cards">
                    <thead>
                      <tr>
                        <th scope="col">Order</th>
                        <th scope="col">Customer</th>
                        <th scope="col">Wants</th>
                        <th scope="col">Stage</th>
                        <th scope="col" className="p2">
                          Ordered as
                        </th>
                        <th scope="col" className="p3">
                          Voucher
                        </th>
                        <th scope="col" className="num">
                          Advance
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {showing.map((o) => {
                        const chip = statusChip(o.status);
                        const selected = o.id === open?.id;
                        return (
                          <tr
                            key={o.id}
                            className={selected ? "is-selected" : undefined}
                            data-testid={`special-order-${o.ref}`}
                          >
                            <td>
                              <Link
                                className="order-ref"
                                to={{ search: `?open=${encodeURIComponent(o.ref)}` }}
                                aria-current={selected ? "true" : undefined}
                              >
                                {o.ref}
                              </Link>
                              <span className="sub">{dayText(o.asked_on)}</span>
                            </td>
                            <td>
                              <span className="order-who">{o.customer_name}</span>
                              <span className="sub">{o.customer_mobile}</span>
                            </td>
                            <td>
                              {[o.brand_name, o.style_code].filter(Boolean).join(" · ")}
                              <span className="sub">
                                {[o.colour, o.size].filter(Boolean).join(", ") || "—"}
                              </span>
                            </td>
                            <td>
                              <Status tone={toneOf(chip.tone)}>{chip.label}</Status>
                            </td>
                            <td className="p2">{o.route ? routeText(o) : "—"}</td>
                            <td className="p3 mono">{o.voucher?.number ?? "—"}</td>
                            <td className="num">{advanceText(o)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </SplitView>
          </Section>
        )}
      </div>
    </div>
  );
}

/** The list's view filters: what is still being worked, what needs the
 *  customer told, and the two ways an order ends. */
const VIEWS = [
  { key: "open", label: "Open" },
  { key: "arrived", label: "Arrived" },
  { key: "collected", label: "Collected" },
  { key: "cancelled", label: "Cancelled" },
] as const;

function inView(o: SpecialOrder, view: string): boolean {
  if (!view) return true;
  if (view === "open") return isOpen(o);
  return o.status === view;
}

/** The library's chip tones as Status dots: navy (closed) reads as grey. */
function toneOf(tone: string): StatusTone {
  return tone === "navy" ? "grey" : (tone as StatusTone);
}

/** What a finished step recorded, in a line under its word. */
function stepNote(o: SpecialOrder, s: (typeof STEPS)[number]): string {
  switch (s) {
    case "asked":
      return dayText(o.asked_on);
    case "ordered":
      return [routeText(o), o.ordered_at && formatDateTime(o.ordered_at)]
        .filter(Boolean)
        .join(", ");
    case "arrived":
      return [
        o.arrived_barcode && `Piece ${o.arrived_barcode}`,
        o.arrived_at && formatDateTime(o.arrived_at),
      ]
        .filter(Boolean)
        .join(", ");
    case "told":
      return [o.told_how && toldText(o.told_how), o.told_at && formatDateTime(o.told_at)]
        .filter(Boolean)
        .join(", ");
    case "collected":
      return o.sale_doc_number ? `Bill ${o.sale_doc_number}` : "";
  }
}

/** One order, open beside the list, with the one next step it can take done
 *  where that step stands. */
function OrderPane({
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
  const [route, setRoute] = useState<"transfer" | "booking">(
    sources.length ? "transfer" : "booking",
  );
  const [source, setSource] = useState(sources[0]?.code ?? "");
  const [barcode, setBarcode] = useState("");
  const [booking, setBooking] = useState("");
  // Null until typed: the barcode asked for on the transfer request stands in.
  const [arrived, setArrived] = useState<string | null>(null);
  const piece = arrived ?? o.ordered_barcode;
  const reached = STEPS.indexOf(o.status as (typeof STEPS)[number]);

  /** The controls for the step after the last one done. */
  function nextStep() {
    if (o.status === "asked")
      return (
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
          <div className="order-actions">
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
          </div>
        </fieldset>
      );
    if (o.status === "ordered")
      return (
        <div className="order-step-row">
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
              void send(
                o,
                "arrive",
                { barcode: piece.trim() },
                (after) => `${after.ref} has arrived. Tell the customer.`,
              )
            }
          >
            Arrived
          </button>
        </div>
      );
    if (o.status === "arrived")
      return (
        <div className="order-step-row" data-testid={`special-order-tell-${o.ref}`}>
          <span className="order-sub">Customer told by</span>
          {(["call", "message", "in_person"] as const).map((how) => (
            <button
              key={how}
              type="button"
              className="btn btn-sm"
              data-testid={`special-order-told-${how}-${o.ref}`}
              disabled={!writable}
              onClick={() =>
                void send(
                  o,
                  "tell",
                  { told_how: how },
                  (after) => `${after.ref}: customer told ${toldText(how)}.`,
                )
              }
            >
              {toldText(how)
                .replace(/^by /, "")
                .replace(/^./, (c) => c.toUpperCase())}
            </button>
          ))}
        </div>
      );
    if (o.status === "told")
      return (
        <p className="order-sub">
          To collect: at Billing, press Special order collection and type the order number.
        </p>
      );
    return null;
  }

  return (
    <Pane label={`Special order ${o.ref}`} testId="special-order-pane">
      <div className="order-pane-head">
        <div>
          <div className="order-pane-title">
            <h2 className="heading">{o.ref}</h2>
            <Status tone={toneOf(chip.tone)}>{chip.label}</Status>
          </div>
          <p className="order-sub">
            {o.customer_name}, {o.customer_mobile}
          </p>
        </div>
        <Link className="btn btn-sm order-close" to={{ search: "" }} aria-label="Close the order">
          <X size={16} aria-hidden="true" />
        </Link>
      </div>
      <div>
        <div className="order-who" data-testid={`special-order-item-${o.ref}`}>
          {itemText(o)}
        </div>
        {o.note && <p className="order-sub">{o.note}</p>}
      </div>
      {o.status === "cancelled" ? (
        <p className="order-sub" data-testid={`special-order-steps-${o.ref}`}>
          Cancelled
        </p>
      ) : (
        <ol
          className="order-timeline"
          aria-label="Stages"
          data-testid={`special-order-steps-${o.ref}`}
        >
          {STEPS.map((s, i) => {
            const state = i <= reached ? "is-done" : i === reached + 1 ? "is-current" : "";
            const note = i <= reached ? stepNote(o, s) : "";
            return (
              <li
                key={s}
                className={state || undefined}
                aria-current={state === "is-current" ? "step" : undefined}
              >
                <span className="order-timeline-n" aria-hidden="true">
                  {i <= reached ? <Check size={12} strokeWidth={2.6} /> : i + 1}
                </span>
                <div className="order-timeline-body">
                  <span className="order-timeline-word">
                    {statusChip(s).label}
                    {i <= reached && <span className="sr-only">, done</span>}
                  </span>
                  {note && <p className="order-sub">{note}</p>}
                  {state === "is-current" && nextStep()}
                </div>
              </li>
            );
          })}
        </ol>
      )}
      <dl className="dl">
        <dt>Advance</dt>
        <dd data-testid={`special-order-advance-${o.ref}`}>{advanceText(o)}</dd>
        {o.voucher && (
          <>
            <dt>Receipt voucher</dt>
            <dd className="mono">{o.voucher.number}</dd>
          </>
        )}
        {o.close_reason && (
          <>
            <dt>Closed</dt>
            <dd>{o.close_reason}</dd>
          </>
        )}
      </dl>
      {(o.voucher || o.advance_outcome === "refund_due") && (
        <div className="order-actions">
          {o.voucher && (
            <button type="button" className="btn" onClick={onVoucher}>
              <Printer size={16} aria-hidden="true" /> Voucher
            </button>
          )}
          {o.advance_outcome === "refund_due" && (
            <button
              type="button"
              className="btn btn-cta"
              data-testid={`special-order-refund-${o.ref}`}
              disabled={!writable}
              onClick={() =>
                void send(
                  o,
                  "refund",
                  {},
                  (after) =>
                    `${after.ref}: ${advanceText(after)} by ${after.voucher?.mode.toUpperCase() ?? "cash"}.`,
                )
              }
            >
              Refund {formatINR(o.advance_balance_paise)}
            </button>
          )}
        </div>
      )}
      {isOpen(o) && (
        <div className="order-cancel">
          <span className="order-cancel-label">Cancel the order</span>
          <div className="order-actions">
            <button
              type="button"
              className="btn btn-danger"
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
              className="btn btn-danger"
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
          </div>
        </div>
      )}
    </Pane>
  );
}

/** The receipt voucher as the customer takes it away. */
function Voucher({ order, onClose }: { order: SpecialOrder; onClose: () => void }) {
  const v = order.voucher;
  if (!v) return null;
  return (
    <Section heading={`Receipt voucher ${v.number}`} testId="special-order-voucher">
      <Box className="order-voucher">
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
      </Box>
      <div className="order-actions">
        <button type="button" className="btn btn-primary" onClick={() => window.print()}>
          <Printer size={16} aria-hidden="true" /> Print
        </button>
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
    </Section>
  );
}
