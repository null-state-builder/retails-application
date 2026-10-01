import { describe, expect, it } from "vitest";

import { sheetChecks, sheetTax, suggestionFills, type GridLine } from "./ptGrid";

const line = (cells: Record<string, string | number | null>, hsn = "6203"): GridLine =>
  ({ line_key: "k1", hsn, cells }) as unknown as GridLine;

describe("KDPS work sheet tax rule", () => {
  it("follows the sheet's thresholds on BASIC and MRP", () => {
    expect(sheetTax({ item: "SHIRT", basic_paise: "250000", mrp_paise: "262500" })).toEqual({
      input: 5,
      output: 5,
    });
    expect(sheetTax({ item: "SHIRT", basic_paise: "250001", mrp_paise: "262501" })).toEqual({
      input: 18,
      output: 18,
    });
  });

  it("applies the item and category exceptions first", () => {
    expect(sheetTax({ item: "WALLET", basic_paise: "10000", mrp_paise: "20000" }).input).toBe(18);
    expect(sheetTax({ item: "SAREE", basic_paise: "900000" }).input).toBe(5);
    expect(sheetTax({ item: "X", sub_category: "FABRIC", basic_paise: "900000" }).input).toBe(5);
    expect(sheetTax({ item: "TROLLEY", type: "LUGGAGE", basic_paise: "10000" }).input).toBe(18);
    expect(sheetTax({ basic_paise: "10000" })).toEqual({ input: null, output: null });
  });
});

describe("sheet checks", () => {
  it("flags tax and suggestion differences as hints", () => {
    const notes = sheetChecks(
      line({
        item: "WALLET",
        sub_category: "CASUAL WEAR",
        suggested_sub_category: "ACCESSORIES",
        suggested_type: "ACCESSORIES",
        type: "ACCESSORIES",
        basic_paise: "50000",
        mrp_paise: "99900",
        input_tax_pct: "5.00",
        output_tax_pct: "18.00",
      }),
    );
    expect(Object.keys(notes).sort()).toEqual(["INPUT TAX", "SUB CATEGORY"]);
    expect(notes["INPUT TAX"]).toContain("gives 18%");
  });

  it("names an ITEM with no suggestion", () => {
    expect(sheetChecks(line({ item: "GADGET" })).ITEM).toContain("No SUB CATEGORY");
  });

  it("fills only blank cells that have a suggestion", () => {
    const rows = [
      line({ suggested_sub_category: "CASUAL WEAR", suggested_type: "TOP WEAR", type: "TOP WEAR" }),
    ];
    expect(
      suggestionFills(rows, (l, column) =>
        String(l.cells?.[column === "TYPE" ? "type" : "sub_category"] ?? ""),
      ),
    ).toEqual([{ key: "k1", column: "SUB CATEGORY", value: "CASUAL WEAR" }]);
  });
});
