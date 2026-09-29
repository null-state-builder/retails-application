import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  STATUS_CHIP,
  STATUS_LABEL,
  emptySettle,
  monthLabel,
  nextStep,
  raisable,
  settleBody,
  type BrandClaimDetail,
  type BrandClaimList,
  type SettleDraft,
} from "../lib/brandClaims";
import { commandIdFor, type Pending } from "../lib/debitNotes";
import { Money, formatDateTime } from "../lib/format";

const PAGE_API = "/goods-v1/sell/brand-claims";
const PAGE_PATH = "/brands/claims";
const OFFLINE =
  "Brand claims need a connection. Nothing can be raised, accepted or settled until it is back; what you typed stays here.";

/** Today in the browser's own calendar, as the date input writes it. */
function today(): string {
  return new Date().toLocaleDateString("en-CA");
}

/** Brands > Claims (store operations PRD ST-BRD-3, ST-BRD-6; ticket 26).
 *
 *  Once a month has ended, Accounts raises a claim on each brand for the
 *  brand-funded share of that month's discounts at each store (ticket 25's
 *  split). Accounts records the brand accepting it, then the brand's commercial
 *  credit note that settles it - in full, or short with the difference kept.
 *  A commercial credit note carries no GST and changes no bill. A brand with a
 *  promotion-services agreement has every claim flagged for Accounts. The
 *  server decides who may do what; this page only offers what it allows. */
export function BrandClaimsPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [month, setMonth] = useState("");
  const [list, setList] = useState<BrandClaimList | null>(null);
  const [claim, setClaim] = useState<BrandClaimDetail | null>(null);
  const [acceptNote, setAcceptNote] = useState("");
  const [settle, setSettle] = useState<SettleDraft>({ number: "", date: today(), rupees: "", reason: "" });
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  /** The press whose answer was lost, if any (`commandIdFor`). */
  const pending = useRef<Pending | null>(null);
  const request = useRef(0);

  const shown = useRef<{ id: number; revision: number } | null>(null);
  /** Show `fresh`, keeping what was typed when it is the same claim at the same
   *  revision - a reload after a dropped connection never loses it. */
  const openClaim = useCallback((fresh: BrandClaimDetail | null) => {
    const same =
      fresh !== null &&
      shown.current !== null &&
      shown.current.id === fresh.id &&
      shown.current.revision === fresh.revision;
    shown.current = fresh ? { id: fresh.id, revision: fresh.revision } : null;
    setClaim(fresh);
    if (same || !fresh) return;
    setAcceptNote("");
    setSettle(emptySettle(fresh, today()));
  }, []);

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const [l, c] = await Promise.all([
        api.get<BrandClaimList>(PAGE_API, { params: month ? { month } : {} }),
        id ? api.get<BrandClaimDetail>(`${PAGE_API}/${id}`) : Promise.resolve(null),
      ]);
      if (mine !== request.current) return;
      setLost(false);
      setList(l.data);
      // Opened from an alert or a link: show the claim's own month.
      if (!month) setMonth(c ? c.data.month : l.data.month);
      openClaim(c ? c.data : null);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, [id, month, openClaim]);

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

  /** Send one command. A dropped connection keeps what was typed and the
   *  command's identity for the same press (`commandIdFor`), so pressing again
   *  replays it; any answer from the server ends that command. */
  async function send(
    url: string,
    attempt: { noteId: number; revision: number; step: string },
    body: Record<string, unknown>,
    said: string,
    after: (data: unknown) => void,
  ) {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setBusy(true);
    setError("");
    setDone("");
    const full = { ...attempt, body: JSON.stringify(body) };
    const commandId = commandIdFor(pending.current, full, () => crypto.randomUUID());
    pending.current = { ...full, commandId };
    try {
      const revision = attempt.revision > 0 ? attempt.revision : undefined;
      const answer = await api.post(url, { ...goodsMeta(revision, commandId), ...body });
      pending.current = null;
      setDone(said);
      after(answer.data);
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
      } else {
        pending.current = null;
        setError(apiErrorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  function step(name: "accept" | "settle", body: Record<string, unknown>, said: string) {
    if (!claim) return;
    void send(
      `${PAGE_API}/${claim.id}/${name}`,
      { noteId: claim.id, revision: claim.revision, step: name },
      body,
      said,
      () => void load(),
    );
  }

  function raiseAll() {
    if (!list) return;
    void send(
      `${PAGE_API}/raise`,
      { noteId: 0, revision: 0, step: `raise:${list.month}` },
      { month: list.month },
      `Claims raised for ${monthLabel(list.month)}.`,
      () => void load(),
    );
  }

  const offline = !online || lost;
  const writable = !offline && !busy;
  const allowed = new Set(claim?.allowed_actions ?? []);
  const checked = claim ? settleBody(claim, settle) : null;
  const toRaise = list ? raisable(list.to_raise) : [];

  return (
    <div className="page-pad">
      <PageHeader
        title="Brand Claims"
        lead="At month end, the brand-funded share of each store's discounts becomes a claim on the brand, settled by the brand's commercial credit note."
      />
      {offline && (
        <p className="warn-note" data-testid="bc-offline" role="status">
          {OFFLINE}
          {online && (
            <>
              {" "}
              <button type="button" className="btn" data-testid="bc-retry" onClick={() => void load()}>
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="bc-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="bc-done">
          {done}
        </p>
      )}

      {claim && (
        <section className="card section-card" data-testid="bc-detail">
          <div className="toolbar">
            <h2 className="h3" data-testid="bc-reference">
              {claim.reference}
            </h2>
            <span className={`chip ${STATUS_CHIP[claim.status]}`} data-testid="bc-status">
              {STATUS_LABEL[claim.status]}
            </span>
            <Link className="btn" to={PAGE_PATH} data-testid="bc-close">
              Back to the list
            </Link>
          </div>
          <p data-testid="bc-next">{nextStep(claim)}</p>
          {claim.promotion_flag && (
            <p className="warn-note" data-testid="bc-flag">
              {claim.brand.name} has a promotion-services agreement. Money paid under it may be payment
              for a service, not a discount. Accounts to check before treating it as a discount claim.
            </p>
          )}
          {!claim.switched_on && (claim.status === "raised" || claim.status === "accepted") && (
            <p className="warn-note" data-testid="bc-switched-off">
              Brand discount claims are switched off at {claim.store.name}. The claim stays here, but it
              cannot be moved on until Admin switches them on again in Setup, Feature Switches.
            </p>
          )}
          <dl className="facts" data-testid="bc-facts">
            <dt>Brand</dt>
            <dd>{claim.brand.name}</dd>
            <dt>Store</dt>
            <dd>
              {claim.store.code} · {claim.store.name}
            </dd>
            <dt>Month</dt>
            <dd>
              {monthLabel(claim.month)}
              {claim.sequence > 1 ? ` (claim ${claim.sequence}: bills that arrived after the first)` : ""}
            </dd>
            <dt>Claimed</dt>
            <dd data-testid="bc-amount">
              <Money paise={Number(claim.amount_paise)} /> on {claim.pieces} piece(s)
            </dd>
            {Number(claim.unknown_paise) !== 0 && (
              <>
                <dt>Not claimed</dt>
                <dd data-testid="bc-unknown">
                  <Money paise={Number(claim.unknown_paise)} /> of this brand's offer discount has an
                  unknown split (no approved terms or share), so it is not claimed.
                </dd>
              </>
            )}
            <dt>Raised</dt>
            <dd>
              {formatDateTime(claim.raised_at)} by {claim.raised_by}
            </dd>
            {claim.accepted_on && (
              <>
                <dt>Accepted</dt>
                <dd data-testid="bc-accepted">
                  {claim.accepted_on} ({claim.accepted_by}){claim.accepted_note ? `: ${claim.accepted_note}` : ""}
                </dd>
              </>
            )}
            {claim.credit_note_number && (
              <>
                <dt>Credit note</dt>
                <dd data-testid="bc-credit-note">
                  {claim.credit_note_number} of {claim.credit_note_date} for{" "}
                  <Money paise={Number(claim.settled_paise ?? 0)} /> (commercial, no GST)
                </dd>
              </>
            )}
            {claim.status === "settled_short" && (
              <>
                <dt>Difference kept</dt>
                <dd data-testid="bc-difference">
                  <Money paise={Number(claim.difference_paise ?? 0)} />: {claim.difference_reason}
                </dd>
              </>
            )}
          </dl>
          <p className="muted-cell">
            Settled through the brand's commercial credit note: no GST effect on our bills. Not posted to
            the accounts.
          </p>

          {allowed.has("accept") && (
            <div className="toolbar">
              <input
                className="input"
                maxLength={240}
                placeholder="How the brand agreed (optional)"
                aria-label="How the brand agreed"
                data-testid="bc-accept-note"
                disabled={!writable}
                value={acceptNote}
                onChange={(e) => setAcceptNote(e.target.value)}
              />
              <button
                type="button"
                className="btn btn-primary"
                data-testid="bc-accept"
                disabled={!writable}
                onClick={() =>
                  step(
                    "accept",
                    acceptNote.trim() ? { note: acceptNote.trim() } : {},
                    "Recorded: the brand accepted the claim.",
                  )
                }
              >
                The brand accepted it
              </button>
            </div>
          )}

          {allowed.has("settle") && checked && (
            <div className="toolbar" data-testid="bc-settle-form">
              <label className="field">
                <span>Credit note number</span>
                <input
                  className="input"
                  maxLength={60}
                  data-testid="bc-cn-number"
                  disabled={!writable}
                  value={settle.number}
                  onChange={(e) => setSettle({ ...settle, number: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Credit note date</span>
                <input
                  className="input"
                  type="date"
                  data-testid="bc-cn-date"
                  disabled={!writable}
                  value={settle.date}
                  onChange={(e) => setSettle({ ...settle, date: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Amount (Rs)</span>
                <input
                  className="input"
                  inputMode="decimal"
                  data-testid="bc-cn-amount"
                  disabled={!writable}
                  value={settle.rupees}
                  onChange={(e) => setSettle({ ...settle, rupees: e.target.value })}
                />
              </label>
              {checked.short && (
                <label className="field">
                  <span>Why the brand paid less</span>
                  <input
                    className="input"
                    maxLength={240}
                    data-testid="bc-cn-reason"
                    disabled={!writable}
                    value={settle.reason}
                    onChange={(e) => setSettle({ ...settle, reason: e.target.value })}
                  />
                </label>
              )}
              {!checked.ok && (settle.number || settle.reason) && (
                <p className="warn-note" data-testid="bc-settle-problem">
                  {checked.problem}
                </p>
              )}
              <div className="toolbar">
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="bc-settle"
                  disabled={!writable || !checked.ok}
                  onClick={() => {
                    if (checked.ok)
                      step(
                        "settle",
                        checked.body,
                        checked.short ? "Settled short. The difference is kept." : "Settled.",
                      );
                  }}
                >
                  {checked.short ? "Settle short" : "Settle the claim"}
                </button>
              </div>
            </div>
          )}

          <div className="table-wrap">
            <table className="data" data-testid="bc-lines">
              <thead>
                <tr>
                  <th>Bill</th>
                  <th>Billed</th>
                  <th className="num">Pieces</th>
                  <th className="num">Brand's share</th>
                </tr>
              </thead>
              <tbody>
                {claim.lines.map((line) => (
                  <tr key={`${line.line_id}-${line.brand_paise}-${line.bill_id}`}>
                    <td>
                      {line.bill_number || `Bill ${line.bill_id}`}
                      {line.direction === "return" && <span className="chip"> Given back</span>}
                    </td>
                    <td>{line.billed_at ? formatDateTime(line.billed_at) : "-"}</td>
                    <td className="num">{line.pieces}</td>
                    <td className="num">
                      <Money paise={Number(line.brand_paise)} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="card section-card">
        <div className="toolbar">
          <h2 className="h3">{list ? monthLabel(list.month) : "Month"}</h2>
          <label className="field">
            <span>Month</span>
            <input
              className="input"
              type="month"
              data-testid="bc-month"
              value={month}
              onChange={(e) => e.target.value && setMonth(e.target.value)}
            />
          </label>
        </div>
        {!list ? (
          <p className="muted-cell">{offline ? "" : "Loading…"}</p>
        ) : (
          <>
            {!list.switched_on && (
              <p className="warn-note" data-testid="bc-off">
                Brand discount claims are switched off at every store you work at. Claims already raised
                stay here to read.
              </p>
            )}
            {!list.month_ended && (
              <p className="muted-cell" data-testid="bc-not-ended">
                {monthLabel(list.month)} has not ended yet. Its claims are raised once it has.
              </p>
            )}
            <h3 className="h3">Still to claim</h3>
            {list.to_raise.length === 0 ? (
              <p className="muted-cell" data-testid="bc-nothing">
                No brand-funded discount in this month at your stores.
              </p>
            ) : (
              <div className="table-wrap">
                <table className="data" data-testid="bc-to-raise">
                  <thead>
                    <tr>
                      <th>Store</th>
                      <th>Brand</th>
                      <th className="num">Owed, not claimed yet</th>
                      <th className="num">Unknown split</th>
                      <th className="num">Claims raised</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.to_raise.map((row) => (
                      <tr
                        key={`${row.store.id}-${row.brand_id ?? row.brand_name}`}
                        data-testid={`bc-to-raise-${row.store.code}-${row.brand_id ?? "none"}`}
                      >
                        <td>{row.store.code}</td>
                        <td>
                          {row.brand_name}
                          {row.brand_id === null && <div className="muted-cell">Not one brand in the list</div>}
                        </td>
                        <td className="num">
                          <Money paise={Number(row.amount_paise)} />
                        </td>
                        <td className="num">
                          {Number(row.unknown_paise) === 0 ? "-" : <Money paise={Number(row.unknown_paise)} />}
                        </td>
                        <td className="num">{row.raised}</td>
                        <td>
                          {row.promotion_flag && <span className="chip chip-amber">Promotion services</span>}
                          {!row.switched_on && <span className="chip">Switched off</span>}
                          {row.brand_id !== null && Number(row.amount_paise) < 0 && (
                            <div className="muted-cell">More given back than sold: taken off the next claim</div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {list.can_edit && list.month_ended && (
              <div className="toolbar">
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="bc-raise"
                  disabled={!writable || toRaise.length === 0}
                  title={toRaise.length === 0 ? "Nothing is left to claim for this month." : undefined}
                  onClick={raiseAll}
                >
                  Raise {toRaise.length} claim(s) for {monthLabel(list.month)}
                </button>
              </div>
            )}

            <h3 className="h3">Claims</h3>
            {list.claims.length === 0 ? (
              <p className="muted-cell" data-testid="bc-empty">
                No claims raised for this month.
              </p>
            ) : (
              <div className="table-wrap">
                <table className="data" data-testid="bc-list">
                  <thead>
                    <tr>
                      <th>Claim</th>
                      <th>Brand</th>
                      <th>Store</th>
                      <th className="num">Claimed</th>
                      <th>Status</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.claims.map((row) => (
                      <tr key={row.id} data-testid={`bc-row-${row.id}`}>
                        <td>{row.reference}</td>
                        <td>
                          {row.brand.name}
                          {row.promotion_flag && (
                            <div>
                              <span className="chip chip-amber">Promotion services: check</span>
                            </div>
                          )}
                        </td>
                        <td>{row.store.code}</td>
                        <td className="num">
                          <Money paise={Number(row.amount_paise)} />
                        </td>
                        <td>
                          <span className={`chip ${STATUS_CHIP[row.status]}`}>{STATUS_LABEL[row.status]}</span>
                        </td>
                        <td>
                          <button
                            type="button"
                            className="btn"
                            data-testid={`bc-open-${row.id}`}
                            onClick={() => {
                              setError("");
                              setDone("");
                              navigate(`${PAGE_PATH}/${row.id}`);
                            }}
                          >
                            Open
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
