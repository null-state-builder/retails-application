// FilterChip (design system v2): a small rectangular toggle that narrows a
// list - Waiting 3, Only mine, Show retired - with an optional count. View
// filters are chips, not checkboxes or switches (decision 10): a switch means
// a saved setting. A zero count stays visible and pressable.

import type { ReactNode } from "react";

export function FilterChip({
  pressed,
  onClick,
  count,
  children,
  testId,
}: {
  pressed: boolean;
  onClick: () => void;
  count?: ReactNode | undefined;
  children: ReactNode;
  testId?: string | undefined;
}) {
  return (
    <button
      type="button"
      className="filter-chip"
      aria-pressed={pressed}
      onClick={onClick}
      data-testid={testId}
    >
      {children}
      {count != null && <span className="filter-chip-n">{count}</span>}
    </button>
  );
}

export function FilterChips({
  label,
  children,
  testId,
}: {
  /** The group's accessible name: "Status", "Show". */
  label: string;
  children: ReactNode;
  testId?: string | undefined;
}) {
  return (
    <div className="filter-chips" role="group" aria-label={label} data-testid={testId}>
      {children}
    </div>
  );
}
