import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Check,
  HardDriveDownload,
  RefreshCw,
  Replace,
  Volume2,
  VolumeX,
} from "lucide-react";

import { PageHeader } from "../../components/PageHeader";
import { useAuth } from "../../auth/AuthContext";
import { api, apiErrorMessage } from "../../lib/api";
import { Money, formatDateTime } from "../../lib/format";
import { userCan } from "../../shell/navConfig";
import { AUTHORITY_EXPIRED, describeAuthority } from "../../till/authority";
import { numberingNote } from "../../till/invoiceNumbers";
import { SyncLight } from "../../till/SyncLight";
import { useTill } from "../../till/TillProvider";
import type { TillEngine, TillSnapshot } from "../../till/engine";
import { slabFor, splitLine, taxVersionFor, tillToday } from "../../till/pricing";
import { playTone } from "../../till/sounds";
import type { BillDraft, HandoverState, TillTaxSettings } from "../../till/types";
import "./Till.css";

// ---------------------------------------------------------------------------
// Till & Sync - what the counter holds, and what it still owes head office (#180)
// ---------------------------------------------------------------------------
//
// The till spine's own surface. The billing screen (#181) sits on top of this
// layer and shows the same sync light in its header; this page is where a store
// person, or whoever they ring, can see the whole of it.
//
// Laid out for the cashier's glance first (desktop redesign, grilled 29 Oct
// 2026). One status band answers "can I bill, and has everything gone?" - the
// next bill number, how far head office has got, and every problem as a row
// with its own fix, worst first. Below it sit the work lists (paper re-entry,
// the queue), the counter tasks a manager opens, and this person's own
// settings. The right-hand rail is the facts somebody reads down the phone.
//
// Nothing here is money the person can change. There is no edit affordance on a
// queued bill by construction: it is printed, it is paid for, and the only two
// honest outcomes are that the server takes it or that somebody is told about it.
//
// Two things here move the number the next customer's bill will carry, so both
// are buttons somebody presses and neither happens on its own:
//
//   · **Recover this counter** (#189) - the browser threw the local database
//     away, and this takes the whole dataset again and asks head office how far
//     the store's series has got.
//   · **Move the counter to this machine** - a manager's act, with a reason. It
//     registers this device as the replacement, which records the handover on
//     the server, and lists the bills the old machine never sent so the store
//     can key them in from the printed copies.

export default function TillPage() {
  const { engine, till, accessError } = useTill();

  if (!engine || !till) return <NoCounter reason={accessError} />;

  return (
    <div className="page-pad">
      <PageHeader
        actions={
          <div className="till-actions">
            <SyncLight />
            <button
              type="button"
              className="btn"
              data-testid="till-sync-now"
              disabled={till.busy}
              onClick={() => void engine.syncNow()}
            >
              <RefreshCw size={15} className={till.busy ? "till-spin" : ""} />
              {till.busy ? "Syncing…" : "Sync now"}
            </button>
          </div>
        }
      />

      <div className="till-layout">
        <div className="till-main">
          <StatusBand engine={engine} till={till} />
          {till.handover && <PaperReentry engine={engine} till={till} handover={till.handover} />}
          {till.queue.length > 0 && <QueueTable till={till} />}
          <CounterTasks engine={engine} till={till} />
          <PersonalSettings engine={engine} muted={till.muted} />
          <TestBill />
        </div>
        <FactsRail engine={engine} till={till} />
      </div>
    </div>
  );
}

// -- small shared pieces -------------------------------------------------------

/** A button's outcome: the refusal in words, or what changed. One per action, so
 *  two cards never share a sentence. */
function useOutcome() {
  const [failed, setFailed] = useState("");
  const [said, setSaid] = useState("");
  async function run(work: () => Promise<unknown>, done = ""): Promise<boolean> {
    setFailed("");
    setSaid("");
    try {
      await work();
      setSaid(done);
      return true;
    } catch (error) {
      setFailed(messageOf(error));
      return false;
    }
  }
  return { failed, said, run, setSaid };
}

function Outcome({ failed, said, id }: { failed: string; said: string; id: string }) {
  return (
    <>
      {failed && (
        <p className="till-alert" data-testid={`${id}-failed`}>
          <AlertTriangle size={15} />
          {failed}
        </p>
      )}
      {said && (
        <p className="ok-note till-said" data-testid={`${id}-said`}>
          <Check size={15} />
          {said}
        </p>
      )}
    </>
  );
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// -- the status band -----------------------------------------------------------

type Tone = "green" | "amber" | "red";

/**
 * The one place the page says how the counter is.
 *
 * The ledger is the signature: the next bill number, large, and the short track
 * behind it - how far head office has accepted, and what is still waiting here.
 * That gap is what store people ring about, so it is drawn rather than left for
 * somebody to subtract.
 *
 * Problems are rows inside the band, worst first, each with the one button that
 * fixes it. Only the first fix is the page's red action: a healthy counter has
 * no red button at all.
 */
function StatusBand({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const { user } = useAuth();
  const mayRegister = userCan(user, "sell", "approve");
  const registered = Boolean(till.device?.identity?.registered);

  const problems: { key: string; tone: Tone; node: (cta: boolean) => ReactNode }[] = [];
  if (till.storageLost) {
    problems.push({
      key: "lost",
      tone: "red",
      node: (cta) => <RecoverRow engine={engine} busy={till.busy} cta={cta} />,
    });
  }
  if (till.authority.expired) {
    problems.push({
      key: "expired",
      tone: "red",
      node: (cta) => <ExpiredRow engine={engine} till={till} cta={cta} />,
    });
  }
  if (till.halt) {
    const halt = till.halt;
    problems.push({
      key: "halt",
      tone: "red",
      node: (cta) => (
        <ProblemRow
          testId="till-halt"
          title={`Bill ${halt.doc_number} was not accepted`}
          action={
            <button
              type="button"
              className={cta ? "btn btn-cta" : "btn"}
              data-testid="till-halt-retry"
              onClick={() => void engine.retryHalted()}
            >
              Try this bill again
            </button>
          }
        >
          <p className="till-halt-why">{halt.message}</p>
          <p className="till-problem-text">
            The bill is kept. Later bills wait behind it; new bills carry on. Refused at{" "}
            {formatDateTime(halt.at)} ({halt.code}).
          </p>
        </ProblemRow>
      ),
    });
  }
  if (till.pause) {
    problems.push({
      key: "pause",
      tone: "amber",
      node: (cta) => <PausedRow engine={engine} till={till} cta={cta} />,
    });
  }
  if (!registered) {
    problems.push({
      key: "unregistered",
      tone: "amber",
      node: (cta) => <UnregisteredRow engine={engine} till={till} cta={cta} may={mayRegister} />,
    });
  }
  if (till.legacyStorageQuarantined) {
    problems.push({
      key: "legacy",
      tone: "amber",
      node: () => (
        <ProblemRow title="Earlier counter data needs review" role="status">
          <p className="till-problem-text">
            This browser holds bills and stock from an earlier store cache. They are kept and not
            uploaded. Ask Owner or Admin to reconcile them before recovering any bills.
          </p>
        </ProblemRow>
      ),
    });
  }

  const tone: Tone = problems.some((p) => p.tone === "red")
    ? "red"
    : till.status.colour === "red"
      ? "red"
      : problems.length || till.status.colour === "amber"
        ? "amber"
        : "green";

  return (
    <section className={`till-band till-band-${tone}`} data-testid="till-band" data-tone={tone}>
      <div className="till-band-head">
        <Ledger till={till} />
        {problems.length === 0 && (
          <div className="till-band-say">
            <p className="till-band-sentence" data-testid="till-status-reason">
              {headline(till)}
            </p>
            <p className="till-band-meta">
              {till.syncedAt ? `Last synced ${formatDateTime(till.syncedAt)}.` : "Not synced yet."}
            </p>
          </div>
        )}
      </div>
      {problems.length > 0 && (
        <div className="till-problems">
          {problems.map((p, i) => (
            <div key={p.key} className={`till-problem till-problem-${p.tone}`}>
              {p.node(i === 0)}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/** The band's sentence on a counter with nothing to fix. */
function headline(till: TillSnapshot): string {
  const base = till.status.colour === "green" ? "All bills sent." : till.status.reason;
  const { authority } = till;
  if (authority.known && !authority.expired && authority.until) {
    return `${base} Offline billing open until ${formatDateTime(authority.until)}.`;
  }
  return base;
}

/** The next bill number and the track behind it. */
function Ledger({ till }: { till: TillSnapshot }) {
  const accepted = till.register?.last_accepted_seq;
  const waiting = till.pending;
  return (
    <div className="till-ledger" data-testid="till-summary">
      <div className="till-ledger-next">
        <span className="till-ledger-label">Next bill</span>
        <span className="till-ledger-number" data-testid="till-next-number">
          {till.nextNumber || "—"}
        </span>
      </div>
      <ol className="till-track" aria-label="Where the bills are">
        <li className="till-track-stop">
          <span className="till-track-value">{accepted ?? "—"}</span>
          <span className="till-track-label">
            {accepted === undefined ? "Head office not checked" : "Accepted by head office"}
          </span>
        </li>
        <li className={`till-track-stop${waiting ? " till-track-waiting" : ""}`}>
          <span className="till-track-value">{waiting}</span>
          <span className="till-track-label">Waiting on this counter</span>
        </li>
      </ol>
    </div>
  );
}

function ProblemRow({
  title,
  action,
  children,
  testId,
  role,
  stage,
}: {
  title: string;
  action?: ReactNode;
  children?: ReactNode;
  testId?: string;
  role?: string;
  stage?: string;
}) {
  return (
    <div className="till-problem-row" data-testid={testId} role={role} data-stage={stage}>
      <div className="till-problem-body">
        <h2 className="till-problem-title">
          <AlertTriangle size={15} /> {title}
        </h2>
        {children}
      </div>
      {action && <div className="till-problem-action">{action}</div>}
    </div>
  );
}

/**
 * Putting a counter back together after the browser cleared its data (#189).
 *
 * Shown only when the till has actually noticed a loss, and it is the only way
 * out of that state: until this succeeds the Billing screen will not take a
 * bill, because a counter that does not know its own number would print one that
 * is already on a posted bill.
 */
function RecoverRow({ engine, busy, cta }: { engine: TillEngine; busy: boolean; cta: boolean }) {
  const outcome = useOutcome();
  return (
    <>
      <ProblemRow
        testId="till-recover"
        title="This counter lost its local data"
        action={
          <button
            type="button"
            className={cta ? "btn btn-cta" : "btn"}
            data-testid="till-recover-go"
            disabled={busy}
            onClick={() => void outcome.run(() => engine.recover())}
          >
            <HardDriveDownload size={15} />
            {busy ? "Recovering…" : "Recover this counter"}
          </button>
        }
      >
        <p className="till-problem-text">
          Synced bills are safe. Recover to fetch the price list and the latest bill number before
          billing.
        </p>
      </ProblemRow>
      <Outcome failed={outcome.failed} said="" id="till-recover" />
    </>
  );
}

/** The window closed: nothing recorded is lost, and the queue still drains. */
function ExpiredRow({
  engine,
  till,
  cta,
}: {
  engine: TillEngine;
  till: TillSnapshot;
  cta: boolean;
}) {
  const outcome = useOutcome();
  return (
    <>
      <ProblemRow
        testId="till-authority-expired"
        title={AUTHORITY_EXPIRED}
        action={
          <button
            type="button"
            className={cta ? "btn btn-cta" : "btn"}
            data-testid="till-renew"
            disabled={till.busy || !till.online}
            onClick={() =>
              void outcome.run(() => engine.renew(), "Billing window renewed for another 24 hours.")
            }
          >
            <RefreshCw size={15} className={till.busy ? "till-spin" : ""} />
            {till.busy ? "Renewing…" : "Renew"}
          </button>
        }
      >
        <p className="till-problem-text">Saved bills are safe. Renew to bill again.</p>
      </ProblemRow>
      <Outcome failed={outcome.failed} said={outcome.said} id="till-registration" />
    </>
  );
}

/**
 * Billing paused for a transfer or a reviewed count (change PRD §10.2).
 *
 * Everything shown is the engine's own state, which is what the commit itself
 * reads: a card that asked the server instead could say "paused" about a
 * counter that is not. There is no clock on whoever approves the transfer - the
 * counter stays paused until somebody resumes it.
 */
function PausedRow({
  engine,
  till,
  cta,
}: {
  engine: TillEngine;
  till: TillSnapshot;
  cta: boolean;
}) {
  const outcome = useOutcome();
  const pause = till.pause;
  if (!pause) return null;
  const busy = till.busy;
  return (
    <>
      <ProblemRow
        testId="till-pause-state"
        stage={pause.stage}
        title={
          pause.stage === "pausing"
            ? "Billing paused. Waiting for head office to release the stock."
            : pause.stage === "resuming"
              ? "Billing paused until a fresh stock copy arrives."
              : "Billing paused. Stock released for the transfer or count."
        }
        action={
          <div className="till-actions">
            {pause.stage === "pausing" && (
              <button
                type="button"
                className="btn"
                data-testid="till-pause-retry"
                disabled={busy}
                onClick={() =>
                  void outcome.run(
                    () => engine.pauseForTransfer(pause.reason),
                    "Stock released. Continue the transfer or count.",
                  )
                }
              >
                Ask head office again
              </button>
            )}
            <button
              type="button"
              className={cta ? "btn btn-cta" : "btn"}
              data-testid="till-pause-resume"
              disabled={busy}
              onClick={() =>
                void outcome.run(() => engine.resume(), "Billing resumed with fresh stock.")
              }
            >
              {busy ? "Working…" : "Resume billing"}
            </button>
          </div>
        }
      >
        <p className="till-problem-text" data-testid="till-pause-warning">
          Reason: {pause.reason}. Resume when the transfer or count is done.
        </p>
      </ProblemRow>
      <Outcome failed={outcome.failed} said={outcome.said} id="till-pause" />
    </>
  );
}

/**
 * Not this store's counter (OPS-09). A manager registers the device; a second
 * machine registering beside the first is refused, and the refusal comes back
 * in words.
 */
function UnregisteredRow({
  engine,
  till,
  cta,
  may,
}: {
  engine: TillEngine;
  till: TillSnapshot;
  cta: boolean;
  may: boolean;
}) {
  const outcome = useOutcome();
  return (
    <>
      <ProblemRow
        testId="till-not-registered"
        title="Not this store's counter"
        action={
          may && (
            <button
              type="button"
              className={cta ? "btn btn-cta" : "btn"}
              data-testid="till-register"
              disabled={till.busy || !till.online}
              onClick={() =>
                void outcome.run(
                  () => engine.registerTill({ replace: false, reason: "" }),
                  "This device is now the store's counter.",
                )
              }
            >
              Register this device
            </button>
          )
        }
      >
        <p className="till-problem-text">
          {may
            ? "No 24-hour offline window until this device is registered."
            : "No 24-hour offline window. Ask a manager to register this device."}
        </p>
      </ProblemRow>
      <Outcome failed={outcome.failed} said={outcome.said} id="till-registration" />
    </>
  );
}

// -- work lists ----------------------------------------------------------------

/**
 * The drawer of printed bills the old machine never sent.
 *
 * Each one is a link into Billing rather than a form here: re-entering a bill is
 * billing - the same lines, the same salesperson, the same tender - and a second,
 * simpler screen for it would be a second place where a bill can be got wrong.
 *
 * A number the response did not name is not a number nobody has to key in, so the
 * count is shown beside the list whenever the two differ (a machine dead at bill
 * 5,000 leaves more holes than a response should carry).
 *
 * The ticks come from what the till has actually keyed in, not from what is left
 * in the queue: a re-entered bill leaves the queue the moment head office takes
 * it, and a list that lost its ticks as the store worked would have somebody key
 * the same receipt in twice.
 */
function PaperReentry({
  engine,
  till,
  handover,
}: {
  engine: TillEngine;
  till: TillSnapshot;
  handover: HandoverState;
}) {
  const done = new Set(till.paperEntered);
  // The frozen list the handover named **and** whatever head office is still
  // missing now. Both, because neither alone is the job: the handover's list is
  // capped at 200, so a machine that died at bill 5,000 would strand the rest
  // out of reach - and the register's list is the live one, which drops a number
  // the moment a re-entry syncs, taking its tick with it.
  const outstanding = [...new Set([...handover.unsynced_hint, ...(till.register?.holes ?? [])])]
    .sort((a, b) => a - b)
    .filter((seq) => !done.has(seq));
  const ticked = handover.unsynced_hint.filter((seq) => done.has(seq));
  const listed = [...outstanding, ...ticked].sort((a, b) => a - b);
  const stillHidden = Math.max(0, (till.register?.hole_count ?? 0) - outstanding.length);

  return (
    <section className="card till-card" data-testid="till-reentry">
      <div className="till-section-head">
        <h2 className="h3">Bills to key in from the printed copies</h2>
        <span className="till-progress">
          {ticked.length} of {listed.length} keyed in
        </span>
      </div>
      <p className="muted-cell">
        Handed over {formatDateTime(handover.at)}. Key in each printed bill under its own number.
        {stillHidden > 0 && <> {stillHidden} more appear as these sync.</>}
      </p>

      {listed.length === 0 ? (
        <p className="muted-cell" data-testid="till-reentry-none">
          Nothing left to enter.
        </p>
      ) : (
        <ul className="till-reentry-list">
          {listed.map((seq) => (
            <li key={seq} className={done.has(seq) ? "till-reentry-done" : ""}>
              <span className="till-reentry-no">Bill {seq}</span>
              {done.has(seq) ? (
                <span className="ok-note" data-testid={`till-reentry-done-${seq}`}>
                  <Check size={14} /> keyed in
                </span>
              ) : (
                <Link className="btn" data-testid={`till-reentry-${seq}`} to={`/sell?paper=${seq}`}>
                  Enter this bill
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        className="btn"
        data-testid="till-reentry-clear"
        disabled={outstanding.length > 0}
        title={
          outstanding.length > 0
            ? "There are still bills on this list to key in."
            : "Put the list away - every bill on it has been entered."
        }
        onClick={() => void engine.clearHandover()}
      >
        {outstanding.length > 0 ? `${outstanding.length} still to enter` : "Put this list away"}
      </button>
    </section>
  );
}

/** Shown only while something is waiting: an empty queue is already the 0 in
 *  the band, and a heading over nothing is noise. A refused bill leads. */
function QueueTable({ till }: { till: TillSnapshot }) {
  const halted = till.halt?.doc_number;
  const rows = [...till.queue].sort(
    (a, b) => Number(b.doc_number === halted) - Number(a.doc_number === halted),
  );
  return (
    <section className="till-queue">
      <h2 className="h3 till-section-head">
        Waiting to sync <span className="chip">{till.pending}</span>
      </h2>
      <div className="table-wrap">
        <table className="data" data-testid="till-queue">
          <thead>
            <tr>
              <th>Bill</th>
              <th>Billed at</th>
              <th className="num">Net ₹</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((bill) => {
              const refused = bill.doc_number === halted;
              return (
                <tr key={bill.idempotency_uuid} className={refused ? "till-queue-refused" : ""}>
                  <td>{bill.doc_number}</td>
                  <td>{formatDateTime(bill.billed_at)}</td>
                  <td className="num">
                    <Money paise={bill.totals.net_paise} />
                  </td>
                  <td className={refused ? "" : "muted-cell"}>{queueStatus(bill, refused)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function queueStatus(bill: TillSnapshot["queue"][number], refused: boolean): string {
  if (refused) return "Not accepted";
  if (!bill.attempts) return "Not tried yet";
  const tries = `Tried ${bill.attempts} ${bill.attempts === 1 ? "time" : "times"}`;
  return bill.last_error ? `${tries}: ${bill.last_error}` : tries;
}

// -- counter tasks -------------------------------------------------------------

/** The jobs somebody opens on purpose. Each is one line until its button is
 *  pressed, and then asks for its reason in place. */
function CounterTasks({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const { user } = useAuth();
  const mayMove = userCan(user, "sell", "approve") && Boolean(till.device?.identity?.registered);
  const showPause = !till.pause;
  if (!showPause && !mayMove && !till.onlineAlpha) return null;
  return (
    <section className="card till-card till-tasks">
      <h2 className="h3">Counter tasks</h2>
      <div className="till-task-list">
        {showPause && <PauseTask engine={engine} till={till} />}
        {mayMove && <MoveTask engine={engine} till={till} />}
        {till.onlineAlpha && <PairTask engine={engine} paired={till.devicePaired} />}
      </div>
    </section>
  );
}

function TaskRow({
  title,
  text,
  open,
  button,
  children,
  after,
  testId,
}: {
  title: string;
  text: ReactNode;
  open: boolean;
  button?: ReactNode;
  /** The form, shown while the task is open. */
  children?: ReactNode;
  /** The outcome, shown whether or not the form is open. */
  after?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="till-task" data-testid={testId}>
      <div className="till-task-line">
        <div className="till-task-body">
          <h3 className="till-task-title">{title}</h3>
          <p className="muted-cell">{text}</p>
        </div>
        {!open && button}
      </div>
      {open && <div className="till-task-form">{children}</div>}
      {after}
    </div>
  );
}

/**
 * Transfers and reviewed stock counts: pause billing, release, then resume.
 *
 * Every sync protects the store's stock for this counter, and while it does no
 * transfer out of the store can be approved. Anand, 25 September 2026 (change
 * PRD §10.2): the store's own person pauses billing here and lets the stock go
 * in one step, and nothing is billed until they resume and the counter has
 * taken a fresh copy of the shelf. Once paused, the band carries it.
 */
function PauseTask({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const outcome = useOutcome();
  const busy = till.busy;

  if (till.allocationVersion === null) {
    return (
      <TaskRow
        title="Pause billing for a transfer or count"
        open={false}
        text={
          <span data-testid="till-pause-none">
            This counter holds no stock. A stock count still needs a pause with every bill synced.
          </span>
        }
      />
    );
  }

  return (
    <TaskRow
      testId="till-pause"
      title="Pause billing for a transfer or count"
      open={open}
      after={<Outcome failed={outcome.failed} said={outcome.said} id="till-pause" />}
      text={
        <span data-testid="till-pause-held">
          All bills must be synced. Billing stays off until you resume.
        </span>
      }
      button={
        <button
          type="button"
          className="btn"
          data-testid="till-pause-open"
          onClick={() => {
            setOpen(true);
            outcome.setSaid("");
          }}
        >
          Pause billing
        </button>
      }
    >
      <div className="till-inline-form">
        <div className="field">
          <label htmlFor="till-pause-reason">Reason</label>
          <input
            id="till-pause-reason"
            className="input"
            data-testid="till-pause-reason"
            autoComplete="off"
            disabled={busy}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="btn btn-primary"
          data-testid="till-pause-go"
          disabled={busy || !reason.trim()}
          onClick={() =>
            void outcome
              .run(() => engine.pauseForTransfer(reason), "Paused. Continue the transfer or count.")
              .then((ok) => {
                if (ok) {
                  setReason("");
                  setOpen(false);
                }
              })
          }
        >
          {busy ? "Working…" : "Pause billing"}
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </TaskRow>
  );
}

/**
 * The counter moves onto this machine (#189, OPS-09).
 *
 * One action with one reason: registering this device as the replacement is what
 * retires the old one and writes the store's handover row on the server, so the
 * page never asks for a second handover on top - that would record one move
 * twice. The engine then builds the key-in list from the register it re-reads.
 *
 * Offered only to a manager - the same rung the server gates it at.
 */
function MoveTask({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const outcome = useOutcome();
  const busy = till.busy;

  async function move() {
    const ok = await outcome.run(() =>
      engine.registerTill({ replace: true, reason: reason.trim() }),
    );
    if (!ok) return;
    // The counter's own number, not one the server suggested: where the two
    // clocks disagree about the financial year the till declines to move (see
    // `reconcileRegister`), and reporting the server's figure would say the
    // counter had gone somewhere it had not.
    const after = engine.getSnapshot();
    const toKey = after.handover?.hole_count ?? 0;
    outcome.setSaid(
      `This machine is now the counter. Next bill is ${after.nextNumber}.` +
        (toKey ? ` ${toKey} ${toKey === 1 ? "bill" : "bills"} to key in.` : ""),
    );
    setReason("");
    setOpen(false);
  }

  return (
    <TaskRow
      testId="till-handover"
      title="Move the counter to this machine"
      open={open}
      after={<Outcome failed={outcome.failed} said={outcome.said} id="till-move" />}
      text="Use when the counter machine is replaced or broken. Bills it never sent are listed to key in."
      button={
        <button
          type="button"
          className="btn"
          data-testid="till-move-open"
          onClick={() => {
            setOpen(true);
            outcome.setSaid("");
          }}
        >
          <Replace size={15} /> Move the counter
        </button>
      }
    >
      <div className="till-inline-form">
        <div className="field">
          <label htmlFor="till-move-reason">Reason (recorded with your name)</label>
          <input
            id="till-move-reason"
            className="input"
            data-testid="till-move-reason"
            autoComplete="off"
            disabled={busy}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="btn btn-primary"
          data-testid="till-move-go"
          disabled={busy || !reason.trim() || !till.online}
          onClick={() => void move()}
        >
          {busy ? "Moving…" : "Move the counter here"}
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </TaskRow>
  );
}

function PairTask({ engine, paired }: { engine: TillEngine; paired: boolean }) {
  // An unpaired browser cannot bill, so its code box starts open.
  const [open, setOpen] = useState(!paired);
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  return (
    <TaskRow
      title={paired ? "Pair this counter again" : "Pair this browser"}
      open={open}
      after={message && <p role="status">{message}</p>}
      text="Enter the one-time pairing code from Owner or Admin."
      button={
        <button type="button" className="btn" onClick={() => setOpen(true)}>
          Enter pairing code
        </button>
      }
    >
      <div className="till-inline-form">
        <label className="field">
          <span>Pairing code</span>
          <input
            className="input"
            type="password"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            autoComplete="off"
          />
        </label>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!code}
          onClick={() =>
            void engine
              .pairDevice(code)
              .then(() => {
                setCode("");
                setOpen(false);
                setMessage("This browser is paired.");
              })
              .catch((error) => setMessage(String(error.message)))
          }
        >
          Pair counter
        </button>
        <button type="button" className="btn" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </TaskRow>
  );
}

// -- this counter and you ------------------------------------------------------

function PersonalSettings({ engine, muted }: { engine: TillEngine; muted: boolean }) {
  return (
    <section className="card till-card">
      <h2 className="h3">This counter and you</h2>
      <div className="till-task-list">
        <ScanSounds engine={engine} muted={muted} />
        <CounterPin />
      </div>
    </section>
  );
}

/**
 * The counter's noise, on or off (#247, grill Q8).
 *
 * Per counter rather than per person, because that is what it is about: one
 * till stands next to the music and another sits in a back office. Pressing it
 * plays the tone it is turning on, which is the only honest way to answer "is
 * the sound working?".
 */
function ScanSounds({ engine, muted }: { engine: TillEngine; muted: boolean }) {
  return (
    <div className="till-task">
      <div className="till-task-line">
        <div className="till-task-body">
          <h3 className="till-task-title">
            Scan sounds{" "}
            <span className="muted-cell till-inline-state" data-testid="till-sound-state">
              {muted ? "Off" : "On"}
            </span>
          </h3>
          <p className="muted-cell">Tick on a good scan, buzz on a problem. This counter only.</p>
        </div>
        <button
          type="button"
          className="btn"
          data-testid="till-sound-toggle"
          aria-pressed={!muted}
          onClick={() => {
            void engine.setMuted(!muted);
            // Turning it back on plays the tick, so the answer to "did that
            // work?" is the sound itself rather than a sentence about it.
            if (muted) playTone("tick", false);
          }}
        >
          {muted ? <VolumeX size={15} /> : <Volume2 size={15} />}
          {muted ? "Turn the sounds on" : "Turn the sounds off"}
        </button>
      </div>
    </div>
  );
}

/**
 * A person's own counter PIN (#182).
 *
 * It lives here rather than on the Billing screen because it is not part of
 * selling: it is this person's credential, set through the authenticated server
 * capability (`may_set_till_pin`). Any exception approval separately requires
 * its independently authorised reviewer; possessing a PIN grants no action.
 *
 * They type it themselves, and prove who they are with their own password.
 * Admin may also set a manager's PIN, and it works at once, or clear one
 * (Setup > Users & Roles, store operations ticket 06, baseline B76); the
 * manager may still change their own here. Either way every till picks the
 * change up on its next sync. See "Who sets one" in `accounts/till_pin.py`.
 */
function CounterPin() {
  const { user } = useAuth();
  const [pin, setPin] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [said, setSaid] = useState("");
  const [failed, setFailed] = useState("");
  // Held here rather than re-read from `/me`: the only thing it changes is a
  // sentence, and the person who just set a PIN knows they have one.
  const [hasPin, setHasPin] = useState(Boolean(user?.has_till_pin));
  const [open, setOpen] = useState(false);

  if (!user?.may_set_till_pin) return null;

  async function save() {
    if (saving) return;
    setSaving(true);
    setSaid("");
    setFailed("");
    try {
      await api.put("/auth/me/till-pin", { pin, current_password: password });
      setPin("");
      setPassword("");
      setHasPin(true);
      setOpen(false);
      setSaid("Your counter PIN is set. Tills pick it up on their next sync.");
    } catch (error) {
      setFailed(apiErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <TaskRow
      testId="till-pin"
      title="Your counter PIN"
      open={open}
      after={
        <>
          {failed && (
            <p className="till-alert" data-testid="till-pin-failed">
              <AlertTriangle size={15} />
              {failed}
            </p>
          )}
          {said && (
            <p className="ok-note till-said" data-testid="till-pin-said">
              <Check size={15} />
              {said}
            </p>
          )}
        </>
      }
      text={
        hasPin
          ? "Set. Used for manager approvals at the counter."
          : "Not set. Used for manager approvals at the counter."
      }
      button={
        <button
          type="button"
          className="btn"
          data-testid="till-pin-open"
          onClick={() => {
            setOpen(true);
            setSaid("");
          }}
        >
          {hasPin ? "Change" : "Set a PIN"}
        </button>
      }
    >
      <div className="till-inline-form">
        <div className="field">
          <label htmlFor="till-pin-new">New PIN (4–6 digits)</label>
          <input
            id="till-pin-new"
            className="input"
            data-testid="till-pin-new"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            disabled={saving}
            value={pin}
            onChange={(e) => setPin(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="till-pin-password">Your password</label>
          <input
            id="till-pin-password"
            className="input"
            data-testid="till-pin-password"
            type="password"
            autoComplete="current-password"
            disabled={saving}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="btn btn-primary"
          data-testid="till-pin-save"
          disabled={saving || !pin || !password}
          onClick={() => void save()}
        >
          {saving ? "Setting…" : "Set my PIN"}
        </button>
        <button type="button" className="btn" disabled={saving} onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </TaskRow>
  );
}

// -- the facts rail ------------------------------------------------------------

/**
 * What somebody reads down the phone: which device this is, what head office has
 * taken, and the rules the counter is billing by. Read off the same snapshot the
 * band uses - a second place to look, never a second source.
 */
function FactsRail({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const identity = till.device?.identity ?? null;
  const registered = Boolean(identity?.registered);
  const renewal = useOutcome();

  return (
    <aside className="card till-rail" aria-label="Counter details">
      <section className="till-facts" data-testid="till-registration">
        <h2 className="till-facts-head">Counter</h2>
        <Row label="Store" value={till.storeCode} />
        <Row label="Connection" value={till.online ? "Online" : "Offline"} />
        <Row
          label="Counter"
          value={registered ? (identity?.counter_id ?? "") : "Not registered"}
          testId="till-registered"
        />
        {registered && (
          <>
            <Row
              label="Bill series"
              value={identity?.series_prefix ?? ""}
              testId="till-series-prefix"
            />
            {/* The renewal sits under the time it moves, inside the value
                column, rather than on a line of its own. */}
            <div className="till-row">
              <span className="till-row-label">Offline billing</span>
              <span className="till-row-value till-row-stack">
                <span data-testid="till-authority">
                  {till.authority.until && !till.authority.expired
                    ? `Open until ${shortDateTime(till.authority.until)}`
                    : describeAuthority(till.authority)}
                </span>
                {!till.authority.expired && (
                  <button
                    type="button"
                    className="till-text-btn"
                    data-testid="till-renew"
                    disabled={till.busy || !till.online}
                    onClick={() =>
                      void renewal.run(
                        () => engine.renew(),
                        "Billing window renewed for another 24 hours.",
                      )
                    }
                  >
                    <RefreshCw size={13} className={till.busy ? "till-spin" : ""} />
                    {till.busy ? "Renewing…" : "Renew"}
                  </button>
                )}
              </span>
            </div>
            <Outcome failed={renewal.failed} said={renewal.said} id="till-registration" />
            <Row
              label="Stock version"
              value={
                identity?.working_set_version === null ||
                identity?.working_set_version === undefined
                  ? "—"
                  : String(identity.working_set_version)
              }
              testId="till-working-set"
            />
          </>
        )}
      </section>

      <section className="till-facts">
        <h2 className="till-facts-head">Head office</h2>
        {till.register ? (
          <>
            <Row label="Financial year" value={till.register.fy} />
            <Row
              label="Last accepted"
              value={String(till.register.last_accepted_seq)}
              testId="till-register-last"
            />
            <Row
              label="Bill series open"
              value={till.register.series_open ? "Yes" : "No, bills will not sync"}
            />
            <Row
              label="Missing numbers"
              value={
                till.register.hole_count
                  ? `${till.register.hole_count} (${till.register.holes.slice(0, 8).join(", ")}${
                      till.register.hole_count > 8 ? "…" : ""
                    })`
                  : "None"
              }
            />
          </>
        ) : (
          <p className="muted-cell">Not checked yet. Sync to find out.</p>
        )}
      </section>

      <section className="till-facts">
        <h2 className="till-facts-head">Rules</h2>
        <Row
          label="Tax settings"
          value={taxSettingsNote(till.taxSettings)}
          testId="till-tax-settings"
        />
        <Row
          label="Invoice numbers"
          value={numberingNote(till.numbering)}
          testId="till-invoice-numbers"
        />
        <Row
          label="Offline limits"
          value={till.onlineOnlyRefusals ? "No B2B bills or credit notes" : "Off"}
          testId="till-online-only"
        />
        <Row
          label="Manager PIN"
          value={till.managerPinRules ? "Another manager's, never the cashier's" : "Off"}
          testId="till-manager-pin"
        />
      </section>

      <details className="till-facts till-local">
        <summary>
          <span className="till-facts-head">Local copy</span>
          <span className="muted-cell">{localSummary(till)}</span>
        </summary>
        <Row label="Items" value={count(till.counts.items)} />
        <Row label="Stock lines" value={count(till.counts.stock)} />
        <Row label="Offers" value={count(till.counts.offers)} />
        <Row label="Salespeople" value={count(till.counts.salespeople)} />
        <Row label="Managers" value={count(till.counts.managers)} />
        <Row label="Customers" value={count(till.counts.customers)} />
        <Row label="Tax slabs" value={count(till.counts.gstSlabs)} />
      </details>
    </aside>
  );
}

function localSummary(till: TillSnapshot): string {
  return `${count(till.counts.items)} items, ${count(till.counts.stock)} stock lines`;
}

/** A date and time without the year when it is this year: the rail is narrow,
 *  and a window that closes tomorrow does not need "2026" to be read. */
function shortDateTime(iso: string): string {
  const when = new Date(iso);
  return when.toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    ...(when.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
  });
}

function count(n: number): string {
  return n.toLocaleString("en-IN");
}

/** Which tax settings version this counter bills by today, from what it holds
 *  (ticket 03) - so a cashier offline can see it is still on the one it had. */
function taxSettingsNote(settings: TillTaxSettings | null): string {
  if (!settings?.rules_on) return "Version 1 (tax slabs)";
  const today = taxVersionFor(settings, tillToday());
  const newest = settings.versions.reduce((n, v) => Math.max(n, v.version), 1);
  const held = newest > (today?.version ?? 1) ? `; version ${newest} held for a later date` : "";
  return `Version ${today?.version ?? 1} today${held}`;
}

function Row({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="till-row">
      <span className="till-row-label">{label}</span>
      <span className="till-row-value" data-testid={testId}>
        {value}
      </span>
    </div>
  );
}

// -- no counter ----------------------------------------------------------------

function NoCounter({ reason }: { reason?: string }) {
  const { user } = useAuth();
  const { recoverPending } = useTill();
  const [recoveryMessage, setRecoveryMessage] = useState("");
  const [recovering, setRecovering] = useState(false);
  const singleStore = user?.stores.length === 1;
  async function checkPending() {
    if (!recoverPending || recovering) return;
    setRecovering(true);
    try {
      setRecoveryMessage(await recoverPending());
    } catch (error) {
      setRecoveryMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setRecovering(false);
    }
  }
  return (
    <div className="page-pad">
      <PageHeader />
      <div className="till-main till-no-counter">
        <p className="warn-note" data-testid="till-no-counter">
          {reason ||
            (singleStore
              ? "The counter is unavailable. Complete store setup and confirm this login's store access, then retry."
              : "A counter signs in to one store. Use the store login for billing; company setup is available from First store setup.")}
        </p>
        {(singleStore || recoverPending) && (
          <div className="till-actions">
            {singleStore && (
              <button className="btn" type="button" onClick={() => window.location.reload()}>
                Retry counter access
              </button>
            )}
            {recoverPending && (
              <button
                className="btn"
                type="button"
                disabled={recovering}
                onClick={() => void checkPending()}
              >
                {recovering ? "Checking sale…" : "Check unresolved sale"}
              </button>
            )}
          </div>
        )}
        {recoveryMessage && <p role="status">{recoveryMessage}</p>}
        <section className="card till-card">
          <div className="till-task-list">
            <CounterPin />
          </div>
        </section>
      </div>
    </div>
  );
}

// -- development ---------------------------------------------------------------

/**
 * Prove the spine without the billing screen.
 *
 * The whole of #180 is demonstrable in one click: number a bill locally, watch it
 * queue, watch it drain into a real Sale on the server. It is a development
 * affordance and it is fenced as one - a button that writes a bill nobody sold
 * has no business on a shop floor, so it is compiled out of a production build
 * rather than merely hidden.
 */
function TestBill() {
  const { engine, till } = useTill();
  const [note, setNote] = useState("");
  const [working, setWorking] = useState(false);
  if (!import.meta.env.DEV || !engine || !till) return null;

  async function queueOne() {
    if (!engine) return;
    setWorking(true);
    try {
      const draft = await fixtureBill(engine);
      const bill = await engine.commit(draft);
      setNote(`Queued ${bill.doc_number}.`);
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error));
    } finally {
      setWorking(false);
    }
  }

  return (
    <section className="card till-card till-dev">
      <h2 className="h3">Development only</h2>
      <p className="muted-cell">Queues a one-piece bill from the local price list and syncs it.</p>
      <button
        type="button"
        className="btn"
        data-testid="till-test-bill"
        disabled={working}
        onClick={() => void queueOne()}
      >
        {working ? "Queueing…" : "Queue a test bill"}
      </button>
      {note && (
        <p className="till-dev-note" data-testid="till-test-bill-note">
          {note}
        </p>
      )}
    </section>
  );
}

/** One clean cash sale of whatever the counter has, priced the way the till
 *  prices: ticket price inclusive, tax taken out of it, tender equal to the net. */
async function fixtureBill(engine: NonNullable<ReturnType<typeof useTill>["engine"]>) {
  const item = await engine.db.items.filter((row) => (row.mrp_paise ?? 0) > 0).first();
  if (!item) throw new Error("No priced piece in the local copy yet - sync first.");
  const slabs = await engine.db.gstSlabs.toArray();
  const salesperson = await engine.db.salespeople.orderBy("id").first();
  const net = item.mrp_paise as number;
  const billedAt = new Date().toISOString();
  const split = splitLine(net, 1, slabFor(slabs, item.hsn, billedAt.slice(0, 10)));
  const draft: BillDraft = {
    billed_at: billedAt,
    customer: { name: "", mobile: "", gstin: "" },
    lines: [
      {
        line_no: 1,
        direction: "sale",
        barcode: item.barcode,
        season: item.season,
        qty: 1,
        mrp_paise: net,
        disc_paise: 0,
        net_paise: net,
        gst_rate: split.rate,
        gst_paise: split.gst_paise,
        salesperson: salesperson?.id ?? null,
        offer_evidence: {},
      },
    ],
    tenders: [{ mode: "cash", amount_paise: net }],
    totals: {
      gross_paise: net,
      discount_paise: 0,
      net_paise: net,
      gst_paise: split.gst_paise,
      round_paise: 0,
    },
  };
  return draft;
}
