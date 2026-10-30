// Note (design system v2): an inline message in four tones with a 3px bar and
// ink text. One or two sentences - what happened, what to do - and at most
// one action. Not for a momentary confirmation (a toast) or an empty list
// (EmptyState); never stack more than two on a screen.

import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";

export type NoteTone = "info" | "success" | "warn" | "error";

const ICON = { info: Info, success: CheckCircle2, warn: AlertTriangle, error: XCircle };

export function Note({
  tone = "info",
  children,
  action,
  role,
  testId,
}: {
  tone?: NoteTone | undefined;
  children: ReactNode;
  /** One `.btn.btn-sm`, pushed to the right. */
  action?: ReactNode | undefined;
  /** Errors default to `alert`; pass `status` for one that appears after an action. */
  role?: "alert" | "status" | undefined;
  testId?: string | undefined;
}) {
  const Icon = ICON[tone];
  const cls = tone === "info" ? "note" : `note note-${tone}`;
  return (
    <div
      className={cls}
      role={role ?? (tone === "error" ? "alert" : undefined)}
      data-testid={testId}
    >
      <Icon size={16} aria-hidden="true" />
      <div className="note-body">{children}</div>
      {action}
    </div>
  );
}
