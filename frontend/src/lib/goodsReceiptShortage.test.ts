import { describe, expect, it } from "vitest";
import { receiptShortages } from "./goodsReceiptShortage";
import type { GrnCoverage } from "./goodsReceiving";

function receipt(states: [string, number][] = []): GrnCoverage {
  return {
    grn_header: {},
    lines: { items: [], next_cursor: null, total: 0 },
    count_history: [],
    damage_reports: [],
    invoice: {
      claim_revision_id: "claim",
      revision: 1,
      invoice_number: "FICTIONAL",
      invoice_date: null,
      evidence_id: null,
      lines: [
        {
          line_key: "claim-line",
          description: "Fictional shirt",
          style_code: null,
          sku_id: null,
          size_value_id: null,
          claimed_qty: 4,
          invoice_basic_paise: null,
          invoice_mrp_paise: null,
          remark: null,
        },
      ],
    },
    invoice_comparison: [
      {
        claim_line_key: "claim-line",
        claimed_qty: 4,
        counted_qty: 3,
        difference: -1,
        remaining_shortage_qty: 1,
        line_keys: ["physical-line"],
      },
    ],
    dispositions: states.map(([state, qty], index) => ({
      id: String(index),
      approval_request_id: null,
      kind: "accept_shortage",
      state,
      source_line_key: "claim-line",
      lot_id: null,
      qty,
      reason_code: "SHORT",
      damage_description: null,
      damage_evidence_ids: [],
      resolved_sku_id: null,
      source_value_evidence: { evidence_id: null, cost_paise: null, mrp_paise: null },
      tax_basis_evidence_id: null,
      requested_value_paise: null,
      frozen_value_paise: null,
      maker_id: "maker",
    })),
  };
}

describe("receipt shortage decisions", () => {
  it("offers the server's claim key and preserves the original claimed/count difference", () => {
    const coverage = receipt();
    expect(receiptShortages(coverage)[0]).toMatchObject({
      claim: { line_key: "claim-line" },
      remaining: 1,
    });
    expect(coverage.invoice_comparison[0]).toMatchObject({
      claimed_qty: 4,
      counted_qty: 3,
      difference: -1,
    });
  });
  // GRN history carries ApprovalRequest.State, distinct from the mutation
  // response's approval_pending prefix (inbound.grn_disposition_history).
  it("does not offer shortage waiting for independent review", () => {
    expect(receiptShortages(receipt([["pending", 1]]))).toEqual([]);
  });
  it("keeps accepted shortage at zero after reload, with or without a checked draft", () => {
    for (const coverage of [receipt(), receipt([["approved", 1]])]) {
      coverage.invoice_comparison[0]!.remaining_shortage_qty = 0;
      expect(receiptShortages(coverage)).toEqual([]);
    }
  });
  it("allows a rejected request again and keeps partial remaining quantity", () => {
    const coverage = receipt([
      ["rejected", 1],
      ["approved", 1],
    ]);
    coverage.invoice_comparison[0]!.difference = -3;
    coverage.invoice_comparison[0]!.remaining_shortage_qty = 2;
    expect(receiptShortages(coverage)[0]!.remaining).toBe(2);
  });
  it.each(["rejected", "superseded"])(
    "leaves %s requests available for a new decision",
    (state) => {
      expect(receiptShortages(receipt([[state, 1]]))[0]!.remaining).toBe(1);
    },
  );
  it("reserves only pending requests for the same shortage claim", () => {
    const coverage = receipt([
      ["pending", 1],
      ["pending", 1],
    ]);
    coverage.dispositions[0]!.kind = "hold_excess";
    coverage.dispositions[1]!.source_line_key = "another-claim";
    expect(receiptShortages(coverage)[0]!.remaining).toBe(1);
  });
  it("offers neither physical excess nor unpaired claim keys", () => {
    const coverage = receipt();
    coverage.invoice_comparison[0]!.difference = 1;
    expect(receiptShortages(coverage)).toEqual([]);
    coverage.invoice_comparison[0]!.difference = -1;
    coverage.invoice = null;
    expect(receiptShortages(coverage)).toEqual([]);
  });
});
