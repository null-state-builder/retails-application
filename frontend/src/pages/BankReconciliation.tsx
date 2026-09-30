// Bank Reconciliation — upload a bank statement (CSV or Excel, whichever the
// bank exports), match it against what the books already say
// (`finledger.reconciliation`), and clear up whatever the matcher wasn't sure
// about. A stopgap while waiting on the bank's own statement API (#132): once
// that's available, it becomes a second feed into the same matching engine,
// not a rebuild — this screen doesn't change either way.
import { useState } from "react";
import { AlertTriangle, CheckCircle2, HelpCircle, Link2, Upload, XCircle } from "lucide-react";

import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import { Money } from "../lib/format";
import { useDoc } from "../lib/hooks";
import "./Shared.css";

interface ImportRow {
  id: number;
  created_at: string;
  filename: string;
  bank_label: string;
  row_count: number;
  matched_count: number;
  uploaded_by_name: string;
}

interface Candidate {
  entry_id: number;
  score: number;
  doc_number: string;
  description: string;
  amount_paise: number;
  date: string;
}

interface LineRow {
  id: number;
  import_id: number;
  txn_date: string;
  narration: string;
  debit_paise: number;
  credit_paise: number;
  balance_paise: number | null;
  status: "matched" | "review" | "unmatched" | "ignored";
  match_confidence: number;
  candidates: Candidate[];
  matched_entry_id: number | null;
  matched_entry_doc_number: string | null;
  matched_by_name: string;
}

const STATUS_META: Record<
  LineRow["status"],
  { label: string; color: string; icon: typeof CheckCircle2 }
> = {
  matched: { label: "Matched", color: "var(--green)", icon: CheckCircle2 },
  review: { label: "Needs review", color: "var(--rust)", icon: AlertTriangle },
  unmatched: { label: "Unmatched", color: "var(--red)", icon: XCircle },
  ignored: { label: "Ignored", color: "var(--muted)", icon: HelpCircle },
};

const TABS: { key: "" | LineRow["status"]; label: string }[] = [
  { key: "", label: "All" },
  { key: "review", label: "Needs review" },
  { key: "unmatched", label: "Unmatched" },
  { key: "matched", label: "Matched" },
  { key: "ignored", label: "Ignored" },
];

export function BankReconciliationPage() {
  const { data: imports, reload: reloadImports } = useDoc<{ rows: ImportRow[] }>(
    "/finledger/bank/imports",
  );
  const [selected, setSelected] = useState<number | null>(null);
  const [tab, setTab] = useState<"" | LineRow["status"]>("review");
  const linesUrl = selected
    ? `/finledger/bank/imports/${selected}/lines${tab ? `?status=${tab}` : ""}`
    : null;
  const { data: lines, reload: reloadLines } = useDoc<{ rows: LineRow[] }>(linesUrl);

  const [file, setFile] = useState<File | null>(null);
  const [bankLabel, setBankLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function upload() {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      if (bankLabel) form.append("bank_label", bankLabel);
      const { data } = await api.post("/finledger/bank/imports", form);
      setFile(null);
      setBankLabel("");
      reloadImports();
      setSelected(data.id);
      setTab("review");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function act(lineId: number, body: Record<string, unknown>) {
    setBusy(true);
    try {
      await api.post(`/finledger/bank/lines/${lineId}/match`, body);
      reloadLines();
      reloadImports();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <OperationsPage data-testid="bank-reconciliation-page">
      <PageHeader lead="Upload a bank statement (CSV or Excel) and it's matched against the books automatically — what's left is what needs a look." />

      <div className="warn-note" style={{ marginBottom: 18 }} data-testid="bank-api-roadmap-note">
        Using file upload for now — direct bank API sync is on the roadmap once your bank provides
        API access. When that's available it will feed the same matching engine below, so this
        screen won't need to change.
      </div>

      <div className="card section-card" style={{ marginBottom: 24 }}>
        <div className="form-grid">
          <input
            type="file"
            accept=".csv,.xlsx,.xls"
            className="input"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            data-testid="bank-upload-file-input"
          />
          <input
            className="input"
            placeholder="Bank / account label (optional)"
            value={bankLabel}
            onChange={(e) => setBankLabel(e.target.value)}
            data-testid="bank-upload-label-input"
          />
          <button
            className="btn btn-cta"
            disabled={!file || busy}
            onClick={upload}
            data-testid="bank-upload-submit"
          >
            <Upload size={15} /> Upload &amp; match
          </button>
        </div>
        {error && (
          <div className="warn-note" style={{ marginTop: 10 }} data-testid="bank-upload-error">
            {error}
          </div>
        )}
      </div>

      <div className="table-wrap" style={{ marginBottom: 24 }}>
        <table className="data" data-testid="bank-imports-table">
          <thead>
            <tr>
              <th>File</th>
              <th>Bank</th>
              <th>Uploaded by</th>
              <th className="num">Rows</th>
              <th className="num">Matched</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(imports?.rows ?? []).map((b) => (
              <tr key={b.id} data-testid={`bank-import-row-${b.id}`}>
                <td className="mono">{b.filename}</td>
                <td>{b.bank_label || "—"}</td>
                <td>{b.uploaded_by_name || "—"}</td>
                <td className="num">{b.row_count}</td>
                <td className="num">
                  {b.matched_count}/{b.row_count}
                </td>
                <td>
                  <button
                    className="btn btn-sm"
                    onClick={() => setSelected(b.id)}
                    data-testid={`bank-import-view-${b.id}`}
                  >
                    {selected === b.id ? "Viewing" : "View lines"}
                  </button>
                </td>
              </tr>
            ))}
            {(imports?.rows ?? []).length === 0 && (
              <tr>
                <td colSpan={6} className="muted-cell" data-testid="bank-imports-empty">
                  No statements uploaded yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && (
        <>
          <div className="toolbar" style={{ marginBottom: 14, gap: 6 }}>
            {TABS.map((t) => (
              <button
                key={t.key || "all"}
                className={`btn btn-sm ${tab === t.key ? "btn-cta" : ""}`}
                onClick={() => setTab(t.key)}
                data-testid={`bank-tab-${t.key || "all"}`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="table-wrap">
            <table className="data" data-testid="bank-lines-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Narration</th>
                  <th className="num">Amount ₹</th>
                  <th>Status</th>
                  <th>Match</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(lines?.rows ?? []).map((l) => {
                  const meta = STATUS_META[l.status];
                  const Icon = meta.icon;
                  const signed = l.credit_paise - l.debit_paise;
                  return (
                    <tr key={l.id} data-testid={`bank-line-row-${l.id}`}>
                      <td className="mono">{new Date(l.txn_date).toLocaleDateString("en-IN")}</td>
                      <td>{l.narration}</td>
                      <td
                        className="num mono"
                        style={{ color: signed >= 0 ? "var(--green)" : "var(--red)" }}
                      >
                        <Money paise={signed} />
                      </td>
                      <td>
                        <span
                          style={{
                            color: meta.color,
                            display: "inline-flex",
                            alignItems: "center",
                            gap: 4,
                          }}
                        >
                          <Icon size={13} /> {meta.label}
                        </span>
                      </td>
                      <td>
                        {l.status === "matched" && l.matched_entry_doc_number && (
                          <span className="mono">{l.matched_entry_doc_number}</span>
                        )}
                        {(l.status === "review" || l.status === "unmatched") &&
                          l.candidates.length > 0 && (
                            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                              {l.candidates.map((c) => (
                                <div
                                  key={c.entry_id}
                                  style={{
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 6,
                                    fontSize: 12,
                                  }}
                                >
                                  <button
                                    className="btn btn-sm"
                                    disabled={busy}
                                    onClick={() =>
                                      act(l.id, { action: "link", entry_id: c.entry_id })
                                    }
                                    data-testid={`bank-line-${l.id}-link-${c.entry_id}`}
                                  >
                                    <Link2 size={11} /> {c.doc_number}
                                  </button>
                                  <span className="muted-cell">
                                    {c.description || "—"} · <Money paise={c.amount_paise} /> ·{" "}
                                    {c.score}% match
                                  </span>
                                </div>
                              ))}
                            </div>
                          )}
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 6 }}>
                          {l.status === "matched" && (
                            <button
                              className="btn btn-sm"
                              disabled={busy}
                              onClick={() => act(l.id, { action: "unlink" })}
                              data-testid={`bank-line-${l.id}-unlink`}
                            >
                              Unlink
                            </button>
                          )}
                          {l.status !== "ignored" && l.status !== "matched" && (
                            <button
                              className="btn btn-sm"
                              disabled={busy}
                              onClick={() => act(l.id, { action: "ignore" })}
                              data-testid={`bank-line-${l.id}-ignore`}
                            >
                              Ignore
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {(lines?.rows ?? []).length === 0 && (
                  <tr>
                    <td colSpan={6} className="muted-cell" data-testid="bank-lines-empty">
                      Nothing in this list.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </OperationsPage>
  );
}

export default BankReconciliationPage;
