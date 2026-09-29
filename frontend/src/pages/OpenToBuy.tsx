import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { Money, formatDateTime, paiseToRupees } from "../lib/format";
import { masterLabel, PickerField, usePagedPicker } from "../lib/goodsScreen";
import {
  STAGE_CHIP,
  STAGE_LABEL,
  budgetKey,
  budgetPaise,
  commandIdFor,
  isOver,
  scopeName,
  type OtbAsk,
  type OtbBudget,
  type OtbList,
  type Pending,
} from "../lib/openToBuy";
import "./OpenToBuy.css";

const PAGE_API = "/goods-v1/open-to-buy";
const PAGE_PATH = "/booking/open-to-buy";
const OFFLINE =
  "Open-to-buy needs a connection. Nothing can be saved until it is back; what you typed stays here.";

interface Form {
  brand_id: string;
  season_id: string;
  site_id: string;
  rupees: string;
}
const EMPTY: Form = { brand_id: "", season_id: "", site_id: "", rupees: "" };

/** Booking > Open-to-Buy (store operations PRD ST-BUY-1; ticket 39).
 *
 *  A buying budget at cost per brand, season and site, or for the whole company.
 *  Open-to-buy is the budget less open bookings less goods received; a booking
 *  over it waits for the Owner's approval in the approvals inbox before it can
 *  be confirmed. Everything here is at cost, so it opens only for a login that
 *  sees cost, and the server says who may set a budget. */
export function OpenToBuyPage() {
  const { id } = useParams();
  const [list, setList] = useState<OtbList | null>(null);
  const [ask, setAsk] = useState<OtbAsk | null>(null);
  const [form, setForm] = useState<Form>(EMPTY);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = useRef<Pending | null>(null);
  const request = useRef(0);

  const brands = usePagedPicker(
    "/goods-v1/masters/brands",
    list?.can_set ? {} : null,
    form.brand_id,
    (row) => masterLabel(row),
  );
  const seasons = usePagedPicker(
    "/goods-v1/masters/seasons",
    list?.can_set ? {} : null,
    form.season_id,
    (row) => masterLabel(row),
  );

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const [l, a] = await Promise.all([
        api.get<OtbList>(PAGE_API),
        id ? api.get<OtbAsk>(`${PAGE_API}/asks/${id}`) : Promise.resolve(null),
      ]);
      if (mine !== request.current) return;
      setLost(false);
      setList(l.data);
      setAsk(a ? a.data : null);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, [id]);

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

  /** The budget the form names, if one is set already. */
  const current = list?.budgets.find(
    (b) =>
      String(b.brand.id) === form.brand_id &&
      String(b.season.id) === form.season_id &&
      String(b.site?.id ?? "") === form.site_id,
  );

  function edit(budget: OtbBudget) {
    setForm({
      brand_id: String(budget.brand.id),
      season_id: String(budget.season.id),
      site_id: String(budget.site?.id ?? ""),
      rupees: paiseToRupees(Number(budget.budget_paise)),
    });
    setDone("");
    setError("");
  }

  async function save() {
    const paise = budgetPaise(form.rupees);
    if (!form.brand_id || !form.season_id) {
      setError("Choose a brand and a season.");
      return;
    }
    if (paise === null) {
      setError("Type the budget in rupees, like 250000 or 2,50,000.50.");
      return;
    }
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setBusy(true);
    setError("");
    setDone("");
    const attempt = {
      key: budgetKey(form.brand_id, form.season_id, form.site_id),
      revision: current?.revision ?? null,
      paise,
    };
    const commandId = commandIdFor(pending.current, attempt, () => crypto.randomUUID());
    pending.current = { ...attempt, commandId };
    try {
      const answer = await api.post<OtbBudget>(`${PAGE_API}/budgets`, {
        ...goodsMeta(current?.revision, commandId),
        brand_id: Number(form.brand_id),
        season_id: Number(form.season_id),
        site_id: form.site_id ? Number(form.site_id) : null,
        budget_paise: paise,
      });
      pending.current = null;
      setDone(`Saved. ${scopeName(answer.data)} has a budget of the amount typed.`);
      const fresh = await api.get<OtbList>(PAGE_API);
      setList(fresh.data);
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

  const offline = !online || lost;

  return (
    <div className="page-pad">
      <PageHeader
        title="Open-to-Buy"
        lead="Buying budgets at cost for each brand and season. Open-to-buy is the budget less open bookings and goods received. A booking over it needs the Owner's approval."
      />
      {offline && (
        <p className="warn-note" data-testid="otb-offline" role="status">
          {OFFLINE}
          {online && (
            <>
              {" "}
              <button
                type="button"
                className="btn"
                data-testid="otb-retry"
                onClick={() => void load()}
              >
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="otb-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="otb-done">
          {done}
        </p>
      )}

      {ask && (
        <section className="card section-card" data-testid="otb-ask">
          <div className="toolbar">
            <h2 className="h3">Booking over open-to-buy</h2>
            <span className={`chip ${STAGE_CHIP[ask.stage] ?? ""}`} data-testid="otb-ask-stage">
              {ask.approval?.status === "pending"
                ? STAGE_LABEL.waiting
                : (STAGE_LABEL[ask.stage] ?? ask.stage)}
            </span>
            <Link className="btn" to={PAGE_PATH} data-testid="otb-ask-close">
              Back to budgets
            </Link>
          </div>
          <dl className="facts">
            <dt>Booking</dt>
            <dd>
              <Link to={`/booking/${ask.booking_id}`} data-testid="otb-ask-booking">
                {ask.booking_number ?? "Draft booking"}
              </Link>
            </dd>
            <dt>Brand and season</dt>
            <dd>
              {ask.brand.name} · {ask.season.name}
            </dd>
            <dt>Asked</dt>
            <dd>
              {formatDateTime(ask.asked_at)} by {ask.asked_by}
            </dd>
            {ask.approval && ask.approval.status !== "pending" && (
              <>
                <dt>Decided</dt>
                <dd data-testid="otb-ask-decision">
                  {ask.approval.status} by {ask.approval.decided_by}
                  {ask.approval.reason ? ` - ${ask.approval.reason}` : ""}
                </dd>
              </>
            )}
          </dl>
          <OversTable overs={ask.overs} budgets={list?.budgets ?? []} testId="otb-ask-overs" />
          <p className="muted">The Owner approves or turns it down in the approvals inbox.</p>
        </section>
      )}

      {list?.can_set && (
        <section className="card section-card" data-testid="otb-form">
          <h2 className="h3">{current ? "Change a budget" : "Set a budget"}</h2>
          <div className="otb-form">
            <PickerField
              id="otb-brand"
              label="Brand"
              noun="brand"
              placeholder="Choose a brand"
              value={form.brand_id}
              onChange={(v) => setForm({ ...form, brand_id: v })}
              picker={brands}
            />
            <PickerField
              id="otb-season"
              label="Season"
              noun="season"
              placeholder="Choose a season"
              value={form.season_id}
              onChange={(v) => setForm({ ...form, season_id: v })}
              picker={seasons}
            />
            <label className="field">
              <span>Where</span>
              <select
                className="select"
                data-testid="otb-site"
                value={form.site_id}
                onChange={(e) => setForm({ ...form, site_id: e.target.value })}
              >
                <option value="" disabled={!list.company_switched_on}>
                  Whole company
                </option>
                {list.sites.map((s) => (
                  <option key={s.id} value={String(s.id)}>
                    {s.code} · {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Budget at cost, in rupees</span>
              <input
                className="input"
                inputMode="decimal"
                data-testid="otb-amount"
                value={form.rupees}
                onChange={(e) => setForm({ ...form, rupees: e.target.value })}
              />
            </label>
          </div>
          <div className="otb-actions">
            <button
              type="button"
              className="btn btn-cta"
              data-testid="otb-save"
              disabled={offline || busy}
              onClick={() => void save()}
            >
              Save budget
            </button>
            {(form.brand_id || form.rupees) && (
              <button type="button" className="btn" onClick={() => setForm(EMPTY)}>
                Clear
              </button>
            )}
          </div>
        </section>
      )}

      <section className="card section-card">
        <h2 className="h3">Budgets</h2>
        {list && list.budgets.length === 0 && (
          <p className="muted" data-testid="otb-empty">
            No budget is set yet.
          </p>
        )}
        {list && list.budgets.length > 0 && (
          <div className="table-wrap">
            <table className="data" data-testid="otb-budgets">
              <thead>
                <tr>
                  <th>Brand</th>
                  <th>Season</th>
                  <th>Where</th>
                  <th className="num">Budget</th>
                  <th className="num">Open bookings</th>
                  <th className="num">Goods received</th>
                  <th className="num">Open-to-buy</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.budgets.map((b) => (
                  <tr key={b.id} data-testid={`otb-row-${b.site?.code ?? "company"}`}>
                    <td>{b.brand.name}</td>
                    <td>{b.season.name}</td>
                    <td>
                      {scopeName(b)}
                      {!b.switched_on && (
                        <div className="muted-cell" data-testid="otb-row-off">
                          Switched off: bookings are not checked
                        </div>
                      )}
                    </td>
                    <td className="num" data-testid="otb-row-budget">
                      <Money paise={Number(b.budget_paise)} />
                    </td>
                    <td className="num" data-testid="otb-row-open">
                      <Money paise={Number(b.open_paise)} />
                    </td>
                    <td className="num" data-testid="otb-row-received">
                      <Money paise={Number(b.received_paise)} />
                    </td>
                    <td className="num" data-testid="otb-row-left">
                      <Money paise={Number(b.open_to_buy_paise)} />
                      {isOver(b.open_to_buy_paise) && <div className="chip chip-red">Over</div>}
                      {b.cost_missing_pieces > 0 && (
                        <div className="muted-cell">
                          {b.cost_missing_pieces} piece(s) with no cost not counted
                        </div>
                      )}
                    </td>
                    <td>
                      {list.can_set && (
                        <button
                          type="button"
                          className="btn"
                          data-testid="otb-row-change"
                          onClick={() => edit(b)}
                        >
                          Change
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

/** Each budget a booking goes over, and by how much. */
export function OversTable({
  overs,
  budgets,
  testId,
}: {
  overs: OtbAsk["overs"];
  budgets: Pick<OtbBudget, "id" | "site">[];
  testId: string;
}) {
  const where = (budgetId: number, siteId: number | null) => {
    const known = budgets.find((b) => b.id === budgetId);
    if (known) return scopeName(known);
    return siteId === null ? "Whole company" : "A site's budget";
  };
  return (
    <div className="table-wrap">
      <table className="data" data-testid={testId}>
        <thead>
          <tr>
            <th>Budget</th>
            <th className="num">Open-to-buy left</th>
            <th className="num">This booking</th>
            <th className="num">Over by</th>
          </tr>
        </thead>
        <tbody>
          {overs.map((o) => (
            <tr key={o.budget_id}>
              <td>{where(o.budget_id, o.site_id)}</td>
              <td className="num">
                <Money paise={Number(o.open_to_buy_paise)} />
              </td>
              <td className="num">
                <Money paise={Number(o.booking_paise)} />
                {o.cost_missing_pieces > 0 && (
                  <div className="muted-cell">{o.cost_missing_pieces} piece(s) with no cost</div>
                )}
              </td>
              <td className="num" data-testid="otb-over-by">
                <Money paise={Number(o.over_paise)} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
