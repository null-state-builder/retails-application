// @vitest-environment jsdom
import "fake-indexeddb/auto";

import { QueryClientProvider } from "@tanstack/react-query";
// Plain `createElement` rather than JSX: a `.test.ts` file is a test, not an
// API consumer, to the SO-03 frontend inventory scanner.
import { act, createElement as h, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth, type GoodsSession } from "../auth/AuthContext";
import { useSessionWarmup } from "../shell/useSessionWarmup";
import { api } from "./api";
import {
  BOOTSTRAP_URL,
  runBootstrap,
  seedBootstrap,
  warmUpSession,
  type BootstrapPayload,
} from "./bootstrap";
import { wipeDeviceCache } from "./deviceCache";
import {
  BOOTSTRAP_COUNTS_KEY,
  BOOTSTRAP_REF_VERSION_KEY,
  cacheScope,
  createQueryClient,
  queryClient as appQueryClient,
  queryKeyFor,
} from "./queryCache";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SCOPE = cacheScope({ userId: "101", storeId: 11, roles: ["store_person"] });
const OTHER = cacheScope({ userId: "202", storeId: 11, roles: ["store_person"] });

function payload(ref = "r1"): BootstrapPayload {
  return {
    contract_version: "bootstrap-v1",
    ref_version: ref,
    blocks: {
      "/masters/stores": [{ id: 11 }],
      "/masters/brands": [{ id: 5 }],
      "/vendors": [{ id: 7 }],
      "/masters/summary": { stores: 1 },
    },
    counts: { approvals: 3, alerts: 1 },
  };
}

let getCalls: string[];
let reply: (url: string) => unknown;

beforeEach(() => {
  getCalls = [];
  reply = () => payload();
  vi.spyOn(api, "get").mockImplementation((async (url: string) => {
    getCalls.push(url);
    const answer = reply(url);
    if (answer instanceof Error) throw answer;
    return { data: answer };
  }) as unknown as typeof api.get);
});

afterEach(async () => {
  vi.restoreAllMocks();
  await wipeDeviceCache();
  appQueryClient.clear();
  localStorage.clear();
});

describe("seeding", () => {
  it("writes each block under [scope, url], with counts and ref_version", async () => {
    const client = createQueryClient();
    expect(await runBootstrap(client, SCOPE)).toBe(true);
    expect(getCalls).toEqual([BOOTSTRAP_URL]);
    expect(client.getQueryData(queryKeyFor(SCOPE, "/masters/stores"))).toEqual([{ id: 11 }]);
    expect(client.getQueryData(queryKeyFor(SCOPE, "/masters/brands"))).toEqual([{ id: 5 }]);
    expect(client.getQueryData(queryKeyFor(SCOPE, "/vendors"))).toEqual([{ id: 7 }]);
    expect(client.getQueryData(queryKeyFor(SCOPE, "/masters/summary"))).toEqual({ stores: 1 });
    expect(client.getQueryData(queryKeyFor(SCOPE, BOOTSTRAP_COUNTS_KEY))).toEqual({
      approvals: 3,
      alerts: 1,
    });
    expect(client.getQueryData(queryKeyFor(SCOPE, BOOTSTRAP_REF_VERSION_KEY))).toBe("r1");
    // Nothing lands under anybody else's scope.
    expect(
      client
        .getQueryCache()
        .getAll()
        .every((q) => q.queryKey[0] === SCOPE),
    ).toBe(true);
  });

  it("a moved ref_version marks the scope's other reference entries stale", () => {
    const client = createQueryClient();
    seedBootstrap(client, SCOPE, payload("r1"));
    client.setQueryData(queryKeyFor(SCOPE, "/masters/stores?q=x"), [{ id: 11 }]);
    client.setQueryData(queryKeyFor(SCOPE, "/outbound/transfers"), []);
    client.setQueryData(queryKeyFor(OTHER, "/masters/stores?q=x"), [{ id: 99 }]);
    const invalidated = (scope: string, url: string) =>
      client.getQueryCache().find({ queryKey: queryKeyFor(scope, url) })?.state.isInvalidated;

    seedBootstrap(client, SCOPE, payload("r1"));
    expect(invalidated(SCOPE, "/masters/stores?q=x")).toBe(false);

    seedBootstrap(client, SCOPE, payload("r2"));
    expect(invalidated(SCOPE, "/masters/stores?q=x")).toBe(true);
    expect(invalidated(SCOPE, "/masters/stores")).toBe(false); // freshly seeded
    expect(invalidated(SCOPE, "/outbound/transfers")).toBe(false); // not reference
    expect(invalidated(OTHER, "/masters/stores?q=x")).toBe(false); // not this scope
    expect(client.getQueryData(queryKeyFor(SCOPE, BOOTSTRAP_REF_VERSION_KEY))).toBe("r2");
  });

  it("a failure is silent and seeds nothing", async () => {
    reply = () => Object.assign(new Error("500"), { response: { status: 500 } });
    const client = createQueryClient();
    await expect(runBootstrap(client, SCOPE)).resolves.toBe(false);
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });

  it("an answer for a scope that has since moved on is dropped", async () => {
    const client = createQueryClient();
    expect(await runBootstrap(client, SCOPE, () => false)).toBe(false);
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });

  it("then warms the landing screen's chunk and first list in idle time", async () => {
    reply = (url) => (url === BOOTSTRAP_URL ? payload() : [{ id: 1 }]);
    const client = createQueryClient();
    const preload = vi.fn(() => Promise.resolve());
    warmUpSession({
      client,
      scope: SCOPE,
      isCurrent: () => true,
      landing: () => ({ preload, listUrl: "/outbound/stocktakes" }),
    });
    await vi.waitFor(() => expect(preload).toHaveBeenCalled());
    await vi.waitFor(() =>
      expect(client.getQueryData(queryKeyFor(SCOPE, "/outbound/stocktakes"))).toEqual([{ id: 1 }]),
    );
    expect(getCalls).toEqual([BOOTSTRAP_URL, "/outbound/stocktakes"]);
  });
});

describe("sign-in", () => {
  let signedIn: GoodsSession | null;
  let auth: ReturnType<typeof useAuth>;
  let root: Root | null = null;

  function session(): GoodsSession {
    return {
      contract_version: "access-v2",
      policy_version: "p1",
      user: {
        id: "101",
        human_id: "101",
        display_name: "A",
        email: "a@x",
        must_change_password: false,
      },
      assignments: [
        {
          id: "as-1",
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

  function Shell() {
    const value = useAuth();
    useEffect(() => {
      auth = value;
    });
    auth = value;
    useSessionWarmup();
    return value.user ? h("span", { "data-testid": "home" }, value.user.full_name) : null;
  }

  beforeEach(() => {
    signedIn = null;
    vi.spyOn(api, "post").mockImplementation((async (url: string) => {
      if (url === "/auth/login") {
        signedIn = session();
        return { data: { ...signedIn, csrf_token: "t" } };
      }
      return { data: {} };
    }) as unknown as typeof api.post);
  });

  afterEach(() => {
    act(() => root?.unmount());
    root = null;
  });

  async function settle(ms = 50) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, ms));
    });
  }

  function mount() {
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() =>
      root!.render(
        h(QueryClientProvider, { client: appQueryClient }, h(AuthProvider, null, h(Shell))),
      ),
    );
    return host;
  }

  it("a failing bootstrap never blocks sign-in", async () => {
    reply = (url) => {
      if (url === "/auth/me") {
        return signedIn ?? Object.assign(new Error("401"), { response: { status: 401 } });
      }
      if (url === BOOTSTRAP_URL) {
        return Object.assign(new Error("503"), { response: { status: 503 } });
      }
      return { csrf_token: "t" };
    };
    const host = mount();
    await settle(350);
    await act(async () => {
      await auth.login("a", "pw");
    });
    await settle();
    expect(host.querySelector("[data-testid=home]")?.textContent).toBe("A");
    expect(getCalls).toContain(BOOTSTRAP_URL);
    const scope = cacheScope({ userId: "101", storeId: 11, roles: ["store_person"] });
    expect(appQueryClient.getQueryData(queryKeyFor(scope, "/masters/stores"))).toBeUndefined();
  });

  it("a successful sign-in seeds the cache under the person's scope", async () => {
    reply = (url) => {
      if (url === "/auth/me") {
        return signedIn ?? Object.assign(new Error("401"), { response: { status: 401 } });
      }
      if (url === BOOTSTRAP_URL) return payload();
      return [];
    };
    mount();
    await settle(350);
    await act(async () => {
      await auth.login("a", "pw");
    });
    const scope = cacheScope({ userId: "101", storeId: 11, roles: ["store_person"] });
    await vi.waitFor(() =>
      expect(appQueryClient.getQueryData(queryKeyFor(scope, "/masters/stores"))).toEqual([
        { id: 11 },
      ]),
    );
    expect(getCalls.filter((u) => u === BOOTSTRAP_URL)).toHaveLength(1);
  });
});
