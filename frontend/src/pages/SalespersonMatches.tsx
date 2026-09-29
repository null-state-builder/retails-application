import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta, typedApi } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";

type Payload = ApiRead<ApiSchemas["Matches"]>;
type Row = Payload["rows"][number];

/** Setup > Salesperson Matches (store operations ticket 07, section 29).
 *
 *  The till's salesperson list moved from the old salesperson table to the staff
 *  list. Every old row was frozen, with the bills sold under it, and matched to a
 *  staff record at its store only where that was certain (the same code, or the
 *  same name). The rows listed as unmatched wait for Admin to pick the person;
 *  nothing is guessed, and a past bill's seller never changes. When the person
 *  has no staff record yet, add them in People and access first. */
export function SalespersonMatchesPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [picked, setPicked] = useState<Record<number, string>>({});
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await typedApi.get("/goods-v1/sell/salesperson-matches");
      setData(response.data as Payload);
    } catch (reason) {
      setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function resolve(row: Row) {
    const staffId = picked[row.id];
    if (!staffId) return;
    setError("");
    setSaved("");
    setBusy(true);
    try {
      await api.post(`/goods-v1/sell/salesperson-matches/${row.id}/resolve`, {
        ...goodsMeta(row.revision),
        staff_id: staffId,
      });
      setSaved(`${row.name} (${row.store_code}) is matched.`);
      await load();
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Salesperson Matches" />
        {error ? <p className="warn-note">{error}</p> : <p>Loading salesperson matches…</p>}
      </div>
    );
  }

  const unmatched = data.rows.filter((row) => row.staff === null);
  const matched = data.rows.filter((row) => row.staff !== null);
  const candidates = (storeId: number) =>
    data.candidates.find((entry) => entry.store_id === storeId)?.staff ?? [];

  return (
    <div className="page-pad">
      <PageHeader
        title="Salesperson Matches"
        lead="Each old salesperson matched to a person on the store's staff list. Past bills keep the salesperson they were sold under."
      />
      {!data.can_resolve && (
        <p className="muted-cell" data-testid="matches-read-only">
          Only Admin can resolve an unmatched salesperson. You can see everything here.
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="matches-error">
          {error}
        </p>
      )}
      {saved && (
        <p className="ok-note" data-testid="matches-saved">
          {saved}
        </p>
      )}

      <section className="card section-card">
        <h2 className="h3" data-testid="matches-unmatched-count">
          Unmatched: {data.unmatched}
        </h2>
        {unmatched.length === 0 ? (
          <p className="muted-cell" data-testid="matches-none-unmatched">
            Every old salesperson is matched to a staff record.
          </p>
        ) : (
          <>
            <p className="muted-cell">
              Pick the person each one was. Only people who worked at that store are offered. If the
              person is missing, add them in <Link to="/setup/people-access">People and access</Link>{" "}
              first.
            </p>
            <div className="table-wrap">
              <table className="data" data-testid="matches-unmatched">
                <thead>
                  <tr>
                    <th>Store</th>
                    <th>Old code</th>
                    <th>Old name</th>
                    <th className="num">Past lines</th>
                    <th>Why not matched</th>
                    <th>Staff record</th>
                  </tr>
                </thead>
                <tbody>
                  {unmatched.map((row) => (
                    <tr key={row.id} data-testid={`match-row-${row.id}`}>
                      <td>{row.store_code}</td>
                      <td className="mono">{row.code}</td>
                      <td>{row.name}</td>
                      <td className="num">{row.lines}</td>
                      <td>{row.reason}</td>
                      <td>
                        {data.can_resolve ? (
                          <span className="toolbar">
                            <select
                              className="select"
                              aria-label={`Staff record for ${row.name}`}
                              data-testid={`match-pick-${row.id}`}
                              value={picked[row.id] ?? ""}
                              onChange={(event) =>
                                setPicked({ ...picked, [row.id]: event.target.value })
                              }
                            >
                              <option value="">Pick a person</option>
                              {candidates(row.store_id).map((person) => (
                                <option key={person.id} value={person.id}>
                                  {person.display_name} ({person.staff_code})
                                  {person.active ? "" : " - not active"}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              className="btn btn-primary"
                              data-testid={`match-resolve-${row.id}`}
                              disabled={busy || !picked[row.id]}
                              onClick={() => void resolve(row)}
                            >
                              Match
                            </button>
                          </span>
                        ) : (
                          "Waiting for Admin"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <section className="card section-card">
        <h2 className="h3">Matched</h2>
        {matched.length === 0 ? (
          <p className="muted-cell">Nothing is matched yet.</p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="matches-matched">
              <thead>
                <tr>
                  <th>Store</th>
                  <th>Old code</th>
                  <th>Old name</th>
                  <th className="num">Past lines</th>
                  <th>Staff record</th>
                  <th>How</th>
                </tr>
              </thead>
              <tbody>
                {matched.map((row) => (
                  <tr key={row.id} data-testid={`matched-row-${row.id}`}>
                    <td>{row.store_code}</td>
                    <td className="mono">{row.code}</td>
                    <td>{row.name}</td>
                    <td className="num">{row.lines}</td>
                    <td>
                      {row.staff?.display_name} ({row.staff?.staff_code})
                    </td>
                    <td>{row.rule_label}</td>
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
