// Brands > Terms (store operations PRD ST-BRD-1, ST-BRD-6; ticket 23): the words
// the screen uses, and the draft a Brand Manager types turned into a request.
// The server decides everything; nothing here guesses a term.

export type CommercialModel = "sor" | "outright" | "consignment" | "concession";
export type ProposalStatus = "waiting" | "approved" | "rejected" | "withdrawn";

const MODEL_LABELS: Record<CommercialModel, string> = {
  sor: "SOR",
  outright: "Outright",
  consignment: "Consignment",
  concession: "Concession",
};

/** A model's name, or "Unknown" when none is approved (D9): never a default. */
export function modelLabel(model: string | null | undefined): string {
  return model && model in MODEL_LABELS ? MODEL_LABELS[model as CommercialModel] : "Unknown";
}

const STATUS: Record<ProposalStatus, { label: string; chip: string }> = {
  waiting: { label: "Waiting for the Owner", chip: "chip-amber" },
  approved: { label: "Approved", chip: "chip-green" },
  rejected: { label: "Rejected", chip: "chip-red" },
  withdrawn: { label: "Withdrawn", chip: "" },
};

/** A proposal's status as a chip: words and a colour, never colour alone. */
export function statusChip(status: string): { label: string; chip: string } {
  return STATUS[status as ProposalStatus] ?? { label: status, chip: "" };
}

/** A figure as the screen shows it: a blank one is unknown, never 0. */
export function figure(value: string | number | null | undefined, unit: string): string {
  if (value === null || value === undefined || value === "") return "Unknown";
  return `${value}${unit}`;
}

/** The terms form, as typed. Blank figures stay blank. */
export interface TermsDraft {
  season_id: string;
  applies_from: string;
  model: CommercialModel | "";
  margin_percent: string;
  return_allowance_percent: string;
  discount_funding_percent: string;
  payment_days: string;
  note: string;
}

export function emptyTermsDraft(seasonId: string, today: string): TermsDraft {
  return {
    season_id: seasonId,
    applies_from: today,
    model: "",
    margin_percent: "",
    return_allowance_percent: "",
    discount_funding_percent: "",
    payment_days: "",
    note: "",
  };
}

/** A typed figure for the request: blank is null (unknown), anything else as typed,
 *  so the server names the problem rather than the screen quietly fixing it. */
function orNull(text: string): string | null {
  const trimmed = text.trim();
  return trimmed ? trimmed : null;
}

/** The request body for a terms proposal, without its command envelope. */
export function termsRequest(brandId: number, draft: TermsDraft): Record<string, unknown> {
  const days = draft.payment_days.trim();
  return {
    brand_id: brandId,
    season_id: Number(draft.season_id),
    applies_from: draft.applies_from,
    model: draft.model,
    margin_percent: orNull(draft.margin_percent),
    return_allowance_percent: orNull(draft.return_allowance_percent),
    discount_funding_percent: orNull(draft.discount_funding_percent),
    payment_days: days === "" ? null : /^\d+$/.test(days) ? Number(days) : days,
    note: draft.note.trim(),
  };
}
