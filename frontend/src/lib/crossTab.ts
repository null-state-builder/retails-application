/**
 * Telling the other open tabs that the session ended (app loading and caching
 * plan, fixed rule: logout deletes everything cached for the back office).
 *
 * The session cookie is shared by every tab, so a logout, expiry or password
 * change in one tab ends it in all of them. Each other tab must then forget
 * what it was shown, stop saving, and never write the device copy again, or a
 * tab left open would put the last person's reference data back on disk after
 * the logout deleted it. One tab says so on a `BroadcastChannel`; where that
 * does not exist, through a `storage` event (a key written and removed at
 * once; only other tabs see it). The tab that ends the session is never told
 * its own news.
 */

export const SESSION_CHANNEL = "kdps-backoffice-session";
export const SESSION_ENDED_KEY = "kdps:session-ended";

interface SessionEnded {
  type: "session-ended";
  at: number;
}

type Listener = () => void;

const listeners = new Set<Listener>();
let channel: BroadcastChannel | null | undefined;
let listeningToStorage = false;

function isSessionEnded(value: unknown): value is SessionEnded {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { type?: unknown }).type === "session-ended"
  );
}

function deliver(): void {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // One listener's failure must not stop the others from wiping.
    }
  }
}

function onStorage(event: StorageEvent): void {
  if (event.key === SESSION_ENDED_KEY && event.newValue) deliver();
}

/** The one channel this tab posts and listens on (so it never hears itself),
 *  or null where `BroadcastChannel` does not exist. */
function sessionChannel(): BroadcastChannel | null {
  if (channel !== undefined) return channel;
  if (typeof BroadcastChannel === "undefined") {
    channel = null;
    return channel;
  }
  try {
    channel = new BroadcastChannel(SESSION_CHANNEL);
    channel.onmessage = (event: MessageEvent) => {
      if (isSessionEnded(event.data)) deliver();
    };
    // Node's implementation (tests) would otherwise keep the process alive.
    (channel as { unref?: () => void }).unref?.();
  } catch {
    channel = null;
  }
  return channel;
}

/** Tell every other tab that the session ended. Never throws. */
export function announceSessionEnded(): void {
  const message: SessionEnded = { type: "session-ended", at: Date.now() };
  const open = sessionChannel();
  if (open) {
    try {
      open.postMessage(message);
      return;
    } catch {
      // Fall through to the storage event.
    }
  }
  try {
    localStorage.setItem(SESSION_ENDED_KEY, String(message.at));
    localStorage.removeItem(SESSION_ENDED_KEY);
  } catch {
    // No storage either: the other tabs find out on their next request (401).
  }
}

/** Run `listener` whenever another tab ends the session. Returns an
 *  unsubscribe. */
export function onSessionEndedElsewhere(listener: Listener): () => void {
  listeners.add(listener);
  sessionChannel();
  if (!listeningToStorage && typeof window !== "undefined") {
    // Listened to even with a channel: a tab whose post failed says it this
    // way instead.
    window.addEventListener("storage", onStorage);
    listeningToStorage = true;
  }
  return () => {
    listeners.delete(listener);
  };
}
