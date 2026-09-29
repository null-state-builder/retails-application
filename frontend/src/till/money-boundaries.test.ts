import { describe, expect, it } from "vitest";
import { formatPaiseString, paiseStringToRupees } from "../lib/format";
import { rupeesToPaiseString } from "../lib/goodsPt";
import { addMonths, usesOf, validUntil } from "./giftVoucher";

describe("money and voucher boundaries", () => {
  it("keeps unknown money distinct from zero and refuses an incomplete amount", () => {
    expect(formatPaiseString(null)).toBe("Unknown");
    expect(formatPaiseString("0")).toBe("₹0");
    expect(rupeesToPaiseString("")).toBeNull();
    expect(rupeesToPaiseString("95.")).toBeUndefined();
    expect(rupeesToPaiseString("95.123")).toBeUndefined();
    expect(rupeesToPaiseString("95.12")).toBe("9512");
  });

  it("retains exact paise beyond JavaScript's safe integer", () => {
    expect(paiseStringToRupees("900719925474099312345")).toBe("9007199254740993123.45");
  });

  it("clamps a voucher anniversary to the month's last day and refuses a bad date", () => {
    expect(addMonths("2026-01-31", 1)).toBe("2026-02-28");
    expect(validUntil("2026-09-28", 12)).toBe("2027-09-27");
    expect(() => addMonths("2026-02-30", 1)).toThrow("Invalid voucher calendar day");
  });

  it("never lets voucher tender exceed the bill or its balance", () => {
    const vouchers = [
      { number: "A", code: "11", balance_paise: 700, valid_until: "2027-09-27" },
      { number: "B", code: "22", balance_paise: 500, valid_until: "2027-09-27" },
    ];
    expect(usesOf(vouchers, 1_000).map((v) => v.pays_paise)).toEqual([700, 300]);
  });
});
