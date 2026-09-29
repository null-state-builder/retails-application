// The day-close cash count by note and coin (store operations ticket 41, ST-MNY-2).
//
// The arithmetic the Cash Count screen shows while the cashier types. The server
// works the same figures out again when the count is saved and refuses it if the
// expected cash has changed since the screen read it (`sell/services/cash_count.py`),
// so nothing here is trusted - it is what the person sees.

/** Pieces of each note, keyed by face value in rupees ("500", "200", ...). */
export type NoteCounts = Record<string, number>;

/** The figures the expected cash is made of, as `GET /api/sell/cash-count` sends them. */
export interface ExpectedParts {
  opening_paise: number | null;
  cash_sales_paise: number;
  cash_refunds_paise: number;
  movements_paise: number;
  petty_cash_paise: number;
  /** Petty cash brought from head office (ticket 42): new cash in the store. */
  petty_top_ups_paise: number;
}

/** Every note at nought, in the order the server lists them. */
export function emptyNotes(denominations: number[]): NoteCounts {
  return Object.fromEntries(denominations.map((face) => [String(face), 0]));
}

/** What the cashier counted: each note times its face value, plus the coins. */
export function countedPaise(
  denominations: number[],
  notes: NoteCounts,
  coinsPaise: number,
): number {
  return (
    denominations.reduce((sum, face) => sum + (notes[String(face)] || 0) * face * 100, 0) +
    coinsPaise
  );
}

/** opening + cash sales - cash refunds - deposits and handovers - petty cash
 *  spent + petty cash brought from head office (R-FIN-015; ticket 42). `declaredOpening` is the float typed on a store's first count;
 *  null while it is still blank there, which leaves nothing to expect yet. */
export function expectedPaise(parts: ExpectedParts, declaredOpening: number | null): number | null {
  const opening = parts.opening_paise ?? declaredOpening;
  if (opening === null) return null;
  return (
    opening +
    parts.cash_sales_paise -
    parts.cash_refunds_paise -
    parts.movements_paise -
    parts.petty_cash_paise +
    parts.petty_top_ups_paise
  );
}

/** A note count as typed: whole pieces, nought or more, or null for anything else. */
export function pieces(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return 0;
  if (!/^\d{1,6}$/.test(trimmed)) return null;
  return Number(trimmed);
}

/** How a variance reads to the person at the counter. */
export function varianceWords(variancePaise: number): "matches" | "short" | "over" {
  if (variancePaise === 0) return "matches";
  return variancePaise < 0 ? "short" : "over";
}

/**
 * Why the count cannot be saved right now, or "" if it can.
 *
 * The count is saved straight to head office, never queued: the expected cash
 * is only true once the server holds every bill. So an offline counter, and a
 * counter still holding bills it has not sent, are told to wait - and what they
 * have typed stays on the screen.
 */
export function whyCannotSave(state: {
  online: boolean;
  pending: number;
  hasCounter: boolean;
}): string {
  if (!state.hasCounter) {
    return "Count the cash on the store's counter, where the till is set up.";
  }
  if (!state.online) {
    return "You are offline. The count is saved straight to head office, so it waits until the connection is back. What you have typed stays here.";
  }
  if (state.pending > 0) {
    return state.pending === 1
      ? "1 bill is still waiting to go to head office. Send it first (Sync now on Till & Sync), so the expected cash includes it."
      : `${state.pending} bills are still waiting to go to head office. Send them first (Sync now on Till & Sync), so the expected cash includes them.`;
  }
  return "";
}
