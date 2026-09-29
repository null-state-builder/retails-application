import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { emptyTermsDraft, figure, modelLabel, statusChip, termsRequest } from "../lib/brandTerms";
import type { CommercialModel, TermsDraft } from "../lib/brandTerms";
import { formatDateTime } from "../lib/format";

type Summary = ApiRead<ApiSchemas["BrandTermsSummary"]>;
type Detail = ApiRead<ApiSchemas["BrandTermsDetail"]>;
type Proposal = Summary["waiting"][number];

const PAGE_API = "/goods-v1/masters/brand-terms";
const OFFLINE =
  "Brand terms need a connection. Nothing can be proposed or approved until it is back; what you typed stays here.";

interface PromotionDraft {
  agreement: "" | "yes" | "no";
  applies_from: string;
  note: string;
}

/** A command's identity, kept across retries: if the connection drops after the
 *  server saved it, pressing Save again replays the same command instead of
 *  proposing twice. A fresh one is taken once the server has answered - saved
 *  or refused - because a corrected form is a new command. */
function useCommandId(): [string, () => void] {
  const [id, setId] = useState(() => crypto.randomUUID());
  return [id, () => setId(crypto.randomUUID())];
}

/** Brands > Terms (store operations PRD ST-BRD-1, ST-BRD-6; ticket 23).
 *
 *  Every brand in reach, its commercial model for each season still selling (or
 *  Unknown - nothing is assumed, D9), its promotion-services agreement, and the
 *  list of brands whose model is unknown, for Anand to fill in. The Brand
 *  Manager proposes a change; the Owner, a different person, approves it; a
 *  change applies from its date and never alters a result already worked out.
 *  The server decides who may do what; this page only offers what it allows. */
export function BrandTermsPage() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [brandId, setBrandId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [terms, setTerms] = useState<TermsDraft | null>(null);
  const [promotion, setPromotion] = useState<PromotionDraft | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [termsCommand, nextTermsCommand] = useCommandId();
  const [promotionCommand, nextPromotionCommand] = useCommandId();
  const request = useRef(0);

  const load = useCallback(async (brand: number | null) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const [s, d] = await Promise.all([
        api.get<Summary>(PAGE_API),
        brand === null ? Promise.resolve(null) : api.get<Detail>(`${PAGE_API}/${brand}`),
      ]);
      if (mine !== request.current) return;
      setLost(false);
      setSummary(s.data);
      setDetail(d ? d.data : null);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load(brandId);
  }, [brandId, load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(brandId);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [brandId, load]);

  function open(id: number) {
    setError("");
    setDone("");
    setTerms(null);
    setPromotion(null);
    setBrandId(id);
  }

  /** Send one write. A dropped connection keeps the form as typed and the
   *  command's identity; any answer from the server ends that command. */
  async function send(
    path: string,
    body: Record<string, unknown>,
    answered: () => void = () => {},
  ): Promise<boolean> {
    if (!navigator.onLine) {
      setOnline(false);
      return false;
    }
    setBusy(true);
    setError("");
    setDone("");
    try {
      await api.post(path, body);
      answered();
      return true;
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
      } else {
        answered();
        setError(apiErrorMessage(reason));
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function proposeTerms() {
    if (!detail || !terms) return;
    const revision =
      detail.terms_revisions.find((r) => String(r.season_id) === terms.season_id)
        ?.expected_revision ?? 1;
    const ok = await send(
      `${PAGE_API}/versions`,
      { ...goodsMeta(revision, termsCommand), ...termsRequest(detail.brand.id, terms) },
      nextTermsCommand,
    );
    if (!ok) return;
    setTerms(null);
    setDone("Sent to the Owner for approval. It counts only once approved.");
    await load(brandId);
  }

  async function proposePromotion() {
    if (!detail || !promotion) return;
    const ok = await send(
      `${PAGE_API}/promotion`,
      {
        ...goodsMeta(detail.promotion_revision, promotionCommand),
        brand_id: detail.brand.id,
        applies_from: promotion.applies_from,
        agreement: promotion.agreement === "" ? null : promotion.agreement === "yes",
        note: promotion.note.trim(),
      },
      nextPromotionCommand,
    );
    if (!ok) return;
    setPromotion(null);
    setDone("Sent to the Owner for approval. It counts only once approved.");
    await load(brandId);
  }

  async function decide(proposal: Proposal, outcome: "approved" | "rejected" | "withdrawn") {
    const ok = await send(`${PAGE_API}/decisions`, {
      ...goodsMeta(),
      kind: proposal.kind,
      id: proposal.id,
      outcome,
      note: (reasons[proposal.id] ?? "").trim(),
    });
    if (!ok) return;
    setDone(
      outcome === "approved"
        ? `Approved. ${proposal.brand_name}'s change applies from ${proposal.applies_from}.`
        : outcome === "rejected"
          ? "Rejected. Nothing changed."
          : "Withdrawn. Nothing changed.",
    );
    await load(brandId);
  }

  const offline = !online || lost;
  const notes = (
    <>
      {error && (
        <p className="warn-note" data-testid="brand-terms-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="brand-terms-done">
          {done}
        </p>
      )}
    </>
  );
  const writable = !offline && !busy;
  const offlineNote = offline && (
    <p className="warn-note" data-testid="brand-terms-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="brand-terms-retry"
            onClick={() => void load(brandId)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );

  if (!summary) {
    return (
      <div className="page-pad">
        <PageHeader title="Brand Terms" />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="brand-terms-error">
            {error}
          </p>
        ) : (
          !offline && <p>Loading brand terms…</p>
        )}
      </div>
    );
  }

  const currentSeasons = summary.seasons.filter((s) => s.current);

  return (
    <div className="page-pad">
      <PageHeader
        title="Brand Terms"
        lead="Each brand's commercial terms per season, from a date. The Brand Manager proposes a change and the Owner approves it."
      />
      {offlineNote}
      {!summary.switched_on && (
        <p className="warn-note" data-testid="brand-terms-switched-off">
          Brand terms are switched off at your stores. You can read them, but no change can be
          proposed or approved.
        </p>
      )}
      {!summary.can_propose && !summary.can_approve && (
        <p className="muted-cell" data-testid="brand-terms-read-only">
          The Brand Manager proposes brand terms and the Owner approves them. You can read them.
        </p>
      )}
      {!detail && notes}

      {summary.waiting.length > 0 && (
        <section className="card section-card" data-testid="brand-terms-waiting">
          <h2 className="h3">Waiting for the Owner</h2>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Brand</th>
                  <th>Change</th>
                  <th>From</th>
                  <th>Proposed</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {summary.waiting.map((p) => (
                  <tr key={p.id} data-testid={`brand-waiting-${p.brand_code}`}>
                    <td>
                      {p.brand_name}
                      {p.season_code ? ` · ${p.season_code}` : ""}
                    </td>
                    <td>
                      {p.summary}
                      <div className="muted-cell">{p.note}</div>
                    </td>
                    <td>{p.applies_from}</td>
                    <td>
                      {p.proposed_by || "Someone"}, {formatDateTime(p.proposed_at)}
                    </td>
                    <td>
                      {p.mine ? (
                        <button
                          type="button"
                          className="btn"
                          data-testid={`brand-withdraw-${p.brand_code}`}
                          disabled={!writable}
                          onClick={() => void decide(p, "withdrawn")}
                        >
                          Withdraw
                        </button>
                      ) : summary.can_approve ? (
                        <div className="toolbar">
                          <button
                            type="button"
                            className="btn btn-primary"
                            data-testid={`brand-approve-${p.brand_code}`}
                            disabled={!writable || !summary.switched_on}
                            onClick={() => void decide(p, "approved")}
                          >
                            Approve
                          </button>
                          <input
                            className="input"
                            aria-label="Reason to reject"
                            placeholder="Reason to reject"
                            data-testid={`brand-reject-reason-${p.brand_code}`}
                            value={reasons[p.id] ?? ""}
                            onChange={(e) => setReasons({ ...reasons, [p.id]: e.target.value })}
                          />
                          <button
                            type="button"
                            className="btn"
                            data-testid={`brand-reject-${p.brand_code}`}
                            disabled={!writable}
                            onClick={() => void decide(p, "rejected")}
                          >
                            Reject
                          </button>
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="card section-card" data-testid="brand-terms-unknown">
        <h2 className="h3">Brands with an unknown model</h2>
        {summary.unknown.length === 0 ? (
          <p className="muted-cell">
            Every brand has an approved model for each season selling now.
          </p>
        ) : (
          <>
            <p className="muted-cell">
              Nothing is assumed for these brands. Work that needs the model treats them as unknown
              and says so. Fill them in before P1 goes live.
            </p>
            <ul>
              {summary.unknown.map((b) => (
                <li key={b.id} data-testid={`brand-unknown-${b.code}`}>
                  <button type="button" className="link-btn" onClick={() => open(b.id)}>
                    {b.name}
                  </button>
                  {b.seasons.length > 0 ? ` - no model for ${b.seasons.join(", ")}` : " - no terms"}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="card section-card">
        <h2 className="h3">Brands</h2>
        <div className="table-wrap">
          <table className="data" data-testid="brand-terms-table">
            <thead>
              <tr>
                <th>Brand</th>
                {currentSeasons.map((s) => (
                  <th key={s.id}>{s.code}</th>
                ))}
                <th>Promotion-services agreement</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {summary.brands.map((b) => (
                <tr key={b.id} data-testid={`brand-row-${b.code}`}>
                  <td>
                    {b.name}
                    {!b.is_active && <span className="muted-cell"> (retired)</span>}
                  </td>
                  {b.seasons.map((s) => (
                    <td key={s.season_id} data-testid={`brand-model-${b.code}-${s.season_code}`}>
                      {s.model ? modelLabel(s.model) : <span className="chip">Unknown</span>}
                      {s.waiting && <span className="chip chip-amber">Change waiting</span>}
                    </td>
                  ))}
                  <td data-testid={`brand-promotion-${b.code}`}>
                    {b.promotion_agreement ? "Yes" : "No"}
                    {b.promotion_waiting && <span className="chip chip-amber">Change waiting</span>}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      data-testid={`brand-open-${b.code}`}
                      onClick={() => open(b.id)}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {detail && (
        <BrandPanel
          notes={notes}
          detail={detail}
          terms={terms}
          promotion={promotion}
          writable={writable}
          onTerms={setTerms}
          onPromotion={setPromotion}
          onProposeTerms={() => void proposeTerms()}
          onProposePromotion={() => void proposePromotion()}
        />
      )}
    </div>
  );
}

function BrandPanel({
  notes,
  detail,
  terms,
  promotion,
  writable,
  onTerms,
  onPromotion,
  onProposeTerms,
  onProposePromotion,
}: {
  /** What the last action said, drawn where the person is working. */
  notes: ReactNode;
  detail: Detail;
  terms: TermsDraft | null;
  promotion: PromotionDraft | null;
  writable: boolean;
  onTerms: (draft: TermsDraft | null) => void;
  onPromotion: (draft: PromotionDraft | null) => void;
  onProposeTerms: () => void;
  onProposePromotion: () => void;
}) {
  const canPropose = detail.can_propose && detail.switched_on;
  const firstCurrent = detail.seasons.find((s) => s.current) ?? detail.seasons[0];
  return (
    <section className="card section-card" data-testid="brand-terms-detail">
      <h2 className="h3">{detail.brand.name}</h2>
      {notes}
      <p className="muted-cell" data-testid="brand-setup-label">
        Setup, Brands calls it {detail.brand.setup_label}. That label has a default, so it is not
        taken as the brand's terms.
      </p>

      {canPropose && !terms && (
        <p>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="brand-propose-terms"
            onClick={() => onTerms(emptyTermsDraft(String(firstCurrent?.id ?? ""), detail.today))}
          >
            Propose terms
          </button>
        </p>
      )}
      {terms && (
        <div className="card section-card" data-testid="brand-terms-form">
          <div className="toolbar">
            <label className="field">
              <span>Season</span>
              <select
                className="select"
                data-testid="terms-season"
                value={terms.season_id}
                onChange={(e) => onTerms({ ...terms, season_id: e.target.value })}
              >
                {detail.seasons.map((s) => (
                  <option key={s.id} value={String(s.id)}>
                    {s.code} ({s.name})
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Applies from</span>
              <input
                className="input"
                type="date"
                data-testid="terms-applies-from"
                min={detail.today}
                value={terms.applies_from}
                onChange={(e) => onTerms({ ...terms, applies_from: e.target.value })}
              />
            </label>
            <label className="field">
              <span>Commercial model</span>
              <select
                className="select"
                data-testid="terms-model"
                value={terms.model}
                onChange={(e) =>
                  onTerms({ ...terms, model: e.target.value as CommercialModel | "" })
                }
              >
                <option value="">Pick one</option>
                {detail.models.map((m) => (
                  <option key={m} value={m}>
                    {modelLabel(m)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="toolbar">
            {(
              [
                ["margin_percent", "Margin (%)", "terms-margin"],
                ["return_allowance_percent", "Return allowance (%)", "terms-return-allowance"],
                ["discount_funding_percent", "Brand's share of discounts (%)", "terms-funding"],
                ["payment_days", "Payment days", "terms-payment-days"],
              ] as const
            ).map(([key, label, testId]) => (
              <label className="field" key={key}>
                <span>{label}</span>
                <input
                  className="input"
                  inputMode="decimal"
                  placeholder="Unknown"
                  data-testid={testId}
                  value={terms[key]}
                  onChange={(e) => onTerms({ ...terms, [key]: e.target.value })}
                />
              </label>
            ))}
          </div>
          <p className="muted-cell">
            Leave a figure blank if it is not known. It stays unknown, never 0.
          </p>
          <label className="field">
            <span>Why</span>
            <input
              className="input"
              data-testid="terms-note"
              value={terms.note}
              onChange={(e) => onTerms({ ...terms, note: e.target.value })}
            />
          </label>
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="terms-save"
              disabled={!writable}
              onClick={onProposeTerms}
            >
              Send to the Owner
            </button>
            <button type="button" className="btn" onClick={() => onTerms(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="table-wrap">
        <table className="data" data-testid="brand-terms-history">
          <thead>
            <tr>
              <th>Season</th>
              <th>Version</th>
              <th>From</th>
              <th>Model</th>
              <th className="num">Margin</th>
              <th className="num">Return allowance</th>
              <th className="num">Brand funds</th>
              <th className="num">Payment days</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {detail.terms.length === 0 && (
              <tr>
                <td colSpan={9} className="muted-cell">
                  No terms recorded. The model is unknown for every season.
                </td>
              </tr>
            )}
            {detail.terms.map((row) => {
              const status = statusChip(row.status);
              return (
                <tr key={row.id} data-testid={`terms-version-${row.season_code}-${row.version}`}>
                  <td>{row.season_code}</td>
                  <td>{row.version}</td>
                  <td>{row.applies_from}</td>
                  <td>{modelLabel(row.model)}</td>
                  <td className="num">{figure(row.margin_percent, "%")}</td>
                  <td className="num">{figure(row.return_allowance_percent, "%")}</td>
                  <td className="num">{figure(row.discount_funding_percent, "%")}</td>
                  <td className="num">{figure(row.payment_days, "")}</td>
                  <td>
                    <span className={`chip ${status.chip}`}>{status.label}</span>
                    {row.in_force_today && <span className="chip chip-blue">In force today</span>}
                    <div className="muted-cell">
                      Proposed by {row.proposed_by || "someone"}: {row.note}
                      {row.decided_by && `. ${status.label} by ${row.decided_by}`}
                      {row.decision_note && `: ${row.decision_note}`}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <h3 className="h3">Promotion-services agreement</h3>
      <p className="muted-cell">
        Default No. If Yes, every discount claim for this brand is flagged for Accounts, because
        money paid under such an agreement may be payment for a service, not a discount.
      </p>
      {canPropose && !promotion && (
        <p>
          <button
            type="button"
            className="btn"
            data-testid="brand-propose-promotion"
            onClick={() => onPromotion({ agreement: "", applies_from: detail.today, note: "" })}
          >
            Propose a change
          </button>
        </p>
      )}
      {promotion && (
        <div className="toolbar" data-testid="brand-promotion-form">
          <label className="field">
            <span>Agreement</span>
            <select
              className="select"
              data-testid="promotion-agreement"
              value={promotion.agreement}
              onChange={(e) =>
                onPromotion({
                  ...promotion,
                  agreement: e.target.value as PromotionDraft["agreement"],
                })
              }
            >
              <option value="">Pick one</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </label>
          <label className="field">
            <span>Applies from</span>
            <input
              className="input"
              type="date"
              data-testid="promotion-applies-from"
              min={detail.today}
              value={promotion.applies_from}
              onChange={(e) => onPromotion({ ...promotion, applies_from: e.target.value })}
            />
          </label>
          <label className="field">
            <span>Why</span>
            <input
              className="input"
              data-testid="promotion-note"
              value={promotion.note}
              onChange={(e) => onPromotion({ ...promotion, note: e.target.value })}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="promotion-save"
            disabled={!writable}
            onClick={onProposePromotion}
          >
            Send to the Owner
          </button>
          <button type="button" className="btn" onClick={() => onPromotion(null)}>
            Cancel
          </button>
        </div>
      )}
      <ul data-testid="brand-promotion-history">
        {detail.promotion.length === 0 && <li className="muted-cell">Nothing recorded: No.</li>}
        {detail.promotion.map((row) => {
          const status = statusChip(row.status);
          return (
            <li key={row.id}>
              From {row.applies_from}: {row.agreement ? "Yes" : "No"}{" "}
              <span className={`chip ${status.chip}`}>{status.label}</span>
              {row.in_force_today && <span className="chip chip-blue">In force today</span>} -{" "}
              {row.note}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
