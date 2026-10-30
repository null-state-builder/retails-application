/**
 * Sign-in warm-up (app loading and caching plan, Phase 2).
 *
 * Right after a sign-in or a restored session, one `GET /auth/bootstrap`
 * brings the reference masters this person can read and the action-needed
 * counts. Each block is keyed by the exact URL a screen's `useList` asks for,
 * so it is written straight into that screen's cache entry `[scope, url]` and
 * the screen opens with it. Then, when the browser is idle, the screen they are
 * landing on gets its code and first list warmed (`routeWarmup`).
 *
 * All of it is best effort: a failure is silent, never blocks sign-in, and the
 * screens simply ask for their own data as they did before.
 */
import type { QueryClient } from "@tanstack/react-query";

import { api } from "./api";
import { deviceCacheReady } from "./deviceCache";
import { prefetchList } from "./hooks";
import {
  BOOTSTRAP_COUNTS_KEY,
  BOOTSTRAP_REF_VERSION_KEY,
  isReferenceUrl,
  queryKeyFor,
} from "./queryCache";

export const BOOTSTRAP_URL = "/auth/bootstrap";

export interface BootstrapPayload {
  contract_version: string;
  ref_version: string;
  /** Exact frontend URL → that endpoint's own answer for this person. A block
   *  the endpoint would refuse is absent. */
  blocks: Record<string, unknown>;
  /** Action-needed counts (`approvals`, `alerts`); absent when not readable. */
  counts: Record<string, number>;
}

function isPayload(value: unknown): value is BootstrapPayload {
  if (!value || typeof value !== "object") return false;
  const v = value as Partial<BootstrapPayload>;
  return (
    typeof v.ref_version === "string" &&
    !!v.blocks &&
    typeof v.blocks === "object" &&
    !!v.counts &&
    typeof v.counts === "object"
  );
}

/** Write a bootstrap answer into the cache under `scope`. When `ref_version`
 *  moved since the copy this tab (or the device) holds, every other reference
 *  entry in this scope is marked stale, and refetched now if on screen. */
export function seedBootstrap(client: QueryClient, scope: string, payload: BootstrapPayload): void {
  const previous = client.getQueryData<string>(queryKeyFor(scope, BOOTSTRAP_REF_VERSION_KEY));
  const seeded = new Set<string>();
  for (const [url, data] of Object.entries(payload.blocks)) {
    if (!url.startsWith("/") || data === undefined) continue;
    client.setQueryData(queryKeyFor(scope, url), data);
    seeded.add(url);
  }
  client.setQueryData(queryKeyFor(scope, BOOTSTRAP_COUNTS_KEY), payload.counts);
  if (previous !== undefined && previous !== payload.ref_version) {
    void client.invalidateQueries({
      predicate: (q) => {
        const url = q.queryKey[1];
        return (
          q.queryKey[0] === scope &&
          typeof url === "string" &&
          isReferenceUrl(url) &&
          !seeded.has(url)
        );
      },
    });
  }
  client.setQueryData(queryKeyFor(scope, BOOTSTRAP_REF_VERSION_KEY), payload.ref_version);
}

/** Fetch and seed. Resolves `true` when seeded; `false` on any failure or when
 *  the scope moved on while it was in flight. Never rejects. */
export async function runBootstrap(
  client: QueryClient,
  scope: string,
  isCurrent: () => boolean = () => true,
): Promise<boolean> {
  try {
    // After the device restore, so the restored `ref_version` is what we
    // compare against and nothing restored overwrites a fresher answer.
    await deviceCacheReady();
    if (!isCurrent()) return false;
    // A literal path, so the SO-03 inventory links this caller to its endpoint.
    const { data: payload } = await api.get<unknown>("/auth/bootstrap");
    if (!isCurrent() || !isPayload(payload)) return false;
    seedBootstrap(client, scope, payload);
    return true;
  } catch {
    return false;
  }
}

/** Run `task` when the browser has nothing better to do. Returns a cancel. */
export function whenIdle(task: () => void, timeout = 2_000): () => void {
  if (typeof window !== "undefined" && typeof window.requestIdleCallback === "function") {
    const handle = window.requestIdleCallback(task, { timeout });
    return () => window.cancelIdleCallback(handle);
  }
  const handle = setTimeout(task, 1);
  return () => clearTimeout(handle);
}

export interface LandingWarmup {
  preload?: () => Promise<unknown>;
  listUrl?: string;
}

/** Bootstrap, then (in idle time) the landing screen's code chunk and first
 *  list. Returns a cancel for the idle step; every failure is swallowed. */
export function warmUpSession(options: {
  client: QueryClient;
  scope: string;
  isCurrent: () => boolean;
  landing: () => LandingWarmup;
}): () => void {
  const { client, scope, isCurrent, landing } = options;
  let cancelIdle: (() => void) | null = null;
  let cancelled = false;
  void runBootstrap(client, scope, isCurrent).then(() => {
    if (cancelled || !isCurrent()) return;
    cancelIdle = whenIdle(() => {
      if (cancelled || !isCurrent()) return;
      try {
        const { preload, listUrl } = landing();
        preload?.().catch(() => undefined);
        if (listUrl) prefetchList(client, scope, listUrl).catch(() => undefined);
      } catch {
        // A warm-up is never worth an error.
      }
    });
  });
  return () => {
    cancelled = true;
    cancelIdle?.();
  };
}
