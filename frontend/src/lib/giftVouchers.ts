/** Gift vouchers (store operations ticket 19, ST-POS-4): the words the Gift
 *  vouchers screen and the till show, and the slip the till prints. The server
 *  decides every number, date and balance; these only say them. */

import type { ApiRead, ApiSchemas } from "./api";
import { formatINR, rupeesToPaise } from "./format";
import { dayText } from "./reservations";
import type { Tender } from "./reservations";

export type GiftVoucher = ApiRead<ApiSchemas["GiftVoucher"]>;
export type GiftVoucherListing = ApiRead<ApiSchemas["GiftVoucherList"]>;
/** As the till that sold it gets it back, once: with the code for the slip. */
export type GiftVoucherSold = ApiRead<ApiSchemas["GiftVoucherSold"]>;

export const GIFT_VOUCHERS_API = "/sell/gift-vouchers";
export const GIFT_VOUCHER_LOOKUP_API = "/sell/gift-vouchers/lookup";

/** Online only (§28): said the same way on the screen and at the till. */
export const GIFT_VOUCHERS_OFFLINE =
  "Gift vouchers need the internet. Nothing can be sold, taken or looked up until it is back; what you typed stays here.";

/** Said on every voucher, word for word as the server says it. */
export const NO_GST_WORDS =
  "No GST is charged on a gift voucher. GST is charged on the goods bought with it (Circular 243/37/2024).";

const STATE: Record<string, { label: string; tone: string }> = {
  active: { label: "Usable", tone: "green" },
  used_up: { label: "Used up", tone: "navy" },
  expired: { label: "Expired", tone: "amber" },
};

/** The state as a chip: words and a tone, never colour alone. */
export function stateChip(state: string): { label: string; tone: string } {
  return STATE[state] ?? { label: state, tone: "navy" };
}

const MOVEMENT: Record<string, string> = {
  issued: "Sold",
  used: "Used on a bill",
  expired: "Expired unused (no GST)",
};

export function movementText(m: { kind: string; sale_doc_number: string | null }): string {
  const words = MOVEMENT[m.kind] ?? m.kind;
  return m.sale_doc_number ? `${words} ${m.sale_doc_number}` : words;
}

/** What is left on it, in words. */
export function balanceText(
  v: Pick<GiftVoucher, "state" | "balance_paise" | "expired_paise">,
): string {
  if (v.state === "expired") {
    const lapsed = v.expired_paise + v.balance_paise;
    return lapsed > 0 ? `${formatINR(lapsed)} expired unused` : "Nothing was left";
  }
  if (v.state === "used_up") return "Nothing left";
  return `${formatINR(v.balance_paise)} left`;
}

export interface SellDraft {
  value: string;
  mode: Tender;
  reference: string;
}

export function emptySellDraft(): SellDraft {
  return { value: "", mode: "cash", reference: "" };
}

/** What is wrong with the draft before it is sent, or "" when it can go. */
export function sellProblem(draft: SellDraft): string {
  const paise = rupeesToPaise(draft.value);
  if (paise === null || paise <= 0) return "Type the voucher's value in rupees.";
  return "";
}

/** The voucher slip the till prints: number, value, last day, and the tax words. */
export function voucherSlipHtml(v: GiftVoucherSold): string {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${esc(v.number)}</title>
<style>
  @page { size: 80mm auto; margin: 0; }
  body { box-sizing: border-box; width: 80mm; margin: 0; padding: 4mm; font: 12px/1.45 "Helvetica Neue", Arial, sans-serif; text-align: center; }
  h1 { font-size: 15px; }
  .big { font-size: 22px; font-weight: 700; margin: 6px 0; }
  .dim { color: #666; font-size: 10px; }
  hr { border: 0; border-top: 1px dashed #999; }
</style></head>
<body>
  <h1>${esc(v.store_name)}</h1>
  <p class="dim">GSTIN ${esc(v.store_gstin)}<br>Gift voucher</p>
  <hr>
  <p><strong>${esc(v.number)}</strong><br>Code <strong>${esc(v.code)}</strong></p>
  <p class="big">${esc(formatINR(v.value_paise))}</p>
  <p>Use by ${esc(dayText(v.valid_until))}</p>
  <p class="dim">Sold ${esc(dayText(v.issued_on))}. Use it in parts at any of our stores under this GSTIN; the balance stays on it. Keep the code private: with the number, it spends the voucher.</p>
  <hr>
  <p class="dim">${esc(NO_GST_WORDS)}</p>
</body></html>`;
}

function esc(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
