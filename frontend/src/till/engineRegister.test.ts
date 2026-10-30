import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TillDb } from "./db";
import { TillEngine } from "./engine";
import type { CounterLock } from "./guard";
import type { TillTransport } from "./transport";
import type { RegisterPayload, TillIdentity } from "./types";

const state = vi.hoisted(() => ({ db: null as TillDb | null, meta: new Map<string, unknown>() }));
vi.mock("./db", async (original) => ({
  ...(await original<typeof import("./db")>()),
  tillDb: () => state.db,
}));
vi.mock("./sync", async (original) => ({
  ...(await original<typeof import("./sync")>()),
  renewAuthority: async () => true,
  reconcileRegister: async (): Promise<RegisterPayload> => ({
    fy: "2026-27",
    last_accepted_seq: 1040,
    holes: [1036, 1038],
    hole_count: 2,
    series_open: true,
  }),
}));

function device(): TillDb {
  const empty = { count: async () => 0, toArray: async () => [] };
  return {
    name: "register-device",
    meta: {
      get: async (key: string) =>
        state.meta.has(key) ? { key, value: state.meta.get(key) } : undefined,
      put: async (row: { key: string; value: unknown }) => state.meta.set(row.key, row.value),
    },
    items: empty,
    stock: empty,
    offers: empty,
    salespeople: empty,
    managers: empty,
    gstSlabs: empty,
    customers: empty,
    held: empty,
    consents: empty,
    queue: { orderBy: () => empty },
    onlineSubmissions: { filter: () => ({ first: async () => undefined }) },
  } as unknown as TillDb;
}

const identity: TillIdentity = {
  registered: true,
  counter_id: "3",
  series_prefix: "SHOP3",
  authority_until: null,
  authority_hours: 24,
  working_set_version: null,
  server_time: "2026-10-29T10:00:00Z",
  allocation_version: null,
};

function engineWith(registerTill: TillTransport["registerTill"], handover = vi.fn()) {
  const lock = { held: () => true, acquire: async () => true } as unknown as CounterLock;
  const transport = { registerTill, handover } as unknown as TillTransport;
  return { engine: new TillEngine("SHOP", transport, lock), handover };
}

describe("registering a replacement counter", () => {
  beforeEach(() => {
    state.meta = new Map();
    state.db = device();
  });

  it("builds the key-in list from the register without a second handover call", async () => {
    const registerTill = vi.fn(async () => identity);
    const { engine, handover } = engineWith(registerTill);

    await engine.registerTill({ replace: true, reason: "Old machine died" });

    expect(registerTill).toHaveBeenCalledOnce();
    expect(handover).not.toHaveBeenCalled();
    expect(engine.getSnapshot().handover).toMatchObject({
      resume_from_seq: 1041,
      unsynced_hint: [1036, 1038],
      hole_count: 2,
    });
  });

  it("leaves no key-in list for a first registration", async () => {
    const { engine } = engineWith(vi.fn(async () => identity));

    await engine.registerTill({ replace: false, reason: "" });

    expect(engine.getSnapshot().handover).toBeNull();
  });
});
