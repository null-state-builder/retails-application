import type { Store } from "../auth/AuthContext";

export type StockWorkspaceView =
  | "on-hand"
  | "availability"
  | "location"
  | "in-transit"
  | "quarantine";

/** The goods-journal views, which draw the goods stock screen itself. */
export const GOODS_STOCK_VIEWS: readonly StockWorkspaceView[] = [
  "location",
  "in-transit",
  "quarantine",
];

/** The views Stock on hand offers (desktop redesign Q3): two read the stock
 *  projection, three the goods journal. A goods view is offered only to a
 *  person holding one of the goods grants its screen opened for as a menu line
 *  (`grants`): Goods stock opened for `stock.view` or `stock.accept`, Damage &
 *  Quarantine for `stock.view` alone. No `grants` ⇒ the page's own gate. */
export const STOCK_VIEWS: { key: StockWorkspaceView; label: string; grants?: string[] }[] = [
  { key: "on-hand", label: "On hand" },
  { key: "availability", label: "Available by size" },
  // The goods journal's own on-hand and availability, by location and condition.
  { key: "location", label: "By location", grants: ["stock.view", "stock.accept"] },
  { key: "in-transit", label: "In transit", grants: ["stock.view", "stock.accept"] },
  { key: "quarantine", label: "Quarantine", grants: ["stock.view"] },
];

/** May a person holding `held` goods grants open this view? */
export function stockViewOpen(key: StockWorkspaceView, held: (grant: string) => boolean): boolean {
  const grants = STOCK_VIEWS.find((v) => v.key === key)?.grants;
  return !grants || grants.some(held);
}
export interface StockSelection {
  store: Store | null;
  invalid: boolean;
}

/** URL choices are filters over server context choices, never permission. A
 * missing bookmarked store stays unavailable instead of selecting another. */
export function stockSelection(
  params: URLSearchParams,
  activeStore: Store | null,
  stores: readonly Store[],
): StockSelection {
  if (params.get("scope") === "all") return { store: null, invalid: false };
  const id = params.get("site");
  const code = params.get("store");
  if (id || code) {
    const found = stores.find((store) => (id ? String(store.id) === id : store.code === code));
    return { store: found ?? null, invalid: !found };
  }
  if (params.get("tab") === "search") return { store: null, invalid: false };
  const found = activeStore && stores.find((store) => store.id === activeStore.id);
  return { store: found || (stores.length === 1 ? stores[0]! : null), invalid: false };
}

export function stockView(params: URLSearchParams): StockWorkspaceView {
  const view = params.get("view");
  // The old Damage & Quarantine tab is the Quarantine view now.
  if (params.get("tab") === "damage") return "quarantine";
  if (view === "location" || view === "in-transit" || view === "quarantine") return view;
  return view === "availability" || params.get("tab") === "search" ? "availability" : "on-hand";
}

/** Keep all filter intent when opening old stock bookmarks. Legacy search was
 * explicitly a network view; existing barcode/brand links also searched scope. */
export function stockBookmark(pathname: string, search: string): string {
  const params = new URLSearchParams(search);
  if (pathname === "/stock/search") {
    params.set("tab", "stock");
    params.set("view", "availability");
    if (!params.has("site") && !params.has("store")) params.set("scope", "all");
  } else if (["location", "in-transit", "quarantine"].includes(params.get("view") ?? "")) {
    params.set("tab", "stock");
  } else {
    params.set("tab", "stock");
    if ((params.has("sku") || params.has("brand")) && !params.has("site") && !params.has("store")) {
      params.set("scope", "all");
    }
  }
  return `/inventory?${params.toString()}`;
}

export function stockParams(
  params: URLSearchParams,
  changes: Record<string, string>,
): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const [key, value] of Object.entries(changes)) {
    if (value) next.set(key, value);
    else next.delete(key);
  }
  return next;
}
