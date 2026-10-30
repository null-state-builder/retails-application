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

let idbStore: UseStore | null = null;

function idb(): UseStore | null {
  if (typeof indexedDB === "undefined") return null;
  idbStore ??= createStore(DEVICE_DB, DEVICE_STORE);
  return idbStore;
}

export function busterFor(appVersion: string, scope: string): string {
  return `${appVersion}:${scope}`;
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
  set(DEVICE_KEY, client, store).catch(() => {
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
      return { ...saved, clientState: { mutations: [], queries } };
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
