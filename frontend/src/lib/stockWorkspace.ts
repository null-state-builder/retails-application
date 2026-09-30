import type { Store } from "../auth/AuthContext";

export type StockWorkspaceView = "on-hand" | "availability";
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
  return params.get("view") === "availability" || params.get("tab") === "search"
    ? "availability"
    : "on-hand";
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
    params.set("tab", "damage");
    params.delete("view");
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
