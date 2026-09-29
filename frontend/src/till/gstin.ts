// The buyer's GSTIN, at the counter (#187, grill Q8).
//
// This is the TS mirror of `sell/gstin.py`, and the mirroring is the point
// rather than a convenience. The counter prints the customer's copy offline,
// minutes before the server ever hears about the bill, and the tax split on that
// paper - CGST + SGST or IGST - is decided here. The server re-derives it on
// accept and flags a disagreement; a rule that read differently on the two sides
// would put one tax on the paper and another in the books, on every B2B bill.
//
// Two questions, and only one of them may stop anything:
//
//   · **Which state.** The first two characters, taken exactly as typed. That is
//     what prints.
//   · **Is it well formed.** Soft, always: the counter says so out loud, and the
//     bill closes anyway (Rule 8). The customer is standing there holding the
//     garment - a mistyped character is a tax invoice head office must correct,
//     not a sale to decline.
//
// The checksum earns its keep here more than on the server, because here it is
// caught while the customer is still at the counter and can read their card out
// again. Structure alone passes a transposed pair of digits, which is the
// commonest mistype and the one that costs them their input credit.

/** The GSTN's own alphabet: digits are worth their value, letters carry on from
 *  ten. Base 36 throughout, so 'Z' is 35 rather than a special case. */
const ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";

/** Structure only - the checksum is checked separately so the counter can say
 *  which of the two failed. */
const SHAPE = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$/;

/** State codes the GSTN issues: 01-38 are the states and union territories
 *  (38 = Ladakh, the newest), 97 is "other territory", 99 the UN and embassy
 *  series. A list rather than a range, because it is a government list that
 *  grows - and a range would silently bless 39 the day somebody typos it. */
const STATE_CODES = new Set<string>([
  ...Array.from({ length: 38 }, (_, i) => String(i + 1).padStart(2, "0")),
  "97",
  "99",
]);

/** What `describeGstin` answers when there is nothing wrong. Empty rather than a
 *  word, so a caller can write `if (reason)` and mean it. */
export const WELL_FORMED = "";

/** The three values `Sale.b2b_tax_kind` takes on the server, spelled the same. */
export type B2bTaxKind = "none" | "cgst_sgst" | "igst";

/** A GSTIN as it is sent and compared - trimmed, upper case.
 *
 *  The one place the counter's typing is tidied, and it matches
 *  `sell.gstin.normalise` exactly: a cashier types in whatever case the keyboard
 *  is in, and a value differing from itself by case would flag every second
 *  B2B bill. */
export function normaliseGstin(gstin: string): string {
  return (gstin ?? "").trim().toUpperCase();
}

/** The GSTN's mod-36 check character over the first fourteen.
 *
 *  Each character's value is multiplied by 1 or 2 alternately, the product's
 *  quotient and remainder in base 36 are added together, and the check digit is
 *  whatever takes the running total to the next multiple of 36. */
export function checkDigit(firstFourteen: string): string {
  let total = 0;
  for (let index = 0; index < firstFourteen.length; index += 1) {
    const value = ALPHABET.indexOf(firstFourteen[index]);
    const product = value * (index % 2 ? 2 : 1);
    total += Math.floor(product / 36) + (product % 36);
  }
  return ALPHABET[(36 - (total % 36)) % 36];
}

/**
 * Why this is not a GSTIN, or `WELL_FORMED` if it is.
 *
 * A sentence rather than a boolean, because the counter shows it to a cashier
 * who has to decide whether to ask the customer to read the card out again.
 */
export function describeGstin(gstin: string): string {
  const value = normaliseGstin(gstin);
  if (!value) return WELL_FORMED;
  if (value.length !== 15) return `A GSTIN is 15 characters; this one is ${value.length}.`;
  if (!SHAPE.test(value)) {
    return "Not shaped like a GSTIN (2 digits, a PAN, an entity character, Z, a check digit).";
  }
  if (!STATE_CODES.has(value.slice(0, 2))) {
    return `'${value.slice(0, 2)}' is not a state code the GSTN issues.`;
  }
  if (value[14] !== checkDigit(value.slice(0, 14))) {
    return "The check digit does not match - a character is probably mistyped.";
  }
  return WELL_FORMED;
}

/**
 * The shop's own state, off whichever of its two fields is there.
 *
 * `state_code` is the dataset's field and is the answer whenever it has one.
 * The fallback matters more than it looks: a store's own GSTIN *begins* with its
 * state code, so a counter whose identity arrived without the separate field
 * still knows perfectly well which state it is in - and the alternative,
 * defaulting to a state code that matches nothing, would print IGST on a local
 * buyer's tax invoice while the server posted CGST + SGST.
 *
 * Answers `null` - not `""` - when the counter has no identity at all, because
 * "I do not know which state this shop is in" and "this shop is in state ''" are
 * different statements and only one of them may be allowed to price a bill.
 */
export function storeStateCodeOf(
  store: { gstin?: string; state_code?: string } | null | undefined,
): string | null {
  if (!store) return null;
  return store.state_code || normaliseGstin(store.gstin ?? "").slice(0, 2) || null;
}

/**
 * Which tax split this bill carries.
 *
 * The buyer's state is the first two characters of their GSTIN. Same state as
 * the shop's registration means CGST + SGST; a different one means IGST. Bihar
 * and Jharkhand are separate registered persons, so this is an everyday branch
 * at a KDPS counter and not a corner case.
 *
 * Taken as typed, without asking whether the two characters are a state code at
 * all - `sell.services.accept._b2b_tax_kind` does exactly the same, and the two
 * agreeing is what keeps the paper and the books saying one thing. A GSTIN that
 * fails `describeGstin` is flagged for a human; it is never silently re-taxed.
 */
export function taxKindFor(buyerGstin: string, storeStateCode: string): B2bTaxKind {
  const value = normaliseGstin(buyerGstin);
  if (!value) return "none";
  return value.slice(0, 2) === storeStateCode ? "cgst_sgst" : "igst";
}

/** How each split reads on paper and on the screen. */
export const TAX_KIND_WORDS: Record<B2bTaxKind, string> = {
  none: "",
  cgst_sgst: "CGST + SGST",
  igst: "IGST",
};

/**
 * The tax on a B2B bill, split the way the customer's copy has to show it.
 *
 * One number in, two out, and the halving is where a paise can go missing: an
 * odd tax total split evenly is half a paise each way. CGST takes the floor and
 * SGST the remainder, so the two always add back to exactly what the bill
 * charged - which is the only property that matters, since a single OUTPUT_GST
 * account is what actually posts (the split is a presentation of one liability,
 * not two of them).
 */
export function splitTax(gstPaise: number, kind: B2bTaxKind): { label: string; paise: number }[] {
  if (kind === "igst") return [{ label: "IGST", paise: gstPaise }];
  if (kind !== "cgst_sgst") return [];
  const cgst = Math.floor(gstPaise / 2);
  return [
    { label: "CGST", paise: cgst },
    { label: "SGST", paise: gstPaise - cgst },
  ];
}
