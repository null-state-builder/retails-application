/**
 * The shared cache for reads that do not fit `useList` / `useDoc` (app loading
 * and caching plan, Phase 1).
 *
 * `useList` answers an array and `useDoc` one document. Plenty of screens read
 * something else — a report payload, a `{ summary, rows }` object, a paged
 * `{ items, next_cursor }` envelope, a count that is polled — and used to keep
 * it in their own `useState`, so they showed "Loading…" on every visit. These
 * hooks put those reads in the same `QueryClient`, under the same
 * `[scope, url]` key and the same staleness rules (`staleTimeFor`), so a
 * screen seen before renders at once and is refreshed behind it.
 *
 * Only GETs. A POST is an action, never a cached answer.
 */
import {
  keepPreviousData,
  useQuery,
  type Query,
  type QueryFunctionContext,
} from "@tanstack/react-query";
import { useCallback, useMemo } from "react";

import { useCacheScope } from "./hooks";
import { withQuery, type QueryParams } from "./query";
import { fetchUrl, staleTimeFor } from "./queryCache";

export interface CachedReadOptions<R, T> {
  /** Filters, folded into the URL the same way `useList` folds them: a blank
   *  value is not sent, and the key is the URL they produce. */
  params?: QueryParams | undefined;
  /** "doc" for one document or a dashboard count (fresh 10 s), "list" for a
   *  list (30 s). Reference, action-needed and money URLs override this. */
  kind?: "list" | "doc" | undefined;
  /** Shape the server's answer for the screen. The cache keeps the raw answer
   *  (so `useList`/`useDoc` on the same URL share it); this runs on render and
   *  must be a pure function of the answer. */
  select?: ((raw: R) => T) | undefined;
  /** Poll every this many milliseconds while the screen is shown. */
  refetchInterval?: number | false | undefined;
  /** Keep polling while the tab is hidden (what a `setInterval` used to do). */
  refetchIntervalInBackground?: boolean | undefined;
  /** Keep the previous filter's answer on screen while a new filter loads
   *  (`loading` stays true until the new answer lands). Default true. */
  keepPrevious?: boolean | undefined;
  /** Ask nothing while false (the screen is not allowed to, or not ready). */
  enabled?: boolean | undefined;
}

export interface CachedRead<T> {
  /** The answer for the current URL, or (while `loading`) the previous
   *  filter's answer; `undefined` before anything has arrived. */
  data: T | undefined;
  /** True only while what is shown is not yet the answer to the current URL:
   *  nothing has arrived, a failed read is being asked again, or the previous
   *  filter's answer is standing in. Never true for a quiet refresh. */
  loading: boolean;
  /** Any request in flight, including a refresh behind cached data. */
  fetching: boolean;
  /** The last refusal or failure, as thrown by axios. Not reported while it is
   *  being asked again with nothing on screen (that is `loading`). */
  error: unknown;
  /** When the shown answer arrived (ms since epoch), or null. */
  updatedAt: number | null;
  /** Ask again now; what is shown stays on screen meanwhile. */
  reload: () => void;
}

/** The lower-level form: a custom `queryFn` (for example following every page
 *  of a cursor-paged list) under `[scope, url, variant]`. The variant keeps an
 *  answer that is not the raw response to `url` apart from one that is. */
export function useCachedQuery<R, T = R>(
  url: string | null,
  queryFn: (url: string, context: QueryFunctionContext) => Promise<R>,
  options: CachedReadOptions<R, T> & { variant?: string | undefined } = {},
): CachedRead<T> {
  const scope = useCacheScope();
  const full = url === null || url === "" ? null : withQuery(url, options.params);
  const active = full !== null && options.enabled !== false;
  const keepPrevious = options.keepPrevious !== false;
  const key = options.variant ? [scope, full ?? "", options.variant] : [scope, full ?? ""];
  const query = useQuery<unknown, Error, unknown, readonly string[]>({
    queryKey: key,
    queryFn: (context) => queryFn(full as string, context),
    enabled: active,
    staleTime: staleTimeFor(full ?? "", options.kind ?? "list"),
    refetchInterval: options.refetchInterval ?? false,
    refetchIntervalInBackground: options.refetchIntervalInBackground ?? false,
    // As in `useList`: the previous filter's answer may stand in, but only
    // within one scope — never carried from one store or role into another.
    placeholderData: (
      previous: unknown,
      previousQuery?: Query<unknown, Error, unknown, readonly string[]>,
    ) =>
      keepPrevious && previousQuery?.queryKey[0] === scope ? keepPreviousData(previous) : undefined,
  });

  const raw = active ? (query.data as R | undefined) : undefined;
  // `select` runs on the cached answer, which is structurally shared, so the
  // shaped value keeps its identity until the answer actually changes.
  const { select } = options;
  const data = useMemo(
    () => (raw === undefined ? undefined : select ? select(raw) : (raw as unknown as T)),
    // `select` is usually an inline arrow; it is a pure function of `raw`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [raw],
  );

  const nothing = query.data === undefined;
  const loading =
    active && (query.isPlaceholderData || (nothing && (query.isPending || query.isFetching)));
  const retrying = nothing && query.isFetching;
  const error = active && query.isError && !retrying ? (query.error as unknown) : null;

  const { refetch } = query;
  const reload = useCallback(() => {
    if (active) void refetch({ cancelRefetch: false });
  }, [active, refetch]);

  return {
    data,
    loading,
    fetching: active && query.isFetching,
    error,
    updatedAt: active && !query.isPlaceholderData ? query.dataUpdatedAt || null : null,
    reload,
  };
}

/**
 * One GET, cached under `[scope, url]` like `useList` / `useDoc`, for answers
 * that are not a bare array or a plain document: report payloads, summaries,
 * counts, paged envelopes. Polling is `refetchInterval`.
 */
export function useCachedGet<R, T = R>(
  url: string | null,
  options: CachedReadOptions<R, T> = {},
): CachedRead<T> {
  return useCachedQuery<R, T>(url, (u, { signal }) => fetchUrl<R>(u, signal), options);
}
