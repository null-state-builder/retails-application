/** Debit notes drafted for receiving shortages (store operations ticket 38, ST-REC-3).
 *
 *  The server drafts a note when a shortage is accepted, works out every figure,
 *  and says what the signed-in person may do next (`allowed_actions`). This
 *  file only turns what Accounts types into the review request, and says in
 *  words where a note stands. */
import type { ApiRead, ApiSchemas } from "./api";

export type DebitNote = ApiRead<ApiSchemas["DebitNote"]>;
export type DebitNoteList = ApiRead<ApiSchemas["DebitNoteList"]>;
export type DebitNoteLine = DebitNote["lines"][number];
export type Stage = DebitNote["stage"];

export const STAGE_LABEL: Record<Stage, string> = {
  draft: "Draft - Accounts to review",
  waiting: "Waiting for the Owner",
  approved: "Approved - ready to issue",
  rejected: "Sent back by the Owner",
  issued: "Issued",
  cancelled: "Cancelled",
};

/** The chip class beside the words: never colour alone. */
export const STAGE_CHIP: Record<Stage, string> = {
  draft: "chip-amber",
  waiting: "chip-amber",
  approved: "chip-green",
  rejected: "chip-red",
  issued: "chip-green",
  cancelled: "",
};

/** What happens next, in one sentence, for whoever is looking. */
export function nextStep(note: Pick<DebitNote, "stage" | "missing">): string {
  switch (note.stage) {
    case "draft":
    case "rejected":
      return note.missing.length
        ? "Accounts types what is missing, then asks the Owner to approve it."
        : "Accounts asks the Owner to approve it.";
    case "waiting":
      return "The Owner approves or sends it back in the approvals inbox.";
    case "approved":
      return "Accounts issues it. It then takes head office's debit note number.";
    case "issued":
      return "Issued and never edited. It is not posted to the accounts yet: that waits for OQ-47.";
    case "cancelled":
      return "Cancelled. It was never numbered.";
  }
}

/** What Accounts has typed for one line: the GST rate, and a cost in rupees
 *  where the vendor's invoice gave none. */
export interface LineDraft {
  gst_rate: string;
  cost_rupees: string;
}

export function lineDraft(line: DebitNoteLine): LineDraft {
  return {
    gst_rate: line.gst_rate ?? "",
    cost_rupees:
      line.cost_from === "invoice" || line.unit_cost_paise === null
        ? ""
        : paiseToRupees(Number(line.unit_cost_paise)),
  };
}

/** `26000` -> `"260"`, `26050` -> `"260.50"`. */
export function paiseToRupees(paise: number): string {
  const rupees = Math.floor(paise / 100);
  const rest = paise % 100;
  return rest ? `${rupees}.${String(rest).padStart(2, "0")}` : String(rupees);
}

/** Rupees as typed -> whole paise as text, or null for blank; `false` when it is
 *  not money. Read as text, never through a float. */
export function rupeesToPaise(text: string): string | null | false {
  const typed = text.trim().replace(/,/g, "");
  if (!typed) return null;
  const parsed = /^(\d+)(?:\.(\d{1,2}))?$/.exec(typed);
  if (!parsed) return false;
  const paise = Number(parsed[1]) * 100 + Number((parsed[2] ?? "").padEnd(2, "0"));
  return paise > 0 ? String(paise) : false;
}

export interface ReviewLine {
  key: string;
  gst_rate?: string | null;
  unit_cost_paise?: string | null;
}

/** The review request's lines: only what changed. The invoice's own cost is
 *  never sent - the server keeps it as it is. A line's typed cost that is not
 *  money is returned in `bad`, so the screen can say so before sending. */
export function reviewLines(
  lines: DebitNoteLine[],
  drafts: Record<string, LineDraft>,
): { lines: ReviewLine[]; bad: string[] } {
  const out: ReviewLine[] = [];
  const bad: string[] = [];
  for (const line of lines) {
    const draft = drafts[line.key];
    if (!draft) continue;
    const change: ReviewLine = { key: line.key };
    const rate = draft.gst_rate || null;
    if (rate !== line.gst_rate) change.gst_rate = rate;
    if (line.cost_from !== "invoice") {
      const cost = rupeesToPaise(draft.cost_rupees);
      if (cost === false) bad.push(line.key);
      else if (cost !== line.unit_cost_paise) change.unit_cost_paise = cost;
    }
    if (Object.keys(change).length > 1) out.push(change);
  }
  return { lines: out, bad };
}

/** "5%" or "Not set". */
export function rateText(rate: string | null): string {
  return rate === null ? "Not set" : `${rate}%`;
}

/** One press of a step, as sent: which note, at which revision, which step, what body. */
export interface Attempt {
  noteId: number;
  revision: number;
  step: string;
  body: string;
}

/** A press whose answer never came back, with the command identity it went under. */
export interface Pending extends Attempt {
  commandId: string;
}

/** The command identity for `attempt`. Pressing the very same thing again after a
 *  dropped connection replays the command it was sent under, so the server never
 *  does it twice. Anything else - another step, another body, or the note at a
 *  new revision because the lost press did reach the server - is a new command,
 *  so the server never refuses it as a reused identity. */
export function commandIdFor(pending: Pending | null, attempt: Attempt, fresh: () => string): string {
  const same =
    pending !== null &&
    pending.noteId === attempt.noteId &&
    pending.revision === attempt.revision &&
    pending.step === attempt.step &&
    pending.body === attempt.body;
  return same ? pending.commandId : fresh();
}
