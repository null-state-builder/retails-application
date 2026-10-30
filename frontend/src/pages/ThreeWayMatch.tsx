/** Three-way match at receiving (store operations ticket 37, ST-REC-1).
 *
 *  The GRN step shows each line's booked, invoiced and counted figures and the
 *  server's verdict; the invoice step asks for the invoice's cost only from a
 *  login the server says may see cost. Where the store's switch is off the
 *  server refuses (FEATURE_OFF) and both stay exactly as before this ticket.
 *
 *  It is a reading, never a step: nothing here holds up counting. Offline, the
 *  panel says so and reads again when the connection comes back. */

import { useEffect, useRef } from "react";
import { Scale } from "lucide-react";

import { Note } from "../components/Note";
import { apiErrorCode, apiErrorMessage } from "../lib/api";
import { useCachedGet } from "../lib/cachedQuery";
import { formatPaiseString } from "../lib/format";
import type { ResourceDTO } from "../lib/goodsScreen";
import { bookedNote, qty, resultWords, type ThreeWayData } from "../lib/threeWayMatch";

export type ThreeWayState =
  | { kind: "loading" }
  | { kind: "off" }
  | { kind: "offline" }
  | { kind: "failed"; message: string }
  | { kind: "ready"; data: ThreeWayData };

/** The action an arrival names where the site's switch is on. */
export const THREE_WAY_ACTION = "three_way_match";

/** Whether the switch is on at this arrival's site, as the arrival itself says. */
export function threeWayOn(arrival: { allowed_actions?: string[] } | null | undefined): boolean {
  return Boolean(arrival?.allowed_actions?.includes(THREE_WAY_ACTION));
}

/** The match for one arrival, read again whenever the connection comes back.
 *  `null` asks nothing: the switch is off (or not known yet) at its site. */
export function useThreeWay(
  arrivalId: string | null | undefined,
  refresh: string | number = 0,
): ThreeWayState {
  // Through the shared cache: an arrival opened before shows its match at once
  // and is read again behind it.
  const read = useCachedGet<ResourceDTO<ThreeWayData>, ThreeWayData>(
    arrivalId ? `/goods-v1/inbound/arrivals/${arrivalId}/three-way-match` : null,
    { kind: "doc", select: (r) => r.data, keepPrevious: false },
  );
  const { reload } = read;

  useEffect(() => {
    window.addEventListener("online", reload);
    return () => window.removeEventListener("online", reload);
  }, [reload]);

  // A changed GRN or invoice asks again (not on first sight: that read is the
  // cache's own).
  const lastRefresh = useRef(refresh);
  useEffect(() => {
    if (lastRefresh.current === refresh) return;
    lastRefresh.current = refresh;
    reload();
  }, [refresh, reload]);

  if (!arrivalId) return { kind: "off" };
  if (read.error) {
    const e = read.error;
    const code = apiErrorCode(e);
    const answered = Boolean((e as { response?: unknown })?.response);
    if (code === "FEATURE_OFF" || code === "ACTION_DENIED" || code === "NOT_FOUND") {
      return { kind: "off" };
    }
    if (!answered || !navigator.onLine) return { kind: "offline" };
    return { kind: "failed", message: apiErrorMessage(e) };
  }
  if (read.data) return { kind: "ready", data: read.data };
  return { kind: "loading" };
}

function money(value: string | null | undefined): string {
  return value === null || value === undefined ? "—" : formatPaiseString(value);
}

/** Booked, invoiced and counted, per line, with the verdict. Nothing at all where
 *  the store's switch is off. */
export function ThreeWayPanel({
  arrivalId,
  refresh,
}: {
  arrivalId: string | null;
  /** Changes whenever the GRN or its invoice does, so the match is read again. */
  refresh?: string;
}) {
  const state = useThreeWay(arrivalId, refresh);
  if (state.kind === "off" || state.kind === "loading") return null;
  return (
    <section data-testid="twm-panel">
      <h4 className="gr-h4">
        <Scale size={15} /> Booked, invoiced and counted
      </h4>
      {state.kind === "offline" && (
        <Note tone="warn" testId="twm-offline">
          You are offline, so the match cannot be read now. Counting is not affected. It shows again
          as soon as the connection is back.
        </Note>
      )}
      {state.kind === "failed" && (
        <Note tone="error" testId="twm-failed">
          {state.message}
        </Note>
      )}
      {state.kind === "ready" && <ThreeWayTable data={state.data} />}
    </section>
  );
}

function ThreeWayTable({ data }: { data: ThreeWayData }) {
  const cost = data.sees_cost;
  const mismatched = data.rows.filter((row) => row.status === "mismatch").length;
  return (
    <>
      <p className="gr-hint" data-testid="twm-summary">
        {mismatched === 0
          ? "Every line matches."
          : `${mismatched === 1 ? "1 line does" : `${mismatched} lines do`} not match. The buyer has been asked to look.`}{" "}
        A line matches within {data.qty_tolerance} piece
        {data.qty_tolerance === 1 ? "" : "s"}
        {cost && data.cost_tolerance_paise
          ? ` and ${formatPaiseString(data.cost_tolerance_paise)} of cost over the line`
          : ""}
        .
        {!data.has_invoice &&
          " No invoice is recorded yet, so the count is compared with the booking only."}
      </p>
      <div className="table-wrap">
        <table className="data" data-testid="twm-table">
          <thead>
            <tr>
              <th>Line</th>
              <th className="num">Booked</th>
              <th className="num">Invoiced</th>
              <th className="num">Counted</th>
              {cost && <th className="num">Booked cost</th>}
              {cost && <th className="num">Invoice cost</th>}
              {cost && <th className="num">Cost difference</th>}
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row) => (
              <tr
                key={row.row_key}
                className={row.status === "mismatch" ? "gr-mismatch" : ""}
                data-testid={`twm-row-${row.row_key}`}
              >
                <td>{row.description || "—"}</td>
                <td className="num" data-testid="twm-booked">
                  {row.booked_qty === null ? bookedNote(row) || "—" : row.booked_qty}
                </td>
                <td className="num" data-testid="twm-invoiced">
                  {qty(row.invoiced_qty)}
                </td>
                <td className="num" data-testid="twm-counted">
                  {row.counted_qty}
                </td>
                {cost && <td className="num">{money(row.booked_cost_paise)}</td>}
                {cost && <td className="num">{money(row.invoiced_cost_paise)}</td>}
                {cost && (
                  <td className="num" data-testid="twm-cost-difference">
                    {money(row.cost_difference_paise)}
                  </td>
                )}
                <td data-testid="twm-result">
                  <b>{resultWords(row)}</b>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
