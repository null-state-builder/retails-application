import { useCallback, useEffect, useState } from "react";

import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { apiErrorMessage, typedApi } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";

type Payload = ApiRead<ApiSchemas["MissingHsn"]>;

/** Stock > Items with no HSN (store operations ticket 12, ST-CMP-3).
 *
 *  The pieces a store's counter holds whose HSN is blank or is not an HSN, for
 *  fixing. The till never stops a bill for one - under saved tax settings it
 *  charges the "no rule" rate and flags the bill - so this list is where they
 *  get fixed.
 *  A piece's HSN comes from its PT, which is never edited: the fix is to reverse
 *  and reissue that PT with the HSN from the brand's invoice. Read-only; only
 *  stores in the person's own scope, and only where the switch is on. */
export function MissingHsnPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [siteId, setSiteId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async (site: number | null) => {
    setError("");
    try {
      const response = await typedApi.get("/goods-v1/sell/missing-hsn", {
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
    <OperationsPage>
      <PageHeader
        title="Items with no HSN"
        lead="Pieces at the store whose HSN is missing or is not an HSN. Where tax settings are on, the till still bills them, at the rate set for an HSN no rule covers, and flags the bill. Fix the HSN on the PT they came in on."
      />
      {error && (
        <p className="warn-note" data-testid="missing-hsn-error">
          {error}
        </p>
      )}
      {!data ? (
        !error && <p>Loading…</p>
      ) : data.stores.length === 0 ? (
        <p className="muted-cell" data-testid="missing-hsn-none">
          HSN on every item is not switched on at any store you work at.
        </p>
      ) : (
        <section className="card section-card">
          <div className="toolbar">
            <label className="field">
              <span>Store</span>
              <select
                className="select"
                data-testid="missing-hsn-store"
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
          {data.items.length === 0 ? (
            <p className="ok-note" data-testid="missing-hsn-empty">
              Every item at this store has an HSN.
            </p>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid="missing-hsn-table">
                <thead>
                  <tr>
                    <th>Barcode</th>
                    <th>Season</th>
                    <th>Brand</th>
                    <th>Item</th>
                    <th>Design</th>
                    <th>Size</th>
                    <th>HSN now</th>
                    <th className="num">Pieces here</th>
                    <th>How to fix</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((row) => (
                    <tr
                      key={`${row.barcode}-${row.season}`}
                      data-testid={`missing-hsn-${row.barcode}`}
                    >
                      <td className="mono">{row.barcode}</td>
                      <td>{row.season}</td>
                      <td>{row.brand}</td>
                      <td>{row.item}</td>
                      <td>{row.design}</td>
                      <td>{row.size}</td>
                      <td className="mono">{row.hsn || "None"}</td>
                      <td className="num">{row.qty}</td>
                      <td>
                        {row.fix === "reissue_pt"
                          ? `Reverse and reissue PT ${row.pt_number} with the HSN`
                          : "No correction screen yet. Ask head office."}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </OperationsPage>
  );
}
