// The delivery challan or tax invoice one shipment left with, to print
// (store operations ticket 36, ST-TRF-2).
//
// Which document it is was decided by the server at dispatch, from the two
// sites' GSTINs, and written once: this page only reads it back. Money shows
// only where the server sent it - a reader without the cost grant gets the
// lines without value or tax, and the page says so rather than printing zeros.
//
// Online only. Offline, or when the read is cut off, it says the document
// cannot be read now and reads it again as soon as the connection is back.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Printer, RotateCcw } from "lucide-react";

import { api, apiErrorCode, apiErrorMessage } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { formatDateTime, formatPaiseString } from "../lib/format";
import { Denied } from "../lib/goodsScreen";
import {
  documentLabel,
  documentReason,
  type TransferDocument,
  type TransferDocumentLine,
} from "../lib/goodsTransfers";
import "./Booking.css";

const TRANSFERS = "/goods-v1/outbound/transfers";

const OFFLINE =
  "You are offline, so this document cannot be read now. It will load again when the connection is back. Nothing about the shipment has changed.";

function Party({ label, party }: { label: string; party: TransferDocument["source"] }) {
  return (
    <div data-testid={`document-${label.toLowerCase()}`}>
      <p className="eyebrow">{label}</p>
      <p>
        <strong>{party.legal_name}</strong>
        <br />
        {party.code} · {party.name}
        {party.city ? `, ${party.city}` : ""}
        <br />
        GSTIN {party.gstin} (state code {party.state_code}, {party.state_name})
      </p>
    </div>
  );
}

/** Every transport detail the dispatch recorded, the e-way reference named. */
function transportWords(transport: Record<string, unknown>): string {
  const details = Object.entries(transport)
    .filter(([key, value]) => key !== "eway_reference" && typeof value === "string" && value)
    .map(([key, value]) => `${key === "reference" ? "Reference" : key.toUpperCase()} ${value}`);
  const eway =
    typeof transport.eway_reference === "string" && transport.eway_reference
      ? `E-way bill ${transport.eway_reference}`
      : "No e-way bill reference";
  return [...details, eway].join(" · ");
}

function money(line: TransferDocumentLine, key: keyof TransferDocumentLine): string {
  if (!line.values_shown) return "—";
  const value = line[key];
  return typeof value === "string" ? formatPaiseString(value) : "Not known";
}

export function TransferDocumentPage() {
  const { id = "", dispatchId = "" } = useParams();
  const [document, setDocument] = useState<TransferDocument | null>(null);
  const [denied, setDenied] = useState(false);
  const [none, setNone] = useState(false);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const request = useRef(0);

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const answer = await api.get<TransferDocument>(
        `${TRANSFERS}/${id}/dispatches/${dispatchId}/document`,
      );
      if (mine !== request.current) return;
      setLost(false);
      setError("");
      setDocument(answer.data);
    } catch (reason) {
      if (mine !== request.current) return;
      const status = (reason as { response?: { status?: number } })?.response?.status;
      if (isConnectionLost(reason)) setLost(true);
      else if (apiErrorCode(reason) === "NO_TRANSFER_DOCUMENT") setNone(true);
      else if (status === 403 || status === 404) setDenied(true);
      else setError(apiErrorMessage(reason));
    }
  }, [id, dispatchId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load();
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  const offline = !online || lost;
  const back = (
    <Link to={`/goods/transfers/${id}`} className="btn" data-testid="document-back">
      <ArrowLeft size={15} /> Transfer
    </Link>
  );

  if (denied) return <Denied what="transfer document" />;
  if (none) {
    return (
      <div className="page-pad">
        {back}
        <p className="lead" data-testid="document-none">
          This shipment left with no transfer document from the system: the sending site's
          transfer-documents switch was off when it was dispatched.
        </p>
      </div>
    );
  }

  return (
    <div className="page-pad pt-print">
      <div className="toolbar pt-print-hide">
        {back}
        <div className="spacer" />
        <button
          type="button"
          className="btn btn-cta"
          disabled={!document}
          onClick={() => window.print()}
          data-testid="document-print"
        >
          <Printer size={15} /> Print
        </button>
      </div>
      {offline && (
        <p className="warn-note pt-print-hide" data-testid="document-offline" role="status">
          {OFFLINE}{" "}
          {online && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void load()}
              data-testid="document-retry"
            >
              <RotateCcw size={14} /> Try again
            </button>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note pt-print-hide" data-testid="document-error">
          {error}
        </p>
      )}
      {!document ? (
        <p className="muted">{offline || error ? "" : "Loading…"}</p>
      ) : (
        <article data-testid="transfer-document" data-kind={document.kind}>
          <p className="eyebrow">
            {document.kind === "tax_invoice"
              ? "Tax invoice · stock transfer between registrations"
              : "Delivery challan · stock transfer, not a sale"}
          </p>
          <h1 className="h1" data-testid="document-title">
            {documentLabel(document.kind)} {document.number}
          </h1>
          <p className="lead" data-testid="document-reason">
            {documentReason(document)}
          </p>
          <dl className="kv">
            <dt>Number</dt>
            <dd data-testid="document-number">{document.number}</dd>
            <dt>Date</dt>
            <dd>{document.issued_on}</dd>
            <dt>Shipment</dt>
            <dd>
              {document.sequence_no}, left {formatDateTime(document.dispatched_at)}, recorded by{" "}
              {document.issued_by || "—"}
            </dd>
            <dt>Transport</dt>
            <dd data-testid="document-transport">{transportWords(document.transport)}</dd>
          </dl>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <Party label="From" party={document.source} />
            <Party label="To" party={document.destination} />
          </div>
          {!document.values_shown && (
            <p className="warn-note" data-testid="document-values-hidden">
              The values and tax on this document are shown only to a login allowed to see cost. Ask
              head office to print it with values.
            </p>
          )}
          <div className="table-wrap">
            <table className="data" data-testid="document-lines">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>HSN</th>
                  <th className="num">Qty</th>
                  <th className="num">Rate per piece</th>
                  <th className="num">Value</th>
                  {document.kind === "tax_invoice" && (
                    <>
                      <th className="num">GST %</th>
                      {document.tax_kind === "igst" ? (
                        <th className="num">IGST</th>
                      ) : (
                        <>
                          <th className="num">CGST</th>
                          <th className="num">SGST</th>
                        </>
                      )}
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {document.lines.map((line, index) => (
                  <tr key={index} data-testid="document-line">
                    <td>{line.description}</td>
                    <td>{line.hsn || "—"}</td>
                    <td className="num">{line.qty}</td>
                    <td className="num">{money(line, "unit_cost_paise")}</td>
                    <td className="num" data-testid="document-line-value">
                      {money(line, "taxable_paise")}
                    </td>
                    {document.kind === "tax_invoice" && (
                      <>
                        <td className="num">
                          {line.values_shown && line.rate ? `${line.rate}%` : "—"}
                        </td>
                        {document.tax_kind === "igst" ? (
                          <td className="num">{money(line, "igst_paise")}</td>
                        ) : (
                          <>
                            <td className="num">{money(line, "cgst_paise")}</td>
                            <td className="num">{money(line, "sgst_paise")}</td>
                          </>
                        )}
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <dl className="kv" data-testid="document-totals">
            <dt>Pieces</dt>
            <dd data-testid="document-pieces">{document.pieces}</dd>
            {document.values_shown && (
              <>
                <dt>Value</dt>
                <dd>{formatPaiseString(document.taxable_paise)}</dd>
                {document.kind === "tax_invoice" && (
                  <>
                    <dt>Tax</dt>
                    <dd>{formatPaiseString(document.tax_paise)}</dd>
                    <dt>Total</dt>
                    <dd data-testid="document-total">{formatPaiseString(document.total_paise)}</dd>
                  </>
                )}
              </>
            )}
          </dl>
        </article>
      )}
    </div>
  );
}
