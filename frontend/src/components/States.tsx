// The shared empty and error states (desktop redesign audit, rule 7). One line
// that says why there is nothing, with the action that fills it; one plain
// sentence for a failure, with Retry. Screens adopt these as they are
// redesigned, instead of each writing its own note.

import type { ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

export function EmptyState({
  children,
  action,
  testId,
}: {
  /** Why it is empty, in one line: "No bookings are open." */
  children: ReactNode;
  /** The button or link that fills it, when there is one. */
  action?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="state-line state-empty" data-testid={testId}>
      <span>{children}</span>
      {action}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  busy = false,
  testId,
}: {
  /** A sentence for the person at the screen; `apiErrorMessage` gives one. */
  message: string;
  onRetry?: () => void;
  busy?: boolean;
  testId?: string;
}) {
  return (
    <div className="state-line state-error" role="alert" data-testid={testId}>
      <AlertTriangle size={16} aria-hidden="true" />
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="btn btn-sm" disabled={busy} onClick={onRetry}>
          <RefreshCw size={14} aria-hidden="true" /> Try again
        </button>
      )}
    </div>
  );
}
