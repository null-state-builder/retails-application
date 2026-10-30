// @vitest-environment jsdom
import "fake-indexeddb/auto";

import { QueryClientProvider } from "@tanstack/react-query";
// Plain `createElement` rather than JSX: a `.test.ts` file is a test, not an
// API consumer, to the SO-03 frontend inventory scanner.
import { act, createElement as h, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** A stand-in `BroadcastChannel`: every instance with the same name is a tab;
 *  a post reaches every *other* instance, never the one that sent it. */
const bus = vi.hoisted(() => {
  const channels = new Set<FakeChannel>();
  class FakeChannel {
    name: string;
    onmessage: ((event: { data: unknown }) => void) | null = null;
    closed = false;
    constructor(name: string) {
      this.name = name;
      channels.add(this);
    }
    postMessage(data: unknown) {
      for (const other of channels) {
        if (other !== this && other.name === this.name && !other.closed) {
          other.onmessage?.({ data });
        }
      }
    }
    close() {
      this.closed = true;
      channels.delete(this);
    }
  }
  (globalThis as { BroadcastChannel?: unknown }).BroadcastChannel = FakeChannel;
  return { FakeChannel, channels };
});

import { AuthProvider, useAuth, type GoodsSession } from "../auth/AuthContext";
import { api } from "./api";
import {
  SESSION_CHANNEL,
  SESSION_ENDED_KEY,
  announceSessionEnded,
  onSessionEndedElsewhere,
} from "./crossTab";
import { flushDeviceCache, readDeviceCache, wipeDeviceCache } from "./deviceCache";
import {
  cacheScope,
  createQueryClient,
  isPersistenceBlocked,
  persistScope,
  queryClient as appQueryClient,
  queryKeyFor,
  resetAfterSessionEndedElsewhere,
  resumePersistence,
} from "./queryCache";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const A = cacheScope({ userId: "101", storeId: 11, roles: ["store_person"] });
const STORES = [{ id: 11, code: "X" }];

/** Another open tab, on the same channel. */
let otherTab: InstanceType<typeof bus.FakeChannel>;
let heard: unknown[];

beforeEach(() => {
  heard = [];
  otherTab = new bus.FakeChannel(SESSION_CHANNEL);
  otherTab.onmessage = (event) => heard.push(event.data);
});

afterEach(async () => {
  otherTab.close();
  resumePersistence();
  await wipeDeviceCache();
  appQueryClient.clear();
  vi.restoreAllMocks();
});

async function savedUrls(): Promise<string[]> {
  const saved = await readDeviceCache();
  return (saved?.clientState.queries ?? []).map((q) => String(q.queryKey[1]));
}

describe("telling the other tabs", () => {
  it("posts a session-ended message on the channel, which this tab does not hear", () => {
    const own = vi.fn();
    const off = onSessionEndedElsewhere(own);
    announceSessionEnded();
    off();
    expect(heard).toEqual([{ type: "session-ended", at: expect.any(Number) }]);
    expect(own).not.toHaveBeenCalled();
    // Our own post never wiped this tab.
    expect(isPersistenceBlocked()).toBe(false);
  });
});

describe("hearing another tab end the session", () => {
  it("empties memory, wipes the device copy and never writes it again", async () => {
    await persistScope(A, appQueryClient);
    appQueryClient.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedUrls()).toEqual(["/masters/stores"]));

    otherTab.postMessage({ type: "session-ended", at: Date.now() });

    expect(isPersistenceBlocked()).toBe(true);
    expect(appQueryClient.getQueryCache().getAll()).toHaveLength(0);
    await vi.waitFor(async () => expect(await readDeviceCache()).toBeUndefined());

    // Anything this tab still does afterwards stays in memory only.
    appQueryClient.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    await persistScope(A, appQueryClient);
    appQueryClient.setQueryData(queryKeyFor(A, "/masters/brands"), [{ id: 1 }]);
    flushDeviceCache();
    await new Promise((r) => setTimeout(r, 50));
    expect(await readDeviceCache()).toBeUndefined();
  });

  it("saves again only once a session starts in this tab", async () => {
    const client = createQueryClient();
    await resetAfterSessionEndedElsewhere(client);
    expect(isPersistenceBlocked()).toBe(true);
    resumePersistence();
    await persistScope(A, client);
    client.setQueryData(queryKeyFor(A, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedUrls()).toEqual(["/masters/stores"]));
  });

  it("ignores other messages on the channel", () => {
    otherTab.postMessage({ type: "something-else" });
    otherTab.postMessage("session-ended");
    expect(isPersistenceBlocked()).toBe(false);
  });
});

describe("without BroadcastChannel", () => {
  it("says it, and hears it, through a storage event", async () => {
    vi.resetModules();
    const saved = (globalThis as { BroadcastChannel?: unknown }).BroadcastChannel;
    (globalThis as { BroadcastChannel?: unknown }).BroadcastChannel = undefined;
    try {
      const fresh = await import("./crossTab");
      const writes: [string, string][] = [];
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (key, value) {
        writes.push([key, value]);
      });
      fresh.announceSessionEnded();
      expect(writes).toEqual([[SESSION_ENDED_KEY, expect.any(String)]]);

      const listener = vi.fn();
      fresh.onSessionEndedElsewhere(listener);
      window.dispatchEvent(new StorageEvent("storage", { key: "other", newValue: "1" }));
      window.dispatchEvent(new StorageEvent("storage", { key: SESSION_ENDED_KEY, newValue: null }));
      expect(listener).not.toHaveBeenCalled();
      window.dispatchEvent(
        new StorageEvent("storage", { key: SESSION_ENDED_KEY, newValue: "1730000000000" }),
      );
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      (globalThis as { BroadcastChannel?: unknown }).BroadcastChannel = saved;
    }
  });
});

describe("through sign-in and sign-out", () => {
  let signedIn: GoodsSession | null;
  let auth: ReturnType<typeof useAuth>;
  let root: Root | null = null;

  const session: GoodsSession = {
    contract_version: "access-v2",
    policy_version: "p1",
    user: {
      id: "101",
      human_id: "101",
      display_name: "101",
      email: "101@x",
      must_change_password: false,
    },
    assignments: [
      {
        id: "as-101",
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
        signedIn = session;
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

  async function mountSignedIn() {
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
    await act(async () => {
      await auth.login("a", "pw");
    });
    expect(auth.user).not.toBeNull();
  }

  it("a logout here tells the other tabs", async () => {
    await mountSignedIn();
    heard = [];
    await act(async () => {
      await auth.logout();
    });
    expect(heard).toEqual([{ type: "session-ended", at: expect.any(Number) }]);
  });

  it("a logout in another tab signs this tab out and leaves nothing on the device", async () => {
    await mountSignedIn();
    const scope = cacheScope({ userId: "101", storeId: 11, roles: ["store_person"] });
    appQueryClient.setQueryData(queryKeyFor(scope, "/masters/stores"), STORES);
    flushDeviceCache();
    await vi.waitFor(async () => expect(await savedUrls()).toEqual(["/masters/stores"]));
    heard = [];

    await act(async () => {
      otherTab.postMessage({ type: "session-ended", at: Date.now() });
    });

    expect(auth.user).toBeNull();
    // Not re-announced: the other tabs heard it already.
    expect(heard).toEqual([]);
    await vi.waitFor(async () => expect(await readDeviceCache()).toBeUndefined());
    expect(appQueryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
