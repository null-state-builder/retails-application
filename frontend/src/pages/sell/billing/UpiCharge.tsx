import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, QrCode, RefreshCw, WifiOff } from "lucide-react";

import { Money } from "../../../lib/format";
import { brokeDown, chargeCardOf, chargeStamp } from "../../../till/payment";
import type { ChargeCard, ChargeStanding, PaymentAdapter, UpiCharged } from "../../../till/payment";
// The house modal (`.modal-backdrop` / `.modal` / `.modal-head`), which every
// screen with a dialog on it borrows from the same place.
import "../../Booking.css";

// ---------------------------------------------------------------------------
// The QR charge card (#248, grill Q5)
// ---------------------------------------------------------------------------
//
// Five states on one card, and the cashier can leave from any of them with the
// bill exactly as they left it. Nothing here writes to the cart until the bank
// says a payment happened, and on this build nothing can - the mock adapter
// cannot emit `success` (see `till/payment.ts`). What ships now is the flow and
// the seam; the hardware slice swaps the adapter underneath and touches no
// screen.
//
// **This component paints; it does not decide.** Which face a state wears, what
// it says, and which of the two offers it makes are `chargeCardOf`'s, in
// `till/payment.ts` where they have tests - the same split `balanceStandingOf`
// and the payment card's balance line already use. Getting it wrong is not
// cosmetic: an `unknown` dressed as a failure, or handed a "try again" instead
// of a "check again", is how a cashier collects the same money twice.
//
// **Closing is always available and always safe.** Cancel, a failure, an
// unknown, the backdrop, Escape - every way out lands back on the payment card
// with the UPI figure the cashier typed still in the box, ready to be saved as
// `manual`, which is the fallback that must never stop working: manual UPI
// entry is allowed forever, because billing never stops on the internet.

/** What the card is showing before the adapter has said anything at all. The
 *  first frame of a charge is `generating` and it arrives synchronously, so this
 *  is a floor rather than a state anybody should see. */
const OPENING: ChargeStanding = { state: "generating", reason: "", qr: "" };

export function UpiCharge({
  amountPaise,
  adapter,
  onConfirmed,
  onClose,
  onQr,
}: {
  /** What the UPI row is taking - the figure being charged, and the figure the
   *  stamp will be pinned to. */
  amountPaise: number;
  adapter: PaymentAdapter;
  /** The bank answered. Only reachable behind a real adapter. */
  onConfirmed: (charged: UpiCharged) => void;
  onClose: () => void;
  /** The code the customer should scan, while there is one to scan - handed to
   *  the customer display (ticket 09). Empty otherwise. */
  onQr?: (qr: string) => void;
}) {
  const [standing, setStanding] = useState<ChargeStanding>(OPENING);
  /** Bumped by "Show the QR again", which is how a failed card starts over. */
  const [attempt, setAttempt] = useState(0);
  const [checking, setChecking] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  /** Still on screen. A ref rather than the effect's own `live` flag because
   *  Check status is an `await` started by a click, and the cashier can close
   *  the card while the bank is being asked - at which point a stamp landing on
   *  a bill they have already moved to another tender would be money nobody
   *  agreed to. */
  const onScreen = useRef(true);
  /** Which charge is the live one. Bumped by every start, so an answer to a
   *  question asked about an earlier charge can be told apart from an answer
   *  about this one - see `recheck`. */
  const generation = useRef(0);

  useEffect(() => {
    onQr?.(standing.state === "awaiting" ? standing.qr : "");
  }, [standing, onQr]);

  useEffect(() => {
    onScreen.current = true;
    return () => {
      onScreen.current = false;
    };
  }, []);

  // Focus the card itself, on open and again whenever the state changes. Not a
  // nicety: Escape is one of the ways out this card promises, and a `keydown`
  // handler on a node nothing has focused never fires (`ManagerPin` gets away
  // with the same handler because it focuses its PIN box). Re-focusing matters
  // as much as the first focus, because the buttons come and go with the state
  // - the one the cashier last pressed is unmounted by the answer that arrives,
  // and focus falls off the card with it.
  //
  // It only holds because the dialog carries `data-wedge-ignore` (below). The
  // scan box patrols every 400ms and takes the cursor back off anything that is
  // not a person typing, so without that attribute this focus is reclaimed
  // within half a second - which is how browser QA found Escape dead again
  // after an answer landed.
  useEffect(() => {
    dialog.current?.focus();
  }, [standing.state]);

  useEffect(() => {
    // `live` as well as `adapter.cancel()`: the cancel stops the mock's own
    // timers, and the flag stops a state that is already in flight from landing
    // on an unmounted card. React's StrictMode runs this twice on mount, which
    // is exactly the race, so it is guarded rather than assumed away.
    let live = true;
    generation.current += 1;
    setStanding(OPENING);
    void (async () => {
      try {
        for await (const next of adapter.charge(amountPaise)) {
          if (!live) return;
          setStanding(next);
          const charged = chargeStamp(next, amountPaise);
          if (charged) {
            onConfirmed(charged);
            return;
          }
        }
      } catch (error) {
        // A terminal that threw. `unknown` rather than `failed`, and on purpose:
        // this counter has no idea how far the charge got before its machine
        // gave up, and calling that a failure is the one thing grill Q5 forbids.
        if (live) setStanding(brokeDown(error));
      }
    })();
    return () => {
      live = false;
      void adapter.cancel();
    };
    // `onConfirmed` is in the deps and the screen wraps it in `useCallback` to
    // keep it stable: a fresh identity every render would restart the charge
    // every render - a QR regenerating under a customer trying to scan it.
  }, [adapter, amountPaise, attempt, onConfirmed]);

  /** Ask the bank again. Never a way to fail a charge - `checkStatus` reports
   *  where it stands, and an unknown that stays unknown is the honest answer. */
  async function recheck() {
    if (checking) return;
    // Which charge this question is about. A cashier can press Check status,
    // watch that charge fail, press "Show the QR again", and only then have the
    // bank answer the *first* question - and letting that answer land would
    // paint a settled card over a second charge still out with the customer,
    // which is the double collection this card exists to prevent.
    const mine = generation.current;
    setChecking(true);
    try {
      const now = await adapter.checkStatus();
      // Guarded on both counts, and the stamp especially: the cashier can also
      // close this card while the bank is being asked, and a confirmation
      // landing after that would write a payment onto a bill they have already
      // taken another way.
      if (!onScreen.current || mine !== generation.current || !now) return;
      setStanding(now);
      const charged = chargeStamp(now, amountPaise);
      if (charged) onConfirmed(charged);
    } catch (error) {
      if (onScreen.current && mine === generation.current) setStanding(brokeDown(error));
    } finally {
      if (onScreen.current) setChecking(false);
    }
  }

  const card = chargeCardOf(standing, amountPaise);

  return (
    <div className="modal-backdrop" data-testid="bill-upi-modal" onClick={onClose}>
      <div
        ref={dialog}
        tabIndex={-1}
        className="modal bill-upi"
        role="dialog"
        aria-label="UPI payment"
        data-state={standing.state}
        // This card owns the keyboard while it is open (`useScanBox`). Without
        // it the scan box's patrol pulls the cursor back behind the modal, and
        // a wedge scanner fired at a counter mid-charge would type a barcode
        // into a bill nobody can see and add a line to it.
        data-wedge-ignore
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
        }}
      >
        <div className="modal-head">
          <h3 className="h3">
            <QrCode size={17} style={{ verticalAlign: "-3px", marginRight: 6 }} />
            UPI payment
          </h3>
          <button type="button" className="btn" data-testid="bill-upi-close" onClick={onClose}>
            {card.leaves === "cancel" ? "Cancel" : "Close"}
          </button>
        </div>

        {/* The figure the customer is being asked for, in the size it should be
            read at across a counter. It is the one thing on this card that is
            true in every state. */}
        <p className="bill-upi-amount" data-testid="bill-upi-amount">
          <Money paise={amountPaise} />
        </p>

        {card.tone === "waiting" ? (
          <Waiting standing={standing} says={card.says} />
        ) : (
          <Answer card={card} />
        )}

        <div className="bill-upi-actions">
          {card.canCheck && (
            <button
              type="button"
              className="btn btn-cta"
              data-testid="bill-upi-check"
              disabled={checking}
              onClick={() => void recheck()}
            >
              <RefreshCw size={15} />
              {checking ? "Checking…" : "Check status"}
            </button>
          )}
          {card.canRetry && (
            <button
              type="button"
              className="btn btn-cta"
              data-testid="bill-upi-again"
              onClick={() => setAttempt((n) => n + 1)}
            >
              <QrCode size={15} />
              Show the QR again
            </button>
          )}
        </div>

        <p className="muted-cell" data-testid="bill-upi-fallback">
          Closing this leaves the bill exactly as it is. The UPI amount stays on the payment
          card, and saving it records the payment on the cashier&apos;s word - which the day
          close shows separately from the ones the bank confirmed.
        </p>
      </div>
    </div>
  );
}

/** Generating and awaiting: the QR, and what the counter is waiting for. */
function Waiting({ standing, says }: { standing: ChargeStanding; says: string }) {
  const generating = standing.state === "generating";
  return (
    <>
      <div className="bill-upi-qr" data-testid="bill-upi-qr">
        {/* A real adapter hands over the acquirer's `upi://pay?…` payload and
            this becomes a drawn code. The mock has none, and a made-up square
            that looks scannable and pays nobody would be worse than saying so -
            which is why the placeholder says which counter it is standing on. */}
        <QrCode size={64} aria-hidden="true" />
        <span>
          {generating
            ? "Making the code…"
            : standing.qr
              ? "Ask the customer to scan this with any UPI app."
              : "No payment machine is connected to this counter yet, so there is no code to scan. This is the flow, waiting on the hardware."}
        </span>
      </div>
      <p className="bill-upi-says" data-testid="bill-upi-says">
        {says}
      </p>
    </>
  );
}

/** Success, failed, unknown: what the bank said, in the face it earns. */
function Answer({ card }: { card: ChargeCard }) {
  const Icon = card.tone === "good" ? CheckCircle2 : card.tone === "doubt" ? WifiOff : AlertTriangle;
  return (
    <p className={`bill-upi-answer is-${card.tone}`} data-testid="bill-upi-answer">
      <Icon size={18} />
      <span>{card.says}</span>
    </p>
  );
}
