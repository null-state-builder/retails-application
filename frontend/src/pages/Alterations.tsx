import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import "./Reservations.css";

import { useAuth, allowedUnits } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  ALTERATIONS_API,
  ALTERATIONS_OFFLINE,
  CUSTODY_WORDS,
  STEP_WORDS,
  chargeText,
  draftProblem,
  emptyDraft,
  fromBill,
  garmentLabel,
  isOpen,
  jobRequest,
  momentText,
  nextSteps,
  promisedText,
  statusChip,
} from "../lib/alterations";
import type { AlterationBill, AlterationJob, JobDraft, Step } from "../lib/alterations";
import { formatINR, StatusChip } from "../lib/format";
import { tillToday } from "../till/pricing";

type Listing = ApiRead<ApiSchemas["AlterationList"]>;

/** Customer Orders > Alterations (store operations ticket 22, ST-ORD-3).
 *
 *  A job card per alteration, made from the garment's bill: what to alter, the
 *  measurements, the tailor (in-house or outside), the promised date and any
 *  charge - a paid alteration is its own line on a bill, made at Billing with
 *  "Alteration charge"; a free one has none. The card moves received → with
 *  the tailor → ready → handed over, records when the customer was told, and
 *  the store holds the garment for the customer until handover. Online only:
 *  offline the page says so and keeps what was typed. The server decides
 *  everything; this page shows it. */
export function AlterationsPage() {
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

  const [listing, setListing] = useState<Listing | null>(null);
  const [draft, setDraft] = useState<JobDraft>(emptyDraft);
  const [bill, setBill] = useState<AlterationBill | null>(null);
  const [billNumber, setBillNumber] = useState("");
  const [otherCharge, setOtherCharge] = useState(false);
  const [query, setQuery] = useState("");
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  // One job card's identity across retries: a dropped connection after the
  // server saved it replays the same job card instead of making a second.
  const [commandId, setCommandId] = useState(() => crypto.randomUUID());
  // The same for a step, per job card and step: kept only while the answer is
  // lost, so a later, separate attempt is judged afresh.
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
        const l = await api.get<Listing>(ALTERATIONS_API, {
          params: q ? { store, q } : { store },
        });
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

  async function findBill() {
    const number = billNumber.trim();
    if (!number) return;
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    setDone("");
    try {
      const found = await api.get<AlterationBill>(`${ALTERATIONS_API}/bill`, {
        params: { store, number },
      });
      setBill(found.data);
      setOtherCharge(false);
      setDraft(fromBill(draft, found.data));
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }

  async function save() {
    const problem = draftProblem(draft, tillToday());
    if (problem) {
      setError(problem);
      return;
    }
    const made = await send<AlterationJob>(
      ALTERATIONS_API,
      jobRequest(commandId, store, draft),
      () => setCommandId(crypto.randomUUID()),
    );
    if (!made) return;
    setDraft(emptyDraft());
    setBill(null);
    setBillNumber("");
    setDone(`Job card ${made.ref} made. ${promisedText(made)}.`);
    await load(query);
  }

  async function step(job: AlterationJob, which: Step) {
    const key = `${which}:${job.id}`;
    const after = await send<AlterationJob>(
      `${ALTERATIONS_API}/${job.id}/${which}`,
      { store, command_id: attemptId(key) },
      () => attempts.current.delete(key),
    );
    if (!after) return;
    setDone(`${after.ref}: ${STEP_WORDS[which].toLowerCase()}.`);
    await load(query);
  }

  async function cancel(job: AlterationJob) {
    const key = `cancel:${job.id}`;
    const after = await send<AlterationJob>(
      `${ALTERATIONS_API}/${job.id}/cancel`,
      { store, reason, command_id: attemptId(key) },
      () => attempts.current.delete(key),
    );
    if (!after) return;
    setCancelling(null);
    setReason("");
    setDone(`${after.ref} cancelled. The garment goes back to the customer as it is.`);
    await load(query);
  }

  function attemptId(key: string): string {
    const held = attempts.current.get(key);
    if (held) return held;
    const fresh = crypto.randomUUID();
    attempts.current.set(key, fresh);
    return fresh;
  }

  const offline = !online || lost;
  const writable = !offline && !busy;
  const garment = bill?.garments.find((g) => g.line_no === draft.line_no) ?? null;
  const offlineNote = offline && (
    <p className="warn-note" data-testid="alterations-offline" role="status">
      {ALTERATIONS_OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="alterations-retry"
            onClick={() => void load(query)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );

  return (
    <div className="page-pad">
      <PageHeader
        title="Alterations"
        lead="A job card for each alteration, from the garment's bill. The store holds the garment for the customer until it is handed over."
      />
      {stores.length > 1 && (
        <label className="field">
          <span>Store</span>
          <select
            className="input"
            data-testid="alterations-store"
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
        <p className="warn-note" data-testid="alterations-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="alterations-done">
          {done}
        </p>
      )}
      {!store && <p>Choose a store to see its job cards.</p>}
      {store && !listing && !offline && !error && <p>Loading job cards…</p>}

      {listing && (
        <>
          {!listing.switched_on ? (
            <p className="warn-note" data-testid="alterations-switched-off">
              Alterations are switched off at {listing.store}. No new job card can be made; the ones
              below can still be moved on and handed over.
            </p>
          ) : (
            <section className="card section-card" data-testid="alteration-form">
              <h2 className="h3">New job card</h2>
              <p className="muted-cell">
                A paid alteration is charged at Billing with "Alteration charge" (its own line, GST
                5%, SAC 9988). A free alteration has no charge.
              </p>
              <div className="toolbar">
                <input
                  className="input"
                  aria-label="The garment's bill number"
                  placeholder="The garment's bill number"
                  data-testid="alteration-bill"
                  value={billNumber}
                  onChange={(e) => setBillNumber(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void findBill();
                    }
                  }}
                />
                <button
                  type="button"
                  className="btn"
                  data-testid="alteration-bill-find"
                  disabled={offline}
                  onClick={() => void findBill()}
                >
                  Find bill
                </button>
              </div>

              {bill && (
                <>
                  <fieldset className="field" data-testid="alteration-garments">
                    <legend>Garment on {bill.printed}</legend>
                    {bill.garments.length === 0 && <p>This bill has no garment on it.</p>}
                    {bill.garments.map((g) => (
                      <label key={g.line_no} className="radio">
                        <input
                          type="radio"
                          name="alteration-garment"
                          data-testid={`alteration-garment-${g.line_no}`}
                          disabled={g.free_qty === 0}
                          checked={draft.line_no === g.line_no}
                          onChange={() => setDraft({ ...draft, line_no: g.line_no, qty: 1 })}
                        />{" "}
                        Line {g.line_no}: {garmentLabel(g.garment)}
                        {g.free_qty === 0 ? " (already on a job card or returned)" : ""}
                      </label>
                    ))}
                  </fieldset>
                  {garment && garment.free_qty > 1 && (
                    <label className="field">
                      <span>Pieces to alter (up to {garment.free_qty})</span>
                      <input
                        className="input"
                        inputMode="numeric"
                        data-testid="alteration-qty"
                        value={String(draft.qty)}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            qty: Math.min(
                              Math.max(Number(e.target.value) || 1, 1),
                              garment.free_qty,
                            ),
                          })
                        }
                      />
                    </label>
                  )}
                  <fieldset className="field" data-testid="alteration-charges">
                    <legend>Charge</legend>
                    <label className="radio">
                      <input
                        type="radio"
                        name="alteration-charge"
                        data-testid="alteration-charge-free"
                        checked={draft.charge_line_no === null && !otherCharge}
                        onChange={() => {
                          setOtherCharge(false);
                          setDraft({ ...draft, charge_bill: "", charge_line_no: null });
                        }}
                      />{" "}
                      Free alteration (no charge, no tax)
                    </label>
                    {bill.charges.map((c) => (
                      <label key={c.line_no} className="radio">
                        <input
                          type="radio"
                          name="alteration-charge"
                          data-testid={`alteration-charge-${c.line_no}`}
                          checked={!otherCharge && draft.charge_line_no === c.line_no}
                          onChange={() => {
                            setOtherCharge(false);
                            setDraft({ ...draft, charge_bill: "", charge_line_no: c.line_no });
                          }}
                        />{" "}
                        Paid {formatINR(c.charge_paise)} on this bill (line {c.line_no})
                      </label>
                    ))}
                    <label className="radio">
                      <input
                        type="radio"
                        name="alteration-charge"
                        data-testid="alteration-charge-other"
                        checked={otherCharge}
                        onChange={() => {
                          setOtherCharge(true);
                          setDraft({ ...draft, charge_line_no: null });
                        }}
                      />{" "}
                      Paid on another bill
                    </label>
                    {otherCharge && (
                      <div className="form-grid">
                        <label className="field">
                          <span>That bill's number</span>
                          <input
                            className="input"
                            data-testid="alteration-charge-bill"
                            value={draft.charge_bill}
                            onChange={(e) => setDraft({ ...draft, charge_bill: e.target.value })}
                          />
                        </label>
                        <label className="field">
                          <span>Its alteration line</span>
                          <input
                            className="input"
                            inputMode="numeric"
                            data-testid="alteration-charge-line"
                            value={draft.charge_line_no ?? ""}
                            onChange={(e) =>
                              setDraft({
                                ...draft,
                                charge_line_no: Number(e.target.value) || null,
                              })
                            }
                          />
                        </label>
                      </div>
                    )}
                  </fieldset>
                  <div className="form-grid">
                    <label className="field">
                      <span>Customer name</span>
                      <input
                        className="input"
                        data-testid="alteration-name"
                        value={draft.customer_name}
                        onChange={(e) => setDraft({ ...draft, customer_name: e.target.value })}
                      />
                    </label>
                    <label className="field">
                      <span>Mobile</span>
                      <input
                        className="input"
                        inputMode="tel"
                        data-testid="alteration-mobile"
                        value={draft.customer_mobile}
                        onChange={(e) => setDraft({ ...draft, customer_mobile: e.target.value })}
                      />
                    </label>
                  </div>
                  <label className="field">
                    <span>What to alter</span>
                    <textarea
                      className="input"
                      rows={2}
                      data-testid="alteration-work"
                      value={draft.work}
                      onChange={(e) => setDraft({ ...draft, work: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span>Measurements</span>
                    <textarea
                      className="input"
                      rows={2}
                      data-testid="alteration-measurements"
                      value={draft.measurements}
                      onChange={(e) => setDraft({ ...draft, measurements: e.target.value })}
                    />
                  </label>
                  <fieldset className="field">
                    <legend>Tailor</legend>
                    <label className="radio">
                      <input
                        type="radio"
                        name="alteration-tailor"
                        data-testid="alteration-tailor-in_house"
                        checked={draft.tailor === "in_house"}
                        onChange={() => setDraft({ ...draft, tailor: "in_house" })}
                      />{" "}
                      In-house
                    </label>
                    <label className="radio">
                      <input
                        type="radio"
                        name="alteration-tailor"
                        data-testid="alteration-tailor-outside"
                        checked={draft.tailor === "outside"}
                        onChange={() => setDraft({ ...draft, tailor: "outside" })}
                      />{" "}
                      Outside
                    </label>
                  </fieldset>
                  <div className="form-grid">
                    <label className="field">
                      <span>
                        {draft.tailor === "outside" ? "Tailor's name" : "Tailor's name (optional)"}
                      </span>
                      <input
                        className="input"
                        data-testid="alteration-tailor-name"
                        value={draft.tailor_name}
                        onChange={(e) => setDraft({ ...draft, tailor_name: e.target.value })}
                      />
                    </label>
                    <label className="field">
                      <span>Promised date</span>
                      <input
                        type="date"
                        className="input"
                        data-testid="alteration-promised"
                        min={tillToday()}
                        value={draft.promised_on}
                        onChange={(e) => setDraft({ ...draft, promised_on: e.target.value })}
                      />
                    </label>
                  </div>
                  <button
                    type="button"
                    className="btn btn-primary"
                    data-testid="alteration-save"
                    disabled={!writable}
                    onClick={() => void save()}
                  >
                    Make job card
                  </button>
                </>
              )}
            </section>
          )}

          <section className="card section-card" data-testid="alteration-list">
            <h2 className="h3">Job cards at {listing.store}</h2>
            <div className="toolbar">
              <input
                className="input"
                aria-label="Find a job card"
                placeholder="Job card number, bill number or mobile"
                data-testid="alteration-search"
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
            {listing.jobs.length === 0 ? (
              <p data-testid="alteration-none">No job cards to show.</p>
            ) : (
              // One card per job card, stacked: a real layout at 375 px on the
              // shop floor (PRD §5.2 rule 2), not a squeezed table.
              <ul className="reservation-cards" data-testid="alteration-cards">
                {listing.jobs.map((job) => {
                  const chip = statusChip(job.status);
                  return (
                    <li
                      key={job.id}
                      className="reservation-card"
                      data-testid={`alteration-${job.ref}`}
                    >
                      <div className="reservation-card-head">
                        <strong>{job.ref}</strong>{" "}
                        <StatusChip status={chip.label} tone={chip.tone} />
                      </div>
                      <p>
                        {job.customer_name} ·{" "}
                        <span className="muted-cell">{job.customer_mobile}</span>
                      </p>
                      <p>
                        {garmentLabel(job.garment)} × {job.qty}
                        <span className="muted-cell">
                          {" "}
                          · bill {job.bill}, line {job.line_no}
                        </span>
                      </p>
                      <p data-testid={`alteration-work-${job.ref}`}>
                        {job.work}
                        {job.measurements && (
                          <span className="muted-cell"> · {job.measurements}</span>
                        )}
                      </p>
                      <p>
                        {job.tailor === "outside" ? "Outside tailor" : "In-house tailor"}
                        {job.tailor_name ? `: ${job.tailor_name}` : ""} ·{" "}
                        <span data-testid={`alteration-paid-${job.ref}`}>{chargeText(job)}</span>
                      </p>
                      <p>{promisedText(job)}</p>
                      <p className="muted-cell" data-testid={`alteration-custody-${job.ref}`}>
                        {job.custody.closed_at
                          ? `Custody ended ${momentText(job.custody.closed_at)}`
                          : `Held for the customer: ${CUSTODY_WORDS[job.custody.location] ?? job.custody.location}`}
                      </p>
                      {job.customer_told_at && (
                        <p data-testid={`alteration-told-at-${job.ref}`}>
                          Customer told {momentText(job.customer_told_at)}
                        </p>
                      )}
                      {job.cancel_reason && <p>Cancelled: {job.cancel_reason}</p>}
                      {isOpen(job.status) && (
                        <div className="toolbar">
                          {nextSteps(job).map((s) => (
                            <button
                              key={s}
                              type="button"
                              className={s === "hand-over" ? "btn btn-primary" : "btn"}
                              data-testid={`alteration-${s}-${job.ref}`}
                              disabled={!writable}
                              onClick={() => void step(job, s)}
                            >
                              {STEP_WORDS[s]}
                            </button>
                          ))}
                          {cancelling === job.id ? (
                            <>
                              <input
                                className="input"
                                aria-label="Why it is cancelled"
                                placeholder="Why it is cancelled"
                                data-testid={`alteration-cancel-reason-${job.ref}`}
                                value={reason}
                                onChange={(e) => setReason(e.target.value)}
                              />
                              <button
                                type="button"
                                className="btn"
                                data-testid={`alteration-cancel-confirm-${job.ref}`}
                                disabled={!writable || !reason.trim()}
                                onClick={() => void cancel(job)}
                              >
                                Cancel job card
                              </button>
                            </>
                          ) : (
                            <button
                              type="button"
                              className="btn"
                              data-testid={`alteration-cancel-${job.ref}`}
                              disabled={!writable}
                              onClick={() => {
                                setCancelling(job.id);
                                setReason("");
                              }}
                            >
                              Cancel
                            </button>
                          )}
                        </div>
                      )}
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
