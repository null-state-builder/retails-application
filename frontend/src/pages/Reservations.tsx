import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import "./Reservations.css";

import { useAuth, allowedUnits } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { formatINR, StatusChip } from "../lib/format";
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
    (stores.length === 1 ? stores[0].code : "");

  const [listing, setListing] = useState<Listing | null>(null);
  const [terms, setTerms] = useState<Terms | null>(null);
  const [draft, setDraft] = useState<ReservationDraft>(emptyDraft);
  const [scan, setScan] = useState("");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState<Reservation | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  // One reservation's identity across retries: a dropped connection after the
  // server saved it replays the same reservation instead of making a second.
  const [commandId, setCommandId] = useState(() => crypto.randomUUID());
  // The same for a cancel or refund tap, per reservation: kept only while the
  // answer is lost, so a later, separate attempt is judged afresh.
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
        const [l, t] = await Promise.all([
          api.get<Listing>(RESERVATIONS_API, { params: q ? { store, q } : { store } }),
          api.get<Terms>(`${RESERVATIONS_API}/terms`, { params: { store } }),
        ]);
        if (mine !== request.current) return;
        setLost(false);
        setListing(l.data);
        setTerms(t.data);
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

  /** One write. A dropped connection keeps everything as typed; any answer
   *  from the server ends that command. */
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
    await load(query);
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
    await load(query);
  }

  async function refund(r: Reservation) {
    const key = `refund:${r.id}`;
    const after = await send<Reservation>(
      `${RESERVATIONS_API}/${r.id}/refund`,
      { store, command_id: attemptId(key) },
      () => attempts.current.delete(key),
    );
    if (!after) return;
    setDone(`${after.ref}: ${advanceText(after)} by ${after.voucher?.mode.toUpperCase() ?? "cash"}.`);
    await load(query);
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
    <p className="warn-note" data-testid="reservations-offline" role="status">
      {RESERVATION_OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="reservations-retry"
            onClick={() => void load(query)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const withAdvance = draft.advance.trim() !== "";

  return (
    <div className="page-pad">
      <PageHeader
        title="Reservations"
        lead="Hold pieces for a named customer. The pieces leave the counter until they are collected at Billing."
      />
      {stores.length > 1 && (
        <label className="field">
          <span>Store</span>
          <select
            className="input"
            data-testid="reservations-store"
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
      {offlineNote}
      {error && (
        <p className="warn-note" data-testid="reservations-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="reservations-done">
          {done}
        </p>
      )}
      {!store && <p>Choose a store to see its reservations.</p>}
      {store && !listing && !offline && !error && <p>Loading reservations…</p>}

      {shown?.voucher && <Voucher reservation={shown} onClose={() => setShown(null)} />}

      {listing && terms && (
        <>
          {!listing.switched_on ? (
            <p className="warn-note" data-testid="reservations-switched-off">
              Customer reservations are switched off at {listing.store}. No new reservation can be
              made; the ones below can still be collected, cancelled and refunded.
            </p>
          ) : (
            <section className="card section-card" data-testid="reservation-form">
              <h2 className="h3">New reservation</h2>
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
                <ul className="plain-list" data-testid="reservation-lines">
                  {draft.lines.map((l) => (
                    <li key={l.barcode} data-testid={`reservation-line-${l.barcode}`}>
                      {l.barcode} × {l.qty}{" "}
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
              <button
                type="button"
                className="btn btn-primary"
                data-testid="reservation-save"
                disabled={!writable}
                onClick={() => void reserve()}
              >
                Reserve
              </button>
            </section>
          )}

          <section className="card section-card" data-testid="reservation-list">
            <h2 className="h3">Reservations at {listing.store}</h2>
            <p className="muted-cell">
              To collect: at Billing, press Reservation pickup and type the reservation number.
            </p>
            <div className="toolbar">
              <input
                className="input"
                aria-label="Find a reservation"
                placeholder="Reservation number, voucher number or mobile"
                data-testid="reservation-search"
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
            {listing.reservations.length === 0 ? (
              <p data-testid="reservation-none">No reservations to show.</p>
            ) : (
              // One card per reservation, stacked: a real layout at 375 px on
              // the shop floor (PRD §5.2 rule 2), not a squeezed table.
              <ul className="reservation-cards" data-testid="reservation-cards">
                {listing.reservations.map((r) => {
                  const chip = statusChip(r.status);
                  return (
                    <li key={r.id} className="reservation-card" data-testid={`reservation-${r.ref}`}>
                      <div className="reservation-card-head">
                        <strong>{r.ref}</strong> <StatusChip status={chip.label} tone={chip.tone} />
                      </div>
                      <p>
                        {r.customer_name} · <span className="muted-cell">{r.customer_mobile}</span>
                      </p>
                      <ul className="plain-list">
                        {r.pieces.map((p) => (
                          <li key={p.line_no}>
                            {itemLabel(p.item)} × {p.qty}
                          </li>
                        ))}
                      </ul>
                      <p>
                        Collect by {dayText(r.collect_by)}
                        {r.status === "active" && (
                          <span className="muted-cell"> · {daysLeftText(r.days_left)}</span>
                        )}
                      </p>
                      <p data-testid={`reservation-advance-${r.ref}`}>{advanceText(r)}</p>
                      <div className="toolbar">
                        {r.voucher && (
                          <button type="button" className="btn" onClick={() => setShown(r)}>
                            Voucher
                          </button>
                        )}
                        {r.status === "active" && (
                          <>
                            <button
                              type="button"
                              className="btn"
                              data-testid={`reservation-cancel-customer-${r.ref}`}
                              disabled={!writable}
                              onClick={() => void cancel(r, "customer")}
                            >
                              Customer cancels
                            </button>
                            <button
                              type="button"
                              className="btn"
                              data-testid={`reservation-cancel-store-${r.ref}`}
                              disabled={!writable}
                              onClick={() => void cancel(r, "store")}
                            >
                              Store cancels
                            </button>
                          </>
                        )}
                        {r.advance_outcome === "refund_due" && (
                          <button
                            type="button"
                            className="btn btn-primary"
                            data-testid={`reservation-refund-${r.ref}`}
                            disabled={!writable}
                            onClick={() => void refund(r)}
                          >
                            Refund {formatINR(r.advance_balance_paise)}
                          </button>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}

/** The receipt voucher as the customer takes it away. */
function Voucher({ reservation, onClose }: { reservation: Reservation; onClose: () => void }) {
  const v = reservation.voucher;
  if (!v) return null;
  return (
    <section className="card section-card" data-testid="reservation-voucher">
      <h2 className="h3">Receipt voucher {v.number}</h2>
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
