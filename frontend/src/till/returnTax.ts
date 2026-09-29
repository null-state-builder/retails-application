// Exchange and return tax at the counter (store operations ticket 13, §6 ST-CMP-2).
//
// The till's twin of `sell/return_tax.py`. Both run the golden cases in
// `app/backend/sell/return_tax_vectors/` (`returnTax.vectors.test.ts` here,
// `tests/test_return_tax_vectors.py` there), because the counter works out an
// exchange offline and head office works it out again: a disagreement is a flag
// on a bill already printed. Baselines, CA to confirm; gate: CA sign-off.
//
//   · A piece coming back is reversed at the rate and value on its own bill -
//     what the customer paid for it (D2), the tax inside that at the rate that
//     bill charged. Never today's rule.
//   · Past the credit-note deadline - 30 November after the bill's financial
//     year, or the day Accounts recorded that year's annual return as filed for
//     the GSTIN when that is earlier (B10) - the value still comes back, with no
//     tax reduction: the leg carries no tax.
//   · A bill the bank partly paid (B60): the customer is credited only what they
//     paid for the piece; the bank's part is paid back into the bill and reversed
//     against the bank.
//   · Returns stay within the GSTIN that issued the bill unless the version says
//     otherwise; a store with no GSTIN recorded cannot issue a credit note.

import { refundShare } from "./exchange";
import { splitInclusive } from "./pricing";
import type { TillTaxSettings, TillTaxVersion } from "./types";

export const CROSS_GSTIN = "cross_gstin";
export const NO_GSTIN = "no_gstin";

/** The two choices of a tax version this ticket reads. */
export interface ReturnTaxOptions {
  cross_gstin_returns: boolean;
  /** `{gstin: {"26-27": "YYYY-MM-DD"}}`, as Accounts recorded them. */
  annual_return_filed: Record<string, Record<string, string>>;
}

export const BASELINE_OPTIONS: ReturnTaxOptions = {
  cross_gstin_returns: false,
  annual_return_filed: {},
};

/** A version's options as this module reads them; absent keys are the baselines. */
export function optionsFromJson(raw: Record<string, unknown> | null | undefined): ReturnTaxOptions {
  const filed: Record<string, Record<string, string>> = {};
  const byGstin = raw?.annual_return_filed;
  if (byGstin && typeof byGstin === "object") {
    for (const [gstin, years] of Object.entries(byGstin as Record<string, unknown>)) {
      if (!years || typeof years !== "object") continue;
      for (const [fy, day] of Object.entries(years as Record<string, unknown>)) {
        if (typeof day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(day)) {
          (filed[gstin] ??= {})[fy] = day;
        }
      }
    }
  }
  return { cross_gstin_returns: raw?.cross_gstin_returns === true, annual_return_filed: filed };
}

/** The options of the newest saved version in force on `day` (at `at`) - read
 *  whatever the store's tax-settings switch, because a filing date is a fact,
 *  not a rate (the server's `exchange_tax.options_at`). */
export function optionsAt(
  settings: TillTaxSettings | null | undefined,
  day: string,
  at: Date = new Date(),
): ReturnTaxOptions {
  const live = (settings?.versions ?? []).filter(
    (v) => v.applies_from <= day && (!v.saved_at || Date.parse(v.saved_at) <= at.getTime()),
  );
  const newest = live.reduce<TillTaxVersion | null>(
    (best, v) => (best === null || v.version > best.version ? v : best),
    null,
  );
  return optionsFromJson((newest?.options as Record<string, unknown> | undefined) ?? null);
}

/** `26-27` for a day `YYYY-MM-DD` (April to March). */
export function financialYearOf(day: string): string {
  const year = Number(day.slice(0, 4));
  const month = Number(day.slice(5, 7));
  const start = month >= 4 ? year : year - 1;
  const two = (n: number) => String(n % 100).padStart(2, "0");
  return `${two(start)}-${two(start + 1)}`;
}

/** The last day a credit note may reduce tax for a bill of `originalDay` (B10). */
export function creditNoteDeadline(
  originalDay: string,
  gstin: string,
  options: ReturnTaxOptions,
): string {
  const fy = financialYearOf(originalDay);
  const closes = 2000 + Number(fy.slice(3, 5));
  const statutory = `${closes}-11-30`;
  const filed = options.annual_return_filed[gstin]?.[fy];
  return filed && filed < statutory ? filed : statutory;
}

export function isLate(
  originalDay: string,
  exchangeDay: string,
  gstin: string,
  options: ReturnTaxOptions,
): boolean {
  return exchangeDay > creditNoteDeadline(originalDay, gstin, options);
}

/** Why this store may not take back a bill of that GSTIN, as a code, or null. */
export function gstinRefusal(
  originalGstin: string,
  billingGstin: string,
  options: ReturnTaxOptions,
): string | null {
  if (!billingGstin.trim() || !originalGstin.trim()) return NO_GSTIN;
  if (originalGstin.trim() !== billingGstin.trim() && !options.cross_gstin_returns) {
    return CROSS_GSTIN;
  }
  return null;
}

/** The words the counter shows for a refusal it can work out on its own. */
export function refusalWords(code: string, original: { gstin: string; doc: string }): string {
  if (code === NO_GSTIN) {
    return (
      "This store has no GSTIN recorded, so no credit note can be issued for this return. " +
      "Head office must record the store's GSTIN first."
    );
  }
  return (
    `Bill ${original.doc} was issued under GSTIN ${original.gstin}. A return is taken only ` +
    "within the GSTIN that issued the bill, so it can be returned only at a store under that GSTIN."
  );
}

/**
 * Each sold line's share of a bill's bank offers, `{line_no: paise}` (B60).
 * Spread by value, spare paisa by largest remainder, a tie to the earlier line
 * (B9). Integers only: `bank × net` stays far inside a double's exact range.
 */
export function bankShares(
  lines: { line_no: number; net_paise: number }[],
  bankOfferPaise: number,
): Record<number, number> {
  const total = lines.reduce((n, line) => n + line.net_paise, 0);
  const shares: Record<number, number> = {};
  if (bankOfferPaise <= 0 || total <= 0) {
    for (const line of lines) shares[line.line_no] = 0;
    return shares;
  }
  for (const line of lines) {
    shares[line.line_no] = Math.floor((bankOfferPaise * line.net_paise) / total);
  }
  let spare = bankOfferPaise - Object.values(shares).reduce((n, v) => n + v, 0);
  const byRemainder = [...lines].sort(
    (a, b) =>
      ((bankOfferPaise * b.net_paise) % total) - ((bankOfferPaise * a.net_paise) % total) ||
      a.line_no - b.line_no,
  );
  for (const line of byRemainder) {
    if (spare <= 0) break;
    const previous = shares[line.line_no];
    if (previous === undefined) throw new Error("Bank offer share is missing");
    shares[line.line_no] = previous + 1;
    spare -= 1;
  }
  return shares;
}

/** One sold line of the original bill, as it stands now. */
export interface ReturnableLine {
  line_no: number;
  qty: number;
  net_paise: number;
  gst_rate: string;
  returned_qty: number;
  returned_paise: number;
  /** The line's share of the bill's bank offers (`bankShares`). */
  bank_offer_paise?: number;
  /** What earlier returns of this line took off that share. */
  returned_bank_paise?: number;
}

export interface ReturnLeg {
  original_line: number;
  qty: number;
  refund_paise: number;
  gst_rate: string;
  gst_paise: number;
  bank_offer_paise: number;
  late: boolean;
}

/** The leg for `qty` pieces of `line`: value, tax reversed, the bank's part. */
export function returnLeg(line: ReturnableLine, qty: number, late: boolean): ReturnLeg {
  const refund = refundShare({
    paid_paise: line.net_paise,
    line_qty: line.qty,
    returning: qty,
    returned_qty: line.returned_qty,
    returned_paise: line.returned_paise,
  });
  const bank = refundShare({
    paid_paise: line.bank_offer_paise ?? 0,
    line_qty: line.qty,
    returning: qty,
    returned_qty: line.returned_qty,
    returned_paise: line.returned_bank_paise ?? 0,
  });
  return {
    original_line: line.line_no,
    qty,
    refund_paise: refund,
    gst_rate: line.gst_rate,
    gst_paise: late ? 0 : splitInclusive(refund, line.gst_rate).gst_paise,
    bank_offer_paise: bank,
    late,
  };
}

/** The credit note an exchange issues: what it reverses. */
export interface CreditNoteTotals {
  value_paise: number;
  taxable_paise: number;
  gst_paise: number;
  bank_offer_paise: number;
  /** What the customer is credited: the value less the bank's part. */
  credit_paise: number;
  late: boolean;
}

export function creditNoteTotals(
  legs: { refund_paise: number; gst_paise: number; bank_offer_paise?: number; late?: boolean }[],
): CreditNoteTotals {
  const value = legs.reduce((n, leg) => n + leg.refund_paise, 0);
  const gst = legs.reduce((n, leg) => n + leg.gst_paise, 0);
  const bank = legs.reduce((n, leg) => n + (leg.bank_offer_paise ?? 0), 0);
  return {
    value_paise: value,
    taxable_paise: value - gst,
    gst_paise: gst,
    bank_offer_paise: bank,
    credit_paise: value - bank,
    late: legs.some((leg) => leg.late === true),
  };
}
