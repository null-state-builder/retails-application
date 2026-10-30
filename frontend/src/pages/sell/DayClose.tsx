import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Lock, RefreshCw } from "lucide-react";

import { Note } from "../../components/Note";
import { OperationsPage } from "../../components/OperationsPage";
import { PageHeader } from "../../components/PageHeader";
import { Section } from "../../components/Section";
import { ErrorState, Loading } from "../../components/States";
import { api, apiErrorCode, apiErrorMessage } from "../../lib/api";
import {
  canClose,
  dayWords,
  deriveSteps,
  openHolds,
  whyNotYet,
  type DayCloseState,
  type Step,
} from "../../lib/dayClose";
import { Money } from "../../lib/format";
import { verifyPin } from "../../till/pin";
import { useTill } from "../../till/TillProvider";
import type { TillManager } from "../../till/types";
import { newUuid } from "../../till/uuid";
import { WRONG_PINS_BEFORE_A_PAUSE, useWrongPins } from "./ManagerPin";
import "./DayClose.css";

// ---------------------------------------------------------------------------
// Day close - work down five steps, then a manager closes the business day
// ---------------------------------------------------------------------------
//
// The server says what it holds (`GET /api/sell/day-close`); this counter adds
// its own queue and parked carts (`lib/dayClose`). Close the day stays off while
// any step is open, and the open step says what to do. The manager's PIN is
// checked here against this counter's copy of the hashes, as on a bill, and
// again by the server; it is never stored. Nothing prints: the closed day stays
// readable under Reports.

/** `GET /api/store/cash-summary` - the Day summary's own read, reused here. */
interface CashSummary {
  store: string;
  date: string;
  modes: Record<string, number>;
  bills: number;
  returns: number;
  credit_notes_issued_paise: number;
}

const MODE_WORDS: Record<string, string> = {
  cash: "Cash",
  upi: "UPI",
  card: "Card",
  credit_note: "Credit note",
  bank_offer: "Bank offer",
};

/** A refusal with no answer at all is the connection, not the server. */
function droppedConnection(e: unknown): boolean {
  return !(e as { response?: unknown })?.response;
}

export default function DayClosePage() {
  const { engine, till } = useTill();
  const [state, setState] = useState<DayCloseState | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);
  const online = till?.online ?? navigator.onLine;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await api.get<DayCloseState>("/sell/day-close");
      setState(response.data);
      setLoadError("");
    } catch (e) {
      setLoadError(
        droppedConnection(e)
          ? "The day cannot be read while the connection is down. It reads again when you are back online."
          : apiErrorMessage(e),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  // Read again whenever the counter's own queue or hold list moves.
  const pending = till?.pending ?? 0;
  const heldCount = till?.held.length ?? 0;
  useEffect(() => {
    if (online) void load();
  }, [load, online, pending, heldCount]);

  const counterId = till?.device?.identity.counter_id ?? "";
  const steps = useMemo(
    () =>
      state
        ? deriveSteps(state, {
            ready: Boolean(till?.ready),
            counterId,
            pending,
            halt: till?.halt ?? null,
            onlinePending: Boolean(till?.onlinePending),
            heldOpen: openHolds(till?.held ?? []),
          })
        : [],
    [state, till, counterId, pending],
  );

  return (
    <OperationsPage>
      <PageHeader
        lead={
          state ? (
            <>
              Work down the list to close {dayWords(state.business_day)} at {state.store}. Nothing
              prints; the day stays readable under Reports.
            </>
          ) : (
            "Work down the list to close the day. Nothing prints; the day stays readable under Reports."
          )
        }
        actions={
          <button
            type="button"
            className="btn"
            data-testid="day-close-reload"
            disabled={loading || !online}
            onClick={() => void load()}
          >
            <RefreshCw size={15} aria-hidden="true" />
            Read again
          </button>
        }
      />

      <div className="dayclose-body">
        {!online && (
          <Note tone="warn" testId="day-close-offline">
            You are offline. The day closes at head office, so the steps read again when the
            connection is back.
          </Note>
        )}
        {loadError && !state && (
          <ErrorState
            message={loadError}
            onRetry={() => void load()}
            busy={loading}
            testId="day-close-load-error"
          />
        )}
        {loadError && state && (
          <Note tone="error" testId="day-close-load-error">
            {loadError}
          </Note>
        )}
        {!state && !loadError && <Loading lines={5} label="Reading the day" />}

        {state && (
          <>
            <ol
              className="dayclose-stepper"
              aria-label="Day close progress"
              data-testid="day-close-stepper"
            >
              {steps.map((step, i) => (
                <li
                  key={step.key}
                  className={`dayclose-stepper-${step.status}`}
                  aria-current={step.status === "now" ? "step" : undefined}
                >
                  <b>{i + 1}</b>
                  {step.title}
                </li>
              ))}
            </ol>

            <div className="dayclose-layout">
              <Section heading="Checklist" testId="day-close-checklist">
                <ol className="dayclose-steps">
                  {steps.map((step, i) => (
                    <StepRow
                      key={step.key}
                      step={step}
                      number={i + 1}
                      state={state}
                      online={online}
                      onChanged={setState}
                      steps={steps}
                      pending={pending}
                      counterId={counterId}
                      managersFrom={engine}
                    />
                  ))}
                </ol>
              </Section>
              <aside className="dayclose-aside" aria-label="Today's totals and open items">
                <Totals state={state} online={online} />
                <StillOpen state={state} />
              </aside>
            </div>
          </>
        )}
      </div>
    </OperationsPage>
  );
}

function StepRow({
  step,
  number,
  state,
  online,
  onChanged,
  steps,
  pending,
  counterId,
  managersFrom,
}: {
  step: Step;
  number: number;
  state: DayCloseState;
  online: boolean;
  onChanged: (next: DayCloseState) => void;
  steps: Step[];
  pending: number;
  counterId: string;
  managersFrom: ReturnType<typeof useTill>["engine"];
}) {
  return (
    <li
      className={`dayclose-step dayclose-step-${step.status}`}
      data-testid={`day-close-step-${step.key}`}
      data-state={step.status}
    >
      <span className="dayclose-badge" aria-label={step.status === "done" ? "Done" : undefined}>
        {step.status === "done" ? <Check size={14} aria-hidden="true" /> : number}
      </span>
      <div className="dayclose-step-body">
        <h3 className="dayclose-step-title">{step.title}</h3>
        <p className="dayclose-step-text" data-testid={`day-close-step-${step.key}-text`}>
          {step.text}
        </p>
        {step.key === "summary" && step.status !== "done" && !state.closed && (
          <SummaryTick state={state} online={online} onChanged={onChanged} />
        )}
        {step.key === "close" && !state.closed && (
          <CloseForm
            state={state}
            steps={steps}
            online={online}
            pending={pending}
            counterId={counterId}
            engine={managersFrom}
            onChanged={onChanged}
          />
        )}
      </div>
      {step.link ? (
        <Link className="btn btn-sm" to={step.link.to} data-testid={`day-close-link-${step.key}`}>
          {step.link.label}
        </Link>
      ) : (
        <span />
      )}
    </li>
  );
}

function SummaryTick({
  state,
  online,
  onChanged,
}: {
  state: DayCloseState;
  online: boolean;
  onChanged: (next: DayCloseState) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const tickId = useRef(newUuid());
  // A tick waits for the step's own data: nothing to check while bills are moving.
  async function tick() {
    setBusy(true);
    setMessage("");
    try {
      const response = await api.post<DayCloseState>("/sell/day-close/summary-check", {
        id: tickId.current,
        business_day: state.business_day,
      });
      tickId.current = newUuid();
      onChanged(response.data);
    } catch (e) {
      if (!droppedConnection(e)) tickId.current = newUuid();
      setMessage(
        droppedConnection(e)
          ? "Not saved: the connection dropped. Press it again when you are back online."
          : apiErrorMessage(e),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="dayclose-inline">
      <button
        type="button"
        className="btn btn-primary btn-sm"
        data-testid="day-close-summary-tick"
        disabled={busy || !online}
        onClick={() => void tick()}
      >
        <Check size={14} aria-hidden="true" />
        {busy ? "Saving…" : "I have read the Day summary"}
      </button>
      {message && (
        <Note tone="error" role="status" testId="day-close-summary-message">
          {message}
        </Note>
      )}
    </div>
  );
}

function CloseForm({
  state,
  steps,
  online,
  pending,
  counterId,
  engine,
  onChanged,
}: {
  state: DayCloseState;
  steps: Step[];
  online: boolean;
  pending: number;
  counterId: string;
  engine: ReturnType<typeof useTill>["engine"];
  onChanged: (next: DayCloseState) => void;
}) {
  const [managers, setManagers] = useState<TillManager[]>([]);
  const [managerId, setManagerId] = useState<number | null>(null);
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const closeId = useRef(newUuid());
  const pins = useWrongPins();
  const waiting = pins.wrong >= WRONG_PINS_BEFORE_A_PAUSE;

  // The managers this counter can check a PIN for, from its own database - the
  // same list a bill's approval and the cash count use.
  useEffect(() => {
    if (!engine) return;
    void engine.db.managers.toArray().then((rows) => {
      setManagers(rows);
      setManagerId((current) => current ?? rows[0]?.user_id ?? null);
    });
  }, [engine]);

  const ready = canClose(steps, state, online);
  const reason = whyNotYet(steps, state, online);
  const manager = managers.find((row) => row.user_id === managerId) ?? null;

  async function submit() {
    if (!ready || !manager || !pin || busy || waiting) return;
    setBusy(true);
    setMessage("");
    try {
      if (!(await verifyPin(manager, pin))) {
        pins.wasWrong();
        setPin("");
        setMessage("That is not a manager's PIN for this store.");
        return;
      }
      const response = await api.post<DayCloseState>("/sell/day-close/close", {
        id: closeId.current,
        business_day: state.business_day,
        approved_by: manager.user_id,
        // Checked again by the server, because this is online; never kept.
        manager_pin: pin,
        counter_pending: pending,
        till_number: counterId,
      });
      pins.clear();
      setPin("");
      closeId.current = newUuid();
      onChanged(response.data);
    } catch (e) {
      if (droppedConnection(e)) {
        // Kept: the same id goes again, so a close that did land is not done twice.
        setMessage(
          "Not closed: the connection dropped. Press Close the day again when you are back online.",
        );
      } else {
        closeId.current = newUuid();
        if (apiErrorCode(e) === "WRONG_PIN") pins.wasWrong();
        setPin("");
        setMessage(apiErrorMessage(e));
      }
    } finally {
      setBusy(false);
    }
  }

  if (managers.length === 0) {
    return (
      <Note tone="warn" testId="day-close-no-manager">
        No manager of this store has a counter PIN on this counter yet. One is set from Till &amp;
        Sync, by the manager themselves.
      </Note>
    );
  }

  return (
    <div className="dayclose-close">
      <div className="dayclose-close-fields">
        <div className="field">
          <label htmlFor="day-close-manager">Manager</label>
          <select
            id="day-close-manager"
            className="input"
            data-testid="day-close-manager"
            value={managerId ?? ""}
            disabled={!ready || busy}
            onChange={(e) => setManagerId(Number(e.target.value))}
          >
            {managers.map((row) => (
              <option key={row.user_id} value={row.user_id}>
                {row.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="day-close-pin">Manager PIN</label>
          <input
            id="day-close-pin"
            className="input"
            data-testid="day-close-pin"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            placeholder="4–6 digits"
            value={pin}
            disabled={!ready || busy || waiting}
            onChange={(e) => setPin(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              void submit();
            }}
          />
        </div>
        <button
          type="button"
          className="btn btn-cta"
          data-testid="day-close-submit"
          disabled={!ready || !pin || busy || waiting}
          onClick={() => void submit()}
        >
          <Lock size={15} aria-hidden="true" />
          {busy ? "Closing…" : "Close the day"}
        </button>
      </div>
      {!ready && reason && (
        <p className="dayclose-why" data-testid="day-close-why">
          Close the day stays off: {reason}
        </p>
      )}
      {waiting && (
        <Note tone="warn" testId="day-close-pin-waiting">
          Too many wrong PINs. This waits half a minute before it will take another.
        </Note>
      )}
      {message && (
        <Note tone="error" role="status" testId="day-close-message">
          {message}
        </Note>
      )}
    </div>
  );
}

function Totals({ state, online }: { state: DayCloseState; online: boolean }) {
  const [summary, setSummary] = useState<CashSummary | null>(null);
  const [error, setError] = useState("");
  const day = state.business_day;
  const store = state.store;
  const frontier = state.summary.frontier;

  useEffect(() => {
    if (!online) return;
    let live = true;
    api
      .get<CashSummary>("/store/cash-summary", { params: { date: day, store } })
      .then((response) => {
        if (!live) return;
        setSummary(response.data);
        setError("");
      })
      .catch((e: unknown) => {
        if (live) setError(apiErrorMessage(e));
      });
    return () => {
      live = false;
    };
  }, [day, store, online, frontier]);

  const facts = state.record?.facts as { bills?: number; net_paise?: number } | undefined;
  const modes = summary
    ? Object.entries(summary.modes).filter(([mode, paise]) => paise !== 0 || mode in MODE_WORDS)
    : [];
  const net = modes.reduce((sum, [, paise]) => sum + paise, 0);

  return (
    <Section heading="Today's totals" level={3} testId="day-close-totals">
      <p className="dayclose-sub">
        {dayWords(day)}, {store}
        {summary ? `, ${summary.bills} ${summary.bills === 1 ? "bill" : "bills"}` : ""}
      </p>
      {error && (
        <Note tone="warn" testId="day-close-totals-error">
          The Day summary&apos;s totals cannot be read here: {error}
        </Note>
      )}
      {!summary && !error && <Loading lines={4} label="Reading today's totals" />}
      {summary && (
        <table className="data" data-testid="day-close-totals-table">
          <thead>
            <tr>
              <th>Paid with</th>
              <th className="num">Amount</th>
            </tr>
          </thead>
          <tbody>
            {modes.map(([mode, paise]) => (
              <tr key={mode}>
                <td>{MODE_WORDS[mode] ?? mode}</td>
                <td className="num">
                  <Money paise={paise} />
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>
                Net sales, {summary.bills} {summary.bills === 1 ? "bill" : "bills"}
              </td>
              <td className="num">
                <Money paise={net} />
              </td>
            </tr>
          </tfoot>
        </table>
      )}
      {summary && (
        <p className="dayclose-sub" data-testid="day-close-returns">
          Returns: {summary.returns} {summary.returns === 1 ? "piece" : "pieces"}. Credit notes
          issued: <Money paise={summary.credit_notes_issued_paise} />.
        </p>
      )}
      {state.carried_in.bills > 0 && (
        <p className="dayclose-sub" data-testid="day-close-carried-in">
          Also in this day: {state.carried_in.bills}{" "}
          {state.carried_in.bills === 1 ? "bill" : "bills"} billed after the previous day closed,{" "}
          <Money paise={state.carried_in.net_paise} />.
        </p>
      )}
      {state.closed && facts?.bills !== undefined && (
        <p className="dayclose-sub" data-testid="day-close-final">
          Final at the close: {facts.bills} {facts.bills === 1 ? "bill" : "bills"},{" "}
          <Money paise={facts.net_paise ?? 0} />.
          {state.after_close.bills > 0 &&
            ` ${state.after_close.bills} billed after the close ${state.after_close.bills === 1 ? "belongs" : "belong"} to ${dayWords(state.next_business_day)}.`}
        </p>
      )}
    </Section>
  );
}

function StillOpen({ state }: { state: DayCloseState }) {
  const open = state.still_open;
  const rows = [
    {
      key: "reservations",
      count: open.reservations_holding,
      text: `${open.reservations_holding} ${open.reservations_holding === 1 ? "reservation" : "reservations"} holding${open.reservations_past_date ? `, ${open.reservations_past_date} past ${open.reservations_past_date === 1 ? "its" : "their"} date` : ""}`,
      to: "/orders/reservations",
      label: "Reservations",
    },
    {
      key: "alterations",
      count: open.alterations_ready,
      text: `${open.alterations_ready} ${open.alterations_ready === 1 ? "alteration" : "alterations"} ready, not handed over`,
      to: "/orders/alterations",
      label: "Alterations",
    },
    {
      key: "special-orders",
      count: open.special_orders_arrived,
      text: `${open.special_orders_arrived} special ${open.special_orders_arrived === 1 ? "order" : "orders"} arrived, customer not told`,
      to: "/orders/special",
      label: "Special orders",
    },
  ].filter((row) => row.count > 0);
  return (
    <Section heading="Still open" level={3} testId="day-close-still-open">
      <p className="dayclose-sub">These carry over to tomorrow; they do not stop the close.</p>
      {rows.length === 0 ? (
        <p className="dayclose-sub" data-testid="day-close-still-open-none">
          Nothing carries over.
        </p>
      ) : (
        <ul className="dayclose-open">
          {rows.map((row) => (
            <li key={row.key} data-testid={`day-close-open-${row.key}`}>
              <span>{row.text}</span>
              <Link to={row.to}>{row.label}</Link>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
