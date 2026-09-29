// Gift vouchers at the counter (store operations ticket 19, ST-POS-4).
//
// A voucher is sold at the till as its own document and spent on bills as a
// tender, in parts: each bill takes up to what the voucher holds and the rest
// stays on it. Both are online only (section 28): head office holds every
// voucher's balance, so the counter looks one up before it takes it and never
// takes one offline.
//
// The rule is shared with the server through one file of golden cases
// (`app/backend/sell/vectors/gift_vouchers.json`): the last day a voucher can be
// used, and what each voucher on a bill pays. Head office checks the bill again
// when it arrives and refuses it whole if a voucher cannot cover it.

/** The feature key, as the server's registry spells it. */
export const GIFT_VOUCHER_FEATURE = "gift-vouchers";

/** Section 28: what cannot be done offline, why, and what to do instead. */
export const GIFT_VOUCHER_OFFLINE_REFUSAL =
  "Offline, a gift voucher cannot be sold or taken: its balance is held at head office. " +
  "Take another payment, or wait for the connection.";

/** A voucher head office answered for, as this bill holds it. */
export interface GiftVoucherHeld {
  number: string;
  /** The check code on its slip, as typed: head office asks for it again when
   *  the bill arrives (the number alone runs in sequence, B311). */
  code: string;
  /** What it held when head office was asked. */
  balance_paise: number;
  /** The last day it can be used (YYYY-MM-DD). */
  valid_until: string;
}

/** A voucher on a priced bill: what it pays towards this bill. */
export interface GiftVoucherUse extends GiftVoucherHeld {
  pays_paise: number;
}

function calendarDay(value: string): { year: number; month: number; day: number } {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match?.[1] || !match[2] || !match[3]) throw new Error("Invalid voucher calendar day");
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (
    year < 1000 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > 31 ||
    isoDay(Date.UTC(year, month - 1, day)) !== value
  ) {
    throw new Error("Invalid voucher calendar day");
  }
  return { year, month, day };
}

/** The same day `months` calendar months on; the month's last day where it is shorter. */
export function addMonths(day: string, months: number): string {
  if (!Number.isInteger(months)) throw new Error("Voucher months must be an integer");
  const { year, month, day: date } = calendarDay(day);
  const index = month - 1 + months;
  const y = year + Math.floor(index / 12);
  const m = (((index % 12) + 12) % 12) + 1;
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return isoDay(Date.UTC(y, m - 1, Math.min(date, last)));
}

/** The last day a voucher sold on `issuedOn` can be used: the day before the
 *  same date `months` months on (sold 28 Sep 2026, used up to 27 Sep 2027). */
export function validUntil(issuedOn: string, months: number): string {
  const { year, month, day } = calendarDay(addMonths(issuedOn, months));
  return isoDay(Date.UTC(year, month - 1, day - 1));
}

/** Can a voucher whose last day is `validUntilDay` be used on `day`? */
export function usableOn(validUntilDay: string, day: string): boolean {
  return day <= validUntilDay;
}

/** What each voucher pays towards `owed`, in the order given: each up to what it
 *  holds, never more than is still owed. The rest stays on the voucher. */
export function voucherPays(owed: number, balances: readonly number[]): number[] {
  let left = Math.max(owed, 0);
  return balances.map((balance) => {
    const part = Math.max(0, Math.min(balance, left));
    left -= part;
    return part;
  });
}

/** The vouchers on a bill with what each pays, in the order they were taken. */
export function usesOf(vouchers: readonly GiftVoucherHeld[], owed: number): GiftVoucherUse[] {
  const pays = voucherPays(
    owed,
    vouchers.map((voucher) => voucher.balance_paise),
  );
  return vouchers.map((voucher, index) => {
    const paysPaise = pays[index];
    if (paysPaise === undefined) throw new Error("Voucher allocation is missing");
    return { ...voucher, pays_paise: paysPaise };
  });
}

/** Why a voucher on this bill can no longer pay on `day` (its last day passed
 *  while the bill was open), or "". Head office would refuse the bill whole. */
export function whyVouchersCannotPay(vouchers: readonly GiftVoucherHeld[], day: string): string {
  const lapsed = vouchers.find((voucher) => !usableOn(voucher.valid_until, day));
  return lapsed
    ? `${lapsed.number} could be used only up to ${dayWords(lapsed.valid_until)}. Take it off this bill and take another payment.`
    : "";
}

/** A voucher number as head office keeps it. */
export function normaliseVoucherNumber(typed: string): string {
  return typed.trim().toUpperCase();
}

/** 27 Sep 2027, as the customer reads the slip. */
function dayWords(iso: string): string {
  try {
    const { year, month, day } = calendarDay(iso);
    const monthName = [
      "Jan",
      "Feb",
      "Mar",
      "Apr",
      "May",
      "Jun",
      "Jul",
      "Aug",
      "Sep",
      "Oct",
      "Nov",
      "Dec",
    ][month - 1];
    return `${day} ${monthName ?? ""} ${year}`;
  } catch {
    return iso;
  }
}

function isoDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}
