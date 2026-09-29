/** Saved size per brand (store operations ticket 18, ST-CUS-2): the words the
 *  till's saved-size card shows, and the correction it sends. The server
 *  learns every size from bills and decides what stands; these only say it. */

import type { ApiRead, ApiSchemas } from "./api";

export type SavedSize = ApiRead<ApiSchemas["SavedSizeRow"]>;
export type SavedSizes = ApiRead<ApiSchemas["SavedSizes"]>;

export const SAVED_SIZES_API = "/sell/saved-sizes";

/** Online only: said when there is no connection, or it drops. */
export const SAVED_SIZES_OFFLINE =
  "Saved sizes need the internet. The bill is not affected; they show again when the connection is back.";

/** A correction whose answer was lost: it may or may not have been saved. */
export const SAVED_SIZE_DROPPED =
  "The connection dropped, so the change may not be saved. Press Save again when it is back; it will not be saved twice.";

export interface CorrectionDraft {
  size: string;
  agreed: boolean;
}

function sameSize(a: string, b: string): boolean {
  return (
    a.trim().replace(/\s+/g, " ").toUpperCase() === b.trim().replace(/\s+/g, " ").toUpperCase()
  );
}

/** Where a size came from, in words. */
export function savedSizeNote(row: SavedSize, when: (iso: string) => string): string {
  if (row.how === "staff") return `Corrected with the customer's agreement ${when(row.as_of)}`;
  return row.doc_number
    ? `From bill ${row.doc_number} ${when(row.as_of)}`
    : `From a bill ${when(row.as_of)}`;
}

/** Why a correction cannot be sent yet, or "" when it can. */
export function correctionProblem(row: SavedSize, draft: CorrectionDraft): string {
  if (!draft.size.trim()) return "Type the new size.";
  if (sameSize(draft.size, row.size)) return "That is already the saved size.";
  if (!draft.agreed) return "Ask the customer first, then tick that they agreed.";
  return "";
}

/** The correction as the server takes it: against the size the till showed. */
export function correctionBody(
  id: string,
  mobile: string,
  row: SavedSize,
  draft: CorrectionDraft,
): ApiSchemas["SavedSizeCorrectionWrite"] {
  return {
    id,
    mobile,
    brand: row.brand,
    category: row.category,
    size: draft.size.trim().replace(/\s+/g, " "),
    was_size: row.size,
    agreed: draft.agreed,
  };
}
