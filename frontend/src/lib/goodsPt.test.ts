import { describe, expect, it } from "vitest";
import { isSuperseded, type ApprovalDTO } from "./goodsPt";

const request: ApprovalDTO = {
  id: "approval",
  subject_id: "document",
  subject_kind: "pt",
  subject_revision: 4,
  parent_document: null,
  reviewed_hash: "reviewed",
  state: "pending",
  maker: { id: "maker", name: "Fictional preparer" },
  required_roles: ["owner"],
  requested_action: "pt.approve.receipt",
  policy_version_id: "policy",
  site_id: "site",
  brand_id: "brand",
  title: "Review pinned receipt PT",
  requested_at: null,
  decision_at: null,
  reason_code: null,
  reconciliation: null,
};
const submitted = { state: "submitted", revision: 4, content_hash: "reviewed" };

describe("PT approval revision warning", () => {
  it("keeps an unchanged pending receipt review actionable", () => {
    expect(isSuperseded(request, submitted)).toBe(false);
    expect(isSuperseded(request, null)).toBe(false);
  });
  it.each([
    { ...submitted, revision: 5 },
    { ...submitted, content_hash: "later edit" },
    { ...submitted, state: "draft" },
    { ...submitted, state: "official" },
  ])("warns when a pending request no longer names the live submitted revision", (pt) => {
    expect(isSuperseded(request, pt)).toBe(true);
  });
  it.each(["approved", "rejected", "superseded"])(
    "preserves decided %s history without a stale pending-review warning",
    (state) => {
      expect(
        isSuperseded({ ...request, state }, { state: "official", revision: 5, content_hash: "" }),
      ).toBe(false);
    },
  );
  it("keeps pending reversal checks on the exact official revision without comparing wrapper hashes", () => {
    const reversal = { ...request, requested_action: "pt.reversal.approve" };
    expect(isSuperseded(reversal, { ...submitted, state: "official", content_hash: "" })).toBe(
      false,
    );
    expect(isSuperseded(reversal, { ...submitted, state: "official", revision: 5 })).toBe(true);
    expect(isSuperseded(reversal, submitted)).toBe(true);
  });
});
