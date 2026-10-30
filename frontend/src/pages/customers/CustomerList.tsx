import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Search } from "lucide-react";

import { Note } from "../../components/Note";
import { PageHeader } from "../../components/PageHeader";
import "../../components/SearchBox.css";
import { EmptyState } from "../../components/States";
import { api, apiErrorMessage } from "../../lib/api";
import type { ApiRead, ApiSchemas } from "../../lib/api";
import { isConnectionLost } from "../../lib/auditLog";
import { CUSTOMERS_OFFLINE, searchQuery } from "../../lib/customerRights";
import { formatDateTime } from "../../lib/format";
import { withQuery } from "../../lib/query";
import "./Customers.css";

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

  const count = data?.customers.length ?? 0;

  return (
    <div className="page-pad">
      <PageHeader
        title="Customers"
        lead="Find a customer by number or name, then open their page. Needs a connection."
      />
      <div className="page">
        {offline && (
          <Note
            tone="warn"
            role="status"
            testId="customers-offline"
            action={
              online && (
                <button
                  type="button"
                  className="btn btn-sm"
                  data-testid="customers-retry"
                  onClick={() => void load(term.trim())}
                >
                  Try again
                </button>
              )
            }
          >
            {CUSTOMERS_OFFLINE}
          </Note>
        )}

        <div className="filter-bar customers-find" data-testid="customers-search-bar">
          <div className="searchbox">
            <Search size={16} aria-hidden="true" />
            <input
              type="search"
              aria-label="Number or name"
              autoComplete="off"
              placeholder="Number or name: 9876543210, or Sharma"
              data-testid="customers-search"
              value={term}
              onChange={(e) => setTerm(e.target.value)}
              onKeyDown={(e) => {
                if (e.key !== "Enter") return;
                e.preventDefault();
                void load(term.trim());
              }}
            />
          </div>
          <button
            type="button"
            className="btn btn-primary"
            disabled={loading || offline}
            data-testid="customers-search-go"
            onClick={() => void load(term.trim())}
          >
            {loading ? "Searching…" : "Search"}
          </button>
          {data && count > 0 && (
            <span className="customers-count" role="status">
              {count === 1 ? "1 customer" : `${count.toLocaleString("en-IN")} customers`}
              {searched.current ? ` at your stores match “${searched.current}”` : ""}
              {data.truncated ? ", the most recent. Search to narrow it." : ""}
            </span>
          )}
        </div>

        {error && (
          <Note tone="error" testId="customers-error">
            {error}
          </Note>
        )}

        {data && count === 0 && (
          <EmptyState testId="customers-empty">
            {searched.current
              ? "No customer at your stores matches that. Check the number, or search by name."
              : "No customer has bought at your stores yet."}
          </EmptyState>
        )}

        {data && count > 0 && (
          <div className="table-wrap capped">
            <table className="data" data-testid="customers-rows">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Number</th>
                  <th scope="col">GSTIN</th>
                  <th scope="col" className="num">
                    Bills
                  </th>
                  <th scope="col">Last bought</th>
                </tr>
              </thead>
              <tbody>
                {data.customers.map((row) => (
                  <tr key={row.id} data-testid={`customer-row-${row.mobile}`}>
                    <td>
                      <Link
                        className="customer-name"
                        to={`/customers/${row.id}`}
                        data-testid={`customer-open-${row.mobile}`}
                      >
                        {row.name || "No name held"}
                      </Link>
                    </td>
                    <td className="mono">{row.mobile}</td>
                    <td className={row.gstin ? "mono" : "muted-cell"}>{row.gstin || "None"}</td>
                    <td className="num">{row.bills}</td>
                    <td>{row.last_bill_at ? formatDateTime(row.last_bill_at) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/** The most recent customers, beside an open customer's page (desktop
 *  redesign, layout L): the next customer is one click away. The same scoped
 *  read the list makes; it opens nothing the list would not. */
export function CustomerQueue({ currentId }: { currentId: string }) {
  const [rows, setRows] = useState<Listing["customers"] | null>(null);
  useEffect(() => {
    let live = true;
    api
      .get<Listing>(withQuery(CUSTOMERS_API, searchQuery("")))
      .then((r) => live && setRows(r.data.customers))
      .catch(() => live && setRows(null));
    return () => {
      live = false;
    };
  }, []);
  if (!rows || rows.length === 0) return null;
  return (
    <nav className="box side-queue" aria-label="Recent customers" data-testid="customer-queue">
      <p className="side-queue-head">Recent customers</p>
      <ul>
        {rows.map((row) => (
          <li key={row.id}>
            <Link
              to={`/customers/${row.id}`}
              className={`side-queue-item${String(row.id) === currentId ? " active" : ""}`}
              aria-current={String(row.id) === currentId ? "page" : undefined}
            >
              <span className="side-queue-ref">{row.name || "No name held"}</span>
              <span className="side-queue-meta">
                <span className="mono">{row.mobile}</span>
                {row.bills} {row.bills === 1 ? "bill" : "bills"}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
