// SegmentedControl (design system v2): two to five toggle buttons choosing
// between named options - how a list is shown (Today / Week / Month), or a
// two-option setting where "off" would mean nothing (Active / Retired). At
// most one per list; a second dimension is FilterChips. Exactly one option is
// always pressed. It changes the view, not the panel, so it is a group of
// pressed buttons, not a tablist.

import type { ReactNode } from "react";

export interface SegmentOption<T extends string> {
  value: T;
  label: ReactNode;
  /** A count shown after the word, in muted figures. */
  count?: ReactNode | undefined;
  /** For an option shown as an icon only. */
  ariaLabel?: string | undefined;
  title?: string | undefined;
  testId?: string | undefined;
}

export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
  className = "",
  disabled = false,
  testId,
}: {
  /** The group's accessible name: "Period", "Kind of return". */
  label: string;
  options: SegmentOption<T>[];
  value: T;
  onChange: (value: T) => void;
  className?: string | undefined;
  /** The whole control, while the view it chose is still loading. */
  disabled?: boolean | undefined;
  testId?: string | undefined;
}) {
  return (
    <div className={`seg ${className}`.trim()} role="group" aria-label={label} data-testid={testId}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className="seg-btn"
          aria-pressed={o.value === value}
          aria-label={o.ariaLabel}
          title={o.title}
          data-testid={o.testId}
          disabled={disabled}
          onClick={() => o.value !== value && onChange(o.value)}
        >
          {o.label}
          {o.count != null && <span className="seg-n">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}
