import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Printer, RefreshCw, Replace, Search, X } from "lucide-react";

import { FilterChip, FilterChips } from "../../components/FilterChip";
import { Note } from "../../components/Note";
import { OperationsPage } from "../../components/OperationsPage";
import { PageHeader } from "../../components/PageHeader";
import { Pane, SplitView } from "../../components/SplitView";
import { EmptyState, ErrorState } from "../../components/States";
import { Status } from "../../components/Status";
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
import { BillScope } from "./BillScope";
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
  const [onlySynced, setOnlySynced] = useState(false);
  const [onlyUnsynced, setOnlyUnsynced] = useState(false);

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
    if (bill.posted) {
      try {
        const fresh = await api.get(`/sell/sales/${encodeURIComponent(bill.posted.doc_number)}`);
        bill = { ...bill, posted: fresh.data };
      } catch (error) {
        setPrintProblem(apiErrorMessage(error));
        return;
      }
    } else if (till?.onlineAlpha) {
      setPrintProblem("Only a server-accepted bill can be printed.");
      return;
    }
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

  // Synced / Not yet synced narrow the list on screen; both or neither is all.
  const syncedCount = rows?.filter((r) => r.synced).length ?? 0;
  const unsyncedCount = (rows?.length ?? 0) - syncedCount;
  const shown =
    rows === null || onlySynced === onlyUnsynced
      ? rows
      : rows.filter((r) => r.synced === onlySynced);
  const shownTotal = (shown ?? []).reduce((sum, r) => sum + r.net_paise, 0);
  // One day's list needs only the time; a range keeps the date on every row.
  const oneDay = from === to;

  // The open bill as it prints, for the preview in the pane. The same function
  // the reprint uses, from the copy already on screen - nothing is fetched.
  const store = world.store;
  const receipt = useMemo(() => {
    if (!open) return null;
    if (open.posted) return postedReceiptHtml(open.posted);
    if (open.queued)
      return receiptHtml(open.queued, store ?? { code: "", gstin: "", state_code: "" });
    return null;
  }, [open, store]);

  return (
    <OperationsPage>
      <PageHeader
        title="Bills"
        lead="Today's bills, including any not yet synced. A bill is never edited: print it again, or start an exchange from it."
        actions={<BillScope />}
      />

      <div className="bills-bar" data-testid="bills-bar">
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
        {rows !== null && rows.length > 0 && (
          <FilterChips label="Sync" testId="bills-sync-filter">
            <FilterChip
              pressed={onlySynced}
              count={syncedCount}
              onClick={() => setOnlySynced((on) => !on)}
              testId="bills-filter-synced"
            >
              Synced
            </FilterChip>
            <FilterChip
              pressed={onlyUnsynced}
              count={unsyncedCount}
              onClick={() => setOnlyUnsynced((on) => !on)}
              testId="bills-filter-unsynced"
            >
              Not yet synced
            </FilterChip>
          </FilterChips>
        )}
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
          className="btn"
          disabled={loading}
          data-testid="bills-search"
          onClick={() => void load()}
        >
          <Search size={15} />
          {loading ? "Looking…" : "Search"}
        </button>
      </div>

      {error && <ErrorState message={error} testId="bills-error" />}

      {rows !== null && rows.length === 0 && (
        <EmptyState testId="bills-empty">
          {term.trim() ? "No bill in this range matches that." : "No bills in this range."}
        </EmptyState>
      )}
      {rows !== null && rows.length > 0 && shown?.length === 0 && (
        <EmptyState testId="bills-filtered-empty">
          {onlySynced
            ? "No bill in this range has synced yet."
            : "Every bill in this range has synced."}
        </EmptyState>
      )}

      {/* The list and the open bill side by side, so the next bill is one click
          away instead of a scroll back up past the one just read. */}
      <SplitView
        pane={
          open ? (
            <Pane label={`Bill ${open.row.doc_number || "not numbered"}`} testId="bills-pane">
              <BillDetail
                bill={open}
                receipt={receipt}
                printProblem={printProblem}
                onReprint={() => void reprint(open)}
                onExchange={() => exchange(open.row)}
                onClose={() => {
                  setPrintProblem("");
                  setOpen(null);
                }}
              />
            </Pane>
          ) : undefined
        }
      >
        {shown !== null && shown.length > 0 && (
          <div
            className="table-wrap"
            role="region"
            aria-label="Bill history"
            tabIndex={0}
            data-testid="bills-results"
          >
            <table className="data" data-testid="bills-rows">
              <thead>
                <tr>
                  <th>Bill</th>
                  <th>When</th>
                  <th>Customer</th>
                  <th>Paid with</th>
                  <th className="num">Total</th>
                  <th>Sync</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((row) => {
                  const selected = open?.row.doc_number === row.doc_number;
                  return (
                    <tr
                      key={row.doc_number}
                      className={`bills-row${selected ? " is-selected" : ""}`}
                      data-testid={`bills-row-${row.doc_number}`}
                      // The row opens the bill beside the list; the bill number
                      // is the same action for a keyboard.
                      onClick={() => void openBill(row)}
                    >
                      <td>
                        <button
                          type="button"
                          className="bills-billno mono"
                          aria-current={selected ? "true" : undefined}
                          data-testid={`bills-open-${row.doc_number}`}
                          onClick={(event) => {
                            event.stopPropagation();
                            void openBill(row);
                          }}
                        >
                          {row.doc_number || "not numbered"}
                        </button>
                      </td>
                      <td className="bills-when">
                        {oneDay ? timeOf(row.billed_at) : formatDateTime(row.billed_at)}
                      </td>
                      <td>
                        {row.customer_name ? (
                          <span className="bills-customer">{row.customer_name}</span>
                        ) : null}
                        {row.customer_mobile ? (
                          <span className={row.customer_name ? "sub" : undefined}>
                            {row.customer_mobile}
                          </span>
                        ) : null}
                        {!row.customer_name && !row.customer_mobile ? (
                          <span className="sub">—</span>
                        ) : null}
                      </td>
                      <td>{tenderSummary(row)}</td>
                      <td className="num">
                        <Money paise={row.net_paise} />
                      </td>
                      <td>
                        {row.synced ? (
                          <Status tone="green" testId="bills-synced">
                            Synced
                          </Status>
                        ) : (
                          <Status tone="amber" testId="bills-unsynced">
                            Not yet synced
                          </Status>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr>
                  <td colSpan={4} data-testid="bills-count">
                    {shown.length === 1
                      ? "1 bill"
                      : `${shown.length.toLocaleString("en-IN")} bills`}
                  </td>
                  <td className="num">
                    <Money paise={shownTotal} />
                  </td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </SplitView>
    </OperationsPage>
  );
}

/** A bill on screen: the row it came from, and whichever whole copy exists. */
interface OpenBill {
  row: BillRow;
  posted: PostedBill | null;
  queued: QueuedBill | null;
}

/** "10:38 am": the time alone, for a list that is all one day. */
function timeOf(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
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
  receipt,
  printProblem,
  onReprint,
  onExchange,
  onClose,
}: {
  bill: OpenBill;
  /** The bill as it prints, or null when there is no whole copy to draw. */
  receipt: string | null;
  printProblem: string;
  onReprint: () => void;
  onExchange: () => void;
  onClose: () => void;
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
  const docNumber = row.doc_number || "not numbered";

  return (
    <div className="bills-detail" data-testid="bills-detail">
      <div className="pane-head">
        <div className="bills-detail-title">
          <h3 className="heading mono">{docNumber}</h3>
          <p className="bills-detail-sub">
            {formatDateTime(row.billed_at)}
            {row.customer_name || row.customer_mobile
              ? `, ${[row.customer_name, row.customer_mobile].filter(Boolean).join(", ")}`
              : ""}
          </p>
          {row.synced ? (
            <Status tone="green">At head office</Status>
          ) : (
            <Status tone="amber">In this counter&apos;s queue - not yet synced</Status>
          )}
        </div>
        <button
          type="button"
          className="btn btn-sm bills-detail-close"
          aria-label="Close the bill"
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      <div className="bills-detail-actions">
        <button
          type="button"
          className="btn btn-primary"
          data-testid="bills-reprint"
          onClick={onReprint}
        >
          <Printer size={15} /> Reprint
        </button>
        <button type="button" className="btn" data-testid="bills-exchange" onClick={onExchange}>
          <Replace size={15} /> Exchange
        </button>
      </div>

      {printProblem && (
        <Note tone="warn" role="status" testId="bills-print-problem">
          {printProblem} The bill is unchanged - try again when the printer is ready.
        </Note>
      )}

      {/* Ticket 13: the credit note this exchange issued beside the invoice. */}
      {posted?.credit_note ? (
        <p className="bills-detail-sub" data-testid="bills-credit-note">
          Credit note{" "}
          <strong className="mono">
            {posted.credit_note.number || "not yet numbered - head office is told"}
          </strong>
          {posted.credit_note.original ? ` against ${posted.credit_note.original}` : ""}
          {posted.credit_note.status === "cancelled" ? " - cancelled with this bill" : ""}
        </p>
      ) : null}

      <table className="data bills-detail-lines" data-testid="bills-detail-lines">
        <caption className="sr-only">Bill lines</caption>
        <thead>
          <tr>
            <th>Item</th>
            <th className="num">Qty</th>
            <th className="num">Net</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr key={line.line_no} data-testid={`bills-detail-line-${line.line_no}`}>
              <td>
                {line.what}
                {line.returned ? (
                  <>
                    {" "}
                    <Status tone="blue">Returned</Status>
                  </>
                ) : null}
                <span className="sub mono">{line.barcode}</span>
              </td>
              <td className="num">{line.qty}</td>
              <td className="num">
                <Money paise={line.net_paise} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <dl className="bills-tenders" data-testid="bills-detail-tenders">
        {tenders.map((tender, index) => (
          <div key={index} className="bills-tender" data-testid={`bills-tender-${tender.mode}`}>
            <dt>
              {tenderWords(tender)}
              <span className="bills-tender-proof">{tenderProofWords(tender.upi_state)}</span>
            </dt>
            <dd>
              <Money paise={tender.amount_paise} />
            </dd>
          </div>
        ))}
        <div className="bills-tender bills-tender-total">
          <dt>Paid</dt>
          <dd>
            <Money paise={row.net_paise} />
          </dd>
        </div>
      </dl>

      {receipt && <ReceiptPreview html={receipt} label={`Bill ${docNumber}, print preview`} />}
    </div>
  );
}

/**
 * The 80 mm paper on screen: the very document the reprint sends, drawn in a
 * frame of its own so its print styles never meet the app's. Black on white in
 * both themes, because it shows the paper. The frame runs no script; it is
 * same-origin only so its height can follow the bill's length.
 */
function ReceiptPreview({ html, label }: { html: string; label: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(480);
  return (
    <div className="bills-receipt-tray">
      <iframe
        ref={frame}
        className="bills-receipt"
        title={label}
        sandbox="allow-same-origin"
        srcDoc={html}
        style={{ height }}
        onLoad={() => {
          const doc = frame.current?.contentDocument;
          if (doc) setHeight(Math.ceil(doc.documentElement.scrollHeight));
        }}
      />
    </div>
  );
}
