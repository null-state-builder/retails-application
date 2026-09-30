import { describe, expect, it } from "vitest";
import type { Store } from "../auth/AuthContext";
import { stockBookmark, stockParams, stockSelection, stockView } from "./stockWorkspace";

const stores: Store[] = [
  {
    id: 12,
    code: "FIRST",
    name: "First shop",
    store_type: "store",
    state_name: "",
    state_code: "",
    gstin_number: "",
  },
  {
    id: 27,
    code: "NEXT",
    name: "Next shop",
    store_type: "store",
    state_name: "",
    state_code: "",
    gstin_number: "",
  },
];

describe("one scoped stock workspace", () => {
  it("defaults to the active store and follows explicit bookmark scope across back navigation", () => {
    expect(stockSelection(new URLSearchParams(), stores[0]!, stores).store?.id).toBe(12);
    expect(stockSelection(new URLSearchParams("site=27"), stores[0]!, stores).store?.id).toBe(27);
    expect(stockSelection(new URLSearchParams("scope=all"), stores[0]!, stores)).toEqual({
      store: null,
      invalid: false,
    });
    expect(stockSelection(new URLSearchParams("site=12"), stores[1]!, stores).store?.id).toBe(12);
  });

  it("does not silently replace a revoked or unknown bookmarked store", () => {
    expect(stockSelection(new URLSearchParams("site=27"), stores[0]!, [stores[0]!])).toEqual({
      store: null,
      invalid: true,
    });
    expect(stockSelection(new URLSearchParams("store=FOREIGN"), stores[0]!, stores).invalid).toBe(
      true,
    );
    expect(stockSelection(new URLSearchParams(), stores[1]!, [stores[0]!]).store?.id).toBe(12);
  });

  it("keeps old network search, barcode, damage and filter bookmarks meaningful", () => {
    const search = new URL(
      stockBookmark("/stock/search", "?q=shirt&brand=Reviewed&group=store"),
      "https://proof.example",
    );
    expect(search.pathname).toBe("/inventory");
    expect(search.searchParams.get("scope")).toBe("all");
    expect(search.searchParams.get("q")).toBe("shirt");
    expect(search.searchParams.get("brand")).toBe("Reviewed");
    expect(stockView(search.searchParams)).toBe("availability");
    expect(stockBookmark("/stock", "?sku=000123&group=brand")).toContain("scope=all");
    expect(stockBookmark("/stock", "?view=quarantine&store=FIRST")).toBe(
      "/inventory?store=FIRST&tab=damage",
    );
    expect(stockSelection(new URLSearchParams("tab=search"), stores[0]!, stores).store).toBeNull();
  });

  it("changes view and query without erasing stable store or barcode intent", () => {
    const original = new URLSearchParams("site=27&sku=000123&brand=Reviewed&group=brand");
    const changed = stockParams(original, { q: "Shirt", view: "availability", tab: "stock" });
    expect(changed.get("site")).toBe("27");
    expect(changed.get("sku")).toBe("000123");
    expect(changed.get("group")).toBe("brand");
    expect(original.has("q")).toBe(false);
    expect(stockView(changed)).toBe("availability");
    expect(stockView(stockParams(changed, { view: "" }))).toBe("on-hand");
  });
});
