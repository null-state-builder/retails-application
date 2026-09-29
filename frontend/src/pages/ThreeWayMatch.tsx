/** Three-way match at receiving (store operations ticket 37, ST-REC-1).
 *
 *  The GRN step shows each line's booked, invoiced and counted figures and the
 *  server's verdict; the invoice step asks for the invoice's cost only from a
 *  login the server says may see cost. Where the store's switch is off the
 *  server refuses (FEATURE_OFF) and both stay exactly as before this ticket.
 *
 *  It is a reading, never a step: nothing here holds up counting. Offline, the
 *  panel says so and reads again when the connection comes back. */

import { useEffect, useState } from "react";
import { Scale } from "lucide-react";

import { api, apiErrorCode, apiErrorMessage } from "../lib/api";
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
  const [state, setState] = useState<ThreeWayState>({ kind: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const again = () => setTick((t) => t + 1);
    window.addEventListener("online", again);
    return () => window.removeEventListener("online", again);
  }, []);

  useEffect(() => {
    if (!arrivalId) {
      setState({ kind: "off" });
      return;
    }
    let live = true;
    setState((now) =>
      now.kind === "ready" && now.data.arrival_id === arrivalId ? now : { kind: "loading" },
    );
    api
      .get<ResourceDTO<ThreeWayData>>(`/goods-v1/inbound/arrivals/${arrivalId}/three-way-match`)
      .then((r) => live && setState({ kind: "ready", data: r.data.data }))
      .catch((e) => {
        if (!live) return;
        const code = apiErrorCode(e);
        const answered = Boolean((e as { response?: unknown })?.response);
        if (code === "FEATURE_OFF" || code === "ACTION_DENIED" || code === "NOT_FOUND") {
          setState({ kind: "off" });
        } else if (!answered || !navigator.onLine) {
          setState({ kind: "offline" });
        } else {
          setState({ kind: "failed", message: apiErrorMessage(e) });
        }
      });
    return () => {
      live = false;
    };
  }, [arrivalId, refresh, tick]);

  return state;
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
        <div className="warn-note" data-testid="twm-offline">
          You are offline, so the match cannot be read now. Counting is not affected. It shows again
          as soon as the connection is back.
        </div>
      )}
      {state.kind === "failed" && (
        <div className="warn-note" data-testid="twm-failed">
          {state.message}
        </div>
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
