import { keepPreviousData, useQuery, type QueryClient } from "@tanstack/react-query";
import { useCallback, useContext } from "react";

import { AuthContext } from "../auth/AuthContext";
import { withQuery, type QueryParams } from "./query";
import { cacheScope, fetchUrl, queryKeyFor, staleTimeFor } from "./queryCache";

const NO_ROWS: never[] = [];

/** The cache scope of whoever is looking right now: person, unit, brand and
 *  roles. Part of every query key, so one person's answers are never another's.
 *  Outside an `AuthProvider` (a bare test render) it is the anonymous scope. */
export function useCacheScope(): string {
  const auth = useContext(AuthContext);
  return cacheScope({
    userId: auth?.session?.user.id ?? auth?.user?.id ?? null,
    storeId: auth?.activeStore?.id ?? null,
    brandId: auth?.activeBrand?.id ?? null,
    roles: auth?.session?.assignments.map((a) => a.role_code) ?? [],
  });
}

/**
 * A list endpoint, refetched whenever its filters change.
 *
 * `params` is how a screen carries a search term (#102) or a tab without
 * building URLs itself — and it is compared by the URL it produces, not by
 * object identity, so an inline `{ q }` on every render costs one fetch, not a
 * loop. A blank value is dropped, so an empty search box asks the exact
 * question the screen asked before it had one.
 *
 * Cached per `[scope, url]`: a list seen before renders at once and is
 * refreshed behind it. `loading` is true only while what is shown is *not* the
 * answer to the current URL — nothing yet, or the previous filter's rows kept
 * on screen while the new filter loads — so "No request matches …" is never
 * said from the old filter's rows. `fetching` is any request in flight,
 * including a quiet background refresh.
 */
export function useList<T>(url: string | null, params?: QueryParams) {
  const scope = useCacheScope();
  const full = url === null ? null : withQuery(url, params);
  const query = useQuery({
    queryKey: queryKeyFor(scope, full ?? ""),
    queryFn: ({ signal }) => fetchUrl<T[]>(full as string, signal),
    enabled: full !== null,
    staleTime: staleTimeFor(full ?? "", "list"),
    // The previous filter's rows stay on screen while the new filter loads —
    // but only within one scope. The placeholder lives in the observer, not
    // the cache, so clearing the cache on a store or role switch would not
    // stop the old store's rows being carried into the new one's view.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[0] === scope ? keepPreviousData(previous) : undefined,
  });
  const { refetch } = query;
  const reload = useCallback(() => {
    if (full !== null) void refetch({ cancelRefetch: false });
  }, [full, refetch]);
  return {
    data: (query.data as T[] | undefined) ?? (NO_ROWS as T[]),
    loading: query.isPending || query.isPlaceholderData,
    fetching: query.isFetching,
    updatedAt: query.dataUpdatedAt || null,
    reload,
  };
}

export function useDoc<T>(url: string | null) {
  const scope = useCacheScope();
  const query = useQuery({
    queryKey: queryKeyFor(scope, url ?? ""),
    queryFn: ({ signal }) => fetchUrl<T>(url as string, signal),
    enabled: url !== null && url !== "",
    staleTime: staleTimeFor(url ?? "", "doc"),
  });
  const { refetch } = query;
  // `reload` for the screens that act on the document they are showing (ending a
  // booking, deciding an approval): refetch rather than patch local state, so
  // what is on screen is always what the server says. The cached copy stays on
  // screen while it does.
  const reload = useCallback(() => {
    if (url) void refetch({ cancelRefetch: false });
  }, [url, refetch]);
  return {
    data: (query.data as T | undefined) ?? null,
    // A refused document is an *answer*, not a crash. Out-of-scope reads 404 by
    // design (ADR-0003), so this is the ordinary path for "not yours": the
    // error is returned and `loading` goes false, so a screen rendering
    // `loading || !doc` says so instead of spinning for ever.
    loading: query.isPending,
    error: (query.error as unknown) ?? null,
    fetching: query.isFetching,
    updatedAt: query.dataUpdatedAt || null,
    reload,
  };
}

/** Warm the cache for a list a screen is about to ask for (sidebar hover). */
export function prefetchList(client: QueryClient, scope: string, url: string): Promise<void> {
  return client.prefetchQuery({
    queryKey: queryKeyFor(scope, url),
    queryFn: ({ signal }) => fetchUrl<unknown[]>(url, signal),
    staleTime: staleTimeFor(url, "list"),
  });
}
