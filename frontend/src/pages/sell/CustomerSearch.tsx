import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Printer, Search, X } from "lucide-react";

import { Note } from "../../components/Note";
import { PageHeader } from "../../components/PageHeader";
import "../../components/SearchBox.css";
import { SegmentedControl } from "../../components/SegmentedControl";
import { Pane, SplitView } from "../../components/SplitView";
import { EmptyState, ErrorState } from "../../components/States";
import { Status } from "../../components/Status";
import { api, apiErrorMessage } from "../../lib/api";
import type { ApiRead, ApiSchemas } from "../../lib/api";
import { withQuery } from "../../lib/query";
import { Money, formatDateTime } from "../../lib/format";
import { browserPrintAdapter } from "../../till/print";
import { postedReceiptHtml } from "../../till/receipt";
import { tenderWords } from "../../till/tender";
import type { PostedBill } from "../../till/receipt";
import { BillScope } from "./BillScope";
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

  /** Print from a fresh read of the exact bill, never from the copy on screen.
   *  What was opened a while ago may have been fetched for a session, a field
   *  grant or a scope that has since changed; the server answers again, for
   *  who is signed in now, and a refusal prints nothing and drops the copy. */
  async function reprint(bill: PostedBill) {
    setPrintProblem("");
    let current: PostedBill;
    try {
      const { data } = await api.get(`/sell/sales/${bill.doc_number}`);
      current = data as PostedBill;
    } catch (e) {
      setOpen(null);
      setPrintProblem(apiErrorMessage(e));
      return;
    }
    setOpen(current);
    const outcome = await browserPrintAdapter.print(postedReceiptHtml(current));
    setPrintProblem(outcome.ok ? "" : outcome.reason);
  }

  return (
    <div className="page-pad">
      <PageHeader
        title="Bills"
        lead="Any bill at your stores, by mobile number, name or bill number. A past bill is never edited: print it again."
        actions={<BillScope />}
      />

      <div className="page">
        <div className="filter-bar find-bar" data-testid="find-bar">
          <SegmentedControl
            label="Search by"
            className="find-keys"
            value={key}
            options={KEYS.map((option) => ({
              value: option.key,
              label: option.label,
              testId: `find-key-${option.key}`,
            }))}
            onChange={(next) => {
              setKey(next);
              setRows(null);
              setOpen(null);
            }}
          />
          <div className="searchbox find-field">
            <Search size={16} aria-hidden="true" />
            <input
              id="find-term"
              type="search"
              aria-label={KEYS.find((o) => o.key === key)!.label}
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
            className="btn btn-primary"
            disabled={loading}
            data-testid="find-search"
            onClick={() => void search()}
          >
            {loading ? "Searching…" : "Search"}
          </button>
        </div>

        {error && <ErrorState message={error} testId="find-error" />}
        {printProblem && (
          <Note tone="warn" testId="find-print-problem">
            {printProblem} The bill is unchanged - try again when the printer is ready.
          </Note>
        )}

        {rows === null && !error && (
          <EmptyState testId="find-start">
            Search by mobile number, customer name or bill number to find a bill.
          </EmptyState>
        )}
        {rows !== null && rows.length === 0 && !error && (
          <EmptyState testId="find-empty">
            No bill matches that. Check the number, or try the mobile the customer gave at the
            counter.
          </EmptyState>
        )}

        {/* The list and the open bill side by side, as on the counter's own list. */}
        <SplitView
          pane={
            open && (
              <Pane label={`Bill ${open.doc_number}`} testId="find-detail">
                <BillDetail
                  bill={open}
                  onReprint={() => void reprint(open)}
                  onClose={() => setOpen(null)}
                />
              </Pane>
            )
          }
        >
          {rows !== null && rows.length > 0 && (
            <div className="find-results" data-testid="find-results">
              <p className="find-count" role="status">
                {rows.length === 1 ? "1 bill" : `${rows.length} bills`}
                {/* The server caps the list rather than paging it: this is for
                  finding one customer's bill, not for exporting the day. */}
                {rows.length === 50 ? ", the most recent 50. Narrow the search." : ""}
              </p>
              <div className="table-wrap capped">
                <table className="data" data-testid="find-rows">
                  <thead>
                    <tr>
                      <th scope="col">Bill</th>
                      <th scope="col">When</th>
                      <th scope="col">Customer</th>
                      <th scope="col">What</th>
                      <th scope="col" className="num">
                        Total
                      </th>
                      <th scope="col" className="act">
                        <span className="sr-only">Action</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => {
                      const selected = open?.doc_number === row.doc_number;
                      return (
                        <tr
                          key={row.id}
                          className={`find-row${selected ? " is-selected" : ""}`}
                          data-testid={`find-row-${row.id}`}
                          onClick={() => void openBill(row)}
                        >
                          <td className="mono find-doc">{row.doc_number || "not numbered"}</td>
                          <td>{formatDateTime(row.billed_at)}</td>
                          <td>
                            {row.customer_name || "—"}
                            {row.customer_mobile ? (
                              <span className="sub">{row.customer_mobile}</span>
                            ) : null}
                          </td>
                          <td>{row.lines_summary}</td>
                          <td className="num">
                            <Money paise={row.net_paise} />
                          </td>
                          <td className="act">
                            <button
                              type="button"
                              className="btn btn-sm"
                              data-testid={`find-open-${row.id}`}
                              onClick={(event) => {
                                event.stopPropagation();
                                void openBill(row);
                              }}
                            >
                              {selected ? "Close" : "Open"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </SplitView>
      </div>
    </div>
  );
}

/** The whole bill, read-only. Every field is text - there is not an input, a
 *  select or a save in this pane, because there is nothing a person is allowed
 *  to change about a bill that has printed. */
function BillDetail({
  bill,
  onReprint,
  onClose,
}: {
  bill: PostedBill;
  onReprint: () => void;
  onClose: () => void;
}) {
  return (
    <>
      <div className="find-detail-head">
        <div>
          <p className="find-sub">{bill.store_name || bill.store_code}</p>
          <h2 className="heading mono">{bill.doc_number}</h2>
          <p className="find-sub">
            {formatDateTime(bill.billed_at)}
            {bill.customer_name || bill.customer_mobile
              ? `, ${[bill.customer_name, bill.customer_mobile].filter(Boolean).join(", ")}`
              : ""}
          </p>
        </div>
        <button
          type="button"
          className="btn btn-sm find-close"
          aria-label="Close the bill"
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      <button
        type="button"
        className="btn btn-primary"
        data-testid="find-reprint"
        onClick={onReprint}
      >
        <Printer size={16} aria-hidden="true" /> Print again
      </button>

      <ul className="find-lines" data-testid="find-detail-lines" aria-label="Lines">
        {bill.lines.map((line) => (
          <li key={line.line_no} data-testid={`find-detail-line-${line.line_no}`}>
            <span className="find-line-item">
              {[line.brand, line.item, line.design, line.size, line.color]
                .filter(Boolean)
                .join(" · ") ||
                line.manual_desc ||
                "—"}
              {line.direction === "return" ? (
                <>
                  {" "}
                  <Status tone="blue">Returned</Status>
                </>
              ) : null}
            </span>
            <span className="num">
              <Money paise={line.net_paise} />
            </span>
            <span className="find-sub">
              <span className="mono">{line.barcode}</span>, {line.qty} ×{" "}
              <Money paise={line.mrp_paise} />
              {line.season ? `, ${line.season}` : ""}
              {line.salesman_name || line.salesman_code
                ? `, sold by ${line.salesman_name || line.salesman_code}`
                : ""}
            </span>
          </li>
        ))}
      </ul>

      <dl className="dl find-totals">
        <dt>Gross</dt>
        <dd>
          <Money paise={bill.gross_paise} />
        </dd>
        <dt>You saved</dt>
        <dd>
          <Money paise={bill.discount_paise} />
        </dd>
        <dt>Tax included</dt>
        <dd>
          <Money paise={bill.gst_paise} />
        </dd>
        <dt className="find-paid">Paid</dt>
        <dd className="find-paid">
          <Money paise={bill.net_paise} />
        </dd>
        <dt>Taken by</dt>
        <dd className="find-tenders" data-testid="find-detail-tenders">
          {bill.tenders.map((t) => tenderWords(t)).join(" + ") || "Nothing taken"}
        </dd>
      </dl>
    </>
  );
}
