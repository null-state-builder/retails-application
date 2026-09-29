import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  IndianRupee,
  Plus,
  ReceiptText,
  RotateCcw,
  Send,
  TimerReset,
  Users,
  X,
} from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { userCan } from "../shell/navConfig";
import { api, apiErrorMessage } from "../lib/api";
import { Money } from "../lib/format";
import { ListSearchBar } from "../components/SearchBox";
import "./Booking.css";
import "./Shared.css";
import { PageHeader } from "../components/PageHeader";

interface BalanceT {
  vendor_id: number;
  vendor_code: string;
  vendor_name: string;
  outstanding_paise: number;
  entries: number;
}
interface AgeingRowT {
  vendor_id: number;
  vendor_code: string;
  vendor_name: string;
  payment_terms: string;
  oldest_days: number;
  bucket_0_30_paise: number;
  bucket_31_60_paise: number;
  bucket_60_plus_paise: number;
  total_due_paise: number;
  open_bill_count: number;
}
interface AgeingT {
  as_of: string;
  total_due_paise: number;
  bucket_0_30_paise: number;
  bucket_31_60_paise: number;
  bucket_60_plus_paise: number;
  rows: AgeingRowT[];
}
interface EntryT {
  id: number;
  created_at: string;
  doc_number: string;
  kind: string;
  kind_label: string;
  vendor_name: string;
  amount: number;
  description: string;
  reference: string;
}
type Mode = "bill" | "payment" | null;

export default function VendorLedger() {
  const { user } = useAuth();
  // Mirrors the server gate exactly (`finledger.IsBooksKeeper` = money:manage),
  // read from the same section payload rather than a role list of our own.
  const isFinance = userCan(user, "money", "manage");

  const [balances, setBalances] = useState<{
    total_payable_paise: number;
    vendors_with_dues: number;
    rows: BalanceT[];
  }>();
  const [ageing, setAgeing] = useState<AgeingT>();
  const [entries, setEntries] = useState<EntryT[]>([]);
  const [vendors, setVendors] = useState<{ id: number; name: string }[]>([]);
  const [page, setPage] = useState(1);
  const [count, setCount] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [entriesLoading, setEntriesLoading] = useState(true);
  const [q, setQ] = useState("");

  const [mode, setMode] = useState<Mode>(null);
  const [form, setForm] = useState({
    vendor_id: "",
    amount: "",
    description: "",
    reference: "",
    payMode: "cash",
  });
  const PAGE_SIZE = 50;

  function loadAll() {
    api.get("/finledger/vendor/balances").then((r) => setBalances(r.data));
    api.get("/finledger/vendor/ageing").then((r) => setAgeing(r.data));
    setEntriesLoading(true);
    const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
    if (q.trim()) params.set("q", q.trim());
    api
      .get(`/finledger/vendor/entries?${params}`)
      .then((r) => {
        setEntries(r.data.results);
        setCount(r.data.count);
        setHasNext(Boolean(r.data.next));
      })
      .finally(() => setEntriesLoading(false));
  }
  useEffect(loadAll, [page, q]);
  useEffect(() => setPage(1), [q]);
  useEffect(() => {
    if (isFinance)
      api
        .get("/vendors")
        .then((r) => setVendors(r.data))
        .catch(() => {});
  }, [isFinance]);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      const path = mode === "bill" ? "/finledger/vendor/bill" : "/finledger/vendor/payment";
      await api.post(path, {
        vendor_id: Number(form.vendor_id),
        amount: form.amount,
        description: form.description,
        reference: form.reference,
        mode: form.payMode,
      });
      setMode(null);
      setForm({ vendor_id: "", amount: "", description: "", reference: "", payMode: "cash" });
      setPage(1);
      loadAll();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function reverse(id: number) {
    setBusy(true);
    try {
      await api.post(`/finledger/vendor/entries/${id}/reverse`, {});
      loadAll();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page-pad" data-testid="vendor-ledger-page">
      <PageHeader
        lead="Accounts payable, brand by brand. Append-only: a correction is a reversing entry."
        actions={
          isFinance && (
            <>
              <button
                className="btn"
                onClick={() => {
                  setMode("bill");
                  setError("");
                }}
                data-testid="vl-record-bill-btn"
              >
                <ReceiptText size={15} /> Record Bill
              </button>
              <button
                className="btn btn-cta"
                onClick={() => {
                  setMode("payment");
                  setError("");
                }}
                data-testid="vl-record-payment-btn"
              >
                <Send size={15} /> Record Payment
              </button>
            </>
          )
        }
      />

      <div className="stat-grid">
        <div className="card stat-card">
          <IndianRupee size={18} style={{ color: "var(--rust)" }} />
          <div className="stat-value mono">
            <Money paise={balances?.total_payable_paise ?? 0} />
          </div>
          <div className="stat-label">Total payable (₹)</div>
        </div>
        <div className="card stat-card">
          <Users size={18} style={{ color: "var(--rust)" }} />
          <div className="stat-value mono">{balances?.vendors_with_dues ?? 0}</div>
          <div className="stat-label">Vendors with dues</div>
        </div>
        <div className="card stat-card">
          <TimerReset size={18} style={{ color: "var(--amber)" }} />
          <div className="stat-value mono" data-testid="vl-ageing-0-30">
            <Money paise={ageing?.bucket_0_30_paise ?? 0} />
          </div>
          <div className="stat-label">0–30 days (₹)</div>
        </div>
        <div className="card stat-card">
          <TimerReset size={18} style={{ color: "var(--rust)" }} />
          <div className="stat-value mono" data-testid="vl-ageing-31-60">
            <Money paise={ageing?.bucket_31_60_paise ?? 0} />
          </div>
          <div className="stat-label">31–60 days (₹)</div>
        </div>
        <div className="card stat-card">
          <AlertTriangle size={18} style={{ color: "var(--red)" }} />
          <div className="stat-value mono" data-testid="vl-ageing-60-plus">
            <Money paise={ageing?.bucket_60_plus_paise ?? 0} />
          </div>
          <div className="stat-label">60+ days (₹)</div>
        </div>
        <div className="card stat-card">
          <ReceiptText size={18} style={{ color: "var(--rust)" }} />
          <div className="stat-value mono">{count}</div>
          <div className="stat-label">Ledger entries</div>
        </div>
      </div>

      {mode && (
        <div className="card section-card" data-testid="vl-form" style={{ marginTop: 16 }}>
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">
              {mode === "bill"
                ? "Record vendor bill (we owe)"
                : "Record payment (reduces dues + cash out)"}
            </h3>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={() => setMode(null)} data-testid="vl-cancel">
              <X size={14} /> Cancel
            </button>
          </div>
          <div className="form-grid">
            <select
              className="select"
              value={form.vendor_id}
              onChange={(e) => setForm({ ...form, vendor_id: e.target.value })}
              data-testid="vl-vendor-select"
            >
              <option value="">Vendor…</option>
              {vendors.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name}
                </option>
              ))}
            </select>
            <input
              className="input"
              type="number"
              placeholder="Amount ₹"
              value={form.amount}
              onChange={(e) => setForm({ ...form, amount: e.target.value })}
              data-testid="vl-amount-input"
            />
            {mode === "bill" ? (
              <input
                className="input"
                placeholder="Invoice ref (optional)"
                value={form.reference}
                onChange={(e) => setForm({ ...form, reference: e.target.value })}
                data-testid="vl-ref-input"
              />
            ) : (
              <select
                className="select"
                value={form.payMode}
                onChange={(e) => setForm({ ...form, payMode: e.target.value })}
                data-testid="vl-mode-select"
              >
                <option value="cash">Cash</option>
                <option value="bank">Bank</option>
                <option value="upi">UPI</option>
              </select>
            )}
            <input
              className="input"
              placeholder="Description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              data-testid="vl-desc-input"
            />
            <button
              className="btn btn-cta"
              disabled={busy || !form.vendor_id || !form.amount}
              onClick={submit}
              data-testid="vl-submit"
            >
              <Plus size={15} /> {mode === "bill" ? "Add bill" : "Pay"}
            </button>
          </div>
          {error && (
            <div className="warn-note" style={{ marginTop: 10 }} data-testid="vl-error">
              {error}
            </div>
          )}
        </div>
      )}

      <h3 className="h3" style={{ margin: "22px 0 8px" }}>
        Outstanding by vendor
      </h3>
      {!balances || balances.rows.length === 0 ? (
        <div className="card section-card" data-testid="vl-balances-empty">
          No outstanding vendor dues.
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="vl-balances-table">
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Code</th>
                <th className="num">Entries</th>
                <th className="num">Outstanding ₹</th>
              </tr>
            </thead>
            <tbody>
              {balances.rows.map((b) => (
                <tr key={b.vendor_id} data-testid={`vl-balance-row-${b.vendor_id}`}>
                  <td>
                    <b>{b.vendor_name}</b>
                  </td>
                  <td className="mono">{b.vendor_code}</td>
                  <td className="num">{b.entries}</td>
                  <td className="num mono" style={{ fontWeight: 700 }}>
                    <Money paise={b.outstanding_paise} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="h3" style={{ margin: "22px 0 8px" }}>
        Vendor dues ageing
      </h3>
      {!ageing || ageing.rows.length === 0 ? (
        <div className="card section-card" data-testid="vl-ageing-empty">
          No open vendor dues to age.
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="vl-ageing-table">
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Terms</th>
                <th className="num">Oldest</th>
                <th className="num">0–30 ₹</th>
                <th className="num">31–60 ₹</th>
                <th className="num">60+ ₹</th>
                <th className="num">Total ₹</th>
              </tr>
            </thead>
            <tbody>
              {ageing.rows.map((r) => (
                <tr key={r.vendor_id} data-testid={`vl-ageing-row-${r.vendor_id}`}>
                  <td>
                    <b>{r.vendor_name}</b>
                    <div className="mono" style={{ fontSize: 12 }}>
                      {r.vendor_code} · {r.open_bill_count} bill(s)
                    </div>
                  </td>
                  <td>{r.payment_terms || "—"}</td>
                  <td className="num">{r.oldest_days}d</td>
                  <td className="num mono">
                    <Money paise={r.bucket_0_30_paise} />
                  </td>
                  <td className="num mono" style={{ fontWeight: r.bucket_31_60_paise ? 700 : 400 }}>
                    <Money paise={r.bucket_31_60_paise} />
                  </td>
                  <td
                    className="num mono"
                    style={{
                      fontWeight: r.bucket_60_plus_paise ? 800 : 400,
                      color: r.bucket_60_plus_paise ? "var(--red)" : "inherit",
                    }}
                  >
                    <Money paise={r.bucket_60_plus_paise} />
                  </td>
                  <td className="num mono" style={{ fontWeight: 800 }}>
                    <Money paise={r.total_due_paise} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="h3" style={{ margin: "22px 0 8px" }}>
        All entries
      </h3>
      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search entries — doc number, party"
        label="Search vendor ledger entries"
        testId="vl-entries-search"
        noun="posting"
        count={count}
        loading={entriesLoading}
      />
      {entries.length === 0 ? (
        <div className="card section-card" data-testid="vl-entries-empty">
          {q ? `No entry matches “${q}”.` : "No vendor ledger entries yet."}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="vl-entries-table">
            <thead>
              <tr>
                <th>Voucher</th>
                <th>Type</th>
                <th>Vendor</th>
                <th>Description</th>
                <th className="num">Amount ₹</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} data-testid={`vl-entry-row-${e.id}`}>
                  <td className="mono">{e.doc_number}</td>
                  <td>
                    <span
                      className={`chip chip-${e.kind === "bill" ? "amber" : e.kind === "payment" ? "green" : "red"}`}
                    >
                      {e.kind_label}
                    </span>
                  </td>
                  <td>{e.vendor_name}</td>
                  <td>
                    {e.description}
                    {e.reference ? ` · ${e.reference}` : ""}
                  </td>
                  <td
                    className="num mono"
                    style={{ fontWeight: 700, color: e.amount < 0 ? "var(--rust)" : "inherit" }}
                  >
                    <Money paise={e.amount} />
                  </td>
                  <td>
                    {isFinance && e.kind !== "reversal" && (
                      <button
                        className="btn btn-sm"
                        disabled={busy}
                        onClick={() => reverse(e.id)}
                        data-testid={`vl-reverse-${e.id}`}
                      >
                        <RotateCcw size={13} /> Reverse
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="pager" data-testid="vl-pager">
            <span className="pager-info">
              Showing {(page - 1) * PAGE_SIZE + 1}–{(page - 1) * PAGE_SIZE + entries.length} of{" "}
              {count}
            </span>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
              data-testid="vl-prev"
            >
              Prev
            </button>
            <span className="pager-page">Page {page}</span>
            <button
              className="btn btn-sm"
              disabled={!hasNext}
              onClick={() => setPage((p) => p + 1)}
              data-testid="vl-next"
            >
              Next
            </button>
          </div>
        </div>
      )}
      <Link to="/money/cash" className="btn" style={{ marginTop: 18 }}>
        Cash Ledger →
      </Link>
    </div>
  );
}
