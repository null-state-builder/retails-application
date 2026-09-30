// Money in vs out for one day, split by account — the daily read Accounts
// asked for directly (`KDPS Daily Work Survey - Accounts Team.pdf`: the
// closest thing today is a month-end DSR reconciliation, and everything
// before that "always needs checking"). Reads the same `CashLedgerEntry` rows
// the Cash Ledger already lists (`/money/cash`); this just slices one day
// and keeps in/out apart instead of netting them into one running balance.
import { useState } from "react";
import { ArrowDownRight, ArrowUpRight, Scale } from "lucide-react";

import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { Money } from "../lib/format";
import { useDoc } from "../lib/hooks";
import "./Shared.css";

interface DailyAccount {
  account: string;
  in_paise: number;
  out_paise: number;
  net_paise: number;
}

interface DailyEntry {
  id: number;
  created_at: string;
  doc_number: string;
  kind: string;
  account: string;
  amount: number;
  description: string;
  mode: string;
  vendor_name?: string | null;
}

interface DailyCashResponse {
  date: string;
  money_in_paise: number;
  money_out_paise: number;
  net_paise: number;
  accounts: DailyAccount[];
  entries: DailyEntry[];
}

const ACCOUNT_LABEL: Record<string, string> = {
  CASH: "Cash",
  BANK: "Bank",
  UPI: "UPI",
  CARD: "Card",
};

function today(): string {
  return new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local time
}

function when(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}

export function DailyCashPage() {
  const [day, setDay] = useState(today());
  const { data, loading } = useDoc<DailyCashResponse>(`/finledger/cash/daily?date=${day}`);

  return (
    <OperationsPage data-testid="daily-cash-page">
      <PageHeader lead="What moved today, by account — money in and money out kept apart, not netted into one balance." />
      <div className="toolbar" style={{ marginBottom: 18 }}>
        <input
          type="date"
          className="input"
          value={day}
          max={today()}
          onChange={(e) => setDay(e.target.value)}
          data-testid="daily-cash-date-input"
        />
      </div>
      {loading ? (
        <p>Loading…</p>
      ) : !data ? (
        <p className="muted-cell">Could not load.</p>
      ) : (
        <>
          <div className="stat-grid">
            <div className="card stat-card">
              <ArrowDownRight size={18} style={{ color: "var(--green)" }} />
              <div className="stat-value mono" data-testid="dc-money-in">
                <Money paise={data.money_in_paise} />
              </div>
              <div className="stat-label">Money in</div>
            </div>
            <div className="card stat-card">
              <ArrowUpRight size={18} style={{ color: "var(--red)" }} />
              <div className="stat-value mono" data-testid="dc-money-out">
                <Money paise={data.money_out_paise} />
              </div>
              <div className="stat-label">Money out</div>
            </div>
            <div className="card stat-card">
              <Scale size={18} style={{ color: "var(--rust)" }} />
              <div className="stat-value mono" data-testid="dc-net">
                <Money paise={data.net_paise} />
              </div>
              <div className="stat-label">Net for the day</div>
            </div>
          </div>
          <div className="table-wrap" style={{ marginBottom: 24 }}>
            <table className="data" data-testid="daily-cash-accounts-table">
              <thead>
                <tr>
                  <th>Account</th>
                  <th className="num">In</th>
                  <th className="num">Out</th>
                  <th className="num">Net</th>
                </tr>
              </thead>
              <tbody>
                {data.accounts.map((a) => (
                  <tr key={a.account} data-testid={`dc-account-row-${a.account}`}>
                    <td>
                      <b>{ACCOUNT_LABEL[a.account] ?? a.account}</b>
                    </td>
                    <td className="num" style={{ color: "var(--green)" }}>
                      <Money paise={a.in_paise} />
                    </td>
                    <td className="num" style={{ color: "var(--red)" }}>
                      <Money paise={a.out_paise} />
                    </td>
                    <td className="num">
                      <Money paise={a.net_paise} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.entries.length === 0 ? (
            <p className="muted-cell" data-testid="daily-cash-empty">
              Nothing moved on this day.
            </p>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid="daily-cash-entries-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Voucher</th>
                    <th>Account</th>
                    <th>Note</th>
                    <th className="num">Amount ₹</th>
                  </tr>
                </thead>
                <tbody>
                  {data.entries.map((e) => (
                    <tr key={e.id}>
                      <td className="mono">{when(e.created_at)}</td>
                      <td className="mono">{e.doc_number}</td>
                      <td>{ACCOUNT_LABEL[e.account] ?? e.account}</td>
                      <td>
                        {e.description}
                        {e.vendor_name ? ` · ${e.vendor_name}` : ""}
                      </td>
                      <td
                        className="num mono"
                        style={{
                          fontWeight: 700,
                          color: e.amount >= 0 ? "var(--green)" : "var(--red)",
                        }}
                      >
                        <Money paise={e.amount} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </OperationsPage>
  );
}

export default DailyCashPage;
