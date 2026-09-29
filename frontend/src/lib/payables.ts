/** Brand payables for outright brands (store operations ticket 28, ST-MNY-4).
 *
 *  The server works out every figure - what is owed, paid and due, the ageing
 *  bands, which brands have no recorded model - and says what the signed-in
 *  person may do next (`allowed_actions`). This file only names the bands in
 *  words and turns what Accounts types into requests. */
import type { ApiRead, ApiSchemas } from "./api";
import { isConnectionLost } from "./auditLog";
import { rupeesToPaise } from "./debitNotes";
import { formatINR } from "./format";

export type PayablesSummary = ApiRead<ApiSchemas["PayablesSummary"]>;
export type PayableOptions = ApiRead<ApiSchemas["PayableOptions"]>;
export type PayableInvoice = ApiRead<ApiSchemas["PayableInvoice"]>;
export type PayablePayment = ApiRead<ApiSchemas["PayablePayment"]>;
export type Position = PayablesSummary["totals"];
export type Band = keyof Position["bands"];

/** The ageing bands, left to right, in days past the due date. */
export const BANDS: { key: Band; label: string }[] = [
  { key: "not_due", label: "Not yet due" },
  { key: "days_0_30", label: "0-30 days late" },
  { key: "days_31_60", label: "31-60 days late" },
  { key: "days_61_90", label: "61-90 days late" },
  { key: "over_90", label: "Over 90 days late" },
  { key: "due_unknown", label: "Due date unknown" },
];

export function bandLabel(band: string | null | undefined): string {
  return BANDS.find((b) => b.key === band)?.label ?? "Paid";
}

export const MODE_LABEL: Record<string, string> = {
  bank: "Bank transfer",
  cheque: "Cheque",
  upi: "UPI",
  cash: "Cash",
};

/** True when a save may or may not have been recorded: no answer came back, the
 *  server said to retry the same command (`retryable`, e.g. OUTCOME_UNKNOWN), or
 *  a gateway failed (5xx). Such a save is only ever sent again under the same
 *  command identity, so it is recorded once. */
export function outcomeUnknown(error: unknown): boolean {
  if (isConnectionLost(error)) return true;
  const response = (error as { response?: { status?: number; data?: { retryable?: unknown } } } | null)
    ?.response;
  if (!response) return false;
  return response.data?.retryable === true || (response.status ?? 0) >= 500;
}

/** Today in the browser's own calendar, as the date input writes it. */
export function today(): string {
  return new Date().toLocaleDateString("en-CA");
}

export type Check<T> = { ok: true; body: T } | { ok: false; problem: string };

/** What Accounts types for one vendor invoice. */
export interface InvoiceDraft {
  store: string;
  vendor: string;
  brand: string;
  season: string;
  number: string;
  date: string;
  rupees: string;
  note: string;
}

export function emptyInvoice(store = ""): InvoiceDraft {
  return { store, vendor: "", brand: "", season: "", number: "", date: today(), rupees: "", note: "" };
}

/** The invoice request, or what is wrong with what was typed. The server checks
 *  it all again; this only saves a round trip. */
export function invoiceBody(draft: InvoiceDraft): Check<Record<string, unknown>> {
  if (!draft.store) return { ok: false, problem: "Pick the store that received the goods." };
  if (!draft.vendor) return { ok: false, problem: "Pick the vendor." };
  if (!draft.brand) return { ok: false, problem: "Pick the brand." };
  if (!draft.season) return { ok: false, problem: "Pick the season of the goods." };
  if (!draft.number.trim()) return { ok: false, problem: "Type the vendor's invoice number." };
  if (!draft.date) return { ok: false, problem: "Pick the invoice date." };
  if (draft.date > today()) return { ok: false, problem: "The invoice date cannot be after today." };
  const paise = rupeesToPaise(draft.rupees);
  if (!paise) return { ok: false, problem: "Type the invoice total with tax in rupees, for example 11800 or 11800.50." };
  return {
    ok: true,
    body: {
      store_id: Number(draft.store),
      vendor_id: Number(draft.vendor),
      brand_id: Number(draft.brand),
      season_id: Number(draft.season),
      invoice_number: draft.number.trim(),
      invoice_date: draft.date,
      amount_paise: paise,
      ...(draft.note.trim() ? { note: draft.note.trim() } : {}),
    },
  };
}

/** Vendors a new invoice can come from: retired ones are left out (they can still be paid). */
export function invoiceVendors(options: PayableOptions | null): PayableOptions["vendors"] {
  return (options?.vendors ?? []).filter((v) => v.is_active);
}

/** The brands a vendor supplies, from the options. */
export function brandsOf(options: PayableOptions | null, vendorId: string): PayableOptions["brands"] {
  if (!options || !vendorId) return [];
  const vendor = options.vendors.find((v) => String(v.id) === vendorId);
  const ids = new Set(vendor?.brand_ids ?? []);
  return options.brands.filter((b) => ids.has(b.id));
}

/** Open invoices with something left, for one vendor at one store, oldest due
 *  first (an unknown due date last), as a payment would clear them. */
export function payableInvoices(invoices: PayableInvoice[], store: string, vendor: string): PayableInvoice[] {
  return invoices
    .filter(
      (inv) =>
        inv.status === "open" &&
        Number(inv.outstanding_paise) > 0 &&
        String(inv.store.id) === store &&
        String(inv.vendor.id) === vendor,
    )
    .sort((a, b) => {
      const da = a.due_date ?? "9999-12-31";
      const db = b.due_date ?? "9999-12-31";
      return da === db ? a.invoice_date.localeCompare(b.invoice_date) || a.id - b.id : da.localeCompare(db);
    });
}

/** Spread a payment over invoices, oldest due first; each line in rupees as typed. */
export function spreadOldestFirst(invoices: PayableInvoice[], paise: number): Record<number, string> {
  const out: Record<number, string> = {};
  let left = paise;
  for (const inv of invoices) {
    if (left <= 0) break;
    const share = Math.min(left, Number(inv.outstanding_paise));
    out[inv.id] = rupeesText(share);
    left -= share;
  }
  return out;
}

/** 118000 -> "1180", 118050 -> "1180.50". */
export function rupeesText(paise: number): string {
  return paise % 100 ? (paise / 100).toFixed(2) : String(paise / 100);
}

/** What Accounts types for one payment. */
export interface PaymentDraft {
  store: string;
  vendor: string;
  date: string;
  mode: string;
  reference: string;
  rupees: string;
  note: string;
  /** Rupees against each invoice id. */
  lines: Record<number, string>;
}

export function emptyPayment(store = ""): PaymentDraft {
  return { store, vendor: "", date: today(), mode: "bank", reference: "", rupees: "", note: "", lines: {} };
}

export function paymentBody(draft: PaymentDraft, open: PayableInvoice[]): Check<Record<string, unknown>> {
  if (!draft.store) return { ok: false, problem: "Pick the store the payment is for." };
  if (!draft.vendor) return { ok: false, problem: "Pick the vendor paid." };
  if (!draft.date) return { ok: false, problem: "Pick the day it was paid." };
  if (draft.date > today()) return { ok: false, problem: "The payment date cannot be after today." };
  if (!draft.reference.trim())
    return { ok: false, problem: "Type the bank reference, cheque number or UPI reference." };
  const total = rupeesToPaise(draft.rupees);
  if (!total) return { ok: false, problem: "Type the amount paid in rupees." };
  const allocations: { invoice_id: number; amount_paise: string }[] = [];
  let sum = 0;
  for (const inv of open) {
    const typed = draft.lines[inv.id] ?? "";
    if (!typed.trim()) continue;
    const paise = rupeesToPaise(typed);
    if (!paise) return { ok: false, problem: `The amount against ${inv.invoice_number} is not rupees.` };
    if (Number(paise) > Number(inv.outstanding_paise))
      return { ok: false, problem: `${inv.invoice_number} has less than that left to pay.` };
    allocations.push({ invoice_id: inv.id, amount_paise: paise });
    sum += Number(paise);
  }
  if (!allocations.length) return { ok: false, problem: "Say which invoices this payment pays." };
  if (sum !== Number(total))
    return {
      ok: false,
      problem: `The invoice lines add up to ${formatINR(sum)}, not the ${formatINR(Number(total))} paid.`,
    };
  return {
    ok: true,
    body: {
      store_id: Number(draft.store),
      vendor_id: Number(draft.vendor),
      paid_on: draft.date,
      amount_paise: total,
      mode: draft.mode,
      reference: draft.reference.trim(),
      allocations,
      ...(draft.note.trim() ? { note: draft.note.trim() } : {}),
    },
  };
}
