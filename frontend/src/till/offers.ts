// The offer engine at the counter (#183, D5, grill Q9).
//
// This file is a port of `app/backend/offers/resolution.py`, function for
// function, and the two are held together by `app/backend/offers/vectors/*.json`
// - the same golden carts, run through both engines, compared to the paisa
// (`offers.vectors.test.ts`). That pairing is the whole point of the slice: the
// same cart is priced twice, once here on a counter machine that may not have
// seen head office for a day, and again on the server when the bill syncs. If
// the two ever disagree the receipt and the books disagree, and the receipt is
// already in a bag on a bus to Deoghar.
//
// So: **no changes here without the same change there, and a vector that proves
// it.** The comments are deliberately thinner than the Python's - that module is
// where the reasoning lives, and two copies of an explanation drift apart faster
// than two copies of an algorithm.
//
// Integer paise everywhere (ADR-0004). Percentages are two-decimal strings and
// become hundredths of a percent; half-up is written as integer arithmetic so
// the two languages round the same way rather than the same way usually.

import type { NoOffer, OfferCredit, OfferEvidence, StackedCredit, TillOffer } from "./types";

export const LAYER_BRAND = "brand";
/** Layer 1 claims a line; these two stack onto what it left, in this order. */
export const ADD_ON_LAYERS = ["storewide", "bank"] as const;

const TRIGGER_NONE = "none";
const TRIGGER_SPEND = "spend";
const TRIGGER_QTY = "qty";
const TRIGGER_GROUP = "group";

const REWARD_PCT_OFF = "pct_off";
const REWARD_AMT_OFF = "amt_off";
const REWARD_ITEM_FREE = "item_free";
const REWARD_FIXED_PRICE = "fixed_price";
const REWARD_GIFT = "gift";

/** What an add-on may do to a price a brand offer has already reduced. */
const ADD_ON_REWARDS = new Set([REWARD_PCT_OFF, REWARD_AMT_OFF]);
const FALLBACK_REWARDS = new Set([REWARD_PCT_OFF, REWARD_AMT_OFF, REWARD_FIXED_PRICE]);

// --- GST after discount (store operations ticket 11) -----------------------
// Mirrors the block of the same name in `resolution.py`; the reasoning is there.

/** A buy-X-get-Y offer's `reward_config.allocation`: how each group's price is
 *  shared for tax. `by_mrp` (the default) or `free_piece` (as before). */
export const ALLOCATION_BY_MRP = "by_mrp";
export const ALLOCATION_FREE_PIECE = "free_piece";

export function allocationOf(rule: TillOffer): string {
  const value = String((rule.reward_config ?? {}).allocation ?? ALLOCATION_BY_MRP);
  return value === ALLOCATION_FREE_PIECE ? ALLOCATION_FREE_PIECE : ALLOCATION_BY_MRP;
}

/** A bank-layer offer's `reward_config.reduces_value`. Anything but `true` is
 *  No, the default: a payment, not a price cut. */
export function reducesValue(rule: TillOffer): boolean {
  return (rule.reward_config ?? {}).reduces_value === true;
}

/** One row of the bill, as the rulebook needs to see it. */
export interface OfferCartLine {
  line_no: number;
  brand: string;
  item: string;
  design: string;
  size: string;
  color: string;
  barcode: string;
  season: string;
  qty: number;
  mrp_paise: number;
  no_discount: boolean;
}

export interface OfferCart {
  lines: OfferCartLine[];
  /** The till's own clock, `YYYY-MM-DD`. Every rule's dates are judged against
   *  this and nothing else, so an offline counter stops a dead offer on time. */
  day: string;
  /** Gift offers the counter could not honour, by id (D5 Q11). */
  declinedGifts?: number[];
}

// The evidence shapes live in `./types`, with the rest of what travels to the
// server: they are written here but read there, months later, by the daily
// applied-versus-rulebook check.
export type { NoOffer, OfferCredit, OfferEvidence, StackedCredit };

export interface Entitlement {
  offer_id: number;
  offer_name: string;
  barcode: string;
  token_price_paise: number;
}

export interface LineOutcome {
  line_no: number;
  discount_paise: number;
  /** The brand-layer winner - the one the sale line's `offer_id` records. */
  offer_id: number | null;
  /** `{}` when nothing applied, matching what the server writes. */
  evidence: OfferEvidence | NoOffer;
}

/** A bank instant discount recorded as a payment, not a price cut (ticket 11). */
export interface BankOffer {
  offer_id: number;
  offer_name: string;
  amount_paise: number;
}

export interface Resolution {
  lines: LineOutcome[];
  entitlements: Entitlement[];
  total_discount_paise: number;
  /** Only on a cart resolved after discount - so every older vector keeps its
   *  exact shape. Each becomes a "bank offer" tender on the bill. */
  bank_offers?: BankOffer[];
}

export interface ResolveOptions {
  /** Ticket 11: the store's `gst-after-discount` switch, as the bill records it. */
  afterDiscount?: boolean;
}

// --- integer arithmetic, mirrored from Python ------------------------------

/** `"7.50"` → 750. Off the digits, never through a float (ADR-0004). */
export function hundredths(value: unknown): number {
  const text = String(value ?? "").trim();
  if (!text) return 0;
  const sign = text.startsWith("-") ? -1 : 1;
  const [whole = "", frac = ""] = text.replace(/^[+-]+/, "").split(".");
  const w = whole || "0";
  const f = (frac + "00").slice(0, 2);
  if (!/^\d+$/.test(w) || !/^\d+$/.test(f)) throw new Error(`'${text}' is not a percentage`);
  return sign * (Number(w) * 100 + Number(f));
}

/** `amount x percent`, half-up, in integers only. */
export function percentOf(amountPaise: number, percentHundredths: number): number {
  if (amountPaise <= 0 || percentHundredths <= 0) return 0;
  return Math.floor((2 * amountPaise * percentHundredths + 10_000) / 20_000);
}

/** A brand or category name reduced to what two spellings of it share. */
function normalise(text: unknown): string {
  return String(text ?? "")
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "");
}

function names(scope: Record<string, unknown>, key: string): Set<string> {
  const raw = scope[key];
  if (!raw) return new Set();
  const list = Array.isArray(raw) ? raw : [raw];
  return new Set(list.map(normalise).filter(Boolean));
}

// --- scope -----------------------------------------------------------------

/** The explicit "we do not know which buying cohort this came from" season
 *  (store and warehouse operations PRD §4), by the two spellings a line can
 *  carry it under. The engine reads no master, here or on the server, so the
 *  sentinel is named in both twins - see `offers/resolution.py`. */
export const UNKNOWN_HISTORICAL_SEASON_CODE = "UNKNOWN-HIST";
export const UNKNOWN_HISTORICAL_SEASON_NAME = "Unknown historical season";

export function isUnknownHistoricalSeason(value: unknown): boolean {
  const text = normalise(value);
  return (
    text === normalise(UNKNOWN_HISTORICAL_SEASON_CODE) ||
    text === normalise(UNKNOWN_HISTORICAL_SEASON_NAME)
  );
}

function facets(line: OfferCartLine): [string, string][] {
  return [
    ["brands", line.brand],
    ["categories", line.item],
    ["styles", line.design],
    ["sizes", line.size],
    ["colors", line.color],
    ["barcodes", line.barcode],
    ["seasons", line.season],
  ];
}

export function covers(rule: TillOffer, line: OfferCartLine, day: string): boolean {
  if (day < rule.starts_on) return false;
  if (rule.ends_on !== null && day > rule.ends_on) return false;
  // The AMM/NOD flag beats every rule there is, storewide included (D5 Q3).
  if (line.no_discount) return false;
  if (line.qty <= 0 || line.mrp_paise <= 0) return false;
  if (rule.brand && normalise(rule.brand) !== normalise(line.brand)) return false;

  const scope = rule.item_scope ?? {};
  // A piece whose cohort was never established is outside every offer that
  // names a season, including one that names the unknown season itself. An
  // offer with no season scope still reaches it.
  if (isUnknownHistoricalSeason(line.season) && names(scope, "seasons").size) return false;
  for (const [key, value] of facets(line)) {
    const wanted = names(scope, key);
    if (wanted.size && !wanted.has(normalise(value))) return false;
  }
  const floor = scope.mrp_min_paise;
  if (floor != null && line.mrp_paise < Number(floor)) return false;
  const ceiling = scope.mrp_max_paise;
  if (ceiling != null && line.mrp_paise > Number(ceiling)) return false;

  const exclude = (scope.exclude ?? {}) as Record<string, unknown>;
  return !facets(line).some(([key, value]) => names(exclude, key).has(normalise(value)));
}

// --- proposals -------------------------------------------------------------

interface Proposal {
  rule: TillOffer;
  shares: Map<number, number>;
  total: number;
}

type Dials = Record<string, unknown>;

function slabsOf(rule: TillOffer): Dials[] {
  const raw = (rule.trigger_config ?? {}).slabs;
  return Array.isArray(raw) ? (raw as Dials[]) : [];
}

/** The step of a ladder this cart has reached; `null` means none of them. */
function reachedSlab(rule: TillOffer, spendPaise: number, units: number): Dials | null {
  if (rule.trigger_type === TRIGGER_NONE || rule.trigger_type === TRIGGER_GROUP) return {};
  let key: string;
  let reached: number;
  if (rule.trigger_type === TRIGGER_SPEND) {
    key = "min_paise";
    reached = spendPaise;
  } else if (rule.trigger_type === TRIGGER_QTY) {
    key = "min_qty";
    reached = units;
  } else {
    return null;
  }
  const qualifying = slabsOf(rule)
    .filter((slab) => reached >= Number(slab[key] ?? 0))
    .sort((a, b) => Number(a[key] ?? 0) - Number(b[key] ?? 0));
  return qualifying.length ? qualifying[qualifying.length - 1] : null;
}

/** The step this set of lines reaches. Always measured on printed MRP (D5 Q13). */
function slabFor(rule: TillOffer, covered: OfferCartLine[]): Dials | null {
  return reachedSlab(
    rule,
    covered.reduce((n, l) => n + l.mrp_paise * l.qty, 0),
    covered.reduce((n, l) => n + l.qty, 0),
  );
}

/** The reward's dials, with the reached step's own values on top. */
function rewardParams(rule: TillOffer, slab: Dials): Dials {
  const params: Dials = { ...(rule.reward_config ?? {}) };
  for (const [key, value] of Object.entries(slab)) {
    if (!key.startsWith("min_")) params[key] = value;
  }
  return params;
}

/**
 * Share a lump sum across lines in proportion to what they are worth.
 *
 * Largest-remainder, so the parts sum to exactly the lump. The spare paisa goes
 * to the dearest line first, then the earliest - arbitrary, but it has to be
 * decided or the two engines disagree by a paisa on a three-line bill.
 */
function spread(
  amountPaise: number,
  weights: [number, number][],
  earlierFirst = false,
): Map<number, number> {
  const totalWeight = weights.reduce((n, [, w]) => n + w, 0);
  const shares = new Map<number, number>();
  if (amountPaise <= 0 || totalWeight <= 0) return shares;
  const remainders: [number, number, number][] = [];
  for (const [lineNo, weight] of weights) {
    const numerator = amountPaise * weight;
    shares.set(lineNo, Math.floor(numerator / totalWeight));
    remainders.push([numerator % totalWeight, weight, lineNo]);
  }
  let spare = amountPaise - [...shares.values()].reduce((n, v) => n + v, 0);
  // Baseline B9 (ticket 11): a tie goes to the earlier line, whatever it is worth.
  remainders.sort((a, b) =>
    earlierFirst ? b[0] - a[0] || a[2] - b[2] : b[0] - a[0] || b[1] - a[1] || a[2] - b[2],
  );
  for (const [, , lineNo] of remainders) {
    if (spare <= 0) break;
    shares.set(lineNo, (shares.get(lineNo) ?? 0) + 1);
    spare -= 1;
  }
  return shares;
}

/** Buy X get Y's groups: what goes free per line, and how many of each line's
 *  pieces are in a completed group at all. Dearest first, the cheapest free. */
function groupUnits(
  lines: OfferCartLine[],
  base: Map<number, number>,
  config: Record<string, unknown>,
): { free: Map<number, number>; members: Map<number, number> } {
  const buy = Math.max(Number(config.buy ?? 0), 0);
  const get = Math.max(Number(config.get ?? 0), 0);
  const free = new Map<number, number>();
  const members = new Map<number, number>();
  if (buy <= 0 || get <= 0) return { free, members };
  const repeat = config.repeat === undefined ? true : Boolean(config.repeat);

  const units: [number, number][] = [];
  for (const line of lines) {
    const remaining = base.get(line.line_no) ?? 0;
    if (remaining <= 0) continue;
    const unitPrice = Math.floor(remaining / line.qty);
    for (let i = 0; i < line.qty; i += 1) units.push([unitPrice, line.line_no]);
  }
  units.sort((a, b) => b[0] - a[0] || a[1] - b[1]);

  const group = buy + get;
  let taken = 0;
  while (units.length - taken >= group) {
    for (const [, lineNo] of units.slice(taken, taken + group)) {
      members.set(lineNo, (members.get(lineNo) ?? 0) + 1);
    }
    for (const [price, lineNo] of units.slice(taken + buy, taken + group)) {
      free.set(lineNo, (free.get(lineNo) ?? 0) + price);
    }
    taken += group;
    if (!repeat) break;
  }
  return { free, members };
}

/** Buy X get Y as several goods sold for one price (ticket 11): each group's
 *  price spread over its pieces by MRP, spare paisa to the earlier line (B9). */
function spreadGroupByMrp(
  lines: OfferCartLine[],
  base: Map<number, number>,
  free: Map<number, number>,
  members: Map<number, number>,
): Map<number, number> {
  const qtyOf = new Map(lines.map((line) => [line.line_no, line.qty]));
  const worth: [number, number][] = [...members.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([lineNo, units]) => [
      lineNo,
      Math.floor((base.get(lineNo) ?? 0) / (qtyOf.get(lineNo) ?? 1)) * units,
    ]);
  const price =
    worth.reduce((n, [, w]) => n + w, 0) - [...free.values()].reduce((n, v) => n + v, 0);
  const shares = spread(price, worth, true);
  return new Map(worth.map(([lineNo, value]) => [lineNo, value - (shares.get(lineNo) ?? 0)]));
}

function sharesFor(
  rewardType: string,
  rule: TillOffer,
  covered: OfferCartLine[],
  base: Map<number, number>,
  params: Dials,
  afterDiscount = false,
): Map<number, number> {
  const shares = new Map<number, number>();
  if (rewardType === REWARD_PCT_OFF) {
    const percent = hundredths(params.percent ?? "0");
    for (const line of covered) {
      shares.set(line.line_no, percentOf(base.get(line.line_no) ?? 0, percent));
    }
    return shares;
  }
  if (rewardType === REWARD_AMT_OFF) {
    return spread(
      Number(params.amount_paise ?? 0),
      covered.map((line) => [line.line_no, base.get(line.line_no) ?? 0]),
      afterDiscount,
    );
  }
  if (rewardType === REWARD_FIXED_PRICE) {
    const price = Number(params.price_paise ?? 0);
    for (const line of covered) {
      const perUnit = Math.max(Math.floor((base.get(line.line_no) ?? 0) / line.qty) - price, 0);
      shares.set(line.line_no, perUnit * line.qty);
    }
    return shares;
  }
  if (rewardType === REWARD_ITEM_FREE) {
    const { free, members } = groupUnits(covered, base, rule.trigger_config ?? {});
    if (afterDiscount && allocationOf(rule) === ALLOCATION_BY_MRP) {
      return spreadGroupByMrp(covered, base, free, members);
    }
    return free;
  }
  return shares;
}

function propose(
  rule: TillOffer,
  lines: OfferCartLine[],
  base: Map<number, number>,
  day: string,
  afterDiscount = false,
): Proposal | null {
  const covered = lines.filter((line) => covers(rule, line, day) && (base.get(line.line_no) ?? 0) > 0);
  if (!covered.length) return null;
  const slab = slabFor(rule, covered);
  if (slab === null) return null;

  const raw = sharesFor(
    rule.reward_type,
    rule,
    covered,
    base,
    rewardParams(rule, slab),
    afterDiscount,
  );
  const shares = new Map<number, number>();
  for (const [lineNo, paise] of raw) {
    // No rule may give away more of a line than the line still costs.
    if (paise > 0) shares.set(lineNo, Math.min(paise, base.get(lineNo) ?? 0));
  }
  if (!shares.size) return null;
  return { rule, shares, total: [...shares.values()].reduce((n, v) => n + v, 0) };
}

// --- gifts, which are earned rather than deducted --------------------------

function splitGifts(
  rules: TillOffer[],
  cart: OfferCart,
): { keep: TillOffer[]; earned: Entitlement[] } {
  const declined = new Set(cart.declinedGifts ?? []);
  const keep: TillOffer[] = [];
  const earned: Entitlement[] = [];
  for (const rule of rules) {
    if (rule.reward_type !== REWARD_GIFT) {
      keep.push(rule);
      continue;
    }
    const covered = cart.lines.filter((line) => covers(rule, line, cart.day));
    if (!covered.length) continue;
    const slab = slabFor(rule, covered);
    if (slab === null) continue;
    const params = rewardParams(rule, slab);
    if (!declined.has(rule.id)) {
      earned.push({
        offer_id: rule.id,
        offer_name: rule.name,
        barcode: String(params.gift_barcode ?? ""),
        token_price_paise: Number(params.token_price_paise ?? 0),
      });
      continue;
    }
    const fallback = (params.fallback ?? {}) as Dials;
    const rewardType = String(fallback.reward_type ?? "");
    if (!FALLBACK_REWARDS.has(rewardType)) continue;
    keep.push({
      ...rule,
      reward_type: rewardType,
      reward_config: (fallback.reward_config ?? {}) as Record<string, unknown>,
    });
  }
  return { keep, earned };
}

// --- the greedy loop -------------------------------------------------------

interface Awards {
  discount: Map<number, number>;
  winner: Map<number, Proposal>;
  stack: Map<number, [Proposal, number][]>;
  beat: Map<number, [Proposal, number][]>;
}

function push<T>(into: Map<number, T[]>, key: number, value: T): void {
  const found = into.get(key);
  if (found) found.push(value);
  else into.set(key, [value]);
}

/** One layer, resolved to a fixed point. See the Python for why it loops. */
function runLayer(
  rules: TillOffer[],
  cart: OfferCart,
  awards: Awards,
  exclusive: boolean,
  afterDiscount = false,
): void {
  const claimed = new Set<number>();
  let remaining = [...rules];
  let firstRound = true;

  while (remaining.length) {
    const openLines = cart.lines.filter((line) => !claimed.has(line.line_no));
    if (!openLines.length) break;
    const base = new Map(
      openLines.map((line) => [
        line.line_no,
        line.mrp_paise * line.qty - (awards.discount.get(line.line_no) ?? 0),
      ]),
    );
    const proposals = remaining
      .map((rule) => propose(rule, openLines, base, cart.day, afterDiscount))
      .filter((p): p is Proposal => p !== null)
      .sort(
        (a, b) => b.total - a.total || a.rule.priority - b.rule.priority || a.rule.id - b.rule.id,
      );
    if (!proposals.length) break;
    const best = proposals[0];

    if (firstRound && exclusive) {
      for (const other of proposals.slice(1)) {
        for (const [lineNo, paise] of [...other.shares].sort((a, b) => a[0] - b[0])) {
          push(awards.beat, lineNo, [other, paise]);
        }
      }
      firstRound = false;
    }

    for (const [lineNo, paise] of best.shares) {
      awards.discount.set(lineNo, (awards.discount.get(lineNo) ?? 0) + paise);
      if (exclusive) awards.winner.set(lineNo, best);
      else push(awards.stack, lineNo, [best, paise]);
      claimed.add(lineNo);
    }
    remaining = remaining.filter((rule) => rule.id !== best.rule.id);
  }
}

function evidenceFor(line: OfferCartLine, awards: Awards): OfferEvidence | NoOffer {
  const saved = awards.discount.get(line.line_no) ?? 0;
  if (saved <= 0) return {};
  const winner = awards.winner.get(line.line_no);
  return {
    offer_id: winner ? winner.rule.id : null,
    offer_name: winner ? winner.rule.name : "",
    layer: winner ? winner.rule.layer : "",
    saved_paise: saved,
    beat: (awards.beat.get(line.line_no) ?? [])
      .filter(([p]) => !winner || p.rule.id !== winner.rule.id)
      .map(([p, paise]) => ({
        offer_id: p.rule.id,
        offer_name: p.rule.name,
        saved_paise: paise,
      })),
    stack: (awards.stack.get(line.line_no) ?? []).map(([p, paise]) => ({
      offer_id: p.rule.id,
      offer_name: p.rule.name,
      layer: p.rule.layer,
      saved_paise: paise,
    })),
  };
}

/** Price a cart against the rulebook. The one entry point. */
export function resolveOffers(
  cart: OfferCart,
  rules: TillOffer[],
  options: ResolveOptions = {},
): Resolution {
  const afterDiscount = options.afterDiscount === true;
  const awards: Awards = {
    discount: new Map(),
    winner: new Map(),
    stack: new Map(),
    beat: new Map(),
  };

  const { keep, earned } = splitGifts(
    rules.filter((rule) => rule.layer === LAYER_BRAND),
    cart,
  );
  runLayer(keep, cart, awards, true, afterDiscount);

  let bank: BankOffer[] = [];
  for (const layer of ADD_ON_LAYERS) {
    const layerRules = rules.filter(
      (rule) => rule.layer === layer && rule.combinable && ADD_ON_REWARDS.has(rule.reward_type),
    );
    if (afterDiscount && layer === "bank") {
      bank = bankLayerAfterDiscount(layerRules, cart, awards);
      continue;
    }
    runLayer(layerRules, cart, awards, false, afterDiscount);
  }

  const lines = cart.lines.map((line) => ({
    line_no: line.line_no,
    discount_paise: awards.discount.get(line.line_no) ?? 0,
    offer_id: awards.winner.get(line.line_no)?.rule.id ?? null,
    evidence: evidenceFor(line, awards),
  }));
  return {
    lines,
    entitlements: earned.sort((a, b) => a.offer_id - b.offer_id),
    total_discount_paise: lines.reduce((n, l) => n + l.discount_paise, 0),
    ...(afterDiscount ? { bank_offers: bank } : {}),
  };
}

/** The bank layer with GST after discount (ticket 11): resolved in one pass as
 *  it always was, on a copy, then each winner sorted by its own setting - a
 *  value-reducing one is a line discount as before, every other one a "bank
 *  offer" payment. See `_bank_layer_after_discount` in Python. */
function bankLayerAfterDiscount(rules: TillOffer[], cart: OfferCart, awards: Awards): BankOffer[] {
  if (!rules.length) return [];
  const shadow: Awards = {
    discount: new Map(awards.discount),
    winner: new Map(),
    stack: new Map(),
    beat: new Map(),
  };
  runLayer(rules, cart, shadow, false, true);
  const totals = new Map<number, BankOffer>();
  for (const [lineNo, stacked] of shadow.stack) {
    for (const [proposal, paise] of stacked) {
      if (reducesValue(proposal.rule)) {
        awards.discount.set(lineNo, (awards.discount.get(lineNo) ?? 0) + paise);
        push(awards.stack, lineNo, [proposal, paise]);
        continue;
      }
      const seen = totals.get(proposal.rule.id);
      totals.set(proposal.rule.id, {
        offer_id: proposal.rule.id,
        offer_name: proposal.rule.name,
        amount_paise: (seen?.amount_paise ?? 0) + paise,
      });
    }
  }
  return [...totals.keys()].sort((a, b) => a - b).map((id) => totals.get(id) as BankOffer);
}
