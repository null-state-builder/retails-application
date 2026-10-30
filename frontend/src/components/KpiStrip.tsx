// KpiStrip (design system v2): one lead figure stated with a sentence, then a
// single unboxed strip of at most five secondary figures between two
// hairlines. Not a row of equal tiles. Values come formatted (Indian
// grouping); an unknown value is "—" with the reason, never 0. Only the
// comparison word carries green or red, and `up` means good, not just higher.

import type { ReactNode } from "react";

export type Trend = "up" | "down";

export function LeadFigure({
  label,
  value,
  children,
  testId,
}: {
  label: ReactNode;
  value: ReactNode;
  /** The sentence with the comparison: "₹4,200 more than last Thursday by 3 pm." */
  children?: ReactNode | undefined;
  testId?: string | undefined;
}) {
  return (
    <div className="lead-figure" data-testid={testId}>
      <span className="lead-figure-l">{label}</span>
      <span className="lead-figure-v">{value}</span>
      {children && <p className="lead-figure-d">{children}</p>}
    </div>
  );
}

export interface Kpi {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode | undefined;
  trend?: Trend | undefined;
  testId?: string | undefined;
}

export function KpiStrip({ items, label }: { items: Kpi[]; label?: string }) {
  return (
    <dl className="kpis" aria-label={label}>
      {items.slice(0, 5).map((k, i) => (
        <div className="kpi-cell" key={i} data-testid={k.testId}>
          <dt className="kpi-l">{k.label}</dt>
          <dd className="kpi-v">{k.value}</dd>
          {k.detail != null && <dd className={`kpi-d ${k.trend ?? ""}`.trim()}>{k.detail}</dd>}
        </div>
      ))}
    </dl>
  );
}
