import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { commandIdFor, type Pending } from "../lib/debitNotes";
import { Money } from "../lib/format";
import {
  BANDS,
  MODE_LABEL,
  bandLabel,
  brandsOf,
  emptyInvoice,
  emptyPayment,
  invoiceBody,
  invoiceVendors,
  outcomeUnknown,
  payableInvoices,
  paymentBody,
  spreadOldestFirst,
  type InvoiceDraft,
  type PayableOptions,
  type PayablesSummary,
  type PaymentDraft,
  type Position,
} from "../lib/payables";
import { rupeesToPaise } from "../lib/debitNotes";

const PAGE_API = "/goods-v1/finledger/payables";
const OFFLINE =
  "Payables need a connection. Nothing can be recorded or cancelled until it is back; what you typed stays here.";

type Cancelling = { kind: "invoices" | "payments"; id: number; revision: number; reason: string };

/** One press: where it goes, what it is, and what to say and do after. */
interface Press {
  url: string;
  attempt: { noteId: number; revision: number; step: string };
  body: Record<string, unknown>;
  said: string;
  after: () => void;
}

function Amount({ paise }: { paise: string }) {
  return Number(paise) === 0 ? <span className="muted-cell">-</span> : <Money paise={Number(paise)} />;
}

function PositionCells({ row }: { row: Position }) {
  return (
    <>
      <td className="num">
        <Money paise={Number(row.owed_paise)} />
      </td>
      <td className="num">
        <Amount paise={row.paid_paise} />
      </td>
      <td className="num">
        <Money paise={Number(row.outstanding_paise)} />
      </td>
      <td className="num" data-testid="pay-due">
        <Amount paise={row.due_paise} />
      </td>
      {BANDS.map((band) => (
        <td className="num" key={band.key}>
          <Amount paise={row.bands[band.key]} />
        </td>
      ))}
      <td className="num">{row.oldest_days_late ?? "-"}</td>
    </>
  );
}

/** Money > Payables (store operations PRD ST-MNY-4; ticket 28).
 *
 *  Per outright brand or vendor: what is owed (the vendor invoices Accounts
 *  records, total with tax), what is paid (payments Accounts records against
 *  them), what is due (unpaid past its due date: the invoice date plus the
 *  payment days in the brand's terms) and how long past due. SOR and consignment
 *  are not here: what is owed to them arises on sale and comes later (P4). A
 *  brand with no recorded model is listed apart and never counted in the
 *  totals. Owner and Accounts read; Accounts records. The server decides who
 *  may do what and which stores; this page only offers what it allows. Online
 *  only: offline it says so and keeps what was typed. */
export function PayablesPage() {
  const [store, setStore] = useState("");
  const [group, setGroup] = useState<"brand" | "vendor">("brand");
  const [data, setData] = useState<PayablesSummary | null>(null);
  const [options, setOptions] = useState<PayableOptions | null>(null);
  const [invoice, setInvoice] = useState<InvoiceDraft>(() => emptyInvoice());
  const [payment, setPayment] = useState<PaymentDraft>(() => emptyPayment());
  const [cancelling, setCancelling] = useState<Cancelling | null>(null);
  /** The payment form's store, read on its own: the page's store filter may differ. */
  const [payStore, setPayStore] = useState<PayablesSummary | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = useRef<Pending | null>(null);
  /** A press whose answer was lost: "Send again" sends exactly it once more. */
  const [unsent, setUnsent] = useState<Press | null>(null);
  const request = useRef(0);

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const params: Record<string, string> = { group };
      if (store) params.store = store;
      const [summary, choices] = await Promise.all([
        api.get<PayablesSummary>(PAGE_API, { params }),
        api.get<PayableOptions>(`${PAGE_API}/options`),
      ]);
      if (mine !== request.current) return;
      setLost(false);
      setData(summary.data);
      setOptions(choices.data);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, [group, store]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadPayStore = useCallback(async (storeId: string) => {
    if (!storeId || !navigator.onLine) {
      setPayStore(null);
      return;
    }
    try {
      const answer = await api.get<PayablesSummary>(PAGE_API, { params: { store: storeId } });
      setPayStore(answer.data);
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void loadPayStore(payment.store);
    // Read again whenever the page reads again (after every save), so what is
    // left to pay is never stale.
  }, [payment.store, data, loadPayStore]);

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

  /** Send one command. A dropped connection keeps what was typed, and the press
   *  itself, so "Send again" replays it under the same command identity
   *  (`commandIdFor`) and the server records it once; any answer from the server
   *  ends that command. */
  async function send(press: Press) {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setBusy(true);
    setError("");
    setDone("");
    const full = { ...press.attempt, body: JSON.stringify(press.body) };
    const commandId = commandIdFor(pending.current, full, () => crypto.randomUUID());
    pending.current = { ...full, commandId };
    try {
      const revision = press.attempt.revision > 0 ? press.attempt.revision : undefined;
      await api.post(press.url, { ...goodsMeta(revision, commandId), ...press.body });
      pending.current = null;
      setUnsent(null);
      setDone(press.said);
      press.after();
      void load();
    } catch (reason) {
      if (outcomeUnknown(reason)) {
        if (isConnectionLost(reason)) setLost(true);
        else setError(apiErrorMessage(reason));
        setUnsent(press);
      } else {
        pending.current = null;
        setUnsent(null);
        setError(apiErrorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  const offline = !online || lost;
  const writable = !offline && !busy;
  const onStores = (options?.stores ?? []).filter((s) => s.switched_on);
  const checkedInvoice = invoiceBody(invoice);
  const open = payStore ? payableInvoices(payStore.invoices, payment.store, payment.vendor) : [];
  const openKey = open.map((inv) => `${inv.id}:${inv.outstanding_paise}`).join(",");
  const checkedPayment = paymentBody(payment, open);

  // An amount typed before the vendor's invoices arrived is spread over them
  // once they do, unless lines were typed by hand.
  useEffect(() => {
    const paise = rupeesToPaise(payment.rupees);
    if (!paise || !open.length || Object.keys(payment.lines).length) return;
    setPayment((current) => ({ ...current, lines: spreadOldestFirst(open, Number(paise)) }));
  }, [openKey]);
  const vendorsHere = options?.vendors ?? [];
  const newInvoiceVendors = invoiceVendors(options);
  const noun = group === "brand" ? "Brand" : "Vendor";

  return (
    <div className="page-pad">
      <PageHeader
        title="Payables"
        lead="What is owed to each outright brand or vendor, what is paid, what is due and how long past due."
      />
      <p className="muted-cell" data-testid="pay-later">
        {data?.later ??
          "Outright brands only. What is owed to SOR and consignment brands comes here later (P4)."}
      </p>
      {offline && (
        <p className="warn-note" data-testid="pay-offline" role="status">
          {OFFLINE}
          {online && (
            <>
              {" "}
              <button type="button" className="btn" data-testid="pay-retry" onClick={() => void load()}>
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {unsent && (
        <p className="warn-note" data-testid="pay-unsent" role="status">
          The answer to your last save was lost, so it may or may not be recorded. Send it again: it is
          recorded once either way.{" "}
          <button
            type="button"
            className="btn btn-primary"
            data-testid="pay-resend"
            disabled={!online || busy}
            onClick={() => void send(unsent)}
          >
            Send again
          </button>
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="pay-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="pay-done">
          {done}
        </p>
      )}

      <section className="card section-card">
        <div className="toolbar">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="pay-store"
              value={store}
              onChange={(e) => setStore(e.target.value)}
            >
              <option value="">Every store</option>
              {(data?.stores ?? []).map((s) => (
                <option key={s.id} value={String(s.id)}>
                  {s.code} · {s.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Show per</span>
            <select
              className="input"
              data-testid="pay-group"
              value={group}
              onChange={(e) => setGroup(e.target.value === "vendor" ? "vendor" : "brand")}
            >
              <option value="brand">Brand</option>
              <option value="vendor">Vendor</option>
            </select>
          </label>
          {data && <span className="muted-cell">Ageing as of {data.as_of}</span>}
        </div>
        {!data ? (
          <p className="muted-cell">{offline ? "" : "Loading…"}</p>
        ) : (
          <>
            {!data.switched_on && (
              <p className="warn-note" data-testid="pay-off">
                Brand payables are switched off at every store you work at. What is recorded stays here to
                read.
              </p>
            )}
            {data.rows.length === 0 ? (
              <p className="muted-cell" data-testid="pay-empty">
                Nothing is owed to an outright brand at these stores.
              </p>
            ) : (
              <div className="table-wrap">
                <table className="data" data-testid="pay-summary">
                  <thead>
                    <tr>
                      <th>{noun}</th>
                      <th className="num">Owed</th>
                      <th className="num">Paid</th>
                      <th className="num">Outstanding</th>
                      <th className="num">Due now</th>
                      {BANDS.map((band) => (
                        <th className="num" key={band.key}>
                          {band.label}
                        </th>
                      ))}
                      <th className="num">Most days late</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.map((row) => (
                      <tr key={row.key} data-testid={`pay-row-${row.key}`}>
                        <td>{row.name}</td>
                        <PositionCells row={row} />
                      </tr>
                    ))}
                    <tr data-testid="pay-total">
                      <th>Total</th>
                      <PositionCells row={data.totals} />
                    </tr>
                  </tbody>
                </table>
              </div>
            )}

            {data.unknown.length > 0 && (
              <div data-testid="pay-unknown">
                <h3 className="h3">Brands with no recorded model</h3>
                <p className="muted-cell">
                  These brands have no approved terms for the invoice's season and date, so they are not
                  counted as outright above and have no due date. Record their terms in Brands, Terms.
                </p>
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Brand</th>
                        <th className="num">Owed</th>
                        <th className="num">Paid</th>
                        <th className="num">Outstanding</th>
                        <th className="num">Open invoices</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.unknown.map((row) => (
                        <tr key={row.key} data-testid={`pay-unknown-${row.key}`}>
                          <td>{row.name}</td>
                          <td className="num">
                            <Money paise={Number(row.owed_paise)} />
                          </td>
                          <td className="num">
                            <Amount paise={row.paid_paise} />
                          </td>
                          <td className="num">
                            <Money paise={Number(row.outstanding_paise)} />
                          </td>
                          <td className="num">{row.open_invoices}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        )}
      </section>

      {data?.can_edit && onStores.length > 0 && options && (
        <section className="card section-card" data-testid="pay-invoice-form">
          <h2 className="h3">Record a vendor invoice</h2>
          <p className="muted-cell">
            From the vendor's tax invoice for an outright brand: the total including tax. The due date comes
            from the payment days in the brand's terms for that season.
          </p>
          <div className="toolbar">
            <label className="field">
              <span>Store</span>
              <select
                className="input"
                data-testid="pay-inv-store"
                disabled={!writable}
                value={invoice.store}
                onChange={(e) => setInvoice({ ...invoice, store: e.target.value })}
              >
                <option value="">Pick…</option>
                {onStores.map((s) => (
                  <option key={s.id} value={String(s.id)}>
                    {s.code} · {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Vendor</span>
              <select
                className="input"
                data-testid="pay-inv-vendor"
                disabled={!writable}
                value={invoice.vendor}
                onChange={(e) => setInvoice({ ...invoice, vendor: e.target.value, brand: "" })}
              >
                <option value="">Pick…</option>
                {newInvoiceVendors.map((v) => (
                  <option key={v.id} value={String(v.id)}>
                    {v.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Brand</span>
              <select
                className="input"
                data-testid="pay-inv-brand"
                disabled={!writable || !invoice.vendor}
                value={invoice.brand}
                onChange={(e) => setInvoice({ ...invoice, brand: e.target.value })}
              >
                <option value="">Pick…</option>
                {brandsOf(options, invoice.vendor).map((b) => (
                  <option key={b.id} value={String(b.id)}>
                    {b.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Season</span>
              <select
                className="input"
                data-testid="pay-inv-season"
                disabled={!writable}
                value={invoice.season}
                onChange={(e) => setInvoice({ ...invoice, season: e.target.value })}
              >
                <option value="">Pick…</option>
                {options.seasons.map((s) => (
                  <option key={s.id} value={String(s.id)}>
                    {s.code}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="toolbar">
            <label className="field">
              <span>Invoice number</span>
              <input
                className="input"
                maxLength={60}
                data-testid="pay-inv-number"
                disabled={!writable}
                value={invoice.number}
                onChange={(e) => setInvoice({ ...invoice, number: e.target.value })}
              />
            </label>
            <label className="field">
              <span>Invoice date</span>
              <input
                className="input"
                type="date"
                data-testid="pay-inv-date"
                disabled={!writable}
                value={invoice.date}
                onChange={(e) => setInvoice({ ...invoice, date: e.target.value })}
              />
            </label>
            <label className="field">
              <span>Total with tax (Rs)</span>
              <input
                className="input"
                inputMode="decimal"
                data-testid="pay-inv-amount"
                disabled={!writable}
                value={invoice.rupees}
                onChange={(e) => setInvoice({ ...invoice, rupees: e.target.value })}
              />
            </label>
            <label className="field">
              <span>Note (optional)</span>
              <input
                className="input"
                maxLength={240}
                data-testid="pay-inv-note"
                disabled={!writable}
                value={invoice.note}
                onChange={(e) => setInvoice({ ...invoice, note: e.target.value })}
              />
            </label>
          </div>
          {!checkedInvoice.ok && invoice.number && (
            <p className="warn-note" data-testid="pay-inv-problem">
              {checkedInvoice.problem}
            </p>
          )}
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="pay-inv-save"
              disabled={!writable || !checkedInvoice.ok}
              onClick={() => {
                if (!checkedInvoice.ok) return;
                void send({
                  url: `${PAGE_API}/invoices`,
                  attempt: { noteId: 0, revision: 0, step: "invoice" },
                  body: checkedInvoice.body,
                  said: `Invoice ${invoice.number.trim()} recorded.`,
                  after: () => setInvoice(emptyInvoice(invoice.store)),
                });
              }}
            >
              Record the invoice
            </button>
          </div>
        </section>
      )}

      {data?.can_edit && onStores.length > 0 && options && (
        <section className="card section-card" data-testid="pay-payment-form">
          <h2 className="h3">Record a payment</h2>
          <p className="muted-cell">
            A payment already made to one vendor for one store. Spread it over that vendor's open invoices
            there, of any brand. Nothing is sent to the bank from here.
          </p>
          <div className="toolbar">
            <label className="field">
              <span>Store</span>
              <select
                className="input"
                data-testid="pay-pmt-store"
                disabled={!writable}
                value={payment.store}
                onChange={(e) => setPayment({ ...payment, store: e.target.value, lines: {} })}
              >
                <option value="">Pick…</option>
                {onStores.map((s) => (
                  <option key={s.id} value={String(s.id)}>
                    {s.code} · {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Vendor</span>
              <select
                className="input"
                data-testid="pay-pmt-vendor"
                disabled={!writable}
                value={payment.vendor}
                onChange={(e) => setPayment({ ...payment, vendor: e.target.value, lines: {} })}
              >
                <option value="">Pick…</option>
                {vendorsHere.map((v) => (
                  <option key={v.id} value={String(v.id)}>
                    {v.name}
                    {v.is_active ? "" : " (retired)"}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Paid on</span>
              <input
                className="input"
                type="date"
                data-testid="pay-pmt-date"
                disabled={!writable}
                value={payment.date}
                onChange={(e) => setPayment({ ...payment, date: e.target.value })}
              />
            </label>
            <label className="field">
              <span>How</span>
              <select
                className="input"
                data-testid="pay-pmt-mode"
                disabled={!writable}
                value={payment.mode}
                onChange={(e) => setPayment({ ...payment, mode: e.target.value })}
              >
                {(options.modes.length ? options.modes : Object.keys(MODE_LABEL)).map((m) => (
                  <option key={m} value={m}>
                    {MODE_LABEL[m] ?? m}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Reference</span>
              <input
                className="input"
                maxLength={60}
                placeholder="UTR, cheque or UPI reference"
                data-testid="pay-pmt-reference"
                disabled={!writable}
                value={payment.reference}
                onChange={(e) => setPayment({ ...payment, reference: e.target.value })}
              />
            </label>
            <label className="field">
              <span>Amount paid (Rs)</span>
              <input
                className="input"
                inputMode="decimal"
                data-testid="pay-pmt-amount"
                disabled={!writable}
                value={payment.rupees}
                onChange={(e) => {
                  const rupees = e.target.value;
                  const paise = rupeesToPaise(rupees);
                  setPayment({
                    ...payment,
                    rupees,
                    lines: paise ? spreadOldestFirst(open, Number(paise)) : payment.lines,
                  });
                }}
              />
            </label>
          </div>
          {payment.store && payment.vendor && open.length === 0 && (
            <p className="muted-cell" data-testid="pay-pmt-nothing">
              Nothing is left to pay this vendor at this store.
            </p>
          )}
          {open.length > 0 && (
            <div className="table-wrap">
              <table className="data" data-testid="pay-pmt-lines">
                <thead>
                  <tr>
                    <th>Invoice</th>
                    <th>Brand</th>
                    <th>Due</th>
                    <th className="num">Left to pay</th>
                    <th className="num">Pay now (Rs)</th>
                  </tr>
                </thead>
                <tbody>
                  {open.map((inv) => (
                    <tr key={inv.id}>
                      <td>{inv.invoice_number}</td>
                      <td>{inv.brand.name}</td>
                      <td>{inv.due_date ?? "Unknown"}</td>
                      <td className="num">
                        <Money paise={Number(inv.outstanding_paise)} />
                      </td>
                      <td className="num">
                        <input
                          className="input"
                          inputMode="decimal"
                          aria-label={`Pay now against ${inv.invoice_number}`}
                          data-testid={`pay-pmt-line-${inv.id}`}
                          disabled={!writable}
                          value={payment.lines[inv.id] ?? ""}
                          onChange={(e) =>
                            setPayment({ ...payment, lines: { ...payment.lines, [inv.id]: e.target.value } })
                          }
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!checkedPayment.ok && payment.rupees && (
            <p className="warn-note" data-testid="pay-pmt-problem">
              {checkedPayment.problem}
            </p>
          )}
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="pay-pmt-save"
              disabled={!writable || !checkedPayment.ok}
              onClick={() => {
                if (!checkedPayment.ok) return;
                void send({
                  url: `${PAGE_API}/payments`,
                  attempt: { noteId: 0, revision: 0, step: "payment" },
                  body: checkedPayment.body,
                  said: `Payment ${payment.reference.trim()} recorded.`,
                  after: () => setPayment(emptyPayment(payment.store)),
                });
              }}
            >
              Record the payment
            </button>
          </div>
        </section>
      )}

      {data && (
        <section className="card section-card">
          <h2 className="h3">Invoices</h2>
          {data.invoices.length === 0 ? (
            <p className="muted-cell">No vendor invoices recorded at these stores.</p>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid="pay-invoices">
                <thead>
                  <tr>
                    <th>Invoice</th>
                    <th>Vendor</th>
                    <th>Brand</th>
                    <th>Store</th>
                    <th>Dated</th>
                    <th>Due</th>
                    <th className="num">Total</th>
                    <th className="num">Left to pay</th>
                    <th>Where it stands</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.invoices.map((inv) => (
                    <tr key={inv.id} data-testid={`pay-inv-${inv.id}`}>
                      <td>{inv.invoice_number}</td>
                      <td>{inv.vendor.name}</td>
                      <td>
                        {inv.brand.name}
                        {inv.model === "unknown" && <div className="muted-cell">Model unknown</div>}
                      </td>
                      <td>{inv.store.code}</td>
                      <td>{inv.invoice_date}</td>
                      <td>{inv.due_date ?? "Unknown"}</td>
                      <td className="num">
                        <Money paise={Number(inv.amount_paise)} />
                      </td>
                      <td className="num">
                        <Amount paise={inv.outstanding_paise} />
                      </td>
                      <td>
                        {inv.status === "cancelled" ? (
                          <span className="chip">Cancelled: {inv.cancel_reason}</span>
                        ) : (
                          <span className={`chip ${inv.band && inv.band !== "not_due" ? "chip-amber" : ""}`}>
                            {bandLabel(inv.band)}
                          </span>
                        )}
                      </td>
                      <td>
                        {inv.allowed_actions.includes("cancel") && (
                          <button
                            type="button"
                            className="btn"
                            data-testid={`pay-inv-cancel-${inv.id}`}
                            disabled={!writable}
                            onClick={() =>
                              setCancelling({ kind: "invoices", id: inv.id, revision: inv.revision, reason: "" })
                            }
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2 className="h3">Payments</h2>
          {data.payments.length === 0 ? (
            <p className="muted-cell">No payments recorded at these stores.</p>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid="pay-payments">
                <thead>
                  <tr>
                    <th>Paid on</th>
                    <th>Vendor</th>
                    <th>Store</th>
                    <th>How</th>
                    <th>Reference</th>
                    <th className="num">Amount</th>
                    <th>Invoices</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.payments.map((pmt) => (
                    <tr key={pmt.id} data-testid={`pay-pmt-${pmt.id}`}>
                      <td>{pmt.paid_on}</td>
                      <td>{pmt.vendor.name}</td>
                      <td>{pmt.store.code}</td>
                      <td>{MODE_LABEL[pmt.mode] ?? pmt.mode}</td>
                      <td>
                        {pmt.reference}
                        {pmt.status === "cancelled" && <div className="chip">Cancelled: {pmt.cancel_reason}</div>}
                      </td>
                      <td className="num">
                        <Money paise={Number(pmt.amount_paise)} />
                      </td>
                      <td>
                        {pmt.allocations.map((a) => (
                          <div key={a.invoice_id}>
                            {a.invoice_number} ({a.brand_name}): <Money paise={Number(a.amount_paise)} />
                          </div>
                        ))}
                      </td>
                      <td>
                        {pmt.allowed_actions.includes("cancel") && (
                          <button
                            type="button"
                            className="btn"
                            data-testid={`pay-pmt-cancel-${pmt.id}`}
                            disabled={!writable}
                            onClick={() =>
                              setCancelling({ kind: "payments", id: pmt.id, revision: pmt.revision, reason: "" })
                            }
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {cancelling && (
            <div className="toolbar" data-testid="pay-cancel-form">
              <label className="field">
                <span>Why cancel this {cancelling.kind === "invoices" ? "invoice" : "payment"}?</span>
                <input
                  className="input"
                  maxLength={240}
                  data-testid="pay-cancel-reason"
                  disabled={!writable}
                  value={cancelling.reason}
                  onChange={(e) => setCancelling({ ...cancelling, reason: e.target.value })}
                />
              </label>
              <button
                type="button"
                className="btn btn-primary"
                data-testid="pay-cancel-confirm"
                disabled={!writable || !cancelling.reason.trim()}
                onClick={() =>
                  void send({
                    url: `${PAGE_API}/${cancelling.kind}/${cancelling.id}/cancel`,
                    attempt: { noteId: cancelling.id, revision: cancelling.revision, step: `cancel:${cancelling.kind}` },
                    body: { reason: cancelling.reason.trim() },
                    said:
                      cancelling.kind === "invoices"
                        ? "Invoice cancelled."
                        : "Payment cancelled. What it paid is owed again.",
                    after: () => setCancelling(null),
                  })
                }
              >
                Cancel it
              </button>
              <button type="button" className="btn" disabled={busy} onClick={() => setCancelling(null)}>
                Keep it
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
