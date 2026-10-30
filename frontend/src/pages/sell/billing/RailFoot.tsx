import { CircleAlert } from "lucide-react";

import { Money } from "../../../lib/format";
import type { CounterMode } from "./BillBar";

/** The rail's fixed commit point, below the scrollable customer and payment
 *  sections: To pay at 44px, then Save & print (design system v2 TillLayout and
 *  TotalsStrip - the one big figure, directly above the one action that commits
 *  money). */
export function RailFoot({
  blocked,
  saving,
  mode,
  paper,
  lastBillNumber,
  onReprint,
  onSave,
  duePaise = null,
}: {
  /** The amount due; null while there is none yet - a return before its
   *  original bill is found. */
  duePaise?: number | null;
  blocked: string;
  saving: boolean;
  mode: CounterMode;
  paper: number | null;
  lastBillNumber: string | null;
  onReprint: () => void;
  onSave: () => void;
}) {
  const primaryLabel = saving
    ? "Saving…"
    : paper !== null
      ? `Save bill ${paper}`
      : mode === "return"
        ? "Exchange & print"
        : "Save & print";

  return (
    <footer className="bill-rail-foot">
      <p
        className="bill-rail-due"
        data-testid={duePaise !== null ? "bill-rail-due" : undefined}
        data-ready={duePaise !== null ? undefined : "false"}
      >
        <span className="bill-rail-due-label" id="bill-due-label">
          To pay
        </span>
        <output className="bill-due" data-testid="bill-due" aria-labelledby="bill-due-label">
          {duePaise !== null ? <Money paise={duePaise} /> : <>Nothing yet &middot; ₹0</>}
        </output>
      </p>
      {blocked && (
        <p className="bill-rail-blocked" data-testid="bill-blocked">
          <CircleAlert size={14} />
          {blocked}
        </p>
      )}
      <div className="bill-rail-actions">
        <button
          type="button"
          className="btn bill-rail-reprint"
          data-testid="bill-reprint"
          title="Reprint (no keyboard shortcut)"
          disabled={!lastBillNumber || saving}
          onClick={onReprint}
        >
          Reprint
          {lastBillNumber && <span className="mono">{lastBillNumber}</span>}
        </button>
        <button
          type="button"
          className="btn btn-cta btn-lg bill-rail-save"
          data-testid="bill-save"
          title={mode === "return" ? "Exchange & Print (F9)" : "Save & Print (F9)"}
          aria-keyshortcuts="F9"
          disabled={Boolean(blocked) || saving}
          onClick={onSave}
        >
          {primaryLabel}
          {!saving && (
            <kbd className="bill-key" aria-hidden="true">
              F9
            </kbd>
          )}
        </button>
      </div>
    </footer>
  );
}
