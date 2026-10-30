// Switch (design system v2, decision 8): an on/off setting that takes effect
// the moment it is flipped, with its state written beside it - On, Off, or
// "On, held off" in amber when something else keeps a chosen setting from
// working. Not for a form that saves with a button (a checkbox), a choice
// between two named options (SegmentedControl) or a list filter (FilterChip).
// While the save is in flight the input is disabled; if it fails the consumer
// shows a Note and the switch reads its old value again.

import type { ReactNode } from "react";

export function switchStateWord(checked: boolean, held = false): string {
  if (!checked) return "Off";
  return held ? "On, held off" : "On";
}

export function Switch({
  checked,
  onChange,
  label,
  ariaLabel,
  ariaLabelledBy,
  held = false,
  busy = false,
  testId,
  stateTestId,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** Visible label before the track; leave out inside a SettingRow, whose title names it. */
  label?: ReactNode | undefined;
  ariaLabel?: string | undefined;
  ariaLabelledBy?: string | undefined;
  /** On, but held off by something else; the reason goes in a hint below. */
  held?: boolean | undefined;
  /** Saving: the input is disabled until the change comes back. */
  busy?: boolean | undefined;
  testId?: string | undefined;
  stateTestId?: string | undefined;
}) {
  return (
    <label className="switch">
      {label && <span className="switch-label">{label}</span>}
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={busy}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        onChange={(e) => onChange(e.target.checked)}
        data-testid={testId}
      />
      <span className="track" aria-hidden="true" />
      <span
        className={`state-word ${checked && held ? "held" : ""}`.trim()}
        aria-hidden="true"
        data-testid={stateTestId}
      >
        {switchStateWord(checked, held)}
      </span>
    </label>
  );
}
