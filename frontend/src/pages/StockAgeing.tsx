import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { ageText, dayText, firstArrivalText, groupHeading } from "../lib/stockAgeing";

type Payload = ApiRead<ApiSchemas["StockAgeing"]>;
type Row = Payload["rows"][number];
type Season = Payload["seasons"][number];

const OFFLINE = "Stock ageing needs a connection. Reconnect to see it.";

/** Stock > Stock Ageing (store operations ticket 33, ST-INV-2).
 *
 *  The store's stock aged against its season, not flat days: in-season stock is
 *  flagged after the set days with no sale here, stock whose season has ended is
 *  aged from the day it ended, and the unknown historical season is its own
 *  group, never guessed. Each row shows the days since the pieces first arrived
 *  in the company and the days at this store. The server decides every group,
 *  age and flag; this page only shows them.
 *
 *  A master-data steward records the day a season ended here; each change is in
 *  the audit log. Everything is live data, so offline the page says it needs a
 *  connection instead of showing a stale copy as current. Only stores in the
 *  person's own scope, and only where the switch is on. */
export function StockAgeingPage() {
  // The store lives in the address, so an alert opened while this page is
  // already showing another store moves it to the right one.
  const [params, setParams] = useSearchParams();
  const siteId = Number(params.get("site_id")) || null;
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  // A store the address asked for that this person cannot see (or that was
  // switched off since the alert was raised): said once, then the page carries on.
  const [refused, setRefused] = useState("");
  const [saved, setSaved] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
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
        const response = await api.get<Payload>("/goods-v1/stock/ageing", {
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

  /** True once the server has it, so the editor can drop its draft. */
  async function saveSeasonEnd(season: Season, endedOn: string | null): Promise<boolean> {
    if (!navigator.onLine) {
      setOnline(false);
      return false;
    }
    setError("");
    setSaved("");
    setBusy(true);
    let done = false;
    try {
      await api.post("/goods-v1/stock/ageing/season-end", {
        ...goodsMeta(),
        season_id: season.id,
        ended_on: endedOn,
      });
      setSaved(
        endedOn
          ? `${season.name} ended on ${dayText(endedOn)}.`
          : `${season.name} has no end date now.`,
      );
      done = true;
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
    await load(siteId);
    return done;
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <p className="warn-note" data-testid="ageing-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="ageing-retry"
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
      title="Stock Ageing"
      lead="Stock at the store aged against its season. In-season stock is flagged after the set days with no sale here; stock whose season has ended counts as aged from the day it ended; stock of the unknown historical season is its own group and is never guessed."
    />
  );
  const errorNote = (error || refused) && (
    <p className="warn-note" data-testid="ageing-error">
      {error || refused}
    </p>
  );

  if (!data) {
    return (
      <OperationsPage>
        {header}
        {offlineNote}
        {errorNote || (online && !lost && <p>Loading…</p>)}
      </OperationsPage>
    );
  }

  if (data.stores.length === 0) {
    return (
      <OperationsPage>
        {header}
        {offlineNote}
        {errorNote}
        <p className="muted-cell" data-testid="ageing-none">
          Season-aware stock ageing is not switched on at any store you work at.
        </p>
      </OperationsPage>
    );
  }

  return (
    <OperationsPage>
      {header}
      {offlineNote}
      {errorNote}
      {saved && (
        <p className="ok-note" data-testid="ageing-saved">
          {saved}
        </p>
      )}
      <section className="card section-card">
        <div className="toolbar">
          <label className="field">
            <span>Store</span>
            <select
              className="select"
              data-testid="ageing-store"
              value={data.site_id ?? ""}
              disabled={disabled}
              onChange={(event) => {
                setRefused("");
                // Only the store changes: inside Inventory the tab stays.
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
          <span className="muted-cell">As of {dayText(data.today)}</span>
        </div>
        {!data.goods_records ? (
          <p className="warn-note" data-testid="ageing-legacy">
            This store's stock is not on the goods records yet, so it cannot be aged.
          </p>
        ) : data.rows.length === 0 ? (
          <p className="ok-note" data-testid="ageing-empty">
            No stock at this store.
          </p>
        ) : (
          data.groups
            .filter((group) => group.qty > 0)
            .map((group) => (
              <AgeingGroupTable
                key={group.group}
                group={group}
                idleDays={data.idle_days}
                rows={data.rows.filter((row) => row.group === group.group)}
              />
            ))
        )}
      </section>
      <SeasonEnds
        seasons={data.seasons}
        canSet={data.can_set_season_end}
        today={data.today}
        disabled={disabled}
        onSave={saveSeasonEnd}
      />
    </OperationsPage>
  );
}

function AgeingGroupTable({
  group,
  idleDays,
  rows,
}: {
  group: Payload["groups"][number];
  idleDays: number | null;
  rows: Row[];
}) {
  return (
    <div data-testid={`ageing-group-${group.group}`}>
      <h3 className="section-title">{groupHeading(group.group, idleDays)}</h3>
      <p className="muted-cell" data-testid={`ageing-total-${group.group}`}>
        {group.qty} piece(s) of {group.items} item(s)
        {group.aged_qty > 0 ? `, ${group.aged_qty} aged` : ""}
      </p>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Barcode</th>
              <th>Brand</th>
              <th>Design</th>
              <th>Size</th>
              <th>Colour</th>
              <th>Season</th>
              <th className="num">Pieces</th>
              <th>In the company</th>
              <th>At this store</th>
              <th>Last sale here</th>
              <th>Age</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={`${row.origin_id}-${row.arrived_here_on}`}
                data-testid={`ageing-row-${row.barcode || row.sku_id}`}
              >
                <td className="mono">{row.barcode || "No barcode"}</td>
                <td>{row.brand}</td>
                <td>{row.design}</td>
                <td>{row.size}</td>
                <td>{row.colour}</td>
                <td>{row.season_code || "None"}</td>
                <td className="num">{row.qty}</td>
                <td data-testid="ageing-in-company">{firstArrivalText(row)}</td>
                <td data-testid="ageing-here">
                  {row.days_here} {row.days_here === 1 ? "day" : "days"} (since{" "}
                  {dayText(row.arrived_here_on)})
                </td>
                <td>{row.last_sale_on ? dayText(row.last_sale_on) : "None"}</td>
                <td data-testid="ageing-age">
                  {row.aged && <span className="chip chip-amber">Aged</span>} {ageText(row)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SeasonEnds({
  seasons,
  canSet,
  today,
  disabled,
  onSave,
}: {
  seasons: Season[];
  canSet: boolean;
  today: string;
  disabled: boolean;
  onSave: (season: Season, endedOn: string | null) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState<Record<number, string>>({});
  // Once saved, the server's value is the one to show, not what was typed.
  async function save(season: Season, endedOn: string | null) {
    if (!(await onSave(season, endedOn))) return;
    setDraft((current) => {
      const next = { ...current };
      delete next[season.id];
      return next;
    });
  }
  const listed = seasons.filter((season) => !season.historical_unknown);
  if (listed.length === 0) return null;
  return (
    <section className="card section-card" data-testid="ageing-seasons">
      <h3 className="section-title">Season end dates</h3>
      <p className="muted-cell">
        Stock of a season counts as aged from the day the season ended.
        {canSet
          ? " Record the day once the season has ended; every change is in the audit log."
          : " A master-data steward records these."}
      </p>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Season</th>
              <th>Status</th>
              <th>Ended on</th>
              {canSet && <th />}
            </tr>
          </thead>
          <tbody>
            {listed.map((season) => {
              const value = draft[season.id] ?? season.ended_on ?? "";
              return (
                <tr key={season.id} data-testid={`ageing-season-${season.code}`}>
                  <td>
                    {season.name} ({season.code})
                  </td>
                  <td>{season.status}</td>
                  <td>
                    {canSet ? (
                      <input
                        type="date"
                        className="input"
                        data-testid={`ageing-season-date-${season.code}`}
                        max={today}
                        value={value}
                        disabled={disabled}
                        onChange={(event) =>
                          setDraft((current) => ({ ...current, [season.id]: event.target.value }))
                        }
                      />
                    ) : season.ended_on ? (
                      dayText(season.ended_on)
                    ) : (
                      "Not recorded"
                    )}
                  </td>
                  {canSet && (
                    <td>
                      <button
                        type="button"
                        className="btn"
                        data-testid={`ageing-season-save-${season.code}`}
                        disabled={disabled || !value || value === season.ended_on}
                        onClick={() => void save(season, value)}
                      >
                        Save
                      </button>{" "}
                      {season.ended_on && (
                        <button
                          type="button"
                          className="btn"
                          data-testid={`ageing-season-clear-${season.code}`}
                          disabled={disabled}
                          onClick={() => void save(season, null)}
                        >
                          Clear
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
