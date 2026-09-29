// Customers (store operations PRD §8 ST-CUS-1, §5.1; tickets 16 and 17): the words the
// customer list and page use. The server decides who may see and do what;
// nothing here decides it again.

/** Said wherever the Customers screens cannot reach head office (PRD §28). */
export const CUSTOMERS_OFFLINE =
  "Customers need a connection. Nothing can be shown, corrected, withdrawn, erased, merged or moved until it is back; what you typed stays here.";

/** One consent as it stands: `null` is never asked, which is off. */
export interface ConsentStanding {
  given: boolean;
  how: string;
  answered_at: string;
}

/** A consent as a chip: words and a colour, never colour alone. */
export function consentChip(standing: ConsentStanding | null | undefined): {
  label: string;
  chip: string;
} {
  if (!standing) return { label: "Never asked (off)", chip: "" };
  if (standing.given) return { label: "Yes", chip: "chip-green" };
  return {
    label: standing.how === "counter" ? "No - withdrawn" : "No",
    chip: "chip-amber",
  };
}

/** The consent questions, as the page names them. */
export const CONSENT_QUESTIONS = [
  { key: "bill", label: "Send my bill" },
  { key: "offers", label: "Send me offers" },
] as const;

export type ConsentQuestion = (typeof CONSENT_QUESTIONS)[number]["key"];

/** Where an answer was given, in words. */
export function answeredHow(how: string): string {
  return how === "display" ? "on the customer display" : "at the counter";
}

/** What a search box holds, as the list's query: trimmed, or nothing. */
export function searchQuery(term: string): Record<string, string> {
  const q = term.trim();
  return q ? { q } : {};
}

/** The correction form as typed, and what it was built from. */
export interface CorrectionDraft {
  name: string;
  gstin: string;
}

/** The body of a correction: the new values and the ones the form saw, so the
 *  server refuses it if somebody changed the customer in between. */
export function correctionRequest(
  siteId: number,
  draft: CorrectionDraft,
  was: CorrectionDraft,
): Record<string, unknown> {
  return {
    site_id: siteId,
    name: draft.name.trim(),
    gstin: draft.gstin.trim().toUpperCase(),
    was_name: was.name,
    was_gstin: was.gstin,
  };
}

/** Is the confirmation typed right: exactly the number's last four digits. */
export function lastFourMatches(mobile: string, typed: string): boolean {
  return /^\d{4}$/.test(typed.trim()) && mobile.endsWith(typed.trim());
}

/** A number as the server keeps it: digits only, a leading +91 or 0 dropped,
 *  so "+91 98765-43210" and "09876543210" are one number (ticket 17). */
export function bareMobile(typed: string): string {
  const digits = typed.replace(/\D/g, "");
  if (digits.length === 12 && digits.startsWith("91")) return digits.slice(2);
  if (digits.length === 11 && digits.startsWith("0")) return digits.slice(1);
  return digits;
}

/** Is a typed new number a ten-digit mobile, and not the one held now. */
export function newNumberOk(typed: string, held: string): boolean {
  const bare = bareMobile(typed);
  return bare.length === 10 && bare !== held;
}

/** The body of a move to a new number (ticket 17): the number the form was
 *  built from goes with it, so a changed record is refused, not overwritten. */
export function moveRequest(
  siteId: number,
  typed: string,
  was: string,
  present: boolean,
): Record<string, unknown> {
  return {
    site_id: siteId,
    new_mobile: bareMobile(typed),
    was_mobile: was,
    customer_present: present,
  };
}

/** The body of a merge (ticket 17): the other record, its number's last four
 *  digits typed again, and that the customer is here. */
export function mergeRequest(
  siteId: number,
  otherId: number,
  lastFour: string,
  present: boolean,
): Record<string, unknown> {
  return {
    site_id: siteId,
    other_id: otherId,
    other_last4: lastFour.trim(),
    customer_present: present,
  };
}

/** Another number of the customer's, in words. */
export function otherNumberNote(
  reason: string,
  until: string | null,
  format: (at: string) => string,
): string {
  if (reason === "merged") return "merged in: still theirs";
  return until ? `their number until ${format(until)}` : "their old number";
}
