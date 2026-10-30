import { useState } from "react";
import { FileCheck2, Link2Off, ReceiptText } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { TAX_KIND_WORDS } from "../till/gstin";
import type { B2bTaxKind } from "../till/gstin";
import { Money } from "../lib/format";
import { Note } from "../components/Note";
import { PageHeader } from "../components/PageHeader";
import { UpdatedAgo } from "../components/UpdatedAgo";
import { useCachedGet } from "../lib/cachedQuery";
import { SegmentedControl } from "../components/SegmentedControl";
import { EmptyState } from "../components/States";
import { Status } from "../components/Status";
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
      <Status tone="red" testId="irn-overdue">
        {late === 1 ? "1 day late" : `${late} days late`}
      </Status>
    );
  }
  if (days === 0) return <Status tone="red">Due today</Status>;
  return (
    <Status tone={days <= 7 ? "amber" : "grey"}>
      {days === 1 ? "1 day left" : `${days} days`}
    </Status>
  );
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
      await api.put(`/sell/irn-queue/${row.id}`, {
        status,
        irn: status === "generated" ? irn : "",
      });
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
        className="btn btn-primary btn-sm"
        disabled={busy || !irn}
        onClick={() => void send("generated")}
        data-testid={`irn-save-${row.id}`}
      >
        <FileCheck2 size={14} /> Record
      </button>
      <button
        type="button"
        className="btn btn-sm"
        disabled={busy}
        onClick={() => void send("failed")}
        data-testid={`irn-fail-${row.id}`}
      >
        <Link2Off size={14} /> Portal refused
      </button>
      {error && (
        <div className="irn-record-error">
          <Note tone="error" testId={`irn-error-${row.id}`}>
            {error}
          </Note>
        </div>
      )}
    </>
  );
}

export default function IrnQueue() {
  const [tab, setTab] = useState<IrnStatus>("pending");
  // Through the shared cache: a tab seen before shows at once and is read again
  // behind it; `loading` is true only until the current tab's answer is in.
  const read = useCachedGet<QueueT>("/sell/irn-queue", { params: { status: tab } });
  const queue = read.data;
  const loading = read.loading;
  const error = read.error ? apiErrorMessage(read.error) : "";
  const load = read.reload;

  const rows = queue?.rows ?? [];

  return (
    <div className="page-pad">
      <PageHeader
        lead="Business bills waiting on an e-invoice reference. Thirty days from the sale."
        actions={<UpdatedAgo updatedAt={read.updatedAt} fetching={read.fetching} />}
      />

      {queue && queue.overdue_count > 0 && (
        <Note tone="warn" testId="irn-overdue-banner">
          {queue.overdue_count === 1
            ? "1 bill is past its thirty days."
            : `${queue.overdue_count} bills are past their thirty days.`}{" "}
          A bill without an IRN is not a valid tax invoice, and its customer cannot claim the
          credit.
        </Note>
      )}

      {/* Three states, each swapping the whole list: the house SegmentedControl
          (decision 9), not a tab bar of this screen's own. */}
      <div className="filter-bar">
        <SegmentedControl
          label="Which bills"
          testId="irn-tabs"
          options={TABS.map((t) => ({
            value: t.key,
            label: t.label,
            count: t.key === "pending" && queue ? queue.pending_count : undefined,
            testId: `irn-tab-${t.key}`,
          }))}
          value={tab}
          onChange={setTab}
        />
      </div>

      {error && (
        <Note tone="error" testId="irn-load-error">
          {error}
        </Note>
      )}

      {loading ? (
        <p className="lead">Loading…</p>
      ) : rows.length === 0 ? (
        <EmptyState testId="irn-empty">
          {tab === "pending"
            ? "Nothing waiting. Every business bill has its reference."
            : "Nothing here."}
        </EmptyState>
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
                    {row.customer_name ? (
                      <div className="muted-cell">{row.customer_name}</div>
                    ) : null}
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
