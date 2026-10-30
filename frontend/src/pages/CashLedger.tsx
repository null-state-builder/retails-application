import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDownCircle, ArrowUpCircle, Plus, RotateCcw, X } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { userCan } from "../shell/navConfig";
import { api, apiErrorMessage } from "../lib/api";
import { Money } from "../lib/format";
import { KpiStrip, LeadFigure } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { ListSearchBar } from "../components/SearchBox";
import { Box, Section } from "../components/Section";
import { EmptyState } from "../components/States";
import "./Booking.css";
import "./Shared.css";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { UpdatedAgo } from "../components/UpdatedAgo";
import { useCachedGet } from "../lib/cachedQuery";

interface AccountT {
  account: string;
  balance_paise: number;
  entries: number;
}
interface EntryT {
  id: number;
  created_at: string;
  doc_number: string;
  kind: string;
  kind_label: string;
  account: string;
  amount: number;
  description: string;
  vendor_name: string;
}

export default function CashLedger() {
  const { user } = useAuth();
  // Mirrors the server gate exactly (`finledger.IsBooksKeeper` = money:manage),
  // read from the same section payload rather than a role list of our own.
  const isFinance = userCan(user, "money", "manage");

  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");

  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    direction: "in",
    amount: "",
    account: "CASH",
    description: "",
  });
  const PAGE_SIZE = 50;

  // Read through the shared cache: a revisit shows the last copy at once and
  // re-asks behind it (money reads are never trusted as fresh).
  const summaryRead = useCachedGet<{ total_paise: number; accounts: AccountT[] }>(
    "/finledger/cash/summary",
    { kind: "doc" },
  );
  const summary = summaryRead.data;
  const entriesRead = useCachedGet<{ results: EntryT[]; count: number; next: string | null }>(
    "/finledger/cash/entries",
    { params: { page, page_size: PAGE_SIZE, q } },
  );
  const entries = entriesRead.data?.results ?? [];
  const count = entriesRead.data?.count ?? 0;
  const hasNext = Boolean(entriesRead.data?.next);
  const entriesLoading = entriesRead.loading;
  function loadAll() {
    summaryRead.reload();
    entriesRead.reload();
  }
  useEffect(() => setPage(1), [q]);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      await api.post("/finledger/cash/movement", {
        direction: form.direction,
        amount: form.amount,
        account: form.account || "CASH",
        description: form.description,
      });
      setOpen(false);
      setForm({ direction: "in", amount: "", account: "CASH", description: "" });
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
      await api.post(`/finledger/cash/entries/${id}/reverse`, {});
      loadAll();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <OperationsPage data-testid="cash-ledger-page">
      <PageHeader
        lead="Cash and bank movements. Append-only: a correction is a reversing entry."
        actions={
          <>
            <UpdatedAgo
              updatedAt={entriesRead.updatedAt}
              fetching={entriesRead.fetching || summaryRead.fetching}
            />
            {isFinance && (
              <button
                className="btn btn-primary"
                onClick={() => {
                  setOpen(true);
                  setError("");
                }}
                data-testid="cl-record-btn"
              >
                <Plus size={15} /> Record Movement
              </button>
            )}
          </>
        }
      />

      <div data-testid="cl-summary">
        <LeadFigure
          label="Total cash on hand (₹)"
          value={
            <span className="mono" data-testid="cl-total">
              <Money paise={summary?.total_paise} />
            </span>
          }
        />
        {(summary?.accounts ?? []).length > 0 && (
          <KpiStrip
            label="By account"
            items={(summary?.accounts ?? []).map((a) => ({
              label: `${a.account} (${a.entries})`,
              value: (
                <span className="mono">
                  <Money paise={a.balance_paise} />
                </span>
              ),
            }))}
          />
        )}
      </div>

      {open && (
        <Box data-testid="cl-form" style={{ marginTop: 16 }}>
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">Record cash movement</h3>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={() => setOpen(false)} data-testid="cl-cancel">
              <X size={14} /> Cancel
            </button>
          </div>
          <div className="form-grid">
            <select
              className="select"
              value={form.direction}
              onChange={(e) => setForm({ ...form, direction: e.target.value })}
              data-testid="cl-direction-select"
            >
              <option value="in">Receipt (cash in)</option>
              <option value="out">Payment (cash out)</option>
            </select>
            <input
              className="input"
              type="number"
              placeholder="Amount ₹"
              value={form.amount}
              onChange={(e) => setForm({ ...form, amount: e.target.value })}
              data-testid="cl-amount-input"
            />
            <select
              className="select"
              value={form.account}
              onChange={(e) => setForm({ ...form, account: e.target.value })}
              data-testid="cl-account-input"
            >
              <option value="CASH">CASH</option>
              <option value="BANK">BANK</option>
              <option value="UPI">UPI</option>
            </select>
            <input
              className="input"
              placeholder="Description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              data-testid="cl-desc-input"
            />
            <button
              className="btn btn-cta"
              disabled={busy || !form.amount}
              onClick={submit}
              data-testid="cl-submit"
            >
              <Plus size={15} /> Save
            </button>
          </div>
          {error && (
            <Note tone="error" testId="cl-error">
              {error}
            </Note>
          )}
        </Box>
      )}

      <Section heading="All movements">
        <ListSearchBar
          value={q}
          onChange={setQ}
          placeholder="Search movements — doc number, party"
          label="Search cash ledger entries"
          testId="cl-entries-search"
          noun="movement"
          count={count}
          loading={entriesLoading}
        />
        {entries.length === 0 ? (
          <EmptyState testId="cl-entries-empty">
            {q ? `No movement matches “${q}”.` : "No cash movements yet."}
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="cl-entries-table">
              <thead>
                <tr>
                  <th>Voucher</th>
                  <th>Type</th>
                  <th>Account</th>
                  <th>Description</th>
                  <th className="num">Amount ₹</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.id} data-testid={`cl-entry-row-${e.id}`}>
                    <td className="mono">{e.doc_number}</td>
                    <td>
                      {e.kind === "receipt" ? (
                        <ArrowDownCircle size={12} style={{ verticalAlign: "-2px" }} aria-hidden />
                      ) : e.kind === "payment" ? (
                        <ArrowUpCircle size={12} style={{ verticalAlign: "-2px" }} aria-hidden />
                      ) : null}{" "}
                      {e.kind_label}
                    </td>
                    <td className="mono">{e.account}</td>
                    <td>
                      {e.description}
                      {e.vendor_name ? ` · ${e.vendor_name}` : ""}
                    </td>
                    <td
                      className="num mono"
                      style={{ fontWeight: 600, color: e.amount < 0 ? "var(--red)" : "inherit" }}
                    >
                      <Money paise={e.amount} />
                    </td>
                    <td className="act">
                      {isFinance && e.kind !== "reversal" && (
                        <button
                          className="btn btn-sm"
                          disabled={busy}
                          onClick={() => reverse(e.id)}
                          data-testid={`cl-reverse-${e.id}`}
                        >
                          <RotateCcw size={13} /> Reverse
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="pager" data-testid="cl-pager">
              <span className="pager-info">
                Showing {(page - 1) * PAGE_SIZE + 1}–{(page - 1) * PAGE_SIZE + entries.length} of{" "}
                {count}
              </span>
              <div className="spacer" />
              <button
                className="btn btn-sm"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                data-testid="cl-prev"
              >
                Prev
              </button>
              <span className="pager-page">Page {page}</span>
              <button
                className="btn btn-sm"
                disabled={!hasNext}
                onClick={() => setPage((p) => p + 1)}
                data-testid="cl-next"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </Section>
      <Link to="/money/vendor" className="btn" style={{ marginTop: 18 }}>
        ← Vendor Ledger
      </Link>
    </OperationsPage>
  );
}
