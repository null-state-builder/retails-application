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
 *   it saw before, only cheaper.
 *
 * The server's ETag covers who is asking, in which unit/brand, and the exact
 * body, so a 304 can only ever confirm the body remembered here (a different
 * context simply gets its own 200, which replaces it). The memory is also
 * emptied with the query cache on logout and any scope change
 * (`forgetRevalidation`, called from `queryCache.ts`). Nothing here touches
 * the service worker, which stays shell-only.
 */
import type { AxiosInstance, AxiosResponse, InternalAxiosRequestConfig } from "axios";

/** Bodies kept for revalidation. Small: the hot endpoints are a dozen URLs. */
export const REVALIDATE_LIMIT = 64;

interface Remembered {
  etag: string;
  data: unknown;
}

const remembered = new Map<string, Remembered>();
const KEY = Symbol("kdps-revalidate-key");

type Tagged = InternalAxiosRequestConfig & { [KEY]?: string };

function headerOf(config: InternalAxiosRequestConfig, name: string): string {
  const value = config.headers?.get?.(name);
  return typeof value === "string" ? value : "";
}

function remember(key: string, entry: Remembered): void {
  remembered.delete(key);
  remembered.set(key, entry);
  while (remembered.size > REVALIDATE_LIMIT) {
    const oldest = remembered.keys().next().value;
    if (oldest === undefined) break;
    remembered.delete(oldest);
  }
}

/** Forget every remembered body (logout, session expiry, scope change). */
export function forgetRevalidation(): void {
  remembered.clear();
}

/** How many bodies are remembered (tests and diagnostics). */
export function revalidationSize(): number {
  return remembered.size;
}

/** Add the revalidation interceptors to `client`. */
export function installConditionalGet(client: AxiosInstance): void {
  client.interceptors.request.use((config: Tagged) => {
    if (String(config.method ?? "get").toLowerCase() !== "get") return config;
    if (config.responseType && config.responseType !== "json") return config;
    if (headerOf(config, "If-None-Match")) return config;
    const key = client.getUri(config);
    config[KEY] = key;
    const known = remembered.get(key);
    if (known) {
      config.headers.set("If-None-Match", known.etag);
      const accept = config.validateStatus;
      config.validateStatus = (status) =>
        status === 304 || (accept ? accept(status) : status >= 200 && status < 300);
    }
    return config;
  });
  client.interceptors.response.use((response: AxiosResponse) => {
    const key = (response.config as Tagged)[KEY];
    if (!key) return response;
    if (response.status === 304) {
      const known = remembered.get(key);
      if (!known) return response;
      remember(key, known);
      return { ...response, status: 200, statusText: "OK", data: known.data };
    }
    const etag = response.headers?.["etag"];
    if (response.status === 200 && typeof etag === "string" && etag) {
      remember(key, { etag, data: response.data });
    } else {
      remembered.delete(key);
    }
    return response;
  });
}
