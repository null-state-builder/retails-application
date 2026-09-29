import { Award } from "lucide-react";

import { Money } from "../../../../lib/format";
import { maxPointsFor, pointsEarned, pointsValuePaise } from "./usePlusPreview";
import type { Plus } from "./usePlusPreview";

/** Points at checkout: what they have, use some, see what this bill earns.
 *  Online only (PRD ST-CUS-3), so an offline till says so instead of hiding it. */
export function LoyaltyRow({
  plus,
  payablePaise,
  online,
  locked,
}: {
  plus: Plus;
  payablePaise: number;
  online: boolean;
  locked: boolean;
}) {
  const { customer, usePointsCount } = plus.state;
  if (!customer) return null;
  const max = maxPointsFor(customer, payablePaise);
  const use = Math.min(usePointsCount, max);
  const paidPaise = Math.max(0, payablePaise - pointsValuePaise(use));
  return (
    <section className="plus-card" data-testid="plus-loyalty">
      <header className="plus-head">
        <h3 className="eyebrow">Points</h3>
        <span data-testid="plus-balance">
          {customer.points.toLocaleString("en-IN")} ={" "}
          <Money paise={pointsValuePaise(customer.points)} />
        </span>
      </header>
      {!online ? (
        <p className="plus-note">
          Points need the connection. Take another payment, or wait for the connection.
        </p>
      ) : (
        <>
          <div className="plus-row">
            <div className="plus-stepper" role="group" aria-label="Points to use">
              <button
                type="button"
                className="btn plus-step"
                aria-label="Fewer points"
                disabled={locked || use <= 0}
                onClick={() => plus.patch({ usePointsCount: Math.max(0, use - 100) })}
              >
                −
              </button>
              <output className="mono" data-testid="plus-use">
                {use}
              </output>
              <button
                type="button"
                className="btn plus-step"
                aria-label="More points"
                disabled={locked || use >= max}
                onClick={() => plus.patch({ usePointsCount: Math.min(max, use + 100) })}
              >
                +
              </button>
            </div>
            <button
              type="button"
              className="btn plus-btn"
              data-testid="plus-use-all"
              disabled={locked || max === 0}
              onClick={() => plus.patch({ usePointsCount: max })}
            >
              Use all
            </button>
          </div>
          {use > 0 && (
            <p className="plus-line">
              <span>Points take off</span>{" "}
              <strong>
                −<Money paise={pointsValuePaise(use)} />
              </strong>
            </p>
          )}
          <p className="plus-earn" data-testid="plus-earn">
            <Award size={14} aria-hidden /> You'll earn {pointsEarned(paidPaise)} points on this
            bill
          </p>
        </>
      )}
    </section>
  );
}
