/**
 * A screen loaded on first use (route code splitting, caching plan Phase 1).
 *
 * `lazyPage(() => import("./pages/X"), "XPage")` is a `React.lazy` component
 * that also carries `preload()`, so the sidebar can start fetching a screen's
 * code when a hand reaches for its link, before the click.
 */
import { lazy, type ComponentType, type LazyExoticComponent } from "react";

export type LazyPage = LazyExoticComponent<ComponentType> & { preload: () => Promise<unknown> };

const RELOADED_KEY = "kdps:chunk-reload";

/** A deploy replaces the hashed chunk files; a tab opened before it asks for a
 *  chunk that no longer exists. Reload once to pick up the new build, rather
 *  than leave the screen blank; a second failure is a real error. */
function recoverFromStaleBuild(error: unknown): never {
  let reloaded = false;
  try {
    reloaded = sessionStorage.getItem(RELOADED_KEY) === "1";
    if (!reloaded) sessionStorage.setItem(RELOADED_KEY, "1");
  } catch {
    reloaded = true;
  }
  if (!reloaded && typeof window !== "undefined") window.location.reload();
  throw error;
}

export function lazyPage<M, K extends keyof M>(load: () => Promise<M>, name: K): LazyPage {
  let pending: Promise<M> | null = null;
  // A failed warm-up (a hover) is forgotten so the real open can try again;
  // only the real open may reload the page.
  const preload = () =>
    (pending ??= load().then(
      (module) => {
        try {
          sessionStorage.removeItem(RELOADED_KEY);
        } catch {
          // Storage unavailable: nothing to reset.
        }
        return module;
      },
      (error: unknown) => {
        pending = null;
        throw error;
      },
    ));
  const Page = lazy(() =>
    preload().then(
      (module) => ({ default: module[name] as unknown as ComponentType }),
      recoverFromStaleBuild,
    ),
  );
  return Object.assign(Page, { preload });
}
