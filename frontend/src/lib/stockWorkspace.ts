import type { Store } from "../auth/AuthContext";

export type StockWorkspaceView = "on-hand" | "availability" | "in-transit" | "quarantine";

/** The views Stock on hand offers (desktop redesign Q3): two read the stock
 *  projection, two the goods journal. A goods view is offered only to a person
 *  holding the goods grant its screen needs. */
export const STOCK_VIEWS: { key: StockWorkspaceView; label: string; goods: boolean }[] = [
  { key: "on-hand", label: "On hand", goods: false },
  { key: "availability", label: "Available by size", goods: false },
  { key: "in-transit", label: "In transit", goods: true },
  { key: "quarantine", label: "Quarantine", goods: true },
];
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
  if (view === "in-transit" || view === "quarantine") return view;
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
  } else if (params.get("view") === "quarantine") {
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
