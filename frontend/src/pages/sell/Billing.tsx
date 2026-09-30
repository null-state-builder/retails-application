import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MutableRefObject, RefObject } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AlertTriangle, Gift, MonitorSmartphone, Undo2, X } from "lucide-react";

import { useAuth } from "../../auth/AuthContext";
import { PlusRail } from "./billing/plus/PlusRail";
import { PageHeader } from "../../components/PageHeader";
import { api, apiErrorMessage, typedApi } from "../../lib/api";
import { Money } from "../../lib/format";
import { useTill } from "../../till/TillProvider";
import { useCounterRoom } from "../../till/useCounterRoom";
import {
  addManualPiece,
  addPiece,
  emptyCart,
  inheritBillSalesperson,
  priceCart,
  toDraft,
  whyItCannotClose,
} from "../../till/cart";
import type { Cart, CartLine, PricedLine } from "../../till/cart";
import { useCart } from "../../till/useCart";
import { whyOfflineRefuses } from "../../till/onlineOnly";
import type { HeldBill } from "../../till/db";
import { clearDraft, persistDraft, readDraft, rekeyDraft, restoredDraft } from "../../till/draft";
import type { DraftPayload } from "../../till/draft";
import {
  isPastReturnWindow,
  lateReturnAsk,
  legsFrom,
  markReturnedPiece,
  pickedFromExchange,
  takeEverythingBack,
} from "../../till/exchange";
import type { Exchange, OriginalLine, PickedReturn, PickedReturns } from "../../till/exchange";
import { heldPayload, holdsToReview, restoreHold } from "../../till/held";
import { gstinRefusal, optionsAt, refusalWords } from "../../till/returnTax";
import { tillToday } from "../../till/pricing";
import { whySplitIsOff } from "../../till/split";
import { displayUrl, displayViewOf } from "../../till/customerDisplay";
import { useTillDisplay } from "../../till/useTillDisplay";
import { storeStateCodeOf, taxKindFor } from "../../till/gstin";
import type { B2bTaxKind } from "../../till/gstin";
import { describePiece, resolveScan, searchPieces } from "../../till/lookup";
import {
  UNCACHED_ORIGINAL,
  billSearchForCustomer,
  billSeqFrom,
  findHeldBill,
  findHeldBillByDoc,
  foundFromExchange,
  fromServer,
  mergeRecentBills,
  recentQueuedBills,
  resolveRecentMatch,
  searchCachedBillsByCustomer,
  searchKnownCustomers,
  searchQueuedBillsByCustomer,
  withQueuedReturns,
} from "../../till/original";
import type { FoundBill, RecentBillSummary } from "../../till/original";
import { LatestRequest } from "../../till/latest";
import { withStableQueue } from "../../till/sync";
import { mockPaymentAdapter } from "../../till/payment";
import type { PaymentAdapter, UpiCharged } from "../../till/payment";
import { browserPrintAdapter } from "../../till/print";
import { playTone, toneForScan } from "../../till/sounds";
import { postedReceiptHtml, receiptHtml } from "../../till/receipt";
import { toTenders } from "../../till/tender";
import type { Payment } from "../../till/tender";
import { covers } from "../../till/pin";
import type { Ask, Authorisation } from "../../till/pin";
import type { TillSnapshot } from "../../till/engine";
import type { QueuedBill, TillCustomer, TillItem, TillKnownCustomer } from "../../till/types";
import { emptyUndo, popUndo, pushUndo } from "../../till/undo";
import type { UndoStack } from "../../till/undo";
import { useScanBox } from "../../till/useScanBox";
import { useTillWorld } from "../../till/useTillWorld";
import { useCounterKeys } from "../../till/useCounterKeys";
// The house modal (`.modal-backdrop` / `.modal` / `.modal-head`), which every
// screen with a dialog on it borrows from the same place.
import "../Booking.css";
import { newUuid } from "../../till/uuid";
import { usePositionedPopover } from "../../shell/usePositionedPopover";
import { Lines } from "./billing/BillGrid";
import { BillBar } from "./billing/BillBar";
import type { CounterMode } from "./billing/BillBar";
import { ScanHero } from "./billing/ScanHero";
import { CustomerStrip } from "./billing/CustomerStrip";
import { ConsentCard } from "./billing/ConsentCard";
import { SavedSizeCard } from "./billing/SavedSizeCard";
import { ReservationPickupCard } from "./billing/ReservationPickupCard";
import { AlterationChargeCard } from "./billing/AlterationChargeCard";
import { addAlterationCharge } from "../../till/alteration";
import type { Reservation } from "../../lib/reservations";
import { SpecialOrderCollectCard } from "./billing/SpecialOrderCollectCard";
import { GiftVoucherCard } from "./billing/GiftVoucherCard";
import { whyVouchersCannotPay } from "../../till/giftVoucher";
import type { GiftVoucherHeld } from "../../till/giftVoucher";
import type { SpecialOrder } from "../../lib/specialOrders";
import { FinishOverlay } from "./billing/FinishOverlay";
import { HeldBills } from "./billing/HeldBills";
import { PaymentPanel } from "./billing/PaymentPanel";
import { RailFoot } from "./billing/RailFoot";
import { TaxFigure } from "./billing/TaxFigure";
import { UpiCharge } from "./billing/UpiCharge";
import { AgainstBill, RecentBills, ReturnCustomerSearch } from "./billing/ReturnCounter";
import type { ReturnBillMatch, ReturnSearchKey } from "./billing/ReturnCounter";
import { ManagerPin, useWrongPins } from "./ManagerPin";
import "./Billing.css";

// ---------------------------------------------------------------------------
// Billing - the counter (#181, D10 §4)
// ---------------------------------------------------------------------------
//
// The most important screen in the store, on the placement staff already know:
// scan box top right and always focused, the line grid in the middle, the
// payment panel on the right, the customer strip under it, totals, and a row of
// plain buttons. RetailJI contributes that placement and nothing else.
//
// Three things about it are decisions rather than taste.
//
// **Every action is a visible button.** F2/F3/F4/F9 and Esc are accelerators,
// never the only door (grill Q4, 2 Aug); `useCounterKeys` claims them from the
// browser while the scan hero keeps Enter for a wedge scanner.
//
// **Save & Print is the commit point, and it commits locally.** The number, the
// shelf and the queue move in one IndexedDB transaction (`commitBill`), the
// receipt prints afterwards, and the server hears about it whenever it can. A
// printer that is off is a banner and a Reprint button - never a lost sale, and
// never a bill number spent on nothing (G2).
//
// **Nothing here shows cost or margin** (H2), and ordinary scanning, pricing and
// committing ask the server nothing: every price, tax rate and stock figure
// comes from the counter's own copy. Finding an older return bill is the narrow
// exception; it asks the local queue first and head-office history only online.
//
// The payment panel has three incoming tender modes.
//
// **Cash is the balance until somebody types in it.** An ordinary all-cash sale
// should not need the total keyed into a box to say so, and a split should be
// exactly as explicit as it is. `tender.ts` holds that rule.
//
// **A hold is not a bill** (#185, grill Q13). Parking a cart writes one row in
// one local table: no number is taken, no piece leaves the shelf, nothing is
// queued. Picking one up prices it again from today's world, because a bill kept
// overnight is sold at tomorrow's prices and offers, not at the ones it happened
// to be parked under.
//
// **A piece coming back rides on the bill** (#273). Return mode finds the old
// bill and marks its incoming lines right here, then nets them against whatever
// is being bought instead. The refund is what the customer *paid*, never today's
// price; a bill whose returns outweigh its outgoing pieces is refused before
// close, and cash never comes out of the drawer.
//
// **A scan that finds nothing never sends the customer away** (#186, grill Q5).
// The tag is in their hand, so the counter offers to bill it off the tag: a line
// with a typed description and a typed price, no cohort behind it, and no cost of
// record. The server takes the bill, posts the money, and parks the cost event
// until the PT prices the piece - which is the only way Rule 5 (nothing at
// nought) and Rule 8 (nothing blocked) can both hold at once.

/** A fresh bill's customer strip. Spelled once because it is cleared from four
 *  places - New Bill, Hold, Save & Print, and the reset after a hold is parked -
 *  and a literal in one of them that forgot a field would leave the last
 *  customer's GSTIN on the next customer's tax invoice. */
const NO_CUSTOMER: TillCustomer = { name: "", mobile: "", gstin: "" };

/** Stand-in width for the scan box's floating prompts, before either has ever
 *  mounted (`usePositionedPopover`'s first clamp). Kept in step with
 *  `.bill-float`'s width in Billing.css. */
const SCAN_FLOAT_WIDTH = 380;

export default function BillingPage() {
  useCounterRoom();
  const { user } = useAuth();
  const { engine, till } = useTill();

  if (!engine || !till) return <NoCounter />;
  const storeName = user?.stores?.find((s) => s.code === engine.storeCode)?.name;

  return <Counter key={engine.storeCode} {...(storeName ? { storeName } : {})} />;
}

function Counter({
  storeName,
  // Passed rather than imported, unlike the print adapter next to it: the
  // payment terminal is the one seam a test has to be able to swap, and the
  // hardware slice (#190's sibling) replaces exactly this default (#248,
  // design.md). Nothing else on the screen knows which adapter it is.
  payments = mockPaymentAdapter,
}: {
  storeName?: string;
  payments?: PaymentAdapter;
}) {
  const { engine, till } = useTill();
  const { user } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const { cart, setCart, take: takeScanned } = useCart();
  const [customer, setCustomer] = useState<TillCustomer>(NO_CUSTOMER);
  const [saving, setSaving] = useState(false);
  const [holding, setHolding] = useState(false);
  const [note, setNote] = useState("");
  const [printProblem, setPrintProblem] = useState("");
  const [lastBill, setLastBill] = useState<{
    bill: QueuedBill;
    receipt: string;
    /** Null is the blank box, which means the customer handed over exactly the
     *  cash tender: no cash-received line on the paper and no change. */
    cashReceivedPaise: number | null;
  } | null>(null);
  const [finishOpen, setFinishOpen] = useState(false);
  /** Only the newest print attempt may speak to the screen. A reprint can be
   * superseded by another reprint or by the next customer's first scan. */
  const printRun = useRef(0);
  const [typed, setTyped] = useState("");
  /** A barcode this counter could not place, waiting on the cashier to say
   *  whether it is a mistyped tag or a piece that arrived before its paperwork
   *  (#186). Empty means nothing is being asked. */
  const [unknown, setUnknown] = useState("");
  // The last salesperson *picked*, which is not the same as the last one saved: a
  // bill often runs several lines before it closes, and the second piece should
  // land on the person who sold the first (D10 §4), not on whoever sold the
  // previous customer. Nought until somebody picks, and then the counter's copy
  // takes over from what the dataset remembered across the session.
  const [lastPicked, setLastPicked] = useState<string | null>(null);
  /** The bill-level actor is a default, not a replacement for a deliberate
   * per-line choice. It is materialised only for the held/saved bill. */
  const [billSalesperson, setBillSalesperson] = useState<string | null | undefined>(undefined);
  const [mode, setMode] = useState<CounterMode>(() =>
    params.get("mode") === "return" ? "return" : "sale",
  );
  const [returnFound, setReturnFound] = useState<FoundBill | null>(null);
  const [returnPicked, setReturnPicked] = useState<PickedReturns>({});
  const [returnOutgoing, setReturnOutgoing] = useState(false);
  const [returnLooking, setReturnLooking] = useState(false);
  const [returnError, setReturnError] = useState("");
  const [returnSearchOpen, setReturnSearchOpen] = useState(false);
  const [returnSearchKey, setReturnSearchKey] = useState<ReturnSearchKey>("mobile");
  const [returnSearchTerm, setReturnSearchTerm] = useState("");
  const [returnCustomers, setReturnCustomers] = useState<TillKnownCustomer[]>([]);
  const [returnMatches, setReturnMatches] = useState<ReturnBillMatch[] | null>(null);
  const [returnAsking, setReturnAsking] = useState<Ask[] | null>(null);
  const [recentBills, setRecentBills] = useState<ReturnBillMatch[]>([]);
  const returnRequests = useRef(new LatestRequest());
  const returnPins = useWrongPins();
  // Bumped by every commit so the in-memory copy re-reads the shelf the sale
  // just moved. The sync time covers the other direction.
  const [commits, setCommits] = useState(0);

  useEffect(() => {
    document.documentElement.dataset.counterMode = mode;
    return () => {
      delete document.documentElement.dataset.counterMode;
    };
  }, [mode]);

  useEffect(() => {
    const db = engine?.db;
    if (mode !== "return" || !db) return;
    let active = true;
    async function load() {
      try {
        const localSummaries = await recentQueuedBills(db!, 3);
        let serverSummaries: RecentBillSummary[] = [];
        if (navigator.onLine) {
          try {
            const res = await fetch("/api/sell/sales?recent=3");
            if (res.ok) {
              const data: unknown = await res.json();
              serverSummaries = Array.isArray(data)
                ? data.flatMap((candidate: unknown) => {
                    if (!candidate || typeof candidate !== "object") return [];
                    const row = candidate as Record<string, unknown>;
                    if (
                      typeof row.doc_number !== "string" ||
                      typeof row.billed_at !== "string" ||
                      typeof row.net_paise !== "number"
                    )
                      return [];
                    return [
                      {
                        doc_number: row.doc_number,
                        billed_at: row.billed_at,
                        customer_name:
                          typeof row.customer_name === "string" ? row.customer_name : "",
                        customer_mobile:
                          typeof row.customer_mobile === "string" ? row.customer_mobile : "",
                        net_paise: row.net_paise,
                        local: null,
                      },
                    ];
                  })
                : [];
            }
          } catch {
            // Ignore fetch errors
          }
        }
        const merged = mergeRecentBills(localSummaries, serverSummaries, 3);
        if (active) {
          setRecentBills(merged);
        }
      } catch {
        // Ignore
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [mode, engine?.db, commits]);
  /** Open while a UPI charge is on the card (#248). It carries no state of its
   *  own beyond "open": the figure being charged is read from the UPI row at
   *  the moment it opens, and closing it leaves the bill untouched. */
  const [charging, setCharging] = useState(false);
  /** The code the UPI card is showing, for the customer display (ticket 09). */
  const [upiQr, setUpiQr] = useState("");
  /** A bill was just saved and the counter is empty: the customer display
   *  says thank you until the next bill starts or somebody presses New bill. */
  const [thanking, setThanking] = useState(false);
  // --- cart safety: autosave and undo (#244) --------------------------------
  /** One snapshot per cart action, oldest first - see `till/undo.ts`. */
  const [undoStack, setUndoStack] = useState<UndoStack>(emptyUndo);
  /** The draft read on mount has landed - whether or not it had anything to
   *  restore. Autosave waits for this: writing the still-pristine cart the
   *  instant this screen mounts would beat the read to the punch and clobber
   *  the very draft it is about to look for. */
  const [restored, setRestored] = useState(false);
  /** Flipped once this screen has deliberately started a fresh bill - New
   *  bill, a commit, or a hold. A restore that lands after that is a screen
   *  the cashier has already moved on from, and must not resurrect it
   *  (binding rule 6, "don't restore over a fresh bill"). */
  const skipRestore = useRef(false);
  /** Which run of typing the undo stack is currently coalescing into. Bumped
   *  by anything that is not more typing in the same box, so a run cannot span
   *  an action that happened in the middle of it - see `editLine`. */
  const runSeq = useRef(0);
  /** Whatever is really on screen the instant the draft read could land -
   *  kept in sync at the only places that can populate it before that
   *  happens: a scan (`takePiece`/`takeUnknown`), return picking, and the
   *  customer strip. A `useEffect` mirroring `cart`/`customer`
   *  into a ref would run on React's own effect schedule, which is not
   *  guaranteed to have flushed before a sibling promise's `.then` lands -
   *  the same "no guaranteed order" the mount-race comment below already
   *  names - so this is written at the real mutation call sites instead,
   *  synchronously, where there is no such gap. Nothing reads it once
   *  `restored` is true. */
  const onScreenRef = useRef<{ cart: Cart; customer: TillCustomer }>({
    cart: emptyCart(),
    customer: NO_CUSTOMER,
  });
  /** A draft this screen would not auto-restore - a previous business day, or
   *  a paper number the counter no longer recognises - parked here for the
   *  cashier to resume or drop, rather than applied or lost silently (the 2
   *  Aug 2026 draft-age and paper-conflict rulings). */
  const [pendingDraft, setPendingDraft] = useState<{
    draft: DraftPayload;
    reason: "stale" | "paper-conflict";
  } | null>(null);
  /** Wrong PINs at this counter, and the pause they earn (`useWrongPins`). Only
   *  a speed bump: the hash is on this device by design (grill Q1), so what it
   *  buys is somebody guessing having to stand at the counter visibly doing
   *  nothing. */
  // The hold list opens on demand, and opens itself when the Dashboard's "bills
  // on hold" row sent somebody here to clear them (`/sell?holds=1`). Read once,
  // into state: after that the panel is the cashier's to open and close, and a
  // value that kept re-reading the address bar would spring open again on every
  // render behind their back.
  const [showHolds, setShowHolds] = useState(() => params.has("holds"));

  const world = useTillWorld(engine?.db ?? null, `${till?.syncedAt ?? ""}#${commits}`);
  const scan = useScanBox(world.loaded);

  /**
   * Re-entering a printed bill from the machine this counter replaced (#189).
   *
   * The number comes off the paper, from the list on Till & Sync, and so does the
   * date: a bill printed last Tuesday belongs in last Tuesday's books, and
   * stamping it with today's clock would put it in the wrong day's takings and
   * price its tax off the wrong slab.
   *
   * **Derived from the address and the counter's live state, never held in
   * state.** Each Sell route mounts its own `TillProvider`, so arriving here
   * from the handover list means a brand-new engine whose first snapshot knows
   * nothing yet - no register, no handover, no counter. A mode resolved once at
   * mount would therefore resolve to "not a paper bill" every single time, and
   * the screen would quietly hand the cashier an ordinary counter: Save & Print
   * would take a *new* number, print a second receipt, and leave the hole
   * exactly where it was. So the address bar is the request and the snapshot is
   * the answer, and leaving paper mode means taking the request away.
   *
   * Only a number the counter still regards as outstanding is honoured. The link
   * that gets somebody here is on the handover list, so a number from anywhere
   * else is a hand-typed address, and letting one through would offer to bill a
   * second time under a number head office already holds - which halts the whole
   * store's queue when it lands (`BILL_NO_TAKEN` is terminal).
   */
  const paper = useMemo(() => outstandingPaperSeq(params, till), [params, till]);
  const [paperAt, setPaperAt] = useState(() => localNow());

  /**
   * Land a decided draft on screen whole - cart, customer, and its paper claim
   * if `outstandingPaperSeq` still accepts it right now (#244, binding rule
   * 0c). Used both by the mount-time auto-restore below and by the cashier's
   * own "Resume" on a flagged draft (`pendingDraft`).
   *
   * Paper mode restores by putting `paper=N` back in the address bar, never by
   * holding a state flag (`Billing.tsx`'s own paper mode is always re-derived
   * from the URL) - and only when it is still outstanding.
   *
   * A paper claim that can no longer be honoured refuses the **whole** restore
   * and returns false, rather than landing the cart and customer with a note
   * about the missing half (round-2 finding). The 2 Aug 2026 ruling is that
   * the snapshot restores as one or not at all, and this is the case where
   * the halves differ in kind: a bill saved as a re-entry of printed bill N,
   * landed without its paper claim, is no longer a re-entry at all - Save &
   * Print would draw it a fresh number and print a second receipt for a bill
   * the customer is already holding. There is nothing safe to keep, so the
   * cashier is told and the draft is left standing for them to discard.
   */
  const applyDraft = useCallback(
    (payload: DraftPayload, snapshotTill: TillSnapshot | null): boolean => {
      const claimsPaper = payload.paper !== null;
      if (
        claimsPaper &&
        outstandingPaperSeq(paperParams(payload.paper as number), snapshotTill) !== payload.paper
      ) {
        setNote(
          `That bill was a re-entry of printed bill ${payload.paper}, and the counter no longer ` +
            "regards that number as outstanding - so none of it has been restored. It cannot be " +
            "keyed in again under that number. Discard it, or check the number before starting over.",
        );
        return false;
      }
      runSeq.current += 1;
      onScreenRef.current = { cart: payload.cart, customer: payload.customer };
      setCart(payload.cart);
      const restoredReturn = foundFromExchange(payload.cart.exchange);
      setReturnFound(restoredReturn);
      setReturnPicked(pickedFromExchange(payload.cart.exchange));
      setReturnOutgoing(Boolean(payload.cart.exchange && payload.cart.lines.length));
      if (payload.cart.exchange) setMode("return");
      setCustomer(payload.customer);
      setUndoStack(emptyUndo());
      setPendingDraft(null);
      const next = new URLSearchParams(params);
      let replaceParams = false;
      if (payload.cart.exchange && params.get("mode") !== "return") {
        next.set("mode", "return");
        replaceParams = true;
      }
      if (claimsPaper) {
        setPaperAt(payload.paperAt ?? localNow());
        next.set("paper", String(payload.paper));
        replaceParams ||= params.get("paper") !== String(payload.paper);
      }
      if (replaceParams) setParams(next, { replace: true });
      return true;
    },
    [params, setParams],
  );

  /**
   * Reopening Billing after a crash, a closed tab or a power cut (#244).
   *
   * Read exactly once, guarded the way `useTillWorld` guards its own reload: a
   * read that loses the StrictMode/remount race is dropped rather than
   * applied. `restoredDraft` is the one decision over the whole snapshot - cart
   * lines, exchange legs and customer fields together (the 2 Aug 2026 ruling):
   * whichever the screen already has by the time the read lands (a piece
   * scanned or marked for return before it came back, in no
   * guaranteed order against this one) wins over the saved draft *whole*, so a
   * restore can never mix a real exchange with a crashed bill's customer.
   * `skipRestore` is the fourth guard: a New bill, a commit or a hold started
   * before this read landed means the cashier has already moved past what it
   * would restore.
   *
   * `engine.getSnapshot()` reads the counter's live state directly rather than
   * the `till` this component re-renders with - `till` only updates once React
   * gets round to it, which is not guaranteed to have happened yet at the exact
   * moment this read lands, and a paper check against a still-loading snapshot
   * would wrongly refuse a bill that is in fact still outstanding.
   *
   * `rekeyDraft` runs here, once, on the read itself - never inside a
   * `setCart` updater - so every restored line and leg gets a brand-new
   * internal key before anything on screen can touch it (the 2 Aug 2026
   * ruling): a saved key colliding with the next scan's would let one PIN
   * authorisation silently cover a line nobody approved.
   */
  useEffect(() => {
    if (!engine) return;
    let alive = true;
    void readDraft(engine.db)
      .then((draft) => {
        if (!alive || !draft || skipRestore.current) return;
        const fresh = rekeyDraft(draft);
        const snapshotTill = engine.getSnapshot();
        const decision = restoredDraft(
          onScreenRef.current,
          fresh,
          tillToday(),
          paperConsistent(fresh.paper, params, snapshotTill),
        );
        if (decision.kind === "apply") {
          applyDraft(decision.draft, snapshotTill);
        } else if (decision.kind !== "drop") {
          setPendingDraft({ draft: decision.draft, reason: decision.kind });
        }
      })
      .finally(() => {
        if (alive) setRestored(true);
      });
    return () => {
      alive = false;
    };
  }, [engine]);

  /** Is there a bill on this screen at all? Customer fields count: a name and
   *  a GSTIN typed before the first scan are as much a part of the snapshot as
   *  a line is (the 2 Aug 2026 atomic ruling), and a crash between the two
   *  would otherwise lose them. */
  const billStarted =
    cart.lines.length > 0 ||
    Boolean(cart.exchange) ||
    Boolean(customer.name || customer.mobile || customer.gstin);

  /**
   * Write-through: every cart or customer-field change lands here (#244).
   *
   * Held off until the mount read above has landed (see `restored`), and while
   * a flagged draft is still waiting on the cashier *and the counter is still
   * empty*: writing the empty screen through in that window would overwrite
   * the very row the notice is offering to resume before they get to answer
   * it.
   *
   * It deliberately does *not* stay held once they start billing (round-2
   * finding). The gate used to be `pendingDraft` alone, which made every
   * mutator clear the notice to get autosave back - so one scan silently
   * dropped a question the 2 Aug 2026 ruling says only Resume or Discard may
   * answer. Suspending on emptiness instead lets the notice stand while the
   * new bill is protected, and costs nothing: `pendingDraft` holds the payload
   * in memory, so Resume no longer depends on the stored row surviving.
   *
   * `persistDraft` never throws (flag, never block): a failed write is a
   * console line, and the bill carries on regardless.
   */
  useEffect(() => {
    if (!engine || !restored) return;
    if (pendingDraft && !billStarted) return;
    // An empty counter clears the row rather than writing an empty one.
    //
    // Both halves matter. Writing: this effect lands *after* the `clearDraft`
    // its caller fired in the same tick, so a bare `put` here would put the
    // row straight back and quietly defeat every commit, hold and New bill
    // (round-2 finding). Clearing: a cashier who deletes the last line by hand
    // takes no such path at all, and leaving the older row would offer to
    // restore the very lines they just took off.
    // Paper mode is no exception: a re-entry with nothing keyed into it yet is
    // still an empty counter, and its number is in the address bar rather than
    // the row. Exempting it left `newBill` writing a blank paper-claiming row
    // straight back over its own `clearDraft` - the same ping-pong, confined
    // to the one mode where a stray restore is most expensive.
    if (!billStarted) {
      void clearDraft(engine.db);
      return;
    }
    void persistDraft(engine.db, cart, customer, paper, paper === null ? null : paperAt);
  }, [engine, restored, pendingDraft, billStarted, cart, customer, paper, paperAt]);

  const leavePaperMode = useCallback(() => {
    if (!params.has("paper")) return;
    const next = new URLSearchParams(params);
    next.delete("paper");
    setParams(next, { replace: true });
  }, [params, setParams]);

  const today = useMemo(() => tillToday(), []);
  // The counter's remembered salesperson defaults a new bill only while they are
  // still on this store's staff list: somebody who has left or moved is not
  // stamped onto the next customer's lines (ticket 07).
  const remembered = lastPicked ?? world.lastSalesperson;
  const defaultSalesperson =
    remembered !== null && world.salespeople.some((person) => person.id === remembered)
      ? remembered
      : null;
  const soldBy = billSalesperson === undefined ? defaultSalesperson : billSalesperson;
  // A new bill inherits the counter's last actor once, then its bill-level
  // choice remains stable while a cashier assigns a different person per line.
  useEffect(() => {
    if (billSalesperson === undefined && defaultSalesperson !== null) {
      setBillSalesperson(defaultSalesperson);
    }
  }, [billSalesperson, defaultSalesperson]);
  const cartWithSoldBy = useMemo(() => inheritBillSalesperson(cart, soldBy), [cart, soldBy]);
  const differingSalespeople = cart.lines.filter(
    (line) => line.salesperson !== null && line.salesperson !== soldBy,
  ).length;
  // Which state this shop is registered in - the other half of every B2B tax
  // split. Null until the counter's identity has synced, and null is *not* a
  // state code: see `storeStateCodeOf` and `toDraft`.
  const storeState = storeStateCodeOf(world.store);
  // Which heads the tax breakup shows, derived exactly as `CustomerStrip` shows
  // it and `toDraft` sends it: a counter that has not learned its own state
  // raises no tax invoice at all, so there is nothing to split.
  const taxKind: B2bTaxKind = storeState === null ? "none" : taxKindFor(customer.gstin, storeState);
  const bill = useMemo(
    () =>
      priceCart(cartWithSoldBy, world, today, {
        capPercent: world.policy.manual_discount_cap_percent,
        allowManualDiscountOnOfferLines: world.policy.manual_discount_on_offer_lines,
      }),
    [cartWithSoldBy, world, today],
  );
  const lateAsks = useMemo(() => {
    const exchange = bill.exchange;
    if (
      !exchange?.billed_at ||
      !isPastReturnWindow(exchange.billed_at, world.policy.return_window_days)
    ) {
      return [];
    }
    return [lateReturnAsk(exchange)];
  }, [bill.exchange, world.policy.return_window_days]);
  // The counter's own refusals come first (#189): "this till does not know which
  // number it is on" is not something a cashier can fix by editing the cart, and
  // showing them a line-level complaint instead would send them looking at the
  // wrong thing.
  const returnBlocked =
    mode !== "return" || bill.exchange
      ? ""
      : !returnFound
        ? "Find the original bill before taking anything back."
        : "Mark at least one piece coming back.";
  // Ticket 05: what an offline counter must not do - a B2B bill, an exchange
  // against one, a credit note as payment - is refused before the bill is made,
  // in one sentence that says what to do instead. A bill keyed in from its
  // printed copy was made long ago and is never refused here.
  const offlineRefusal =
    till && paper === null
      ? whyOfflineRefuses({
          online: till.online,
          refusalsOn: till.onlineOnlyRefusals,
          // As `toDraft` sends it: a counter that does not know its own state
          // raises no tax invoice, so there is no GSTIN on the bill to refuse.
          buyerGstin: storeState === null ? "" : customer.gstin,
          originalBuyerGstin:
            (mode === "return" ? returnFound?.buyer_gstin : "") ||
            bill.exchange?.original_bill?.buyer_gstin ||
            "",
          tenderModes: toTenders(bill.split).map((tender) => tender.mode),
          // Ticket 20: collecting a reservation is online only.
          reservation: Boolean(bill.reservation),
          // Ticket 21: so is collecting a special order.
          specialOrder: Boolean(bill.special_order),
          // Ticket 19: and paying with a gift voucher.
          giftVoucher: bill.gift_vouchers.length > 0,
        })
      : "";
  // Before the return picker's own prompt: a cashier told to mark a piece
  // coming back would do so, only to be refused after.
  const blocked =
    till?.blocked ||
    offlineRefusal ||
    returnBlocked ||
    // Ticket 08: off, the till makes no split bills.
    whySplitIsOff(bill.lines, till?.splitSale ?? false) ||
    // Ticket 19: a voucher whose last day passed while the bill was open.
    // Asked of today's date now, not the page's: a counter left open past
    // midnight must not let yesterday's last day through.
    whyVouchersCannotPay(bill.gift_vouchers, tillToday()) ||
    whyItCannotClose(bill);

  // Ticket 09: the customer display. The till only ever sends it the view
  // `displayViewOf` builds - picked field by field from the bill - and never
  // waits on it: a display that is closed or broken changes nothing here.
  // Only the tab that holds the counter speaks to the display: a second tab of
  // this counter is blocked from billing, and its empty screen must not wipe
  // the live bill off the customer's display.
  const display = useTillDisplay(
    till?.customerDisplay && till.lockHeld && engine ? engine.storageScope : null,
  );
  const billOnCounter = cart.lines.length > 0 || Boolean(cart.exchange?.lines.length);
  useEffect(() => {
    if (billOnCounter) setThanking(false);
  }, [billOnCounter]);
  const upiOnDisplay = charging && bill.split.upi_paise > 0;
  const displayView = useMemo(
    () =>
      displayViewOf(bill, {
        justSaved: thanking,
        upi: upiOnDisplay ? { amount_paise: bill.split.upi_paise, qr: upiQr } : null,
      }),
    [bill, thanking, upiOnDisplay, upiQr],
  );
  useEffect(() => {
    display?.show(displayView);
  }, [display, displayView]);

  // Nothing may be typed into a bill while it is being committed: the cart is
  // read once inside `save`, and a line arriving after that read would be a
  // piece the customer paid for and the queue never heard of. Parking one is the
  // same read and the same hazard, one table down.
  const locked = saving || holding || Boolean(till?.onlinePending);

  // Quiet, and only once there is a bill on screen worth saving - an empty
  // counter has nothing autosave is protecting yet.
  const draftSaved = restored && billStarted;

  const suggestions = useMemo(
    () =>
      (mode === "sale" || (returnFound && returnOutgoing)) && typed.trim().length >= 2
        ? searchPieces(world.items, typed).map((piece) => ({
            piece,
            stock: world.stock.find((s) => s.barcode === piece.barcode)?.qty ?? 0,
          }))
        : [],
    [mode, returnFound, returnOutgoing, typed, world.items, world.stock],
  );
  const demoCodes = useMemo(
    () => [...new Set(world.items.map((item) => item.barcode))].slice(0, 4),
    [world.items],
  );

  /** Whether this counter makes a noise at all (#247, grill Q8) - set on Till &
   *  Sync, held in the counter's own database, so it is the same answer for
   *  whoever is standing here next. The fallback is for the type only: this
   *  component does not render without a counter (`BillingPage` narrows), and
   *  sound on is the shipped default either way. */
  const muted = till?.muted ?? false;

  /** Empty the scan box and put down whatever it was asking about.
   *
   *  The two always move together: `unknown` is a question *about* what is in the
   *  box, so a box that has been cleared and a question still on the screen is a
   *  cashier being asked about a barcode they can no longer see. */
  const clearScan = useCallback(() => {
    setTyped("");
    setUnknown("");
  }, []);

  /** Both alert channels start over at each of the three "next bill starts"
   *  moments - a piece scanned, an unknown taken onto the bill, or New bill
   *  pressed - so neither an old confirmation nor a stale print failure can
   *  outlive the bill it belonged to (see `pickBillAlert`'s note,
   *  Billing.tsx:1013). One named site, so a fourth such moment does not
   *  have to remember to copy the pair by hand (round-3 finding).
   *  `resumeHold` needs its own message rather than a blank `note`, so it
   *  keeps clearing `printProblem` on its own instead of calling this. */
  const startingANewBill = useCallback(() => {
    printRun.current += 1;
    setNote("");
    setPrintProblem("");
  }, []);

  /** Remember the cart as it stood before a mutator lands - one step for the
   *  Undo button (#244). */
  const pushCartUndo = useCallback((snapshot: Cart, run?: string) => {
    setUndoStack((stack) => pushUndo(stack, snapshot, run));
  }, []);

  const takePiece = useCallback(
    (piece: TillItem, alternatives: TillItem[], stock: number) => {
      if (locked) return;
      // `useCart`'s `take`, not a bare `setCart`: it chains through the
      // functional updater's own `current` rather than the outer `cart`
      // closure (#257, the #168 fix's own pattern - PR #314's
      // `addLineIfAbsent`). A wedge firing scans faster than React repaints
      // can call this handler again before the previous scan's `setCart` has
      // committed; reading the closure would then compute both lines from
      // the same pre-burst cart, and whichever `setCart` call React applies
      // last would win outright, silently dropping every line computed
      // before it. `current` is always whatever React actually has pending,
      // so the same race chains the two scans onto one another instead of
      // racing them. `scanPiece` inside `take` is also what turns a rescan
      // of a tag already on the bill into a qty bump rather than a duplicate
      // (#244).
      takeScanned(piece, { stock, alternatives }, soldBy, (next) => {
        onScreenRef.current = { ...onScreenRef.current, cart: next };
      });
      // The undo snapshot is one render behind under the same race - a burst
      // pushes the same pre-burst cart onto the stack more than once rather
      // than one entry per scan. Nothing is lost: an extra identical entry
      // just makes one Undo press pop what would otherwise have taken two,
      // which is the pre-existing, tolerable shape #244 already shipped -
      // #257 is about a scan never reaching the bill, not the undo stack's
      // granularity across one. Sourcing this from `onScreenRef.current.cart`
      // instead would only trade this for a worse bug: that mirror is not
      // kept in sync for `editLine`/`removeLeg`, so an edit sitting between
      // two scans would silently vanish under Undo.
      pushCartUndo(cart);
      startingANewBill();
      // The cashier is looking at the customer, not at the screen (#247, grill
      // Q8). A tag that belongs to two live seasons has landed a line but is
      // still asking which one (`SeasonCell`), and that is a buzz: the ruling is
      // about what still needs a person, not about whether a row appeared.
      playTone(toneForScan({ resolved: true, ambiguous: alternatives.length > 1 }), muted);
      // Picking a real piece answers the "was that tag mistyped?" ask - it was.
      clearScan();
    },
    [cart, clearScan, locked, muted, pushCartUndo, soldBy, startingANewBill, takeScanned],
  );

  /**
   * A garment the counter has never heard of, onto the bill anyway (#186).
   *
   * Grill Q5's whole point: the customer is holding it, so the scan finding
   * nothing is a thing the *shop* has to sort out, not a reason to send them
   * away. The line carries what the cashier types and what the tag says, and the
   * server parks its cost event until the paperwork lands.
   */
  const takeUnknown = useCallback(
    (code: string) => {
      if (locked) return;
      pushCartUndo(cart);
      // `addManualPiece` mints a key by default - computed once and reused
      // below for the same reason `takePiece` does: a second call would mint
      // a second, different key for what is meant to be the same line.
      const manualLine = { ...addManualPiece(code), salesperson: soldBy };
      setCart((current) => {
        const next = { ...current, lines: [...current.lines, manualLine] };
        onScreenRef.current = { ...onScreenRef.current, cart: next };
        return next;
      });
      startingANewBill();
      clearScan();
      scan.focus();
    },
    [cart, clearScan, locked, pushCartUndo, scan, soldBy, startingANewBill],
  );

  function changeMode(nextMode: CounterMode) {
    if (locked) return;
    returnRequests.current.invalidate();
    setMode(nextMode);
    setReturnLooking(false);
    setReturnError("");
    clearScan();
    const next = new URLSearchParams(params);
    if (nextMode === "return") next.set("mode", "return");
    else next.delete("mode");
    setParams(next, { replace: true });
    window.requestAnimationFrame(scan.focus);
  }

  async function pickRecentBill(match: ReturnBillMatch) {
    setReturnLooking(true);
    setReturnError("");
    try {
      const found = await resolveRecentMatch(match);
      const checked = engine?.db ? await withQueuedReturns(engine.db, found) : found;
      const refusal = whyNotReturnable(checked);
      if (refusal) {
        setReturnError(refusal);
        return;
      }
      setReturnFound(checked);
      setExchangeFromPicked(checked, {});
      setReturnSearchOpen(false);
    } catch (err) {
      setReturnError(apiErrorMessage(err));
    } finally {
      setReturnLooking(false);
    }
  }

  function setExchangeFromPicked(found: FoundBill, picked: PickedReturns) {
    const lines = legsFrom(found, picked);
    setReturnPicked(picked);
    setCart((current) => {
      const exchange: Exchange | null = lines.length
        ? {
            original: found.original,
            lines,
            billed_at: found.billed_at,
            original_bill: {
              lines: found.lines,
              billed_at: found.billed_at,
              customer_name: found.customer_name,
              customer_mobile: found.customer_mobile,
              buyer_gstin: found.buyer_gstin ?? "",
              net_paise: found.net_paise,
              local: found.local,
              // Ticket 13: what the bank paid of it (B60) and whose GSTIN it is.
              bank_offer_paise: found.bank_offer_paise ?? 0,
              store_gstin: found.store_gstin ?? "",
            },
          }
        : null;
      const next = { ...current, exchange };
      onScreenRef.current = { ...onScreenRef.current, cart: next };
      return next;
    });
  }

  /** Ticket 13: why this counter cannot take the found bill back, or "" -
   *  another GSTIN's bill (unless the setting allows it), a store with no GSTIN,
   *  or another store's bill. Only where the return tax switch is on (B5). */
  function whyNotReturnable(found: FoundBill): string {
    if (world.tax?.return_tax !== true) return "";
    const own = world.store?.gstin ?? "";
    const theirs = found.store_gstin || own;
    const code = gstinRefusal(theirs, own, optionsAt(world.tax, today));
    if (code) return refusalWords(code, { gstin: theirs, doc: found.original.doc_number });
    if (found.store_code && world.store && found.store_code !== world.store.code) {
      return (
        `Bill ${found.original.doc_number} was issued by ${found.store_code}. This counter takes ` +
        `back only ${world.store.code}'s own bills; take it to ${found.store_code}.`
      );
    }
    return "";
  }

  function loadReturnBill(found: FoundBill) {
    const refusal = whyNotReturnable(found);
    if (refusal) {
      setReturnLooking(false);
      setReturnError(refusal);
      return;
    }
    setReturnLooking(false);
    setReturnFound(found);
    setReturnPicked({});
    setReturnOutgoing(false);
    setReturnError("");
    setReturnCustomers([]);
    setReturnMatches(null);
    setReturnSearchOpen(false);
    clearScan();
    setCart((current) => {
      const next = { ...current, exchange: null, authorisation: null };
      onScreenRef.current = { ...onScreenRef.current, cart: next };
      return next;
    });
    window.requestAnimationFrame(scan.focus);
  }

  function changeReturnBill() {
    returnRequests.current.invalidate();
    setReturnLooking(false);
    setReturnFound(null);
    setReturnPicked({});
    setReturnOutgoing(false);
    setReturnError("");
    setReturnCustomers([]);
    setReturnMatches(null);
    setCart((current) => {
      const next = { ...current, exchange: null, authorisation: null };
      onScreenRef.current = { ...onScreenRef.current, cart: next };
      return next;
    });
    clearScan();
    window.requestAnimationFrame(scan.focus);
  }

  async function findReturnBill(query: string) {
    const isCurrent = returnRequests.current.start();
    const reference = query.trim();
    const seq = billSeqFrom(reference);
    if (!engine || !reference) {
      setReturnLooking(false);
      setReturnError("Type or scan the bill number from the customer's copy.");
      return;
    }
    setReturnLooking(true);
    setReturnError("");
    try {
      const fy = till?.register?.fy || (await engine.db.queue.orderBy("id").last())?.fy || "";
      if (!isCurrent()) return;
      // Queue first, then this device's cache of the store's recent bills
      // (OPS-09, PRD §10.3). The order is not an optimisation: a bill rung up ten
      // minutes ago exists only in the queue, and a bill from last week exists
      // only in the cache until the line is back.
      const exactLocal = await findHeldBillByDoc(engine.db, reference);
      if (!isCurrent()) return;
      // Ticket 13: a full number (anything with a "/": a bill key, or an invoice
      // number in the new series) is that bill - another store's, or another
      // year's - never this store's bill of this year with the same sequence.
      // Only a bare number ("74") falls back to the sequence.
      const fullNumber = world.tax?.return_tax === true && reference.includes("/");
      const local =
        exactLocal ??
        (fy && seq !== null && !fullNumber ? await findHeldBill(engine.db, fy, seq) : null);
      if (!isCurrent()) return;
      if (local) {
        loadReturnBill(local);
        return;
      }
      if (!till?.online) {
        // §10.4 and §10.6: an exchange offline works against a *cached* original
        // and nothing else. The till cannot know what has already been given back
        // off a bill it has never seen, and guessing is how one piece gets
        // returned twice.
        setReturnError(UNCACHED_ORIGINAL);
        return;
      }
      const { data: rows } = await typedApi.get("/sell/sales", {
        params: { doc: reference },
      });
      if (!isCurrent()) return;
      const matches = rows.flatMap((row) => (row.doc_number ? [row.doc_number] : []));
      if (!matches.length) {
        // Ticket 13: a bill another store issued is not in this store's list;
        // head office says where it can go back (never what is on it).
        const elsewhere = world.tax?.return_tax === true ? await whereReturnable(reference) : "";
        if (!isCurrent()) return;
        setReturnError(elsewhere || `No bill ${reference} at this store.`);
        return;
      }
      if (matches.length > 1) {
        setReturnError("More than one bill matches. Type or scan the full bill number.");
        return;
      }
      const matchedDoc = matches[0];
      if (!matchedDoc) {
        setReturnError("No bill number was returned for that reference.");
        return;
      }
      const found = await withStableQueue(engine.db, async () => {
        if (!isCurrent()) return null;
        const { data } = await typedApi.get(
          `/sell/sales/${encodeURIComponent(matchedDoc)}` as "/sell/sales/{doc_number}",
        );
        if (!isCurrent()) return null;
        return withQueuedReturns(engine.db, fromServer(data));
      });
      if (isCurrent() && found) loadReturnBill(found);
    } catch (error) {
      if (isCurrent()) setReturnError(apiErrorMessage(error));
    } finally {
      if (isCurrent()) setReturnLooking(false);
    }
  }

  /** Ticket 13: head office's words on where a bill of another store can be
   *  returned, or "" when it has none (no such bill, or the check failed). */
  async function whereReturnable(reference: string): Promise<string> {
    try {
      const { data } = await typedApi.get("/sell/return-where", { params: { doc: reference } });
      return data.refusal ? data.message : "";
    } catch {
      return "";
    }
  }

  /**
   * A bill named in the address bar - `/sell?mode=return&doc=…` (OPS-08).
   *
   * How Bills starts an exchange: the row a person is looking at hands the
   * counter the bill number, and the counter finds it exactly as it would if
   * somebody had scanned the customer's copy - the counter's own queue first,
   * then head office. The original is only ever *read*; what the exchange writes
   * is a new bill carrying the returned legs.
   *
   * Once per bill number, and never again: the lookup is a person's decision,
   * and one that re-ran on every render would throw away whatever they had
   * started marking.
   */
  const askedForDoc = useRef("");
  useEffect(() => {
    const doc = (params.get("doc") ?? "").trim();
    if (!doc || mode !== "return" || !engine || askedForDoc.current === doc) return;
    askedForDoc.current = doc;
    void findReturnBill(doc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, mode, engine]);

  function markReturnScan(code: string) {
    if (!returnFound) return;
    const marked = markReturnedPiece(returnFound, returnPicked, code);
    if (marked.error) {
      setReturnError(marked.error);
      playTone(toneForScan({ resolved: false, ambiguous: false }), muted);
      return;
    }
    setExchangeFromPicked(returnFound, marked.picked);
    setReturnError("");
    clearScan();
    playTone(toneForScan({ resolved: true, ambiguous: false }), muted);
  }

  function pickReturnLine(line: OriginalLine, patch: Partial<PickedReturn>) {
    const existing = returnPicked[line.line_no] ?? {
      qty: 0,
      reason: "",
      condition: "good" as const,
    };
    const nextChoice = { ...existing, ...patch };
    const next = { ...returnPicked };
    if (nextChoice.qty > 0) next[line.line_no] = nextChoice;
    else delete next[line.line_no];
    setExchangeFromPicked(returnFound!, next);
    scan.focus();
  }

  async function searchReturnCustomer(term = returnSearchTerm, key = returnSearchKey) {
    const isCurrent = returnRequests.current.start();
    if (!engine || !term.trim()) {
      setReturnLooking(false);
      return;
    }
    setReturnLooking(true);
    setReturnError("");
    try {
      // Three sources, in the order the counter can actually answer from
      // (OPS-09): this till's own queue, this till's cache of the store's recent
      // bills, and - only if there is a line - head office. The first two work
      // with no network at all, which is what makes an offline exchange possible.
      const [queued, cached, knownCustomers] = await Promise.all([
        searchQueuedBillsByCustomer(engine.db, key, term),
        searchCachedBillsByCustomer(engine.db, key, term),
        searchKnownCustomers(engine.db, key, term),
      ]);
      if (!isCurrent()) return;
      setReturnCustomers(knownCustomers);
      const byNumber = new Map<string, ReturnBillMatch>();
      for (const found of [...queued, ...cached]) {
        if (byNumber.has(found.original.doc_number)) continue;
        byNumber.set(found.original.doc_number, {
          doc_number: found.original.doc_number,
          billed_at: found.billed_at,
          customer_name: found.customer_name,
          customer_mobile: found.customer_mobile,
          net_paise: found.net_paise,
          local: found,
        });
      }
      setReturnMatches([...byNumber.values()]);
      if (!till?.online) return;
      try {
        const { data } = await typedApi.get("/sell/sales", {
          params: { [key]: term.trim() },
        });
        if (!isCurrent()) return;
        for (const row of data) {
          if (!row.doc_number || byNumber.has(row.doc_number)) continue;
          byNumber.set(row.doc_number, {
            doc_number: row.doc_number,
            billed_at: row.billed_at,
            customer_name: row.customer_name ?? "",
            customer_mobile: row.customer_mobile ?? "",
            net_paise: row.net_paise ?? 0,
            local: null,
          });
        }
        setReturnMatches([...byNumber.values()]);
      } catch (error) {
        if (!isCurrent()) return;
        setReturnError(
          byNumber.size || knownCustomers.length
            ? "Head-office bill history could not be reached; results on this till are still shown."
            : apiErrorMessage(error),
        );
      }
    } catch (error) {
      if (isCurrent()) {
        setReturnError(apiErrorMessage(error));
        setReturnCustomers([]);
        setReturnMatches(null);
      }
    } finally {
      if (isCurrent()) setReturnLooking(false);
    }
  }

  async function openReturnMatch(match: ReturnBillMatch) {
    const isCurrent = returnRequests.current.start();
    if (!engine) return;
    if (match.local) {
      loadReturnBill(match.local);
      return;
    }
    setReturnLooking(true);
    setReturnError("");
    try {
      const found = await withStableQueue(engine.db, async () => {
        if (!isCurrent()) return null;
        const { data } = await typedApi.get(
          `/sell/sales/${encodeURIComponent(match.doc_number)}` as "/sell/sales/{doc_number}",
        );
        if (!isCurrent()) return null;
        return withQueuedReturns(engine.db, fromServer(data));
      });
      if (isCurrent() && found) loadReturnBill(found);
    } catch (error) {
      if (isCurrent()) setReturnError(apiErrorMessage(error));
    } finally {
      if (isCurrent()) setReturnLooking(false);
    }
  }

  function applyCounterScan(code: string) {
    if (mode === "sale") {
      applyScan(code);
      return;
    }
    if (!returnFound) {
      void findReturnBill(code);
      return;
    }
    if (returnOutgoing) applyScan(code);
    else markReturnScan(code);
  }

  const applyScan = useCallback(
    (code: string) => {
      const found = resolveScan(code, world);
      if (!found.barcode) return;
      if (!found.chosen) {
        // A2 / grill Q5: the customer is holding the garment, so this is a
        // sentence and an offer rather than a refusal. The suggestion list is
        // still worth a look first - the commonest reason a tag does not
        // resolve is that it was mistyped, not that the piece is new.
        playTone(toneForScan({ resolved: false, ambiguous: false }), muted);
        setUnknown(found.barcode);
        setTyped(code.trim());
        return;
      }
      takePiece(found.chosen, found.candidates, found.stock);
    },
    [muted, takePiece, world],
  );

  function editLine(key: string, patch: Partial<CartLine>) {
    if (locked) return;
    // One line's one field is one undo step, however many keystrokes it took:
    // the grid's cells fire this on every character (round-2 finding).
    //
    // `runSeq` is what ends a run. Coalescing looks only at the top of the
    // stack, so without it a qty edit, then something that pushes nothing
    // (a payment or customer field, an Undo), then a *second* qty edit on the
    // same line would fold into the first and lose the figure in between.
    // Anything that is not more typing in this same box bumps the counter, and
    // the next edit starts a step of its own.
    pushCartUndo(cart, `${key}:${Object.keys(patch).sort().join(",")}#${runSeq.current}`);
    setCart((current) => ({
      ...current,
      lines: current.lines.map((line) => (line.key === key ? { ...line, ...patch } : line)),
    }));
  }

  /**
   * Crediting the sale, and the cursor going home afterwards.
   *
   * Picking from a `<select>` leaves the keyboard inside it, so the next scan
   * would type the barcode into the dropdown. The focus patrol deliberately
   * will not take focus off anything a person can type into, so the screen has
   * to hand it back at the moment the picking is done (AC 2).
   */
  function pickSalesperson(key: string, salesperson: string | null) {
    editLine(key, { salesperson });
    setLastPicked(salesperson);
    if (salesperson != null) void engine?.rememberSalesperson(salesperson);
    scan.focus();
  }

  function pickBillSalesperson(salesperson: string | null) {
    if (locked) return;
    setBillSalesperson(salesperson);
    setLastPicked(salesperson);
    if (salesperson !== null) void engine?.rememberSalesperson(salesperson);
    scan.focus();
  }

  function applyBillSalesperson() {
    if (locked) return;
    if (soldBy === null) return;
    runSeq.current += 1;
    setCart((current) => ({
      ...current,
      lines: current.lines.map((line) => ({ ...line, salesperson: soldBy })),
    }));
    scan.focus();
  }

  /** Take a piece back off the bill - the customer changed their mind about
   *  giving it back, or the wrong line was picked. */
  function removeLeg(key: string) {
    pushCartUndo(cart);
    setCart((current) => {
      const legs = (current.exchange?.lines ?? []).filter((leg) => leg.key !== key);
      return {
        ...current,
        // An exchange with no legs left is not an exchange: keeping the empty
        // shell would leave the bill pointing at an original it gives nothing
        // back against, and refusing to close (`whyExchangeCannotClose`).
        exchange: legs.length && current.exchange ? { ...current.exchange, lines: legs } : null,
      };
    });
    scan.focus();
  }

  /** Ticket 20: a reservation's pieces onto this bill, as head office holds
   *  them, with its advance to pay towards it and its customer on the bill. */
  function collectReservation(r: Reservation) {
    pushCartUndo(cart);
    runSeq.current += 1;
    setCart((current) => ({
      ...current,
      lines: [
        ...current.lines,
        ...r.pieces.map((p) => ({
          ...addPiece(p.item as TillItem, { stock: 0, alternatives: [] }),
          qty: p.qty,
        })),
      ],
      reservation: { id: r.id, ref: r.ref, advance_paise: r.advance_balance_paise },
    }));
    editCustomer({ ...customer, name: r.customer_name, mobile: r.customer_mobile });
    scan.focus();
  }

  /** Ticket 22: a paid alteration's own line, sold by whoever sells the bill. */
  function addAlteration(chargePaise: number) {
    const charge = till?.alterationCharge;
    if (!charge) return;
    pushCartUndo(cart);
    runSeq.current += 1;
    setCart((current) => ({
      ...current,
      lines: [...current.lines, addAlterationCharge(charge, chargePaise, soldBy)],
    }));
    scan.focus();
  }

  /** Ticket 20: the reservation off this bill; its pieces stay as scanned lines. */
  function clearReservation() {
    pushCartUndo(cart);
    setCart((current) => ({ ...current, reservation: null }));
    scan.focus();
  }

  /** Ticket 21: a special order onto this bill - its advance to pay towards
   *  it, its customer on the bill, and the piece that arrived scanned on. */
  function collectSpecialOrder(o: SpecialOrder) {
    pushCartUndo(cart);
    runSeq.current += 1;
    setCart((current) => ({
      ...current,
      special_order: { id: o.id, ref: o.ref, advance_paise: o.advance_balance_paise },
    }));
    editCustomer({ ...customer, name: o.customer_name, mobile: o.customer_mobile });
    // The piece that arrived, scanned on unless the cashier already did.
    if (!cart.lines.some((line) => line.barcode === o.arrived_barcode))
      applyScan(o.arrived_barcode);
    scan.focus();
  }

  /** Ticket 19: a gift voucher head office answered for, to pay towards this bill. */
  function takeGiftVoucher(voucher: GiftVoucherHeld) {
    pushCartUndo(cart);
    setCart((current) => ({
      ...current,
      gift_vouchers: [...(current.gift_vouchers ?? []), voucher],
    }));
    scan.focus();
  }

  /** Ticket 19: a gift voucher off this bill; nothing is spent from it. */
  function removeGiftVoucher(number: string) {
    pushCartUndo(cart);
    setCart((current) => ({
      ...current,
      gift_vouchers: (current.gift_vouchers ?? []).filter((v) => v.number !== number),
    }));
    scan.focus();
  }

  /** Ticket 21: the special order off this bill; its piece stays as a scanned line. */
  function clearSpecialOrder() {
    pushCartUndo(cart);
    setCart((current) => ({ ...current, special_order: null }));
    scan.focus();
  }

  function removeLine(key: string) {
    pushCartUndo(cart);
    setCart((current) => ({ ...current, lines: current.lines.filter((l) => l.key !== key) }));
    scan.focus();
  }

  /** Step the bill back one action (#244). Nothing here touches stock or
   *  money - the whole safety of it is that this bill is not real until Save
   *  & Print. */
  function undo() {
    const popped = popUndo(undoStack);
    if (!popped) return;
    runSeq.current += 1;
    setUndoStack(popped.stack);
    setCart(popped.cart);
    scan.focus();
  }

  /**
   * One keystroke, one chip, one "All cash" - every change to how this bill is
   * being paid comes through here.
   *
   * The patch lands on `bill.payment` rather than on `cart.payment`, and that is
   * the whole of PRD §9.1's "every cart or offer change revalidates the split":
   * `priceCart` has already re-read the allocation against what the bill now
   * comes to, and an allocation about a stale amount has already been cleared
   * there. Patching the cart's own copy would put the stale figures straight
   * back. Every edit re-stamps the due it was made against, so the next
   * repricing can tell whether it still answers the bill.
   */
  function editPayment(patch: Partial<Payment>) {
    runSeq.current += 1;
    setCart((current) => ({
      ...current,
      // What the customer pays: the bill less any bank offer (ticket 11).
      payment: { ...bill.payment, ...patch, due_paise: bill.payable_paise },
    }));
  }

  /**
   * The bank answered a UPI charge (#248) - the one thing that puts a
   * `confirmed` stamp on a bill.
   *
   * Unreachable on this build by construction: the mock adapter cannot emit
   * success (`till/payment.ts`), so the till only ever sends `manual`. It is
   * written now because the hardware slice must be able to land without
   * touching this screen.
   *
   * `useCallback` because `UpiCharge` restarts its charge when this changes,
   * and a fresh closure on every render would regenerate the QR under a
   * customer trying to scan it.
   */
  const upiConfirmed = useCallback((charged: UpiCharged) => {
    runSeq.current += 1;
    setCart((current) => ({
      ...current,
      // The charged figure, not the typed one. They are the same figure - the
      // card charges what the row says - but pinning the stamp's own amount
      // into the row is what keeps `confirmedUpiOf` honest if they ever drift.
      payment: {
        ...current.payment,
        upi_paise: charged.amount_paise,
        upi_charge: charged,
      },
    }));
    // The card deliberately stays open on a success. It is the one state that
    // carries a fact the cashier may need to read out - the acquirer's
    // reference - and a card that closed itself would flash it past them; it is
    // also the only state that would otherwise never be on screen at all, which
    // would leave the hardware slice as the first thing ever to render it.
    setNote(`The bank confirmed the UPI payment. Reference ${charged.reference}.`);
  }, []);

  /** The customer strip, typed into directly - the third source (with a scan
   *  and return picking) that can race the mount-time draft read. */
  function editCustomer(next: TillCustomer) {
    if (locked) return;
    runSeq.current += 1;
    onScreenRef.current = { ...onScreenRef.current, customer: next };
    setCustomer(next);
  }

  /** The cashier's explicit answer to a flagged draft (#244) - a previous
   *  business day, or a paper number the counter no longer holds. Resuming
   *  reprices at today's rules the moment it lands (`priceCart` always reprices
   *  from `today`, binding rule 9); nothing here is a hold, so no repricing
   *  machinery of its own is needed. */
  function resumePendingDraft() {
    if (!pendingDraft || !engine) return;
    // Never over a bill already in progress. The notice now stands until it is
    // answered rather than vanishing on the first scan (round-2 finding), so
    // for the first time Resume can be pressed with a real cart on screen -
    // and `applyDraft` replaces cart and customer wholesale and empties the
    // undo stack, which would take a bill away from in front of a customer
    // with no way back. Finishing or clearing what is on the counter is the
    // cashier's own decision, so this asks rather than choosing for them.
    if (billStarted) {
      setNote(
        "Finish or clear the bill on the counter first - resuming the saved one would replace it.",
      );
      return;
    }
    applyDraft(pendingDraft.draft, engine.getSnapshot());
    scan.focus();
  }

  /** Let a flagged draft go without resuming it. */
  function discardPendingDraft() {
    setPendingDraft(null);
    if (engine) void clearDraft(engine.db);
    scan.focus();
  }

  /**
   * Put an empty counter on screen: the state half of every "this bill is
   * over" moment - New bill, a hold parked, a bill committed.
   *
   * One named site because it is five statements that must not drift apart,
   * and they already had (round-2 finding). `resumeHold` is the near miss that
   * makes the point: it lands a *different* cart rather than an empty one, so
   * it could not call this - and being spelled by hand it was missing both
   * `skipRestore` and the emptied undo stack, which is a crashed draft landing
   * on a released hold and a previous customer's lines one Undo away.
   *
   * The draft row is each caller's own business: `newBill` can fire and forget
   * it, while `holdBill` and the commit path must await it inside their own
   * error handling.
   */
  function freshCounter() {
    returnRequests.current.invalidate();
    skipRestore.current = true;
    runSeq.current += 1;
    onScreenRef.current = { cart: emptyCart(), customer: NO_CUSTOMER };
    setCart(emptyCart());
    setCustomer(NO_CUSTOMER);
    setUndoStack(emptyUndo());
    setPendingDraft(null);
    setBillSalesperson(undefined);
    // A charge card left open over a bill that no longer exists would be
    // charging a figure nobody is being asked for.
    setCharging(false);
    setUpiQr("");
    setReturnFound(null);
    setReturnPicked({});
    setReturnOutgoing(false);
    setReturnLooking(false);
    setReturnError("");
    setReturnCustomers([]);
    setReturnMatches(null);
    setReturnSearchOpen(false);
    clearScan();
  }

  function newBill() {
    if (till?.onlinePending) return;
    setThanking(false);
    freshCounter();
    startingANewBill();
    if (engine) void clearDraft(engine.db);
    scan.focus();
  }

  /** Ticket 09: open (or bring back) the customer display window. */
  function openCustomerDisplay() {
    if (!engine) return;
    const opened = window.open(
      displayUrl(engine.storeCode),
      "kdps-customer-display",
      "popup=yes,width=1024,height=700",
    );
    if (!opened) {
      setNote(
        "The browser blocked the customer display window. Allow pop-ups for this site, " +
          "then press Customer display again.",
      );
    }
    scan.focus();
  }

  function nextBill() {
    freshCounter();
    startingANewBill();
    setFinishOpen(false);
    window.requestAnimationFrame(scan.focus);
  }

  // --- bills on hold (#185, grill Q13) --------------------------------------

  const holds = till?.held ?? [];
  // Recomputed against the list rather than stored: "before today" is a fact
  // about the clock, and a flag written at park time would be wrong by morning.
  const toReview = useMemo(() => holdsToReview(holds), [holds]);
  // Two reasons a parked bill may not be picked up yet, and both would be silent
  // damage rather than an error:
  //
  //   · an open cart would be thrown away by the one that replaces it;
  //   · a counter whose copy has not loaded would reprice every line against an
  //     empty world - which is to say against the prices the hold was parked at -
  //     while the hold itself disappeared. That is the exact thing grill Q13's
  //     "reprices at that day's offers" forbids, and it is reachable: the
  //     Dashboard's `?holds=1` opens this list at mount, before the world lands.
  const holdsBlocked = !world.loaded
    ? "Opening the counter… a parked bill is priced at today's rates, so it waits for the price list."
    : cart.lines.length
      ? "Save, hold or clear the bill on screen before picking up another."
      : "";

  /**
   * Park the bill and clear the counter for the next customer.
   *
   * One tap, and the customer's name is the label when the strip has one -
   * "label optional" (grill Q13) with nothing extra for a cashier to fill in. A
   * hold with no name identifies itself by what is in it.
   *
   * The screen locks while it writes, for the same reason Save & Print does: the
   * cart is read once, and a piece scanned after that read is a piece that is
   * neither on the hold nor on the screen.
   */
  async function holdBill() {
    if (!engine || !cart.lines.length || locked) return;
    setHolding(true);
    try {
      await engine.hold({
        held_uuid: newUuid(),
        label: customer.name.trim(),
        payload: heldPayload(cartWithSoldBy, customer, {
          net_paise: bill.net_paise,
          pieces: bill.pieces,
        }),
      });
      freshCounter();
      await clearDraft(engine.db);
      setNote("Bill held. Scan the next customer's first piece.");
      setShowHolds(false);
    } catch (error) {
      setNote(messageOf(error));
    } finally {
      setHolding(false);
      scan.focus();
    }
  }

  /** Pick a parked bill back up, at today's prices. */
  async function resumeHold(hold: HeldBill) {
    if (!engine || holdsBlocked) return;
    const picked = restoreHold(hold, world);
    // A hold landing on the counter is a bill starting, so it takes the same
    // three guards every other such moment takes (round-2 finding - this was
    // the one path with none of them).
    //
    // `skipRestore` + the `onScreenRef` mirror, because this is a fourth
    // source that can race the mount-time draft read: without them a hold
    // resumed before that read lands is overwritten by the crashed draft,
    // with the hold already released - the atomic-restore failure the 2 Aug
    // 2026 ruling forbids, in the one shape where the evidence is gone too.
    //
    // `emptyUndo`, because the undo stack belongs to the bill that built it:
    // popping it onto a resumed hold would put the previous customer's lines
    // and prices on a bill that is about to be printed and posted.
    skipRestore.current = true;
    runSeq.current += 1;
    onScreenRef.current = { cart: picked.cart, customer: picked.customer };
    setCart(picked.cart);
    setCustomer(picked.customer);
    setUndoStack(emptyUndo());
    setPendingDraft(null);
    setShowHolds(false);
    // A held bill can carry lines of its own, so it can put the cart into a
    // state `holdBill` will happily hold again without a scan ever running
    // `takePiece`/`takeUnknown` - this is the other "next bill starts" moment
    // a stale print problem must not survive (round-2 finding: Billing.tsx:1013).
    setPrintProblem("");
    setNote(
      picked.staleLines
        ? "Bill picked up. Priced at today's rates - check the lines the counter no longer stocks."
        : "Bill picked up, priced at today's rates.",
    );
    try {
      // The hold goes only after the cart is on screen: if this throws, the cart
      // is in front of the cashier and the hold is still in the list, which is a
      // duplicate somebody can see rather than a bill nobody has.
      await engine.releaseHold(hold.held_uuid);
    } catch (error) {
      setNote(`Bill picked up, but it is still on the hold list: ${messageOf(error)}`);
    }
    scan.focus();
  }

  /** A hold answered at day close - kept for tomorrow, or let go. */
  async function answerHold(work: Promise<void>) {
    try {
      await work;
    } catch (error) {
      setNote(messageOf(error));
    }
  }

  async function print(receipt: string, documentNumber?: string): Promise<void> {
    const run = ++printRun.current;
    if (till?.onlineAlpha && documentNumber) {
      // Re-authorise the exact issued document at delivery, including reprints.
      try {
        const { data } = await api.get(`/sell/sales/${encodeURIComponent(documentNumber)}`);
        receipt = postedReceiptHtml(data);
      } catch (error) {
        setPrintProblem(apiErrorMessage(error));
        return;
      }
    }
    if (run !== printRun.current) return;
    const outcome = await browserPrintAdapter.print(receipt);
    if (run !== printRun.current) return;
    setPrintProblem(
      outcome.ok
        ? ""
        : `${outcome.reason} The bill is saved - press Reprint when the printer is ready.`,
    );
  }

  /**
   * Save & Print.
   *
   * The order is the whole of grill Q2: commit first, print second. If printing
   * throws, the sale is already a numbered row in a durable queue and the
   * customer's money is accounted for; if it were the other way round a printer
   * fault would leave a printed receipt with no bill behind it.
   *
   * A paper re-entry takes the same path and stops before the printer (#189).
   * The receipt for that bill is already in a customer's hand, and printing a
   * second copy of a bill that is being keyed in from the first is how one sale
   * ends up looking like two on a shop floor.
   */
  async function save(authorisation: Authorisation | null = cart.authorisation) {
    if (!engine || blocked || saving || till?.onlinePending) return;
    let refocusScan = true;
    setSaving(true);
    setPrintProblem("");
    try {
      const billToSave =
        authorisation === cart.authorisation
          ? bill
          : priceCart({ ...cartWithSoldBy, authorisation }, world, today, {
              capPercent: world.policy.manual_discount_cap_percent,
              allowManualDiscountOnOfferLines: world.policy.manual_discount_on_offer_lines,
            });
      const billedAt = paper === null ? new Date().toISOString() : new Date(paperAt).toISOString();
      const draft = toDraft(billToSave, { billedAt, customer, storeStateCode: storeState });
      const queued =
        paper === null
          ? await engine.commit(draft, {
              originalBuyerGstin: billToSave.exchange?.original_bill?.buyer_gstin ?? "",
            })
          : await engine.reenterFromPaper(draft, paper);
      setCommits((n) => n + 1);
      const receipt = receiptHtml(queued, world.store ?? FALLBACK_STORE, {
        ...(storeName ? { storeName } : {}),
        cashReceivedPaise: billToSave.split.cash_received_paise,
        describe: describeFrom(billToSave.lines),
      });
      setLastBill({
        bill: queued,
        receipt,
        cashReceivedPaise: billToSave.split.cash_received_paise,
      });
      freshCounter();
      await clearDraft(engine.db);
      if (paper === null) {
        setThanking(true);
        setNote(`Bill ${queued.doc_number} saved.`);
        setFinishOpen(true);
        refocusScan = false;
        await print(receipt, queued.doc_number);
      } else {
        leavePaperMode();
        setNote(
          `Bill ${queued.doc_number} entered from its printed copy. ` +
            "It is on the list to sync, and the customer keeps the receipt they have.",
        );
      }
    } catch (error) {
      setNote(messageOf(error));
    } finally {
      setSaving(false);
      if (refocusScan) scan.focus();
    }
  }

  async function recoverOnlineBill() {
    if (!engine || saving) return;
    setSaving(true);
    try {
      const queued = await engine.retryOnline();
      const receipt = receiptHtml(queued, world.store ?? FALLBACK_STORE, {
        ...(storeName ? { storeName } : {}),
      });
      setLastBill({ bill: queued, receipt, cashReceivedPaise: queued.cash_received_paise ?? null });
      freshCounter();
      await clearDraft(engine.db);
      setThanking(true);
      setNote(`Bill ${queued.doc_number} accepted.`);
      setFinishOpen(true);
      await print(receipt, queued.doc_number);
    } catch (error) {
      setNote(messageOf(error));
    } finally {
      setSaving(false);
    }
  }

  function trySave() {
    if (blocked || saving || till?.onlinePending) return;
    if (till?.onlineAlpha && lateAsks.length) {
      setNote(
        "This late return needs recorded review by another independently authorised person. Ask Owner or Admin to arrange approval before taking payment.",
      );
      return;
    }
    if (lateAsks.length && !covers(cart.authorisation, lateAsks)) {
      setReturnAsking(lateAsks);
      return;
    }
    void save();
  }

  // Which one banner the frame's alert strip shows (#243) - see `pickBillAlert`.
  // `blocked` never wins a line here: a blocked counter takes over the whole
  // work area below instead, which is the stronger treatment Rule 5 asks for.
  // `paper` is not a flag here either, for the same reason: see the block
  // below, which renders off `paper !== null` directly.
  const alert = pickBillAlert({
    blocked: Boolean(till?.blocked),
    loading: !world.loaded,
    noPriceList: world.loaded && !world.items.length,
    printProblem: Boolean(printProblem),
    note: Boolean(note),
    gift: bill.entitlements.length > 0,
    holdsDue: toReview.length > 0,
  });

  // Shared by the three lifecycle buttons below - one call site instead of
  // three identical ones, mirroring `alert`'s own `blocked: Boolean(till?.blocked)`
  // just above.
  const counterBlocked = Boolean(till?.blocked);

  /** Dismissing the scan box's floating prompts (G-4) is the same act as
   *  answering "not that" by hand: put down the question and give the cursor
   *  back, whether the counter did it by clicking outside, pressing Escape, or
   *  the button inside `NotInSystem`. */
  const closeScanFloat = useCallback(() => {
    clearScan();
    scan.focus();
  }, [clearScan, scan]);

  const dismissCounterError = useCallback(() => {
    returnRequests.current.invalidate();
    setReturnLooking(false);
    setNote("");
    setPrintProblem("");
    setReturnError("");
    closeScanFloat();
  }, [closeScanFloat]);

  const closeReturnSearch = useCallback(() => {
    returnRequests.current.invalidate();
    setReturnLooking(false);
    setReturnSearchOpen(false);
    scan.focus();
  }, [scan]);

  function openLookup() {
    if (mode === "return") {
      returnRequests.current.invalidate();
      setReturnLooking(false);
      setReturnSearchOpen(true);
      setReturnError("");
      return;
    }
    navigate("/sell/customers");
  }

  useCounterKeys({
    disabled: Boolean(charging || showHolds || counterBlocked || locked || returnAsking),
    finishOpen,
    onHold: () => void holdBill(),
    onLookup: openLookup,
    onNewBill: newBill,
    onSave: trySave,
    onBackToScan: dismissCounterError,
    onNextBill: nextBill,
  });

  // "Did you mean" and "bill it off the tag" both hang off the scan box as one
  // floating panel (G-4: "nothing pushes the layout"), portaled out of the
  // work area so neither can push the rail or the footer. `usePositionedPopover`
  // already does measure/re-measure/clamp/outside-click - the scan box just
  // needed the hook to accept an `<input>` and to hang the panel below rather
  // than beside it, which is what the `<HTMLInputElement>` and `"below"` here
  // are for.
  const scanFloat = usePositionedPopover<HTMLInputElement>(
    unknown || suggestions.length > 0 ? "scan" : null,
    closeScanFloat,
    SCAN_FLOAT_WIDTH,
    "below",
  );
  const returnSearchFloat = usePositionedPopover<HTMLInputElement>(
    mode === "return" && returnSearchOpen ? "return-customer" : null,
    closeReturnSearch,
    680,
    "below",
  );

  const returnReady = mode !== "return" || returnFound !== null;

  return (
    <div className="bill-page" data-mode={mode}>
      {/* BillBar spans the full width of the counter frame (grid-column: 1 / -1). */}
      <BillBar
        mode={mode}
        nextNumber={till?.nextNumber ?? ""}
        draftSaved={draftSaved}
        salespeople={world.salespeople}
        soldBy={soldBy}
        differingLines={differingSalespeople}
        heldCount={holds.length}
        showingHolds={showHolds}
        disabled={counterBlocked || locked}
        canHold={cart.lines.length > 0}
        onModeChange={changeMode}
        onSoldByChange={pickBillSalesperson}
        onApplySoldBy={applyBillSalesperson}
        onShowHolds={() => setShowHolds((open) => !open)}
        onHold={() => void holdBill()}
        onNewBill={newBill}
        extraActions={
          display && (
            <button
              type="button"
              className="btn"
              data-testid="bill-customer-display"
              title={
                till?.online
                  ? "Open the customer display in a second window"
                  : "The customer display needs the internet to open. Once open, it keeps working offline."
              }
              disabled={!till?.online}
              onClick={openCustomerDisplay}
            >
              <MonitorSmartphone size={15} /> Customer display
            </button>
          )
        }
      />

      {/* Work area: left column (grid-column: 1). Only .bill-lines scrolls. */}
      <section className="bill-lines">
        <div className="bill-top" aria-hidden={finishOpen || undefined}>
          <ScanHero
            mode={mode}
            boxRef={mergeRefs(scan.ref, scanFloat.triggerRef, returnSearchFloat.triggerRef)}
            value={typed}
            disabled={locked || counterBlocked || finishOpen || returnLooking}
            placeholder={
              mode === "sale" || returnOutgoing
                ? "Scan a tag, or type a design number"
                : returnFound
                  ? "Scan a piece coming back"
                  : "Scan or type the full bill number"
            }
            returnStage={!returnFound ? "bill" : returnOutgoing ? "exchange" : "return"}
            hasError={
              mode === "return" && !returnOutgoing ? Boolean(returnError) : Boolean(unknown)
            }
            errorBarcode={mode === "return" && !returnOutgoing ? typed.trim() : unknown}
            {...(mode === "return" && !returnOutgoing ? { errorMessage: returnError } : {})}
            demoCodes={mode === "sale" || returnOutgoing ? demoCodes : []}
            onChange={setTyped}
            onSubmit={applyCounterScan}
            onLookup={openLookup}
            onDismissError={dismissCounterError}
            onBillOffTag={() => unknown && takeUnknown(unknown)}
            onDemoScan={(code) => {
              applyCounterScan(code);
              scan.focus();
            }}
          />

          {paper !== null && (
            <div className="bill-paper" data-testid="bill-paper">
              <div>
                <strong>Entering printed bill {paper}</strong> - this one was rung up on the machine
                this counter replaced and never reached head office. Enter it exactly as the printed
                copy reads. It keeps its own number, and nothing prints.
              </div>
              <div className="field">
                <label htmlFor="bill-paper-at">Date and time on the printed copy</label>
                <input
                  id="bill-paper-at"
                  className="input"
                  data-testid="bill-paper-at"
                  type="datetime-local"
                  disabled={locked}
                  value={paperAt}
                  onChange={(e) => setPaperAt(e.target.value)}
                />
              </div>
              <button
                type="button"
                className="btn"
                data-testid="bill-paper-cancel"
                disabled={locked}
                onClick={leavePaperMode}
              >
                <X size={15} /> Not this one
              </button>
            </div>
          )}

          {pendingDraft && (
            <div className="bill-alert bill-pending-draft" data-testid="bill-pending-draft">
              <AlertTriangle size={15} />
              <span>
                {pendingDraft.reason === "stale"
                  ? "A bill was left in progress on a previous business day."
                  : pendingDraft.draft.paper !== null
                    ? `A bill was left in progress mid paper re-entry (bill ${pendingDraft.draft.paper}), ` +
                      "which the counter no longer matches to what is open now."
                    : "A bill was left in progress while the counter was mid a paper re-entry."}{" "}
                Resume it, or start fresh.
              </span>
              <span className="bill-pending-draft-actions">
                <button
                  type="button"
                  className="btn"
                  data-testid="bill-pending-draft-resume"
                  onClick={resumePendingDraft}
                >
                  Resume
                </button>
                <button
                  type="button"
                  className="btn"
                  data-testid="bill-pending-draft-discard"
                  onClick={discardPendingDraft}
                >
                  Discard
                </button>
              </span>
            </div>
          )}

          {alert === "loading" && <p className="warn-note">Opening the counter…</p>}
          {alert === "no-price-list" && (
            <p className="warn-note" data-testid="bill-no-price-list">
              This counter has no local price list yet. Sync from Till &amp; Sync before billing.
            </p>
          )}
          {till?.onlinePending && (
            <section className="card" role="status" data-testid="online-sale-pending">
              <strong>
                {till.onlinePending.state === "rejected"
                  ? "Bill not issued"
                  : "Checking whether the sale completed"}
              </strong>
              <p>
                {till.onlinePending.error || "Keep this submission. Do not collect payment again."}
              </p>
              {till.onlinePending.state === "rejected" ? (
                <button
                  className="btn"
                  onClick={() =>
                    void engine!
                      .reviseOnline()
                      .then(() =>
                        setNote("Prices refreshed. Review the bill before submitting again."),
                      )
                      .catch((error) => setNote(messageOf(error)))
                  }
                >
                  Review and revise bill
                </button>
              ) : (
                <button
                  className="btn btn-primary"
                  disabled={saving || !till.online}
                  onClick={() => void recoverOnlineBill()}
                >
                  Retry same submission
                </button>
              )}
            </section>
          )}
          {alert === "print-problem" && (
            <p className="bill-alert" data-testid="bill-print-problem">
              <AlertTriangle size={15} />
              {printProblem}
            </p>
          )}
          {alert === "note" && (
            <p className="ok-note" data-testid="bill-note">
              {note}
            </p>
          )}
          {alert === "gift" &&
            bill.entitlements.map((gift) => (
              <p className="ok-note" data-testid={`bill-gift-${gift.offer_id}`} key={gift.offer_id}>
                <Gift size={15} /> This bill earns a gift: {gift.offer_name}. Scan it onto the bill
                {gift.token_price_paise > 0 ? (
                  <>
                    {" "}
                    at its token price of <Money paise={gift.token_price_paise} />
                  </>
                ) : (
                  " free of charge"
                )}
                , and hand it over.
              </p>
            ))}
          {alert === "holds-due" && (
            <p className="bill-alert" data-testid="bill-holds-due">
              <AlertTriangle size={15} />
              {toReview.length === 1
                ? "1 bill has been on hold since before today."
                : `${toReview.length} bills have been on hold since before today.`}{" "}
              Keep each one for tomorrow or let it go.
              <button type="button" className="btn" onClick={() => setShowHolds(true)}>
                Review them
              </button>
            </p>
          )}
        </div>

        {till?.blocked ? (
          <p
            className="bill-alert bill-alert-stop bill-blocked-area"
            data-testid="bill-counter-blocked"
          >
            <AlertTriangle size={18} />
            {till.blocked}{" "}
            <Link className="btn" to="/sell/till">
              Open Till &amp; Sync
            </Link>
          </p>
        ) : (
          <>
            {showHolds && (
              <HeldBills
                holds={holds}
                toReview={toReview}
                blocked={holdsBlocked}
                onResume={(hold) => void resumeHold(hold)}
                onKeep={(hold) => engine && void answerHold(engine.keepHold(hold.held_uuid))}
                onLetGo={(hold) => engine && void answerHold(engine.releaseHold(hold.held_uuid))}
              />
            )}
            {mode === "return" && !returnFound && (
              <RecentBills recentBills={recentBills} onPick={pickRecentBill} />
            )}
            {mode === "return" && returnFound ? (
              <AgainstBill
                found={returnFound}
                picked={returnPicked}
                outgoing={returnOutgoing}
                late={isPastReturnWindow(returnFound.billed_at, world.policy.return_window_days)}
                windowDays={world.policy.return_window_days}
                locked={locked}
                onPick={pickReturnLine}
                onTakeEverything={() =>
                  setExchangeFromPicked(returnFound, takeEverythingBack(returnFound, returnPicked))
                }
                onOutgoing={(outgoing) => {
                  setReturnOutgoing(outgoing);
                  setReturnError("");
                  clearScan();
                  window.requestAnimationFrame(scan.focus);
                }}
                onChangeBill={changeReturnBill}
              />
            ) : bill.exchange ? (
              <ExchangeBack
                exchange={bill.exchange}
                refundPaise={bill.refund_paise}
                locked={locked}
                onRemove={removeLeg}
              />
            ) : null}
            <Lines
              lines={bill.lines}
              salespeople={world.salespeople}
              splitAllowed={till?.splitSale ?? false}
              locked={locked}
              onEdit={editLine}
              onSalesperson={pickSalesperson}
              onPicked={scan.focus}
              onRemove={removeLine}
              onUndo={undo}
              canUndo={undoStack.length > 0}
              footer={<Totals bill={bill} taxKind={taxKind} />}
            />
          </>
        )}
      </section>

      {/* Payment rail: right column (grid-column: 2). */}
      <aside className="bill-pay">
        <div className="bill-rail-body">
          <PaymentPanel
            bill={bill}
            payment={bill.payment}
            locked={locked}
            returnReady={returnReady}
            onChange={editPayment}
            onShowQr={() => {
              // A code belongs to one charge: never show the last one's.
              setUpiQr("");
              setCharging(true);
            }}
          />
          {params.get("preview") === "plus" && (
            <PlusRail
              payablePaise={bill.payable_paise}
              online={till?.online ?? true}
              locked={locked}
            />
          )}
          {params.get("preview") !== "plus" && (
            <CustomerStrip
              value={customer}
              storeStateCode={storeState}
              db={engine?.db ?? null}
              locked={locked}
              onChange={editCustomer}
            />
          )}
          {/* Ticket 20: collecting a customer reservation. Only where the store
              has it on (or this bill already carries one), and only in the tab
              that holds the counter. */}
          {engine &&
            till?.lockHeld &&
            !cart.special_order &&
            (till.customerReservation || cart.reservation) && (
              <ReservationPickupCard
                storeCode={engine.storeCode}
                online={till.online}
                locked={locked}
                current={cart.reservation ?? null}
                onCollect={collectReservation}
                onClear={clearReservation}
              />
            )}
          {/* Ticket 21: collecting a special order, the same way. */}
          {engine &&
            till?.lockHeld &&
            !cart.reservation &&
            (till.specialOrders || cart.special_order) && (
              <SpecialOrderCollectCard
                storeCode={engine.storeCode}
                online={till.online}
                locked={locked}
                current={cart.special_order ?? null}
                onCollect={collectSpecialOrder}
                onClear={clearSpecialOrder}
              />
            )}
          {/* Ticket 19: selling and taking gift vouchers. Only where the store
              has them on (or this bill already carries one), and only in the
              tab that holds the counter. Online only. */}
          {engine && till?.lockHeld && (till.giftVouchers || bill.gift_vouchers.length > 0) && (
            <GiftVoucherCard
              storeCode={engine.storeCode}
              online={till.online}
              locked={locked}
              held={bill.gift_vouchers}
              onTake={takeGiftVoucher}
              onRemove={removeGiftVoucher}
              onPrint={(html) => void print(html)}
            />
          )}
          {/* Ticket 22: a paid alteration's charge. Only where the store has
              alterations on; it bills offline like any line. */}
          {till?.alterationCharge && till.lockHeld && (
            <AlterationChargeCard
              charge={till.alterationCharge}
              locked={locked}
              onAdd={addAlteration}
            />
          )}
          {/* Ticket 18: the customer's saved sizes, read online once a number is
              on the bill. Only where the store has it on; never part of Save & Print. */}
          {till?.savedSizes && till.lockHeld && (
            <SavedSizeCard mobile={customer.mobile} online={till.online} />
          )}
          {/* Ticket 15: the customer's own consent, asked on the display. Only
              where the store has it on, and only in the tab that holds the
              counter; it never touches Save & Print. */}
          {till?.customerConsent && till.lockHeld && (
            <ConsentCard
              engine={engine}
              display={display}
              mobile={customer.mobile}
              wording={till.consentWording}
              tillNumber={till.device?.identity.counter_id ?? ""}
              online={till.online}
              pending={till.consentsPending}
              refused={till.consentsRefused}
            />
          )}
        </div>
        <RailFoot
          duePaise={returnReady ? bill.payable_paise : null}
          blocked={
            till?.onlinePending
              ? "Resolve the previous submission before issuing another bill."
              : blocked
          }
          saving={saving}
          mode={mode}
          paper={paper}
          lastBillNumber={lastBill?.bill.doc_number ?? null}
          onReprint={() => lastBill && void print(lastBill.receipt, lastBill.bill.doc_number)}
          onSave={trySave}
        />
      </aside>

      {finishOpen && lastBill && (
        <FinishOverlay
          bill={lastBill.bill}
          cashReceivedPaise={lastBill.cashReceivedPaise}
          printProblem={printProblem}
          busy={saving}
          onPrint={() => void print(lastBill.receipt, lastBill.bill.doc_number)}
          onNext={nextBill}
        />
      )}

      {/* "Did you mean" and "bill it off the tag" (G-4): portaled to
          `document.body` and placed by `usePositionedPopover`, so neither can
          push the rail or the footer a pixel - they float over the grid
          instead. The portal also escapes the blocked-counter takeover (AC5:
          the block replaces the work area entirely), so it must gate on
          `counterBlocked` itself - otherwise it can append lines to a cart
          nobody can see. */}
      {!counterBlocked &&
        !locked &&
        scanFloat.at &&
        createPortal(
          <div
            ref={scanFloat.popoverRef}
            className="bill-float"
            // Applied whole, never field by field: the hook anchors a `"below"`
            // popover by whichever edge leaves it room, and a style that named
            // only `top` would strand a flipped one where the last placement
            // put it (`PopoverPlacement`).
            style={{ ...scanFloat.at }}
          >
            {unknown && (
              <NotInSystem
                barcode={unknown}
                locked={locked}
                onBill={() => takeUnknown(unknown)}
                onDismiss={closeScanFloat}
              />
            )}
            {suggestions.length > 0 && (
              <Suggestions
                pieces={suggestions}
                onPick={(piece) => {
                  const found = resolveScan(piece.barcode, world);
                  takePiece(piece, found.candidates, found.stock);
                  scan.focus();
                }}
              />
            )}
          </div>,
          document.body,
        )}

      {!counterBlocked &&
        !locked &&
        returnSearchFloat.at &&
        createPortal(
          <div
            ref={returnSearchFloat.popoverRef}
            className="bill-float return-customer-float"
            style={{ ...returnSearchFloat.at }}
          >
            <ReturnCustomerSearch
              searchKey={returnSearchKey}
              term={returnSearchTerm}
              online={Boolean(till?.online)}
              looking={returnLooking}
              error={returnError}
              customers={returnCustomers}
              matches={returnMatches}
              onSearchKey={(key) => {
                returnRequests.current.invalidate();
                setReturnLooking(false);
                setReturnSearchKey(key);
                setReturnCustomers([]);
                setReturnMatches(null);
              }}
              onTerm={(term) => {
                returnRequests.current.invalidate();
                setReturnLooking(false);
                setReturnSearchTerm(term);
              }}
              onSearch={() => void searchReturnCustomer()}
              onPickCustomer={(known) => {
                const selected = billSearchForCustomer(known);
                setReturnSearchKey(selected.key);
                setReturnSearchTerm(selected.term);
                void searchReturnCustomer(selected.term, selected.key);
              }}
              onPick={(match) => void openReturnMatch(match)}
            />
          </div>,
          document.body,
        )}

      {returnAsking && (
        <ManagerPin
          managers={world.managers}
          asks={returnAsking}
          cashierId={till?.managerPinRules ? (user?.id ?? null) : null}
          wrong={returnPins.wrong}
          onWrong={returnPins.wasWrong}
          onClose={() => setReturnAsking(null)}
          onAuthorised={(authorisation) => {
            returnPins.clear();
            setReturnAsking(null);
            setCart((current) => {
              const next = { ...current, authorisation };
              onScreenRef.current = { ...onScreenRef.current, cart: next };
              return next;
            });
            void save(authorisation);
          }}
        />
      )}

      {/* The QR charge card (#248). Gated on the figure as well as on the
          cashier's tap: a card charging nought is not a charge, and the UPI row
          can fall to nought behind the modal in only one way - a `freshCounter`
          - which closes it anyway. */}
      {charging && bill.split.upi_paise > 0 && (
        <UpiCharge
          amountPaise={bill.split.upi_paise}
          adapter={payments}
          onConfirmed={upiConfirmed}
          onQr={setUpiQr}
          onClose={() => {
            setUpiQr("");
            setCharging(false);
            scan.focus();
          }}
        />
      )}
    </div>
  );
}

/** Whatever went wrong, as a sentence for the counter. */
function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * The bill number this screen was sent here to re-enter from paper (#189), or
 * null - which is anything the counter does not still regard as outstanding.
 *
 * Two questions, and the second is the one that matters. A positive whole number
 * only means the address bar is well formed; what makes it a bill to key in is
 * that head office is missing it and this till has not already keyed it in. Both
 * are read from the till's own state:
 *
 *   · `register.holes` is the live list, so a store working through more than the
 *     200 a response carries keeps going as earlier ones close;
 *   · `handover.unsynced_hint` is the frozen list somebody was handed, which
 *     still names a bill after a sync has closed it in the register's eyes but
 *     before the drawer has been worked through;
 *   · `paperEntered` is what this counter has actually keyed in, and it wins over
 *     both.
 *
 * Anything else is somebody's hand-typed address, and honouring one would offer
 * to bill again under a number the server already holds - which halts the whole
 * store's queue when it lands.
 */
export function outstandingPaperSeq(
  params: URLSearchParams,
  till: TillSnapshot | null,
): number | null {
  const asked = Number(params.get("paper"));
  if (!Number.isInteger(asked) || asked < 1 || !till) return null;
  if (till.paperEntered.includes(asked)) return null;
  const missing = [...(till.register?.holes ?? []), ...(till.handover?.unsynced_hint ?? [])];
  return missing.includes(asked) ? asked : null;
}

/** Which one of the counter's seven possible banners to show (#243).
 *
 *  Every one of these used to be its own paragraph, stacking - a bill with a
 *  gift earned, a note from an exchange, and holds waiting review could carry
 *  three banners at once, each pushing the totals further down the page. The
 *  frame gives them one line, so something has to decide which one wins when
 *  several are true at once.
 *
 *  `paper` is not one of the seven: keying in a printed bill needs its date
 *  field and its "Not this one" exit live the whole time it is active, not
 *  only when it happens to win this line (round-2 finding - a failed save in
 *  paper mode set `note`, which hid both controls with only "start a new
 *  bill" left to escape by). It renders from its own always-on band
 *  (`paper !== null`, see `Counter`) instead, the same way `blocked` renders
 *  from the work area instead of this line. */
export type BillAlertKind =
  | "blocked"
  | "loading"
  | "no-price-list"
  | "print-problem"
  | "note"
  | "gift"
  | "holds-due";

export interface BillAlertFlags {
  blocked: boolean;
  loading: boolean;
  noPriceList: boolean;
  printProblem: boolean;
  note: boolean;
  gift: boolean;
  holdsDue: boolean;
}

/**
 * Precedence, not a boolean OR: the order these banners used to stack in, top
 * to bottom, becomes the order they take turns in now that only one may show.
 *
 * `blocked` is first and wins outright over everything, including a print
 * problem that is also true - Rule 5 says collapsing alerts to one line must
 * not soften the second-window hard block, so it never shares this line with
 * a lesser alert. It does not render *from* this line at all, in fact: the
 * blocked counter takes over the whole work area instead (see `Counter`),
 * which is the stronger treatment the rule asks for.
 *
 * `print-problem` is second, ahead of `note`: `save()` sets both in the same
 * order every time printing fails after a successful commit (`setNote` then
 * `await print(receipt)`, which is the only place `printProblem` is ever set
 * true), so letting `note` win here would bury the one thing on this screen
 * that tells the cashier the receipt did not print and Reprint is what to
 * press (round-2 finding). That only stays a safe trade because `printProblem`
 * cannot outlive the bill it belongs to: `takePiece`/`takeUnknown` clear it on
 * the next scan and `resumeHold` clears it before swapping the cart for a held
 * bill's, so `holdBill`'s and `resumeHold`'s own failure notes are never a
 * *stale* print problem's casualty (round-2 finding, second pass). A live,
 * still-relevant print problem can still outrank `answerHold`'s note - that is
 * a genuine conflict between two unrelated live alerts sharing one line, not
 * staleness, and is left as a residual (see `deviations.md`).
 *
 * `note` is third, ahead of `loading`/`no-price-list`, because it is the one
 * channel every failure on this screen reports through (`save`, `holdBill`,
 * `resumeHold`, `answerHold` all funnel their catch block into it). Before the
 * collapse these banners stacked, so an error note was never hidden behind a
 * mode banner - a mode is a lesser alert than an error, not the other way
 * round, and a cashier who does not see why Save failed either re-submits a
 * bill that already went through or walks away thinking one never did.
 */
export function pickBillAlert(flags: BillAlertFlags): BillAlertKind | null {
  if (flags.blocked) return "blocked";
  if (flags.printProblem) return "print-problem";
  if (flags.note) return "note";
  if (flags.loading) return "loading";
  if (flags.noPriceList) return "no-price-list";
  if (flags.gift) return "gift";
  if (flags.holdsDue) return "holds-due";
  return null;
}

/** `outstandingPaperSeq`'s own params shape, built for a number that did not
 *  come off the address bar - a draft's `paper`, being checked on its own
 *  terms rather than the URL's. */
function paperParams(paper: number): URLSearchParams {
  return new URLSearchParams({ paper: String(paper) });
}

/**
 * Is a draft's paper claim safe to restore, given what the counter and the
 * address bar currently say (#244, binding rule 0c)?
 *
 * An ordinary draft (`paper: null`) is fine unless the address bar is itself
 * mid a *different* paper re-entry - landing a crashed, unrelated cart onto a
 * re-entry the cashier explicitly navigated to is the same "mixed halves"
 * failure the cart/customer predicate already refuses. A draft that does
 * claim paper mode must not contradict a different number already in the
 * address bar, and must still be one `outstandingPaperSeq` accepts right now -
 * a number that has since synced, or been keyed in by somebody else, is no
 * longer this draft's to restore.
 */
export function paperConsistent(
  draftPaper: number | null,
  params: URLSearchParams,
  till: TillSnapshot | null,
): boolean {
  const urlPaper = outstandingPaperSeq(params, till);
  if (draftPaper === null) return urlPaper === null;
  if (urlPaper !== null && urlPaper !== draftPaper) return false;
  return outstandingPaperSeq(paperParams(draftPaper), till) === draftPaper;
}

/** Now, in the shape `<input type="datetime-local">` wants - which is local
 *  time with no zone on it, not an ISO instant. The till's own clock, because a
 *  bill's date is the store's day (see `tillToday`). */
function localNow(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

/** Where the store's own registration has not landed yet. The receipt still
 *  prints - a customer's copy with no GSTIN on it is better than no copy. */
const FALLBACK_STORE = { code: "", gstin: "", state_code: "" };

/** How a line reads on paper, from the cart the counter just billed.
 *
 *  A sold-before-inward line has no brand and no item name, so what the cashier
 *  typed is the only description that exists - and it is the one the customer
 *  needs, because the alternative is a receipt line that is just a barcode. */
function describeFrom(lines: PricedLine[]) {
  const words = new Map(
    lines.map((line) => [
      line.line_no,
      // Ticket 22: an alteration charge reads with its service code on paper.
      line.kind === "alteration"
        ? line.manual_desc
        : describePiece(line) || line.manual_desc.trim(),
    ]),
  );
  return (line: { line_no: number; barcode: string }) => words.get(line.line_no) || line.barcode;
}

/** One DOM node into two refs that each need it for a different reason: the
 *  focus patrol's own ref (`useScanBox`) and the scan-box float's trigger ref
 *  (`usePositionedPopover`), which cannot share a single `useRef` because they
 *  come from two different hooks that each own theirs. */
function mergeRefs<T>(...refs: RefObject<T>[]): (node: T | null) => void {
  return (node) => {
    refs.forEach((ref) => {
      (ref as MutableRefObject<T | null>).current = node;
    });
  };
}

/** The Rule 5 recovery door stays a positioned float: the scanner's error card
 * tells the cashier what happened, while this is the non-blocking action for a
 * garment that arrived before its paperwork. */
function NotInSystem({
  barcode,
  locked,
  onBill,
  onDismiss,
}: {
  barcode: string;
  locked: boolean;
  onBill: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="card section-card bill-unknown" data-testid="bill-unknown">
      <p className="bill-unknown-note">
        <AlertTriangle size={15} />
        <span>
          Barcode <span className="mono">{barcode}</span> is not in this counter's copy.
        </span>
      </p>
      <div className="bill-unknown-actions">
        <button type="button" className="btn" disabled={locked} onClick={onBill}>
          Bill it off the tag
        </button>
        <button type="button" className="btn" onClick={onDismiss}>
          Not that
        </button>
      </div>
    </div>
  );
}

function Suggestions({
  pieces,
  onPick,
}: {
  pieces: { piece: TillItem; stock: number }[];
  onPick: (piece: TillItem) => void;
}) {
  return (
    <div className="card section-card bill-suggest" data-testid="bill-suggestions">
      <p className="eyebrow">Did you mean</p>
      <div className="bill-suggest-rows">
        {pieces.map(({ piece, stock }) => (
          <button
            key={`${piece.barcode}/${piece.season}`}
            type="button"
            className="btn bill-suggest-row"
            data-testid={`bill-suggest-${piece.barcode}`}
            onClick={() => onPick(piece)}
          >
            <span>{describePiece(piece)}</span>
            <span className="muted-cell">
              {piece.season} · {stock} in stock
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The pieces coming back on this bill (#184, D2).
 *
 * Its own block above the line grid rather than rows inside it, and that is the
 * decision rather than the easy way out. A returned piece is priced at what the
 * customer paid on *another* bill - it has no MRP here, no discount, no offer and
 * no salesperson - so seven of the grid's twelve columns would be blank on it, and
 * the two rows a cashier has to keep straight (going out, coming back) would be
 * interleaved in one list with a minus sign as the only thing telling them apart.
 *
 * The reason and the condition are the counter's to set, and the condition is the
 * one that matters: a damaged piece goes to quarantine and never back on the
 * shelf, which is a decision a person makes with the garment in their hand.
 */
function ExchangeBack({
  exchange,
  refundPaise,
  locked,
  onRemove,
}: {
  exchange: Exchange;
  refundPaise: number;
  locked: boolean;
  onRemove: (key: string) => void;
}) {
  return (
    <section className="card section-card bill-exchange" data-testid="bill-exchange">
      <p className="eyebrow">
        <Undo2 size={14} /> Coming back · against{" "}
        <span className="mono">{exchange.original.doc_number}</span>
      </p>
      <div className="table-wrap">
        <table className="data" data-testid="bill-exchange-lines">
          <thead>
            <tr>
              <th>Piece</th>
              <th className="num">Qty</th>
              <th>Reason</th>
              <th>Condition</th>
              <th className="num">Back</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {exchange.lines.map((leg) => (
              <tr key={leg.key} data-testid={`bill-exchange-${leg.original_line}`}>
                <td>
                  {leg.description}
                  <br />
                  <span className="mono muted-cell">{leg.barcode}</span>
                </td>
                <td className="num">{leg.qty}</td>
                <td>{leg.reason || <span className="muted-cell">not said</span>}</td>
                <td>
                  {leg.condition === "damaged" ? (
                    <span className="bill-overcap">damaged · quarantine</span>
                  ) : (
                    "good · back on the shelf"
                  )}
                </td>
                <td className="num">
                  <Money paise={leg.refund_paise} />
                </td>
                <td>
                  <button
                    type="button"
                    className="line-del"
                    disabled={locked}
                    data-testid={`bill-exchange-remove-${leg.original_line}`}
                    aria-label={`Take the returned piece on line ${leg.original_line} off this bill`}
                    onClick={() => onRemove(leg.key)}
                  >
                    <X size={14} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted-cell" data-testid="bill-exchange-total">
        Given back: <Money paise={refundPaise} /> - what the customer paid for these pieces, not
        today&rsquo;s price.
      </p>
    </section>
  );
}

function Totals({ bill, taxKind }: { bill: ReturnType<typeof priceCart>; taxKind: B2bTaxKind }) {
  return (
    <div className="bill-totals" data-testid="bill-totals">
      <Figure label="Pieces" value={String(bill.pieces)} />
      <Figure label="Gross" value={<Money paise={bill.gross_paise} />} />
      {bill.saved_paise > 0 && (
        <Figure label="You saved" value={<Money paise={bill.saved_paise} />} testId="bill-saved" />
      )}
      {bill.refund_paise > 0 && (
        <Figure
          label="Given back"
          value={<Money paise={bill.refund_paise} />}
          testId="bill-given-back"
        />
      )}
      {bill.round_paise !== 0 && (
        <Figure
          label="Rounding"
          value={<Money paise={bill.round_paise} />}
          testId="bill-rounding"
        />
      )}
      {/* Not a `Figure` any more (#247, grill Q7): the tax figure is the way
          into the breakup that used to be twelve numbers down the grid, and it
          words itself from the same rule the paper does (`tax.taxLabel`). */}
      <TaxFigure bill={bill} kind={taxKind} />
      {/* Ticket 03: which tax settings version this bill is taxed under. Shown
          only where the saved settings are in use - a store on the slab table
          (version 1, every real store until the CA signs off) sees nothing new. */}
      {bill.tax_setting_version > 1 && (
        <Figure
          label="Tax settings"
          value={`Version ${bill.tax_setting_version}`}
          testId="bill-tax-version"
        />
      )}
      {bill.lines.some((line) => line.tax_rule_missing) && (
        <p className="warn-note" data-testid="bill-tax-rule-missing">
          No tax rule covers the HSN of{" "}
          {bill.lines
            .filter((line) => line.tax_rule_missing)
            .map((line) => `line ${line.line_no}`)
            .join(", ")}
          . It is charged the rate set for that case, and head office is told.
        </p>
      )}
      <Figure label="Net" value={<Money paise={bill.net_paise} />} testId="bill-net" strong />
      {/* Ticket 11: a bank instant discount is a payment the bank makes, not a
          price cut. The bill's total stands; the customer is asked for the rest. */}
      {bill.bank_offers.map((offer) => (
        <Figure
          key={offer.offer_id}
          label={`Bank offer · ${offer.offer_name}`}
          value={<Money paise={-offer.amount_paise} />}
          testId="bill-bank-offer"
        />
      ))}
      {/* Ticket 13: the credit note printed beside this invoice, and the bank
          offer's part of a returned piece, which the customer pays back. */}
      {bill.credit_note && (
        <Figure
          label={`Credit note · tax ${bill.credit_note.late ? "not reduced" : "reversed"}`}
          value={
            <>
              <Money paise={bill.credit_note.value_paise} />
              {!bill.credit_note.late && (
                <span className="muted-cell">
                  {" "}
                  (tax <Money paise={bill.credit_note.gst_paise} />)
                </span>
              )}
            </>
          }
          testId="bill-credit-note"
        />
      )}
      {bill.credit_note?.late && (
        <p className="warn-note" data-testid="bill-credit-note-late">
          This piece is back after its credit-note deadline, so the customer gets its value but the
          credit note reduces no tax. Head office is told.
        </p>
      )}
      {bill.refund_bank_paise > 0 && (
        <Figure
          label="Bank offer part, paid back"
          value={<Money paise={bill.refund_bank_paise} />}
          testId="bill-bank-part"
        />
      )}
      {/* Ticket 19: what each gift voucher pays; the rest stays on it. */}
      {bill.gift_vouchers
        .filter((voucher) => voucher.pays_paise > 0)
        .map((voucher) => (
          <Figure
            key={voucher.number}
            label={`Gift voucher · ${voucher.number}`}
            value={<Money paise={-voucher.pays_paise} />}
            testId="bill-gift-voucher"
          />
        ))}
      {(bill.bank_offer_paise > 0 || bill.refund_bank_paise > 0 || bill.gift_voucher_paise > 0) && (
        <Figure
          label="To pay"
          value={<Money paise={bill.payable_paise} />}
          testId="bill-payable"
          strong
        />
      )}
    </div>
  );
}

function Figure({
  label,
  value,
  testId,
  strong,
}: {
  label: string;
  value: React.ReactNode;
  testId?: string;
  strong?: boolean;
}) {
  return (
    <div className={strong ? "bill-figure bill-figure-strong" : "bill-figure"}>
      <span className="bill-figure-label">{label}</span>
      <span data-testid={testId}>{value}</span>
    </div>
  );
}

function NoCounter() {
  return (
    <div className="page-pad">
      <PageHeader lead="The counter." />
      <p className="warn-note" data-testid="bill-no-counter">
        This login is not a counter. A till signs in as one store: the local price list and manager
        authorisations belong to a single shop, so a login that can see several has no counter to
        bill from.
      </p>
    </div>
  );
}
