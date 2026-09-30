import type { ComponentPropsWithoutRef, ReactNode } from "react";

import "./OperationsPage.css";

/** The store's operational screens share a page frame. It is opt-in: the
 * counter and Today keep their own established layouts. */
export function OperationsPage({ className = "", ...props }: ComponentPropsWithoutRef<"div">) {
  return <div {...props} className={`page-pad operations-page ${className}`.trim()} />;
}

/** Wide operational tables scroll inside the page and remain reachable with
 * a keyboard. The label describes the table rather than a technical view. */
export function OperationsTable({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className="operations-table-scroll" role="region" aria-label={label} tabIndex={0}>
      {children}
    </div>
  );
}
