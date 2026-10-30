// "Could not refresh" (app loading and caching plan): screens open on the
// cached copy and refresh it behind the scenes. When that refresh fails, the
// rows already on screen stay — they are still the last thing the server said
// — but the person must be told they may be out of date. A warning, not an
// error: nothing is blocked, and one button asks again.
//
// Drawn only while there is something on screen to be stale. A read that
// failed with nothing to show is the screen's own error state, not this.

import { apiErrorMessage } from "../lib/api";
import { Note } from "./Note";
import { agoText } from "./UpdatedAgo";

export function RefreshFailed({
  error,
  shown,
  updatedAt,
  onRetry,
  testId = "refresh-failed",
}: {
  /** The read's last failure (`useList` / `useDoc` / `useCachedGet` `error`). */
  error: unknown;
  /** Whether earlier data is on screen. */
  shown: boolean;
  /** When the shown data arrived (ms since epoch). */
  updatedAt?: number | null | undefined;
  /** Ask again (`reload`). */
  onRetry?: (() => void) | undefined;
  testId?: string | undefined;
}) {
  if (error == null || !shown) return null;
  const when = updatedAt ? agoText(updatedAt, Date.now()) : null;
  return (
    <Note
      tone="warn"
      role="status"
      testId={testId}
      action={
        onRetry ? (
          <button type="button" className="btn btn-sm" onClick={onRetry}>
            Try again
          </button>
        ) : undefined
      }
    >
      Could not refresh, so this may be out of date{when ? ` (last loaded ${when})` : ""}.{" "}
      {apiErrorMessage(error)}
    </Note>
  );
}
