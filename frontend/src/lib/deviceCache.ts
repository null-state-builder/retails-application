/**
 * The back office's copy of reference data *on the device* (app loading and
 * caching plan, Phase 2), so reopening the browser renders stores, brands,
 * seasons and vendors at once instead of asking for them again.
 *
 * Deliberately small and strict:
 * - One IndexedDB entry (`kdps-backoffice-cache` / `queries` / `client`),
 *   separate from the till's Dexie database, which this never touches.
 * - Only what the caller's `shouldPersistUrl` allows is written (`queryCache.ts`
 *   passes reference masters only: never money, lists, documents or
 *   approvals), and only entries under the scope being persisted.
 * - The entry is stamped `appVersion:scope`. A restore under any other person,
 *   unit, brand, role set or build refuses it and deletes it. Entries older
 *   than `maxAge` are dropped the same way.
 * - `wipeDeviceCache` (logout, expiry, password change, any scope change)
 *   stops persisting, drops a write still waiting, and deletes the entry.
 * - Phase 3 (owner decision 2026-10-30): vendor and legal-entity PAN, payment
 *   terms and any bank account details are never written to the device
 *   (`DEVICE_WITHHELD_FIELDS`). They are stripped from the copy on its way to
 *   IndexedDB, so the screen in memory keeps them; an entry that lost a field
 *   is saved already marked stale, so after a restore its screen shows the
 *   rest at once and asks the server again for the whole answer.
 */
import type { Query, QueryClient } from "@tanstack/react-query";
import {
  persistQueryClient,
  type PersistedClient,
  type Persister,
} from "@tanstack/react-query-persist-client";
import { createStore, del, get, set, type UseStore } from "idb-keyval";

export const DEVICE_DB = "kdps-backoffice-cache";
export const DEVICE_STORE = "queries";
export const DEVICE_KEY = "client";
/** Writes are coalesced: a burst of cache updates costs one IndexedDB write. */
export const WRITE_THROTTLE_MS = 1_000;
/** The shape of what is on the device. Bumped whenever what may be saved
 *  narrows, so a copy written under the old rules is refused and deleted.
 *  d2: PAN, payment terms and bank details withheld (Phase 3). */
export const DEVICE_FORMAT = "d2";

/** Fields that may be shown on screen but never saved on the device: vendor
 *  and legal-entity PAN, payment terms, and (should a master ever carry them)
 *  bank account details. Matched by exact key at any depth. */
export const DEVICE_WITHHELD_FIELDS: ReadonlySet<string> = new Set([
  "pan",
  "pan_number",
  "pan_no",
  "payment_terms",
  "payment_terms_days",
  "bank_account",
  "bank_account_number",
  "bank_account_no",
  "account_number",
  "ifsc",
  "ifsc_code",
]);

/** A copy of `value` without any withheld field, and whether one was there.
 *  Never mutates `value` (it is the live in-memory answer). */
export function withholdFields(value: unknown): { value: unknown; stripped: boolean } {
  if (Array.isArray(value)) {
    let stripped = false;
    const items = value.map((item) => {
      const out = withholdFields(item);
      stripped ||= out.stripped;
      return out.value;
    });
    return { value: items, stripped };
  }
  if (value !== null && typeof value === "object") {
    let stripped = false;
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      if (DEVICE_WITHHELD_FIELDS.has(key)) {
        stripped = true;
        continue;
      }
      const inner = withholdFields(item);
      stripped ||= inner.stripped;
      out[key] = inner.value;
    }
    return { value: out, stripped };
  }
  return { value, stripped: false };
}

/** The device form of a dehydrated client: every query's data without the
 *  withheld fields, and any query that lost one marked invalidated, so a
 *  restored copy never stands in for the full answer: its screen refetches as
 *  soon as it is shown. */
export function forDevice(client: PersistedClient): PersistedClient {
  const queries = client.clientState.queries.map((query) => {
    const { value, stripped } = withholdFields(query.state.data);
    if (!stripped) return query;
    return { ...query, state: { ...query.state, data: value, isInvalidated: true } };
  });
  return { ...client, clientState: { ...client.clientState, queries } };
}

let idbStore: UseStore | null = null;

function idb(): UseStore | null {
  if (typeof indexedDB === "undefined") return null;
  idbStore ??= createStore(DEVICE_DB, DEVICE_STORE);
  return idbStore;
}

export function busterFor(appVersion: string, scope: string): string {
  return `${DEVICE_FORMAT}:${appVersion}:${scope}`;
}

export interface DeviceCacheOptions {
  appVersion: string;
  maxAge: number;
  shouldPersistUrl: (url: string) => boolean;
}

/** Bumped by every stop/wipe: a write or restore from an older run is void. */
let generation = 0;
let pendingClient: PersistedClient | null = null;
let pendingTimer: ReturnType<typeof setTimeout> | null = null;
let active: {
  client: QueryClient;
  scope: string;
  stop: () => void;
  ready: Promise<void>;
} | null = null;

function dropPendingWrite(): void {
  if (pendingTimer !== null) clearTimeout(pendingTimer);
  pendingTimer = null;
  pendingClient = null;
}

function flush(gen: number): void {
  const client = pendingClient;
  dropPendingWrite();
  const store = idb();
  if (!client || !store || gen !== generation) return;
  // Stripped here, on the way to disk, so memory keeps the full answer.
  set(DEVICE_KEY, forDevice(client), store).catch(() => {
    // A full or blocked disk only costs the next cold start a fetch.
  });
}

function persistedUrl(query: { queryKey: readonly unknown[] }): string | null {
  const url = query.queryKey[1];
  return typeof url === "string" ? url : null;
}

function scopedPersister(gen: number, scope: string, options: DeviceCacheOptions): Persister {
  const buster = busterFor(options.appVersion, scope);
  return {
    persistClient(client) {
      if (gen !== generation) return;
      pendingClient = client;
      pendingTimer ??= setTimeout(() => flush(gen), WRITE_THROTTLE_MS);
    },
    async restoreClient() {
      const store = idb();
      if (!store) return undefined;
      const saved = await get<PersistedClient>(DEVICE_KEY, store);
      if (!saved || gen !== generation) return undefined;
      if (saved.buster !== buster) {
        // Someone else's (or another build's) copy: refuse it and remove it.
        await del(DEVICE_KEY, store);
        return undefined;
      }
      // The stamp already says whose copy this is; keep only entries that
      // agree with it and with the allowlist, whatever the stored file says.
      const queries = saved.clientState.queries.filter((q) => {
        const url = persistedUrl(q);
        return q.queryKey[0] === scope && url !== null && options.shouldPersistUrl(url);
      });
      // Withheld fields never reach memory from disk either, whatever the
      // stored file says.
      return forDevice({ ...saved, clientState: { mutations: [], queries } });
    },
    async removeClient() {
      dropPendingWrite();
      const store = idb();
      if (store) await del(DEVICE_KEY, store);
    },
  };
}

/** Restore this scope's saved reference data into `client`, then keep saving
 *  it. Resolves once the restore is done (or refused); never rejects. Calling
 *  it again for the scope and client already running is a no-op. */
export function startDeviceCache(
  client: QueryClient,
  scope: string,
  options: DeviceCacheOptions,
): Promise<void> {
  if (!scope) return Promise.resolve();
  if (active?.scope === scope && active.client === client) return active.ready;
  stopDeviceCache();
  if (!idb()) return Promise.resolve();
  const gen = generation;
  const shouldDehydrateQuery = (query: Query) => {
    const url = persistedUrl(query);
    return (
      query.state.status === "success" &&
      query.queryKey[0] === scope &&
      url !== null &&
      options.shouldPersistUrl(url)
    );
  };
  const [unsubscribe, restored] = persistQueryClient({
    queryClient: client,
    persister: scopedPersister(gen, scope, options),
    maxAge: options.maxAge,
    buster: busterFor(options.appVersion, scope),
    dehydrateOptions: { shouldDehydrateQuery, shouldDehydrateMutation: () => false },
  });
  const onHide = () => {
    if (document.visibilityState === "hidden") flush(gen);
  };
  const onPageHide = () => flush(gen);
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", onPageHide);
  }
  const ready = restored.catch(() => undefined);
  active = {
    client,
    scope,
    ready,
    stop: () => {
      unsubscribe();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onHide);
        window.removeEventListener("pagehide", onPageHide);
      }
    },
  };
  return ready;
}

/** The current scope's restore, for work that must run after it (the
 *  bootstrap compares against the restored `ref_version`). */
export function deviceCacheReady(): Promise<void> {
  return active?.ready ?? Promise.resolve();
}

/** Stop saving (memory stays as it is). Any write still waiting is dropped. */
export function stopDeviceCache(): void {
  generation += 1;
  dropPendingWrite();
  active?.stop();
  active = null;
}

/** Stop saving and delete what is on the device. Never rejects. */
export async function wipeDeviceCache(): Promise<void> {
  stopDeviceCache();
  const store = idb();
  if (!store) return;
  await del(DEVICE_KEY, store).catch(() => undefined);
}

/** Write what is waiting now (tests; the page also flushes when hidden). */
export function flushDeviceCache(): void {
  flush(generation);
}

/** What is on the device right now (tests and diagnostics). */
export async function readDeviceCache(): Promise<PersistedClient | undefined> {
  const store = idb();
  return store ? get<PersistedClient>(DEVICE_KEY, store) : undefined;
}
