import { CircleAlert } from "lucide-react";

import { Money } from "../../../lib/format";
import type { CounterMode } from "./BillBar";

/** The rail's fixed commit point, below the scrollable payment and customer tiles. */
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
  /** The amount due, shown beside Save & print only where the rail stacks
   *  under the lines (tablet upright, phone); null while there is none yet. */
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
      {blocked && (
        <p className="bill-rail-blocked" data-testid="bill-blocked">
          <CircleAlert size={14} />
          {blocked}
        </p>
      )}
      <div className="bill-rail-actions">
        {duePaise !== null && (
          <p className="bill-rail-due" data-testid="bill-rail-due">
            <span>To pay</span>
            <strong className="tabular">
              <Money paise={duePaise} />
            </strong>
          </p>
        )}
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
          className="btn btn-cta bill-rail-save"
          data-testid="bill-save"
          title={mode === "return" ? "Exchange & Print (F9)" : "Save & Print (F9)"}
          disabled={Boolean(blocked) || saving}
          onClick={onSave}
        >
          {primaryLabel}
          {!saving && <span className="bill-rail-key mono">F9</span>}
        </button>
      </div>
    </footer>
  );
}
