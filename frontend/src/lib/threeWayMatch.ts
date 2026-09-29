/** Three-way match at receiving (store operations ticket 37, ST-REC-1).
 *
 *  The server compares what was booked, invoiced and counted per line
 *  (`GET /goods-v1/inbound/arrivals/{id}/three-way-match`) and judges each line;
 *  the screen only says what it was told. A login without the cost field is
 *  sent no cost keys at all and no "cost differs" result - there is nothing
 *  here to hide. */

export type ThreeWayResult = "short" | "excess" | "cost_differs";
export type BookingSource = "linked" | "not_booked" | "several" | "no_booking";

export interface ThreeWayRow {
  row_key: string;
  invoice_line_key: string | null;
  grn_line_keys: string[];
  booking_line_key: string | null;
  booking: BookingSource;
  description: string;
  booked_qty: number | null;
  invoiced_qty: number | null;
  counted_qty: number;
  booked_cost_paise?: string | null;
  invoiced_cost_paise?: string | null;
  cost_difference_paise?: string | null;
  results: ThreeWayResult[];
  status: "match" | "mismatch";
}

export interface ThreeWayData {
  arrival_id: string;
  grn_id: string | null;
  has_booking: boolean;
  has_invoice: boolean;
  sees_cost: boolean;
  qty_tolerance: number;
  cost_tolerance_paise?: string;
  exception_id: string | null;
  rows: ThreeWayRow[];
}

const RESULT_WORDS: Record<ThreeWayResult, string> = {
  short: "Short",
  excess: "Excess",
  cost_differs: "Cost differs",
};

/** "Match", or each result in words: "Short, cost differs". */
export function resultWords(row: Pick<ThreeWayRow, "results">): string {
  if (row.results.length === 0) return "Match";
  return row.results
    .map((result, index) => {
      const words = RESULT_WORDS[result] ?? result;
      return index === 0 ? words : words.toLowerCase();
    })
    .join(", ");
}

/** Where the booked figure came from, when there is none to show. */
export function bookedNote(row: Pick<ThreeWayRow, "booking">): string {
  switch (row.booking) {
    case "no_booking":
      return "No booking";
    case "not_booked":
      return "Not on the booking";
    case "several":
      return "Two booking lines - the buyer links it";
    default:
      return "";
  }
}

/** A quantity, or a dash when that figure does not exist for the line. */
export function qty(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value);
}
