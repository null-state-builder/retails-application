// The paper for a partner-store payment, once Accounts has recorded it
// (`PartnerDues.tsx`'s "Record Payment" / history table). Built the same way
// the till's customer receipt is (`till/receipt.ts`): a whole HTML document
// rather than a React tree, because what prints is not the screen - it goes
// into an isolated frame carrying none of the app's own styles
// (`till/print.ts`'s `browserPrintAdapter`), and a reprint from history days
// later must read exactly the same as it did the day it was recorded.
//
// A4, not the till's 80mm roll: this is Chetna's copy for a physical file, not
// a customer's pocket. Letterhead-styled per Accounts' own request - the
// survey behind #Settlement-Receipts (`KDPS Daily Work Survey - Accounts
// Team.pdf`) is one long complaint about not being able to trust a figure
// without chasing it down; a signed-looking slip that says what was received,
// from whom and by whom is the paper trail that removes the chasing.

export interface SettlementReceiptData {
  doc_number: string;
  created_at: string;
  store_code: string;
  store_name: string;
  amount_paise: number;
  mode: string;
  reference: string;
  description: string;
  posted_by_name: string;
}

/** Everything here is either a store's own name/code or free text Accounts
 *  typed into the reference/description fields - unescaped, that is markup
 *  injection into a document this code builds by concatenation. */
function esc(text: string): string {
  return text.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string,
  );
}

/** Always to the paise, on a document somebody may add up by eye. */
function money(paise: number): string {
  const grouped = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(paise / 100);
  return `₹${grouped}`;
}

function when(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

const MODE_WORDS: Record<string, string> = {
  cash: "Cash",
  bank: "Bank transfer",
  upi: "UPI",
  cheque: "Cheque",
  other: "Other",
};

export function settlementReceiptHtml(entry: SettlementReceiptData): string {
  const rows: [string, string][] = [
    ["Received from", `${esc(entry.store_code)} — ${esc(entry.store_name)}`],
    ["Mode", MODE_WORDS[entry.mode] ?? esc(entry.mode || "—")],
    ...(entry.reference ? ([["Reference", esc(entry.reference)]] as [string, string][]) : []),
    ...(entry.description ? ([["Note", esc(entry.description)]] as [string, string][]) : []),
    ...(entry.posted_by_name ? ([["Recorded by", esc(entry.posted_by_name)]] as [string, string][]) : []),
  ];

  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${esc(entry.doc_number)}</title>
<style>
  @page { size: A4; margin: 20mm; }
  body { box-sizing: border-box; margin: 0 auto; max-width: 140mm; font: 13px/1.6 "Helvetica Neue", Arial, sans-serif; color: #1a1a1a; }
  .letterhead { text-align: center; }
  .letterhead .co { font-size: 20px; font-weight: 700; letter-spacing: 0.06em; }
  .letterhead .sub { font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: #666; margin-top: 4px; }
  hr { border: 0; border-top: 2px solid #1a1a1a; margin: 12px 0 18px; }
  hr.thin { border-top: 1px solid #ccc; margin: 18px 0; }
  h1 { font-size: 15px; text-align: center; text-transform: uppercase; letter-spacing: 0.08em; margin: 0 0 18px; }
  .meta { display: flex; justify-content: space-between; font-size: 12px; color: #555; margin-bottom: 18px; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 7px 0; vertical-align: top; border-bottom: 1px solid #eee; }
  td.k { width: 38%; color: #666; }
  .amount { text-align: center; margin: 22px 0; }
  .amount .figure { font-size: 30px; font-weight: 700; }
  .amount .label { font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: #666; }
  .sign { display: flex; justify-content: space-between; margin-top: 56px; font-size: 12px; }
  .sign .line { width: 55mm; border-top: 1px solid #1a1a1a; padding-top: 4px; text-align: center; color: #555; }
  footer { text-align: center; font-size: 10px; color: #999; margin-top: 28px; }
</style></head>
<body>
  <div class="letterhead">
    <div class="co">KDPS LIFESTYLE PVT LTD</div>
    <div class="sub">Settlement Receipt</div>
  </div>
  <hr>
  <div class="meta">
    <span>Voucher ${esc(entry.doc_number)}</span>
    <span>${esc(when(entry.created_at))}</span>
  </div>
  <div class="amount">
    <div class="figure">${money(entry.amount_paise)}</div>
    <div class="label">Received</div>
  </div>
  <table><tbody>
    ${rows.map(([k, v]) => `<tr><td class="k">${k}</td><td>${v}</td></tr>`).join("")}
  </tbody></table>
  <div class="sign">
    <div class="line">Received by</div>
    <div class="line">For KDPS Lifestyle Pvt Ltd</div>
  </div>
  <footer>Computer-generated settlement voucher — offsets the partner's dues in the Partner Dues report.</footer>
</body></html>`;
}
