// "Updated 40s ago" (app loading and caching plan, fixed rule: stock and money
// screens show when the data was last updated). Pages now open on the cached
// copy and refresh it behind the scenes, so a figure on screen may be a few
// seconds or minutes old; this says how old, in the page header, and says so
// quietly while a fresh copy is being read. Caption colour, no frame: it is a
// footnote to the header, not a status.

import { useEffect, useState } from "react";

/** "just now", "40s ago", "3 min ago", "2 h ago", "3 days ago". */
export function agoText(updatedAt: number, now: number): string {
  const seconds = Math.max(0, Math.floor((now - updatedAt) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** How often the label is redrawn: every 5 s for the first minute, then every
 *  30 s (minutes do not need second-by-second ticking). */
function tickFor(age: number): number {
  return age < 60_000 ? 5_000 : 30_000;
}

export function UpdatedAgo({
  updatedAt,
  fetching = false,
  testId = "updated-ago",
}: {
  /** When the shown data arrived (ms since epoch); nothing is drawn while null. */
  updatedAt: number | null | undefined;
  /** A refresh is in flight behind what is shown. */
  fetching?: boolean | undefined;
  testId?: string | undefined;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!updatedAt) return undefined;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), tickFor(Date.now() - updatedAt));
    return () => window.clearInterval(t);
  }, [updatedAt]);

  if (!updatedAt) return null;
  const at = new Date(updatedAt);
  return (
    <span
      className="muted-cell"
      data-testid={testId}
      data-refreshing={fetching ? "true" : undefined}
      aria-busy={fetching || undefined}
    >
      Updated{" "}
      <time dateTime={at.toISOString()} title={at.toLocaleString("en-IN")}>
        {agoText(updatedAt, now)}
      </time>
      {fetching && " · refreshing…"}
    </span>
  );
}
