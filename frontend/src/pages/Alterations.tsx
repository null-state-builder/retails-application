import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, Plus, X } from "lucide-react";

import "./Reservations.css";

import { useAuth, allowedUnits } from "../auth/AuthContext";
import { FilterChip, FilterChips } from "../components/FilterChip";
import { Note } from "../components/Note";
import { PageHeader } from "../components/PageHeader";
import { SearchBox } from "../components/SearchBox";
import { Section } from "../components/Section";
import { Pane, SplitView } from "../components/SplitView";
import { EmptyState, Loading } from "../components/States";
import { Status } from "../components/Status";
import type { StatusTone } from "../components/Status";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  ALTERATIONS_API,
  ALTERATIONS_OFFLINE,
  CUSTODY_WORDS,
  STEP_WORDS,
  chargeText,
  dayText,
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
import { formatINR } from "../lib/format";
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
  // The open job card is in the URL, so back, refresh and a shared link
  // reopen it; the form and the list's filter are this visit's.
  const [params] = useSearchParams();
  const openRef = params.get("open") ?? "";
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState<string>("open");

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

  /** A search looks across every stage, so what it finds is on screen. */
  const search = useCallback(
    (term: string) => {
      setQuery(term);
      if (term.trim()) setFilter("");
      void load(term.trim());
    },
    [load],
  );

  const offline = !online || lost;
  const writable = !offline && !busy;
  const garment = bill?.garments.find((g) => g.line_no === draft.line_no) ?? null;
  const rows = listing?.jobs ?? [];
  const open = rows.find((j) => j.ref === openRef) ?? null;
  const showing = rows.filter((j) => inView(j, filter));
  const offlineNote = offline && (
    <Note
      tone="warn"
      role="status"
      testId="alterations-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="alterations-retry"
            onClick={() => void load(query)}
          >
            Try again
          </button>
        )
      }
    >
      {ALTERATIONS_OFFLINE}
    </Note>
  );

  return (
    <div className="page-pad">
      <PageHeader
        title="Alterations"
        lead="A job card for each alteration, from the garment's bill. The store holds the garment for the customer until it is handed over."
        actions={
          listing?.switched_on && (
            <button
              type="button"
              className="btn btn-primary"
              aria-expanded={adding}
              data-testid="alteration-new"
              onClick={() => setAdding(!adding)}
            >
              <Plus size={16} aria-hidden="true" /> New job card
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
          <Note tone="error" testId="alterations-error">
            {error}
          </Note>
        )}
        {done && (
          <Note tone="success" role="status" testId="alterations-done">
            {done}
          </Note>
        )}
        {!store && <EmptyState>Choose a store to see its job cards.</EmptyState>}
        {store && !listing && !offline && !error && <Loading label="Loading job cards" />}

        {listing && !listing.switched_on && (
          <Note tone="warn" testId="alterations-switched-off">
            Alterations are switched off at {listing.store}. No new job card can be made; the ones
            below can still be moved on and handed over.
          </Note>
        )}

        {listing && listing.switched_on && adding && (
          <Section heading="New job card" testId="alteration-form">
            <p className="muted-cell">
              A paid alteration is charged at Billing with "Alteration charge" (its own line, GST
              5%, SAC 9988). A free alteration has no charge.
            </p>
            <div className="order-step-row">
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
                          qty: Math.min(Math.max(Number(e.target.value) || 1, 1), garment.free_qty),
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
                <div className="order-actions">
                  <button
                    type="button"
                    className="btn btn-primary"
                    data-testid="alteration-save"
                    disabled={!writable}
                    onClick={() => void save()}
                  >
                    Make job card
                  </button>
                </div>
              </>
            )}
          </Section>
        )}

        {listing && (
          <Section heading={`Job cards at ${listing.store}`} testId="alteration-list">
            <div className="orders-bar">
              <FilterChips label="Stage">
                {VIEWS.map((v) => (
                  <FilterChip
                    key={v.key}
                    pressed={filter === v.key}
                    count={rows.filter((j) => inView(j, v.key)).length}
                    onClick={() => setFilter(filter === v.key ? "" : v.key)}
                    testId={`alteration-show-${v.key}`}
                  >
                    {v.label}
                  </FilterChip>
                ))}
              </FilterChips>
              <SearchBox
                label="Find a job card"
                placeholder="Job card number, bill number or mobile"
                testId="alteration-search"
                value={query}
                onChange={search}
              />
            </div>
            <SplitView
              pane={
                open && (
                  <JobPane
                    key={open.id}
                    job={open}
                    writable={writable}
                    cancelling={cancelling === open.id}
                    reason={reason}
                    onReason={setReason}
                    onStep={(s) => void step(open, s)}
                    onCancelStart={() => {
                      setCancelling(open.id);
                      setReason("");
                    }}
                    onCancel={() => void cancel(open)}
                  />
                )
              }
            >
              {rows.length === 0 ? (
                <EmptyState testId="alteration-none">No job cards to show.</EmptyState>
              ) : showing.length === 0 ? (
                <EmptyState>Nothing at this stage.</EmptyState>
              ) : (
                <div className="table-wrap capped">
                  <table className="data" data-testid="alteration-cards">
                    <thead>
                      <tr>
                        <th scope="col">Job card</th>
                        <th scope="col">Customer</th>
                        <th scope="col">Garment</th>
                        <th scope="col">Tailor</th>
                        <th scope="col">Promised</th>
                        <th scope="col" className="p2">
                          Charge
                        </th>
                        <th scope="col" className="p3">
                          Garment is
                        </th>
                        <th scope="col">Stage</th>
                      </tr>
                    </thead>
                    <tbody>
                      {showing.map((job) => {
                        const chip = statusChip(job.status);
                        const selected = job.id === open?.id;
                        const due = dueText(job);
                        return (
                          <tr
                            key={job.id}
                            className={selected ? "is-selected" : undefined}
                            data-testid={`alteration-${job.ref}`}
                          >
                            <td>
                              <Link
                                className="order-ref"
                                to={{ search: `?open=${encodeURIComponent(job.ref)}` }}
                                aria-current={selected ? "true" : undefined}
                              >
                                {job.ref}
                              </Link>
                            </td>
                            <td>
                              <span className="order-who">{job.customer_name}</span>
                              <span className="sub">{job.customer_mobile}</span>
                            </td>
                            <td>
                              {garmentLabel(job.garment)}
                              {job.qty > 1 ? ` × ${job.qty}` : ""}
                              <span className="sub">Bill {job.bill}</span>
                            </td>
                            <td>
                              {job.tailor === "outside" ? "Outside" : "In-house"}
                              {job.tailor_name && <span className="sub">{job.tailor_name}</span>}
                            </td>
                            <td>
                              {dayText(job.promised_on)}
                              {due && <span className="sub">{due}</span>}
                            </td>
                            <td className="p2">{chargeText(job)}</td>
                            <td className="p3">{custodyText(job)}</td>
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

/** The list's view filters: everything still with the store, each stage,
 *  and the two ways a job card ends. */
const VIEWS = [
  { key: "open", label: "Open" },
  { key: "received", label: "Received" },
  { key: "with_tailor", label: "With the tailor" },
  { key: "ready", label: "Ready" },
  { key: "handed_over", label: "Handed over" },
  { key: "cancelled", label: "Cancelled" },
] as const;

/** The stages a job card moves through, for its stepper. */
const STAGES = ["received", "with_tailor", "ready", "handed_over"] as const;

function inView(job: AlterationJob, view: string): boolean {
  if (!view) return true;
  if (view === "open") return isOpen(job.status);
  return job.status === view;
}

/** The library's chip tones as Status dots: navy (closed) reads as grey. */
function toneOf(tone: string): StatusTone {
  return tone === "navy" ? "grey" : (tone as StatusTone);
}

/** How long is left before the promised day, for a job card still open. */
function dueText(job: AlterationJob): string {
  if (!isOpen(job.status)) return "";
  if (job.days_left < 0) return job.days_left === -1 ? "1 day late" : `${-job.days_left} days late`;
  if (job.days_left === 0) return "Today";
  return job.days_left === 1 ? "1 day left" : `${job.days_left} days left`;
}

function custodyText(job: AlterationJob): string {
  return job.custody.closed_at
    ? `Custody ended ${momentText(job.custody.closed_at)}`
    : `Held for the customer: ${CUSTODY_WORDS[job.custody.location] ?? job.custody.location}`;
}

/** One job card, open beside the list, with the steps it can take next. */
function JobPane({
  job,
  writable,
  cancelling,
  reason,
  onReason,
  onStep,
  onCancelStart,
  onCancel,
}: {
  job: AlterationJob;
  writable: boolean;
  cancelling: boolean;
  reason: string;
  onReason: (reason: string) => void;
  onStep: (step: Step) => void;
  onCancelStart: () => void;
  onCancel: () => void;
}) {
  const chip = statusChip(job.status);
  const reached = STAGES.indexOf(job.status as (typeof STAGES)[number]);
  return (
    <Pane label={`Job card ${job.ref}`} testId="alteration-pane">
      <div className="order-pane-head">
        <div>
          <div className="order-pane-title">
            <h2 className="heading">{job.ref}</h2>
            <Status tone={toneOf(chip.tone)}>{chip.label}</Status>
          </div>
          <p className="order-sub">
            {job.customer_name}, {job.customer_mobile}
          </p>
        </div>
        <Link
          className="btn btn-sm order-close"
          to={{ search: "" }}
          aria-label="Close the job card"
        >
          <X size={16} aria-hidden="true" />
        </Link>
      </div>
      <div>
        <div className="order-who">
          {garmentLabel(job.garment)} × {job.qty}
        </div>
        <p className="order-sub">
          <span className="mono">{job.garment.barcode}</span>, bill{" "}
          <span className="mono">{job.bill}</span>, line {job.line_no}
        </p>
      </div>
      <dl className="dl">
        <dt>What to alter</dt>
        <dd data-testid={`alteration-work-${job.ref}`}>{job.work}</dd>
        {job.measurements && (
          <>
            <dt>Measurements</dt>
            <dd>{job.measurements}</dd>
          </>
        )}
        <dt>Tailor</dt>
        <dd>
          {job.tailor === "outside" ? "Outside tailor" : "In-house tailor"}
          {job.tailor_name ? `: ${job.tailor_name}` : ""}
        </dd>
        <dt>Promised</dt>
        <dd>{promisedText(job).replace(/^Promised /, "")}</dd>
        <dt>Charge</dt>
        <dd data-testid={`alteration-paid-${job.ref}`}>{chargeText(job)}</dd>
        <dt>Garment</dt>
        <dd data-testid={`alteration-custody-${job.ref}`}>{custodyText(job)}</dd>
        {job.customer_told_at && (
          <>
            <dt>Customer told</dt>
            <dd data-testid={`alteration-told-at-${job.ref}`}>
              Customer told {momentText(job.customer_told_at)}
            </dd>
          </>
        )}
        {job.cancel_reason && (
          <>
            <dt>Cancelled</dt>
            <dd>{job.cancel_reason}</dd>
          </>
        )}
      </dl>
      {job.status !== "cancelled" && (
        <ol className="order-steps" aria-label="Stage">
          {STAGES.map((s, i) => {
            const state =
              i < reached || job.status === "handed_over"
                ? "is-done"
                : i === reached
                  ? "is-current"
                  : "";
            return (
              <li
                key={s}
                className={state || undefined}
                aria-current={state === "is-current" ? "step" : undefined}
              >
                <b aria-hidden="true">{state === "is-done" ? <Check size={11} /> : i + 1}</b>
                {statusChip(s).label}
                {state === "is-done" && <span className="sr-only">, done</span>}
              </li>
            );
          })}
        </ol>
      )}
      {isOpen(job.status) && (
        <>
          <div className="order-actions">
            {nextSteps(job).map((s) => (
              <button
                key={s}
                type="button"
                className={s === "hand-over" ? "btn btn-primary" : "btn"}
                data-testid={`alteration-${s}-${job.ref}`}
                disabled={!writable}
                onClick={() => onStep(s)}
              >
                {STEP_WORDS[s]}
              </button>
            ))}
          </div>
          <div className="order-cancel">
            {cancelling ? (
              <>
                <label className="field">
                  <span>Why it is cancelled</span>
                  <input
                    className="input"
                    placeholder="For example: customer took it unaltered"
                    data-testid={`alteration-cancel-reason-${job.ref}`}
                    value={reason}
                    onChange={(e) => onReason(e.target.value)}
                  />
                </label>
                <div className="order-actions">
                  <button
                    type="button"
                    className="btn btn-danger"
                    data-testid={`alteration-cancel-confirm-${job.ref}`}
                    disabled={!writable || !reason.trim()}
                    onClick={onCancel}
                  >
                    Cancel job card
                  </button>
                </div>
              </>
            ) : (
              <div className="order-actions">
                <button
                  type="button"
                  className="btn btn-danger"
                  data-testid={`alteration-cancel-${job.ref}`}
                  disabled={!writable}
                  onClick={onCancelStart}
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </Pane>
  );
}
