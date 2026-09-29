// The customer's copy (#181, D10 §4, requirement G2).
//
// A whole HTML document rather than a React tree, because what prints is not the
// screen: it goes into an isolated frame that carries none of the app's styles,
// and the hardware spike (#190) may later hand the same string to an ESC/POS
// agent instead of to a browser. Building it as text keeps the one thing the
// customer walks out with independent of how the till happens to render today.
//
// Sized for a 80mm thermal roll, and readable on A4 if that is all a store has.
//
// It carries no cost and no margin (H2), and it says out loud when a bill was
// written with no line to head office - the origin tag is evidence for the daily
// check, and a store person seeing it on the paper knows why head office may not
// have this bill yet.

import { changeFor } from "./cart";
import { code128Svg } from "./barcode";
import { tenderWords } from "./tender";
import { splitTax, taxKindFor } from "./gstin";
import { taxLabel } from "./tax";
import type { B2bTaxKind } from "./gstin";
import { describePiece } from "./lookup";
import type {
  BillLine,
  BillTender,
  QueuedBill,
  TillCustomer,
  TillStoreIdentity,
} from "./types";

export interface ReceiptOptions {
  /** Cash the customer physically handed over, so the paper can show the change.
   *  Not what the bill *took* in cash - that is on the bill's own tender rows.
   *
   *  Null or absent is the blank box, which means the customer handed over
   *  exactly the cash tender: no cash-received line and no change. */
  cashReceivedPaise?: number | null;
  storeName?: string;
  /** How a line reads to a customer - "MUFTI Shirt · M · Navy".
   *
   *  Supplied by the screen rather than read off the line, because the bill's
   *  lines carry no description: the wire payload is the contract's, and the
   *  brand, size and colour of a piece are the *server's* to write from the
   *  cohort. The counter has them in the cart it just billed, so it lends them
   *  to the paper. A reprint keeps the finished string rather than re-deriving
   *  one (A7: reprint only, never re-render). */
  describe?: (line: BillLine) => string;
  /** The e-invoice reference, once head office has raised one (#187).
   *
   *  Only ever present on a *reprint*: a counter's copy is printed the moment
   *  the sale closes, before head office has raised one. With none, the paper
   *  prints no IRN line at all - never a promise that one will follow (store
   *  operations PRD ST-CMP-4, ticket 05). A reprint pulled off the posted
   *  document prints the IRN once it exists. */
  irn?: string;
  /** Ticket 13: the credit note an exchange issued, once head office has
   *  numbered it - only on a reprint. The counter's own copy prints before the
   *  bill reaches head office, and says the number follows. */
  creditNote?: { number: string; original: string | null } | null;
}

/**
 * Money for a printed line, always to the paise.
 *
 * Deliberately not `formatINR`, which drops the decimals on a whole rupee: that
 * is right on a screen and wrong in a column of figures somebody adds up by eye.
 */
function money(paise: number): string {
  const grouped = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(paise / 100);
  return `₹${grouped}`;
}

/** Everything a customer typed goes through here. A name is free text on a
 *  document we build by concatenation, and that is the whole recipe for markup
 *  injection if it is not escaped once, in one place. */
function esc(text: string): string {
  return text.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string,
  );
}

function when(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString("en-IN");
}

/** The bill, as paper. */
export function receiptHtml(
  bill: QueuedBill,
  store: TillStoreIdentity,
  options: ReceiptOptions = {},
): string {
  // The same function the screen quoted the customer, not a second copy of the
  // sum: the printed change and the change on the display have to agree. Change
  // is against the *cash* the bill took, never the bill - a ₹1,000 note handed
  // over on a half-carded bill is change on the cash half only.
  const cashTaken = bill.tenders
    .filter((tender) => tender.mode === "cash")
    .reduce((n, tender) => n + tender.amount_paise, 0);
  const change = changeFor(options.cashReceivedPaise ?? 0, cashTaken);
  const pieces = bill.lines.reduce((n, line) => n + line.qty, 0);
  const customer: TillCustomer = bill.customer ?? { name: "", mobile: "", gstin: "" };

  const describe = options.describe ?? ((line: BillLine) => line.manual_desc || line.barcode);
  const rows = bill.lines
    .map(
      (line) => `<tr>
        <td>${esc(describe(line))}<br>
          <span class="dim">${esc(line.barcode)}${line.season ? ` · ${esc(line.season)}` : ""}</span></td>
        <td class="n">${line.qty}</td>
        <td class="n">${money(line.mrp_paise)}</td>
        <td class="n">${money(line.net_paise)}</td>
      </tr>`,
    )
    .join("");

  // What kind of bill this is (#187). Read off the bill, because that is what
  // the counter derived at Save & Print and the customer's two copies must not
  // disagree; derived only as a fallback, for a bill queued before the field
  // existed. A GSTIN with no split beside it is a bill nobody can claim credit
  // on, so this is not a field to leave blank quietly.
  const buyerGstin = customer.gstin ?? "";
  const taxKind: B2bTaxKind = bill.b2b_tax_kind ?? taxKindFor(buyerGstin, store.state_code);
  // "Tax included ₹-655.78" is what a return-only bill printed before browser QA
  // of #184 read one: arithmetically right, and not a sentence anybody would put
  // on a customer's copy. A bill that gives back more than it sells gives the tax
  // back too, so it says so, and the figure is the amount rather than a minus.
  const givesTaxBack = bill.totals.gst_paise < 0;
  // The two words come from `tax.ts` rather than from here, because the footer
  // and the breakup panel now say the same thing about the same number (#247)
  // and the paper is the one that must not be argued with.
  const label = taxLabel(bill.totals.gst_paise);
  const taxPaise = Math.abs(bill.totals.gst_paise);
  const taxRows =
    taxKind === "none"
      ? [[label, money(taxPaise)]]
      : splitTax(taxPaise, taxKind).map(
          (part) =>
            [`${part.label} ${givesTaxBack ? "given back" : "included"}`, money(part.paise)] as [
              string,
              string,
            ],
        );

  // What the customer handed back, and what it was worth to them (#184, D2).
  // On the paper because it is the half of the sum they cannot otherwise check:
  // the pieces going out are priced off tags they can read, and the pieces coming
  // back are priced at what *they paid* on a bill from weeks ago.
  const back = bill.exchange?.lines ?? [];
  const givenBack = back.reduce((n, leg) => n + leg.refund_paise, 0);
  const returnedRows = back
    .map(
      (leg) => `<tr>
        <td>${esc(leg.barcode)}<br>
          <span class="dim">given back${leg.reason ? ` \u00b7 ${esc(leg.reason)}` : ""}</span></td>
        <td class="n">${leg.qty}</td>
        <td class="n">&minus;${money(leg.refund_paise)}</td>
      </tr>`,
    )
    .join("");
  // Ticket 13: where the return tax rules are on, an exchange is a credit note
  // for the pieces coming back and a new tax invoice for the pieces going out,
  // printed together on this one slip (§6 ST-CMP-2; baseline, CA to confirm).
  if (bill.return_tax && back.length) {
    return withCreditNote(bill, store, options, { rows, pieces, customer, buyerGstin, taxKind, change, cashTaken });
  }
  const returnedBlock = back.length
    ? `<hr><p class="dim">Given back against bill ${esc(String(bill.exchange?.original.till_seq ?? ""))}</p>
  <table><thead><tr><th>Item</th><th class="n">Qty</th><th class="n">Back</th></tr></thead>
  <tbody>${returnedRows}</tbody></table>`
    : "";

  const totals = [
    ["Pieces", String(pieces)],
    ["Gross", money(bill.totals.gross_paise)],
    ...(bill.totals.discount_paise ? [["You saved", money(bill.totals.discount_paise)]] : []),
    ...(givenBack ? [["Given back", `\u2212${money(givenBack)}`]] : []),
    ...(bill.totals.round_paise ? [["Rounding", money(bill.totals.round_paise)]] : []),
    ...taxRows,
  ]
    .map(([label, value]) => `<div class="row"><span>${label}</span><span>${value}</span></div>`)
    .join("");

  // A bill whose returns outweigh its sales pays the customer, and it pays them
  // in a credit note (grill Q7). The note's *number* is head office's to
  // allocate, so a counter printing offline cannot know it - and saying "to
  // follow" is the truth rather than a blank. The store writes it on this slip
  // when the bill syncs, exactly as it does with an IRN.
  const owed = Math.max(-bill.totals.net_paise, 0);
  const bankPaid = bill.tenders
    .filter((tender) => tender.mode === "bank_offer")
    .reduce((n, tender) => n + tender.amount_paise, 0);
  const dueRow = owed
    ? `<div class="row due"><span>Credit note</span><span>${money(owed)}</span></div>
  <p class="dim">No cash is paid out on a return. This is a credit note for use at this shop; its number follows when the bill reaches head office.</p>`
    : bankPaid
      ? // Ticket 11: a bank instant discount is a payment the bank makes, not a
        // price cut, so the invoice total stands and the customer is asked for
        // the rest. The bank offer itself is listed with the other payments.
        `<div class="row"><span>Invoice total</span><span>${money(bill.totals.net_paise)}</span></div>
  <div class="row"><span>Less bank offer</span><span>\u2212${money(bankPaid)}</span></div>
  <div class="row due"><span>To pay</span><span>${money(bill.totals.net_paise - bankPaid)}</span></div>`
      : `<div class="row due"><span>To pay</span><span>${money(bill.totals.net_paise)}</span></div>`;

  // The buyer's own registration, on the paper, above the lines. Without it the
  // document is not a tax invoice and the customer cannot claim the credit the
  // GSTIN was given for - which is the entire reason they handed it over.
  const buyerBlock = buyerGstin
    ? `<p class="dim buyer">Buyer${customer.name ? ` ${esc(customer.name)}` : ""}<br>
        GSTIN ${esc(buyerGstin)}${options.irn ? `<br>
        IRN ${esc(options.irn)}` : ""}</p>`
    : "";

  // How it was paid, mode by mode. A split bill's customer copy has to say which
  // card was charged what, because that is the line they will query.
  const tendered = bill.tenders
    .map(
      (tender) =>
        `<div class="row"><span>${esc(tenderWords(tender))}</span><span>${money(tender.amount_paise)}</span></div>`,
    )
    .join("");
  const received =
    options.cashReceivedPaise && options.cashReceivedPaise !== cashTaken
      ? `<div class="row"><span>Cash received</span><span>${money(options.cashReceivedPaise)}</span></div>`
      : "";
  const changeRow = change
    ? `<div class="row"><span>Change</span><span>${money(change)}</span></div>`
    : "";

  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${esc(bill.doc_number)}</title>
<style>${STYLE}</style></head>
<body>
  <h1>${esc(options.storeName || store.code)}</h1>
  <p class="mid dim">GSTIN ${esc(store.gstin)}<br>Tax invoice</p>
  <hr>
  ${invoiceHeading(bill)}
  <p class="dim">Bill ${esc(bill.doc_number)}<br>${esc(when(bill.billed_at))}${
    customer.name || customer.mobile
      ? `<br>${esc([customer.name, customer.mobile].filter(Boolean).join(" · "))}`
      : ""
  }</p>
  ${buyerBlock}
  <table><thead><tr><th>Item</th><th class="n">Qty</th><th class="n">Rate</th><th class="n">Amount</th></tr></thead>
  <tbody>${rows}</tbody></table>
  ${returnedBlock}
  <hr>
  ${totals}
  ${dueRow}
  ${tendered}${received}${changeRow}
  ${footer(bill)}
</body></html>`;
}

const STYLE = `
  @page { size: 80mm auto; margin: 0; }
  body { box-sizing: border-box; width: 80mm; margin: 0; padding: 4mm; font: 12px/1.45 "Helvetica Neue", Arial, sans-serif; }
  h1 { font-size: 15px; text-align: center; }
  .dim { color: #666; font-size: 10px; }
  .mid { text-align: center; }
  hr { border: 0; border-top: 1px dashed #999; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 2px 0; vertical-align: top; }
  th { font-size: 10px; text-transform: uppercase; border-bottom: 1px solid #000; }
  .n { text-align: right; white-space: nowrap; }
  .row { display: flex; justify-content: space-between; padding: 1px 0; }
  .due { font-weight: 700; font-size: 15px; border-top: 1px solid #000; padding-top: 4px; }
  .buyer { border: 1px solid #999; padding: 3px 4px; }
  .receipt-barcode { display: block; width: 100%; height: 16mm; margin: 8px 0 3px; }
  .receipt-barcode-number { color: #000; font: 700 10px/1.2 monospace; overflow-wrap: anywhere; }
  footer { padding-top: 6px; }
  .doc { font-weight: 700; text-transform: uppercase; font-size: 11px; margin: 6px 0 2px; }
`;

function invoiceHeading(bill: QueuedBill): string {
  return bill.tax_invoice_number
    ? `<p class="mid"><strong>Invoice No. ${esc(bill.tax_invoice_number)}</strong></p>`
    : "";
}

function footer(bill: QueuedBill): string {
  return `<footer class="mid dim">
    ${bill.origin === "offline" ? "Billed offline &middot; will reach head office when the line is back<br>" : ""}
    Exchange within the store's policy, with this bill.
    ${code128Svg(bill.doc_number, { className: "receipt-barcode" })}
    <div class="receipt-barcode-number">${esc(bill.doc_number)}</div>
    Scan it to bring anything back.<br>Thank you.
  </footer>`;
}

function row(label: string, value: string, className = "row"): string {
  return `<div class="${className}"><span>${label}</span><span>${value}</span></div>`;
}

/**
 * The exchange slip under the return tax rules (ticket 13): the new tax invoice
 * and the credit note, one after the other on one piece of paper.
 *
 * The invoice carries the pieces going out, taxed today, and its own tax. The
 * credit note carries the pieces coming back at their own bill's values and the
 * tax it reverses - or says that it reduces none, past the credit-note deadline.
 * A bank offer's part of a piece coming back is shown as paid back, never as
 * credited (B60). What the customer pays is the tenders the bill took.
 */
function withCreditNote(
  bill: QueuedBill,
  store: TillStoreIdentity,
  options: ReceiptOptions,
  parts: {
    rows: string;
    pieces: number;
    customer: TillCustomer;
    buyerGstin: string;
    taxKind: B2bTaxKind;
    change: number;
    cashTaken: number;
  },
): string {
  const back = bill.exchange?.lines ?? [];
  const soldValue = bill.lines.reduce((n, line) => n + line.net_paise, 0);
  const soldTax = bill.lines.reduce((n, line) => n + line.gst_paise, 0);
  const value = back.reduce((n, leg) => n + leg.refund_paise, 0);
  const reversed = back.reduce((n, leg) => n + leg.gst_paise, 0);
  const bank = back.reduce((n, leg) => n + (leg.bank_offer_paise ?? 0), 0);
  const late = reversed === 0 && back.some((leg) => Number(leg.gst_rate) > 0);
  const taxRows =
    parts.taxKind === "none"
      ? [row("GST included", money(soldTax))]
      : splitTax(soldTax, parts.taxKind).map((part) => row(`${part.label} included`, money(part.paise)));
  const original = options.creditNote?.original
    ? esc(options.creditNote.original)
    : esc(`${bill.exchange?.original.fy ?? ""} bill ${bill.exchange?.original.till_seq ?? ""}`);
  const number = options.creditNote?.number
    ? `<p class="mid"><strong>Credit Note No. ${esc(options.creditNote.number)}</strong></p>`
    : `<p class="mid dim">Credit note number: given when this bill reaches head office.</p>`;
  const backRows = back
    .map(
      (leg) => `<tr>
        <td>${esc(leg.barcode)}<br>
          <span class="dim">${leg.reason ? esc(leg.reason) : "given back"}${leg.gst_paise ? ` · GST ${esc(leg.gst_rate)}%` : ""}</span></td>
        <td class="n">${leg.qty}</td>
        <td class="n">${money(leg.refund_paise)}</td>
      </tr>`,
    )
    .join("");
  const bankPaid = bill.tenders
    .filter((tender) => tender.mode === "bank_offer")
    .reduce((n, tender) => n + tender.amount_paise, 0);
  const toPay = bill.tenders
    .filter((tender) => tender.mode !== "bank_offer")
    .reduce((n, tender) => n + tender.amount_paise, 0);
  const tendered = bill.tenders
    .map((tender) => row(esc(tenderWords(tender)), money(tender.amount_paise)))
    .join("");
  const received =
    options.cashReceivedPaise && options.cashReceivedPaise !== parts.cashTaken
      ? row("Cash received", money(options.cashReceivedPaise))
      : "";
  const buyerBlock = parts.buyerGstin
    ? `<p class="dim buyer">Buyer${parts.customer.name ? ` ${esc(parts.customer.name)}` : ""}<br>
        GSTIN ${esc(parts.buyerGstin)}${options.irn ? `<br>IRN ${esc(options.irn)}` : ""}</p>`
    : "";
  const who =
    parts.customer.name || parts.customer.mobile
      ? `<br>${esc([parts.customer.name, parts.customer.mobile].filter(Boolean).join(" · "))}`
      : "";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${esc(bill.doc_number)}</title>
<style>${STYLE}</style></head>
<body>
  <h1>${esc(options.storeName || store.code)}</h1>
  <p class="mid dim">GSTIN ${esc(store.gstin)}<br>Tax invoice and credit note</p>
  <hr>
  <p class="doc">Tax invoice</p>
  ${invoiceHeading(bill)}
  <p class="dim">Bill ${esc(bill.doc_number)}<br>${esc(when(bill.billed_at))}${who}</p>
  ${buyerBlock}
  <table><thead><tr><th>Item</th><th class="n">Qty</th><th class="n">Rate</th><th class="n">Amount</th></tr></thead>
  <tbody>${parts.rows}</tbody></table>
  ${row("Pieces", String(parts.pieces))}
  ${bill.totals.discount_paise ? row("You saved", money(bill.totals.discount_paise)) : ""}
  ${row("Invoice value", money(soldValue))}
  ${taxRows.join("")}
  <hr>
  <p class="doc">Credit note</p>
  ${number}
  <p class="dim">Against bill ${original}. Each piece at what was paid for it, with the tax that bill charged.</p>
  <table><thead><tr><th>Item</th><th class="n">Qty</th><th class="n">Value</th></tr></thead>
  <tbody>${backRows}</tbody></table>
  ${row("Credit note value", money(value))}
  ${
    late
      ? `<p class="dim">No tax reduction: this piece came back after the credit-note deadline.</p>`
      : row("GST reversed", money(reversed))
  }
  ${bank ? row("Bank offer part, paid back", money(bank)) : ""}
  ${row("Credited to you", `−${money(value - bank)}`)}
  <hr>
  ${bill.totals.round_paise ? row("Rounding", money(bill.totals.round_paise)) : ""}
  ${bankPaid ? row("Less bank offer", `−${money(bankPaid)}`) : ""}
  ${row("To pay", money(toPay), "row due")}
  ${tendered}${received}${parts.change ? row("Change", money(parts.change)) : ""}
  ${footer(bill)}
</body></html>`;
}

/** One line of a posted bill, as the read serializer sends it.
 *
 *  The money fields are all here, and none of them is optional: a reprint that
 *  filled a column it had not been given would print a nought against a garment
 *  that was discounted or taxed, and the customer's two copies would disagree. */
export interface PostedLine {
  line_no: number;
  direction: string;
  barcode: string;
  season: string;
  brand: string;
  item: string;
  design: string;
  size: string;
  color: string;
  manual_desc: string;
  salesman_code: string;
  salesman_name: string;
  qty: number;
  mrp_paise: number;
  disc_paise: number;
  net_paise: number;
  gst_rate: string;
  gst_paise: number;
  /** Why it came back, and where it went - on a return leg only (#184). */
  return_reason?: string;
  condition?: string;
  /** Ticket 13 (B60): a return leg's bank offer part. */
  bank_offer_paise?: number;
}

/** A bill as the *server* has it - `GET /api/sell/sales/{doc_number}`. */
export interface PostedBill {
  doc_number: string;
  /** The number in the new invoice series (ticket 04), or null before it. */
  tax_invoice_number?: string | null;
  billed_at: string;
  origin: string;
  store_code: string;
  store_name: string;
  store_gstin: string;
  customer_name: string;
  customer_mobile: string;
  /** The buyer's registration and the split the bill was raised under (#187).
   *  Both come off the posted document rather than being re-derived: the shop's
   *  registration can change hands and a state code cannot be looked up for a
   *  bill from two years ago, but what this bill charged is a fact it carries. */
  buyer_gstin: string;
  b2b_tax_kind: B2bTaxKind;
  /** Blank until head office has raised one. Blank prints no IRN line, exactly
   *  as the original did (ST-CMP-4: nothing promises an IRN to follow). */
  irn: string;
  /** The bill this one gave pieces back against, when it carries an exchange
   *  (#184). Null on an ordinary sale. */
  exchange_of: { doc_number: string; fy: string; till_seq: number } | null;
  lines: PostedLine[];
  tenders: BillTender[];
  gross_paise: number;
  discount_paise: number;
  net_paise: number;
  gst_paise: number;
  round_paise: number;
  /** What the customer handed over in notes, as the counter recorded it. Null
   *  on a bill that took no cash, and on every bill printed before the field
   *  existed - both of which print no cash-received line, exactly as they did. */
  cash_received_paise?: number | null;
  /** Ticket 13: taken by the return tax rules, and the credit note it issued. */
  return_tax?: boolean;
  credit_note?: { number: string; original: string | null; status?: string } | null;
}

/**
 * A bill found by customer search, as paper (#185, E2).
 *
 * The same template as the till's own reprint, deliberately: a customer holding
 * two copies of one bill should not be able to tell which screen printed them.
 * What differs is where the facts come from - the counter that billed it may be
 * another machine, or last month's - so this reads the posted document and
 * nothing else.
 *
 * It re-renders rather than replaying a stored string, which is the one place the
 * note above ("a reprint keeps the finished string") has to bend: there is no
 * stored string for a bill this device never billed. It is safe for the reason
 * the note exists - the source is the posted document itself, so the paper says
 * what the books say, and A7 is untouched because nothing here can write.
 */
export function postedReceiptHtml(bill: PostedBill): string {
  // **A returned leg is not a sold line, and printing it as one is not a
  // cosmetic slip** (#184). The posted bill keeps both kinds in one `lines`
  // list with `direction` telling them apart; render them together and a piece
  // the customer *handed back* appears on the paper as one they bought, the
  // column of amounts no longer comes to the total under it, and a customer
  // reading their own copy is told they were charged for a refund. So the legs
  // are lifted out into the same exchange block the counter's own copy prints.
  const sold = bill.lines.filter((line) => line.direction !== "return");
  const back = bill.lines.filter((line) => line.direction === "return");
  return receiptHtml(
    {
      idempotency_uuid: "",
      store: bill.store_code,
      fy: "",
      till_seq: 0,
      attempts: 0,
      doc_number: bill.doc_number,
      ...(bill.tax_invoice_number ? { tax_invoice_number: bill.tax_invoice_number } : {}),
      billed_at: bill.billed_at,
      // Anything but "offline" simply drops the offline footnote; a reprint of a
      // bill written offline still says so, because that is a fact about the bill.
      origin: bill.origin === "offline" ? "offline" : "online",
      customer: {
        name: bill.customer_name,
        mobile: bill.customer_mobile,
        gstin: bill.buyer_gstin,
      },
      b2b_tax_kind: bill.b2b_tax_kind,
      lines: sold.map((line) => ({
        line_no: line.line_no,
        direction: "sale" as const,
        barcode: line.barcode,
        season: line.season,
        qty: line.qty,
        mrp_paise: line.mrp_paise,
        disc_paise: line.disc_paise,
        net_paise: line.net_paise,
        gst_rate: line.gst_rate,
        gst_paise: line.gst_paise,
        manual_desc: line.manual_desc,
      })),
      ...(back.length
        ? {
            exchange: {
              // The original the bill gave back against. Nought where the books
              // do not hold it - a paper-era return, which the accept pipeline
              // takes and flags (`return_orig_missing`) - and the block then
              // says "given back" without naming a bill, which is the truth.
              original: {
                fy: bill.exchange_of?.fy ?? "",
                till_seq: bill.exchange_of?.till_seq ?? 0,
              },
              lines: back.map((line) => ({
                line_no: line.line_no,
                barcode: line.barcode,
                season: line.season,
                qty: line.qty,
                refund_paise: line.net_paise,
                gst_rate: line.gst_rate,
                gst_paise: line.gst_paise,
                reason: line.return_reason ?? "",
                condition: line.condition === "damaged" ? ("damaged" as const) : ("good" as const),
                original_line: line.line_no,
                ...(line.bank_offer_paise ? { bank_offer_paise: line.bank_offer_paise } : {}),
              })),
            },
          }
        : {}),
      tenders: bill.tenders,
      ...(bill.return_tax ? { return_tax: true } : {}),
      totals: {
        gross_paise: bill.gross_paise,
        discount_paise: bill.discount_paise,
        net_paise: bill.net_paise,
        gst_paise: bill.gst_paise,
        round_paise: bill.round_paise,
      },
    },
    { code: bill.store_code, gstin: bill.store_gstin, state_code: "" },
    {
      storeName: bill.store_name,
      // What the counter recorded the customer handing over, off the posted
      // bill itself. A reprint that re-derived it from the cash tender would
      // print a receipt with no change line on a sale that gave change.
      cashReceivedPaise: bill.cash_received_paise ?? null,
      irn: bill.irn,
      creditNote: bill.credit_note ?? null,
      // The server writes the brand, item and size onto every line at billing
      // (Rule 3), so unlike the till's own receipt this one is not lent a
      // description - it has the snapshot the bill was printed from.
      describe: (line) => {
        const posted = bill.lines.find((row) => row.line_no === line.line_no);
        if (!posted) return line.barcode;
        return posted.manual_desc || describePiece(posted) || posted.barcode;
      },
    },
  );
}
