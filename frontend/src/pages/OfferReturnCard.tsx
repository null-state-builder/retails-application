// Return on each offer (store operations ticket 31, ST-OFR-1, §16).
//
// Once an offer has run, its page shows what the goods it covers sold while it
// ran - sales, pieces, discount given, the part this offer gave and, only if the
// server sends them, gross margin and the brand-funded part - against a baseline
// period the viewer states (by default the same number of days just before it
// started). It needs a connection; offline it says so and comes back when the
// line does.

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Download, TrendingUp } from "lucide-react";

import { api, apiErrorCode, apiErrorMessage } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { baselineQuery, cellText, changeText, figuresFor, periodDates } from "../lib/offerReturn";
import type { Baseline, OfferReturn } from "../lib/offerReturn";
import { asOfText } from "../lib/salesReport";

const OFFLINE = "The offer's return needs a connection. Reconnect to see it.";

/** Refusals that mean "not for you here": the card is simply not drawn. */
const HIDDEN = new Set(["FEATURE_OFF", "SCOPE_DENIED", "NOT_RUN"]);

interface Props {
  offerId: number;
}

export function OfferReturnCard({ offerId }: Props) {
  const [data, setData] = useState<OfferReturn | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  /** The baseline shown: what the viewer typed, or the server's answer. */
  const [form, setForm] = useState<Baseline>({ from: "", to: "" });
  /** The baseline last answered; null is the server's default. */
  const asked = useRef<Baseline | null>(null);
  /** The baseline a dropped connection cut off, so "Try again" asks for it again. */
  const wanted = useRef<Baseline | null>(null);
  const request = useRef(0);

  const load = useCallback(
    async (baseline: Baseline | null) => {
      if (!navigator.onLine) {
        setOnline(false);
        return;
      }
      const mine = ++request.current;
      wanted.current = baseline;
      setBusy(true);
      setError("");
      try {
        const response = await api.get<OfferReturn>("/reports/offer-return", {
          params: { offer: offerId, ...baselineQuery(baseline) },
        });
        if (mine !== request.current) return;
        asked.current = baseline;
        setLost(false);
        setHidden(false);
        setData(response.data);
        const base = response.data.periods.find((period) => period.key === "baseline");
        if (base) setForm({ from: base.date_from, to: base.date_to });
      } catch (reason) {
        if (mine !== request.current) return;
        if (isConnectionLost(reason)) {
          setLost(true);
          return;
        }
        // A refused baseline is not asked for again; the last answer stands.
        wanted.current = asked.current;
        if (!baseline && HIDDEN.has(apiErrorCode(reason) ?? "")) setHidden(true);
        else setError(apiErrorMessage(reason));
      } finally {
        if (mine === request.current) setBusy(false);
      }
    },
    [offerId],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(wanted.current);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  function compare(event: FormEvent) {
    event.preventDefault();
    void load({ ...form });
  }

  async function exportSheet() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/offer-return/export.xlsx", {
        params: { offer: offerId, ...baselineQuery(asked.current) },
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `offer-return-${offerId}.xlsx`;
      link.click();
      URL.revokeObjectURL(href);
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError("The spreadsheet could not be made. Try again.");
    }
  }

  if (hidden) return null;

  const figures = data ? figuresFor(data) : [];
  const [ran, base] = data?.periods ?? [];

  return (
    <section className="card section-card" data-testid="ret-card">
      <p className="eyebrow">
        <TrendingUp size={14} /> What it earned and what it cost
      </p>
      <p className="hint">
        The goods this offer covers, at its stores, while it ran - against a baseline period you
        choose. Customers, stock and season differ between two periods, so the change is not all
        the offer's doing.
      </p>

      {(!online || lost) && (
        <p className="warn-note" data-testid="ret-offline" role="status">
          {OFFLINE}
          {online && (
            <>
              {" "}
              <button
                type="button"
                className="btn"
                data-testid="ret-retry"
                disabled={busy}
                onClick={() => void load(wanted.current)}
              >
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="ret-error">
          {error}
        </p>
      )}

      <form className="filter-bar" onSubmit={compare} data-testid="ret-baseline">
        <label>
          Baseline from{" "}
          <input
            type="date"
            value={form.from}
            onChange={(event) => setForm({ ...form, from: event.target.value })}
            data-testid="ret-baseline-from"
            required
          />
        </label>
        <label>
          to{" "}
          <input
            type="date"
            value={form.to}
            onChange={(event) => setForm({ ...form, to: event.target.value })}
            data-testid="ret-baseline-to"
            required
          />
        </label>
        <button
          type="submit"
          className="btn"
          disabled={busy || !online}
          data-testid="ret-compare"
        >
          Compare
        </button>
      </form>

      {data && ran && base && (
        <>
          <p className="hint" data-testid="ret-as-of">
            {data.as_of ? `Bills ${asOfText(data.as_of).replace(/^As/, "as")}` : "No copy of the bills yet"} ·{" "}
            {data.stores.map((s) => s.code).join(", ") || "no stores"}
            {!base.stated && " · Baseline: the same number of days just before the offer started"}
          </p>
          <div className="table-wrap">
            <table className="data" data-testid="ret-table">
              <thead>
                <tr>
                  <th>{data.title}</th>
                  <th data-testid="ret-period-offer">
                    {ran.label}
                    <br />
                    <span className="hint">
                      {periodDates(ran)} · {ran.days} days
                    </span>
                  </th>
                  <th data-testid="ret-period-baseline">
                    {base.label}
                    <br />
                    <span className="hint">
                      {periodDates(base)} · {base.days} days
                    </span>
                  </th>
                  <th>Change</th>
                </tr>
              </thead>
              <tbody>
                {figures.map((figure) => (
                  <tr key={figure.key} data-testid={`ret-row-${figure.key}`}>
                    <td>{figure.label}</td>
                    <td data-testid={`ret-cell-offer-${figure.key}`}>{cellText(ran, figure)}</td>
                    <td data-testid={`ret-cell-baseline-${figure.key}`}>{cellText(base, figure)}</td>
                    <td data-testid={`ret-change-${figure.key}`}>{changeText(data.change, figure)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {data && data.missing.length > 0 && (
        <ul className="hint" data-testid="ret-missing">
          {data.missing.map((item) => (
            <li key={item.code} data-testid={`ret-missing-${item.code}`}>
              {item.text}
            </li>
          ))}
        </ul>
      )}
      {data && (
        <details data-testid="ret-basis">
          <summary>How this is worked out</summary>
          <ul className="hint">
            {data.basis.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      )}

      {data && (
        <div className="filter-bar" style={{ marginTop: 12 }}>
          <button
            type="button"
            className="btn btn-lg"
            disabled={busy || !online}
            onClick={() => void exportSheet()}
            data-testid="ret-export"
          >
            <Download size={16} /> Export to Excel
          </button>
        </div>
      )}
    </section>
  );
}

export default OfferReturnCard;
