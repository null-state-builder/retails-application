// Offer simulation (store operations ticket 30, ST-OFR-3, §16).
//
// Before a draft offer is approved, it is priced against real bills from the
// last 4 weeks and the same 4 weeks last year. The card shows the estimated
// discount, pieces affected and - only if the server sends it - the margin
// effect, always labelled as an estimate. The server keeps the result, so the
// person who clears the offer sees the same figures the author saw. It needs a
// connection; offline it says so and comes back when the line does.

import { useCallback, useEffect, useRef, useState } from "react";
import { Calculator, Download } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { api, apiErrorCode, apiErrorMessage } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { cellText, figuresFor, periodDates } from "../lib/offerSimulation";
import type { Simulation } from "../lib/offerSimulation";
import { asOfText } from "../lib/salesReport";

const OFFLINE = "The offer simulation needs a connection. Reconnect to see or run it.";

/** Running an estimate is authoring work (`offers_price: operate` or higher);
 *  reading one is not. The server checks the same rung. */
const RUNS = new Set(["operate", "approve", "manage"]);

/** Refusals that mean "not for you here": the card is simply not drawn. */
const HIDDEN = new Set(["FEATURE_OFF", "SCOPE_DENIED"]);

interface Props {
  offerId: number;
  status: string;
  /** The offer's own save time: a change reloads the card (and makes it stale). */
  updatedAt?: string | undefined;
}

export function OfferSimulationCard({ offerId, status, updatedAt }: Props) {
  const { user } = useAuth();
  const [data, setData] = useState<Simulation | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const send = useCallback(
    async (run: boolean) => {
      if (!navigator.onLine) {
        setOnline(false);
        return;
      }
      const mine = ++request.current;
      setBusy(true);
      setError("");
      try {
        const response = run
          ? await api.post<Simulation>("/reports/offer-simulation", { offer: offerId })
          : await api.get<Simulation>("/reports/offer-simulation", { params: { offer: offerId } });
        if (mine !== request.current) return;
        setLost(false);
        setHidden(false);
        setData(response.data);
      } catch (reason) {
        if (mine !== request.current) return;
        if (isConnectionLost(reason)) setLost(true);
        else if (!run && HIDDEN.has(apiErrorCode(reason) ?? "")) setHidden(true);
        else setError(apiErrorMessage(reason));
      } finally {
        if (mine === request.current) setBusy(false);
      }
    },
    [offerId],
  );

  useEffect(() => {
    void send(false);
  }, [send, updatedAt]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void send(false);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [send]);

  async function exportSheet() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/offer-simulation/export.xlsx", {
        params: { offer: offerId },
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `offer-simulation-${offerId}.xlsx`;
      link.click();
      URL.revokeObjectURL(href);
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError("The spreadsheet could not be made. Try again.");
    }
  }

  if (hidden) return null;

  const mayRun = RUNS.has(user?.capabilities?.offers_price ?? "none");
  const canRun = status === "draft" && mayRun;
  const figures = data ? figuresFor(data) : [];
  const ran = data?.simulation != null;

  return (
    <section className="card section-card" data-testid="sim-card">
      <p className="eyebrow">
        <Calculator size={14} /> What it would have cost{" "}
        <span className="chip chip-amber" data-testid="sim-estimate-label">
          Estimate
        </span>
      </p>
      <p className="hint">
        Priced by the till's own offer engine on real bills from the last 4 weeks and the same 4
        weeks last year. Customers may buy differently when it runs, so this is a guide, not a
        promise.
      </p>

      {(!online || lost) && (
        <p className="warn-note" data-testid="sim-offline" role="status">
          {OFFLINE}
          {online && (
            <>
              {" "}
              <button
                type="button"
                className="btn"
                data-testid="sim-retry"
                disabled={busy}
                onClick={() => void send(false)}
              >
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="sim-error">
          {error}
        </p>
      )}

      {data && ran && (
        <>
          <p className="hint" data-testid="sim-run-line">
            Run {asOfText(data.run_at).replace(/^As of /, "")}
            {data.run_by ? ` by ${data.run_by}` : ""} ·{" "}
            {data.as_of
              ? `Bills ${asOfText(data.as_of).replace(/^As/, "as")}`
              : "No copy of the bills yet"}{" "}
            · {data.stores.map((s) => s.code).join(", ") || "no stores"}
          </p>
          {data.stale && (
            <p className="warn-note" data-testid="sim-stale">
              The offer changed after this estimate. Run it again to see the offer as it now stands.
            </p>
          )}
          <div className="table-wrap">
            <table className="data" data-testid="sim-table">
              <thead>
                <tr>
                  <th>Estimate</th>
                  {data.periods.map((period) => (
                    <th key={period.key} data-testid={`sim-period-${period.key}`}>
                      {period.label}
                      <br />
                      <span className="hint">{periodDates(period)}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {figures.map((figure) => (
                  <tr key={figure.key} data-testid={`sim-row-${figure.key}`}>
                    <td>{figure.label}</td>
                    {data.periods.map((period) => (
                      <td key={period.key} data-testid={`sim-cell-${period.key}-${figure.key}`}>
                        {cellText(period, figure)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {data && data.missing.length > 0 && (
        <ul className="hint" data-testid="sim-missing">
          {data.missing.map((item) => (
            <li key={item.code} data-testid={`sim-missing-${item.code}`}>
              {item.text}
            </li>
          ))}
        </ul>
      )}
      {data && (
        <details data-testid="sim-basis">
          <summary>How this is worked out</summary>
          <ul className="hint">
            {data.basis.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      )}

      {(canRun || ran) && (
        <div className="filter-bar" style={{ marginTop: 12 }}>
          {canRun && (
            <button
              type="button"
              className="btn btn-lg"
              disabled={busy || !online}
              onClick={() => void send(true)}
              data-testid="sim-run"
            >
              <Calculator size={16} />{" "}
              {ran ? "Run the estimate again" : "Estimate what it would cost"}
            </button>
          )}
          {ran && (
            <button
              type="button"
              className="btn btn-lg"
              disabled={busy || !online}
              onClick={() => void exportSheet()}
              data-testid="sim-export"
            >
              <Download size={16} /> Export to Excel
            </button>
          )}
        </div>
      )}
    </section>
  );
}

export default OfferSimulationCard;
