// @vitest-environment jsdom
import "fake-indexeddb/auto";

import { QueryObserver, type QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { flushDeviceCache, readDeviceCache, wipeDeviceCache } from "./deviceCache";
import { GIFT_VOUCHERS_API, GIFT_VOUCHER_LOOKUP_API } from "./giftVouchers";
import { withQuery } from "./query";
import {
  STALE_TIME,
  cacheScope,
  createQueryClient,
  invalidateAfterMutation,
  isFinancialUrl,
  isPersistableUrl,
  persistScope,
  queryClient as appQueryClient,
  queryKeyFor,
  referenceTouchedBy,
  staleTimeFor,
} from "./queryCache";

afterEach(async () => {
  await wipeDeviceCache();
  appQueryClient.clear();
});

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

const SCOPE = cacheScope({ userId: "101", storeId: 11, roles: ["owner"] });

/** A mounted read: an observer with a counting fetcher, already answered. */
async function mounted(client: QueryClient, url: string) {
  const fetches = { count: 0 };
  const observer = new QueryObserver(client, {
    queryKey: queryKeyFor(SCOPE, url),
    queryFn: async () => {
      fetches.count += 1;
      return [{ id: fetches.count }];
    },
    staleTime: staleTimeFor(url, "list"),
  });
  const unsubscribe = observer.subscribe(() => undefined);
  await vi.waitFor(() => expect(fetches.count).toBe(1));
  return { fetches, unsubscribe };
}

function seeded(client: QueryClient, url: string) {
  client.setQueryData(queryKeyFor(SCOPE, url), [{ id: 1 }]);
}

function present(client: QueryClient, url: string) {
  return client.getQueryCache().find({ queryKey: queryKeyFor(SCOPE, url) }) !== undefined;
}

function invalidated(client: QueryClient, url: string) {
  return client.getQueryState(queryKeyFor(SCOPE, url))?.isInvalidated;
}

describe("a write refreshes the reference data it touched, across families", () => {
  it("maps goods-v1 master and vendor writes to the reference reads", () => {
    expect(referenceTouchedBy("/goods-v1/masters/entities/5")).toEqual(["/masters/entities"]);
    expect(referenceTouchedBy("/goods-v1/masters/gstins/2/retire")).toEqual(["/masters/gstins"]);
    expect(referenceTouchedBy("/goods-v1/masters/stores/4/locations/9").sort()).toEqual([
      "/masters/locations",
      "/masters/stores",
    ]);
    expect(referenceTouchedBy("/goods-v1/masters/brands/3")).toEqual(["/masters/brands"]);
    expect(referenceTouchedBy("/goods-v1/masters/subbrands")).toEqual(["/masters/brands"]);
    expect(referenceTouchedBy("/goods-v1/vendors/7")).toEqual(["/vendors"]);
    expect(referenceTouchedBy("/vendors/7")).toEqual(["/vendors"]);
    expect(referenceTouchedBy("/masters/seasons/1")).toEqual(["/masters/seasons"]);
    // Not reference data: nothing extra.
    expect(referenceTouchedBy("/goods-v1/masters/brand-terms/decisions")).toEqual([]);
    expect(referenceTouchedBy("/goods-v1/masters/configurations")).toEqual([]);
    expect(referenceTouchedBy("/outbound/transfers/4/dispatch")).toEqual([]);
  });

  it("an Organisation write refetches the mounted /masters read and drops the rest", async () => {
    const client = createQueryClient();
    const entities = await mounted(client, "/masters/entities");
    seeded(client, "/masters/entities?include_inactive=1");
    seeded(client, "/masters/stores");
    invalidateAfterMutation("/goods-v1/masters/entities/5", client);
    // Mounted: asked again now, not only marked stale.
    await vi.waitFor(() => expect(entities.fetches.count).toBe(2));
    // Not on screen: dropped, so no superseded copy is shown or saved.
    expect(present(client, "/masters/entities?include_inactive=1")).toBe(false);
    // Reference data the write could not have touched stays as it was.
    expect(present(client, "/masters/stores")).toBe(true);
    expect(invalidated(client, "/masters/stores")).toBe(false);
    entities.unsubscribe();
  });

  it("a goods-v1 vendor write refreshes /vendors", async () => {
    const client = createQueryClient();
    const vendors = await mounted(client, "/vendors");
    seeded(client, "/vendors?include_inactive=1");
    invalidateAfterMutation("/goods-v1/vendors/7", client);
    await vi.waitFor(() => expect(vendors.fetches.count).toBe(2));
    expect(present(client, "/vendors?include_inactive=1")).toBe(false);
    vendors.unsubscribe();
  });

  it("a store's location write refreshes stores and locations", async () => {
    const client = createQueryClient();
    const locations = await mounted(client, "/masters/locations");
    seeded(client, "/masters/stores");
    invalidateAfterMutation("/goods-v1/masters/stores/4/locations", client);
    await vi.waitFor(() => expect(locations.fetches.count).toBe(2));
    expect(present(client, "/masters/stores")).toBe(false);
    locations.unsubscribe();
  });

  it("drops the superseded master from the device copy", async () => {
    const client = createQueryClient();
    await persistScope(SCOPE, client);
    seeded(client, "/masters/entities");
    seeded(client, "/masters/stores");
    flushDeviceCache();
    const savedUrls = async () =>
      ((await readDeviceCache())?.clientState.queries ?? []).map((q) => String(q.queryKey[1]));
    await vi.waitFor(async () =>
      expect((await savedUrls()).sort()).toEqual(["/masters/entities", "/masters/stores"]),
    );
    invalidateAfterMutation("/goods-v1/masters/entities", client);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedUrls()).toEqual(["/masters/stores"]));
  });
});

describe("a write refreshes what is on screen in every family", () => {
  it("refetches mounted reads of other families and only marks the rest stale", async () => {
    const client = createQueryClient();
    const bookings = await mounted(client, "/bookings");
    const stores = await mounted(client, "/masters/stores");
    seeded(client, "/goods-v1/arrivals");
    invalidateAfterMutation("/outbound/transfers/4/dispatch", client);
    await vi.waitFor(() => expect(bookings.fetches.count).toBe(2));
    expect(invalidated(client, "/goods-v1/arrivals")).toBe(true);
    // Reference data the write cannot have touched is not asked again.
    expect(stores.fetches.count).toBe(1);
    expect(invalidated(client, "/masters/stores")).toBe(false);
    bookings.unsubscribe();
    stores.unsubscribe();
  });
});

describe("screens newly read through the cache", () => {
  it("keeps history, the audit log, SOH rows and PT lines in memory only", () => {
    for (const url of [
      "/goods-v1/history/site/s1",
      "/goods-v1/masters/audit-log?person=7&store=11",
      "/goods-v1/ptmapper/soh-imports/9/rows?stocked=1&q=",
      "/goods-v1/ptmapper/files/4?line_cursor=c2",
    ]) {
      expect(isPersistableUrl(url)).toBe(false);
      expect(staleTimeFor(url, "list")).not.toBe(STALE_TIME.reference);
    }
  });
});
