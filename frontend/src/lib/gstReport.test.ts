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
  it("treats a copy that was never built, main or second, as unknown", () => {
    expect(figuresUnknown([{ code: "NOT_BUILT" }])).toBe(true);
    expect(figuresUnknown([{ code: "SALES_NOT_BUILT" }])).toBe(true);
    expect(figuresUnknown([{ code: "LATE" }, { code: "SWITCHED_OFF" }])).toBe(false);
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
