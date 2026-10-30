// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { GIFT_VOUCHERS_API, GIFT_VOUCHER_LOOKUP_API } from "./giftVouchers";
import { withQuery } from "./query";
import {
  STALE_TIME,
  isFinancialUrl,
  isPersistableUrl,
  queryClient as appQueryClient,
  staleTimeFor,
} from "./queryCache";

afterEach(() => appQueryClient.clear());

/** The URLs the money and stock-value screens actually ask for, built the way
 *  each screen builds them (`useCachedGet` params go through `withQuery`). */
const MONEY_AND_VALUE_URLS = [
  // DaySummary, DayClose
  withQuery("/store/cash-summary", { date: "2026-10-30" }),
  withQuery("/store/cash-summary", { date: "2026-10-30", store: 11 }),
  // GiftVouchers
  withQuery(GIFT_VOUCHERS_API, { store: 11, show: "active", q: "GV-1" }),
  withQuery(GIFT_VOUCHER_LOOKUP_API, { code: "GV-1" }),
  // PettyCash
  withQuery("/sell/petty-cash", { site_id: 11 }),
  "/sell/petty-cash/spends/7",
  // Bills, CustomerSearch
  withQuery("/sell/sales", { from: "2026-10-01", to: "2026-10-30", q: "S-1" }),
  "/sell/sales/S-0001",
  // StockLedger, StockOnHand, Home stock tile
  withQuery("/stockledger/entries", { doc_number: "GRN-1", q: "x", page: 2, page_size: 50 }),
  "/stockledger/summary",
  "/stockledger/on-hand",
  withQuery("/stockledger/on-hand", { group_by: "sku", q: "shirt", store: "X" }),
  // OutboundInTransit: pieces and value in transit
  "/stockledger/in-transit",
  // GoodsStock, quarantine view
  "/goods-v1/stockledger/quarantine?site_id=11&basis=physical",
  "/goods-v1/stockledger/quarantine",
  // BrandTerms
  "/goods-v1/masters/brand-terms",
  "/goods-v1/masters/brand-terms/5",
  // BrandClaims
  withQuery("/goods-v1/sell/brand-claims", { month: "2026-10" }),
  "/goods-v1/sell/brand-claims/3",
  // DebitNotes
  withQuery("/goods-v1/inbound/debit-notes", { show: "open" }),
  "/goods-v1/inbound/debit-notes/4",
  // ThreeWayMatch
  "/goods-v1/inbound/arrivals/12/three-way-match",
  // SorAgeing
  withQuery("/goods-v1/stock/sor-ageing", { site_id: 11 }),
  "/brands/sor-ageing?site_id=11",
  // BrandReports (already under /reports)
  withQuery("/reports/brand-reports", { month: "2026-10", store: 11 }),
  // The /brands screens' own paths, should a read ever use them.
  "/brands/terms",
  "/brands/claims/3",
  "/brands/reports?month=2026-10",
];

describe("money and stock-value reads", () => {
  it.each(MONEY_AND_VALUE_URLS)("%s is financial: asked again on sight, never saved", (url) => {
    expect(isFinancialUrl(url)).toBe(true);
    expect(staleTimeFor(url, "list")).toBe(STALE_TIME.financial);
    expect(staleTimeFor(url, "doc")).toBe(STALE_TIME.financial);
    expect(isPersistableUrl(url)).toBe(false);
  });

  it("leaves reference masters and plain lists alone", () => {
    for (const url of [
      "/masters/stores",
      "/masters/brands",
      "/masters/brands?q=x",
      "/masters/seasons",
      "/masters/entities",
      "/vendors",
      "/vendors?include_inactive=1",
    ]) {
      expect(isFinancialUrl(url)).toBe(false);
      expect(staleTimeFor(url, "list")).toBe(STALE_TIME.reference);
      expect(isPersistableUrl(url)).toBe(true);
    }
    for (const url of [
      "/goods-v1/masters/brands",
      "/goods-v1/sell/salesperson-matches",
      "/sell/flags?status=open",
      "/goods-v1/stockledger/in-transit",
    ]) {
      expect(isFinancialUrl(url)).toBe(false);
    }
  });
});

describe("action-needed lists", () => {
  it("keeps both approvals inboxes as fresh as an open document", () => {
    expect(staleTimeFor("/approvals/inbox", "list")).toBe(STALE_TIME.doc);
    expect(staleTimeFor("/goods-v1/approvals/inbox", "list")).toBe(STALE_TIME.doc);
    expect(staleTimeFor("/goods-v1/approvals/inbox?limit=100", "list")).toBe(STALE_TIME.doc);
    expect(STALE_TIME.doc).toBe(10_000);
    expect(isPersistableUrl("/goods-v1/approvals/inbox?limit=100")).toBe(false);
    // Other goods-v1 approvals reads are ordinary lists.
    expect(staleTimeFor("/goods-v1/approvals/history", "list")).toBe(STALE_TIME.list);
  });
});
