import type { ReactNode } from "react";
import { PauseCircle } from "lucide-react";

export type CounterMode = "sale" | "return";

/** The counter's bill identity and lifecycle controls. It deliberately owns no
 * cart state: Billing keeps the existing hold, draft and actor safeguards.
 *
 * The Look-up button has been removed: the scan hero (F3) is the only door. */
export function BillBar({
  mode,
  nextNumber,
  draftSaved,
  salespeople,
  soldBy,
  differingLines,
  heldCount,
  showingHolds,
  disabled,
  canHold,
  onModeChange,
  onSoldByChange,
  onApplySoldBy,
  onShowHolds,
  onHold,
  onNewBill,
  extraActions,
}: {
  mode: CounterMode;
  nextNumber: string;
  draftSaved: boolean;
  salespeople: { id: string; name: string }[];
  soldBy: string | null;
  differingLines: number;
  heldCount: number;
  showingHolds: boolean;
  disabled: boolean;
  canHold: boolean;
  onModeChange: (mode: CounterMode) => void;
  onSoldByChange: (salesperson: string | null) => void;
  onApplySoldBy: () => void;
  onShowHolds: () => void;
  onHold: () => void;
  onNewBill: () => void;
  /** Buttons that sit before Held bills - the customer display's (ticket 09). */
  extraActions?: ReactNode;
}) {
  return (
    <header className="bill-bar" data-testid="bill-bar">
      <div className="bill-bar-inner">
        {/* Mode toggle + bill identity group, separated from Sold-by by a divider. */}
        <div className="bill-bar-left">
          <div className="bill-mode" role="group" aria-label="Counter mode">
            <button
              type="button"
              className={`bill-mode-button ${mode === "sale" ? "is-active" : ""}`}
              aria-pressed={mode === "sale"}
              title="Sale mode"
              disabled={disabled}
              onClick={() => onModeChange("sale")}
            >
              Sale
            </button>
            <button
              type="button"
              className={`bill-mode-button ${mode === "return" ? "is-active" : ""}`}
              aria-pressed={mode === "return"}
              title="Return & exchange mode"
              disabled={disabled}
              onClick={() => onModeChange("return")}
            >
              Return &amp; exchange
            </button>
          </div>

          <div className="bill-identity" aria-live="polite">
            <strong>
              Next bill <span className="mono">{nextNumber}</span>
            </strong>
            <span className="muted-cell">{draftSaved ? "Draft · saved" : "Ready to scan"}</span>
          </div>
        </div>

        <span className="bill-bar-divider" aria-hidden="true" />

        {/* Sold-by: self-contained field. */}
        <label className="bill-sold-by-field">
          <span>Sold by</span>
          <select
            className="select"
            data-testid="bill-sold-by"
            disabled={disabled}
            value={soldBy ?? ""}
            onChange={(event) => onSoldByChange(event.target.value || null)}
          >
            <option value="">Nobody</option>
            {salespeople.map((salesperson) => (
              <option key={salesperson.id} value={salesperson.id}>
                {salesperson.name}
              </option>
            ))}
          </select>
        </label>
        {differingLines > 0 && soldBy !== null && (
          <button
            type="button"
            className="btn"
            data-testid="bill-apply-sold-by"
            title="Apply the bill-level Sold by choice to every line"
            disabled={disabled}
            onClick={onApplySoldBy}
          >
            Apply to all {differingLines} {differingLines === 1 ? "line" : "lines"}
          </button>
        )}

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
            disabled={disabled || !canHold}
            onClick={onHold}
          >
            <PauseCircle size={15} /> Hold
          </button>
          <button
            type="button"
            className="btn"
            data-testid="bill-new"
            title="New bill (F4)"
            disabled={disabled}
            onClick={onNewBill}
          >
            New bill
          </button>
        </div>
      </div>
    </header>
  );
}
