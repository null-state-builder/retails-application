import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { useAuth } from "../auth/AuthContext";
import { warmUpSession } from "../lib/bootstrap";
import { useCacheScope } from "../lib/hooks";
import { BOOTSTRAP_COUNTS_KEY, queryKeyFor } from "../lib/queryCache";
import { routeWarmup } from "../routes";

/** Scopes with a bootstrap in flight, so a remount or StrictMode's second
 *  effect does not ask twice. */
const inFlight = new Set<string>();

/**
 * Sign-in warm-up (caching plan Phase 2), mounted once in the shell: when a
 * session opens, or its unit/brand/role scope changes, fetch `/auth/bootstrap`
 * into the cache, then in idle time warm the screen being landed on.
 *
 * Runs once per scope per cache lifetime: the bootstrap's counts entry lives
 * in memory only, so it is there exactly when this scope was already warmed
 * since the last sign-in or scope change. A temporary-password session is
 * skipped (the server refuses it anyway). Failures are silent.
 */
export function useSessionWarmup(): void {
  const client = useQueryClient();
  const scope = useCacheScope();
  const { session } = useAuth();
  const signedIn = Boolean(session) && !session?.user.must_change_password;

  useEffect(() => {
    if (!signedIn || !scope) return;
    if (inFlight.has(scope)) return;
    if (client.getQueryState(queryKeyFor(scope, BOOTSTRAP_COUNTS_KEY))) return;
    inFlight.add(scope);
    let current = true;
    const cancel = warmUpSession({
      client,
      scope,
      isCurrent: () => current,
      landing: () => routeWarmup(window.location.pathname),
    });
    return () => {
      current = false;
      cancel();
      inFlight.delete(scope);
    };
  }, [client, scope, signedIn]);
}
