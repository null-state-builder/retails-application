import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TillDb } from "./db";
import { TillEngine } from "./engine";
import type { CounterLock } from "./guard";
import { syncDown } from "./sync";
import { TillHttpError } from "./transport";
import type { TillTransport } from "./transport";
import type { DatasetPayload } from "./types";

const state = vi.hoisted(() => ({ db: null as TillDb | null }));
vi.mock("./db", async (original) => ({
  ...(await original<typeof import("./db")>()),
  tillDb: () => state.db,
}));
vi.mock("./sync", async (original) => ({
  ...(await original<typeof import("./sync")>()),
  syncDown: vi.fn(async () => undefined),
  reconcileRegister: async () => undefined,
  drainQueue: async () => ({ flags: [], accepted: 0, retryAfterMs: null }),
  pushHeld: async () => undefined,
}));

function cachedDevice(): TillDb {
  const meta = new Map<string, unknown>([
    ["sellingMode", "online_alpha"],
    ["syncedAt", "prior-session"],
    ["nextSeq", 4],
  ]);
  const empty = { count: async () => 0, toArray: async () => [] };
  return {
    name: "trusted-tenant-site-device",
    meta: {
      get: async (key: string) => (meta.has(key) ? { key, value: meta.get(key) } : undefined),
      put: async (row: { key: string; value: unknown }) => meta.set(row.key, row.value),
    },
    items: { count: async () => 20 },
    stock: { count: async () => 20 },
    offers: empty,
    salespeople: empty,
    managers: { count: async () => 1 },
    gstSlabs: empty,
    customers: empty,
    held: empty,
    consents: empty,
    queue: { orderBy: () => empty },
    onlineSubmissions: { filter: () => ({ first: async () => undefined }) },
  } as unknown as TillDb;
}

describe("alpha session projection boundary", () => {
  beforeEach(() => {
    state.db = cachedDevice();
    vi.mocked(syncDown).mockReset();
    vi.stubGlobal("navigator", { onLine: true });
    vi.stubGlobal("localStorage", { setItem: () => undefined, removeItem: () => undefined });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("keeps a prior cache unverified until this session's dataset arrives, and closes access on denial", async () => {
    const lock = { held: () => true, acquire: async () => true } as unknown as CounterLock;
    const engine = new TillEngine(
      "SHOP",
      {} as TillTransport,
      lock,
      "trusted-tenant-site-device",
      true,
    );
    vi.mocked(syncDown).mockRejectedValue(new TillHttpError(0, "NETWORK", "Offline"));
    await engine.syncNow();
    expect(engine.getSnapshot().datasetReady).toBe(true);
    expect(engine.getSnapshot().counts.items).toBe(20);
    expect(engine.getSnapshot().liveAccessVerified).toBe(false);

    vi.mocked(syncDown).mockResolvedValue({} as DatasetPayload);
    await engine.syncNow();
    expect(engine.getSnapshot().liveAccessVerified).toBe(true);

    vi.mocked(syncDown).mockRejectedValue(
      new TillHttpError(403, "FIELD_DENIED", "Customer field revoked"),
    );
    await engine.syncNow();
    expect(engine.getSnapshot().liveAccessVerified).toBe(false);
    expect(engine.getSnapshot().lastError).toBe("Customer field revoked");
    expect(await engine.db.items.count()).toBe(20);
  });
});
