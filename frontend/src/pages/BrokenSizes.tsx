import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  actionText,
  categoryText,
  closedText,
  heldText,
  measureText,
  outcomeText,
  parseSizes,
} from "../lib/brokenSizes";
import { formatDateTime } from "../lib/format";

type Payload = ApiRead<ApiSchemas["BrokenSizes"]>;
type AlertRow = Payload["open"][number];
type Rule = Payload["rules"][number];
type Action = "transfer" | "markdown" | "other";

const OFFLINE = "Broken sizes need a connection. Reconnect to see them.";

/** Stock > Broken Sizes (store operations ticket 32, ST-INV-1).
 *
 *  A style-colour at the store missing its category's share of core sizes (40%
 *  or more to start) while it still has stock is a broken-size alert. The daily
 *  check opens, refreshes and closes them; the server decides all of it, and this
 *  page only shows it. The store records what it did about each alert, once, so
 *  the 7-day measure can be read; a master-data steward sets each category's core
 *  sizes and share. Every write is in the audit log.
 *
 *  Everything is live data, so offline the page says it needs a connection
 *  instead of showing a stale copy as current. Only stores in the person's own
 *  scope, and only where the switch is on. */
export function BrokenSizesPage() {
  const [params, setParams] = useSearchParams();
  const siteId = Number(params.get("site_id")) || null;
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [refused, setRefused] = useState("");
  const [saved, setSaved] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(
    async (site: number | null) => {
      if (!navigator.onLine) {
        setOnline(false);
        return;
      }
      const mine = ++request.current;
      setBusy(true);
      setError("");
      try {
        const response = await api.get<Payload>("/goods-v1/stock/broken-sizes", {
          params: site ? { site_id: site } : {},
        });
        if (mine !== request.current) return;
        setLost(false);
        setData(response.data);
      } catch (reason) {
        if (mine !== request.current) return;
        if (isConnectionLost(reason)) setLost(true);
        else if (site && apiErrorCode(reason) === "NOT_FOUND") {
          setRefused(apiErrorMessage(reason));
          setParams(
            (current) => {
              const next = new URLSearchParams(current);
              next.delete("site_id");
              return next;
            },
            { replace: true },
          );
        } else setError(apiErrorMessage(reason));
      } finally {
        if (mine === request.current) setBusy(false);
      }
    },
    [setParams],
  );

  useEffect(() => {
    void load(siteId);
  }, [load, siteId]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(siteId);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load, siteId]);

  /** Send one write; true once the server has it. The page reloads either way. */
  async function write(
    path: string,
    body: Record<string, unknown>,
    done: string,
  ): Promise<boolean> {
    if (!navigator.onLine) {
      setOnline(false);
      return false;
    }
    setError("");
    setSaved("");
    setBusy(true);
    let ok = false;
    let refusal = "";
    try {
      await api.post(path, { ...goodsMeta(), ...body });
      setSaved(done);
      ok = true;
    } catch (reason) {
      if (isConnectionLost(reason)) {
        // No answer at all: say so and leave the page as it was; Try again reloads it.
        setLost(true);
        return false;
      }
      refusal = apiErrorMessage(reason);
    } finally {
      setBusy(false);
    }
    await load(siteId);
    // After the reload, which starts by clearing the last error.
    if (refusal) setError(refusal);
    return ok;
  }

  const act = (alert: AlertRow, action: Action, note: string) =>
    write(
      "/goods-v1/stock/broken-sizes/act",
      { alert_id: alert.id, action, note },
      `${alert.style_code}${alert.colour ? ` ${alert.colour}` : ""}: recorded.`,
    );

  const saveRule = (category: string, sizes: string[], percent: number, active: boolean) =>
    write(
      "/goods-v1/stock/broken-sizes/rules",
      { category, core_sizes: sizes, missing_percent: percent, active },
      active
        ? `${categoryText(category)}: core sizes saved. They apply from the next daily check.`
        : `${categoryText(category)}: rule removed. Its stock is not checked from the next daily check.`,
    );

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="broken-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="broken-retry"
            disabled={busy}
            onClick={() => void load(siteId)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const header = (
    <PageHeader
      title="Broken Sizes"
      lead="Style-colours at the store missing too many of their category's core sizes while they still have stock. Checked every day. Record what you did about each one within 7 days."
    />
  );
  const errorNote = (error || refused) && (
    <p className="warn-note" data-testid="broken-error">
      {error || refused}
    </p>
  );

  if (!data) {
    return (
      <div className="page-pad">
        {header}
        {offlineNote}
        {errorNote || (online && !lost && <p>Loading…</p>)}
      </div>
    );
  }

  if (data.stores.length === 0) {
    return (
      <div className="page-pad">
        {header}
        {offlineNote}
        {errorNote}
        <p className="muted-cell" data-testid="broken-none">
          Broken-size alerts are not switched on at any store you work at.
        </p>
      </div>
    );
  }

  return (
    <div className="page-pad">
      {header}
      {offlineNote}
      {errorNote}
      {saved && (
        <p className="ok-note" data-testid="broken-saved">
          {saved}
        </p>
      )}
      <section className="card section-card">
        <div className="toolbar">
          <label className="field">
            <span>Store</span>
            <select
              className="select"
              data-testid="broken-store"
              value={data.site_id ?? ""}
              disabled={disabled}
              onChange={(event) => {
                setRefused("");
                setParams((current) => {
                  const next = new URLSearchParams(current);
                  next.set("site_id", event.target.value);
                  return next;
                });
              }}
            >
              {data.stores.map((store) => (
                <option key={store.id} value={store.id}>
                  {store.name} ({store.code})
                </option>
              ))}
            </select>
          </label>
          <span className="muted-cell" data-testid="broken-checked">
            {data.checked_at ? `Last checked ${formatDateTime(data.checked_at)}` : "Checked daily"}
          </span>
        </div>
        <p data-testid="broken-measure">{measureText(data.measure)}</p>
        {!data.goods_records ? (
          <p className="warn-note" data-testid="broken-legacy">
            This store's stock is not on the goods records yet, so its sizes cannot be checked.
          </p>
        ) : data.open.length === 0 ? (
          <p className="ok-note" data-testid="broken-empty">
            No broken sizes open at this store.
          </p>
        ) : (
          <OpenAlerts rows={data.open} canAct={data.can_act} disabled={disabled} onAct={act} />
        )}
        {data.unruled_categories.length > 0 && (
          <p className="muted-cell" data-testid="broken-unruled">
            Not checked, no core sizes set: {data.unruled_categories.map(categoryText).join(", ")}.
          </p>
        )}
        {data.mixed.length > 0 && (
          <p className="muted-cell" data-testid="broken-mixed">
            Not checked, its items name more than one category: {data.mixed.join("; ")}.
          </p>
        )}
      </section>
      {data.closed.length > 0 && <ClosedAlerts rows={data.closed} />}
      <Rules rules={data.rules} canSet={data.can_set_rules} disabled={disabled} onSave={saveRule} />
    </div>
  );
}

function OpenAlerts({
  rows,
  canAct,
  disabled,
  onAct,
}: {
  rows: AlertRow[];
  canAct: boolean;
  disabled: boolean;
  onAct: (alert: AlertRow, action: Action, note: string) => Promise<boolean>;
}) {
  return (
    <div data-testid="broken-open">
      <h3 className="section-title">Open</h3>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Brand</th>
              <th>Style</th>
              <th>Colour</th>
              <th>Category</th>
              <th>Missing core sizes</th>
              <th>Held here</th>
              <th>Opened</th>
              <th>Acted on</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} data-testid={`broken-row-${row.style_code}`}>
                <td>{row.brand}</td>
                <td>{row.style_code}</td>
                <td>{row.colour || "None"}</td>
                <td>{categoryText(row.category)}</td>
                <td data-testid="broken-missing">
                  {row.missing_sizes.join(", ")}{" "}
                  <span className="muted-cell">
                    ({row.missing_percent}% of {row.core_sizes.join(", ")}; alert at{" "}
                    {row.rule_percent}%)
                  </span>
                </td>
                <td>{heldText(row.held)}</td>
                <td>{formatDateTime(row.opened_at)}</td>
                <td data-testid="broken-acted">
                  {row.acted_at ? (
                    <>
                      {actionText(row.action, row.action_note)}
                      <span className="muted-cell">
                        {" "}
                        ({row.acted_by || "someone"}, {formatDateTime(row.acted_at)})
                      </span>
                    </>
                  ) : canAct ? (
                    <ActControls row={row} disabled={disabled} onAct={onAct} />
                  ) : (
                    "Not yet"
                  )}{" "}
                  <span
                    className={`chip ${row.outcome === "missed" ? "chip-amber" : ""}`}
                    data-testid="broken-outcome"
                  >
                    {outcomeText(row.outcome)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ActControls({
  row,
  disabled,
  onAct,
}: {
  row: AlertRow;
  disabled: boolean;
  onAct: (alert: AlertRow, action: Action, note: string) => Promise<boolean>;
}) {
  const [action, setAction] = useState<Action>("transfer");
  const [note, setNote] = useState("");
  const needsNote = action === "other" && !note.trim();
  return (
    <span className="toolbar">
      <select
        className="select"
        data-testid="broken-action"
        value={action}
        disabled={disabled}
        onChange={(event) => setAction(event.target.value as Action)}
      >
        <option value="transfer">Asked for a transfer</option>
        <option value="markdown">Asked for a markdown</option>
        <option value="other">Something else</option>
      </select>
      <input
        className="input"
        data-testid="broken-note"
        placeholder={action === "other" ? "What was done" : "Note (optional)"}
        maxLength={240}
        value={note}
        disabled={disabled}
        onChange={(event) => setNote(event.target.value)}
      />
      <button
        type="button"
        className="btn"
        data-testid="broken-record"
        disabled={disabled || needsNote}
        onClick={() => void onAct(row, action, note)}
      >
        Record
      </button>
    </span>
  );
}

function ClosedAlerts({ rows }: { rows: AlertRow[] }) {
  return (
    <section className="card section-card" data-testid="broken-closed">
      <h3 className="section-title">Recently closed</h3>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Style</th>
              <th>Colour</th>
              <th>Opened</th>
              <th>Acted on</th>
              <th>Closed</th>
              <th>7 days</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>
                  {row.brand} {row.style_code}
                </td>
                <td>{row.colour || "None"}</td>
                <td>{formatDateTime(row.opened_at)}</td>
                <td>
                  {row.acted_at
                    ? `${actionText(row.action, row.action_note)} (${formatDateTime(row.acted_at)})`
                    : "Not acted on"}
                </td>
                <td>
                  {row.closed_at ? formatDateTime(row.closed_at) : ""}:{" "}
                  {closedText(row.closed_reason)}
                </td>
                <td>{outcomeText(row.outcome)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Rules({
  rules,
  canSet,
  disabled,
  onSave,
}: {
  rules: Rule[];
  canSet: boolean;
  disabled: boolean;
  onSave: (category: string, sizes: string[], percent: number, active: boolean) => Promise<boolean>;
}) {
  const [category, setCategory] = useState("");
  const [sizes, setSizes] = useState("");
  const [percent, setPercent] = useState("40");
  // An edited rule keeps whether it is in use; a new one starts in use.
  const [active, setActive] = useState(true);
  const listed = parseSizes(sizes);
  const share = Number(percent);
  const valid = listed.length > 0 && Number.isInteger(share) && share >= 1 && share <= 100;

  async function add() {
    if (!(await onSave(category.trim(), listed, share, active))) return;
    setCategory("");
    setSizes("");
    setPercent("40");
    setActive(true);
  }

  return (
    <section className="card section-card" data-testid="broken-rules">
      <h3 className="section-title">Core sizes by category</h3>
      <p className="muted-cell">
        A style-colour is broken when this share or more of its category's core sizes is missing at
        the store while it still has stock. A category with no core sizes is not checked.
        {canSet
          ? " Changes apply from the next daily check; every change is in the audit log."
          : " A master-data steward sets these."}
      </p>
      {rules.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Category</th>
                <th>Core sizes</th>
                <th className="num">Alert at</th>
                <th>In use</th>
                {canSet && <th />}
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => (
                <tr key={rule.category} data-testid={`broken-rule-${rule.category || "none"}`}>
                  <td>{categoryText(rule.category)}</td>
                  <td>{rule.core_sizes.join(", ")}</td>
                  <td className="num">{rule.missing_percent}% missing</td>
                  <td>{rule.active ? "Yes" : "Removed"}</td>
                  {canSet && (
                    <td>
                      <button
                        type="button"
                        className="btn"
                        data-testid={`broken-rule-edit-${rule.category || "none"}`}
                        disabled={disabled}
                        onClick={() => {
                          setCategory(rule.category);
                          setSizes(rule.core_sizes.join(", "));
                          setPercent(String(rule.missing_percent));
                          setActive(rule.active);
                        }}
                      >
                        Edit
                      </button>{" "}
                      <button
                        type="button"
                        className="btn"
                        data-testid={`broken-rule-toggle-${rule.category || "none"}`}
                        disabled={disabled}
                        onClick={() =>
                          void onSave(
                            rule.category,
                            rule.core_sizes,
                            rule.missing_percent,
                            !rule.active,
                          )
                        }
                      >
                        {rule.active ? "Remove" : "Use again"}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {canSet && (
        <div className="toolbar" data-testid="broken-rule-form">
          <label className="field">
            <span>Category</span>
            <input
              className="input"
              data-testid="broken-rule-category"
              placeholder="Blank for No category"
              maxLength={120}
              value={category}
              disabled={disabled}
              onChange={(event) => setCategory(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Core sizes</span>
            <input
              className="input"
              data-testid="broken-rule-sizes"
              placeholder="S, M, L, XL"
              value={sizes}
              disabled={disabled}
              onChange={(event) => setSizes(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Alert at % missing</span>
            <input
              className="input"
              type="number"
              min={1}
              max={100}
              data-testid="broken-rule-percent"
              value={percent}
              disabled={disabled}
              onChange={(event) => setPercent(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="broken-rule-save"
            disabled={disabled || !valid}
            onClick={() => void add()}
          >
            Save
          </button>
        </div>
      )}
    </section>
  );
}
