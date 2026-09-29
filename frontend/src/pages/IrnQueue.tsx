import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, FileCheck2, Link2Off, ReceiptText } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { TAX_KIND_WORDS } from "../till/gstin";
import type { B2bTaxKind } from "../till/gstin";
import { Money } from "../lib/format";
import { PageHeader } from "../components/PageHeader";
import "./Booking.css";
import "./IrnQueue.css";

// ---------------------------------------------------------------------------
// The IRN queue (#187, grill Q8) - head office's thirty-day clock
// ---------------------------------------------------------------------------
//
// Above the e-invoice threshold every GSTIN-bearing counter sale is legally a
// B2B tax invoice that must receive an IRN from the government portal within
// thirty days, or it is invalid and the customer loses the input credit they
// handed their registration over for.
//
// The shop cannot raise one and is never asked to: the counter prints "IRN to
// follow" and the bill lands here, oldest deadline first. This screen is the
// list and the one thing you do with it - record what the portal answered. It
// cannot touch the bill itself (A7); the row beside it is a fact about a filing.

/** One bill waiting on the portal, as `GET /api/sell/irn-queue` sends it. */
interface IrnRowT {
  id: number;
  /** `26-27/DEO/SAL/2` - the store is the middle of it, which is why this screen
   *  draws no store column. The API sends `store_code`/`store_name` beside it;
   *  they are not read here. */
  doc_number: string;
  billed_at: string;
  buyer_gstin: string;
  customer_name: string;
  b2b_tax_kind: B2bTaxKind;
  net_paise: number;
  gst_paise: number;
  due_on: string;
  /** Negative once the deadline has gone by. Worked out server-side against one
   *  reading of the clock, so thirty rows cannot disagree about what day it is. */
  days_left: number;
  status: IrnStatus;
  irn: string;
  handled_by_name: string;
  handled_at: string | null;
}

interface QueueT {
  today: string;
  rows: IrnRowT[];
  /** The settled tail is capped at 200; pending rows never are. Said out loud
   *  because a list that stopped and did not mention it reads as "that is all". */
  truncated: boolean;
  pending_count: number;
  overdue_count: number;
}

/** The three states a queue row can be in - `IrnQueueItem.Status` on the server,
 *  and a `ChoiceField` there, so a fourth string is a 400. */
type IrnStatus = "pending" | "failed" | "generated";

const TABS: { key: IrnStatus; label: string }[] = [
  { key: "pending", label: "To raise" },
  { key: "failed", label: "Failed" },
  { key: "generated", label: "Raised" },
];

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** How much of the thirty days is left, said the way a person would say it.
 *
 *  The number is the fact and the tone is the urgency; both come off the same
 *  server-computed `days_left`, so a row can never read "3 days left" in green. */
function Clock({ days }: { days: number }) {
  if (days < 0) {
    const late = Math.abs(days);
    return (
      <span className="chip chip-red" data-testid="irn-overdue">
        {late === 1 ? "1 day late" : `${late} days late`}
      </span>
    );
  }
  if (days === 0) return <span className="chip chip-red">Due today</span>;
  const tone = days <= 7 ? "amber" : "grey";
  return <span className={`chip chip-${tone}`}>{days === 1 ? "1 day left" : `${days} days`}</span>;
}

/** Recording what the portal said, on one bill.
 *
 *  Two answers and no third: a reference, or a refusal that keeps the bill on
 *  somebody's list. There is deliberately no way back to "not tried yet" - "we
 *  tried and it did not work" is worth keeping. */
function RecordPanel({ row, onDone }: { row: IrnRowT; onDone: () => void }) {
  const [irn, setIrn] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function send(status: "generated" | "failed") {
    setError("");
    setBusy(true);
    try {
      await api.put(`/sell/irn-queue/${row.id}`, { status, irn: status === "generated" ? irn : "" });
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <input
        className="input mono"
        value={irn}
        disabled={busy}
        maxLength={64}
        placeholder="IRN from the portal"
        aria-label={`IRN for ${row.doc_number}`}
        onChange={(e) => setIrn(e.target.value.trim())}
        data-testid={`irn-input-${row.id}`}
      />
      <button
        type="button"
        className="btn btn-primary"
        disabled={busy || !irn}
        onClick={() => void send("generated")}
        data-testid={`irn-save-${row.id}`}
      >
        <FileCheck2 size={14} /> Record
      </button>
      <button
        type="button"
        className="btn"
        disabled={busy}
        onClick={() => void send("failed")}
        data-testid={`irn-fail-${row.id}`}
      >
        <Link2Off size={14} /> Portal refused
      </button>
      {error && (
        <div className="login-error irn-record-error" data-testid={`irn-error-${row.id}`}>
          {error}
        </div>
      )}
    </>
  );
}

export default function IrnQueue() {
  const [tab, setTab] = useState<IrnStatus>("pending");
  const [queue, setQueue] = useState<QueueT>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    api
      .get("/sell/irn-queue", { params: { status: tab } })
      .then((r) => setQueue(r.data))
      .catch((e) => setError(apiErrorMessage(e)))
      .finally(() => setLoading(false));
  }, [tab]);

  useEffect(load, [load]);

  const rows = queue?.rows ?? [];

  return (
    <div className="page-pad">
      <PageHeader lead="Business bills waiting on an e-invoice reference. Thirty days from the sale." />

      {queue && queue.overdue_count > 0 && (
        <p className="warn-note" data-testid="irn-overdue-banner">
          <AlertTriangle size={15} />{" "}
          {queue.overdue_count === 1
            ? "1 bill is past its thirty days."
            : `${queue.overdue_count} bills are past their thirty days.`}{" "}
          A bill without an IRN is not a valid tax invoice, and its customer cannot claim the
          credit.
        </p>
      )}

      {/* The house chip picker, not a tab bar of this screen's own: three
          filters over one list is exactly what it is for, and a second way of
          drawing a segmented control is a second thing to keep in step. */}
      <div className="filter-bar chip-picker" data-testid="irn-tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            aria-pressed={tab === t.key}
            className={`chip chip-pick ${tab === t.key ? "chip-navy" : ""}`}
            onClick={() => setTab(t.key)}
            data-testid={`irn-tab-${t.key}`}
          >
            {t.label}
            {t.key === "pending" && queue ? ` (${queue.pending_count})` : ""}
          </button>
        ))}
      </div>

      {error && (
        <div className="login-error" data-testid="irn-load-error">
          {error}
        </div>
      )}

      {loading ? (
        <p className="lead">Loading…</p>
      ) : rows.length === 0 ? (
        <div className="card section-card" data-testid="irn-empty">
          {tab === "pending"
            ? "Nothing waiting. Every business bill has its reference."
            : "Nothing here."}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data irn-table" data-testid="irn-table">
            <thead>
              <tr>
                {/* No Store column: `26-27/DEO/SAL/2` already names it, and a
                    column that repeats the middle of the one beside it costs a
                    clerk the width of the button they came here to press. */}
                <th>Bill</th>
                <th>Buyer</th>
                <th>Split</th>
                <th className="num">Value</th>
                <th className="num">Tax</th>
                <th>Billed</th>
                <th>Due</th>
                <th>{tab === "pending" || tab === "failed" ? "Record the IRN" : "IRN"}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} data-testid={`irn-row-${row.id}`}>
                  <td>
                    <b className="mono">{row.doc_number}</b>
                  </td>
                  <td className="irn-buyer">
                    <span className="mono">{row.buyer_gstin}</span>
                    {row.customer_name ? <div className="muted-cell">{row.customer_name}</div> : null}
                  </td>
                  <td>{TAX_KIND_WORDS[row.b2b_tax_kind] || "—"}</td>
                  <td className="num">
                    <Money paise={row.net_paise} />
                  </td>
                  <td className="num">
                    <Money paise={row.gst_paise} />
                  </td>
                  <td>{fmtDate(row.billed_at)}</td>
                  <td>
                    {fmtDate(row.due_on)}
                    <div>
                      <Clock days={row.days_left} />
                    </div>
                  </td>
                  <td>
                    {row.status === "generated" ? (
                      <>
                        <span className="mono" data-testid={`irn-value-${row.id}`}>
                          <ReceiptText size={13} /> {row.irn}
                        </span>
                        <div className="muted-cell">
                          {row.handled_by_name}
                          {row.handled_at ? ` · ${fmtDate(row.handled_at)}` : ""}
                        </div>
                      </>
                    ) : (
                      <div className="irn-record">
                        <RecordPanel row={row} onDone={load} />
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {queue?.truncated && (
        <p className="muted-cell" data-testid="irn-truncated" style={{ marginTop: 10 }}>
          Older settled bills are not shown. Everything still waiting is on this list; nothing
          pending is ever cut.
        </p>
      )}
    </div>
  );
}
