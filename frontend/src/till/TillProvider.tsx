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

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
  useState,
} from "react";
import type { ReactNode } from "react";

import { useAuth } from "../auth/AuthContext";

import { TillEngine } from "./engine";
import type { TillSnapshot } from "./engine";
import { httpTransport } from "./transport";
import type { TillTransport } from "./transport";
import type { TillIdentity } from "./types";

interface TillContextValue {
  /** Null when the signed-in person is not a single store - see `tillStore`. */
  engine: TillEngine | null;
  till: TillSnapshot | null;
  accessError: string;
  recoverPending: (() => Promise<string>) | null;
}

const TillContext = createContext<TillContextValue>({
  engine: null,
  till: null,
  accessError: "",
  recoverPending: null,
});

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
  return stores.length === 1 ? (stores[0]?.code ?? "") : "";
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

  const [identity, setIdentity] = useState<{
    user: typeof user;
    store: string;
    value: TillIdentity;
  } | null>(null);
  useEffect(() => {
    let current = true;
    setIdentity(null);
    if (storeCode && user) {
      void (transport ?? httpTransport)
        .till()
        .then((value) => {
          if (current) setIdentity({ user, store: storeCode, value });
        })
        .catch(() => {
          /* no trustworthy namespace: no cached business data */
        });
    }
    return () => {
      current = false;
    };
  }, [user, storeCode, transport]);
  const engine = useMemo(() => {
    if (!user || !identity || identity.user !== user || identity.store !== storeCode) return null;
    const { tenant_id, site_id, device_id } = identity.value;
    const scope =
      tenant_id && site_id && device_id
        ? `${tenant_id}:${site_id}:${device_id}`
        : transport
          ? storeCode
          : "";
    return scope
      ? new TillEngine(
          storeCode,
          transport,
          undefined,
          scope,
          identity.value.selling_mode === "online_alpha",
        )
      : null;
  }, [user, identity, storeCode, transport]);

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

  const value = useMemo(() => {
    // Preserve this device's durable bills, but never let another session see
    // previously cached protected fields before its own server projection lands.
    const waiting = till?.onlineAlpha && !till.liveAccessVerified;
    return {
      engine: waiting ? null : engine,
      till: waiting ? null : till,
      accessError: waiting
        ? till.lastError || "Refreshing this session's authorised counter data…"
        : "",
      // An accepted timeout recovery may be authorised even after readiness is
      // withdrawn. Expose only its server-checked number, never cached payload.
      recoverPending:
        waiting && engine && till.onlinePending?.state === "pending"
          ? async () => {
              const accepted = await engine.retryOnline();
              return `Sale ${accepted.doc_number} was accepted. Do not collect payment again.`;
            }
          : null,
    };
  }, [engine, till]);
  return <TillContext.Provider value={value}>{children}</TillContext.Provider>;
}

// A login with no counter still calls the hook - the rules say so - and both of
// these have to be stable references or the store would re-subscribe every render.
const noSubscribe = () => () => undefined;
const noSnapshot = () => null;
