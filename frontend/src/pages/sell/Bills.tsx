import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Printer, RefreshCw, Replace, Search } from "lucide-react";

import { PageHeader } from "../../components/PageHeader";
import { api, apiErrorMessage } from "../../lib/api";
import type { ApiRead, ApiSchemas } from "../../lib/api";
import { Money, formatDateTime } from "../../lib/format";
import { withQuery } from "../../lib/query";
import {
  billsInRange,
  billsMatching,
  mergeBillRows,
  queuedAsRow,
  tenderSummary,
  todayKey,
} from "../../till/bills";
import type { BillRow } from "../../till/bills";
import { browserPrintAdapter } from "../../till/print";
import { postedReceiptHtml, receiptHtml } from "../../till/receipt";
import type { PostedBill } from "../../till/receipt";
import { tenderProofWords, tenderWords } from "../../till/tender";
import { useTill } from "../../till/TillProvider";
import { useTillWorld } from "../../till/useTillWorld";
import type { QueuedBill } from "../../till/types";
import "./CustomerSearch.css";
import "./Bills.css";

// ---------------------------------------------------------------------------
// Bills (OPS-08, store and warehouse operations PRD §9.2)
// ---------------------------------------------------------------------------
//
// What this counter has done, and where each bill has got to. It opens on today
// because that is the question a store person actually has - "did the ₹4,800 one
// go through?" - and the date range is for the rarer walk back.
//
// **Two sources, one list.** Head office holds every bill it has been told
// about; this browser's queue holds the ones it has not been able to tell anyone
// about yet. A bill rung up ten minutes ago on a shop with no line exists
// nowhere else in the world, and it is exactly the bill somebody comes back
// about. So both are read and joined (`till/bills.ts`), an unsynced bill says so
// on its row, and a bill that has since gone in appears once.
//
// **Nothing here can change a bill.** There is no edit affordance and no
// endpoint behind one: a posted bill is corrected by another document - an
// exchange, a return - never by changing what was printed (A7). The two things a
// person can do from a bill are print it again, which reissues the *same* bill
// with the same number, and start an exchange against it, which opens the
// counter with the original loaded and leaves the original exactly as it was.
//
// A print that fails says so and offers another go. It never writes anything, so
// there is no second bill to be made and none to be lost (R-POS-007).

type SaleRow = ApiRead<ApiSchemas["SaleRow"]>;

export default function BillsPage() {
  const navigate = useNavigate();
  const { engine, till } = useTill();
  // The counter's own identity, for the one receipt this screen renders itself:
  // an unsynced bill has no posted copy to read a registration off, so the
  // reprint has to use the shop's own, exactly as the counter did when it
  // printed the first copy.
  const world = useTillWorld(engine?.db ?? null, till?.syncedAt ?? "");
  const today = todayKey();
  const [from, setFrom] = useState(today);
  const [to, setTo] = useState(today);
  const [term, setTerm] = useState("");
  const [rows, setRows] = useState<BillRow[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<OpenBill | null>(null);
  const [printProblem, setPrintProblem] = useState("");

  const db = engine?.db ?? null;

  /** Ask both sides, and join them. The server's refusal is worth saying out
   *  loud - a counter that cannot reach head office still has its own bills, and
   *  the list is honest about showing only those. */
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setPrintProblem("");
    setOpen(null);
    let local: BillRow[] = [];
    if (db) {
      const queued = await db.queue.orderBy("id").reverse().toArray();
      local = billsMatching(billsInRange(queued.map(queuedAsRow), from, to), term);
    }
    try {
      const { data } = await api.get(
        withQuery("/sell/sales", { from, to, ...(term.trim() ? { q: term.trim() } : {}) }),
      );
      setRows(mergeBillRows(local, (data as SaleRow[]).map(serverAsRow)));
    } catch (e) {
      setError(`${apiErrorMessage(e)} Showing this counter's own bills that have not gone in yet.`);
      setRows(local);
    } finally {
      setLoading(false);
    }
  }, [db, from, to, term]);

  // Today's bills, the moment the screen opens. The term is deliberately not a
  // dependency: a search runs when somebody asks for it, not on every keystroke.
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [db, from, to]);

  /** Open one bill, read-only. A synced bill is fetched whole, because what is
   *  printed has to be the whole document; an unsynced one is read out of the
   *  queue, which is the only copy of it anywhere. */
  async function openBill(row: BillRow) {
    setError("");
    setPrintProblem("");
    if (open?.row.doc_number === row.doc_number) {
      setOpen(null);
      return;
    }
    if (!row.synced) {
      const bill = db
        ? (await db.queue.orderBy("id").toArray()).find((b) => b.doc_number === row.doc_number)
        : undefined;
      if (!bill) {
        setError("That bill is no longer in this counter's queue. Refresh the list.");
        return;
      }
      setOpen({ row, queued: bill, posted: null });
      return;
    }
    try {
      const { data } = await api.get(`/sell/sales/${row.doc_number}`);
      setOpen({ row, queued: null, posted: data as PostedBill });
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  /** The same bill again, with the same number. Nothing is written, so a failed
   *  print leaves exactly one bill and offers another go. */
  async function reprint(bill: OpenBill) {
    const html = bill.posted
      ? postedReceiptHtml(bill.posted)
      : receiptHtml(bill.queued!, world.store ?? { code: "", gstin: "", state_code: "" });
    const outcome = await browserPrintAdapter.print(html);
    setPrintProblem(outcome.ok ? "" : outcome.reason);
  }

  /** Start an exchange against this bill: the counter opens in return mode with
   *  the original loaded, exactly as picking it from the counter's own search
   *  does. The original is untouched - the new bill carries the legs. */
  function exchange(row: BillRow) {
    navigate(`/sell?mode=return&doc=${encodeURIComponent(row.doc_number)}`);
  }

  return (
    <div className="page-pad">
      <PageHeader lead="Today's bills at this counter, including any that have not gone in yet. A bill is never edited - print it again, or start an exchange from it." />

      <div className="card section-card bills-bar" data-testid="bills-bar">
        <div className="field">
          <label htmlFor="bills-from">From</label>
          <input
            id="bills-from"
            className="input"
            type="date"
            value={from}
            data-testid="bills-from"
            onChange={(e) => setFrom(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="bills-to">To</label>
          <input
            id="bills-to"
            className="input"
            type="date"
            value={to}
            data-testid="bills-to"
            onChange={(e) => setTo(e.target.value)}
          />
        </div>
        <div className="field bills-search">
          <label htmlFor="bills-term">Customer or bill number</label>
          <input
            id="bills-term"
            className="input"
            autoComplete="off"
            placeholder="Sharma, 9876543210, or 74"
            value={term}
            data-testid="bills-term"
            onChange={(e) => setTerm(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              void load();
            }}
          />
        </div>
        <button
          type="button"
          className="btn btn-cta"
          disabled={loading}
          data-testid="bills-search"
          onClick={() => void load()}
        >
          <Search size={15} />
          {loading ? "Looking…" : "Search"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={loading}
          data-testid="bills-today"
          onClick={() => {
            setTerm("");
            setFrom(today);
            setTo(today);
          }}
        >
          <RefreshCw size={15} /> Today
        </button>
        <Link className="btn" to="/sell" data-testid="bills-back">
          Back to billing
        </Link>
      </div>

      {error && (
        <div className="warn-note" data-testid="bills-error">
          {error}
        </div>
      )}
      {printProblem && (
        <div className="warn-note" data-testid="bills-print-problem">
          {printProblem} The bill is unchanged - try again when the printer is ready.
        </div>
      )}

      {rows !== null && rows.length === 0 && (
        <div className="card section-card" data-testid="bills-empty">
          <p className="lead">No bills in this range at this counter.</p>
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <div className="card section-card" data-testid="bills-results">
          <p className="eyebrow">{rows.length === 1 ? "1 bill" : `${rows.length} bills`}</p>
          <table className="lines-table" data-testid="bills-rows">
            <thead>
              <tr>
                <th>Bill</th>
                <th>When</th>
                <th>Customer</th>
                <th>Paid with</th>
                <th className="num">Total</th>
                <th>Sync</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.doc_number} data-testid={`bills-row-${row.doc_number}`}>
                  <td className="mono">{row.doc_number || "not numbered"}</td>
                  <td>{formatDateTime(row.billed_at)}</td>
                  <td>
                    {row.customer_name || "—"}
                    {row.customer_mobile ? (
                      <span className="lead"> · {row.customer_mobile}</span>
                    ) : null}
                  </td>
                  <td>{tenderSummary(row)}</td>
                  <td className="num">
                    <Money paise={row.net_paise} />
                  </td>
                  <td>
                    {row.synced ? (
                      <span className="chip" data-testid="bills-synced">
                        Synced
                      </span>
                    ) : (
                      <span className="chip chip-amber" data-testid="bills-unsynced">
                        Not yet synced
                      </span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      data-testid={`bills-open-${row.doc_number}`}
                      onClick={() => void openBill(row)}
                    >
                      {open?.row.doc_number === row.doc_number ? "Close" : "Open"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <BillDetail
          bill={open}
          onReprint={() => void reprint(open)}
          onExchange={() => exchange(open.row)}
        />
      )}
    </div>
  );
}

/** A bill on screen: the row it came from, and whichever whole copy exists. */
interface OpenBill {
  row: BillRow;
  posted: PostedBill | null;
  queued: QueuedBill | null;
}

function serverAsRow(row: SaleRow): BillRow {
  return {
    doc_number: row.doc_number ?? "",
    billed_at: row.billed_at,
    customer_name: row.customer_name ?? "",
    customer_mobile: row.customer_mobile ?? "",
    net_paise: row.net_paise ?? 0,
    tenders: (row.tenders ?? []).map((tender) => ({
      mode: tender.mode,
      amount_paise: tender.amount_paise,
      upi_state: tender.upi_state ?? null,
      ...(tender.gift_voucher_number ? { gift_voucher: tender.gift_voucher_number } : {}),
    })),
    synced: true,
    fy: row.fy,
    till_seq: row.till_seq,
  };
}

/**
 * The whole bill, read-only. Every field is text - there is not an input, a
 * select or a save on this card, because there is nothing a person is allowed to
 * change about a bill that has printed.
 *
 * The tender rows say how each one was *proved*, and that is not decoration:
 * a card row and a UPI row the cashier vouched for are the counter's own word,
 * while a bank-confirmed row carries a reference somebody else can check. The
 * two are never merged (R-POS-007), so they are never drawn the same.
 */
function BillDetail({
  bill,
  onReprint,
  onExchange,
}: {
  bill: OpenBill;
  onReprint: () => void;
  onExchange: () => void;
}) {
  const { row, posted, queued } = bill;
  const lines = posted
    ? posted.lines.map((line) => ({
        line_no: line.line_no,
        what:
          [line.brand, line.item, line.design, line.size, line.color].filter(Boolean).join(" · ") ||
          line.manual_desc ||
          "—",
        barcode: line.barcode,
        qty: line.qty,
        net_paise: line.net_paise,
        returned: line.direction === "return",
      }))
    : (queued?.lines ?? []).map((line) => ({
        line_no: line.line_no,
        what: line.manual_desc || line.barcode,
        barcode: line.barcode,
        qty: line.qty,
        net_paise: line.net_paise,
        returned: line.direction === "return",
      }));
  const tenders = posted?.tenders ?? queued?.tenders ?? [];

  return (
    <div className="card section-card" data-testid="bills-detail">
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <div>
          <p className="eyebrow">
            {row.synced ? "At head office" : "In this counter's queue - not yet synced"}
          </p>
          <h3 className="h3 mono">{row.doc_number}</h3>
          <p className="lead">
            {formatDateTime(row.billed_at)}
            {row.customer_name || row.customer_mobile
              ? ` · ${[row.customer_name, row.customer_mobile].filter(Boolean).join(" · ")}`
              : ""}
          </p>
        </div>
        <div className="spacer" />
        <button type="button" className="btn" data-testid="bills-exchange" onClick={onExchange}>
          <Replace size={15} /> Exchange
        </button>
        <button
          type="button"
          className="btn btn-cta"
          data-testid="bills-reprint"
          onClick={onReprint}
        >
          <Printer size={15} /> Print again
        </button>
      </div>

      {/* Ticket 13: the credit note this exchange issued beside the invoice. */}
      {posted?.credit_note ? (
        <p className="lead" data-testid="bills-credit-note">
          Credit note{" "}
          <strong className="mono">
            {posted.credit_note.number || "not yet numbered - head office is told"}
          </strong>
          {posted.credit_note.original ? ` against ${posted.credit_note.original}` : ""}
          {posted.credit_note.status === "cancelled" ? " - cancelled with this bill" : ""}
        </p>
      ) : null}

      <table className="lines-table" data-testid="bills-detail-lines">
        <thead>
          <tr>
            <th>Item</th>
            <th>Barcode</th>
            <th className="num">Qty</th>
            <th className="num">Net</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr key={line.line_no} data-testid={`bills-detail-line-${line.line_no}`}>
              <td>
                {line.what}
                {line.returned ? <span className="chip chip-navy">Returned</span> : null}
              </td>
              <td className="mono">{line.barcode}</td>
              <td className="num">{line.qty}</td>
              <td className="num">
                <Money paise={line.net_paise} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="bills-tenders" data-testid="bills-detail-tenders">
        {tenders.map((tender, index) => (
          <span key={index} className="bills-tender" data-testid={`bills-tender-${tender.mode}`}>
            <span>{tenderWords(tender)}</span>
            <Money paise={tender.amount_paise} />
            <span className="bills-tender-proof">{tenderProofWords(tender.upi_state)}</span>
          </span>
        ))}
        <span className="find-figure">
          <span className="find-figure-label">Paid</span>
          <span className="find-figure-strong">
            <Money paise={row.net_paise} />
          </span>
        </span>
      </div>
    </div>
  );
}
