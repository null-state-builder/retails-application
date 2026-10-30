// @vitest-environment jsdom
import "fake-indexeddb/auto";

import { QueryClientProvider, QueryObserver } from "@tanstack/react-query";
// Plain `createElement` rather than JSX: a `.test.ts` file is a test, not an
// API consumer, to the SO-03 frontend inventory scanner.
import { act, createElement as h, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth, type GoodsSession } from "../auth/AuthContext";
import { api } from "./api";
import { busterFor, flushDeviceCache, readDeviceCache, wipeDeviceCache } from "./deviceCache";
import {
  APP_VERSION,
  BOOTSTRAP_COUNTS_KEY,
  BOOTSTRAP_REF_VERSION_KEY,
  PERSIST_MAX_AGE,
  cacheScope,
  createQueryClient,
  isPersistableUrl,
  persistScope,
  queryClient as appQueryClient,
  queryKeyFor,
  resetQueryCache,
} from "./queryCache";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const A = cacheScope({ userId: "101", storeId: 11, roles: ["store_person"] });
const A_OTHER_STORE = cacheScope({ userId: "101", storeId: 12, roles: ["store_person"] });
const A_OTHER_ROLE = cacheScope({ userId: "101", storeId: 11, roles: ["warehouse"] });
const B = cacheScope({ userId: "202", storeId: 11, roles: ["store_person"] });

const STORES = [{ id: 11, code: "X" }];

async function savedKeys(): Promise<string[]> {
  const saved = await readDeviceCache();
  return (saved?.clientState.queries ?? []).map(
    (q) => `${String(q.queryKey[0])} ${String(q.queryKey[1])}`,
  );
}

afterEach(async () => {
  await wipeDeviceCache();
  appQueryClient.clear();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("the device allowlist", () => {
  it("keeps reference masters and the bootstrap's ref_version only", () => {
    for (const url of ["/masters/stores", "/masters/brands", "/vendors", "/masters/seasons"]) {
      expect(isPersistableUrl(url)).toBe(true);
    }
    for (const url of [
      "/outbound/transfers",
      "/approvals/inbox",
      "/alerts",
      "/finledger/vendor/ageing",
      "/reports/sales",
      "/masters/summary",
      "/masters/store-targets",
      BOOTSTRAP_COUNTS_KEY,
    ]) {
      expect(isPersistableUrl(url)).toBe(false);
    }
    expect(isPersistableUrl(BOOTSTRAP_REF_VERSION_KEY)).toBe(true);
  });

  it("writes only allowlisted entries of the scope being saved", async () => {
    const client = createQueryClient();
    await persistScope(A, client);
    client.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    client.setQueryData(queryKeyFor(A, BOOTSTRAP_REF_VERSION_KEY), "v1");
    client.setQueryData(queryKeyFor(A, BOOTSTRAP_COUNTS_KEY), { approvals: 2 });
    client.setQueryData(queryKeyFor(A, "/outbound/transfers"), [{ id: 1 }]);
    client.setQueryData(queryKeyFor(A, "/approvals/inbox"), [{ id: 1, value_paise: 100 }]);
    client.setQueryData(queryKeyFor(A, "/finledger/vendor/ageing"), { total_due_paise: 9 });
    client.setQueryData(queryKeyFor(A, "/reports/sales"), { rows: [] });
    client.setQueryData(queryKeyFor(A, "/masters/summary"), { stores: 1 });
    client.setQueryData(queryKeyFor(B, "/masters/stores"), [{ id: 99 }]);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedKeys()).toHaveLength(2));
    expect((await savedKeys()).sort()).toEqual(
      [`${A} /masters/stores`, `${A} ${BOOTSTRAP_REF_VERSION_KEY}`].sort(),
    );
    const saved = await readDeviceCache();
    expect(saved?.buster).toBe(busterFor(APP_VERSION, A));
    expect(JSON.stringify(saved)).not.toMatch(/paise|transfers|approvals|"id":99/);
  });

  it("keeps allowlisted entries in memory as long as the device copy", () => {
    const client = createQueryClient();
    client.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    client.setQueryData(queryKeyFor(A, "/outbound/transfers"), []);
    const gc = (url: string) =>
      client.getQueryCache().find({ queryKey: queryKeyFor(A, url) })?.gcTime;
    expect(gc("/masters/stores")).toBeGreaterThanOrEqual(PERSIST_MAX_AGE);
    expect(gc("/outbound/transfers")).toBeLessThan(PERSIST_MAX_AGE);
  });
});

describe("restore", () => {
  async function saveFor(scope: string) {
    const writer = createQueryClient();
    await persistScope(scope, writer);
    writer.setQueryData(queryKeyFor(scope, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedKeys()).toHaveLength(1));
  }

  it("brings back the same person's copy in the same store and role", async () => {
    await saveFor(A);
    const reader = createQueryClient();
    await persistScope(A, reader);
    expect(reader.getQueryData(queryKeyFor(A, "/masters/stores"))).toEqual(STORES);
  });

  it.each([
    ["another person", B],
    ["another store", A_OTHER_STORE],
    ["another role", A_OTHER_ROLE],
  ])("refuses and deletes a copy saved for %s", async (_label, scope) => {
    await saveFor(A);
    // Forget which scope this module was saving, as a reopened browser would.
    const reader = createQueryClient();
    await persistScope(scope, reader);
    expect(reader.getQueryCache().getAll()).toHaveLength(0);
    expect(await readDeviceCache()).toBeUndefined();
  });

  it("refuses a copy written by another build", async () => {
    await saveFor(A);
    const saved = await readDeviceCache();
    const { stopDeviceCache } = await import("./deviceCache");
    stopDeviceCache();
    const { set, createStore } = await import("idb-keyval");
    await set(
      "client",
      { ...saved, buster: `old-build:${A}` },
      createStore("kdps-backoffice-cache", "queries"),
    );
    const reader = createQueryClient();
    await persistScope(A, reader);
    expect(reader.getQueryCache().getAll()).toHaveLength(0);
    expect(await readDeviceCache()).toBeUndefined();
  });
});

describe("data minimisation (Phase 3)", () => {
  const VENDORS = [
    {
      id: 7,
      code: "V7",
      name: "Vendor Seven",
      gstin: "10ABCDE1234F1Z5",
      city: "Patna",
      pan: "ABCDE1234F",
      payment_terms: "30 days from invoice",
      brands: [1],
    },
  ];
  const ENTITIES = [{ id: 1, code: "E1", name: "Entity", pan: "PQRSX6789K", is_active: true }];

  async function rawDeviceBytes(): Promise<string> {
    return JSON.stringify(await readDeviceCache());
  }

  it("never writes PAN or payment terms to the device, and memory keeps them", async () => {
    const client = createQueryClient();
    await persistScope(A, client);
    client.setQueryData(queryKeyFor(A, "/vendors"), VENDORS);
    client.setQueryData(queryKeyFor(A, "/masters/entities"), ENTITIES);
    client.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedKeys()).toHaveLength(3));

    const bytes = await rawDeviceBytes();
    expect(bytes).not.toMatch(/ABCDE1234F"|PQRSX6789K|30 days from invoice/);
    expect(bytes).not.toMatch(/"pan"|"payment_terms"/);
    // Names, codes, GSTIN and addresses may be kept.
    expect(bytes).toMatch(/Vendor Seven/);
    expect(bytes).toMatch(/10ABCDE1234F1Z5/);
    expect(bytes).toMatch(/Patna/);
    // The screen in memory still has everything.
    expect(client.getQueryData(queryKeyFor(A, "/vendors"))).toEqual(VENDORS);
    expect(client.getQueryData(queryKeyFor(A, "/masters/entities"))).toEqual(ENTITIES);

    const saved = await readDeviceCache();
    const state = (url: string) =>
      saved?.clientState.queries.find((q) => q.queryKey[1] === url)?.state;
    // An entry that lost a field is saved already marked stale; one that lost
    // nothing is saved as it was.
    expect(state("/vendors")?.isInvalidated).toBe(true);
    expect(state("/masters/entities")?.isInvalidated).toBe(true);
    expect(state("/masters/stores")?.isInvalidated).toBe(false);
  });

  it("strips withheld fields at any depth and leaves the input untouched", async () => {
    const { withholdFields } = await import("./deviceCache");
    const input = {
      results: [{ vendor: { name: "N", pan: "X", bank_account_number: "1", ifsc: "I" } }],
    };
    const before = JSON.stringify(input);
    const { value, stripped } = withholdFields(input);
    expect(stripped).toBe(true);
    expect(value).toEqual({ results: [{ vendor: { name: "N" } }] });
    expect(JSON.stringify(input)).toBe(before);
  });

  it("a restored stripped entry is shown at once and refetched in full", async () => {
    const writer = createQueryClient();
    await persistScope(A, writer);
    writer.setQueryData(queryKeyFor(A, "/vendors"), VENDORS);
    writer.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedKeys()).toHaveLength(2));
    const { stopDeviceCache } = await import("./deviceCache");
    stopDeviceCache();

    const reader = createQueryClient();
    await persistScope(A, reader);
    const restored = reader.getQueryData<typeof VENDORS>(queryKeyFor(A, "/vendors"));
    expect(restored?.[0]?.name).toBe("Vendor Seven");
    expect(restored?.[0]).not.toHaveProperty("pan");
    expect(restored?.[0]).not.toHaveProperty("payment_terms");

    const vendors = reader.getQueryCache().find({ queryKey: queryKeyFor(A, "/vendors") });
    const stores = reader.getQueryCache().find({ queryKey: queryKeyFor(A, "/masters/stores") });
    // Stale even under the hour-long reference staleTime, so a screen that
    // mounts refetches it; the untouched masters keep their staleTime.
    expect(vendors?.isStaleByTime(60 * 60 * 1_000)).toBe(true);
    expect(stores?.isStaleByTime(60 * 60 * 1_000)).toBe(false);

    const fetched = vi.fn(async () => VENDORS);
    const observer = new QueryObserver(reader, {
      queryKey: queryKeyFor(A, "/vendors"),
      queryFn: fetched,
      staleTime: 60 * 60 * 1_000,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    await vi.waitFor(() => expect(fetched).toHaveBeenCalledTimes(1));
    await vi.waitFor(() =>
      expect(reader.getQueryData(queryKeyFor(A, "/vendors"))).toEqual(VENDORS),
    );
    unsubscribe();
  });

  it("refuses and deletes a copy written before PAN and terms were withheld", async () => {
    const { stopDeviceCache } = await import("./deviceCache");
    stopDeviceCache();
    const { set, createStore } = await import("idb-keyval");
    await set(
      "client",
      {
        timestamp: Date.now(),
        // The Phase 2 stamp: build and scope, no device format.
        buster: `${APP_VERSION}:${A}`,
        clientState: {
          mutations: [],
          queries: [
            {
              queryKey: [A, "/vendors"],
              queryHash: JSON.stringify([A, "/vendors"]),
              state: { data: VENDORS, status: "success", dataUpdatedAt: Date.now() },
            },
          ],
        },
      },
      createStore("kdps-backoffice-cache", "queries"),
    );
    const reader = createQueryClient();
    await persistScope(A, reader);
    expect(reader.getQueryCache().getAll()).toHaveLength(0);
    expect(await readDeviceCache()).toBeUndefined();
  });
});

describe("wipe", () => {
  it("resetQueryCache deletes the device copy and drops a write still waiting", async () => {
    await persistScope(A);
    appQueryClient.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedKeys()).toHaveLength(1));
    // A change is waiting on the write throttle when the session ends.
    appQueryClient.setQueryData(queryKeyFor(A, "/masters/brands"), [{ id: 1 }]);
    await resetQueryCache();
    flushDeviceCache();
    await new Promise((r) => setTimeout(r, 1_200));
    expect(await readDeviceCache()).toBeUndefined();
  });
});

describe("through sign-in and sign-out", () => {
  let signedIn: GoodsSession | null;
  let auth: ReturnType<typeof useAuth>;
  let root: Root | null = null;

  function session(id: string): GoodsSession {
    return {
      contract_version: "access-v2",
      policy_version: "p1",
      user: { id, human_id: id, display_name: id, email: `${id}@x`, must_change_password: false },
      assignments: [
        {
          id: `as-${id}`,
          role_code: "store_person",
          all_sites: false,
          site_ids: [11],
          all_brands: true,
          brand_ids: [],
          effective_from: "2026-01-01",
          effective_to: null,
        },
      ],
      sections: [],
      capabilities: {},
      navigation: [],
      display_actions: [],
      sites: [],
      context_choices: {
        mode: "units",
        all_units: false,
        sites: [
          {
            id: 11,
            code: "X",
            name: "X",
            store_type: "store",
            state_name: "",
            state_code: "",
            gstin_number: "",
          },
        ],
        brands: [],
      },
      store_features: {},
      expires_at: "2099-01-01T00:00:00Z",
      step_up_valid_until: null,
    };
  }

  function Probe() {
    const value = useAuth();
    useEffect(() => {
      auth = value;
    });
    auth = value;
    return null;
  }

  beforeEach(() => {
    signedIn = null;
    vi.spyOn(api, "get").mockImplementation((async (url: string) => {
      if (url === "/auth/me") {
        if (signedIn) return { data: signedIn };
        throw Object.assign(new Error("401"), { response: { status: 401, data: {} } });
      }
      return { data: [] };
    }) as unknown as typeof api.get);
    vi.spyOn(api, "post").mockImplementation((async (url: string) => {
      if (url === "/auth/login") {
        signedIn = session("101");
        return { data: { ...signedIn, csrf_token: "t" } };
      }
      if (url === "/auth/logout") signedIn = null;
      return { data: {} };
    }) as unknown as typeof api.post);
  });

  afterEach(() => {
    act(() => root?.unmount());
    root = null;
  });

  async function mount() {
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() =>
      root!.render(
        h(QueryClientProvider, { client: appQueryClient }, h(AuthProvider, null, h(Probe))),
      ),
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 400));
    });
  }

  const scopeOf101 = cacheScope({ userId: "101", storeId: 11, roles: ["store_person"] });

  it("logout deletes the device copy", async () => {
    await mount();
    await act(async () => {
      await auth.login("a", "pw");
    });
    appQueryClient.setQueryData(queryKeyFor(scopeOf101, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () =>
      expect(await savedKeys()).toEqual([`${scopeOf101} /masters/stores`]),
    );

    await act(async () => {
      await auth.logout();
    });
    await vi.waitFor(async () => expect(await readDeviceCache()).toBeUndefined());
    expect(appQueryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it("a reopened browser restores its own copy before the first screen", async () => {
    signedIn = session("101");
    const writer = createQueryClient();
    await persistScope(scopeOf101, writer);
    writer.setQueryData(queryKeyFor(scopeOf101, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedKeys()).toHaveLength(1));
    // The tab closes: nothing is running any more.
    const { stopDeviceCache } = await import("./deviceCache");
    stopDeviceCache();

    await mount();
    expect(auth.user?.id).toBe(101);
    expect(appQueryClient.getQueryData(queryKeyFor(scopeOf101, "/masters/stores"))).toEqual(STORES);
  });

  it("a reopened browser under someone else refuses and deletes the copy", async () => {
    signedIn = session("202");
    const writer = createQueryClient();
    await persistScope(scopeOf101, writer);
    writer.setQueryData(queryKeyFor(scopeOf101, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedKeys()).toHaveLength(1));
    const { stopDeviceCache } = await import("./deviceCache");
    stopDeviceCache();

    await mount();
    expect(auth.user?.id).toBe(202);
    expect(
      appQueryClient
        .getQueryCache()
        .getAll()
        .some((q) => String(q.queryKey[0]).includes("u:101")),
    ).toBe(false);
    await vi.waitFor(async () => expect(await readDeviceCache()).toBeUndefined());
  });
});
