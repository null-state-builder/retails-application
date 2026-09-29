import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Printer, Search } from "lucide-react";

import { PageHeader } from "../../components/PageHeader";
import { api, apiErrorMessage } from "../../lib/api";
import type { ApiRead, ApiSchemas } from "../../lib/api";
import { withQuery } from "../../lib/query";
import { Money, formatDateTime } from "../../lib/format";
import { browserPrintAdapter } from "../../till/print";
import { postedReceiptHtml } from "../../till/receipt";
import { tenderWords } from "../../till/tender";
import type { PostedBill } from "../../till/receipt";
import "./CustomerSearch.css";

// ---------------------------------------------------------------------------
// Find a bill (#185, requirements E1 and E2)
// ---------------------------------------------------------------------------
//
// A customer comes back holding a mobile number, a name, or a scrap of paper
// with a bill number on it. This screen finds their bill and prints it again.
//
// **There is no edit affordance anywhere on it, and there is no endpoint behind
// one.** A posted bill is corrected by another document - an exchange, a return -
// and never by changing what was printed (A7). That is not a permission this
// screen is missing; `sell/views.py` has no writer to call.
//
// It is a server screen rather than a till one, and that is deliberate too. The
// counter's local copy holds only what it has not yet synced, so a bill from last
// month, or from the machine that was replaced, is only ever on the server. It
// therefore needs the line - and says so plainly when there is none, rather than
// answering "no bills found" for a question it never got to ask.

/** One row of the result list, exactly as `SaleListCreateView` publishes it
 *  (`responses=SaleRowSerializer(many=True)`) - taken from the generated client
 *  rather than copied by hand (#192). The hand copy said `doc_number: string`;
 *  the serializer has always been able to answer null, and the screen below now
 *  has to say so instead of printing the word "null" into a URL. */
type SaleRow = ApiRead<ApiSchemas["SaleRow"]>;

/** The three keys the contract takes, as the counter would say them. */
const KEYS = [
  { key: "mobile", label: "Mobile number", placeholder: "9876543210" },
  { key: "name", label: "Customer name", placeholder: "Sharma" },
  { key: "doc", label: "Bill number", placeholder: "74, or 26-27/DEO/SAL/74" },
] as const;

type SearchKey = (typeof KEYS)[number]["key"];

export default function CustomerSearchPage() {
  // A bill number can arrive in the URL - the day summary links an exception
  // straight to the bill it is about (#188), and "go and read the bill" is the
  // first thing anybody clearing one does. Anything else and the screen opens on
  // its own default, exactly as before.
  const [params] = useSearchParams();
  const linkedDoc = (params.get("doc") ?? "").trim();
  const [key, setKey] = useState<SearchKey>(linkedDoc ? "doc" : "mobile");
  const [term, setTerm] = useState(linkedDoc);
  const [rows, setRows] = useState<SaleRow[] | null>(null);
  const [open, setOpen] = useState<PostedBill | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [printProblem, setPrintProblem] = useState("");

  const runSearch = useCallback(async (searchKey: SearchKey, q: string) => {
    if (!q) {
      setError("Type a mobile number, a name or a bill number to search for.");
      return;
    }
    setLoading(true);
    setError("");
    setPrintProblem("");
    setOpen(null);
    try {
      const { data } = await api.get(withQuery("/sell/sales", { [searchKey]: q }));
      setRows(data as SaleRow[]);
    } catch (e) {
      setError(apiErrorMessage(e));
      setRows(null);
    } finally {
      setLoading(false);
    }
  }, []);

  function search() {
    void runSearch(key, term.trim());
  }

  // Search once for a bill named in the URL, and never again - the box is a
  // person's to type in after that, and a search that re-ran on every render
  // would throw away whatever they had started.
  const linkSearched = useRef(false);
  useEffect(() => {
    if (!linkedDoc || linkSearched.current) return;
    linkSearched.current = true;
    void runSearch("doc", linkedDoc);
  }, [linkedDoc, runSearch]);

  /** Open one bill, read-only. Fetched rather than expanded from the row: the
   *  list carries a summary, and what is printed has to be the whole document. */
  async function openBill(row: SaleRow) {
    setError("");
    setPrintProblem("");
    // A bill is fetched *by its number*, so a row without one has nothing to
    // fetch. Before #192 the hand-written row shape swore there was always a
    // number, and this asked the server for `/sell/sales/null`.
    if (!row.doc_number) {
      setError("That bill has no number yet, so it cannot be opened.");
      return;
    }
    if (open?.doc_number === row.doc_number) {
      setOpen(null);
      return;
    }
    try {
      const { data } = await api.get(`/sell/sales/${row.doc_number}`);
      setOpen(data as PostedBill);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function reprint(bill: PostedBill) {
    const outcome = await browserPrintAdapter.print(postedReceiptHtml(bill));
    setPrintProblem(outcome.ok ? "" : outcome.reason);
  }

  return (
    <div className="page-pad">
      <PageHeader lead="Find a bill by mobile number, name or bill number. Print it again - a past bill is never edited." />

      <div className="card section-card find-bar" data-testid="find-bar">
        <div className="find-keys" role="group" aria-label="Search by">
          {KEYS.map((option) => (
            <button
              key={option.key}
              type="button"
              className={`btn${key === option.key ? " btn-cta" : ""}`}
              aria-pressed={key === option.key}
              data-testid={`find-key-${option.key}`}
              onClick={() => {
                setKey(option.key);
                setRows(null);
                setOpen(null);
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="field find-field">
          <label htmlFor="find-term">{KEYS.find((o) => o.key === key)!.label}</label>
          <input
            id="find-term"
            className="input"
            autoComplete="off"
            value={term}
            placeholder={KEYS.find((o) => o.key === key)!.placeholder}
            data-testid="find-term"
            onChange={(e) => setTerm(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              void search();
            }}
          />
        </div>
        <button
          type="button"
          className="btn btn-cta"
          disabled={loading}
          data-testid="find-search"
          onClick={() => void search()}
        >
          <Search size={15} />
          {loading ? "Searching…" : "Search"}
        </button>
        <Link className="btn" to="/sell" data-testid="find-back">
          Back to billing
        </Link>
      </div>

      {error && (
        <div className="warn-note" data-testid="find-error">
          {error}
        </div>
      )}
      {printProblem && (
        <div className="warn-note" data-testid="find-print-problem">
          {printProblem} The bill is unchanged - try again when the printer is ready.
        </div>
      )}

      {rows !== null && rows.length === 0 && !error && (
        <div className="card section-card" data-testid="find-empty">
          <p className="lead">
            No bill at this store matches that. Check the number, or try the mobile the customer
            gave at the counter.
          </p>
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <div className="card section-card" data-testid="find-results">
          <p className="eyebrow">
            {rows.length === 1 ? "1 bill" : `${rows.length} bills`}
            {/* The server caps the list rather than paging it: this is for
                finding one customer's bill, not for exporting the day. */}
            {rows.length === 50 ? " - the most recent 50. Narrow the search." : ""}
          </p>
          <table className="lines-table" data-testid="find-rows">
            <thead>
              <tr>
                <th>Bill</th>
                <th>When</th>
                <th>Customer</th>
                <th>What</th>
                <th className="num">Total</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} data-testid={`find-row-${row.id}`}>
                  <td className="mono">{row.doc_number || "not numbered"}</td>
                  <td>{formatDateTime(row.billed_at)}</td>
                  <td>
                    {row.customer_name || "—"}
                    {row.customer_mobile ? (
                      <span className="lead"> · {row.customer_mobile}</span>
                    ) : null}
                  </td>
                  <td>{row.lines_summary}</td>
                  <td className="num">
                    <Money paise={row.net_paise} />
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      data-testid={`find-open-${row.id}`}
                      onClick={() => void openBill(row)}
                    >
                      {open?.doc_number === row.doc_number ? "Close" : "Open"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && <BillDetail bill={open} onReprint={() => void reprint(open)} />}
    </div>
  );
}

/** The whole bill, read-only. Every field is text - there is not an input, a
 *  select or a save on this card, because there is nothing a person is allowed
 *  to change about a bill that has printed. */
function BillDetail({ bill, onReprint }: { bill: PostedBill; onReprint: () => void }) {
  return (
    <div className="card section-card" data-testid="find-detail">
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <div>
          <p className="eyebrow">{bill.store_name || bill.store_code}</p>
          <h3 className="h3 mono">{bill.doc_number}</h3>
          <p className="lead">
            {formatDateTime(bill.billed_at)}
            {bill.customer_name || bill.customer_mobile
              ? ` · ${[bill.customer_name, bill.customer_mobile].filter(Boolean).join(" · ")}`
              : ""}
          </p>
        </div>
        <div className="spacer" />
        <button
          type="button"
          className="btn btn-cta"
          data-testid="find-reprint"
          onClick={onReprint}
        >
          <Printer size={15} /> Print again
        </button>
      </div>

      <table className="lines-table" data-testid="find-detail-lines">
        <thead>
          <tr>
            <th>Item</th>
            <th>Barcode</th>
            <th>Season</th>
            <th>Salesman</th>
            <th className="num">Qty</th>
            <th className="num">Rate</th>
            <th className="num">Net</th>
          </tr>
        </thead>
        <tbody>
          {bill.lines.map((line) => (
            <tr key={line.line_no} data-testid={`find-detail-line-${line.line_no}`}>
              <td>
                {[line.brand, line.item, line.design, line.size, line.color]
                  .filter(Boolean)
                  .join(" · ") ||
                  line.manual_desc ||
                  "—"}
                {line.direction === "return" ? (
                  <span className="chip chip-navy">Returned</span>
                ) : null}
              </td>
              <td className="mono">{line.barcode}</td>
              <td>{line.season || "—"}</td>
              <td>{line.salesman_name || line.salesman_code || "—"}</td>
              <td className="num">{line.qty}</td>
              <td className="num">
                <Money paise={line.mrp_paise} />
              </td>
              <td className="num">
                <Money paise={line.net_paise} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="find-totals">
        <Figure label="Gross" paise={bill.gross_paise} />
        <Figure label="You saved" paise={bill.discount_paise} />
        <Figure label="Tax included" paise={bill.gst_paise} />
        <Figure label="Paid" paise={bill.net_paise} strong />
        <span className="find-tenders" data-testid="find-detail-tenders">
          {bill.tenders.map((t) => tenderWords(t)).join(" + ") || "Nothing taken"}
        </span>
      </div>
    </div>
  );
}

function Figure({ label, paise, strong }: { label: string; paise: number; strong?: boolean }) {
  return (
    <span className="find-figure">
      <span className="find-figure-label">{label}</span>
      <span className={strong ? "find-figure-strong" : undefined}>
        <Money paise={paise} />
      </span>
    </span>
  );
}
