// The till, mounted (#180, D10 step 3).
//
// One engine per signed-in store, started when a Sell screen opens and stopped
// when the last one closes. It is deliberately *not* mounted at the app root: a
// warehouse or head-office login has no counter, no local database and nothing to
// sync, and opening one for them would put a store's price list on their laptop.
//
// React reads the engine through `useSyncExternalStore` rather than through
// state, because the source of truth is IndexedDB and a timer: the queue drains
// while nobody is looking at it, and a screen that mounts halfway through has to
// see where things actually are, not where they were when it rendered.

import { createContext, useContext, useEffect, useMemo, useSyncExternalStore } from "react";
import type { ReactNode } from "react";

import { useAuth } from "../auth/AuthContext";

import { TillEngine } from "./engine";
import type { TillSnapshot } from "./engine";
import type { TillTransport } from "./transport";

interface TillContextValue {
  /** Null when the signed-in person is not a single store - see `tillStore`. */
  engine: TillEngine | null;
  till: TillSnapshot | null;
}

const TillContext = createContext<TillContextValue>({ engine: null, till: null });

/** The engine and the current snapshot, or nulls for a login with no counter. */
export function useTill(): TillContextValue {
  return useContext(TillContext);
}

/**
 * Which store this login is the till for, if any.
 *
 * The counter's own store, from what the server granted - never the top-bar unit
 * switcher. The dataset endpoint refuses a multi-store login with `TILL_SCOPE`
 * for the same reason: a till holds one shop's prices and manager PIN hashes, so
 * letting a person pick which shop they are
 * billing for in a dropdown would hand any regional login a store's credentials.
 */
export function tillStoreCode(user: { stores?: { code: string }[] } | null): string {
  const stores = user?.stores ?? [];
  return stores.length === 1 ? stores[0].code : "";
}

export function TillProvider({
  children,
  transport,
}: {
  children: ReactNode;
  /** Injected by tests and by nothing else. */
  transport?: TillTransport;
}) {
  const { user } = useAuth();
  const storeCode = tillStoreCode(user);

  const engine = useMemo(
    () => (storeCode ? new TillEngine(storeCode, transport) : null),
    [storeCode, transport],
  );

  useEffect(() => {
    if (!engine) return;
    void engine.start();
    return () => engine.stop();
  }, [engine]);

  const till = useSyncExternalStore(
    engine ? engine.subscribe : noSubscribe,
    engine ? engine.getSnapshot : noSnapshot,
    engine ? engine.getSnapshot : noSnapshot,
  );

  const value = useMemo(() => ({ engine, till }), [engine, till]);
  return <TillContext.Provider value={value}>{children}</TillContext.Provider>;
}

// A login with no counter still calls the hook - the rules say so - and both of
// these have to be stable references or the store would re-subscribe every render.
const noSubscribe = () => () => undefined;
const noSnapshot = () => null;
