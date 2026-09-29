import { Money } from "../../../../lib/format";
import { BillDiscountChip } from "./BillDiscountChip";
import { CustomerCard } from "./CustomerCard";
import { LoyaltyRow } from "./LoyaltyRow";
import { MoreTenders, ReceivePaymentSheet } from "./MoreTenders";
import { usePlusPreview } from "./usePlusPreview";
import "./plus.css";

/** The "Counter Plus" preview: extra rail cards, made-up data, nothing saved.
 *  Shown only with `?preview=plus`. Reads the real amount due, never writes it. */
export function PlusRail({
  payablePaise,
  online,
  locked,
}: {
  payablePaise: number;
  online: boolean;
  locked: boolean;
}) {
  const plus = usePlusPreview();
  const { customer, creditPaise, dueDays } = plus.state;
  return (
    <div className="plus-rail" data-testid="plus-rail">
      <p className="plus-banner" role="note">
        Preview: screens only, nothing is saved.
      </p>
      <CustomerCard plus={plus} locked={locked} />
      <LoyaltyRow plus={plus} payablePaise={payablePaise} online={online} locked={locked} />
      <MoreTenders plus={plus} payablePaise={payablePaise} locked={locked} />
      <BillDiscountChip plus={plus} locked={locked} />
      {customer && creditPaise > 0 && (
        <p className="plus-line" data-testid="plus-on-account">
          <span>On account, due in {dueDays} days</span>{" "}
          <strong>
            <Money paise={creditPaise} />
          </strong>
        </p>
      )}
      <ReceivePaymentSheet plus={plus} />
    </div>
  );
}
