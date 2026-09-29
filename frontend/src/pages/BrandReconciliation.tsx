import { useState } from "react";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { Denied, Feedback, useGoodsFetch } from "../lib/goodsScreen";

interface BrandChoice {
  id: number;
  code: string;
  name: string;
}
interface ReviewRow {
  id: number;
  brand_label: string;
  brand_id: number | null;
  derived_brand_id: number | null;
  suggestions: BrandChoice[];
  fingerprint: string;
  site_ids: number[];
  state: string;
}
interface ReviewPage {
  resource: string;
  resources: string[];
  brands: BrandChoice[];
  items: ReviewRow[];
  next_cursor: number | null;
}

export function BrandReconciliationPanel() {
  const [resource, setResource] = useState("stockledger.stockonhand");
  const [cursor, setCursor] = useState(0);
  const [selection, setSelection] = useState<Record<number, number>>({});
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const endpoint = "/goods-v1/masters/brand-reconciliation";
  const page = useGoodsFetch<ReviewPage, ReviewPage | null>(
    `${endpoint}?resource=${encodeURIComponent(resource)}&cursor=${cursor}&limit=50`,
    (data) => data,
    null,
  );
  const rows = page.value?.items ?? [];
  const chosen = rows.filter(
    (row) => row.state !== "whole_site" && selection[row.id] !== undefined,
  );

  function navigate(nextResource: string, nextCursor: number) {
    setResource(nextResource);
    setCursor(nextCursor);
    setSelection({});
    setPassword("");
    setError("");
    setOk("");
  }

  async function apply() {
    if (!chosen.length || !password || busy) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      await api.post(endpoint, {
        ...goodsMeta(),
        resource,
        rows: chosen.map((row) => ({
          id: row.id,
          brand_id: selection[row.id],
          fingerprint: row.fingerprint,
        })),
        current_password: password,
      });
      setOk(`Recorded ${chosen.length} reviewed brand mappings.`);
      setSelection({});
      page.reload();
    } catch (reason) {
      setError(apiErrorMessage(reason));
      // A stale batch is never retried against fresh rows without a new review.
      setSelection({});
      page.reload();
    } finally {
      setBusy(false);
      setPassword("");
    }
  }

  if (page.denied) return <Denied what="brand reconciliation" />;
  return (
    <section className="card section-card" data-testid="brand-reconciliation">
      <h2>Review brand identity</h2>
      <p>
        Select a brand for each row you have verified. Name suggestions are for review only.
        Unresolved rows remain unavailable for operational access. Historical labels and values are
        preserved.
      </p>
      <Feedback error={error || page.failure} ok={ok} />
      <label htmlFor="brand-reconciliation-source">Records</label>
      <select
        id="brand-reconciliation-source"
        className="input"
        value={resource}
        disabled={busy}
        onChange={(event) => navigate(event.target.value, 0)}
      >
        {(page.value?.resources ?? [resource]).map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      {page.loading ? (
        <p>Loading review…</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Record</th>
              <th>Historical label</th>
              <th>Sites</th>
              <th>Evidence</th>
              <th>Reviewed brand</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.id}</td>
                <td>{row.brand_label || "No label"}</td>
                <td>{row.site_ids.join(", ")}</td>
                <td>
                  {row.state === "whole_site"
                    ? "Whole-site alert; no single brand mapping"
                    : row.brand_id !== null
                      ? `Linked to #${row.brand_id}`
                      : row.derived_brand_id !== null
                        ? `Source document: #${row.derived_brand_id}`
                        : row.suggestions.length
                          ? `Name suggestions: ${row.suggestions.map((brand) => `${brand.name} (${brand.code}, #${brand.id})`).join(", ")}`
                          : "No proven mapping"}
                </td>
                <td>
                  <select
                    className="input"
                    aria-label={`Reviewed brand for record ${row.id}`}
                    value={selection[row.id] ?? ""}
                    disabled={busy || row.brand_id !== null || row.state === "whole_site"}
                    onChange={(event) =>
                      setSelection((current) => {
                        const next = { ...current };
                        if (event.target.value) next[row.id] = Number(event.target.value);
                        else delete next[row.id];
                        return next;
                      })
                    }
                  >
                    <option value="">Leave unresolved</option>
                    {(page.value?.brands ?? []).map((brand) => (
                      <option key={brand.id} value={brand.id}>
                        {brand.name} ({brand.code}, #{brand.id})
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!page.loading && rows.length === 0 && !page.failure && <p>No records on this page.</p>}
      <div className="form-actions">
        <button
          className="btn"
          disabled={busy || cursor === 0}
          onClick={() => navigate(resource, 0)}
        >
          First page
        </button>
        <button
          className="btn"
          disabled={busy || page.loading || !page.value?.next_cursor}
          onClick={() => navigate(resource, page.value?.next_cursor ?? 0)}
        >
          Next page
        </button>
      </div>
      <p>{chosen.length} rows selected. Review the exact rows and targets before applying.</p>
      <label htmlFor="brand-reconciliation-password">Confirm your password</label>
      <input
        id="brand-reconciliation-password"
        className="input"
        type="password"
        autoComplete="current-password"
        value={password}
        disabled={busy}
        onChange={(event) => setPassword(event.target.value)}
      />
      <button
        className="btn btn-cta"
        disabled={busy || page.loading || !chosen.length || !password}
        onClick={() => void apply()}
      >
        {busy ? "Applying…" : "Apply reviewed mappings"}
      </button>
    </section>
  );
}
