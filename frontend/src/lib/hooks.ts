import { useCallback, useEffect, useState } from "react";

import { api } from "./api";
import { withQuery, type QueryParams } from "./query";

/**
 * A list endpoint, refetched whenever its filters change.
 *
 * `params` is how a screen carries a search term (#102) or a tab without
 * building URLs itself — and it is compared by the URL it produces, not by
 * object identity, so an inline `{ q }` on every render costs one fetch, not a
 * loop. A blank value is dropped, so an empty search box asks the exact
 * question the screen asked before it had one.
 */
export function useList<T = any>(url: string | null, params?: QueryParams) {
  const [data, setData] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const full = url === null ? null : withQuery(url, params);
  const reload = useCallback(() => {
    if (!full) return;
    setLoading(true);
    api
      .get(full)
      .then((r) => setData(r.data))
      .finally(() => setLoading(false));
  }, [full]);
  useEffect(() => {
    reload();
  }, [reload]);
  return { data, loading, reload };
}

export function useDoc<T = any>(url: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    if (!url) return;
    setLoading(true);
    setError(null);
    api
      .get(url)
      .then((r) => live && setData(r.data))
      // A refused document is an *answer*, not a crash. Out-of-scope reads 404
      // by design (ADR-0003), so this is the ordinary path for "not yours", and
      // without a catch the rejection went unhandled: the console filled with
      // AxiosErrors while `loading` went false with `data` still null, leaving
      // every screen that renders `loading || !doc` spinning for ever on a
      // question the server had already answered.
      .catch((e) => live && setError(e))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [url, tick]);
  // `reload` for the screens that act on the document they are showing (ending a
  // booking, deciding an approval): refetch rather than patch local state, so
  // what is on screen is always what the server says.
  return { data, loading, error, reload: useCallback(() => setTick((t) => t + 1), []) };
}
