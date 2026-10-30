// Section (design system v2): a heading and its content laid straight on the
// page, with no box around it. Space separates sections; `ruled` adds one
// hairline where two would read as one. A `Box` is only for what must be held
// together - a table, the record pane, the bill - and never sits in another box.

import { useId } from "react";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

export function Section({
  heading,
  aside,
  ruled = false,
  level = 2,
  className = "",
  children,
  testId,
}: {
  /** Names the section; never repeated by a label above it. */
  heading: ReactNode;
  /** At most one quiet link (or a small button) on the right of the heading. */
  aside?: ReactNode | undefined;
  ruled?: boolean | undefined;
  /** h2 under the page's h1 by default; h3 inside a pane. */
  level?: 2 | 3 | undefined;
  className?: string | undefined;
  children: ReactNode;
  testId?: string | undefined;
}) {
  const id = useId();
  const H = level === 3 ? "h3" : "h2";
  return (
    <section
      className={`section ${ruled ? "ruled" : ""} ${className}`.replace(/\s+/g, " ").trim()}
      aria-labelledby={id}
      data-testid={testId}
    >
      <div className="section-head">
        <H className="heading" id={id}>
          {heading}
        </H>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Box({ className = "", ...props }: ComponentPropsWithoutRef<"div">) {
  return <div {...props} className={`box ${className}`.trim()} />;
}
