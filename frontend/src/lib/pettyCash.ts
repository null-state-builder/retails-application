// A store's petty cash (store operations ticket 42, ST-MNY-3).
//
// What the Petty Cash screen works out while someone types. The server checks
// every rule again when a spend or top-up is saved (`sell/services/petty_cash.py`),
// so nothing here is trusted - it is what the person sees before pressing Save.

import type { ApiSchemas } from "./api";

export type PettyPosition = ApiSchemas["PettyCashPosition"];
export type PettySpend = ApiSchemas["PettyCashSpendRead"];
export type PettyTopUp = ApiSchemas["PettyCashTopUpRead"];

/** The head that needs a note saying what the money was for. */
export const OTHER_HEAD = "Other";

export const STATUS_LABEL: Record<string, string> = {
  spent: "Spent",
  waiting: "Waiting for the Owner",
  approved: "Approved",
  rejected: "Rejected",
};

export const ORIGIN_LABEL: Record<string, string> = {
  till: "From the till",
  head_office: "From head office",
};

/** What the box can still pay: what it holds, less spends waiting for the Owner. */
export function freePaise(
  position: Pick<PettyPosition, "balance_paise" | "waiting_paise">,
): number {
  return position.balance_paise - position.waiting_paise;
}

/** How much more a top-up may put in before the box is above its float. */
export function roomPaise(position: Pick<PettyPosition, "balance_paise" | "float">): number {
  if (!position.float) return 0;
  return Math.max(position.float.float_paise - position.balance_paise, 0);
}

/** A spend over the limit waits for the Owner (at the limit exactly, it does not). */
export function needsOwner(amountPaise: number, limitPaise: number): boolean {
  return amountPaise > limitPaise;
}

export interface SpendDraft {
  head: string;
  amountPaise: number;
  note: string;
}

/** Why a spend cannot be saved yet, or "" when it can. */
export function whySpendWaits(
  draft: SpendDraft,
  position: Pick<PettyPosition, "switched_on" | "float" | "balance_paise" | "waiting_paise">,
  online: boolean,
): string {
  if (!online) {
    return "You are offline. Petty cash is saved straight to head office, so it waits until the connection is back. What you have typed stays here.";
  }
  if (!position.switched_on) return "Petty cash is switched off for this store.";
  if (!position.float) {
    return "Head office has not set this store's petty cash float and custodian yet.";
  }
  if (!draft.head) return "Choose an expense head.";
  if (draft.amountPaise <= 0) return "Type the amount.";
  if (draft.head === OTHER_HEAD && !draft.note.trim()) return "Say what the money was spent on.";
  if (draft.amountPaise > freePaise(position)) {
    return "The box does not hold that much. Top it up first.";
  }
  return "";
}

export interface TopUpDraft {
  origin: "till" | "head_office";
  amountPaise: number;
  givenBy: string;
  reference: string;
}

/** Why a top-up cannot be saved yet, or "" when it can. */
export function whyTopUpWaits(
  draft: TopUpDraft,
  position: Pick<PettyPosition, "switched_on" | "float" | "balance_paise">,
  online: boolean,
): string {
  if (!online) {
    return "You are offline. Petty cash is saved straight to head office, so it waits until the connection is back. What you have typed stays here.";
  }
  if (!position.switched_on) return "Petty cash is switched off for this store.";
  if (!position.float) {
    return "Head office has not set this store's petty cash float and custodian yet.";
  }
  if (draft.amountPaise <= 0) return "Type the amount.";
  if (draft.origin === "head_office" && !draft.givenBy.trim()) {
    return "Name who brought the cash from head office.";
  }
  if (draft.origin === "head_office" && !draft.reference.trim()) {
    return "Type head office's voucher or receipt number.";
  }
  if (draft.amountPaise > roomPaise(position)) {
    return "That would take the box above its float.";
  }
  return "";
}

/** The store a person is looking at: the address first, then the switcher, then
 *  their only store. Null means they must choose. */
export function chosenStore(
  fromAddress: number | null,
  fromSwitcher: number | null,
  units: { id: number }[],
): number | null {
  if (fromAddress) return fromAddress;
  if (fromSwitcher) return fromSwitcher;
  return units.length === 1 ? units[0].id : null;
}
