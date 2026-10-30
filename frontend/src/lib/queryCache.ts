/**
 * The back office's in-memory server cache (app loading and caching plan,
 * Phase 1).
 *
 * One `QueryClient` for the whole app. It only *renders*: every action still
 * goes to the server, and nothing here is written to disk. The till keeps its
 * own Dexie dataset and never reads from this.
 *
 * Every entry is keyed `[scope, url]`, where scope is who is looking, in which
 * store/brand, under which roles. A different person, unit or role can never
 * read another's entry, and on top of that the whole client is emptied when
 * any of them changes (`resetQueryCache`, called from `AuthContext`).
 */
import { QueryClient, type Query } from "@tanstack/react-query";
import type { InternalAxiosRequestConfig } from "axios";

import { api } from "./api";

const SECOND = 1_000;
const MINUTE = 60 * SECOND;

export const STALE_TIME = {
  /** Masters that change a few times a year: stores, locations, brands, seasons… */
  reference: 60 * MINUTE,
  list: 30 * SECOND,
  /** An open document, and the "what needs me" counts. */
  doc: 10 * SECOND,
  /** Cost, margin, money and reports: always asked again on sight. */
  financial: 0,
} as const;

/** Reference endpoints, by path. A trailing `/…` or query string still counts. */
const REFERENCE = [
  "/masters/stores",
  "/masters/locations",
  "/masters/brands",
  "/masters/seasons",
  "/masters/gstins",
  "/masters/entities",
  "/masters/categories",
  "/masters/tax",
  "/vendors",
  "/suppliers",
  "/tax",
];

/** Anything cost, margin or money: never trusted from memory without asking. */
const FINANCIAL = [
  /^\/finledger(\/|\?|$)/,
  /^\/reports(\/|\?|$)/,
  /^\/sell\/discounts(\/|\?|$)/,
  /partner-(dues|settlements)/,
  /margin/,
  /cost/,
  /payables/,
  /\/bank(\/|\?|$)/,
];

/** Lists people act from, kept as fresh as an open document. */
const ACTION_NEEDED = [/^\/approvals(\/|\?|$)/, /^\/alerts(\/|\?|$)/];

function pathOf(url: string): string {
  return url.split("?")[0] ?? url;
}

function under(path: string, prefix: string): boolean {
  return path === prefix || path.startsWith(`${prefix}/`);
}

export function isReferenceUrl(url: string): boolean {
  const path = pathOf(url);
  return REFERENCE.some((prefix) => under(path, prefix));
}

export function isFinancialUrl(url: string): boolean {
  return FINANCIAL.some((rule) => rule.test(url));
}

export function staleTimeFor(url: string, kind: "list" | "doc"): number {
  if (isFinancialUrl(url)) return STALE_TIME.financial;
  if (isReferenceUrl(url)) return STALE_TIME.reference;
  if (ACTION_NEEDED.some((rule) => rule.test(url))) return STALE_TIME.doc;
  return kind === "list" ? STALE_TIME.list : STALE_TIME.doc;
}

/** Who is looking, where, as what. Part of every query key. */
export interface CacheScopeInput {
  userId: string | number | null | undefined;
  storeId?: number | null;
  brandId?: number | null;
  roles?: readonly string[];
}

export function cacheScope({ userId, storeId, brandId, roles = [] }: CacheScopeInput): string {
  return [
    `u:${userId ?? "anon"}`,
    `s:${storeId ?? "-"}`,
    `b:${brandId ?? "-"}`,
    `r:${[...roles].sort().join(",")}`,
  ].join("|");
}

export function queryKeyFor(scope: string, url: string): readonly [string, string] {
  return [scope, url];
}

/** The one fetcher: the same axios client, interceptors and unit header as
 *  every other request. The signal lets a cleared cache abort its requests. */
export function fetchUrl<T>(url: string, signal?: AbortSignal): Promise<T> {
  return api.get<T>(url, signal ? { signal } : {}).then((r) => r.data);
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The hooks never retried; a refused document is an answer, not a blip.
        retry: false,
        refetchOnWindowFocus: true,
        staleTime: STALE_TIME.list,
        gcTime: 30 * MINUTE,
      },
    },
  });
}

export const queryClient = createQueryClient();

function urlOf(query: Query): string | null {
  const url = query.queryKey[1];
  return typeof url === "string" ? url : null;
}

/** Refresh every cached answer whose URL starts with `prefix` (any scope).
 *  Active screens refetch now; the rest refetch when next shown. */
export function invalidateUrl(prefix: string, client: QueryClient = queryClient): Promise<void> {
  return client.invalidateQueries({
    predicate: (q) => {
      const url = urlOf(q);
      return url !== null && (url === prefix || url.startsWith(prefix));
    },
  });
}

/** Logout, session expiry, store/brand switch or role change: stop every
 *  request in flight and forget everything this tab was shown. */
export function resetQueryCache(client: QueryClient = queryClient): void {
  void client.cancelQueries();
  client.clear();
}

/** After a successful change, the screen should end on what the server says.
 *  Lists under the same first path segment (`/outbound/…`, `/masters/…`,
 *  `/approvals/…`) refetch now if shown; every other non-reference answer is
 *  marked stale, so it is re-asked (behind the cached copy) next time it is
 *  shown. */
export function invalidateAfterMutation(url: string, client: QueryClient = queryClient): void {
  const segment = pathOf(url).split("/").filter(Boolean)[0];
  if (!segment || segment === "auth") return;
  const family = `/${segment}`;
  void client.invalidateQueries({
    predicate: (q) => {
      const u = urlOf(q);
      return u !== null && under(pathOf(u), family);
    },
  });
  void client.invalidateQueries({
    refetchType: "none",
    predicate: (q) => {
      const u = urlOf(q);
      return u !== null && !under(pathOf(u), family) && !isReferenceUrl(u);
    },
  });
}

const MUTATING = new Set(["post", "put", "patch", "delete"]);

function isMutation(
  config: InternalAxiosRequestConfig | undefined,
): config is InternalAxiosRequestConfig {
  return Boolean(config && MUTATING.has(String(config.method ?? "").toLowerCase()));
}

api.interceptors.response.use((response) => {
  if (isMutation(response.config) && response.config.url) {
    invalidateAfterMutation(response.config.url);
  }
  return response;
});
