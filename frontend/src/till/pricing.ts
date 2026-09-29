// MRP-inclusive tax arithmetic at the counter (#180, D10 step 3).
//
// The TypeScript mirror of `sell/pricing.py`, and it exists because the till
// prices a scan with no network: the price tag is GST-inclusive, so the counter
// works backwards from what the customer pays to the tax already sitting inside
// it.
//
// Two rules, both from `CONTEXT.md`, both easy to get subtly wrong:
//
// **Which slab.** 5% at or under ₹2,500 per piece and 18% above, decided on the
// GST-*exclusive*, post-discount, per-piece price. That looks circular - the base
// depends on the rate and the rate on the base - and resolves because the mapping
// is monotone: price the piece at the lower rate first, and if the base that
// produces is still inside the threshold, the lower rate is the right one.
//
// **Where the half-paisa goes.** The base is rounded half-up and the tax is the
// remainder, never rounded on its own, so base + tax is exactly what the customer
// handed over on every line.
//
// All of it in integers. A rate arrives as a two-decimal string ("5.00") and is
// carried as hundredths of a percent, so nothing here ever touches a float - a
// bill that came out a paise different from the server's recomputation would
// raise a `gst_mismatch` flag every single time.

import type { TillGstSlab, TillTaxRule, TillTaxSettings, TillTaxVersion } from "./types";

/** ₹2,500 a piece, as the fallback when no slab has reached the device yet. */
const FALLBACK_SLAB: TillGstSlab = {
  hsn_prefix: "",
  threshold_paise: 250000,
  rate_below: "5.00",
  rate_above: "18.00",
  effective_from: "2025-09-22",
};

export interface TaxSplit {
  /** The rate applied, in the two-decimal form the bill and the server quote. */
  rate: string;
  base_paise: number;
  gst_paise: number;
}

/** A two-decimal percentage as hundredths of a percent: "5.00" → 500. */
export function rateHundredths(rate: string): number {
  const [whole, fraction = ""] = rate.trim().split(".");
  return Number(whole) * 100 + Number((fraction + "00").slice(0, 2));
}

/** The GST-exclusive base inside a tax-inclusive amount, rounded half-up.
 *
 *  Integer arithmetic throughout: `x * 100 / (100 + rate)` in floating point is
 *  a paise adrift often enough to matter across a day's bills. */
export function baseFromInclusive(inclusivePaise: number, rate: string): number {
  const denominator = 10_000 + rateHundredths(rate);
  const numerator = inclusivePaise * 10_000;
  return Math.floor((2 * numerator + denominator) / (2 * denominator));
}

/** Split a tax-inclusive amount at a known rate. The tax is the remainder. */
export function splitInclusive(inclusivePaise: number, rate: string): TaxSplit {
  const base = baseFromInclusive(inclusivePaise, rate);
  return { rate, base_paise: base, gst_paise: inclusivePaise - base };
}

/**
 * Split a whole line, choosing the slab from its per-piece exclusive price.
 *
 * `qty` matters because the threshold is per piece: two ₹2,000 shirts on one line
 * are two 5% pieces, not one 18% line.
 */
export function splitLine(inclusivePaise: number, qty: number, slab: TillGstSlab): TaxSplit {
  if (qty <= 0) throw new Error("a line must carry a positive quantity to be priced");
  // The per-piece base in one step. Dividing by `qty` first and rounding, then
  // taking the tax out of that, rounds twice - and the second rounding can push a
  // piece across the ₹2,500 boundary the server would have left below it.
  const denominator = qty * (10_000 + rateHundredths(slab.rate_below));
  const baseAtLow = Math.floor((2 * inclusivePaise * 10_000 + denominator) / (2 * denominator));
  const rate = baseAtLow <= slab.threshold_paise ? slab.rate_below : slab.rate_above;
  return splitInclusive(inclusivePaise, rate);
}

/**
 * The slab in force on `when`, preferring the row written for this HSN.
 *
 * Date-effective by construction (Rule 11): a bill is taxed by the slab that was
 * live on the day it was billed, and the dataset deliberately ships slabs whose
 * date has not arrived so an offline counter reaches October's rate in October.
 *
 * Where this and the server could disagree: two slabs sharing an
 * `effective_from` and both matching the HSN. The server's ordering does not
 * break that tie, so this takes the longer prefix - the more specific rule - and
 * the daily applied-versus-rulebook check would catch it if head office ever
 * wrote such a pair.
 */
export function slabFor(slabs: TillGstSlab[], hsn: string, when: string): TillGstSlab {
  const live = slabs
    .filter((s) => s.effective_from <= when)
    .sort(
      (a, b) =>
        b.effective_from.localeCompare(a.effective_from) ||
        b.hsn_prefix.length - a.hsn_prefix.length,
    );
  return (
    live.find((s) => s.hsn_prefix && hsn && hsn.startsWith(s.hsn_prefix)) ??
    live.find((s) => !s.hsn_prefix) ??
    live[0] ??
    FALLBACK_SLAB
  );
}

// --- versioned tax settings (store operations ticket 03) -------------------

/** Version 1: the slab table above, which every bill used before versions. */
export const LEGACY_TAX_VERSION = 1;

/** Ticket 12: a rate-schedule rule - one rate for an HSN, whatever the price
 *  (accessories). The mirror of `masters.tax_settings.FLAT_RATE`. */
export const FLAT_RATE = "flat_rate";

/**
 * The saved-settings rule (ST-CMP-1; baseline, CA to confirm): a piece priced P
 * takes `rate_below` when P ÷ (1 + rate_below) ≤ the threshold, compared
 * exactly - `P × 10000 ≤ threshold × qty × (10000 + rate)` in hundredths of a
 * percent - so ₹2,625.00 and ₹2,624.99 take 5% and ₹2,625.01 takes 18% at a
 * ₹2,500 line. The mirror of `sell.pricing.split_by_value_before_tax`.
 */
export function splitByValueBeforeTax(
  inclusivePaise: number,
  qty: number,
  rule: Pick<TillTaxRule, "threshold_paise" | "rate_below" | "rate_above">,
): TaxSplit {
  if (qty <= 0) throw new Error("a line must carry a positive quantity to be priced");
  const low =
    BigInt(inclusivePaise) * 10_000n <=
    BigInt(rule.threshold_paise) * BigInt(qty) * BigInt(10_000 + rateHundredths(rule.rate_below));
  return splitInclusive(inclusivePaise, low ? rule.rate_below : rule.rate_above);
}

/** Ticket 12: an HSN is digits only, 4, 6 or 8 of them, unless the server
 *  says otherwise (`TillTaxSettings.hsn_digits`, `masters.hsn.is_hsn`). */
export const DEFAULT_HSN_DIGITS: readonly number[] = [4, 6, 8];

/** Is `code` an HSN at all? Blank, "NA" or "6205.20" is not: "no HSN". */
export function isHsn(code: string, digits: readonly number[] = DEFAULT_HSN_DIGITS): boolean {
  return /^[0-9]+$/.test(code) && digits.includes(code.length);
}

/** The rule a line's HSN falls under: the longest matching prefix, else a
 *  blank-prefix rule; a line with no HSN (blank, or not an HSN) matches none. */
export function taxRuleFor(
  version: TillTaxVersion,
  hsn: string,
  hsnDigits: readonly number[] = DEFAULT_HSN_DIGITS,
): TillTaxRule | null {
  const code = (hsn ?? "").trim();
  if (!isHsn(code, hsnDigits)) return null;
  const prefixed = version.rules
    .filter((r) => r.hsn_prefix && code.startsWith(r.hsn_prefix))
    .sort((a, b) => b.hsn_prefix.length - a.hsn_prefix.length);
  return prefixed[0] ?? version.rules.find((r) => !r.hsn_prefix) ?? null;
}

/** The saved version a bill of `day`, made at `at`, is taxed under at this
 *  store, or null for version 1 - always the answer while the switch is off.
 *  A version saved after `at` is left out, as the server leaves it out. */
export function taxVersionFor(
  settings: TillTaxSettings | null | undefined,
  day: string,
  at: Date = new Date(),
): TillTaxVersion | null {
  if (!settings?.rules_on) return null;
  const live = settings.versions.filter(
    (v) => v.applies_from <= day && (!v.saved_at || Date.parse(v.saved_at) <= at.getTime()),
  );
  return live.reduce<TillTaxVersion | null>(
    (best, v) => (best === null || v.version > best.version ? v : best),
    null,
  );
}

export interface LineTax {
  split: TaxSplit;
  /** A saved version had no rule for this HSN: it took the version's own
   *  "no rule" rate, and the server flags the bill. */
  ruleMissing: boolean;
}

/** One line's tax under the version the bill falls in (null: version 1). */
export function taxLine(
  version: TillTaxVersion | null,
  slabs: TillGstSlab[],
  hsn: string,
  inclusivePaise: number,
  qty: number,
  day: string,
  hsnDigits: readonly number[] = DEFAULT_HSN_DIGITS,
): LineTax {
  if (version === null) {
    return { split: splitLine(inclusivePaise, qty, slabFor(slabs, hsn, day)), ruleMissing: false };
  }
  const rule = taxRuleFor(version, hsn, hsnDigits);
  if (rule === null) {
    return { split: splitInclusive(inclusivePaise, version.unmatched_rate), ruleMissing: true };
  }
  if (rule.kind === FLAT_RATE) {
    // Ticket 12: the rate schedule - the scheduled rate whatever the price.
    return {
      split: splitInclusive(inclusivePaise, rule.rate ?? rule.rate_above),
      ruleMissing: false,
    };
  }
  return { split: splitByValueBeforeTax(inclusivePaise, qty, rule), ruleMissing: false };
}

/**
 * Today, on the counter's own clock.
 *
 * Not `new Date().toISOString().slice(0, 10)`, which is the day in **UTC**. A
 * shop in Deoghar is five and a half hours ahead, so from half past six every
 * evening - the busiest part of a retail day - the UTC date is already tomorrow.
 * A slab or an offer boundary read that way would move at 18:30 rather than at
 * midnight, and the server, which judges the same bill in `Asia/Kolkata`, would
 * disagree with every bill rung up that evening.
 *
 * The counter's own clock is also what grill Q3 asks for by name: the till
 * starts and stops an offer itself, offline, and "itself" has to mean the day it
 * is actually trading in.
 */
export function tillToday(now: Date = new Date()): string {
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
