// A sale line shared between two salespeople (store operations ticket 08,
// ST-POS-2, baseline B14).
//
// Whole percentages from 1 to 99, two different people, adding to 100. The
// line's value is split to the paisa and the spare paisa goes to the first
// salesperson - the line's own. The server refuses the same shares with the
// same words (`sell/services/split_shares.py`); the counter refuses them first,
// before the bill is printed, because a bill the server refuses after printing
// halts the queue behind it.

export const MIN_PERCENT = 1;
export const MAX_PERCENT = 99;

/** How a line is shared. The first person is the line's own salesperson; this
 *  names the second, and each person's percentage. */
export interface LineSplit {
  /** The second salesperson's staff id, or null before one is picked. */
  with: string | null;
  /** The line's own salesperson's percentage. */
  first_percent: number;
  /** The second salesperson's percentage. */
  second_percent: number;
}

/** A split as the cashier first opens it: half each, nobody picked yet. */
export function newSplit(): LineSplit {
  return { with: null, first_percent: 50, second_percent: 50 };
}

/** A percentage box's text as a whole number, or NaN when it is not one. */
export function percentFrom(text: string): number {
  const trimmed = text.trim();
  if (!/^\d{1,3}$/.test(trimmed)) return Number.NaN;
  return Number(trimmed);
}

/** Why this split cannot stand, in words the cashier reads, or null. */
export function whySplitIsWrong(salesperson: string | null, split: LineSplit): string | null {
  if (!split.with || !salesperson) return "Pick both salespeople for the split.";
  if (split.with === salesperson) return "A line is split between two different salespeople.";
  const percents = [split.first_percent, split.second_percent];
  if (percents.some((p) => !Number.isInteger(p))) return "Each share is a whole percentage.";
  if (percents.some((p) => p < MIN_PERCENT || p > MAX_PERCENT)) {
    return `Each share is from ${MIN_PERCENT}% to ${MAX_PERCENT}%.`;
  }
  if (split.first_percent + split.second_percent !== 100) {
    return "The two shares must add up to 100%.";
  }
  return null;
}

/** Why a bill cannot carry a split at a store whose switch is off, or null.
 *
 *  A split already on a cart or a held bill when a sync turned the switch off
 *  stays on screen so it can be removed, but the bill does not close with it:
 *  off, the till makes no split bills. (A bill already printed while the switch
 *  was on is a different case - the server keeps it and flags it.) */
export function whySplitIsOff(
  lines: { line_no: number; split?: unknown }[],
  allowed: boolean,
): string | null {
  if (allowed) return null;
  const split = lines.find((line) => line.split);
  if (!split) return null;
  return `Splitting a sale is switched off at this store. Remove the split on line ${split.line_no}.`;
}

/** A line's value split by these percentages, to the paisa; the spare paisa
 *  goes to the first salesperson. Mirrors `split_shares.split_value`. */
export function splitValue(valuePaise: number, percents: [number, number]): [number, number] {
  const second = Math.floor((valuePaise * percents[1]) / 100);
  return [valuePaise - second, second];
}
