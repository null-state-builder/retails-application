import { useState } from "react";

import { Note } from "../components/Note";
import { PageHeader } from "../components/PageHeader";
import { Status } from "../components/Status";
import { apiErrorMessage } from "../lib/api";
import { useCachedGet } from "../lib/cachedQuery";
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
  const [siteId, setSiteId] = useState<number | null>(null);
  // From the shared cache: a revisit shows the last answer at once and
  // refreshes it behind it.
  const read = useCachedGet<Payload>("/goods-v1/sell/staff-list", { params: { site_id: siteId } });
  const data = read.data ?? null;
  const error = read.error !== null ? apiErrorMessage(read.error) : "";

  return (
    <div className="page-pad">
      <PageHeader
        title="Staff List"
        lead="Who works at the store, who is active, and who can be named as the salesperson at the till."
      />
      {error && (
        <Note tone="error" testId="staff-list-error">
          {error}
        </Note>
      )}
      {!data ? (
        !error && <p>Loading the staff list…</p>
      ) : data.stores.length === 0 ? (
        <p className="muted-cell" data-testid="staff-list-none">
          The staff list is not switched on at any store you work at.
        </p>
      ) : (
        <>
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
                        <Status tone={row.active ? "green" : "grey"}>
                          {row.active ? "Active" : "Not active"}
                        </Status>
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
        </>
      )}
    </div>
  );
}
