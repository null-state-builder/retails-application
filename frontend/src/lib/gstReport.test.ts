import { describe, expect, it } from "vitest";

import { figuresUnknown, gstCell, maskUnknownFigures } from "./gstReport";
import type { GstColumn } from "./gstReport";

const columns: GstColumn[] = [
  { key: "label", label: "Store", kind: "text" },
  { key: "bills", label: "Bills", kind: "number" },
  { key: "value_paise", label: "Value", kind: "money" },
];
const row = { key: "WRR", label: "The Wardrobe", bills: 0, value_paise: 0 };

describe("unknown report figures", () => {
  it("treats every figure as unknown only when the main copy was never built", () => {
    expect(figuresUnknown([{ code: "NOT_BUILT" }])).toBe(true);
    expect(figuresUnknown([{ code: "SALES_NOT_BUILT" }])).toBe(false);
    expect(figuresUnknown([{ code: "LATE" }, { code: "SWITCHED_OFF" }])).toBe(false);
  });

  it("blanks only what a missing second copy feeds, and never a key column", () => {
    const cols: GstColumn[] = [
      { key: "rate", label: "Rate %", kind: "rate" },
      { key: "day", label: "Date", kind: "date" },
      { key: "sold", label: "Sold", kind: "number" },
      { key: "on_hand", label: "On hand", kind: "number" },
    ];
    const r = { rate: 5, day: "2026-10-01", sold: 4, on_hand: 0 };
    const stockMissing = maskUnknownFigures(r, cols, [{ code: "STOCK_NOT_BUILT" }], {
      copies: { STOCK: ["on_hand"] },
    });
    expect(stockMissing).toEqual({ rate: 5, day: "2026-10-01", sold: 4, on_hand: null });
    const mainMissing = maskUnknownFigures(r, cols, [{ code: "NOT_BUILT" }], { keep: ["rate"] });
    expect(mainMissing).toEqual({ rate: 5, day: "2026-10-01", sold: null, on_hand: null });
    // A second copy the page names no columns for blanks nothing.
    expect(maskUnknownFigures(r, cols, [{ code: "SALES_NOT_BUILT" }])).toEqual(r);
  });

  it("prints a dash, not a nought, for every figure while the copy is missing", () => {
    const masked = maskUnknownFigures(row, columns, [{ code: "NOT_BUILT" }]);
    expect(masked.label).toBe("The Wardrobe");
    expect(gstCell(masked.bills, "number")).toBe("—");
    expect(gstCell(masked.value_paise, "money")).toBe("—");
  });

  it("leaves real noughts alone once the copy exists", () => {
    const kept = maskUnknownFigures(row, columns, [{ code: "LATE" }]);
    expect(gstCell(kept.bills, "number")).toBe("0");
  });

  it("groups whole numbers the Indian way", () => {
    expect(gstCell(1573400, "number")).toBe("15,73,400");
  });
});
