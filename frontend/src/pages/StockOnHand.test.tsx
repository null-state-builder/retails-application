import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { StockValue, stockQuantity } from "./StockOnHand";

describe("resource-derived stock valuation", () => {
  it("shows an unavailable protected value without inventing zero", () => {
    const html = renderToStaticMarkup(<StockValue paise={undefined} />);
    expect(html).toContain("Unavailable");
    expect(html).not.toContain("₹");
  });

  it("distinguishes an authorised zero from a denied value", () => {
    expect(renderToStaticMarkup(<StockValue paise={0} />)).toContain("₹0");
    expect(renderToStaticMarkup(<StockValue paise={150000} />)).toContain("₹1,500");
  });

  it("does not present loading or denied quantity as an empty store", () => {
    expect(stockQuantity(undefined, true)).toBe("Loading…");
    expect(stockQuantity(undefined, false)).toBe("Unavailable");
    expect(stockQuantity(0, false)).toBe(0);
  });
});
