import { useEffect, useRef, useState } from "react";
import { AlertTriangle, KeyRound } from "lucide-react";

import { Money } from "../../lib/format";
import { CASH_VARIANCE, LATE_RETURN, whoAuthorised } from "../../till/pin";
import type { Ask, Authorisation, AuthorisationKind } from "../../till/pin";
import type { TillManager } from "../../till/types";
// The house modal (`.modal-backdrop` / `.modal` / `.modal-head`), which every
// screen with a dialog on it borrows from the same place.
import "../Booking.css";

// ---------------------------------------------------------------------------
// The manager's tap, wherever the counter asks for it (#182, #273)
// ---------------------------------------------------------------------------
//
// One modal for every counter exception: a named manager of this store,
// established by a PIN typed on the device. Keeping the prompt here leaves one
// lock-out rule, one ambiguous-PIN rule, and one "no manager has a PIN yet"
// sentence.
//
// The lock-out count is deliberately **not** held in here. A count that lived in
// the modal would be cleared by closing it, and closing it is one click - so
// three tries per half minute would become three tries per click, which is no
// limit at all. The screen holds it and passes it in.

/** How many wrong PINs the modal takes before it makes somebody wait, and how
 *  long it makes them wait. */
export const WRONG_PINS_BEFORE_A_PAUSE = 3;
export const PAUSE_MS = 30_000;

/**
 * The wrong-PIN count a screen holds on the modal's behalf, and its own timer.
 *
 * A hook rather than state inside the modal, and the reason is the whole of the
 * limit: a count that lived in the modal would be cleared by closing it, and
 * closing it is one click - so three tries per half minute would become three
 * tries per click, which is no limit at all. Every counter prompt gets the same
 * pause from the same three lines.
 */
export function useWrongPins(): { wrong: number; wasWrong: () => void; clear: () => void } {
  const [wrong, setWrong] = useState(0);
  useEffect(() => {
    if (wrong < WRONG_PINS_BEFORE_A_PAUSE) return;
    const timer = setTimeout(() => setWrong(0), PAUSE_MS);
    return () => clearTimeout(timer);
  }, [wrong]);
  return {
    wrong,
    wasWrong: () => setWrong((n) => n + 1),
    clear: () => setWrong(0),
  };
}

/** What each authorisation kind is called on the shop floor. */
const KIND_WORDS: Record<AuthorisationKind, string> = {
  [LATE_RETURN]: "taking it back after the return window closed",
  [CASH_VARIANCE]: "the counted cash does not match the expected cash",
};

/**
 * The manager's PIN (#182).
 *
 * Checked here, on the device, against the hash the dataset sent - the counter
 * may have had no line for a week. The manager types a PIN and not a name:
 * whose it is is what the PIN establishes, and it is what the bill records.
 *
 * No `alert`, no `confirm`, and Escape closes it. Everything else on this page
 * is a visible button (Anand's Phase-3 ruling) and so is everything here.
 */
export function ManagerPin({
  managers,
  asks,
  cashierId = null,
  lead = "This bill needs approving:",
  footnote = "The manager types it themselves. Their name, the time, and what they approved go on the bill.",
  wrong,
  onWrong,
  onClose,
  onAuthorised,
}: {
  managers: TillManager[];
  asks: Ask[];
  /** Ticket 06: the person signed in at this counter, set only where the
   *  store's switch is on. Their own PIN approves nothing here. */
  cashierId?: number | null;
  /** The sentence above what is being approved, and the note under the PIN
   *  box. A bill's by default; the cash count (ticket 41) says its own. */
  lead?: string;
  footnote?: string;
  /** Wrong PINs so far at this counter - held by the screen, not by this modal,
   *  because a modal's own count is cleared by closing it, and closing it is one
   *  click. See `Counter`. */
  wrong: number;
  onWrong: () => void;
  onClose: () => void;
  /** `pin` is handed on only for an online check the server repeats (the cash
   *  count, ticket 41); a bill never sends it anywhere. */
  onAuthorised: (authorisation: Authorisation, pin: string) => void;
}) {
  const [pin, setPin] = useState("");
  const [checking, setChecking] = useState(false);
  const [refused, setRefused] = useState("");
  const box = useRef<HTMLInputElement>(null);
  const waiting = wrong >= WRONG_PINS_BEFORE_A_PAUSE;

  useEffect(() => {
    box.current?.focus();
  }, []);

  async function check() {
    if (checking || waiting || !pin) return;
    setChecking(true);
    setRefused("");
    try {
      const attempt = await whoAuthorised(managers, pin, asks, new Date(), { cashierId });
      if (attempt.selfApproval) {
        // Not a wrong PIN, so it does not count towards the pause: it is the
        // right PIN of the wrong person, and retrying it can never work.
        setRefused(
          asks.some((ask) => ask.kind === CASH_VARIANCE)
            ? "You cannot confirm your own count. Another manager of this store has to type their PIN."
            : "You cannot approve your own bill. Another manager of this store has to type their PIN.",
        );
        setPin("");
        box.current?.focus();
        return;
      }
      if (!attempt.authorisation) {
        // A wrong PIN and a PIN belonging to a manager of another store are the
        // same answer here, and telling them apart would be telling whoever is
        // standing there which. A *shared* PIN is not - it is a thing an
        // administrator has to fix, and no amount of retrying will help.
        setRefused(
          attempt.matched > 1
            ? "More than one manager here uses that PIN, so the bill could not say which of them approved it. One of them has to change theirs."
            : "That is not a manager's PIN for this store.",
        );
        onWrong();
        setPin("");
        box.current?.focus();
        return;
      }
      onAuthorised(attempt.authorisation, pin);
    } finally {
      setChecking(false);
    }
  }

  return (
    <div className="modal-backdrop" data-testid="bill-pin-modal" onClick={onClose}>
      <div
        className="modal bill-pin"
        role="dialog"
        aria-label="Manager approval"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
        }}
      >
        <div className="modal-head">
          <h3 className="h3">
            <KeyRound size={17} style={{ verticalAlign: "-3px", marginRight: 6 }} />
            Manager approval
          </h3>
          <button type="button" className="btn" data-testid="bill-pin-cancel" onClick={onClose}>
            Cancel
          </button>
        </div>

        <p className="lead">{lead}</p>
        <ul className="bill-asks" data-testid="bill-pin-asks">
          {asks.map((ask) => (
            <li key={`${ask.kind}/${ask.ref}`}>
              {ask.label} · {KIND_WORDS[ask.kind]} · <Money paise={ask.paise} />
            </li>
          ))}
        </ul>

        {managers.length === 0 ? (
          <p className="warn-note" data-testid="bill-pin-nobody">
            No manager of this store has a counter PIN yet. One is set from Till &amp; Sync, by the
            manager themselves - and only somebody who may approve selling here can hold one.
          </p>
        ) : (
          <>
            <div className="field">
              <label htmlFor="bill-pin">Manager PIN</label>
              <input
                ref={box}
                id="bill-pin"
                className="input"
                data-testid="bill-pin"
                type="password"
                inputMode="numeric"
                autoComplete="off"
                disabled={checking || waiting}
                value={pin}
                onChange={(e) => setPin(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key !== "Enter") return;
                  e.preventDefault();
                  void check();
                }}
              />
            </div>
            {refused && (
              <p className="bill-alert" data-testid="bill-pin-refused">
                <AlertTriangle size={15} />
                {refused}
              </p>
            )}
            {waiting && (
              <p className="warn-note" data-testid="bill-pin-waiting">
                Too many wrong PINs. This waits half a minute before it will take another.
              </p>
            )}
            <p className="muted-cell">{footnote}</p>
            <button
              type="button"
              className="btn btn-cta"
              data-testid="bill-pin-approve"
              disabled={checking || waiting || !pin}
              onClick={() => void check()}
            >
              {checking ? "Checking…" : "Approve"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
