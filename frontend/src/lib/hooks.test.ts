// @vitest-environment jsdom
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
// Plain `createElement` rather than JSX: a `.test.ts` file is a test, not an
// API consumer, to the SO-03 frontend inventory scanner.
import { Fragment, act, createElement as h, useEffect, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "../auth/AuthContext";
import { api } from "./api";
import { useDoc, useList } from "./hooks";
import {
  STALE_TIME,
  createQueryClient,
  invalidateAfterMutation,
  queryClient as appQueryClient,
  staleTimeFor,
} from "./queryCache";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Reply = { data: unknown } | { status: number };

/** A fake server: answers GETs by URL. A `{status}` answer is a refusal. */
let server: (url: string) => Reply;
let getCalls: string[];

function axiosError(status: number) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    isAxiosError: true,
    response: { status, data: {} },
  });
}

beforeEach(() => {
  getCalls = [];
  vi.spyOn(api, "get").mockImplementation((async (url: string) => {
    getCalls.push(url);
    const reply = server(url);
    if ("status" in reply) throw axiosError(reply.status);
    return { data: reply.data };
  }) as unknown as typeof api.get);
});

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.restoreAllMocks();
  appQueryClient.clear();
  localStorage.clear();
});

function mount(node: ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
}

function rerender(node: ReactNode) {
  act(() => root!.render(node));
}

function unmount() {
  act(() => root!.unmount());
  root = null;
}

/** Let resolved fetches land. */
async function settle() {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

type ListResult = ReturnType<typeof useList<{ id: number }>>;
type DocResult = ReturnType<typeof useDoc<{ id: number }>>;

function ListProbe({
  url,
  params,
  seen,
}: {
  url: string | null;
  params?: Record<string, string>;
  seen: ListResult[];
}) {
  const result = useList<{ id: number }>(url, params);
  seen.push(result);
  return h("span", { "data-testid": "rows" }, result.data.map((r) => r.id).join(","));
}

function DocProbe({ url, seen }: { url: string | null; seen: DocResult[] }) {
  const result = useDoc<{ id: number }>(url);
  seen.push(result);
  return null;
}

function withClient(client: QueryClient, node: ReactNode) {
  return h(QueryClientProvider, { client }, node);
}

describe("useList / useDoc over the query cache", () => {
  it("shows a list it has seen before at once, with no loading flash", async () => {
    server = () => ({ data: [{ id: 1 }, { id: 2 }] });
    const client = createQueryClient();
    const first: ListResult[] = [];
    mount(withClient(client, h(ListProbe, { url: "/outbound/transfers", seen: first })));
    expect(first[0]?.loading).toBe(true);
    await settle();
    expect(first.at(-1)?.loading).toBe(false);
    expect(first.at(-1)?.data).toEqual([{ id: 1 }, { id: 2 }]);
    unmount();

    const again: ListResult[] = [];
    mount(withClient(client, h(ListProbe, { url: "/outbound/transfers", seen: again })));
    // The very first render of the revisit already has the rows.
    expect(again[0]?.loading).toBe(false);
    expect(again[0]?.data).toEqual([{ id: 1 }, { id: 2 }]);
    expect(again.every((r) => !r.loading)).toBe(true);
    await settle();
    // Still fresh (30 s): no second request.
    expect(getCalls).toEqual(["/outbound/transfers"]);
  });

  it("refreshes a stale answer behind the cached one", async () => {
    let version = 1;
    server = () => ({ data: [{ id: version }] });
    const client = createQueryClient();
    const seen: ListResult[] = [];
    mount(withClient(client, h(ListProbe, { url: "/outbound/transfers", seen: seen })));
    await settle();
    expect(seen.at(-1)?.data).toEqual([{ id: 1 }]);
    unmount();

    version = 2;
    await client.invalidateQueries();
    const again: ListResult[] = [];
    mount(withClient(client, h(ListProbe, { url: "/outbound/transfers", seen: again })));
    expect(again[0]?.data).toEqual([{ id: 1 }]);
    await settle();
    expect(again.at(-1)?.data).toEqual([{ id: 2 }]);
    expect(again.every((r) => !r.loading)).toBe(true);
    expect(again.some((r) => r.fetching)).toBe(true);
  });

  it("reload() refetches without blanking what is shown", async () => {
    let version = 1;
    server = () => ({ data: [{ id: version }] });
    const seen: ListResult[] = [];
    mount(withClient(createQueryClient(), h(ListProbe, { url: "/outbound/rtvs", seen: seen })));
    await settle();
    version = 2;
    const before = seen.length;
    act(() => seen.at(-1)!.reload());
    await settle();
    expect(seen.at(-1)?.data).toEqual([{ id: 2 }]);
    expect(seen.slice(before).every((r) => !r.loading)).toBe(true);
  });

  it("compares params by the URL they produce and drops blank values", async () => {
    server = () => ({ data: [] });
    const client = createQueryClient();
    const seen: ListResult[] = [];
    mount(
      withClient(client, h(ListProbe, { url: "/outbound/rtvs", params: { q: "  " }, seen: seen })),
    );
    await settle();
    rerender(
      withClient(client, h(ListProbe, { url: "/outbound/rtvs", params: { q: "" }, seen: seen })),
    );
    rerender(withClient(client, h(ListProbe, { url: "/outbound/rtvs", seen: seen })));
    await settle();
    expect(getCalls).toEqual(["/outbound/rtvs"]);
  });

  it("keeps the previous filter's rows on screen but says they are not the answer yet", async () => {
    server = (url) => ({ data: url.includes("q=b") ? [{ id: 2 }] : [{ id: 1 }] });
    const client = createQueryClient();
    const seen: ListResult[] = [];
    mount(
      withClient(client, h(ListProbe, { url: "/outbound/rtvs", params: { q: "a" }, seen: seen })),
    );
    await settle();
    rerender(
      withClient(client, h(ListProbe, { url: "/outbound/rtvs", params: { q: "b" }, seen: seen })),
    );
    const switching = seen.at(-1)!;
    expect(switching.data).toEqual([{ id: 1 }]);
    expect(switching.loading).toBe(true);
    await settle();
    expect(seen.at(-1)?.data).toEqual([{ id: 2 }]);
    expect(seen.at(-1)?.loading).toBe(false);
  });

  it("asks nothing for a null URL and keeps loading", async () => {
    server = () => ({ data: [{ id: 1 }] });
    const lists: ListResult[] = [];
    const docs: DocResult[] = [];
    mount(
      withClient(
        createQueryClient(),
        h(
          Fragment,
          null,
          h(ListProbe, { url: null, seen: lists }),
          h(DocProbe, { url: null, seen: docs }),
        ),
      ),
    );
    await settle();
    expect(getCalls).toEqual([]);
    expect(lists.at(-1)).toMatchObject({ data: [], loading: true });
    expect(docs.at(-1)).toMatchObject({ data: null, loading: true, error: null });
  });

  it("treats a refused document as an answer: error, not a spinner", async () => {
    server = () => ({ status: 404 });
    const seen: DocResult[] = [];
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    try {
      mount(withClient(createQueryClient(), h(DocProbe, { url: "/bookings/9", seen: seen })));
      await settle();
    } finally {
      process.off("unhandledRejection", unhandled);
    }
    const last = seen.at(-1)!;
    expect(last.loading).toBe(false);
    expect(last.data).toBeNull();
    expect((last.error as { response?: { status: number } }).response?.status).toBe(404);
    expect(getCalls).toEqual(["/bookings/9"]); // no retries
    expect(unhandled).not.toHaveBeenCalled();
  });
});

describe("staleness and invalidation rules", () => {
  it("gives reference, list, document and money endpoints their own staleness", () => {
    expect(staleTimeFor("/masters/stores", "list")).toBe(STALE_TIME.reference);
    expect(staleTimeFor("/masters/brands?q=x", "list")).toBe(STALE_TIME.reference);
    expect(staleTimeFor("/vendors", "list")).toBe(STALE_TIME.reference);
    expect(staleTimeFor("/outbound/transfers", "list")).toBe(STALE_TIME.list);
    expect(staleTimeFor("/outbound/transfers/4", "doc")).toBe(STALE_TIME.doc);
    expect(staleTimeFor("/approvals/inbox", "list")).toBe(STALE_TIME.doc);
    expect(staleTimeFor("/finledger/cash/daily?date=2026-10-01", "doc")).toBe(0);
    expect(staleTimeFor("/outbound/partner-dues", "doc")).toBe(0);
    expect(staleTimeFor("/sell/discounts?from=a&to=b", "doc")).toBe(0);
  });

  it("refreshes the changed family now and marks the rest stale, but not reference data", async () => {
    const client = createQueryClient();
    const seed = (url: string) => client.setQueryData(["scope", url], [{ id: 1 }]);
    ["/outbound/transfers", "/outbound/transfers/4", "/bookings", "/masters/stores"].forEach(seed);
    invalidateAfterMutation("/outbound/transfers/4/dispatch", client);
    const stale = (url: string) => client.getQueryState(["scope", url])?.isInvalidated;
    expect(stale("/outbound/transfers")).toBe(true);
    expect(stale("/outbound/transfers/4")).toBe(true);
    expect(stale("/bookings")).toBe(true);
    expect(stale("/masters/stores")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Denial: one person's, store's or role's answers never reach another's view.

interface Site {
  id: number;
  code: string;
  name: string;
}

const STORE_X: Site = { id: 11, code: "X", name: "Store X" };
const STORE_Y: Site = { id: 12, code: "Y", name: "Store Y" };

function session(userId: string, role: string, sites: Site[]) {
  const stores = sites.map((s) => ({
    ...s,
    store_type: "store",
    state_name: "",
    state_code: "",
    gstin_number: "",
  }));
  return {
    contract_version: "access-v2",
    policy_version: "1",
    user: {
      id: userId,
      human_id: null,
      display_name: `User ${userId}`,
      email: `${userId}@example.test`,
      must_change_password: false,
    },
    assignments: [
      {
        id: `${userId}-a`,
        role_code: role,
        all_sites: false,
        site_ids: sites.map((s) => s.id),
        all_brands: false,
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
    context_choices: { mode: "units", all_units: false, sites: stores, brands: [] },
    store_features: {},
    expires_at: "2099-01-01T00:00:00Z",
    step_up_valid_until: null,
  };
}

describe("denial: no cached answer crosses a person, store or role", () => {
  let signedIn: ReturnType<typeof session> | null;
  let auth: ReturnType<typeof useAuth>;
  let accounts: Record<string, ReturnType<typeof session>>;

  function AuthProbe() {
    const value = useAuth();
    useEffect(() => {
      auth = value;
    });
    auth = value;
    return null;
  }

  function Screen() {
    const { user } = useAuth();
    return user ? h(ListProbe, { url: "/outbound/transfers", seen: [] }) : null;
  }

  beforeEach(() => {
    signedIn = null;
    accounts = {
      a: session("101", "store_person", [STORE_X, STORE_Y]),
      b: session("202", "warehouse", [STORE_X]),
    };
    // The server's answer depends on who is signed in and where: the rows name
    // their owner, so anything of A's on B's screen is visible in the DOM.
    server = (url) => {
      if (url === "/auth/me") return signedIn ? { data: signedIn } : { status: 401 };
      if (url === "/auth/csrf") return { data: { csrf_token: "t" } };
      const who = signedIn?.user.id ?? "nobody";
      const where = auth?.activeStore?.id ?? "all";
      return { data: [{ id: Number(`${who}${where}`) }] };
    };
    vi.spyOn(api, "post").mockImplementation((async (url: string, body: { email?: string }) => {
      if (url === "/auth/login") {
        signedIn = accounts[body.email ?? ""] ?? null;
        return { data: { ...signedIn, csrf_token: "t" } };
      }
      if (url === "/auth/logout") {
        signedIn = null;
        return { data: {} };
      }
      return { data: {} };
    }) as unknown as typeof api.post);
  });

  function app() {
    return h(
      QueryClientProvider,
      { client: appQueryClient },
      h(AuthProvider, null, h(AuthProbe), h(Screen)),
    );
  }

  const rows = () => host?.querySelector("[data-testid=rows]")?.textContent ?? null;
  const cachedRows = () =>
    appQueryClient
      .getQueryCache()
      .getAll()
      .map((q) => JSON.stringify(q.state.data ?? null));

  it("logout empties the cache; the next person sees only their own rows", async () => {
    mount(app());
    await settle();
    await act(async () => {
      await auth.login("a", "pw");
    });
    act(() => auth.setActiveStore(auth.user!.stores[0]!));
    await settle();
    expect(rows()).toBe("10111");
    expect(cachedRows().join()).toContain("10111");

    await act(async () => {
      await auth.logout();
    });
    await settle();
    expect(rows()).toBeNull();
    expect(appQueryClient.getQueryCache().getAll()).toHaveLength(0);

    await act(async () => {
      await auth.login("b", "pw");
    });
    await settle();
    expect(auth.activeStore?.id).toBe(STORE_X.id);
    expect(rows()).toBe("20211");
    expect(cachedRows().join()).not.toContain("101");
    expect(
      appQueryClient
        .getQueryCache()
        .getAll()
        .every((q) => !String(q.queryKey[0]).includes("u:101")),
    ).toBe(true);
  });

  it("a session that expires mid-screen empties the cache", async () => {
    mount(app());
    await settle();
    await act(async () => {
      await auth.login("a", "pw");
    });
    await settle();
    expect(appQueryClient.getQueryCache().getAll().length).toBeGreaterThan(0);
    act(() => {
      window.dispatchEvent(new Event("kdps:session-expired"));
    });
    await settle();
    expect(rows()).toBeNull();
    expect(appQueryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it("switching store empties the cache before the new store renders", async () => {
    mount(app());
    await settle();
    await act(async () => {
      await auth.login("a", "pw");
    });
    act(() => auth.setActiveStore(auth.user!.stores[0]!));
    await settle();
    expect(rows()).toBe("10111");

    act(() => auth.setActiveStore(auth.user!.stores[1]!));
    // Synchronously after the switch, nothing of store X remains.
    expect(cachedRows().join()).not.toContain("10111");
    expect(rows()).not.toBe("10111");
    await settle();
    expect(rows()).toBe("10112");
    expect(cachedRows().join()).not.toContain("10111");
  });

  it("a role change on re-read empties the cache", async () => {
    mount(app());
    await settle();
    await act(async () => {
      await auth.login("b", "pw");
    });
    await settle();
    expect(rows()).toBe("20211");

    signedIn = session("202", "warehouse_manager", [STORE_X]);
    accounts.b = signedIn;
    await act(async () => {
      auth.refreshSession(true);
    });
    await settle();
    const scopes = appQueryClient
      .getQueryCache()
      .getAll()
      .map((q) => String(q.queryKey[0]));
    expect(scopes.every((s) => s.includes("r:warehouse_manager"))).toBe(true);
  });
});
