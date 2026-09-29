// The bell's arithmetic, with no popup around it (#226).
//
// Three rules, each of which a button's badge and the feed beneath it must
// agree on. They live here rather than inside `Notifications` because the badge
// is read at a glance and trusted: a count computed in two places is a count
// that eventually says two things at once.

import { tillToday } from "../till/pricing";
import type { AlertT } from "../components/alert";

/** How many open alerts this person has not seen yet (`api-contract.md` s3).
 *
 *  No stamp means never opened, and that counts as *everything* unread - the
 *  right cold start, since the feed has been filling up without them.
 *
 *  Strictly after the stamp: an alert bearing exactly the stamped moment was on
 *  screen when the stamp was taken, and counting it would re-light the badge on
 *  the very thing the person had just read.
 */
export function unreadAlerts(alerts: AlertT[], seenAt: string | null): number {
  if (!seenAt) return alerts.length;
  const seen = new Date(seenAt).getTime();
  return alerts.filter((a) => new Date(a.created_at).getTime() > seen).length;
}

/** What a count badge reads, or `null` for no badge at all: zero is the
 *  absence of a badge, not a badge saying "0", and past nine the exact number
 *  stops mattering. One function, because the same cap written inline in
 *  three places is how a badge and its popup end up disagreeing. */
export function badgeLabel(count: number): string | null {
  if (count <= 0) return null;
  return count > 9 ? "9+" : String(count);
}

/** How many things wait on this person in the Action Needed popup: approvals
 *  to decide (the older inbox and, since 23 Sep 2026, the goods inbox) plus
 *  open exceptions to fix. A feed that failed (`null`) or that this person may
 *  not see adds nothing - the popup says the failure out loud, so the badge
 *  still counts what did load rather than going dark. */
export function actionNeededCount(
  approvals: readonly unknown[] | null,
  exceptions: readonly unknown[] | null,
  goodsApprovals: readonly unknown[] | null = null,
): number {
  return (approvals?.length ?? 0) + (exceptions?.length ?? 0) + (goodsApprovals?.length ?? 0);
}

/** Goods notifications not yet marked read (E187). Unlike the deadlines, these
 *  are read one by one on the Alerts page, so there is no stamp: a row is
 *  unread until someone marks it. A failed feed counts nothing. */
export function unseenCount(rows: readonly { seen: boolean }[] | null): number {
  return rows ? rows.filter((row) => !row.seen).length : 0;
}

/** The four windows History offers. Order is display order; `7d` is the
 *  default, and sits second so the common choice is next to the narrowest. */
export const RANGE_KEYS = ["today", "7d", "30d", "1y"] as const;

export type RangeKey = (typeof RANGE_KEYS)[number];

export const RANGE_LABELS: Record<RangeKey, string> = {
  today: "Today",
  "7d": "7 days",
  "30d": "30 days",
  "1y": "1 year",
};

export const DEFAULT_RANGE: RangeKey = "7d";

const RANGE_DAYS: Record<RangeKey, number> = { today: 0, "7d": 7, "30d": 30, "1y": 365 };

/**
 * The `?since=` a range asks for, on the counter's own calendar.
 *
 * Built from local date parts and formatted by `tillToday`, never from
 * `toISOString()`: a shop in Deoghar is five and a half hours ahead, so from
 * half past six every evening the UTC date is already tomorrow and "Today"
 * would quietly ask the server for a day that has not started.
 *
 * `new Date(y, m, d - n)` normalises a negative day itself, so month and year
 * boundaries need no arithmetic of our own.
 */
export function sinceFor(range: RangeKey, now: Date = new Date()): string {
  return tillToday(new Date(now.getFullYear(), now.getMonth(), now.getDate() - RANGE_DAYS[range]));
}

export interface AlertDay {
  /** `YYYY-MM-DD` on the reader's calendar - the group key and its sort order. */
  day: string;
  alerts: AlertT[];
}

/**
 * Resolved alerts under the day they cleared, newest day first.
 *
 * The day comes from the local calendar rather than the first ten characters of
 * the ISO string, for the same reason `sinceFor` does: an alert cleared at half
 * past midnight in Deoghar carries yesterday's UTC date, and slicing the string
 * would file it under the wrong heading.
 *
 * Rows arrive newest-first from the server (`-resolved_at`) and that order is
 * preserved inside each group; a row with no `resolved_at` is dropped rather
 * than given a day it does not have.
 */
export function groupResolvedByDay(alerts: AlertT[]): AlertDay[] {
  const byDay = new Map<string, AlertT[]>();
  for (const a of alerts) {
    if (!a.resolved_at) continue;
    // `en-CA` is the one locale that formats as `YYYY-MM-DD`, which is both the
    // key and, being lexicographic, the sort.
    const day = new Date(a.resolved_at).toLocaleDateString("en-CA");
    const bucket = byDay.get(day);
    if (bucket) bucket.push(a);
    else byDay.set(day, [a]);
  }
  return [...byDay.entries()]
    .sort(([a], [b]) => b.localeCompare(a))
    .map(([day, rows]) => ({ day, alerts: rows }));
}

/** A day heading a person reads without decoding: today and yesterday by name,
 *  everything else by date. */
export function dayHeading(day: string, now: Date = new Date()): string {
  if (day === tillToday(now)) return "Today";
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (day === tillToday(yesterday)) return "Yesterday";
  return new Date(`${day}T00:00:00`).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
