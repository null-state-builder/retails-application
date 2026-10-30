import type { ReactNode } from "react";
import { PauseCircle } from "lucide-react";
import { SegmentedControl } from "../../../components/SegmentedControl";

export type CounterMode = "sale" | "return";

/** The counter's bill identity and lifecycle controls. It deliberately owns no
 * cart state: Billing keeps the existing hold, draft and actor safeguards.
 *
 * The Look-up button has been removed: the scan hero (F3) is the only door.
 * The bill-level Sold by moved to the payment rail (`SoldBy`, design system v2
 * TillLayout: customer, then salesperson). */
export function BillBar({
  mode,
  nextNumber,
  draftSaved,
  heldCount,
  showingHolds,
  disabled,
  canHold,
  onModeChange,
  onShowHolds,
  onHold,
  onNewBill,
  extraActions,
}: {
  mode: CounterMode;
  nextNumber: string;
  draftSaved: boolean;
  heldCount: number;
  showingHolds: boolean;
  disabled: boolean;
  canHold: boolean;
  onModeChange: (mode: CounterMode) => void;
  onShowHolds: () => void;
  onHold: () => void;
  onNewBill: () => void;
  /** Buttons that sit before Held bills - the customer display's (ticket 09). */
  extraActions?: ReactNode;
}) {
  return (
    <header className="bill-bar" data-testid="bill-bar">
      <div className="bill-bar-inner">
        <div className="bill-bar-left">
          <SegmentedControl
            label="Counter mode"
            disabled={disabled}
            value={mode}
            options={[
              { value: "sale", label: "Sale", title: "Sale mode" },
              {
                value: "return",
                label: "Return & exchange",
                title: "Return & exchange mode",
              },
            ]}
            onChange={onModeChange}
          />

          <div className="bill-identity" aria-live="polite">
            <strong>
              Next bill <span className="mono">{nextNumber}</span>
            </strong>
            <span className="muted-cell">{draftSaved ? "Draft · saved" : "Ready to scan"}</span>
          </div>
        </div>

        {/* Held bills, Hold, and New bill pushed far right. */}
        <div className="bill-bar-actions">
          {extraActions}
          <button
            type="button"
            className="btn"
            data-testid="bill-holds-open"
            aria-expanded={showingHolds}
            title="Held bills (no keyboard shortcut)"
            disabled={disabled}
            onClick={onShowHolds}
          >
            Held bills ({heldCount})
          </button>
          <button
            type="button"
            className="btn"
            data-testid="bill-hold"
            title="Hold bill (F2)"
            aria-keyshortcuts="F2"
            disabled={disabled || !canHold}
            onClick={onHold}
          >
            <PauseCircle size={15} /> Hold
            <kbd className="bill-key" aria-hidden="true">
              F2
            </kbd>
          </button>
          <button
            type="button"
            className="btn"
            data-testid="bill-new"
            title="New bill (F4)"
            aria-keyshortcuts="F4"
            disabled={disabled}
            onClick={onNewBill}
          >
            New bill
            <kbd className="bill-key" aria-hidden="true">
              F4
            </kbd>
          </button>
        </div>
      </div>
    </header>
  );
}
