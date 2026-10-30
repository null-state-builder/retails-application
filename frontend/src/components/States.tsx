// The shared empty and error states (design system v2 EmptyState). Left
// aligned where the list would be, with no frame: an optional heading, one
// line that says why there is nothing, and the action that fills it; for a
// failure, one plain sentence (from `apiErrorMessage`, never a status code)
// and Try again.

import type { ReactNode } from "react";
import { RefreshCw } from "lucide-react";

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
