// Partner dues — what each partner store owes, summed off the Purchase-Price
// figure every transfer to it already carries (`partner_billing_value_paise`),
// net of whatever Accounts has recorded against it (`PartnerSettlementsView`).
import { useState, Fragment } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, IndianRupee, Plus, Printer, RotateCcw, Send, X } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { userCan } from "../shell/navConfig";
import { api, apiErrorMessage } from "../lib/api";
import { PageHeader } from "../components/PageHeader";
import { Money } from "../lib/format";
import { useDoc } from "../lib/hooks";
import { settlementReceiptHtml } from "../lib/settlementReceipt";
import { browserPrintAdapter } from "../till/print";
import "./Shared.css";

interface DueTransfer {
  id: number;
  doc_number: string | null;
  source_store_code: string;
  dispatch_date: string | null;
  partner_billing_value_paise: number;
}

interface DueStore {
  store_id: number;
  store_code: string;
  store_name: string;
  total_owed_paise: number;
  total_paid_paise: number;
  net_outstanding_paise: number;
  transfer_count: number;
  last_dispatch_date: string | null;
  transfers: DueTransfer[];
}

interface PartnerDuesResponse {
  stores: DueStore[];
  total_owed_paise: number;
  total_paid_paise: number;
  net_outstanding_paise: number;
  billing_mode: "informational" | "gl_posting";
}

interface SettlementRow {
  id: number;
  created_at: string;
  doc_number: string;
  kind: "payment" | "reversal";
  store_id: number;
  store_code: string;
  store_name: string;
  amount_paise: number;
  description: string;
  reference: string;
  mode: string;
  posted_by_name: string;
}

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString("en-IN") : "—";
}

export function PartnerDuesPage() {
  const { user } = useAuth();
  // Mirrors the server gate exactly (`money: manage`), read from the same
  // section payload rather than a role list of our own.
  const isFinance = userCan(user, "money", "manage");

  const { data: resp, loading, reload: reloadDues } = useDoc<PartnerDuesResponse>("/outbound/partner-dues");
  const { data: settlements, reload: reloadSettlements } = useDoc<{ rows: SettlementRow[] }>(
    isFinance ? "/outbound/partner-settlements" : null,
  );
  const [open, setOpen] = useState<number | null>(null);
  const [settleFor, setSettleFor] = useState<DueStore | null>(null);
  const [form, setForm] = useState({ amount: "", mode: "cash", reference: "", description: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const stores = resp?.stores ?? [];

  function startSettle(s: DueStore) {
    setSettleFor(s);
    setOpen(s.store_id);
    setForm({ amount: "", mode: "cash", reference: "", description: "" });
    setError("");
  }

  async function submitSettlement() {
    if (!settleFor) return;
    setBusy(true);
    setError("");
    try {
      await api.post("/outbound/partner-settlements", {
        store_id: settleFor.store_id,
        amount: form.amount,
        mode: form.mode,
        reference: form.reference,
        description: form.description,
      });
      setSettleFor(null);
      reloadDues();
      reloadSettlements();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function reverseSettlement(id: number) {
    setBusy(true);
    try {
      await api.post(`/outbound/partner-settlements/${id}/reverse`, {});
      reloadDues();
      reloadSettlements();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function printReceipt(h: SettlementRow) {
    await browserPrintAdapter.print(
      settlementReceiptHtml({
        doc_number: h.doc_number,
        created_at: h.created_at,
        store_code: h.store_code,
        store_name: h.store_name,
        amount_paise: h.amount_paise,
        mode: h.mode,
        reference: h.reference,
        description: h.description,
        posted_by_name: h.posted_by_name,
      }),
    );
  }

  return (
    <div className="page-pad" data-testid="partner-dues-page">
      <PageHeader lead="What every partner store owes so far, at Purchase Price, net of what Accounts has recorded as received. Nothing here edits a past entry — a correction is a reversing settlement, same as every other KDPS ledger." />
      {loading ? (
        <p>Loading…</p>
      ) : stores.length === 0 ? (
        <p className="muted-cell" data-testid="partner-dues-empty">
          No partner store has been billed yet — mark a store "Partner" in Setup → Stores, then dispatch a
          transfer to it.
        </p>
      ) : (
        <>
          <div className="stat-grid">
            <div className="card stat-card">
              <IndianRupee size={18} style={{ color: "var(--rust)" }} />
              <div className="stat-value mono" data-testid="pd-total-owed"><Money paise={resp!.total_owed_paise} /></div>
              <div className="stat-label">Total billed, all partner stores</div>
            </div>
            <div className="card stat-card">
              <IndianRupee size={18} style={{ color: "var(--green)" }} />
              <div className="stat-value mono" data-testid="pd-total-paid"><Money paise={resp!.total_paid_paise} /></div>
              <div className="stat-label">Total received</div>
            </div>
            <div className="card stat-card">
              <IndianRupee size={18} style={{ color: "var(--red)" }} />
              <div className="stat-value mono" data-testid="pd-net-outstanding"><Money paise={resp!.net_outstanding_paise} /></div>
              <div className="stat-label">Net outstanding</div>
            </div>
          </div>
          <p className="muted-cell" style={{ margin: "10px 0 18px" }}>
            Billing policy is currently{" "}
            <b>{resp!.billing_mode === "gl_posting" ? "posting a receivable" : "informational only"}</b> —{" "}
            <Link to="/money/partner-billing">change it here</Link>.
          </p>
          <div className="table-wrap">
            <table className="data" data-testid="partner-dues-table">
              <thead>
                <tr>
                  <th />
                  <th>Store</th>
                  <th className="num">Owed</th>
                  <th className="num">Paid</th>
                  <th className="num">Outstanding</th>
                  <th className="num">Transfers</th>
                  <th>Last dispatch</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {stores.map((s) => {
                  const expanded = open === s.store_id;
                  const history = (settlements?.rows ?? []).filter((r) => r.store_id === s.store_id);
                  return (
                    <Fragment key={s.store_id}>
                      <tr data-testid={`partner-due-row-${s.store_code}`}>
                        <td>
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => setOpen(expanded ? null : s.store_id)}
                            data-testid={`partner-due-toggle-${s.store_code}`}
                            aria-expanded={expanded}
                          >
                            {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                          </button>
                        </td>
                        <td><b className="mono">{s.store_code}</b> — {s.store_name}</td>
                        <td className="num"><Money paise={s.total_owed_paise} /></td>
                        <td className="num" style={{ color: "var(--green)" }}><Money paise={s.total_paid_paise} /></td>
                        <td className="num">
                          <b style={{ color: s.net_outstanding_paise > 0 ? "var(--red)" : "inherit" }}>
                            <Money paise={s.net_outstanding_paise} />
                          </b>
                        </td>
                        <td className="num">{s.transfer_count}</td>
                        <td>{fmtDate(s.last_dispatch_date)}</td>
                        <td>
                          {isFinance && (
                            <button
                              type="button"
                              className="btn btn-sm btn-cta"
                              onClick={() => startSettle(s)}
                              data-testid={`partner-due-settle-${s.store_code}`}
                            >
                              <Send size={13} /> Record Payment
                            </button>
                          )}
                        </td>
                      </tr>
                      {expanded && (
                        <tr>
                          <td />
                          <td colSpan={7}>
                            {settleFor?.store_id === s.store_id && (
                              <div className="card section-card" data-testid="pd-settle-form" style={{ marginBottom: 14 }}>
                                <div className="toolbar" style={{ marginBottom: 12 }}>
                                  <h3 className="h3">Record payment from {s.store_code}</h3>
                                  <div className="spacer" />
                                  <button className="btn btn-sm" onClick={() => setSettleFor(null)} data-testid="pd-settle-cancel"><X size={14} /> Cancel</button>
                                </div>
                                <div className="form-grid">
                                  <input
                                    className="input"
                                    type="number"
                                    placeholder="Amount ₹"
                                    value={form.amount}
                                    onChange={(e) => setForm({ ...form, amount: e.target.value })}
                                    data-testid="pd-settle-amount-input"
                                  />
                                  <select
                                    className="select"
                                    value={form.mode}
                                    onChange={(e) => setForm({ ...form, mode: e.target.value })}
                                    data-testid="pd-settle-mode-select"
                                  >
                                    <option value="cash">Cash</option>
                                    <option value="bank">Bank</option>
                                    <option value="upi">UPI</option>
                                    <option value="cheque">Cheque</option>
                                    <option value="other">Other</option>
                                  </select>
                                  <input
                                    className="input"
                                    placeholder="Reference (optional)"
                                    value={form.reference}
                                    onChange={(e) => setForm({ ...form, reference: e.target.value })}
                                    data-testid="pd-settle-reference-input"
                                  />
                                  <input
                                    className="input"
                                    placeholder="Description (optional)"
                                    value={form.description}
                                    onChange={(e) => setForm({ ...form, description: e.target.value })}
                                    data-testid="pd-settle-description-input"
                                  />
                                  <button
                                    className="btn btn-cta"
                                    disabled={busy || !form.amount}
                                    onClick={submitSettlement}
                                    data-testid="pd-settle-submit"
                                  >
                                    <Plus size={15} /> Record
                                  </button>
                                </div>
                                {error && <div className="warn-note" style={{ marginTop: 10 }} data-testid="pd-settle-error">{error}</div>}
                              </div>
                            )}
                            <table className="data" data-testid={`partner-due-detail-${s.store_code}`}>
                              <thead>
                                <tr><th>Transfer</th><th>From</th><th>Dispatched</th><th className="num">Billed</th></tr>
                              </thead>
                              <tbody>
                                {s.transfers.map((t) => (
                                  <tr key={t.id}>
                                    <td>
                                      <Link to={`/transfer/${t.id}`} className="mono">
                                        {t.doc_number || `Draft #${t.id}`}
                                      </Link>
                                    </td>
                                    <td>{t.source_store_code}</td>
                                    <td>{fmtDate(t.dispatch_date)}</td>
                                    <td className="num"><Money paise={t.partner_billing_value_paise} /></td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                            {isFinance && (
                              <>
                                <h4 style={{ margin: "16px 0 8px", fontSize: 13, color: "var(--muted)" }}>Payments received</h4>
                                {history.length === 0 ? (
                                  <p className="muted-cell" data-testid={`partner-due-history-empty-${s.store_code}`}>No payments recorded yet.</p>
                                ) : (
                                  <table className="data" data-testid={`partner-due-history-${s.store_code}`}>
                                    <thead>
                                      <tr><th>Voucher</th><th>When</th><th>Mode</th><th>Note</th><th className="num">Amount ₹</th><th /></tr>
                                    </thead>
                                    <tbody>
                                      {history.map((h) => (
                                        <tr key={h.id} data-testid={`pd-history-row-${h.id}`}>
                                          <td className="mono">{h.doc_number}</td>
                                          <td>{fmtDate(h.created_at)}</td>
                                          <td>{h.mode || "—"}</td>
                                          <td>{h.description}{h.reference ? ` · ${h.reference}` : ""}</td>
                                          <td className="num mono" style={{ fontWeight: 700, color: h.kind === "reversal" ? "var(--rust)" : "inherit" }}>
                                            <Money paise={h.amount_paise} />
                                          </td>
                                          <td>{h.kind !== "reversal" && (
                                            <div style={{ display: "flex", gap: 6 }}>
                                              <button
                                                className="btn btn-sm"
                                                onClick={() => printReceipt(h)}
                                                data-testid={`pd-history-print-${h.id}`}
                                              >
                                                <Printer size={12} /> Receipt
                                              </button>
                                              <button className="btn btn-sm" disabled={busy} onClick={() => reverseSettlement(h.id)} data-testid={`pd-history-reverse-${h.id}`}>
                                                <RotateCcw size={12} /> Reverse
                                              </button>
                                            </div>
                                          )}</td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                )}
                              </>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

export default PartnerDuesPage;
