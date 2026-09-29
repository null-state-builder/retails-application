import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Search } from "lucide-react";

import { PageHeader } from "../../components/PageHeader";
import { api, apiErrorMessage } from "../../lib/api";
import type { ApiRead, ApiSchemas } from "../../lib/api";
import { isConnectionLost } from "../../lib/auditLog";
import { CUSTOMERS_OFFLINE, searchQuery } from "../../lib/customerRights";
import { formatDateTime } from "../../lib/format";
import { withQuery } from "../../lib/query";

type Listing = ApiRead<ApiSchemas["CustomerList"]>;

export const CUSTOMERS_API = "/goods-v1/sell/customers";

/** Customers > Customer List (store operations PRD §5.1, ST-CUS-1; ticket 16).
 *
 *  The customers who bought at the person's stores, newest buyer first, and a
 *  search by number or name. Each opens the customer's page. Needs a
 *  connection; offline it says so and keeps what was typed. */
export function CustomerListPage() {
  const [term, setTerm] = useState("");
  const [data, setData] = useState<Listing | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [loading, setLoading] = useState(false);
  const request = useRef(0);
  const searched = useRef("");

  const load = useCallback(async (q: string) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    searched.current = q;
    setLoading(true);
    setError("");
    try {
      const response = await api.get<Listing>(withQuery(CUSTOMERS_API, searchQuery(q)));
      if (mine !== request.current) return;
      setLost(false);
      setData(response.data);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      if (mine === request.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load("");
  }, [load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(searched.current);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  const offline = !online || lost;

  return (
    <div className="page-pad">
      <PageHeader
        title="Customers"
        lead="Find a customer by number or name, then open their page. Needs a connection."
      />
      {offline && (
        <p className="warn-note" data-testid="customers-offline" role="status">
          {CUSTOMERS_OFFLINE}
          {online && (
            <>
              {" "}
              <button
                type="button"
                className="btn"
                data-testid="customers-retry"
                onClick={() => void load(term.trim())}
              >
                Try again
              </button>
            </>
          )}
        </p>
      )}

      <div className="card section-card toolbar" data-testid="customers-search-bar">
        <label className="field" style={{ flex: 1 }}>
          <span>Number or name</span>
          <input
            className="input"
            autoComplete="off"
            placeholder="9876543210, or Sharma"
            data-testid="customers-search"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              void load(term.trim());
            }}
          />
        </label>
        <button
          type="button"
          className="btn btn-cta"
          disabled={loading || offline}
          data-testid="customers-search-go"
          onClick={() => void load(term.trim())}
        >
          <Search size={15} /> {loading ? "Searching…" : "Search"}
        </button>
      </div>

      {error && (
        <p className="warn-note" data-testid="customers-error">
          {error}
        </p>
      )}

      {data && data.customers.length === 0 && (
        <p className="muted-cell" data-testid="customers-empty">
          No customer at your stores matches that. Check the number, or search by name.
        </p>
      )}

      {data && data.customers.length > 0 && (
        <section className="card section-card">
          <p className="eyebrow">
            {data.customers.length === 1 ? "1 customer" : `${data.customers.length} customers`}
            {data.truncated ? " - the most recent. Search to narrow it." : ""}
          </p>
          <div className="table-wrap">
            <table className="data" data-testid="customers-rows">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Number</th>
                  <th>GSTIN</th>
                  <th className="num">Bills</th>
                  <th>Last bought</th>
                </tr>
              </thead>
              <tbody>
                {data.customers.map((row) => (
                  <tr key={row.id} data-testid={`customer-row-${row.mobile}`}>
                    <td>
                      <Link to={`/customers/${row.id}`} data-testid={`customer-open-${row.mobile}`}>
                        {row.name || "No name held"}
                      </Link>
                    </td>
                    <td className="mono">{row.mobile}</td>
                    <td className="mono">{row.gstin || "—"}</td>
                    <td className="num">{row.bills}</td>
                    <td>{row.last_bill_at ? formatDateTime(row.last_bill_at) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
