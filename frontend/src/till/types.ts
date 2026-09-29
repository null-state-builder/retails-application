// What travels between the counter and the server (#180, D10 step 3).
//
// Written by hand rather than generated: `src/lib/api-schema.ts` is produced from
// the API's OpenAPI document, and these two payloads are the till's, which means
// the *till* has to keep working when it has not spoken to a server for a week.
// The shapes here are the contract's (`docs/features/pos-store-front/
// api-contract.md`, step 3, including its 30 Jul amendment), and the fields the
// amendment made nullable are nullable here.

import type { TillConsentWording } from "./consent";
import type { B2bTaxKind } from "./gstin";

/** One piece as the counter knows it: a barcode in a season, at a ticket price. */
export interface TillItem {
  barcode: string;
  season: string;
  design: string;
  brand: string;
  item: string;
  size: string;
  color: string;
  hsn: string;
  /** Null - never nought - when nobody has recorded an MRP for this lot. The
   *  contract is explicit: a zero here would bill a garment at nothing and post
   *  no revenue or tax against it, so a screen must ask a human for the price
   *  rather than treat this as free. */
  mrp_paise: number | null;
  no_discount: boolean;
  /** This lot's season is the one explicit *unknown historical season* (OPS-03),
   *  so a screen can say so rather than showing a customer-facing cashier a code.
   *  Absent at a legacy store, which has no such value. */
  season_unknown_historical?: boolean;
}

export interface TillStock {
  barcode: string;
  qty: number;
  /** Which buying season this quantity is, at a goods-v1 store (OPS-07). A legacy
   *  store's rows carry a barcode and a quantity and nothing else, so this is
   *  absent there rather than blank - the two are different facts.
   *
   *  Note for OPS-09, which owns the till's local working set: the local `stock`
   *  table is still keyed by barcode alone, so a barcode standing in two seasons
   *  collapses to one row on the device. The server sends the seasons apart; the
   *  device cannot yet hold them apart. */
  season?: string;
}

export interface TillGstSlab {
  hsn_prefix: string;
  threshold_paise: number;
  /** Two-decimal strings, so the till's back-calculation out of an MRP-inclusive
   *  price cannot drift a paise from the server's. */
  rate_below: string;
  rate_above: string;
  effective_from: string;
}

/** One HSN's rule in a saved tax settings version (store operations ticket 03).
 *  A piece priced P (tax included, after discount) takes `rate_below` when
 *  P ÷ (1 + rate_below) ≤ `threshold_paise`, otherwise `rate_above`. A blank
 *  prefix covers every HSN no longer prefix covers. */
export interface TillTaxRule {
  /** `price_line`, or `flat_rate` (ticket 12): the rate schedule's one rate for
   *  this HSN, whatever the price. A flat rule is also sent with no price line
   *  and both rates equal to it, so an older till charges the same. */
  kind: string;
  hsn_prefix: string;
  name: string;
  threshold_paise: number;
  rate_below: string;
  rate_above: string;
  /** A `flat_rate` rule's rate. */
  rate?: string;
}

/** A saved tax settings version, 2 and up. Version 1 is the slab table above. */
export interface TillTaxVersion {
  version: number;
  /** The first day, on the counter's own calendar, it taxes a bill. */
  applies_from: string;
  /** When head office saved it (ISO). A bill made before that moment, by this
   *  counter's clock, is not taxed under it - the server's own comparison.
   *  Optional while an older server is still in a rolling deploy. */
  saved_at?: string;
  rules: TillTaxRule[];
  /** The rate a line takes when no rule covers its HSN; the server flags it. */
  unmatched_rate: string;
  /** Ticket 11: the version's non-rate choices. Absent from an older server,
   *  which means the baseline: the total rounded to the nearest rupee. */
  options?: {
    round_total_paise?: number;
    /** Ticket 13: may a store take back a bill another GSTIN issued? Absent: no. */
    cross_gstin_returns?: boolean;
    /** Ticket 13: `{gstin: {"26-27": "YYYY-MM-DD"}}`, as Accounts recorded them. */
    annual_return_filed?: Record<string, Record<string, string>>;
  };
}

/** The tax settings this counter holds (ticket 03). */
export interface TillTaxSettings {
  /** This store's switch. Off: every bill is taxed by version 1, the slabs. */
  rules_on: boolean;
  /** Every saved version, future-dated ones too, so the counter reaches a new
   *  version on its date with no network. */
  versions: TillTaxVersion[];
  /** Ticket 11: this store's `gst-after-discount` switch. Absent (an older
   *  server) or false: bills are priced exactly as before. */
  after_discount?: boolean;
  /** Ticket 12: the lengths an HSN may have (digits only). Any other code is
   *  "no HSN" and matches no rule. Absent (an older server): 4, 6 or 8. */
  hsn_digits?: number[];
  /** Ticket 13: this store's `exchange-return-tax` switch. Absent (an older
   *  server) or false: returns and exchanges work exactly as before. */
  return_tax?: boolean;
}

export interface TillOffer {
  id: number;
  /** What the counter calls it - the chip on the line and the "you saved" line
   *  read this, so it is the one field here a customer ever hears out loud. */
  name: string;
  layer: string;
  brand?: string;
  trigger_type: string;
  trigger_config: Record<string, unknown>;
  reward_type: string;
  reward_config: Record<string, unknown>;
  item_scope: Record<string, unknown>;
  starts_on: string;
  ends_on: string | null;
  combinable: boolean;
  priority: number;
}

/** One rule this line's discount was chosen over, and by how much. */
export interface OfferCredit {
  offer_id: number;
  offer_name: string;
  saved_paise: number;
}

export interface StackedCredit extends OfferCredit {
  layer: string;
}

/**
 * Why a line cost what it cost - what rides to `SaleLine.offer_evidence` (B3).
 *
 * A wire shape rather than an engine one, which is why it lives here: it is
 * written at the counter, sent up with the bill, stored on the line, and read
 * months later by the daily applied-versus-rulebook check when the rule itself
 * may have been ended and replaced twice over.
 */
export interface OfferEvidence {
  offer_id: number | null;
  offer_name: string;
  layer: string;
  saved_paise: number;
  beat: OfferCredit[];
  stack: StackedCredit[];
}

/** Nothing applied. Written as `{}`, exactly as the server writes it. */
export type NoOffer = Record<string, never>;

/** One person on this store's staff list who may be named as the salesperson
 *  (ticket 07). Only the id and the name reach the counter (overall PRD §10.4). */
export interface TillSalesperson {
  id: string;
  name: string;
}

export interface TillManager {
  user_id: number;
  name: string;
  till_pin_hash: string;
}

/** A selling period, with the master's own ordering. Seasons are named, never
 *  dated, so "older" is `sort_order` and nothing else - and a closed season sorts
 *  behind every open one however old it is. */
export interface TillSeason {
  code: string;
  name: string;
  status: string;
  sort_order: number;
  /** The one explicit "unknown historical season" (OPS-03), for labelling.
   *  Optional because an instance that predates it answers without it. */
  historical_unknown?: boolean;
}

/** Somebody KDPS has billed before, as the last dataset pull left them (#245).
 *
 *  Deliberately a different type from `TillCustomer` even though the three fields
 *  match today, because they are answers to different questions: `TillCustomer`
 *  is who *this bill* is for and is snapshotted onto it, this is a row in the
 *  shared phone book the typeahead searches. Only one of the two can grow a
 *  field - a customer's last visit belongs on the master, never on a printed
 *  bill - and collapsing them now is what would make that addition silently
 *  change what every cart carries.
 *
 *  All-KDPS, not this store's: a Deoghar regular must be recognised in Ranchi
 *  (grill Q6). No purchase history rides down with it. */
export interface TillKnownCustomer {
  mobile: string;
  name: string;
  gstin: string;
}

/** The shop floor's money dials. */
export interface TillPolicy {
  /** How much of a line's MRP a cashier may knock off, as a two-decimal string.
   * Above it, the till rejects the bill outright; a manager PIN cannot lift it. */
  manual_discount_cap_percent: string;
  /** Whether head office allows a cashier's manual discount to stack on a line
   * already reduced by the rulebook. Defaults to false for an older server. */
  manual_discount_on_offer_lines: boolean;
  /** How many local calendar days after its bill an exchange needs no manager. */
  return_window_days: number;
  /** How many days of this store's bills the till holds for offline exchange
   * (OPS-09). Head office's dial, not the device's. */
  cached_bill_days: number;
}

export interface TillStoreIdentity {
  code: string;
  gstin: string;
  state_code: string;
}

/** The counter's transfer pause as this device holds it (change PRD §10.2,
 *  Anand, 25 September 2026). While a row is here, no bill is finalised.
 *
 *  · `pausing` - written here, the server's release not yet confirmed. A lost
 *    answer leaves the counter here, on the safe side.
 *  · `paused` - the server released the stock and holds the pause.
 *  · `resuming` - the server has ended the pause; the counter is still shut
 *    until a full, fresh dataset has landed. */
export interface TillPauseState {
  stage: "pausing" | "paused" | "resuming";
  reason: string;
  at: string;
  /** Where this counter would bill next when it paused: the financial year it
   *  was counting in (its own, not today's - see `pause.positionNow`) and the
   *  number in that year. */
  fy: string;
  nextSeq: number;
}

/** Which device this store's counter is, and how long it may keep billing
 *  (OPS-09, PRD §10.1). The shape of `GET /api/sell/till` and of the `till`
 *  block on every dataset response.
 *
 *  `server_time` is the load-bearing field. The window is the server's, measured
 *  on the server's clock, and a shop-floor machine's clock is a thing anybody can
 *  change - so the till anchors "how much is left" to this moment rather than to
 *  its own idea of now. See `authority.ts`. */
export interface TillIdentity {
  registered: boolean;
  counter_id: string;
  series_prefix: string;
  authority_until: string | null;
  authority_hours: number;
  working_set_version: number | null;
  server_time: string;
  allocation_version: number | null;
  /** The counter is in its transfer pause (change PRD §10.2). Absent from a
   *  server older than the pause, which means "not paused". */
  paused?: boolean;
  pause_fy?: string | null;
  pause_next_seq?: number | null;
  pause_reason?: string;
  paused_at?: string | null;
  /** Answered once, by `POST /api/sell/till/register`, and kept on the device. */
  device_token?: string;
}

/** One line of a bill the till cached for exchange (OPS-09, PRD §10.3).
 *
 *  Every figure is what the *original* bill charged. No cost, no margin and no
 *  receipt-origin value is in this shape, and the server does not send one. */
export interface CachedBillLine {
  line_no: number;
  direction: string;
  /** Ticket 22: "alteration" for a paid alteration's charge, never taken back.
   *  Absent from a server before the ticket: goods. */
  kind?: string;
  barcode: string;
  season: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  item: string;
  hsn: string;
  qty: number;
  mrp_paise: number;
  net_paise: number;
  gst_rate: string;
  gst_paise: number;
  manual_desc: string;
  returned_qty: number;
  returned_paise: number;
  /** Ticket 13 (B60): what earlier returns took off the line's bank-offer share. */
  returned_bank_paise?: number;
}

/** A bill of this store's from the last few days, held on the device so an
 *  exchange can be taken against it with no network (PRD §10.3-10.4).
 *
 *  An exchange against a bill that is *not* here is refused offline, by design:
 *  the till cannot know what has already been given back off a bill it has never
 *  seen, and guessing is how one piece gets returned twice. */
export interface CachedBill {
  fy: string;
  till_seq: number;
  doc_number: string;
  till_number: string;
  billed_at: string;
  customer_name: string;
  customer_mobile: string;
  /** The buyer's GSTIN, "" on a B2C bill (ticket 05: an offline exchange against
   *  a B2B bill is refused). Optional while an older server says nothing. */
  buyer_gstin?: string;
  net_paise: number;
  lines: CachedBillLine[];
  tenders: { mode: string; amount_paise: number }[];
}

/** `GET /api/sell/dataset`. */
export interface DatasetPayload {
  cursor: string;
  full: boolean;
  /** Which reading of this store's shelf the `stock` section is (OPS-07). It goes
   *  up every time goods move at this store, so a till holding a different number
   *  is holding a stale shelf. `null` at a legacy store, whose stock is deltaed by
   *  the cursor instead; optional while an older API is still in a rolling deploy. */
  working_set_version?: number | null;
  store: TillStoreIdentity;
  /** Which device this store's counter is, and how long its window has left
   *  (OPS-09). Optional while an older API is still in a rolling deploy - and an
   *  absent block means "this server has nothing to say", never "you are not
   *  registered": treating it as the latter would stop a counter billing over a
   *  deploy. */
  till?: TillIdentity;
  items: TillItem[];
  stock: TillStock[];
  /** This store's own recent bills, for an offline exchange (OPS-09, §10.3). Sent
   *  whole every time: what is still returnable off a bill changes when *another*
   *  bill gives a piece back, which no watermark over the sold bill would notice.
   *  Absent from an older server and empty at a legacy store. */
  bills?: CachedBill[];
  gst_slabs: TillGstSlab[];
  /** Ticket 03. Absent from an older server, which means "nothing to say": the
   *  counter keeps what it holds rather than dropping to version 1. */
  tax_settings?: TillTaxSettings;
  /** Ticket 05: whether this counter refuses online-only work while offline.
   *  Optional so an older server in a rolling deploy says nothing. */
  online_only_refusals?: boolean;
  /** Ticket 06: whether a manager's PIN must be somebody other than the cashier.
   *  Optional so an older server in a rolling deploy says nothing. */
  manager_pin_rules?: boolean;
  /** Ticket 08: whether a line may be split between two salespeople.
   *  Optional so an older server in a rolling deploy says nothing. */
  split_sale?: boolean;
  /** Ticket 09: whether the till offers a customer display.
   *  Optional so an older server in a rolling deploy says nothing. */
  customer_display?: boolean;
  /** Ticket 15: whether the till asks for customer consent, and the wording it
   *  asks with. Optional so an older server in a rolling deploy says nothing. */
  customer_consent?: boolean;
  consent_wording?: TillConsentWording;
  /** Ticket 20: whether the till offers "Reservation pickup". Optional so an
   *  older server in a rolling deploy says nothing. */
  customer_reservation?: boolean;
  /** Ticket 18: whether the till shows the customer's saved sizes. Optional
   *  for the same reason. The sizes themselves are read online. */
  saved_sizes?: boolean;
  /** Ticket 21: whether the till offers special order collection. Optional
   *  for the same reason. */
  special_orders?: boolean;
  /** Ticket 19: whether the till sells and takes gift vouchers. Optional for
   *  the same reason. */
  gift_vouchers?: boolean;
  /** Ticket 22: the alteration charge line this counter makes, or null when the
   *  store's switch is off. Optional so an older server says nothing. */
  alteration_charge?: TillAlterationCharge | null;
  offers: TillOffer[];
  /** Ticket 07: the staff list's salespeople active at this store. Optional so an
   *  older server in a rolling deploy says nothing, and the counter keeps what it
   *  holds rather than emptying its picker. */
  salespeople?: TillSalesperson[];
  managers: TillManager[];
  seasons: TillSeason[];
  /** Optional while an older API instance is still in a rolling deploy. */
  policy?: TillPolicy;
  /** Everybody the business has billed, deltaed by the same cursor as the items.
   *  No `deleted` sibling: after a customer is erased (ticket 16) the list comes
   *  whole instead, with `customers_whole`.
   *
   *  Optional because a server that predates #245 answers without it, and the
   *  till has to read that absence as "nothing to say" rather than "the phone
   *  book is empty" - a rolling deploy is exactly those few minutes. Spelled
   *  optional rather than guarded-but-required so the compiler keeps the guard
   *  honest; `seasons` above carries the same contract in the older spelling. */
  customers?: TillKnownCustomer[];
  /** Ticket 16: `customers` is the whole list, to replace the till's copy -
   *  sent after a customer was erased. Absent from an older server. */
  customers_whole?: boolean;
  deleted: {
    items: string[];
    offers: number[];
  };
}

/** `GET /api/sell/register` - what the server has accepted from this counter. */
export interface RegisterPayload {
  fy: string;
  last_accepted_seq: number;
  holes: number[];
  hole_count: number;
  series_open: boolean;
}

/** `POST /api/sell/register/handover` - what a new machine is told when it takes
 *  over a store's counter (#189).
 *
 *  `unsynced_hint` is bounded at 200 and `hole_count` is not, deliberately: a
 *  machine that died at bill 5,000 leaves more receipts than a response should
 *  carry, and a screen listing 200 without saying so would tell somebody they
 *  had finished when they had not. */
export interface HandoverPayload {
  resume_from_seq: number;
  unsynced_hint: number[];
  hole_count: number;
}

/** A handover as the counter remembers it - the job list a store works down. */
export interface HandoverState extends HandoverPayload {
  /** When the handover was done, ISO, by the till's own clock. */
  at: string;
}

/**
 * Numbers this counter has keyed back in from their printed copies.
 *
 * Kept apart from the handover, and kept for the *year* rather than for the
 * handover, because it is what makes "exactly once" true rather than what makes
 * a screen tick a box. Three things would each defeat a weaker home for it: a
 * re-entered bill leaves the queue as soon as the server takes it, the handover
 * list is something a store puts away when it is finished, and a page reload
 * with the same address in the bar would happily do the whole thing again.
 *
 * `fy` scopes it because the counter restarts at 1 every April, so last year's
 * bill 61 and this year's are two different bills.
 */
export interface PaperEntered {
  fy: string;
  seqs: number[];
}

/** Ticket 22: the paid alteration line, as head office defines it - its service
 *  code, its fixed rate and how it reads on the bill. Never the till's own copy. */
export interface TillAlterationCharge {
  sac: string;
  gst_rate: string;
  description: string;
}

export interface BillLine {
  line_no: number;
  direction: "sale" | "return";
  /** Ticket 22: only on a paid alteration's own line. Absent is goods, so every
   *  other line goes on the wire - and replays - exactly as before. */
  kind?: "alteration";
  barcode: string;
  season?: string;
  qty: number;
  mrp_paise: number;
  disc_paise: number;
  net_paise: number;
  gst_rate: string;
  gst_paise: number;
  /** The staff record who sold the line (ticket 07). */
  salesperson?: string | null;
  /** Ticket 08: a line shared between two salespeople, the first being the
   *  line's own. Absent on every unsplit line. */
  shares?: { salesperson: string; percent: number }[];
  offer_id?: number | null;
  offer_evidence?: OfferEvidence | NoOffer;
  manual_desc?: string;
  override_by?: number | null;
}

/** One piece coming back on a bill, as the wire carries it (#184).
 *
 *  Everything money-shaped on it is what the *original* bill charged, not what
 *  today's price list says: `refund_paise` is what the customer actually paid for
 *  that quantity (D2) and `gst_rate`/`gst_paise` are the tax inside it at the
 *  rate that bill was raised under. The server recomputes both and refuses the
 *  whole bill where either is a paisa out. */
export interface BillExchangeLine {
  line_no: number;
  barcode: string;
  season?: string;
  qty: number;
  refund_paise: number;
  gst_rate: string;
  gst_paise: number;
  reason: string;
  /** Good goes back on the shelf; damaged goes to quarantine and never becomes
   *  sellable again without somebody looking at it (D3). */
  condition: "good" | "damaged";
  /** Which line of the original bill this gives back. */
  original_line: number;
  /** Ticket 13 (B60): the bank offer's part of `refund_paise`. Sent only when
   *  there is one, so every other bill goes on the wire as before. */
  bank_offer_paise?: number;
}

/** The exchange block on a bill: which bill is being given back against, and
 *  which of its lines. The server resolves the original from `(fy, till_seq)`
 *  pinned to the billing store, so the pair is the whole reference. */
export interface BillExchange {
  original: { fy: string; till_seq: number; store?: string };
  lines: BillExchangeLine[];
}

export interface BillTender {
  /** The four trimmed modes and no others - `SaleTender.Mode` on the server, and
   *  a `ChoiceField` there, so a fifth string is a bill the queue halts on. */
  mode: "cash" | "card" | "upi" | "bank_offer" | "advance" | "gift_voucher";
  amount_paise: number;
  /** Ticket 11: the bank offer a `bank_offer` tender is. Only on that mode. */
  offer_id?: number;
  /** Ticket 19: the number of the gift voucher a `gift_voucher` tender spends.
   *  Only on that mode. */
  gift_voucher?: string;
  /** Ticket 19: the check code on that voucher's slip. Only on that mode. */
  gift_voucher_code?: string;
  /** How the money was proven - required by the server on a UPI row, forbidden
   *  on every other mode (#241). The till only ever sends `manual` until the QR
   *  charge card (#248) lands. */
  upi_state?: "confirmed" | "manual";
  /** The acquirer's transaction reference. Only ever set alongside `confirmed`. */
  upi_reference?: string;
}

export interface BillTotals {
  gross_paise: number;
  discount_paise: number;
  net_paise: number;
  gst_paise: number;
  round_paise: number;
}

/** Who the bill is for, as the counter has them.
 *
 *  All three are optional to a *sale* and none is asked for: the mobile is for
 *  finding the bill again, the name reads on the paper, and a GSTIN is what
 *  turns a retail bill into a B2B tax invoice with a thirty-day clock on head
 *  office behind it (#187, grill Q8). Spelled once because the cart, the hold
 *  and the wire all carry the same three fields, and a fourth added to two of
 *  the three would be a hold that quietly lost it. */
export interface TillCustomer {
  name: string;
  mobile: string;
  gstin: string;
}

/** A bill as the screen hands it to the till: everything except its identity.
 *  The number, the financial year and the idempotency key are the till layer's
 *  to assign, and assigning them is the commit (see `numbering.ts`). */
export interface BillDraft {
  billed_at: string;
  origin?: "offline" | "online" | "paper";
  customer?: TillCustomer;
  /** The split the customer's copy was printed with. The server derives its own
   *  from the same GSTIN and flags a disagreement rather than preferring either
   *  (contract step 11) - so this is evidence about the paper, not an
   *  instruction. "none" on every B2C bill. */
  b2b_tax_kind?: B2bTaxKind;
  lines: BillLine[];
  tenders: BillTender[];
  totals: BillTotals;
  /** A piece coming back on this same bill (#184). Absent on an ordinary sale,
   *  which is almost all of them. */
  exchange?: BillExchange;
  /** The manager's tap: who authorised the exceptions on this bill, what they
   *  were shown, and when they typed the PIN (which is not Save & Print). */
  override?: { user_id: number; kind: string; at: string };
  /** The tax settings version the bill was taxed under (ticket 03, R-POS-013). */
  tax_setting_version?: number;
  /** Ticket 11: priced with GST after discount. Sent only when true, so a bill
   *  from a store with the switch off goes on the wire exactly as before. */
  gst_after_discount?: boolean;
  /** Ticket 13: the pieces coming back were taken by the exchange and return
   *  tax rules. Sent only when true, for the same reason. */
  return_tax?: boolean;
  /** Ticket 20: the customer reservation this bill collects. Its pieces are on
   *  the bill, and an `advance` tender is its advance. Absent on every other
   *  bill, so they go on the wire exactly as before. */
  reservation?: string;
  /** Ticket 21: the special order this bill collects. The piece that arrived
   *  is on the bill, and an `advance` tender is its advance. Absent on every
   *  other bill. */
  special_order?: string;
}

/** A bill after the commit: numbered, keyed, and in the queue. This is the exact
 *  body `POST /api/sell/sales` takes, plus the queue's own bookkeeping. */
export interface QueuedBill extends BillDraft {
  /** Dexie's insertion order, and therefore the FIFO the queue drains in. */
  id?: number;
  idempotency_uuid: string;
  store: string;
  fy: string;
  till_seq: number;
  origin: "offline" | "online" | "paper";
  /** What the customer's copy says, rendered at the till so the counter can
   *  name the bill before the server has ever seen it. */
  doc_number: string;
  /** `{StoreCode}-{CounterID}-{Seq}` - how the bill reads on the shop floor when
   *  this device is a registered till (R-POS-005, OPS-09). Empty at a store with
   *  no registered counter, which numbers exactly as it always has. */
  till_number?: string;
  /** The tax invoice number in the new series, `XXX/26-27/n`, taken from this
   *  counter's number block (ticket 04). Absent before the new format starts, at
   *  a store where it is off, and on a bill re-entered from paper. */
  tax_invoice_number?: string;
  /** How many times this bill has been offered to the server. Evidence for the
   *  exception card, and the input to the backoff. */
  attempts: number;
  last_error?: string;
}

/** What the server answered for a bill the queue drained. */
export interface AcceptedBill {
  doc_number: string;
  tax_invoice_number?: string | null;
  id: number;
  flags: string[];
}

/** A bill the server refused for a reason retrying cannot mend. */
export interface QueueHalt {
  doc_number: string;
  idempotency_uuid: string;
  code: string;
  message: string;
  at: string;
}
