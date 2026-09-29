import { useEffect, useRef } from "react";
import type { KeyboardEvent } from "react";
import { AlertTriangle, Check, Printer } from "lucide-react";

import { formatINR } from "../../../lib/format";
import { code128Svg } from "../../../till/barcode";
import { changeFor } from "../../../till/cart";
import { tenderWords } from "../../../till/tender";
import type { QueuedBill } from "../../../till/types";

export function FinishOverlay({
  bill,
  cashReceivedPaise,
  printProblem,
  busy,
  onPrint,
  onNext,
}: {
  bill: QueuedBill;
  /** Null is the blank box: exactly the cash tender, so no change. */
  cashReceivedPaise: number | null;
  printProblem: string;
  busy: boolean;
  onPrint: () => void;
  onNext: () => void;
}) {
  const printButton = useRef<HTMLButtonElement>(null);
  const nextButton = useRef<HTMLButtonElement>(null);
  const paid = bill.tenders.reduce((sum, tender) => sum + tender.amount_paise, 0);
  const cashTaken = bill.tenders
    .filter((tender) => tender.mode === "cash")
    .reduce((sum, tender) => sum + tender.amount_paise, 0);
  const change = changeFor(cashReceivedPaise ?? 0, cashTaken);

  useEffect(() => {
    if (!busy) nextButton.current?.focus();
  }, [busy]);

  function keepFocusInside(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== "Tab" || !printButton.current || !nextButton.current) return;
    if (event.shiftKey && document.activeElement === printButton.current) {
      event.preventDefault();
      nextButton.current.focus();
    } else if (!event.shiftKey && document.activeElement === nextButton.current) {
      event.preventDefault();
      printButton.current.focus();
    }
  }

  return (
    <div className="bill-finish-backdrop" role="presentation">
      <section
        className="bill-finish"
        role="dialog"
        aria-modal="true"
        aria-labelledby="bill-finish-title"
        onKeyDown={keepFocusInside}
      >
        <header className="bill-finish-head">
          <span className="bill-finish-check" aria-hidden="true">
            <Check size={22} strokeWidth={3} />
          </span>
          <div>
            <p className="eyebrow">Done</p>
            <h2 id="bill-finish-title">{bill.exchange ? "Exchange saved" : "Bill saved"}</h2>
            {bill.tax_invoice_number && (
              <p className="mono" data-testid="bill-invoice-number">
                Invoice No. {bill.tax_invoice_number}
              </p>
            )}
            <p className="mono">{bill.doc_number}</p>
          </div>
        </header>

        {printProblem && (
          <p className="bill-alert bill-finish-print-problem" role="status">
            <AlertTriangle size={16} />
            <span>{printProblem}</span>
          </p>
        )}

        <div className="bill-finish-body">
          <div className="bill-finish-payment">
            <div className="bill-finish-paid">
              <span>Paid</span>
              <strong>{formatINR(paid)}</strong>
            </div>
            <div className="bill-finish-split">
              {bill.tenders.map((tender, index) => (
                <div key={`${tender.mode}-${index}`}>
                  <span>{tenderWords(tender)}</span>
                  <span>{formatINR(tender.amount_paise)}</span>
                </div>
              ))}
              {change > 0 && (
                <div className="bill-finish-change">
                  <span>Change</span>
                  <strong>{formatINR(change)}</strong>
                </div>
              )}
            </div>
          </div>

          <div className="bill-finish-barcode">
            <div dangerouslySetInnerHTML={{ __html: code128Svg(bill.doc_number) }} />
            <strong className="mono">{bill.doc_number}</strong>
            <span>Scan this bill to bring anything back</span>
          </div>
        </div>

        <footer className="bill-finish-actions">
          <button ref={printButton} type="button" className="btn" disabled={busy} onClick={onPrint}>
            <Printer size={16} /> Print again
          </button>
          <button
            ref={nextButton}
            type="button"
            className="btn primary"
            disabled={busy}
            onClick={onNext}
          >
            Next bill <kbd>Enter</kbd>
          </button>
        </footer>
      </section>
    </div>
  );
}
