import { useState } from "react";
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
// person, or whoever they ring, can see the whole of it: what came down, what is
// waiting to go up, which bill number we are on, and - the one thing that ever
// needs a human - a bill the server would not take.
//
// Nothing here is money the person can change. There is no edit affordance on a
// queued bill by construction: it is printed, it is paid for, and the only two
// honest outcomes are that the server takes it or that somebody is told about it.
//
// It is also where a counter is put back together (#189), which is why the two
// recovery cards sit at the top rather than with the counts. Both of them move
// the number the next customer's bill will carry, so both are buttons somebody
// presses and neither happens on its own:
//
//   · **Recover this counter** - the browser threw the local database away, the
//     till is refusing to bill, and this takes the whole dataset again and asks
//     head office how far the store's series has got.
//   · **Move the counter to this machine** - the deliberate handover. A manager's
//     act, with a reason, and it hands back the bills the old machine never sent
//     so the store can key them in from the printed copies.

export default function TillPage() {
  const [pairingCode, setPairingCode] = useState("");
  const [pairingMessage, setPairingMessage] = useState("");
  const { engine, till, accessError } = useTill();

  if (!engine || !till) return <NoCounter reason={accessError} />;

  return (
    <div className="page-pad">
      <PageHeader
        lead="Bills offline and sends them to head office."
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

      <div className="till-stack">
        {till.status.colour !== "green" && (
          <p
            className={till.status.colour === "red" ? "till-alert" : "warn-note"}
            data-testid="till-status-reason"
          >
            {till.status.colour === "red" && <AlertTriangle size={15} />}
            {till.status.reason}
          </p>
        )}

        {/* The page has already narrowed both to non-null, so the recovery cards
            take them as props rather than reaching for the context again - one
            way in, and no optional chaining over a value that cannot be null. */}
        {till.storageLost && <RecoverCounter engine={engine} busy={till.busy} />}

        {till.legacyStorageQuarantined && (
          <section className="card till-card till-recover" role="status">
            <h2 className="h3">
              <AlertTriangle size={16} /> Earlier counter data needs review
            </h2>
            <p className="muted-cell">
              This browser holds bills and stock from an earlier store cache. They are kept and not
              uploaded. Ask Owner or Admin to reconcile them before recovering any bills.
            </p>
          </section>
        )}

        {till.halt && (
          <div className="card till-card till-halt" data-testid="till-halt">
            <h2 className="h3">Bill {till.halt.doc_number} was not accepted</h2>
            <p className="till-halt-why">{till.halt.message}</p>
            <p className="muted-cell">
              The bill is kept. Later bills wait behind it; new bills carry on. Refused at{" "}
              {formatDateTime(till.halt.at)} · {till.halt.code}
            </p>
            <div className="till-actions">
              <button
                type="button"
                className="btn"
                data-testid="till-halt-retry"
                onClick={() => void engine.retryHalted()}
              >
                Try this bill again
              </button>
            </div>
          </div>
        )}

        <Summary till={till} />

        {/* Shown only while something is waiting: an empty queue is already the
            0 in the summary above, and a heading over nothing is noise. */}
        {till.queue.length > 0 && (
          <section className="till-queue">
            <h2 className="h3 till-queue-heading">
              Waiting to sync <span className="chip">{till.pending}</span>
            </h2>
            <div className="table-wrap">
              <table className="data" data-testid="till-queue">
                <thead>
                  <tr>
                    <th>Bill</th>
                    <th>Billed at</th>
                    <th>Lines</th>
                    <th className="num">Net</th>
                    <th className="num">Tries</th>
                    <th>Last answer</th>
                  </tr>
                </thead>
                <tbody>
                  {till.queue.map((bill) => (
                    <tr key={bill.idempotency_uuid}>
                      <td>{bill.doc_number}</td>
                      <td>{formatDateTime(bill.billed_at)}</td>
                      <td>{bill.lines.length}</td>
                      <td className="num">
                        <Money paise={bill.totals.net_paise} />
                      </td>
                      <td className="num">{bill.attempts}</td>
                      <td className="muted-cell">{bill.last_error || "not tried yet"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        <div className="till-grid">
          <Registration engine={engine} till={till} />

          <section className="card till-card">
            <h2 className="h3">Head office</h2>
            {till.register ? (
              <>
                <Row label="Financial year" value={till.register.fy} />
                <Row
                  label="Last bill accepted"
                  value={String(till.register.last_accepted_seq)}
                  testId="till-register-last"
                />
                <Row
                  label="Bill series open"
                  value={till.register.series_open ? "Yes" : "No - bills will not sync"}
                />
                <Row
                  label="Missing bill numbers"
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
        </div>

        <section className="card till-card">
          <h2 className="h3">On this counter</h2>
          <div className="till-cols">
            <div>
              <h3 className="till-col-head">Data</h3>
              <Row label="Items" value={String(till.counts.items)} />
              <Row label="Stock lines" value={String(till.counts.stock)} />
              <Row label="Offers" value={String(till.counts.offers)} />
              <Row label="Salespeople" value={String(till.counts.salespeople)} />
              <Row label="Managers" value={String(till.counts.managers)} />
              <Row label="Customers" value={String(till.counts.customers)} />
              <Row label="Tax slabs" value={String(till.counts.gstSlabs)} />
            </div>
            <div>
              <h3 className="till-col-head">Rules</h3>
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
                value={till.onlineOnlyRefusals ? "No B2B bills or credit notes offline" : "Off"}
                testId="till-online-only"
              />
              <Row
                label="Manager PIN"
                value={till.managerPinRules ? "Another manager's PIN, never the cashier's" : "Off"}
                testId="till-manager-pin"
              />
            </div>
          </div>
        </section>

        <PauseForTransfer engine={engine} till={till} />

        <Handover engine={engine} till={till} />

        {till.onlineAlpha && (
          <section className="card till-card">
            <h2 className="h3">{till.devicePaired ? "Counter pairing" : "Pair this browser"}</h2>
            <p className="muted-cell">Enter the one-time pairing code from Owner or Admin.</p>
            <div className="till-inline-form">
              <label className="field">
                <span>Pairing code</span>
                <input
                  className="input"
                  type="password"
                  value={pairingCode}
                  onChange={(event) => setPairingCode(event.target.value)}
                  autoComplete="off"
                />
              </label>
              <button
                className="btn btn-primary"
                onClick={() =>
                  void engine
                    .pairDevice(pairingCode)
                    .then(() => {
                      setPairingCode("");
                      setPairingMessage("This browser is paired.");
                    })
                    .catch((error) => setPairingMessage(String(error.message)))
                }
              >
                Pair counter
              </button>
            </div>
            {pairingMessage && <p role="status">{pairingMessage}</p>}
          </section>
        )}

        <div className="till-settings">
          <ScanSounds engine={engine} muted={till.muted} />
          <CounterPin />
        </div>
        <TestBill />
      </div>
    </div>
  );
}

/** The four things a cashier opens this page to check, read off the snapshot the
 *  cards below already show - a second place to look, never a second source. */
function Summary({ till }: { till: TillSnapshot }) {
  return (
    <div className="till-summary" data-testid="till-summary">
      <Tile label="Next bill" value={till.nextNumber || "—"} />
      <Tile
        label="Waiting to sync"
        value={String(till.pending)}
        tone={till.pending ? "amber" : undefined}
      />
      <Tile
        label="Offline billing"
        value={windowLeft(till.authority)}
        tone={till.authority.expired ? "red" : undefined}
      />
      <Tile label="Last synced" value={till.syncedAt ? formatDateTime(till.syncedAt) : "Never"} />
    </div>
  );
}

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "amber" | "red" | undefined;
}) {
  return (
    <div className={`card till-tile${tone ? ` till-tile-${tone}` : ""}`}>
      <span className="till-tile-label">{label}</span>
      <span className="till-tile-value">{value}</span>
    </div>
  );
}

/** The billing window in a tile's few words. `describeAuthority` keeps the
 *  sentence other screens print. */
function windowLeft(state: TillSnapshot["authority"]): string {
  if (!state.known) return "No window";
  if (state.expired) return "Expired";
  const hours = Math.floor(state.remainingMs / 3_600_000);
  if (hours >= 1) return `About ${hours} h left`;
  return `About ${Math.max(1, Math.round(state.remainingMs / 60_000))} min left`;
}

/**
 * Which device this store's counter is, and how long it may keep billing (OPS-09).
 *
 * The one card on this page that answers a question the counter cannot answer for
 * itself: *may I bill right now, with no network?* Three states, and all three
 * are shown rather than inferred, because a counter that is about to stop taking
 * bills is a thing a store person has to be able to see coming.
 *
 *   · **Not registered.** This store has no counter, or has one somewhere else. A
 *     manager registers the device; a second machine registering beside the first
 *     is refused, and the card says what that means in words rather than showing
 *     a code.
 *   · **Registered, window open.** The series this device owns, and roughly how
 *     long it has left. Renew is offered at any time and costs nothing - it needs
 *     a line, which is exactly the evidence the window stands on.
 *   · **Registered, window closed.** The counter will not start a new bill.
 *     Nothing recorded is lost, the queue still drains, and the card says so -
 *     the failure mode to avoid is a store person deciding the day's takings have
 *     gone and doing something drastic about it.
 */
function Registration({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const { user } = useAuth();
  const [failed, setFailed] = useState("");
  const [said, setSaid] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [reason, setReason] = useState("");
  const mayRegister = userCan(user, "sell", "approve");
  const identity = till.device?.identity ?? null;
  const registered = Boolean(identity?.registered);

  async function renew() {
    setFailed("");
    setSaid("");
    try {
      await engine.renew();
      setSaid("Billing window renewed for another 24 hours.");
    } catch (error) {
      setFailed(messageOf(error));
    }
  }

  async function register(replace: boolean) {
    setFailed("");
    setSaid("");
    try {
      await engine.registerTill({ replace, reason: reason.trim() });
      setReplacing(false);
      setReason("");
      setSaid("This device is now the store's counter.");
    } catch (error) {
      setFailed(messageOf(error));
    }
  }

  return (
    <section className="card till-card" data-testid="till-registration">
      <h2 className="h3">Counter</h2>
      <Row label="Store" value={till.storeCode} />
      <Row label="Connection" value={till.online ? "Online" : "Offline"} />
      <Row
        label="Registered"
        value={registered ? `Yes · counter ${identity?.counter_id}` : "No"}
        testId="till-registered"
      />
      {registered && (
        <>
          <Row
            label="Bill series"
            value={identity?.series_prefix ?? ""}
            testId="till-series-prefix"
          />
          <Row
            label="Billing window"
            value={
              till.authority.until && !till.authority.expired
                ? `Open until ${formatDateTime(till.authority.until)}`
                : describeAuthority(till.authority)
            }
            testId="till-authority"
          />
          <Row
            label="Stock version"
            value={
              identity?.working_set_version === null || identity?.working_set_version === undefined
                ? "—"
                : String(identity.working_set_version)
            }
            testId="till-working-set"
          />
        </>
      )}
      {!registered && (
        <p className="muted-cell" data-testid="till-not-registered">
          Not this store&rsquo;s counter. No 24-hour offline window.
        </p>
      )}
      {till.authority.expired && (
        <p className="till-alert" data-testid="till-authority-expired">
          <AlertTriangle size={15} />
          {AUTHORITY_EXPIRED}. Saved bills are safe. Renew to bill again.
        </p>
      )}
      {failed && (
        <p className="till-alert" data-testid="till-registration-failed">
          <AlertTriangle size={15} />
          {failed}
        </p>
      )}
      {said && (
        <p className="ok-note" data-testid="till-registration-said">
          <Check size={15} />
          {said}
        </p>
      )}
      <div className="till-actions">
        {registered && (
          <button
            type="button"
            className="btn"
            data-testid="till-renew"
            disabled={till.busy || !till.online}
            onClick={() => void renew()}
          >
            <RefreshCw size={15} className={till.busy ? "till-spin" : ""} />
            {till.busy ? "Renewing…" : "Renew"}
          </button>
        )}
        {mayRegister && !registered && (
          <button
            type="button"
            className="btn btn-cta"
            data-testid="till-register"
            disabled={till.busy || !till.online}
            onClick={() => void register(false)}
          >
            Register this device
          </button>
        )}
        {mayRegister && registered && !replacing && (
          <button
            type="button"
            className="btn"
            data-testid="till-replace-open"
            onClick={() => setReplacing(true)}
          >
            <Replace size={15} /> Replace the counter device
          </button>
        )}
      </div>
      {mayRegister && replacing && (
        <div className="till-handover-ask" data-testid="till-replace-ask">
          <p className="muted-cell">
            This device gets a new counter number. Unsent bills from the old one must be keyed in
            from the printed copies.
          </p>
          <label className="field">
            <span>Reason</span>
            <input
              value={reason}
              data-testid="till-replace-reason"
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-cta"
            data-testid="till-replace-go"
            disabled={till.busy || !reason.trim() || !till.online}
            onClick={() => void register(true)}
          >
            Replace the counter device
          </button>
        </div>
      )}
    </section>
  );
}

/**
 * Putting a counter back together after the browser cleared its data (#189).
 *
 * Shown only when the till has actually noticed a loss, and it is the only way
 * out of that state: until this succeeds the Billing screen will not take a
 * bill, because a counter that does not know its own number would print one that
 * is already on a posted bill.
 *
 * The card says what the counter will be on afterwards rather than merely "done",
 * which is the whole of the acceptance criterion's "no silent counter reset" -
 * the number a store's bills carry has moved, and somebody should read it.
 */
function RecoverCounter({ engine, busy }: { engine: TillEngine; busy: boolean }) {
  const [failed, setFailed] = useState("");

  async function recover() {
    setFailed("");
    try {
      await engine.recover();
    } catch (error) {
      setFailed(messageOf(error));
    }
  }

  return (
    <section className="card till-card till-recover" data-testid="till-recover">
      <h2 className="h3">
        <AlertTriangle size={16} /> This counter lost its local data
      </h2>
      <p className="muted-cell">
        This browser lost the counter&rsquo;s data. Synced bills are safe. Recover to fetch the
        price list and the latest bill number before billing.
      </p>
      {failed && (
        <p className="till-alert" data-testid="till-recover-failed">
          <AlertTriangle size={15} />
          {failed}
        </p>
      )}
      <button
        type="button"
        className="btn btn-cta"
        data-testid="till-recover-go"
        disabled={busy}
        onClick={() => void recover()}
      >
        <HardDriveDownload size={15} />
        {busy ? "Recovering…" : "Recover this counter"}
      </button>
    </section>
  );
}

/**
 * The register handover, and the paper re-entry it leaves behind (#189, grill Q1).
 *
 * Two things in one card because they are one job with a gap in the middle: a
 * manager moves the store's counter onto this machine, and then somebody works
 * through the receipts the old machine printed and never sent.
 *
 * The list survives a reload and the ticks survive the queue draining, because
 * the work does: a drawer of forty receipts is an afternoon, and a list that
 * reset when a bill synced would have somebody keying the same one twice.
 *
 * The move itself is offered only to a manager - the same rung the server gates
 * it at - and the list is shown to whoever is standing here, because keying a
 * receipt back in is ordinary counter work.
 */
function Handover({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const { user } = useAuth();
  const [reason, setReason] = useState("");
  const [failed, setFailed] = useState("");
  const [said, setSaid] = useState("");
  const [asking, setAsking] = useState(false);
  const busy = till.busy;
  const mayHandOver = userCan(user, "sell", "approve");
  const handover = till.handover;

  if (!mayHandOver && !handover) return null;

  async function move() {
    setFailed("");
    setSaid("");
    try {
      await engine.handOver(reason.trim());
      setReason("");
      setAsking(false);
      // The counter's own number, not the one the server suggested. They are the
      // same in every ordinary case - and where they are not, it is because the
      // two clocks disagree about the financial year and the till has declined
      // to move (see `reconcileRegister`). Reporting the server's figure there
      // would tell somebody the counter had gone somewhere it had not.
      setSaid(
        "This machine is now the counter for this store. The next bill is " +
          `${engine.getSnapshot().nextNumber}.`,
      );
    } catch (error) {
      setFailed(messageOf(error));
    }
  }

  return (
    <section className="card till-card till-handover" data-testid="till-handover">
      <h2 className="h3">Move counter to this machine</h2>

      {mayHandOver && !asking && (
        <>
          <p className="muted-cell">
            Use when the counter machine is replaced or broken. Bills it never sent are listed here
            to key in again.
          </p>
          <button
            type="button"
            className="btn"
            data-testid="till-handover-open"
            onClick={() => {
              setAsking(true);
              setSaid("");
            }}
          >
            <Replace size={15} />
            Move the counter to this machine
          </button>
        </>
      )}

      {mayHandOver && asking && (
        <>
          <div className="field">
            <label htmlFor="till-handover-reason">Reason (recorded with your name)</label>
            <input
              id="till-handover-reason"
              className="input"
              data-testid="till-handover-reason"
              autoComplete="off"
              disabled={busy}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
          <div className="till-handover-actions">
            <button
              type="button"
              className="btn btn-cta"
              data-testid="till-handover-go"
              disabled={busy || !reason.trim()}
              onClick={() => void move()}
            >
              {busy ? "Moving…" : "Move the counter here"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => {
                setAsking(false);
                setFailed("");
              }}
            >
              Cancel
            </button>
          </div>
        </>
      )}

      {failed && (
        <p className="till-alert" data-testid="till-handover-failed">
          <AlertTriangle size={15} />
          {failed}
        </p>
      )}
      {said && (
        <p className="ok-note" data-testid="till-handover-said">
          {said}
        </p>
      )}

      {handover && <PaperReentry engine={engine} till={till} handover={handover} />}
    </section>
  );
}

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
    <div className="till-reentry" data-testid="till-reentry">
      <h3 className="h3">Bills to key in from the printed copies</h3>
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
 * taken a fresh copy of the shelf. There is no clock on whoever approves the
 * transfer - the counter stays paused until somebody resumes it.
 *
 * Everything shown is the engine's own state, which is what the commit itself
 * reads: a card that asked the server instead could say "paused" about a
 * counter that is not.
 */
function PauseForTransfer({ engine, till }: { engine: TillEngine; till: TillSnapshot }) {
  const [reason, setReason] = useState("");
  const [failed, setFailed] = useState("");
  const [said, setSaid] = useState("");
  const { pause, allocationVersion, busy } = till;

  async function run(work: () => Promise<void>, done: string) {
    setFailed("");
    setSaid("");
    try {
      await work();
      setReason("");
      setSaid(done);
    } catch (error) {
      setFailed(messageOf(error));
    }
  }

  return (
    <section className="card till-card" data-testid="till-pause">
      <h2 className="h3">Pause billing</h2>
      {pause && (
        <>
          <p className="till-alert" data-testid="till-pause-state" data-stage={pause.stage}>
            <AlertTriangle size={15} />
            {pause.stage === "pausing"
              ? "Billing paused. Waiting for head office to release the stock."
              : pause.stage === "resuming"
                ? "Billing paused until a fresh stock copy arrives."
                : "Billing paused. Stock released for the transfer or count."}
          </p>
          <p className="muted-cell" data-testid="till-pause-warning">
            Reason: {pause.reason}. Resume when the transfer or count is done.
          </p>
          <div className="till-actions">
            {pause.stage === "pausing" && (
              <button
                type="button"
                className="btn btn-secondary"
                data-testid="till-pause-retry"
                disabled={busy}
                onClick={() =>
                  void run(
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
              className="btn btn-cta"
              data-testid="till-pause-resume"
              disabled={busy}
              onClick={() => void run(() => engine.resume(), "Billing resumed with fresh stock.")}
            >
              {busy ? "Working…" : "Resume billing"}
            </button>
          </div>
        </>
      )}
      {!pause && allocationVersion === null && (
        <p className="muted-cell" data-testid="till-pause-none">
          This counter holds no stock. A stock count still needs a pause with every bill synced.
        </p>
      )}
      {!pause && allocationVersion !== null && (
        <>
          <p className="muted-cell" data-testid="till-pause-held">
            Pause before a stock transfer or stock count. All bills must be synced. Billing stays
            off until you resume.
          </p>
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
              className="btn btn-cta"
              data-testid="till-pause-go"
              disabled={busy || !reason.trim()}
              onClick={() =>
                void run(
                  () => engine.pauseForTransfer(reason),
                  "Paused. Continue the transfer or count.",
                )
              }
            >
              {busy ? "Working…" : "Pause billing"}
            </button>
          </div>
        </>
      )}
      {failed && (
        <p className="till-alert" data-testid="till-pause-failed">
          <AlertTriangle size={15} />
          {failed}
        </p>
      )}
      {said && (
        <p className="ok-note" data-testid="till-pause-said">
          {said}
        </p>
      )}
    </section>
  );
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
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
      setSaid("Your counter PIN is set.");
    } catch (error) {
      setFailed(apiErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="card till-card till-pin-card">
      <h2 className="h3">Your counter PIN</h2>
      <p className="muted-cell">
        {hasPin
          ? "You have a PIN. A new one replaces it on the next sync."
          : "Your PIN for manager approvals at the counter."}
      </p>

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

      {failed && (
        <p className="till-alert" data-testid="till-pin-failed">
          <AlertTriangle size={15} />
          {failed}
        </p>
      )}
      {said && (
        <p className="ok-note" data-testid="till-pin-said">
          {said}
        </p>
      )}

      <button
        type="button"
        className="btn"
        data-testid="till-pin-save"
        disabled={saving || !pin || !password}
        onClick={() => void save()}
      >
        {saving ? "Setting…" : "Set my PIN"}
      </button>
    </section>
  );
}

/**
 * The counter's noise, on or off (#247, grill Q8).
 *
 * Here rather than on the Billing screen, and per counter rather than per
 * person, because that is what it is about: one till stands next to the music
 * and another sits in a back office, and neither is head office's decision or a
 * cashier's login's. It is one switch and it takes effect on the next scan.
 *
 * Pressing it plays the tone it is turning on, which is the only honest way to
 * answer "is the sound working?" - a shop with the machine's volume down would
 * otherwise turn the setting on and off all afternoon.
 */
function ScanSounds({ engine, muted }: { engine: TillEngine; muted: boolean }) {
  return (
    <section className="card till-card till-sound-card">
      <h2 className="h3">Scan sounds</h2>
      <p className="muted-cell">
        Tick on a good scan, buzz on a problem. Set for this counter only.
      </p>
      <div className="till-sound-row">
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
        <span className="muted-cell" data-testid="till-sound-state">
          {muted ? "Off" : "On"}
        </span>
      </div>
    </section>
  );
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
      <PageHeader lead="Your store's counter and access." />
      <p className="warn-note" data-testid="till-no-counter">
        {reason ||
          (singleStore
            ? "The counter is unavailable. Complete store setup and confirm this login's store access, then retry."
            : "A counter signs in to one store. Use the store login for billing; company setup is available from First store setup.")}
      </p>
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
      {recoveryMessage && <p role="status">{recoveryMessage}</p>}
      <CounterPin />
    </div>
  );
}

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
