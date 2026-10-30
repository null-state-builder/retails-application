import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
import type { SyntheticEvent } from "react";

import { prefetchList, useCacheScope } from "../lib/hooks";
import { routeWarmup } from "../routes";

/**
 * Sidebar warm-up (caching plan Phase 1): when a pointer or keyboard focus
 * reaches a menu link, start fetching that screen's code chunk and, where the
 * route table knows it, its first list — so the click usually lands on a
 * screen that is already there. Attach the returned handler to the menu
 * container's `onPointerOver` and `onFocus`; it reads the link from the event.
 */
export function useRoutePrefetch() {
  const client = useQueryClient();
  const scope = useCacheScope();
  const last = useRef("");
  return useCallback(
    (event: SyntheticEvent) => {
      const link = (event.target as Element | null)?.closest?.("a[href]");
      const href = link?.getAttribute("href");
      if (!href || !href.startsWith("/") || href === last.current) return;
      last.current = href;
      const url = new URL(href, window.location.origin);
      const { preload, listUrl } = routeWarmup(url.pathname);
      preload?.().catch(() => {
        // A warm-up that fails is retried by the real open.
      });
      if (listUrl && !url.search) void prefetchList(client, scope, listUrl);
    },
    [client, scope],
  );
}
