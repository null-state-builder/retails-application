// Charging a customer's UPI from the counter (#248, grill Q5).
//
// One interface with one implementation, for the same reason as `PrintAdapter`
// next door: the implementation is the open question. Which acquirer, which
// soundbox, which SDK on which device - none of that is decided, and all of it
// is a hardware slice with lead time. `PaymentAdapter` is the seam that lets the
// answer land later without a screen changing.
//
// The rule that shapes everything here is grill Q5, and it is a rule about
// *money*: **manual UPI stays allowed forever, and the till never vouches for
// what it did not see the bank say.** Every UPI tender carries a stamp -
// `confirmed` when the bank answered through this charge flow, `manual` when the
// cashier vouched (soundbox, static QR, no line) - and the control on manual is
// visibility, not a block: the day close shows the two totals side by side.
//
// So the mock below **cannot emit `success`**. Not "does not"; cannot. Its
// answers are typed to exclude it and `payment.test.ts` drives every one of them
// out to the wire body to prove no `confirmed` reaches the server before real
// hardware exists. A confirmed stamp is the till telling head office a bank
// confirmed money, and there is no bank on the other end of a mock.
//
// **The acquirer owns the timeout.** Nothing on this counter turns a charge into
// a failure on its own clock. A charge the till cannot reach may well have taken
// the customer's money already, so it is `unknown` - a state with its own words
// and its own re-check - and never quietly `failed`, which would send the
// cashier to collect the same money twice.

/** Where a charge stands. The five the card draws, and the only five. */
export type ChargeState = "generating" | "awaiting" | "success" | "failed" | "unknown";

/** A charge, as the counter sees it right now. */
export interface ChargeStanding {
  state: ChargeState;
  /** The acquirer's transaction reference. Only ever set alongside `success` -
   *  the server refuses a `confirmed` tender without one, and refuses one on
   *  anything else (api-contract §2). */
  reference?: string;
  /** What the bank says it actually took, in paise. Set alongside `success`,
   *  and required for one to become a stamp (`chargeStamp`).
   *
   *  Not the same figure as the one the charge was started with, and that is
   *  the point: a real acquirer can capture part of a charge, or answer about a
   *  QR the customer re-presented, and a till that assumed the two were equal
   *  would stamp `confirmed` on a sum the bank never approved. An adapter that
   *  cannot say what was taken says nothing here, and the row falls back to
   *  `manual` - the cashier's word, which is the honest one. */
  amount_paise?: number;
  /** A sentence for the counter. Empty while there is nothing to say. */
  reason: string;
  /** What the acquirer wants encoded in the QR - a `upi://pay?…` payload. Empty
   *  when the adapter has none, which is the mock's whole answer: the card then
   *  draws a placeholder that says so, rather than a scannable-looking square
   *  that pays nobody. */
  qr: string;
}

/**
 * The counter's payment terminal, whatever it turns out to be.
 *
 * Flat rather than a per-charge handle, and deliberately: a counter has one
 * terminal and takes one payment at a time, so "the charge in flight" is a real
 * thing with one answer. `checkStatus` and `cancel` are about that one.
 *
 * `charge` is an async iterable rather than a promise because the states are the
 * point - the cashier watches a QR appear and then watches it be waited on, and
 * a promise would only hand back the ending.
 */
export interface PaymentAdapter {
  /** Start a charge and walk its states until it settles or is cancelled. */
  charge(amountPaise: number): AsyncIterable<ChargeStanding>;
  /** Ask again where the charge in flight stands - the Check status button.
   *  `null` when there is no charge to ask about. */
  checkStatus(): Promise<ChargeStanding | null>;
  /** Give up on the charge in flight. */
  cancel(): Promise<void>;
}

/** How long the mock spends making the QR. Short: the card must not sit on
 *  "Generating" long enough for a cashier to wonder whether it is stuck. */
export const GENERATING_MS = 600;

/** How long the mock's acquirer takes to answer.
 *
 *  A real UPI QR waits on a *person* - unlocking a phone, scanning, typing a
 *  PIN - which is tens of seconds and no fixed number. Six is a demo's patience,
 *  not an estimate of anything, and nothing outside this file may depend on it:
 *  the card shows Cancel and Check status for the whole of it. */
export const ANSWER_MS = 6_000;

/** The answers the mock is allowed to end on.
 *
 *  `success` is not in this type, and that is the enforcement (design.md
 *  assumption 6): a `confirmed` stamp cannot exist before real hardware because
 *  there is nothing here that can produce one. */
export type MockAnswer = Extract<ChargeState, "failed" | "unknown">;

/** Both answers, in the order one adapter hands them out - so a cashier or a
 *  reviewer can reach either by pressing the button twice, with no randomness
 *  to chase. */
export const MOCK_ANSWERS: readonly MockAnswer[] = ["failed", "unknown"];

const REASONS: Record<MockAnswer, string> = {
  failed:
    "The bank turned this payment down. Show the QR again, or take the money another way.",
  unknown:
    "This counter cannot reach the bank, so it does not know whether the money went through. Check again in a moment - or ask the customer to show their phone, and record the payment by hand.",
};

/** What the mock says while it is making a QR nobody has to look at yet. */
const GENERATING: ChargeStanding = { state: "generating", reason: "", qr: "" };

/** What it says once the QR is up. Deliberately no `qr` payload - see the field. */
const AWAITING: ChargeStanding = { state: "awaiting", reason: "", qr: "" };

export interface MockOptions {
  /** The answers to give, in order, cycling. Typed to exclude `success`. */
  answers?: readonly MockAnswer[];
}

/**
 * A payment terminal that is not there (#248).
 *
 * A factory rather than a singleton because the charge in flight is state, and a
 * module-level object would carry one test's abandoned charge into the next.
 * The app holds one; a test holds its own.
 */
export function createMockPaymentAdapter(options: MockOptions = {}): PaymentAdapter {
  const answers = options.answers?.length ? options.answers : MOCK_ANSWERS;

  /** Where the charge in flight stands, or `null` when there is none. It
   *  outlives a charge that *settled* - the answer is what Check status is for -
   *  and dies with one that was cancelled or taken over. */
  let standing: ChargeStanding | null = null;
  /** The charge in flight, as a thing to mark abandoned. A flag rather than a
   *  callback because it has to be readable at every step of the generator, not
   *  only inside a wait. */
  let live: { abandoned: boolean } | null = null;
  /** Cuts the wait the charge in flight is currently sitting in. */
  let wake: (() => void) | null = null;
  let given = 0;

  /** Drop whatever is in flight. Both `cancel` and a second `charge` come
   *  through here - React's StrictMode starts the card twice on every mount,
   *  and "Show QR" again on a failed card is a second charge over a first. */
  function drop(): void {
    if (live) live.abandoned = true;
    live = null;
    standing = null;
    wake?.();
    wake = null;
  }

  /** Wait - and stop waiting the moment the charge is dropped, rather than
   *  leaving a timer to land an answer on a card the cashier has closed. */
  function pause(ms: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        wake = null;
        resolve();
      }, ms);
      wake = () => {
        clearTimeout(timer);
        resolve();
      };
    });
  }

  async function* charge(amountPaise: number): AsyncIterable<ChargeStanding> {
    // `amountPaise` is what a real adapter puts in the QR. The mock has nothing
    // to put it in, and reads it only to refuse the one charge that is not a
    // charge at all.
    if (amountPaise <= 0) return;
    drop();
    const mine = { abandoned: false };
    live = mine;
    try {
      standing = GENERATING;
      yield standing;
      // Re-checked after every yield as well as after every wait: a cancel can
      // land while the card is rendering the state it was just handed, and a
      // charge that resumed from there would walk on to an answer nobody is
      // waiting for.
      if (mine.abandoned) return;

      await pause(GENERATING_MS);
      if (mine.abandoned) return;
      standing = AWAITING;
      yield standing;
      if (mine.abandoned) return;

      await pause(ANSWER_MS);
      if (mine.abandoned) return;
      const answer = answers[given % answers.length];
      given += 1;
      standing = { state: answer, reason: REASONS[answer], qr: "" };
      yield standing;
    } finally {
      // The settled `standing` deliberately stays - Check status is allowed to
      // repeat the answer. What ends here is only this charge's claim on the
      // terminal, so a later `cancel` cannot reach back into a finished one.
      if (live === mine) live = null;
    }
  }

  return {
    charge,

    // Where it *stands*, and nothing more. The one thing this must never do is
    // decide: an `unknown` re-checked stays `unknown` however long the cashier
    // keeps asking, because the acquirer owns the timeout and this counter has
    // no way to tell "still trying" from "the money moved" (grill Q5).
    async checkStatus(): Promise<ChargeStanding | null> {
      return standing;
    },

    async cancel(): Promise<void> {
      drop();
    },
  };
}

/** The counter's terminal for this build. Passed to the screen rather than
 *  imported by it, so a test can hand it another (design.md). */
export const mockPaymentAdapter: PaymentAdapter = createMockPaymentAdapter();

/** What a charge leaves on the bill: the bank's reference, and the figure it
 *  answered about. */
export interface UpiCharged {
  reference: string;
  /** Pinned so an edit to the UPI box afterwards cannot inherit the stamp -
   *  `tender.confirmedUpiOf` drops it the moment the two disagree. */
  amount_paise: number;
}

/**
 * What a charge's outcome does to the bill - the one place an adapter's answer
 * becomes a stamp.
 *
 * Everything short of a success the bank fully accounted for is `null`, which
 * leaves the cashier doing exactly what they do today: typing an amount into the
 * UPI box, stamped `manual`. Three things have to be true, and every one of them
 * fails closed:
 *
 * - the bank said `success`;
 * - it gave a reference - a `confirmed` tender without one is a `VALIDATION` at
 *   the server, on a bill already printed and already in a customer's hand;
 * - and it says it took **this** figure. `amountPaise` is what the counter asked
 *   for and `standing.amount_paise` is what the bank answered about; comparing
 *   the till to itself would prove nothing, and an acquirer that captured part
 *   of a charge would otherwise be recorded as having taken all of it.
 */
export function chargeStamp(standing: ChargeStanding, amountPaise: number): UpiCharged | null {
  if (standing.state !== "success" || amountPaise <= 0) return null;
  if (standing.amount_paise !== amountPaise) return null;
  const reference = (standing.reference ?? "").trim();
  return reference ? { reference, amount_paise: amountPaise } : null;
}

/**
 * The terminal itself gave up - it threw rather than answering (#248).
 *
 * `unknown`, never `failed`, and this is the same ruling as an unreachable bank
 * rather than a softer version of it: a machine that threw may have got the
 * charge away before it did, and the counter has no way to tell. The only safe
 * answer is the one that asks a person to look.
 */
export function brokeDown(error: unknown): ChargeStanding {
  const said = error instanceof Error && error.message ? ` (${error.message})` : "";
  return {
    state: "unknown",
    reason:
      "This counter's payment machine stopped answering, so nobody here knows whether the payment went through" +
      `${said}. Check with the customer before taking the money again, and record it by hand if they have paid.`,
    qr: "",
  };
}

/** How the charge card reads in this state: which of the four faces, in what
 *  words, with which ways on and which way out. */
export interface ChargeCard {
  /** `waiting` is the QR and the wait; the other three are answers. */
  tone: "waiting" | "good" | "bad" | "doubt";
  /** What the card says under the amount. */
  says: string;
  /** Ask the bank again is on offer. */
  canCheck: boolean;
  /** Start a fresh charge is on offer. */
  canRetry: boolean;
  /** What the way out is called - giving up on a charge still running, or
   *  closing an answer that has already landed. */
  leaves: "cancel" | "close";
}

/**
 * The charge card, resolved (#248).
 *
 * A rule with tests rather than five branches inside a component, following
 * `balanceStandingOf` next door in `tender.ts` - and for the same reason. Which
 * face a state wears is not decoration: `unknown` painted like `failed`, or an
 * `unknown` offered a "try again" instead of a "check again", is how a cashier
 * ends up collecting money the bank may already have taken (grill Q5). So the
 * three answers are told apart here, where it can be asserted, and the component
 * only paints what it is handed.
 *
 * The two offers are deliberately not the same button under two names. `check`
 * asks the bank where an existing charge stands and can only ever report; `retry`
 * abandons it and starts another. An `unknown` gets the first and never the
 * second - starting a second charge against a payment that may already have gone
 * through is exactly the double-collection this whole state exists to prevent.
 *
 * `amountPaise` is here so the card cannot say something the bill does not do.
 * A `success` the bank cannot fully account for - no figure, a different figure,
 * no reference - is refused by `chargeStamp` and the row goes up `manual`; a
 * card that painted it green and said "goes on the bill as confirmed" would have
 * the cashier believing the bank vouched for money it did not. One function
 * decides, and both the words and the stamp read it.
 */
export function chargeCardOf(standing: ChargeStanding, amountPaise: number): ChargeCard {
  switch (standing.state) {
    case "generating":
      return {
        tone: "waiting",
        says: "Asking the bank for a code.",
        canCheck: false,
        canRetry: false,
        leaves: "cancel",
      };
    case "awaiting":
      return {
        tone: "waiting",
        says:
          "Waiting for the bank to say the money arrived. This card waits as long as it takes - only the bank decides that a payment did not happen.",
        canCheck: true,
        canRetry: false,
        leaves: "cancel",
      };
    case "success": {
      const stamp = chargeStamp(standing, amountPaise);
      if (!stamp) {
        // A payment happened; what the counter cannot say is that *this* is the
        // payment it asked for. Doubt rather than good, and the words send the
        // cashier to the customer's phone before they save.
        return {
          tone: "doubt",
          says:
            "The bank says a payment went through, but not one this counter can account for - the amount it answered about is not the amount asked for, or it gave no reference. This bill will record the UPI on your word rather than the bank's. Check what the customer was actually charged.",
          canCheck: true,
          canRetry: false,
          leaves: "close",
        };
      }
      return {
        tone: "good",
        says: `The bank confirmed this payment. Reference ${stamp.reference}. It goes on the bill as confirmed.`,
        canCheck: false,
        canRetry: false,
        leaves: "close",
      };
    }
    case "failed":
      return {
        tone: "bad",
        says: standing.reason,
        canCheck: false,
        canRetry: true,
        leaves: "close",
      };
    case "unknown":
      return {
        tone: "doubt",
        says: standing.reason,
        canCheck: true,
        canRetry: false,
        leaves: "close",
      };
  }
}
