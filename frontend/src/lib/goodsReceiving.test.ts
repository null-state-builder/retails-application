import { describe, expect, it } from "vitest";
import { transferReceivingPath } from "./goodsReceiving";

const TRANSFER = "6b1ddca3-6421-4a17-870a-a63170065919";

describe("receiving transfer links", () => {
  it("opens the canonical transfer using the server's stable identity", () => {
    expect(transferReceivingPath({ kind: "transfer_dispatch", transfer_id: TRANSFER })).toBe(
      `/goods/transfers/${TRANSFER}`,
    );
  });
  it("does not invent a transfer from a dispatch ID, display label or another kind", () => {
    expect(transferReceivingPath({ kind: "transfer_dispatch" })).toBeNull();
    expect(
      transferReceivingPath({ kind: "transfer_dispatch", transfer_id: "FIRST → SECOND" }),
    ).toBeNull();
    expect(transferReceivingPath({ kind: "customer_return", transfer_id: TRANSFER })).toBeNull();
    expect(transferReceivingPath({ kind: "vendor_delivery", transfer_id: TRANSFER })).toBeNull();
  });
});
