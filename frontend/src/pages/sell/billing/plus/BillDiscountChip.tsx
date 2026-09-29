import { useState } from "react";
import { Percent } from "lucide-react";

import { Money } from "../../../../lib/format";
import { RupeeInput } from "../RupeeInput";
import type { Plus } from "./usePlusPreview";

/** One discount for the whole bill, with a manager PIN. PRD B62 says bill
 *  discounts come from offers only, so this is marked as needing a ruling. */
export function BillDiscountChip({ plus, locked }: { plus: Plus; locked: boolean }) {
  const [open, setOpen] = useState(false);
  const { discountPaise } = plus.state;
  return (
    <section className="plus-card" data-testid="plus-discount">
      <header className="plus-head">
        <h3 className="eyebrow">Bill discount</h3>
        <span className="plus-ruling">Needs a ruling</span>
      </header>
      <button
        type="button"
        className="btn plus-btn plus-wide"
        aria-expanded={open}
        disabled={locked}
        onClick={() => setOpen(!open)}
      >
        <Percent size={16} aria-hidden />{" "}
        {discountPaise > 0 ? (
          <>
            Bill discount <Money paise={discountPaise} />
          </>
        ) : (
          "Add bill discount"
        )}
      </button>
      {open && (
        <div className="plus-fields">
          <RupeeInput
            placeholder="0"
            testId="plus-discount-amount"
            label="Discount amount"
            paise={discountPaise}
            locked={locked}
            onChange={(p) => plus.patch({ discountPaise: p ?? 0 })}
          />
          <input className="input plus-input" placeholder="Reason" aria-label="Reason" />
          <p className="plus-note">
            A manager types their own PIN to approve. Shown here only, nothing is taken off the real
            bill.
          </p>
        </div>
      )}
    </section>
  );
}
