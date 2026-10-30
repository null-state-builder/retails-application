/**
 * Cheap refreshes (app loading and caching plan, Phase 3).
 *
 * The server answers its hottest back-office reads with an `ETag` and
 * `Cache-Control: no-store, private` (`backend/core/conditional_get.py`).
 * `no-store` is deliberate: those bodies carry vendor PAN, payment terms and
 * stock values, which must not land in the browser's HTTP cache on disk (it
 * outlives a logout). So the browser never revalidates on its own, and this
 * module does it instead, in memory only:
 *
 * - a 200 with an `ETag` is remembered (full URL → ETag, body);
 * - the next GET of the same URL sends `If-None-Match`; a header set by the
 *   page puts the request in `no-store` cache mode, so the browser hands the
 *   304 through instead of answering it from a cache;
 * - a 304 is turned back into a 200 carrying the remembered body, so every
 *   caller (`useList`, the bootstrap, anything on `api.get`) sees exactly what
 *   it saw before, only cheaper. Each caller gets its own copy (the body is
 *   kept as JSON text), so sorting or editing the rows changes nothing kept;
 * - a 304 whose body is no longer remembered (evicted, or forgotten while the
 *   request was in flight) is never handed on empty: the request is replayed
 *   once without `If-None-Match` and that full answer is returned;
 * - the memory is capped by count (`REVALIDATE_LIMIT`) and by approximate
 *   size (`REVALIDATE_BYTE_LIMIT`), oldest first.
 *
 * The server's ETag covers who is asking, in which unit/brand, and the exact
 * body, so a 304 can only ever confirm the body remembered here (a different
 * context simply gets its own 200, which replaces it). The memory is also
 * emptied with the query cache on logout and any scope change
 * (`forgetRevalidation`, called from `queryCache.ts`). Nothing here touches
 * the service worker, which stays shell-only.
 */
import {
  AxiosHeaders,
  type AxiosInstance,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";

/** Bodies kept for revalidation. Small: the hot endpoints are a dozen URLs. */
export const REVALIDATE_LIMIT = 64;
/** Roughly how much remembered JSON (its text length) may be kept in all.
 *  The oldest bodies go first when a new one would pass it; a body bigger than
 *  this on its own is never remembered. */
export const REVALIDATE_BYTE_LIMIT = 4 * 1024 * 1024;

let countLimit = REVALIDATE_LIMIT;
let byteLimit = REVALIDATE_BYTE_LIMIT;

interface Remembered {
  etag: string;
  /** The body as JSON text: a frozen copy (nothing a caller does to the data
   *  it was handed can change it) whose length is its approximate size. */
  json: string;
}

const remembered = new Map<string, Remembered>();
let rememberedBytes = 0;
/** Bumped by `forgetRevalidation`: an answer to a request sent before the
 *  memory was emptied (logout, scope change) is never remembered. */
let generation = 0;

const KEY = Symbol("kdps-revalidate-key");
const GENERATION = Symbol("kdps-revalidate-generation");
const ACCEPT = Symbol("kdps-revalidate-accept");
/** A string key, not a symbol: it must survive axios's `mergeConfig` when the
 *  request is replayed. Marks a replay that must not be conditional again. */
const FRESH = "kdpsFreshRead";

type Tagged = InternalAxiosRequestConfig & {
  [KEY]?: string;
  [GENERATION]?: number;
  [ACCEPT]?: InternalAxiosRequestConfig["validateStatus"];
  [FRESH]?: boolean;
};

function headerOf(config: InternalAxiosRequestConfig, name: string): string {
  const value = config.headers?.get?.(name);
  return typeof value === "string" ? value : "";
}

function drop(key: string): void {
  const known = remembered.get(key);
  if (!known) return;
  rememberedBytes -= known.json.length;
  remembered.delete(key);
}

function remember(key: string, etag: string, data: unknown): void {
  drop(key);
  let json: string | undefined;
  try {
    json = JSON.stringify(data);
  } catch {
    json = undefined;
  }
  if (json === undefined || json.length > byteLimit) return;
  remembered.set(key, { etag, json });
  rememberedBytes += json.length;
  while (remembered.size > countLimit || rememberedBytes > byteLimit) {
    const oldest = remembered.keys().next().value;
    if (oldest === undefined) break;
    drop(oldest);
  }
}

/** Mark `key` as most recently used. */
function touch(key: string, entry: Remembered): void {
  remembered.delete(key);
  remembered.set(key, entry);
}

/** Forget every remembered body (logout, session expiry, scope change). */
export function forgetRevalidation(): void {
  remembered.clear();
  rememberedBytes = 0;
  generation += 1;
}

/** How many bodies are remembered (tests and diagnostics). */
export function revalidationSize(): number {
  return remembered.size;
}

/** Approximate size of the remembered bodies, in JSON characters. */
export function revalidationBytes(): number {
  return rememberedBytes;
}

/** Tests only: change the caps (`undefined` restores the defaults). */
export function setRevalidationLimits(limits?: { count?: number; bytes?: number }): void {
  countLimit = limits?.count ?? REVALIDATE_LIMIT;
  byteLimit = limits?.bytes ?? REVALIDATE_BYTE_LIMIT;
}

/** Add the revalidation interceptors to `client`. */
export function installConditionalGet(client: AxiosInstance): void {
  client.interceptors.request.use((config: Tagged) => {
    if (String(config.method ?? "get").toLowerCase() !== "get") return config;
    if (config.responseType && config.responseType !== "json") return config;
    if (headerOf(config, "If-None-Match")) return config;
    const key = client.getUri(config);
    config[KEY] = key;
    config[GENERATION] = generation;
    const known = config[FRESH] ? undefined : remembered.get(key);
    if (known) {
      config.headers.set("If-None-Match", known.etag);
      const accept = config.validateStatus;
      config[ACCEPT] = accept;
      config.validateStatus = (status) =>
        status === 304 || (accept ? accept(status) : status >= 200 && status < 300);
    }
    return config;
  });
  client.interceptors.response.use((response: AxiosResponse) => {
    const config = response.config as Tagged;
    const key = config[KEY];
    if (!key) return response;
    if (response.status === 304) {
      const known = remembered.get(key);
      if (!known) {
        // The body this request revalidated was evicted, or forgotten, while
        // it was in flight: never hand a caller an empty 304. Ask once more,
        // unconditionally, and answer with that.
        const headers = AxiosHeaders.from(config.headers);
        headers.delete("If-None-Match");
        const replay: Tagged = {
          ...config,
          headers,
          validateStatus: config[ACCEPT] ?? ((status: number) => status >= 200 && status < 300),
          [FRESH]: true,
        };
        delete replay[KEY];
        delete replay[GENERATION];
        delete replay[ACCEPT];
        return client.request(replay);
      }
      touch(key, known);
      // A fresh copy every time: a screen that sorts or edits the rows it was
      // given must not change what the next 304 hands out.
      return { ...response, status: 200, statusText: "OK", data: JSON.parse(known.json) };
    }
    const etag = response.headers?.["etag"];
    if (config[GENERATION] !== generation) return response;
    if (response.status === 200 && typeof etag === "string" && etag) {
      remember(key, etag, response.data);
    } else {
      drop(key);
    }
    return response;
  });
}
