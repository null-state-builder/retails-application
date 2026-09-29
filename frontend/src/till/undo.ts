// Stepping the bill back one action at a time (#244, grill ruling 30 Jul 2026).
//
// A stack of whole `Cart` snapshots, one pushed ahead of every cart mutator -
// a scan, a delete, a quantity, discount or salesperson edit - and popped by the
// Undo button. Safe by construction: nothing on this bill touches stock or
// money until Save & Print, so stepping it backwards is exactly as safe as the
// last action never having happened.
//
// Deliberately in memory only. `draft.ts` is what survives a crash, and it
// remembers where the bill last stood, not the steps that got it there -
// keeping those would mean persisting on every keystroke in the discount box,
// for a history a crash undoes anyway. No redo either: popping a step off
// throws it away rather than parking it to be replayed.

import type { Cart } from "./cart";

/** How many steps back the button can go. Not configurable (grill Q2-Q3) -
 *  this is a safety net sized generously past any one bill's line count, not a
 *  feature with a dial on it. */
export const UNDO_LIMIT = 50;

/** One step back, and - for a step that came from typing - what was being
 *  typed into, so a run of keystrokes in one box collapses into one step.
 *  `run` is absent on discrete actions (a scan, a delete), which never
 *  collapse into anything. */
export interface UndoStep {
  cart: Cart;
  run?: string;
}

export type UndoStack = UndoStep[];

export function emptyUndo(): UndoStack {
  return [];
}

/**
 * Remember `snapshot`, the cart as it stood immediately before the change that
 * is about to land. The oldest step falls off once the stack is past
 * `UNDO_LIMIT`, so a long bill's undo depth is bounded rather than growing into
 * a second history of the whole day.
 *
 * `run` names what is being edited - one line's one field. The grid's cells
 * fire on every keystroke, so without it a ₹150 discount is three steps and
 * Undo walks back through "₹15" and "₹1" before reaching the price the line
 * actually had (round-2 finding). Consecutive pushes under the same `run`
 * keep the *first* snapshot and add nothing: the state before the typing
 * started is the one worth going back to, and the 50-step limit stays a
 * budget for actions rather than characters.
 */
export function pushUndo(stack: UndoStack, snapshot: Cart, run?: string): UndoStack {
  const top = stack[stack.length - 1];
  if (run !== undefined && top?.run === run) return stack;
  const next = [...stack, { cart: snapshot, ...(run === undefined ? {} : { run }) }];
  return next.length > UNDO_LIMIT ? next.slice(next.length - UNDO_LIMIT) : next;
}

/** Step back one action: the cart before it, and the stack with that step
 *  taken off. `null` once there is nothing left to undo. */
export function popUndo(stack: UndoStack): { cart: Cart; stack: UndoStack } | null {
  const last = stack.at(-1);
  return last ? { cart: last.cart, stack: stack.slice(0, -1) } : null;
}
