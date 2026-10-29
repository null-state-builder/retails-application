import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";

import { allowedUnits, useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { OperationsPage } from "../components/OperationsPage";
import { hold } from "../lib/goodsScreen";
import { SearchBox } from "../components/SearchBox";
import {
  STOCK_VIEWS,
  stockBookmark,
  stockParams,
  stockSelection,
  stockView,
} from "../lib/stockWorkspace";
import StockOnHand from "./StockOnHand";
import CrossStoreSearch from "./CrossStoreSearch";
import { GoodsStockPage } from "./GoodsStock";

export interface StockWorkspaceFilter {
  storeId: string;
  storeCode: string;
  query: string;
}

export function StockBookmark() {
  const { pathname, search } = useLocation();
  return <Navigate replace to={stockBookmark(pathname, search)} />;
}

/** The one switch between Stock on hand's views (desktop redesign Q3). A
 *  goods-journal view is offered only to a person holding `stock.view`, the
 *  grant its own screen answers to. */
export function StockViews() {
  const { session } = useAuth();
  const [params] = useSearchParams();
  const view = stockView(params);
  const offered = STOCK_VIEWS.filter((v) => !v.goods || hold(session, "stock.view"));
  return (
    <div className="seg stock-views" aria-label="Stock view" data-testid="stock-workspace-views">
      {offered.map((v) => (
        <Link
          key={v.key}
          className={`seg-btn ${view === v.key ? "active" : ""}`}
          aria-current={view === v.key ? "page" : undefined}
          to={`?${stockParams(params, {
            tab: "stock",
            view: v.key === "on-hand" ? "" : v.key,
            find: "",
          })}`}
          data-testid={`stock-workspace-view-${v.key}`}
        >
          {v.label}
        </Link>
      ))}
    </div>
  );
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
  // The goods-journal views draw the goods stock screen itself, with its own
  // reads, filters and gate, under this page's switch.
  if ((view === "in-transit" || view === "quarantine") && hold(session, "stock.view")) {
    return <GoodsStockPage stockView={view} views={<StockViews />} />;
  }
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
        <StockViews />
      </div>
      <p className="muted stock-scope" data-testid="stock-workspace-scope">
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
