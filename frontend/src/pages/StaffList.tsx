import { useCallback, useEffect, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { apiErrorMessage, typedApi } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";

type Payload = ApiRead<ApiSchemas["StaffList"]>;

function day(value: string | null | undefined): string {
  return value ? value.slice(0, 10) : "";
}

/** HRMS > Staff List (store operations ticket 07, ST-HR-1).
 *
 *  One store's staff: who is assigned there, who is active, and who sells at
 *  the till. The till's salesperson picker reads this same list - the active
 *  people marked as salespeople. Only stores in the person's own scope, and only
 *  where the staff list is switched on; the server refuses anything else. */
export function StaffListPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [siteId, setSiteId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async (site: number | null) => {
    setError("");
    try {
      const response = await typedApi.get("/goods-v1/sell/staff-list", {
        params: site ? { site_id: site } : {},
      });
      setData(response.data as Payload);
    } catch (reason) {
      setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load(siteId);
  }, [load, siteId]);

  return (
    <div className="page-pad">
      <PageHeader
        title="Staff List"
        lead="Who works at the store, who is active, and who can be named as the salesperson at the till."
      />
      {error && (
        <p className="warn-note" data-testid="staff-list-error">
          {error}
        </p>
      )}
      {!data ? (
        !error && <p>Loading the staff list…</p>
      ) : data.stores.length === 0 ? (
        <p className="muted-cell" data-testid="staff-list-none">
          The staff list is not switched on at any store you work at.
        </p>
      ) : (
        <section className="card section-card">
          <div className="toolbar">
            <label className="field">
              <span>Store</span>
              <select
                className="select"
                data-testid="staff-list-store"
                value={data.site_id ?? ""}
                onChange={(event) => setSiteId(Number(event.target.value))}
              >
                {data.stores.map((store) => (
                  <option key={store.id} value={store.id}>
                    {store.name} ({store.code})
                  </option>
                ))}
              </select>
            </label>
          </div>
          {data.staff.length === 0 ? (
            <p className="muted-cell" data-testid="staff-list-empty">
              Nobody is on this store's staff list yet. Add people in Setup, People and access.
            </p>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid="staff-list-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Staff code</th>
                    <th>Status</th>
                    <th>Salesperson at the till</th>
                    <th>Here since</th>
                    <th>Until</th>
                  </tr>
                </thead>
                <tbody>
                  {data.staff.map((row) => (
                    <tr key={row.id} data-testid={`staff-row-${row.staff_code}`}>
                      <td>{row.display_name}</td>
                      <td className="mono">{row.staff_code}</td>
                      <td>
                        <span className={row.active ? "chip" : "chip chip-muted"}>
                          {row.active ? "Active" : "Not active"}
                        </span>
                      </td>
                      <td>{row.salesperson ? "Yes" : "No"}</td>
                      <td>{day(row.assigned_from)}</td>
                      <td>{day(row.assigned_to)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
