// SettingRow (design system v2, decision 9): one setting or task - a title and
// a sentence on the left, its one control in a right column at least 140px
// wide, so every control in the list ends on one edge and every button is the
// same width. A second action goes in the inline form (`more`) that opens
// below the line across both columns. Not for table rows or status-band rows.

import { useId } from "react";
import type { ReactNode } from "react";

export function Settings({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <div className="settings" data-testid={testId}>
      {children}
    </div>
  );
}

export function SettingRow({
  title,
  description,
  control,
  more,
  after,
  level = 3,
  titleId,
  testId,
}: {
  title: ReactNode;
  /** One sentence. */
  description?: ReactNode | undefined;
  /** One switch or one button; hidden while `more` is open. */
  control?: ReactNode | undefined;
  /** The inline form, shown while the task is open. */
  more?: ReactNode | undefined;
  /** The outcome, shown whether or not the form is open. */
  after?: ReactNode | undefined;
  level?: 2 | 3 | undefined;
  /** For a control that names itself by the title (`aria-labelledby`). */
  titleId?: string | undefined;
  testId?: string | undefined;
}) {
  const fallbackId = useId();
  const id = titleId ?? fallbackId;
  const H = level === 2 ? "h2" : "h3";
  return (
    <div className="setting-row" data-testid={testId}>
      <div>
        <H className="setting-row-title" id={id}>
          {title}
        </H>
        {description && <p className="setting-row-desc">{description}</p>}
      </div>
      {!more && control != null ? <div className="setting-row-control">{control}</div> : <span />}
      {more && <div className="setting-row-more">{more}</div>}
      {after && <div className="setting-row-more">{after}</div>}
    </div>
  );
}
