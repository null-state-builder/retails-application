import { Search, Undo2 } from "lucide-react";

import { Money, formatDateTime } from "../../../lib/format";
import {
  describeOriginal,
  refundFor,
  returnableQty,
} from "../../../till/exchange";
import type { OriginalLine, PickedReturn, PickedReturns } from "../../../till/exchange";
import type { FoundBill } from "../../../till/original";
import type { TillKnownCustomer } from "../../../till/types";

export type ReturnSearchKey = "mobile" | "name";

/** One customer-search result. Local rows already carry their full bill; server
 * rows are opened through the detail endpoint only after the cashier picks one. */
export interface ReturnBillMatch {
  doc_number: string;
  billed_at: string;
  customer_name: string;
  customer_mobile: string;
  net_paise: number;
  local: FoundBill | null;
}

const REASONS = ["size", "fit", "colour", "defect", "changed mind", "gift"] as const;

export function ReturnCustomerSearch({
  searchKey,
  term,
  online,
  looking,
  error,
  customers,
  matches,
  onSearchKey,
  onTerm,
  onSearch,
  onPickCustomer,
  onPick,
}: {
  searchKey: ReturnSearchKey;
  term: string;
  online: boolean;
  looking: boolean;
  error: string;
  customers: TillKnownCustomer[];
  matches: ReturnBillMatch[] | null;
  onSearchKey: (key: ReturnSearchKey) => void;
  onTerm: (term: string) => void;
  onSearch: () => void;
  onPickCustomer: (customer: TillKnownCustomer) => void;
  onPick: (match: ReturnBillMatch) => void;
}) {
  return (
    <section className="return-customer-search" data-testid="return-customer-search">
      <div className="return-customer-head">
        <div>
          <strong>Find a customer&rsquo;s bill</strong>
          <span className="muted-cell">Search by the mobile or name they used at billing.</span>
        </div>
        {!online && <span className="return-offline">Offline · head-office history needs the line</span>}
      </div>
      <div className="return-customer-form">
        <div className="bill-mode" role="group" aria-label="Search customer by">
          {(["mobile", "name"] as const).map((key) => (
            <button
              key={key}
              type="button"
              className={`bill-mode-button ${searchKey === key ? "is-active" : ""}`}
              aria-pressed={searchKey === key}
              onClick={() => onSearchKey(key)}
            >
              {key === "mobile" ? "Mobile" : "Name"}
            </button>
          ))}
        </div>
        <input
          className="input"
          autoComplete="off"
          data-testid="return-customer-term"
          placeholder={searchKey === "mobile" ? "9876543210" : "Customer name"}
          value={term}
          onChange={(event) => onTerm(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            onSearch();
          }}
        />
        <button
          type="button"
          className="btn btn-cta"
          data-testid="return-customer-submit"
          disabled={looking || !term.trim()}
          onClick={onSearch}
        >
          <Search size={15} /> {looking ? "Searching…" : "Search"}
        </button>
      </div>
      {customers.length > 0 && (
        <div className="return-known-customers" data-testid="return-known-customers">
          <span className="eyebrow">Known customers</span>
          <div>
            {customers.map((customer) => (
              <button
                key={customer.mobile}
                type="button"
                className="btn"
                onClick={() => onPickCustomer(customer)}
              >
                <strong>{customer.name || "Unnamed customer"}</strong>
                <small className="mono">{customer.mobile}</small>
              </button>
            ))}
          </div>
        </div>
      )}
      {error && <p className="bill-alert">{error}</p>}
      {matches?.length === 0 && !error && (
        <p className="muted-cell">No bill this counter can see matches that customer.</p>
      )}
      {matches && matches.length > 0 && (
        <div className="return-customer-results">
          {matches.map((match) => (
            <button
              key={match.doc_number}
              type="button"
              className="return-customer-row"
              data-testid={`return-customer-bill-${match.doc_number}`}
              onClick={() => onPick(match)}
            >
              <span>
                <strong className="mono">{match.doc_number}</strong>
                <small>
                  {formatDateTime(match.billed_at)} · {[match.customer_name, match.customer_mobile]
                    .filter(Boolean)
                    .join(" · ") || "No customer name"}
                </small>
              </span>
              <span>
                <Money paise={match.net_paise} />
                {match.local && <small>On this till</small>}
              </span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

export function AgainstBill({
  found,
  picked,
  outgoing,
  late,
  windowDays,
  locked,
  onPick,
  onTakeEverything,
  onOutgoing,
  onChangeBill,
}: {
  found: FoundBill;
  picked: PickedReturns;
  outgoing: boolean;
  late: boolean;
  windowDays: number;
  locked: boolean;
  onPick: (line: OriginalLine, patch: Partial<PickedReturn>) => void;
  onTakeEverything: () => void;
  onOutgoing: (outgoing: boolean) => void;
  onChangeBill: () => void;
}) {
  const value = found.net_paise;
  const marked = Object.values(picked).reduce((sum, choice) => sum + choice.qty, 0);
  return (
    <section className="return-against" data-testid="return-against-bill">
      <header className="return-against-head">
        <div>
          <span className="eyebrow">Against bill</span>
          <strong className="mono">{found.original.doc_number}</strong>
          <span className="muted-cell">
            {formatDateTime(found.billed_at)}
            {found.customer_name ? ` · ${found.customer_name}` : ""}
            {found.customer_mobile ? ` · ${found.customer_mobile}` : ""}
          </span>
        </div>
        <div className="return-against-value">
          <span>Paid on bill</span>
          <strong><Money paise={value} /></strong>
        </div>
        <div className="return-against-actions">
          <button type="button" className="btn" disabled={locked} onClick={onTakeEverything}>
            <Undo2 size={14} /> Take everything back
          </button>
          <button type="button" className="btn" disabled={locked} onClick={onChangeBill}>
            Change bill
          </button>
        </div>
      </header>

      {late && (
        <p className="warn-note" data-testid="return-late">
          This bill is past the {windowDays}-day return window. Save &amp; Print will ask a manager.
        </p>
      )}

      <div className="return-lines-wrap">
        <table className="return-lines" data-testid="return-lines">
          <thead>
            <tr>
              <th>Original piece</th>
              <th className="num">Bought</th>
              <th className="num">Already back</th>
              <th className="num">Coming back</th>
              <th>Reason</th>
              <th>Condition</th>
              <th className="num">Refund value</th>
            </tr>
          </thead>
          <tbody>
            {found.lines.map((line) => {
              const choice = picked[line.line_no];
              const qty = choice?.qty ?? 0;
              const left = returnableQty(line);
              return (
                <tr
                  key={line.line_no}
                  className={qty ? "is-marked" : "is-unmarked"}
                  data-testid={`return-line-${line.line_no}`}
                >
                  <td>
                    <strong>{describeOriginal(line)}</strong>
                    <small className="mono">{line.barcode}</small>
                  </td>
                  <td className="num">{line.qty}</td>
                  <td className="num">{line.returned_qty || "—"}</td>
                  <td className="num">
                    <input
                      className="input bill-cell"
                      inputMode="numeric"
                      aria-label={`Quantity of line ${line.line_no} coming back`}
                      disabled={locked || left === 0}
                      value={qty || ""}
                      placeholder="0"
                      onChange={(event) => onPick(line, { qty: wantedQty(event.target.value, left) })}
                    />
                    <small>of {left}</small>
                  </td>
                  <td>
                    <select
                      className="select bill-cell"
                      aria-label={`Reason for returning line ${line.line_no}`}
                      disabled={locked || !qty}
                      value={choice?.reason ?? ""}
                      onChange={(event) => onPick(line, { reason: event.target.value })}
                    >
                      <option value="">Not said</option>
                      {REASONS.map((reason) => <option key={reason} value={reason}>{reason}</option>)}
                    </select>
                  </td>
                  <td>
                    <select
                      className="select bill-cell"
                      aria-label={`Condition of returned line ${line.line_no}`}
                      disabled={locked || !qty}
                      value={choice?.condition ?? "good"}
                      onChange={(event) => onPick(line, {
                        condition: event.target.value === "damaged" ? "damaged" : "good",
                      })}
                    >
                      <option value="good">Good · back on shelf</option>
                      <option value="damaged">Damaged · quarantine</option>
                    </select>
                  </td>
                  <td className="num">
                    {qty ? <Money paise={refundFor(line, qty)} /> : <span>₹0</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <footer className="return-exchange-switch">
        <label>
          <input
            type="checkbox"
            checked={outgoing}
            disabled={locked}
            onChange={(event) => onOutgoing(event.target.checked)}
          />
          <span>
            <strong>Scan pieces going out</strong>
            <small>{outgoing ? "Scanner is adding the customer’s replacement pieces." : "Turn on to add the exchange sale."}</small>
          </span>
        </label>
        <span className="muted-cell">{marked} {marked === 1 ? "piece" : "pieces"} marked back</span>
      </footer>
    </section>
  );
}

function wantedQty(typed: string, maximum: number): number {
  const whole = Math.trunc(Number(typed));
  if (!Number.isFinite(whole) || whole <= 0) return 0;
  return Math.min(whole, maximum);
}

/** Three recent bills shown in the empty return grid for quick selection. */
export function RecentBills({
  recentBills,
  onPick,
}: {
  recentBills: ReturnBillMatch[];
  onPick: (match: ReturnBillMatch) => void;
}) {
  if (!recentBills.length) return null;
  return (
    <div className="bill-recent-panel" data-testid="bill-recent-panel">
      <div className="bill-recent-head">
        <p className="eyebrow">Recent bills</p>
        <span className="muted-cell">Pick one to start return</span>
      </div>
      <div className="bill-recent-rows">
        {recentBills.map((match) => (
          <button
            key={match.doc_number}
            type="button"
            className="bill-recent-row"
            data-testid={`bill-recent-${match.doc_number}`}
            onClick={() => onPick(match)}
          >
            <div className="bill-recent-row-left">
              <strong>{match.doc_number}</strong>
              <small>
                {match.customer_name || "Walk-in customer"}
                {match.customer_mobile ? ` · ${match.customer_mobile}` : ""}
              </small>
            </div>
            <div className="bill-recent-row-right">
              <strong><Money paise={match.net_paise} /></strong>
              <small>{formatDateTime(match.billed_at)}</small>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
