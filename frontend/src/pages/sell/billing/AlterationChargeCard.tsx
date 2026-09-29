import { useState } from "react";
import { Scissors } from "lucide-react";

import { chargePaiseFrom } from "../../../till/alteration";
import type { TillAlterationCharge } from "../../../till/types";

/**
 * A paid alteration's charge at the counter (store operations ticket 22, ST-ORD-3).
 *
 * Staff type what the customer pays for the work; it goes on the bill as its
 * own line at the fixed rate head office sends (5% under SAC 9988). A free
 * alteration needs no line. The job card - what to alter, measurements, the
 * tailor, the promised date - is made afterwards on Customer Orders,
 * Alterations, from this bill. Works offline like any line.
 */
export function AlterationChargeCard({
  charge,
  locked,
  onAdd,
}: {
  charge: TillAlterationCharge;
  locked: boolean;
  onAdd: (chargePaise: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [note, setNote] = useState("");

  function add() {
    const paise = chargePaiseFrom(typed);
    if (paise === null) {
      setNote("Type the charge in rupees, more than nothing. A free alteration needs no line.");
      return;
    }
    onAdd(paise);
    setTyped("");
    setNote("");
    setOpen(false);
  }

  return (
    <section className="bill-consent" data-testid="alteration-charge">
      {!open ? (
        <button
          type="button"
          className="btn bill-business-toggle"
          data-testid="alteration-open"
          disabled={locked}
          onClick={() => setOpen(true)}
        >
          <Scissors size={15} /> Alteration charge
        </button>
      ) : (
        <>
          <label className="field">
            <span>
              Charge for the alteration (Rs), GST {Number(charge.gst_rate)}% included, SAC{" "}
              {charge.sac}
            </span>
            <input
              className="input"
              inputMode="decimal"
              data-testid="alteration-amount"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  add();
                }
              }}
            />
          </label>
          <p className="muted-cell bill-consent-note">
            A free alteration needs no line. Make the job card on Customer Orders, Alterations after
            saving the bill.
          </p>
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="alteration-add"
              disabled={locked}
              onClick={add}
            >
              Add to bill
            </button>
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              Close
            </button>
          </div>
        </>
      )}
      {note && (
        <p className="bill-alert bill-consent-note" data-testid="alteration-note">
          {note}
        </p>
      )}
    </section>
  );
}
