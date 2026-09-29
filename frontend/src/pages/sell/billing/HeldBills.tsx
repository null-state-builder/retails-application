import { Clock } from "lucide-react";

import { Money } from "../../../lib/format";
import { describeHold, localDay } from "../../../till/held";
import type { HeldBill } from "../../../till/db";

// The parked carts, as a list somebody standing at a counter can act on (#185).
//
// It lives inside Billing rather than at an address of its own, because a hold is
// half of a bill and resuming one is the same act as scanning the next piece -
// walking to another screen to fetch it would be a page load in the middle of a
// sale (design.md: "the Dashboard's held-bills count links into Billing's hold
// list").
//
// Two things about the buttons are decisions.
//
// **"Let it go" is a plain button, not a confirm dialog.** A browser `confirm()`
// freezes the tab until it is answered, which on a counter is a frozen till, and
// what is behind it is a cart - not money, not a document, not a number. The
// undo is the customer saying "actually, yes" and the pieces being scanned again.
//
// **A hold from before today wears its date and gets a third button.** Grill Q13
// says nothing expires silently: the store answers keep-or-let-go itself, and
// until it does the hold stays exactly where it is.

export interface HeldBillsProps {
  holds: HeldBill[];
  /** Holds parked before today, which the store has to answer for. */
  toReview: HeldBill[];
  /** Empty when a hold can be picked up; the reason it cannot, otherwise. */
  blocked: string;
  onResume: (hold: HeldBill) => void;
  onKeep: (hold: HeldBill) => void;
  onLetGo: (hold: HeldBill) => void;
}

export function HeldBills({ holds, toReview, blocked, onResume, onKeep, onLetGo }: HeldBillsProps) {
  const stale = new Set(toReview.map((hold) => hold.held_uuid));
  return (
    <div className="card section-card bill-holds" data-testid="bill-holds">
      <p className="eyebrow">Bills on hold</p>
      {blocked && (
        <p className="muted-cell bill-holds-blocked" data-testid="bill-holds-blocked">
          {blocked}
        </p>
      )}
      {!holds.length && (
        <p className="muted-cell" data-testid="bill-holds-empty">
          Nothing is parked. Hold a bill to serve the next customer without losing this one.
        </p>
      )}
      <div className="bill-holds-rows">
        {holds.map((hold) => {
          const parkedBefore = stale.has(hold.held_uuid);
          return (
            <div
              key={hold.held_uuid}
              className="bill-hold-row"
              data-testid={`bill-hold-${hold.held_uuid}`}
            >
              <div className="bill-hold-who">
                <span className="bill-hold-name">{hold.label || describeHold(hold)}</span>
                <span className="muted-cell">
                  <Clock size={12} /> {when(hold.held_at)}
                  {hold.label ? ` · ${describeHold(hold)}` : ""}
                  {parkedBefore ? " · held since before today" : ""}
                </span>
              </div>
              <span className="bill-hold-money">
                <Money paise={hold.payload?.net_paise ?? 0} />
              </span>
              <div className="bill-hold-actions">
                <button
                  type="button"
                  className="btn btn-cta"
                  data-testid={`bill-hold-resume-${hold.held_uuid}`}
                  disabled={Boolean(blocked)}
                  onClick={() => onResume(hold)}
                >
                  Resume
                </button>
                {parkedBefore && (
                  <button
                    type="button"
                    className="btn"
                    data-testid={`bill-hold-keep-${hold.held_uuid}`}
                    onClick={() => onKeep(hold)}
                  >
                    Keep for tomorrow
                  </button>
                )}
                <button
                  type="button"
                  className="btn"
                  data-testid={`bill-hold-drop-${hold.held_uuid}`}
                  onClick={() => onLetGo(hold)}
                >
                  Let it go
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** The time it was parked, and the date too once that is no longer today - the
 *  counter reads "11:05" all day and needs "30 Jul, 20:14" the morning after. */
function when(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const clock = at.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
  if (localDay(at) === localDay()) return clock;
  return `${at.toLocaleDateString("en-IN", { day: "numeric", month: "short" })}, ${clock}`;
}
