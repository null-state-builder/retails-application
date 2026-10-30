// The shared empty and error states (design system v2 EmptyState). Left
// aligned where the list would be, with no frame: an optional heading, one
// line that says why there is nothing, and the action that fills it; for a
// failure, one plain sentence (from `apiErrorMessage`, never a status code)
// and Try again.

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { RefreshCw } from "lucide-react";

/** Skeletons wait this long, so a quick load never flashes one (v2 Motion). */
export const LOADING_DELAY_MS = 300;
/** Once shown, a skeleton stays at least this long, so it never blinks. */
export const LOADING_HOLD_MS = 400;

/** Whether a loading placeholder should show: not for the first 300ms of a
 *  load, and once shown, for at least 400ms. Until it shows, keep the previous
 *  content on screen rather than a blank. */
export function useDelayedLoading(loading: boolean): boolean {
  const [shown, setShown] = useState(false);
  const shownAt = useRef(0);
  useEffect(() => {
    if (loading && !shown) {
      const t = window.setTimeout(() => {
        shownAt.current = Date.now();
        setShown(true);
      }, LOADING_DELAY_MS);
      return () => window.clearTimeout(t);
    }
    if (!loading && shown) {
      const left = LOADING_HOLD_MS - (Date.now() - shownAt.current);
      if (left <= 0) {
        setShown(false);
        return undefined;
      }
      const t = window.setTimeout(() => setShown(false), left);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [loading, shown]);
  return shown;
}

/** Skeleton lines in place of a list that is loading, after the delay. The
 *  label is for screen readers; nothing visible says "Loading". */
export function Loading({
  lines = 3,
  label = "Loading",
  testId,
}: {
  lines?: number | undefined;
  label?: string | undefined;
  testId?: string | undefined;
}) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => setVisible(true), LOADING_DELAY_MS);
    return () => window.clearTimeout(t);
  }, []);
  return (
    <div className="loading-lines" role="status" aria-label={label} data-testid={testId}>
      {visible &&
        Array.from({ length: lines }, (_, i) => (
          <span key={i} className="skeleton" style={{ width: `${90 - i * 15}%` }} />
        ))}
    </div>
  );
}

export function EmptyState({
  heading,
  children,
  action,
  testId,
}: {
  /** "No bookings open". Leave out when the line says it all. */
  heading?: ReactNode | undefined;
  /** Why it is empty, in one line: "No bookings are open." */
  children: ReactNode;
  /** The button or link that fills it, when there is one. */
  action?: ReactNode | undefined;
  testId?: string | undefined;
}) {
  return (
    <div className="state state-empty" data-testid={testId}>
      {heading && <h3 className="heading">{heading}</h3>}
      <p>{children}</p>
      {action}
    </div>
  );
}

export function ErrorState({
  heading = "This could not load",
  message,
  onRetry,
  busy = false,
  testId,
}: {
  heading?: ReactNode | undefined;
  /** A sentence for the person at the screen; `apiErrorMessage` gives one. */
  message: string;
  onRetry?: () => void | undefined;
  busy?: boolean | undefined;
  testId?: string | undefined;
}) {
  return (
    <div className="state state-error" role="alert" data-testid={testId}>
      <h3 className="heading">{heading}</h3>
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="btn btn-sm" disabled={busy} onClick={onRetry}>
          <RefreshCw size={14} aria-hidden="true" /> {busy ? "Retrying…" : "Try again"}
        </button>
      )}
    </div>
  );
}
