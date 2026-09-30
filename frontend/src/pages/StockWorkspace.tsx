import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";

import { allowedUnits, useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { OperationsPage } from "../components/OperationsPage";
import { hold } from "../lib/goodsScreen";
import { SearchBox } from "../components/SearchBox";
import { stockBookmark, stockParams, stockSelection, stockView } from "../lib/stockWorkspace";
import StockOnHand from "./StockOnHand";
import CrossStoreSearch from "./CrossStoreSearch";

export interface StockWorkspaceFilter {
  storeId: string;
  storeCode: string;
  query: string;
}

export function StockBookmark() {
  const { pathname, search } = useLocation();
  return <Navigate replace to={stockBookmark(pathname, search)} />;
}

export function StockWorkspace() {
  const { user, session, activeStore } = useAuth();
  const [params, setParams] = useSearchParams();
  const stores = user ? allowedUnits(user) : [];
  const selection = stockSelection(params, activeStore, stores);
  const view = stockView(params);
  const q = params.get("q") ?? "";
  const filters: StockWorkspaceFilter = {
    storeId: selection.store ? String(selection.store.id) : "",
    storeCode: selection.store?.code ?? "",
    query: q,
  };
  const scopedLabel = selection.store
    ? `${selection.store.code} · ${selection.store.name}`
    : "All authorised stores";

  return (
    <OperationsPage>
      <PageHeader
        title="Stock"
        lead="Check what is on hand, find a size, or request stock. Each view covers your current access."
        actions={
          hold(session, "transfer.allocate") ? (
            <Link className="btn" to="/goods/transfers/requests">
              Stock requests
            </Link>
          ) : undefined
        }
      />
      <div className="filter-bar" data-testid="stock-workspace-filters">
        <div className="field">
          <label htmlFor="stock-workspace-store">Store</label>
          <select
            id="stock-workspace-store"
            data-testid="stock-workspace-store"
            className="select"
            value={selection.invalid ? "unavailable" : filters.storeId || "all"}
            onChange={(event) =>
              setParams(
                stockParams(params, {
                  site: event.target.value === "all" ? "" : event.target.value,
                  scope: event.target.value === "all" ? "all" : "",
                  store: "",
                }),
              )
            }
          >
            {selection.invalid && <option value="unavailable">Bookmarked store unavailable</option>}
            <option value="all">All authorised stores</option>
            {stores.map((store) => (
              <option key={store.id} value={store.id}>
                {store.code} · {store.name}
              </option>
            ))}
          </select>
        </div>
        <SearchBox
          value={q}
          onChange={(value) => setParams(stockParams(params, { q: value }), { replace: true })}
          placeholder="Scan a tag or search item, design, brand"
          label="Search stock"
          testId="stock-workspace-search"
        />
      </div>
      <div className="seg" aria-label="Stock view" data-testid="stock-workspace-views">
        {(
          [
            ["on-hand", "On hand"],
            ["availability", "Available by size"],
          ] as const
        ).map(([key, label]) => (
          <Link
            key={key}
            className={`seg-btn ${view === key ? "active" : ""}`}
            aria-current={view === key ? "page" : undefined}
            to={`?${stockParams(params, { tab: "stock", view: key === "on-hand" ? "" : key })}`}
          >
            {label}
          </Link>
        ))}
      </div>
      <p className="muted" data-testid="stock-workspace-scope">
        {scopedLabel}. Totals include only the stock you may read; unavailable values are not zero.
      </p>
      {selection.invalid ? (
        <p className="warn-note" role="alert">
          This bookmarked store is no longer in your available store choices. Select an authorised
          store to continue.
        </p>
      ) : view === "availability" ? (
        <CrossStoreSearch key={`availability:${filters.storeId}`} workspace={filters} />
      ) : (
        <StockOnHand key={`on-hand:${filters.storeId}`} view="stock" workspace={filters} />
      )}
    </OperationsPage>
  );
}
