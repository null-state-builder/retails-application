/** Claims for brand-funded discounts (store operations ticket 26, ST-BRD-3, ST-BRD-6).
 *
 *  The server works out every figure - what each brand owes for a month, what is
 *  unknown, whether the promotion-services flag applies - and says what the
 *  signed-in person may do next (`allowed_actions`). This file only says in
 *  words where a claim stands and turns what Accounts types into requests. */
import type { ApiRead, ApiSchemas } from "./api";
import { rupeesToPaise } from "./debitNotes";

export type BrandClaim = ApiRead<ApiSchemas["BrandClaim"]>;
export type BrandClaimDetail = ApiRead<ApiSchemas["BrandClaimDetail"]>;
export type BrandClaimList = ApiRead<ApiSchemas["BrandClaimList"]>;
export type ToRaise = BrandClaimList["to_raise"][number];
export type Status = BrandClaim["status"];

export const STATUS_LABEL: Record<Status, string> = {
  raised: "Raised - waiting for the brand",
  accepted: "Accepted by the brand",
  settled: "Settled",
  settled_short: "Settled short",
};

/** The chip class beside the words: never colour alone. */
export const STATUS_CHIP: Record<Status, string> = {
  raised: "chip-amber",
  accepted: "chip-amber",
  settled: "chip-green",
  settled_short: "chip-red",
};

/** What happens next, in one sentence, for whoever is looking. */
export function nextStep(claim: Pick<BrandClaim, "status">): string {
  switch (claim.status) {
    case "raised":
      return "Send it to the brand. When the brand agrees, Accounts records it as accepted.";
    case "accepted":
      return "When the brand's credit note arrives, Accounts records it here to settle the claim.";
    case "settled":
      return "Settled in full by the brand's commercial credit note. No GST effect on our bills.";
    case "settled_short":
      return "Settled for less than claimed. The difference is kept with its reason.";
  }
}

/** "2026-08" -> "August 2026". */
export function monthLabel(month: string): string {
  const [year, number] = month.split("-").map(Number);
  if (!year || !number) return month;
  return new Date(Date.UTC(year, number - 1, 1)).toLocaleDateString("en-IN", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** Rows that would raise a claim: a brand in the list, something owed, the switch on. */
export function raisable(rows: ToRaise[]): ToRaise[] {
  return rows.filter((row) => row.brand_id !== null && Number(row.amount_paise) > 0 && row.switched_on);
}

/** What Accounts types when the brand's credit note arrives. */
export interface SettleDraft {
  number: string;
  date: string;
  rupees: string;
  reason: string;
}

export function emptySettle(claim: Pick<BrandClaim, "amount_paise">, today: string): SettleDraft {
  const paise = Number(claim.amount_paise);
  const rupees = paise % 100 ? (paise / 100).toFixed(2) : String(paise / 100);
  return { number: "", date: today, rupees, reason: "" };
}

export type SettleCheck =
  | { ok: true; body: Record<string, string>; short: boolean }
  | { ok: false; problem: string; short: boolean };

/** The settle request, or what is wrong with what was typed. The server checks
 *  it all again; this only saves a round trip. */
export function settleBody(claim: Pick<BrandClaim, "amount_paise">, draft: SettleDraft): SettleCheck {
  const paise = rupeesToPaise(draft.rupees);
  const claimed = Number(claim.amount_paise);
  const short = typeof paise === "string" && Number(paise) < claimed;
  if (!draft.number.trim()) return { ok: false, problem: "Type the credit note's number.", short };
  if (!draft.date) return { ok: false, problem: "Pick the credit note's date.", short };
  if (paise === false || paise === null)
    return { ok: false, problem: "The amount is rupees, for example 1200 or 1200.50.", short };
  if (Number(paise) > claimed)
    return { ok: false, problem: "The credit note cannot be for more than the claim.", short };
  if (short && !draft.reason.trim())
    return { ok: false, problem: "Say why the brand paid less than claimed.", short };
  return {
    ok: true,
    short,
    body: {
      credit_note_number: draft.number.trim(),
      credit_note_date: draft.date,
      settled_paise: paise,
      ...(short ? { difference_reason: draft.reason.trim() } : {}),
    },
  };
}
