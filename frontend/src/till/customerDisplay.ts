// ---------------------------------------------------------------------------
// The customer display (store operations ticket 09, ST-POS-6)
// ---------------------------------------------------------------------------
//
// A second browser window, opened from the till on the same device, turned to
// face the customer. It follows the bill through a `BroadcastChannel` between
// the two windows - no server round trip - so it keeps working offline, and
// the till never waits on it: every message is fire-and-forget, and a display
// that is closed, frozen or never opened changes nothing about a bill.
//
// **What reaches the display is built by picking fields, never by removing
// them.** `displayViewOf` constructs a fresh object from named fields of the
// priced bill, so a field added to the bill tomorrow (a cost, a salesperson
// share, a PIN prompt) does not reach a customer's screen unless somebody adds
// it here on purpose. The display picks again on arrival (`readMessage`), so a
// message with anything more on it is cut back to the same shape.
//
// It never carries cost, margin, salesperson or split details, PIN prompts,
// staff notes, the customer strip, or anything from a bill other than the one
// on the counter: every view replaces the last one whole.
//
// **Questions for the customer (ticket 15).** Nothing in ticket 09 asks one yet;
// the slot is here so ticket 15 only has to call `ask`. The till can put a question on the
// display (`ask`) and gets the customer's answer back through the same channel.
// Only an answer to a question still open, naming one of its own choices, is
// accepted; anything else is ignored. Consent itself is ticket 15's.

import { describePiece } from "./lookup";

/** One line of the bill as the customer sees it. */
export interface DisplayLine {
  description: string;
  qty: number;
  /** The ticket price of one piece. */
  mrp_paise: number;
  /** The offers that reduced this line, by their names. */
  offers: string[];
  /** Everything off the ticket price on this line: offers and any discount. */
  saved_paise: number;
  /** What the line comes to. */
  amount_paise: number;
}

/** A piece coming back on this bill (an exchange). */
export interface DisplayReturn {
  description: string;
  qty: number;
  amount_paise: number;
}

/** The UPI amount being charged, and the code to scan when there is one. */
export interface DisplayUpi {
  amount_paise: number;
  /** The acquirer's `upi://pay?...` payload. Empty until a payment machine
   *  supplies one (ST-POS-1): the display then says so rather than drawing a
   *  code that pays nobody. */
  qr: string;
}

export type DisplayView =
  | { screen: "welcome" }
  | {
      screen: "bill";
      lines: DisplayLine[];
      returns: DisplayReturn[];
      /** Every offer applied on the bill, once each. */
      offers: string[];
      savings_paise: number;
      total_paise: number;
      upi: DisplayUpi | null;
    }
  | { screen: "thanks" };

export const WELCOME: DisplayView = { screen: "welcome" };
export const THANKS: DisplayView = { screen: "thanks" };

/** A question the customer answers on the display themselves (ticket 15). */
export interface DisplayQuestion {
  id: string;
  text: string;
  choices: { value: string; label: string }[];
}

/** What the till says to the display. */
export type TillToDisplay =
  | { type: "view"; view: DisplayView }
  | { type: "ask"; question: DisplayQuestion }
  | { type: "unask"; id: string }
  /** The till's counter closed: the display goes back to its welcome. */
  | { type: "bye" };

/** What the display says to the till. */
export type DisplayToTill =
  /** Just opened (or reopened): send me where the bill is now. */
  | { type: "hello" }
  | { type: "answer"; id: string; value: string };

/** One channel per store, so a display only ever hears its own store's till. */
export function displayChannelName(storeCode: string): string {
  return `kdps-customer-display:${storeCode}`;
}

/** The address the till opens the display at. */
export function displayUrl(storeCode: string): string {
  return `/sell/display?store=${encodeURIComponent(storeCode)}`;
}

// --- building the view (the till) --------------------------------------------

/** The shape of a priced line this module reads - named, so nothing else can be
 *  read by accident. */
interface PricedLineLike {
  brand: string;
  item: string;
  design: string;
  size: string;
  color: string;
  barcode: string;
  manual_desc: string;
  qty: number;
  mrp_paise: number;
  disc_paise: number;
  offer_paise: number;
  net_paise: number;
  offer_credits: { offer_name: string }[];
}

interface ExchangeLegLike {
  description: string;
  barcode: string;
  qty: number;
  refund_paise: number;
}

interface PricedBillLike {
  lines: PricedLineLike[];
  exchange: { lines: ExchangeLegLike[] } | null;
  saved_paise: number;
  net_paise: number;
  /** Ticket 11: the bill less any bank offer. Absent means none. */
  payable_paise?: number;
}

function lineDescription(line: PricedLineLike): string {
  return line.manual_desc || describePiece(line) || line.barcode;
}

/**
 * The customer's view of the bill on the counter.
 *
 * Built field by field. An empty counter shows the welcome - or the thank-you,
 * when the bill before it has just been saved. The thank-you names no figure,
 * so it can never show a total that belonged to somebody else.
 */
export function displayViewOf(
  bill: PricedBillLike,
  options: { justSaved: boolean; upi: DisplayUpi | null },
): DisplayView {
  const returns = bill.exchange?.lines ?? [];
  if (!bill.lines.length && !returns.length) return options.justSaved ? THANKS : WELCOME;
  const offers: string[] = [];
  const lines = bill.lines.map((line): DisplayLine => {
    const names = unique(line.offer_credits.map((credit) => credit.offer_name).filter(Boolean));
    for (const name of names) if (!offers.includes(name)) offers.push(name);
    return {
      description: lineDescription(line),
      qty: line.qty,
      mrp_paise: line.mrp_paise,
      offers: names,
      saved_paise: line.disc_paise + line.offer_paise,
      amount_paise: line.net_paise,
    };
  });
  return {
    screen: "bill",
    lines,
    returns: returns.map((leg) => ({
      description: leg.description || leg.barcode,
      qty: leg.qty,
      amount_paise: leg.refund_paise,
    })),
    offers,
    savings_paise: bill.saved_paise,
    // What the customer pays: the bill less any bank offer (ticket 11).
    total_paise: bill.payable_paise ?? bill.net_paise,
    upi: options.upi ? { amount_paise: options.upi.amount_paise, qr: options.upi.qr } : null,
  };
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

// --- reading a message (the display) ------------------------------------------

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function strings(value: unknown): string[] | null {
  return Array.isArray(value) && value.every((v) => typeof v === "string") ? [...value] : null;
}

function readLine(raw: unknown): DisplayLine | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const description = str(r.description);
  const qty = num(r.qty);
  const mrp = num(r.mrp_paise);
  const offers = strings(r.offers);
  const saved = num(r.saved_paise);
  const amount = num(r.amount_paise);
  if (description === null || qty === null || mrp === null || offers === null) return null;
  if (saved === null || amount === null) return null;
  return { description, qty, mrp_paise: mrp, offers, saved_paise: saved, amount_paise: amount };
}

function readReturn(raw: unknown): DisplayReturn | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const description = str(r.description);
  const qty = num(r.qty);
  const amount = num(r.amount_paise);
  if (description === null || qty === null || amount === null) return null;
  return { description, qty, amount_paise: amount };
}

function readAll<T>(raw: unknown, read: (item: unknown) => T | null): T[] | null {
  if (!Array.isArray(raw)) return null;
  const out: T[] = [];
  for (const item of raw) {
    const picked = read(item);
    if (picked === null) return null;
    out.push(picked);
  }
  return out;
}

/** A view as it arrives, cut back to exactly the fields above; null if it is
 *  not one. */
export function readView(raw: unknown): DisplayView | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (r.screen === "welcome") return WELCOME;
  if (r.screen === "thanks") return THANKS;
  if (r.screen !== "bill") return null;
  const lines = readAll(r.lines, readLine);
  const returns = readAll(r.returns, readReturn);
  const offers = strings(r.offers);
  const savings = num(r.savings_paise);
  const total = num(r.total_paise);
  if (!lines || !returns || !offers || savings === null || total === null) return null;
  let upi: DisplayUpi | null = null;
  if (r.upi !== null && r.upi !== undefined) {
    const u = r.upi as Record<string, unknown>;
    const amount = num(u?.amount_paise);
    const qr = str(u?.qr);
    if (amount === null || qr === null) return null;
    upi = { amount_paise: amount, qr };
  }
  return {
    screen: "bill",
    lines,
    returns,
    offers,
    savings_paise: savings,
    total_paise: total,
    upi,
  };
}

function readQuestion(raw: unknown): DisplayQuestion | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const id = str(r.id);
  const text = str(r.text);
  const choices = readAll(r.choices, (c) => {
    if (!c || typeof c !== "object") return null;
    const value = str((c as Record<string, unknown>).value);
    const label = str((c as Record<string, unknown>).label);
    return value === null || label === null ? null : { value, label };
  });
  if (!id || text === null || !choices || !choices.length) return null;
  return { id, text, choices };
}

/** A message from the till, cut back to its own shape; null if it is not one. */
export function readTillMessage(raw: unknown): TillToDisplay | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  switch (r.type) {
    case "view": {
      const view = readView(r.view);
      return view ? { type: "view", view } : null;
    }
    case "ask": {
      const question = readQuestion(r.question);
      return question ? { type: "ask", question } : null;
    }
    case "unask": {
      const id = str(r.id);
      return id ? { type: "unask", id } : null;
    }
    case "bye":
      return { type: "bye" };
    default:
      return null;
  }
}

/** A message from the display, cut back to its own shape; null if it is not one. */
export function readDisplayMessage(raw: unknown): DisplayToTill | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (r.type === "hello") return { type: "hello" };
  if (r.type === "answer") {
    const id = str(r.id);
    const value = str(r.value);
    return id && value !== null ? { type: "answer", id, value } : null;
  }
  return null;
}

// --- the two ends of the channel -----------------------------------------------

/** What both ends need of a `BroadcastChannel` - small, so a test can fake it. */
export interface ChannelLike {
  postMessage(message: unknown): void;
  close(): void;
  onmessage: ((event: { data: unknown }) => void) | null;
}

/** Post, and never let the display's end throw into the till's. */
function post(channel: ChannelLike, message: unknown): void {
  try {
    channel.postMessage(message);
  } catch {
    // A closed channel or a message the browser would not clone: the display
    // misses one update and catches up on the next. The bill is not touched.
  }
}

interface OpenAsk {
  question: DisplayQuestion;
  resolve: (value: string | null) => void;
}

/**
 * The till's end. `show` sends the current view; `ask` puts a question on the
 * display and resolves with the customer's answer, or null if the question is
 * withdrawn or the till's counter closes first. Nothing here is awaited by a
 * bill.
 */
export class TillDisplayLink {
  private view: DisplayView = WELCOME;
  private readonly asks = new Map<string, OpenAsk>();
  private closed = false;

  constructor(private readonly channel: ChannelLike) {
    channel.onmessage = (event) => this.hear(event.data);
  }

  show(view: DisplayView): void {
    if (this.closed) return;
    this.view = view;
    post(this.channel, { type: "view", view } satisfies TillToDisplay);
  }

  ask(question: DisplayQuestion): Promise<string | null> {
    if (this.closed) return Promise.resolve(null);
    this.asks.get(question.id)?.resolve(null);
    return new Promise((resolve) => {
      this.asks.set(question.id, { question, resolve });
      post(this.channel, { type: "ask", question } satisfies TillToDisplay);
    });
  }

  withdraw(id: string): void {
    const open = this.asks.get(id);
    if (!open) return;
    this.asks.delete(id);
    open.resolve(null);
    post(this.channel, { type: "unask", id } satisfies TillToDisplay);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const open of this.asks.values()) open.resolve(null);
    this.asks.clear();
    post(this.channel, { type: "bye" } satisfies TillToDisplay);
    this.channel.onmessage = null;
    try {
      this.channel.close();
    } catch {
      // Already closed.
    }
  }

  private hear(raw: unknown): void {
    const message = readDisplayMessage(raw);
    if (!message) return;
    if (message.type === "hello") {
      post(this.channel, { type: "view", view: this.view } satisfies TillToDisplay);
      for (const open of this.asks.values()) {
        post(this.channel, { type: "ask", question: open.question } satisfies TillToDisplay);
      }
      return;
    }
    const open = this.asks.get(message.id);
    if (!open || !open.question.choices.some((choice) => choice.value === message.value)) return;
    this.asks.delete(message.id);
    open.resolve(message.value);
  }
}

/** Where the display stands: the bill on the counter and any open question. */
export interface DisplayState {
  view: DisplayView;
  question: DisplayQuestion | null;
}

export const DISPLAY_START: DisplayState = { view: WELCOME, question: null };

/** The display's state after one message from the till. */
export function displayStateAfter(state: DisplayState, message: TillToDisplay): DisplayState {
  switch (message.type) {
    case "view":
      return { ...state, view: message.view };
    case "ask":
      return { ...state, question: message.question };
    case "unask":
      return state.question?.id === message.id ? { ...state, question: null } : state;
    case "bye":
      return DISPLAY_START;
  }
}

/**
 * The display's end. It says hello on opening, so a display opened (or
 * reloaded) mid-bill catches up at once, and hands every message it accepts to
 * `onState`.
 */
export class DisplayScreenLink {
  private state: DisplayState = DISPLAY_START;

  constructor(
    private readonly channel: ChannelLike,
    private readonly onState: (state: DisplayState) => void,
  ) {
    channel.onmessage = (event) => this.hear(event.data);
    post(channel, { type: "hello" } satisfies DisplayToTill);
  }

  answer(id: string, value: string): void {
    if (this.state.question?.id !== id) return;
    post(this.channel, { type: "answer", id, value } satisfies DisplayToTill);
    this.state = { ...this.state, question: null };
    this.onState(this.state);
  }

  close(): void {
    this.channel.onmessage = null;
    try {
      this.channel.close();
    } catch {
      // Already closed.
    }
  }

  private hear(raw: unknown): void {
    const message = readTillMessage(raw);
    if (!message) return;
    this.state = displayStateAfter(this.state, message);
    this.onState(this.state);
  }
}

/** A real channel where the browser has one; null where it does not, and then
 *  the till simply offers no display. */
export function openChannel(storeCode: string): ChannelLike | null {
  if (typeof BroadcastChannel === "undefined") return null;
  try {
    return new BroadcastChannel(displayChannelName(storeCode)) as unknown as ChannelLike;
  } catch {
    return null;
  }
}
