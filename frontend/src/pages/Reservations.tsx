import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Plus, Printer, X } from "lucide-react";

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
import { formatINR } from "../lib/format";
import {
  RESERVATION_OFFLINE,
  RESERVATIONS_API,
  TENDERS,
  addScan,
  advanceText,
  dayText,
  daysLeftText,
  draftProblem,
  emptyDraft,
  itemLabel,
  reservationRequest,
  statusChip,
} from "../lib/reservations";
import type { Reservation, ReservationDraft, Tender } from "../lib/reservations";

type Listing = ApiRead<ApiSchemas["ReservationList"]>;
type Terms = ApiRead<ApiSchemas["ReservationTerms"]>;

/** The list's view filters, in the order a reservation lives. */
const STATUSES = ["active", "collected", "expired", "cancelled"] as const;

/** Customer Orders > Reservations (store operations ticket 20, ST-ORD-1).
 *
 *  Staff hold specific pieces for a named customer: the pieces leave the
 *  counter until collected, for 7 days (3 during an end-of-season sale). An
 *  optional advance in cash, card or UPI gets a receipt voucher (RV series, no
 *  GST). The customer collects at Billing, where the advance pays towards the
 *  bill; on cancellation or expiry the pieces come back and the advance is
 *  refunded or kept as the terms said. Online only: offline the page says so
 *  and keeps what was typed. The server decides everything; this page shows it. */
export function ReservationsPage() {
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

  const [draft, setDraft] = useState<ReservationDraft>(emptyDraft);
  const [scan, setScan] = useState("");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState<Reservation | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // When a request last went unanswered; a list read after it clears it.
  const [lostAt, setLostAt] = useState(0);
  const [busy, setBusy] = useState(false);
  // One reservation's identity across retries: a dropped connection after the
  // server saved it replays the same reservation instead of making a second.
  const [commandId, setCommandId] = useState(() => crypto.randomUUID());
  // The same for a cancel or refund tap, per reservation: kept only while the
  // answer is lost, so a later, separate attempt is judged afresh.
  const attempts = useRef(new Map<string, string>());
  // The open reservation is in the URL, so back, refresh and a shared link
  // reopen it; the form and the list's filter are this visit's.
  const [params] = useSearchParams();
  const openRef = params.get("open") ?? "";
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState<string>("active");

  // The store's list, from the shared cache: a revisit shows the last list
  // at once and refreshes it behind it. A search is part of the URL.
  const read = useCachedGet<Listing>(store ? RESERVATIONS_API : null, {
    params: { store, q: query.trim() },
  });
  const { reload } = read;
  // The store's terms: never another store's standing in while these load.
  const termsRead = useCachedGet<Terms>(store ? `${RESERVATIONS_API}/terms` : null, {
    params: { store },
    keepPrevious: false,
  });
  const { reload: reloadTerms } = termsRead;
  const terms = termsRead.data ?? null;
  // Only this store's list: another store's stays off screen while this one loads.
  const listing = read.data && read.data.store === store ? read.data : null;
  const failed = read.error ?? termsRead.error;
  const readLost = failed !== null && isConnectionLost(failed);
  const lost = readLost || lostAt > (read.updatedAt ?? 0);
  const readError = failed !== null && !readLost ? apiErrorMessage(failed) : "";
  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    reload();
    reloadTerms();
  }, [reload, reloadTerms]);

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

  /** A search looks across every status, so what it finds is on screen. */
  const search = useCallback((term: string) => {
    setQuery(term);
    if (term.trim()) setFilter("");
  }, []);

  /** One write. A dropped connection keeps everything as typed and the command's
   *  identity; any answer from the server - saved or refused - ends that command. */
  async function send<T>(
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

  async function reserve() {
    const problem = draftProblem(draft);
    if (problem) {
      setError(problem);
      return;
    }
    const made = await send<Reservation>(
      RESERVATIONS_API,
      reservationRequest(commandId, store, draft),
      () => setCommandId(crypto.randomUUID()),
    );
    if (!made) return;
    setDraft(emptyDraft());
    setShown(made);
    setDone(`Reserved as ${made.ref}. Collect by ${dayText(made.collect_by)}.`);
    load();
  }

  async function cancel(r: Reservation, by: "customer" | "store") {
    const key = `cancel:${r.id}`;
    const after = await send<Reservation>(
      `${RESERVATIONS_API}/${r.id}/cancel`,
      { store, by, command_id: attemptId(key) },
      () => attempts.current.delete(key),
    );
    if (!after) return;
    setShown(null);
    setDone(`${after.ref} cancelled. The pieces are back on sale. Advance: ${advanceText(after)}.`);
    load();
  }

  async function refund(r: Reservation) {
    const key = `refund:${r.id}`;
    const after = await send<Reservation>(
      `${RESERVATIONS_API}/${r.id}/refund`,
      { store, command_id: attemptId(key) },
      () => attempts.current.delete(key),
    );
    if (!after) return;
    setDone(
      `${after.ref}: ${advanceText(after)} by ${after.voucher?.mode.toUpperCase() ?? "cash"}.`,
    );
    load();
  }

  function attemptId(key: string): string {
    const held = attempts.current.get(key);
    if (held) return held;
    const fresh = crypto.randomUUID();
    attempts.current.set(key, fresh);
    return fresh;
  }

  function addPiece() {
    setDraft({ ...draft, lines: addScan(draft.lines, scan) });
    setScan("");
  }

  const offline = !online || lost;
  const writable = !offline && !busy;
  const offlineNote = offline && (
    <Note
      tone="warn"
      role="status"
      testId="reservations-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="reservations-retry"
            onClick={() => load()}
          >
            Try again
          </button>
        )
      }
    >
      {RESERVATION_OFFLINE}
    </Note>
  );
  const withAdvance = draft.advance.trim() !== "";
  const rows = listing?.reservations ?? [];
  const open = rows.find((r) => r.ref === openRef) ?? null;
  const showing = filter ? rows.filter((r) => r.status === filter) : rows;
  const refundsDue = rows.filter((r) => r.advance_outcome === "refund_due");

  return (
    <div className="page-pad">
      <PageHeader
        title="Reservations"
        lead="Hold pieces for a named customer. The pieces leave the counter until they are collected at Billing."
        actions={
          listing?.switched_on && (
            <button
              type="button"
              className="btn btn-primary"
              aria-expanded={adding}
              data-testid="reservation-new"
              onClick={() => setAdding(!adding)}
            >
              <Plus size={16} aria-hidden="true" /> New reservation
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
              data-testid="reservations-store"
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
        {offlineNote}
        {(error || readError) && (
          <Note tone="error" testId="reservations-error">
            {error || readError}
          </Note>
        )}
        {done && (
          <Note tone="success" role="status" testId="reservations-done">
            {done}
          </Note>
        )}
        {!store && <EmptyState>Choose a store to see its reservations.</EmptyState>}
        {store && !listing && !offline && !error && !readError && (
          <Loading label="Loading reservations" />
        )}

        {shown?.voucher && <Voucher reservation={shown} onClose={() => setShown(null)} />}

        {listing && terms && !listing.switched_on && (
          <Note tone="warn" testId="reservations-switched-off">
            Customer reservations are switched off at {listing.store}. No new reservation can be
            made; the ones below can still be collected, cancelled and refunded.
          </Note>
        )}

        {listing && terms && listing.switched_on && adding && (
          <Section heading="New reservation" testId="reservation-form">
            <p className="muted-cell" data-testid="reservation-length">
              Held for {terms.days} days, until {dayText(terms.collect_by)}
              {terms.sale_period ? " (shorter while the end-of-season sale is running)" : ""}.
            </p>
            <div className="form-grid">
              <label className="field">
                <span>Customer name</span>
                <input
                  className="input"
                  data-testid="reservation-name"
                  value={draft.customer_name}
                  onChange={(e) => setDraft({ ...draft, customer_name: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Mobile</span>
                <input
                  className="input"
                  inputMode="tel"
                  data-testid="reservation-mobile"
                  value={draft.customer_mobile}
                  onChange={(e) => setDraft({ ...draft, customer_mobile: e.target.value })}
                />
              </label>
            </div>
            <label className="field">
              <span>Scan or type each piece's barcode</span>
              <input
                className="input"
                data-testid="reservation-scan"
                value={scan}
                onChange={(e) => setScan(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addPiece();
                  }
                }}
              />
            </label>
            {draft.lines.length > 0 && (
              <ul className="order-pieces" data-testid="reservation-lines">
                {draft.lines.map((l) => (
                  <li key={l.barcode} data-testid={`reservation-line-${l.barcode}`}>
                    <span>
                      <span className="mono">{l.barcode}</span> × {l.qty}
                    </span>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() =>
                        setDraft({ ...draft, lines: draft.lines.filter((x) => x !== l) })
                      }
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="form-grid">
              <label className="field">
                <span>Advance in rupees (optional)</span>
                <input
                  className="input"
                  inputMode="decimal"
                  data-testid="reservation-advance"
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
                        name="reservation-mode"
                        data-testid={`reservation-mode-${t.value}`}
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
                    data-testid="reservation-reference"
                    value={draft.reference}
                    onChange={(e) => setDraft({ ...draft, reference: e.target.value })}
                  />
                </label>
              )}
            </div>
            <p className="muted-cell" data-testid="reservation-terms">
              {withAdvance ? terms.terms_with_advance : terms.terms_without_advance}
            </p>
            <div className="order-actions">
              <button
                type="button"
                className="btn btn-primary"
                data-testid="reservation-save"
                disabled={!writable}
                onClick={() => void reserve()}
              >
                Reserve
              </button>
            </div>
          </Section>
        )}

        {listing && terms && (
          <Section heading={`Reservations at ${listing.store}`} testId="reservation-list">
            <div className="orders-bar">
              <FilterChips label="Status">
                {STATUSES.map((s) => (
                  <FilterChip
                    key={s}
                    pressed={filter === s}
                    count={rows.filter((r) => r.status === s).length}
                    onClick={() => setFilter(filter === s ? "" : s)}
                    testId={`reservation-show-${s}`}
                  >
                    {statusChip(s).label}
                  </FilterChip>
                ))}
              </FilterChips>
              <SearchBox
                label="Find a reservation"
                placeholder="Reservation number, voucher number or mobile"
                testId="reservation-search"
                value={query}
                onChange={search}
              />
            </div>
            {refundsDue.length > 0 && (
              <Note tone="warn" testId="reservation-refunds-due">
                An advance is due back:{" "}
                {refundsDue.map((r, i) => (
                  <span key={r.id}>
                    {i > 0 && "; "}
                    <Link
                      className="link"
                      to={{ search: `?open=${encodeURIComponent(r.ref)}` }}
                      onClick={() => setFilter("")}
                    >
                      {r.ref}
                    </Link>
                    , {formatINR(r.advance_balance_paise)} to {r.customer_name}
                  </span>
                ))}
                .
              </Note>
            )}
            <SplitView
              pane={
                open && (
                  <ReservationPane
                    key={open.id}
                    reservation={open}
                    writable={writable}
                    onVoucher={() => setShown(open)}
                    onCancel={(by) => void cancel(open, by)}
                    onRefund={() => void refund(open)}
                  />
                )
              }
            >
              {rows.length === 0 ? (
                <EmptyState testId="reservation-none">No reservations to show.</EmptyState>
              ) : showing.length === 0 ? (
                <EmptyState>No {statusChip(filter).label.toLowerCase()} reservations.</EmptyState>
              ) : (
                <div className="table-wrap capped">
                  <table className="data" data-testid="reservation-cards">
                    <thead>
                      <tr>
                        <th scope="col">Reservation</th>
                        <th scope="col">Customer</th>
                        <th scope="col">Pieces</th>
                        <th scope="col">Collect by</th>
                        <th scope="col" className="p2">
                          Reserved
                        </th>
                        <th scope="col" className="p3">
                          Voucher
                        </th>
                        <th scope="col" className="num">
                          Advance
                        </th>
                        <th scope="col">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {showing.map((r) => {
                        const chip = statusChip(r.status);
                        const selected = r.id === open?.id;
                        const pieces = r.pieces.reduce((n, p) => n + p.qty, 0);
                        return (
                          <tr
                            key={r.id}
                            className={selected ? "is-selected" : undefined}
                            data-testid={`reservation-${r.ref}`}
                          >
                            <td>
                              <Link
                                className="order-ref"
                                to={{ search: `?open=${encodeURIComponent(r.ref)}` }}
                                aria-current={selected ? "true" : undefined}
                              >
                                {r.ref}
                              </Link>
                            </td>
                            <td>
                              <span className="order-who">{r.customer_name}</span>
                              <span className="sub">{r.customer_mobile}</span>
                            </td>
                            <td>
                              {r.pieces.map((p) => itemLabel(p.item)).join(", ")}
                              <span className="sub">
                                {pieces === 1 ? "1 piece" : `${pieces} pieces`}
                              </span>
                            </td>
                            <td>
                              {dayText(r.collect_by)}
                              {r.status === "active" && (
                                <span className="sub">{daysLeftText(r.days_left)}</span>
                              )}
                            </td>
                            <td className="p2">{dayText(r.reserved_on)}</td>
                            <td className="p3 mono">{r.voucher?.number ?? "—"}</td>
                            <td className="num">{advanceText(r)}</td>
                            <td>
                              <Status tone={toneOf(chip.tone)}>{chip.label}</Status>
                            </td>
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

/** The library's chip tones as Status dots: navy (closed) reads as grey. */
function toneOf(tone: string): StatusTone {
  return tone === "navy" ? "grey" : (tone as StatusTone);
}

/** One reservation, open beside the list: who, until when, the advance, the
 *  pieces held, and what can still be done with it. */
function ReservationPane({
  reservation: r,
  writable,
  onVoucher,
  onCancel,
  onRefund,
}: {
  reservation: Reservation;
  writable: boolean;
  onVoucher: () => void;
  onCancel: (by: "customer" | "store") => void;
  onRefund: () => void;
}) {
  const chip = statusChip(r.status);
  const total = r.pieces.reduce((sum, p) => sum + (p.item.mrp_paise ?? 0) * p.qty, 0);
  const priced = r.pieces.every((p) => p.item.mrp_paise != null);
  return (
    <Pane label={`Reservation ${r.ref}`} testId="reservation-pane">
      <div className="order-pane-head">
        <div>
          <div className="order-pane-title">
            <h2 className="heading">{r.ref}</h2>
            <Status tone={toneOf(chip.tone)}>{chip.label}</Status>
          </div>
          <p className="order-sub">Reserved {dayText(r.reserved_on)}</p>
        </div>
        <Link
          className="btn btn-sm order-close"
          to={{ search: "" }}
          aria-label="Close the reservation"
        >
          <X size={16} aria-hidden="true" />
        </Link>
      </div>
      <dl className="dl">
        <dt>Customer</dt>
        <dd>
          <span className="order-who">{r.customer_name}</span>, {r.customer_mobile}
        </dd>
        <dt>Collect by</dt>
        <dd>
          {dayText(r.collect_by)}
          {r.status === "active" && `, ${daysLeftText(r.days_left).toLowerCase()}`}
        </dd>
        <dt>Advance</dt>
        <dd data-testid={`reservation-advance-${r.ref}`}>{advanceText(r)}</dd>
        {r.voucher && (
          <>
            <dt>Receipt voucher</dt>
            <dd className="mono">{r.voucher.number}</dd>
          </>
        )}
        {r.sale_doc_number && (
          <>
            <dt>Bill</dt>
            <dd className="mono">{r.sale_doc_number}</dd>
          </>
        )}
        {r.close_reason && (
          <>
            <dt>Closed</dt>
            <dd>{r.close_reason}</dd>
          </>
        )}
      </dl>
      <Section heading="Pieces held" level={3}>
        <ul className="order-pieces">
          {r.pieces.map((p) => (
            <li key={p.line_no}>
              <span>
                {itemLabel(p.item)} × {p.qty}
              </span>
              <span className="num">
                {p.item.mrp_paise != null ? formatINR(p.item.mrp_paise * p.qty) : "—"}
              </span>
              <span className="order-sub mono">{p.barcode}</span>
            </li>
          ))}
          {priced && r.pieces.length > 1 && (
            <li className="order-who">
              <span>{r.pieces.length} lines</span>
              <span className="num">{formatINR(total)}</span>
            </li>
          )}
        </ul>
      </Section>
      {r.status === "active" && (
        <Note>
          To collect: at Billing, press Reservation pickup and type the reservation number.
        </Note>
      )}
      {(r.voucher || r.advance_outcome === "refund_due") && (
        <div className="order-actions">
          {r.voucher && (
            <button type="button" className="btn" onClick={onVoucher}>
              <Printer size={16} aria-hidden="true" /> Voucher
            </button>
          )}
          {r.advance_outcome === "refund_due" && (
            <button
              type="button"
              className="btn btn-cta"
              data-testid={`reservation-refund-${r.ref}`}
              disabled={!writable}
              onClick={onRefund}
            >
              Refund {formatINR(r.advance_balance_paise)}
            </button>
          )}
        </div>
      )}
      {r.status === "active" && (
        <div className="order-cancel">
          <span className="order-cancel-label">Cancel the reservation</span>
          <div className="order-actions">
            <button
              type="button"
              className="btn btn-danger"
              data-testid={`reservation-cancel-customer-${r.ref}`}
              disabled={!writable}
              onClick={() => onCancel("customer")}
            >
              Customer cancels
            </button>
            <button
              type="button"
              className="btn btn-danger"
              data-testid={`reservation-cancel-store-${r.ref}`}
              disabled={!writable}
              onClick={() => onCancel("store")}
            >
              Store cancels
            </button>
          </div>
          <p className="order-sub">
            The pieces go back on the counter. The advance is kept or refunded as the voucher's
            terms say.
          </p>
        </div>
      )}
    </Pane>
  );
}

/** The receipt voucher as the customer takes it away. */
function Voucher({ reservation, onClose }: { reservation: Reservation; onClose: () => void }) {
  const v = reservation.voucher;
  if (!v) return null;
  return (
    <Section heading={`Receipt voucher ${v.number}`} testId="reservation-voucher">
      <Box className="order-voucher">
        <p>
          {v.store_name} · GSTIN {v.store_gstin}
          <br />
          Date {dayText(v.issued_on)} · Reservation {reservation.ref}
        </p>
        <p>
          Received from {reservation.customer_name} ({reservation.customer_mobile}):{" "}
          <strong>{formatINR(v.amount_paise)}</strong> by {v.mode.toUpperCase()}
          {v.reference ? ` (${v.reference})` : ""}.
        </p>
        <ul className="plain-list">
          {reservation.pieces.map((p) => (
            <li key={p.line_no}>
              {itemLabel(p.item)} × {p.qty}
            </li>
          ))}
        </ul>
        <p data-testid="reservation-voucher-terms">{reservation.terms}</p>
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
