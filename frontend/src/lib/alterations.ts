/** Alterations (store operations ticket 22, ST-ORD-3): the words the Alterations
 *  screen shows. The server decides every status, date and amount; these only
 *  say them, and check a form before it is sent. */

import type { ApiRead, ApiSchemas } from "./api";
import { formatINR } from "./format";
import { dayText as reservationDay } from "./reservations";

export type AlterationJob = ApiRead<ApiSchemas["Alteration"]>;
export type AlterationBill = ApiRead<ApiSchemas["AlterationBill"]>;
export type AlterationGarment = ApiRead<ApiSchemas["AlterationGarment"]>;

export const ALTERATIONS_API = "/sell/alterations";

/** Online only: said the same way everywhere on the screen. */
export const ALTERATIONS_OFFLINE =
  "Job cards need the internet. Nothing can be taken in, moved on or handed over until it is back; what you typed stays here.";

const STATUS: Record<string, { label: string; tone: string }> = {
  received: { label: "Received", tone: "blue" },
  with_tailor: { label: "With the tailor", tone: "amber" },
  ready: { label: "Ready", tone: "green" },
  handed_over: { label: "Handed over", tone: "navy" },
  cancelled: { label: "Cancelled", tone: "navy" },
};

/** The status as a chip: words and a tone, never colour alone. */
export function statusChip(status: string): { label: string; tone: string } {
  return STATUS[status] ?? { label: status, tone: "navy" };
}

/** The next step a job card can take, and what the button says. */
export type Step = "send" | "ready" | "told" | "hand-over";

export function nextSteps(job: Pick<AlterationJob, "status" | "customer_told_at">): Step[] {
  switch (job.status) {
    case "received":
      return ["send", "ready"];
    case "with_tailor":
      return ["ready"];
    case "ready":
      return job.customer_told_at ? ["hand-over"] : ["told", "hand-over"];
    default:
      return [];
  }
}

export const STEP_WORDS: Record<Step, string> = {
  send: "Sent to the tailor",
  ready: "Ready",
  told: "Customer told",
  "hand-over": "Handed over",
};

export function isOpen(status: string): boolean {
  return status === "received" || status === "with_tailor" || status === "ready";
}

/** What the garment is, as the customer would recognise it. */
export function garmentLabel(g: AlterationGarment): string {
  const words = [g.brand, g.item || g.design, g.size && `size ${g.size}`, g.color]
    .filter(Boolean)
    .join(" · ");
  return words || g.barcode;
}

export function chargeText(job: Pick<AlterationJob, "charge_paise">): string {
  return job.charge_paise > 0 ? `Paid ${formatINR(job.charge_paise)}` : "Free";
}

/** A day as the store reads it: 5 Oct 2026 (the Reservations screen's words). */
export function dayText(iso: string): string {
  return reservationDay(iso.slice(0, 10));
}

/** A moment as the store reads it, on the device's own clock: 5 Oct 2026, 14:05. */
export function momentText(iso: string): string {
  const at = new Date(iso);
  const pad = (n: number): string => String(n).padStart(2, "0");
  const day = `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
  return `${dayText(day)}, ${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

export function promisedText(job: Pick<AlterationJob, "promised_on" | "days_left" | "status">): string {
  const day = `Promised ${dayText(job.promised_on)}`;
  if (!isOpen(job.status)) return day;
  if (job.days_left < 0) return `${day} · late`;
  if (job.days_left === 0) return `${day} · today`;
  return `${day} · ${job.days_left} day${job.days_left === 1 ? "" : "s"} left`;
}

export const CUSTODY_WORDS: Record<string, string> = {
  store: "At the store",
  outside_tailor: "With the outside tailor",
};

/** The job card as typed, before the server has it. */
export interface JobDraft {
  bill: string;
  line_no: number | null;
  qty: number;
  /** The paid alteration's own line on `charge_bill` (blank: the garment's bill), or none. */
  charge_bill: string;
  charge_line_no: number | null;
  customer_name: string;
  customer_mobile: string;
  work: string;
  measurements: string;
  tailor: "in_house" | "outside" | "";
  tailor_name: string;
  promised_on: string;
}

export function emptyDraft(): JobDraft {
  return {
    bill: "",
    line_no: null,
    qty: 1,
    charge_bill: "",
    charge_line_no: null,
    customer_name: "",
    customer_mobile: "",
    work: "",
    measurements: "",
    tailor: "",
    tailor_name: "",
    promised_on: "",
  };
}

/** The draft with a found bill's customer and first free garment filled in. */
export function fromBill(draft: JobDraft, bill: AlterationBill): JobDraft {
  const free = bill.garments.find((g) => g.free_qty > 0);
  return {
    ...draft,
    bill: bill.bill,
    line_no: free?.line_no ?? null,
    qty: 1,
    charge_bill: "",
    charge_line_no: bill.charges.length === 1 ? bill.charges[0].line_no : null,
    customer_name: draft.customer_name || bill.customer_name,
    customer_mobile: draft.customer_mobile || bill.customer_mobile,
  };
}

/** Why the draft cannot be sent yet, in a sentence - or "". The server checks
 *  everything again; this only saves a round trip for the obvious. */
export function draftProblem(draft: JobDraft, today: string): string {
  if (!draft.bill) return "Find the garment's bill first.";
  if (draft.line_no === null) return "Pick the garment on the bill.";
  if (!draft.customer_name.trim()) return "Type the customer's name.";
  if (draft.customer_mobile.replace(/\D/g, "").slice(-10).length !== 10) {
    return "Type the customer's 10-digit mobile number.";
  }
  if (!draft.work.trim()) return "Say what to alter.";
  if (!draft.tailor) return "Say whether the tailor is in-house or outside.";
  if (draft.tailor === "outside" && !draft.tailor_name.trim()) {
    return "Name the outside tailor.";
  }
  if (!draft.promised_on) return "Pick the promised date.";
  if (draft.promised_on < today) return "The promised date cannot be in the past.";
  return "";
}

/** The `POST` body for a new job card. */
export function jobRequest(id: string, store: string, draft: JobDraft): Record<string, unknown> {
  return {
    id,
    store,
    bill: draft.bill,
    line_no: draft.line_no,
    qty: draft.qty,
    charge_bill: draft.charge_bill.trim(),
    charge_line_no: draft.charge_line_no,
    customer_name: draft.customer_name.trim(),
    customer_mobile: draft.customer_mobile.trim(),
    work: draft.work.trim(),
    measurements: draft.measurements.trim(),
    tailor: draft.tailor,
    tailor_name: draft.tailor_name.trim(),
    promised_on: draft.promised_on,
  };
}
