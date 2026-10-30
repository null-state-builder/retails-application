// Status (design system v2 Chip): a settled state is a small dot and a plain
// word - Draft, Synced, In transit. A filled `.chip` is kept for the few states
// where someone has to act. The word always says the state; the dot is
// decoration and never the only signal.

import type { ReactNode } from "react";

/** green done, amber waiting, red refused or short, blue information,
 *  purple suggested by the system, grey draft or closed. */
export type StatusTone = "green" | "amber" | "red" | "blue" | "purple" | "grey";

export function Status({
  tone = "grey",
  children,
  testId,
}: {
  tone?: StatusTone | undefined;
  children: ReactNode;
  testId?: string | undefined;
}) {
  const cls = tone === "grey" ? "status" : `status status-${tone}`;
  return (
    <span className={cls} data-testid={testId}>
      {children}
    </span>
  );
}
