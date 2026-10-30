/**
 * The back office's in-memory server cache (app loading and caching plan,
 * Phase 1).
 *
 * One `QueryClient` for the whole app. It only *renders*: every action still
 * goes to the server. The till keeps its own Dexie dataset and never reads
 * from this.
 *
 * Phase 3: the hottest reads are revalidated with `If-None-Match` from memory
 * (`conditionalGet.ts`), so a refresh that finds nothing changed costs a 304.
 *
 * Phase 2: an explicit allowlist — reference masters only (`isPersistableUrl`)
 * — is also kept on the device for 24 h by `deviceCache.ts`, stamped with the
 * app version and the scope, and deleted whenever the scope ends.
 *
 * Every entry is keyed `[scope, url]`, where scope is who is looking, in which
 * store/brand, under which roles. A different person, unit or role can never
 * read another's entry, and on top of that the whole client is emptied when
 * any of them changes (`resetQueryCache`, called from `AuthContext`).
 */
import {
  QueryClient,
  type DefaultError,
  type DefaultedQueryObserverOptions,
  type Query,
  type QueryKey,
  type QueryObserverOptions,
} from "@tanstack/react-query";
import type { InternalAxiosRequestConfig } from "axios";

import { api } from "./api";
import { forgetRevalidation, installConditionalGet } from "./conditionalGet";
import { onSessionEndedElsewhere } from "./crossTab";
import { startDeviceCache, wipeDeviceCache } from "./deviceCache";

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;

/** How long a copy on the device may be shown before it is thrown away. */
export const PERSIST_MAX_AGE = 24 * HOUR;

/** Cache entries the sign-in bootstrap writes that are not an endpoint's URL:
 *  the `ref_version` it last saw (kept on the device, so a reopened browser can
 *  tell whether its saved masters are current) and the action-needed counts
 *  (memory only). */
export const BOOTSTRAP_REF_VERSION_KEY = "bootstrap:ref_version";
export const BOOTSTRAP_COUNTS_KEY = "bootstrap:counts";

/** Which build wrote a device copy; a new deploy never reads an old one. */
export const APP_VERSION: string =
  typeof __KDPS_APP_VERSION__ === "string" ? __KDPS_APP_VERSION__ : "dev";

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
  // Open-to-buy budgets are cost figures, on bookings and on their own.
  /open-to-buy/,
  /\/bank(\/|\?|$)/,
  // Cash, vouchers, petty cash and bills are money.
  /^\/store\/cash-summary(\/|\?|$)/,
  /^\/sell\/gift-vouchers(\/|\?|$)/,
  /^\/sell\/petty-cash(\/|\?|$)/,
  /^\/sell\/sales(\/|\?|$)/,
  // Stock values: the ledger's on-hand, summary, postings and in transit.
  /^\/stockledger\/(on-hand|summary|entries|in-transit)(\/|\?|$)/,
  /^\/goods-v1\/stockledger\/quarantine(\/|\?|$)/,
  // Brand money: terms, claims, debit notes, invoice matching, SOR ageing.
  /^\/goods-v1\/masters\/brand-terms(\/|\?|$)/,
  /^\/goods-v1\/sell\/brand-claims(\/|\?|$)/,
  /^\/goods-v1\/inbound\/debit-notes(\/|\?|$)/,
  /\/three-way-match(\/|\?|$)/,
  /\/sor-ageing(\/|\?|$)/,
  /^\/brands\/(terms|claims|reports|sor-ageing)(\/|\?|$)/,
];

/** Lists people act from, kept as fresh as an open document. */
const ACTION_NEEDED = [
  /^\/approvals(\/|\?|$)/,
  /^\/goods-v1\/approvals\/inbox(\/|\?|$)/,
  /^\/alerts(\/|\?|$)/,
];

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

/** The device allowlist: reference masters, never anything financial, plus the
 *  bootstrap's `ref_version`. Lists, documents, approvals, alerts and counts
 *  are never written to disk. */
export function isPersistableUrl(url: string): boolean {
  if (url === BOOTSTRAP_REF_VERSION_KEY) return true;
  return isReferenceUrl(url) && !isFinancialUrl(url);
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

/** A device copy is kept 24 h, so the entries it holds must outlive the usual
 *  30 min in memory too — for those entries only. (`gcTime` only ever grows.) */
class BackOfficeQueryClient extends QueryClient {
  override defaultQueryOptions<
    TQueryFnData = unknown,
    TError = DefaultError,
    TData = TQueryFnData,
    TQueryData = TQueryFnData,
    TQueryKey extends QueryKey = QueryKey,
    TPageParam = never,
  >(
    options:
      | QueryObserverOptions<TQueryFnData, TError, TData, TQueryData, TQueryKey, TPageParam>
      | DefaultedQueryObserverOptions<TQueryFnData, TError, TData, TQueryData, TQueryKey>,
  ): DefaultedQueryObserverOptions<TQueryFnData, TError, TData, TQueryData, TQueryKey> {
    const defaulted = super.defaultQueryOptions(options);
    const url = (defaulted.queryKey as readonly unknown[] | undefined)?.[1];
    if (typeof url === "string" && isPersistableUrl(url)) {
      defaulted.gcTime = Math.max(defaulted.gcTime ?? 0, PERSIST_MAX_AGE);
    }
    return defaulted;
  }
}

export function createQueryClient(): QueryClient {
  return new BackOfficeQueryClient({
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

/** Logout, session expiry, password change, store/brand switch or role
 *  change: stop every request in flight, forget everything this tab was shown,
 *  and delete the copy on the device. */
export function resetQueryCache(client: QueryClient = queryClient): Promise<void> {
  // Stop saving before clearing, so the clear itself is never written back.
  const wiped = wipeDeviceCache();
  void client.cancelQueries();
  client.clear();
  forgetRevalidation();
  return wiped;
}

/** The first scope this tab opens (sign-in, or a reopened browser): empty the
 *  memory but keep the device copy — `persistScope` refuses it unless it was
 *  saved for exactly this scope and build. */
export function clearQueryMemory(client: QueryClient = queryClient): void {
  void client.cancelQueries();
  client.clear();
  forgetRevalidation();
}

/** Set when another tab ended the session: this tab saves nothing to the
 *  device again until a session starts in it (`resumePersistence`). */
let persistenceBlocked = false;

/** Whether this tab may save to the device (tests and diagnostics). */
export function isPersistenceBlocked(): boolean {
  return persistenceBlocked;
}

/** A session started in this tab (sign-in, or a restored session): it may
 *  save its own scope again. */
export function resumePersistence(): void {
  persistenceBlocked = false;
}

/** Another tab ended the session (logout, expiry, password change): stop
 *  every request, forget everything, delete the device copy, and refuse to
 *  write it again from this tab. */
export function resetAfterSessionEndedElsewhere(client: QueryClient = queryClient): Promise<void> {
  persistenceBlocked = true;
  return resetQueryCache(client);
}

onSessionEndedElsewhere(() => void resetAfterSessionEndedElsewhere());

/** Restore this scope's device copy and keep saving its allowlisted entries.
 *  Resolves when the restore is done or refused; never rejects. Does nothing
 *  after another tab ended the session, until one starts here again. */
export function persistScope(scope: string, client: QueryClient = queryClient): Promise<void> {
  if (persistenceBlocked) return Promise.resolve();
  return startDeviceCache(client, scope, {
    appVersion: APP_VERSION,
    maxAge: PERSIST_MAX_AGE,
    shouldPersistUrl: isPersistableUrl,
  });
}

/** The reference data a write may have changed, as `/masters/…` and
 *  `/vendors` prefixes. The goods-v1 master screens write through
 *  `/goods-v1/masters/<kind>` and `/goods-v1/vendors`, while the reference
 *  reads (and the sign-in bootstrap's blocks, which are seeded under the same
 *  URLs) live under `/masters/<kind>` and `/vendors`: the two families must
 *  move together. */
export function referenceTouchedBy(url: string): string[] {
  const parts = pathOf(url).split("/").filter(Boolean);
  const touched = new Set<string>();
  let rest = parts;
  if (rest[0] === "goods-v1") rest = rest.slice(1);
  if (rest[0] === "vendors") touched.add("/vendors");
  if (rest[0] === "masters" && rest[1]) {
    const kind = rest[1];
    if (kind === "vendors") touched.add("/vendors");
    else touched.add(`/masters/${kind === "subbrands" ? "brands" : kind}`);
    // A store's locations are written under the store.
    if (kind === "stores" && rest.includes("locations")) touched.add("/masters/locations");
  }
  return [...touched].filter((prefix) => isReferenceUrl(prefix));
}

/** After a successful change, the screen should end on what the server says.
 *  - Every mounted answer refetches now: the changed family's (first path
 *    segment: `/outbound/…`, `/goods-v1/…`) and every other family's too,
 *    except reference data the write cannot have touched. What is not on
 *    screen is marked stale and is re-asked (behind the cached copy) next
 *    time it is shown.
 *  - Reference data the write did touch (`referenceTouchedBy`, across the
 *    legacy and goods-v1 families): mounted reads refetch now, and copies not
 *    on screen are dropped, so the device copy loses them on its next write
 *    rather than restoring a superseded master for up to 24 h. */
export function invalidateAfterMutation(url: string, client: QueryClient = queryClient): void {
  const segment = pathOf(url).split("/").filter(Boolean)[0];
  if (!segment || segment === "auth") return;
  const family = `/${segment}`;
  const touched = referenceTouchedBy(url);
  const isTouched = (u: string) => touched.some((prefix) => under(pathOf(u), prefix));
  if (touched.length > 0) {
    client.removeQueries({
      predicate: (q) => {
        const u = urlOf(q);
        return u !== null && isTouched(u) && !q.isActive();
      },
    });
  }
  void client.invalidateQueries({
    predicate: (q) => {
      const u = urlOf(q);
      if (u === null) return false;
      if (isTouched(u)) return true;
      return under(pathOf(u), family) || !isReferenceUrl(u);
    },
  });
}

installConditionalGet(api);

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
