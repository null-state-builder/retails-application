// Real tag printing (Rule 12: no vendor SDK — the sandbox has no printer to
// integrate against, and the user wants "the most universal option"). This
// uses the browser's own print dialog, which works with whatever printer is
// already installed on that computer — including the thermal label printers
// (Zebra, TSC, Brother QL...) most stores use, since those install as a normal
// OS printer driver. `@media print` in `TagPrint.css` isolates one 50mm×50mm
// rounded tag per physical label and hides the rest of the page; `window.print()`
// is the only "integration point" a web app gets without a specific printer's
// network API, and the user confirmed that is the option they want.
import { useState } from "react";
import { Printer, X } from "lucide-react";

import "./TagPrint.css";

export interface TagPrintRow {
  barcode: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  item?: string;
  mrp_paise: number | null;
}

function money(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "—";
  return `₹${(paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function Tag({ row }: { row: TagPrintRow }) {
  return (
    <div className="print-tag">
      <p className="print-tag-brand">{row.brand || "(unbranded)"}</p>
      <p className="print-tag-item">{[row.design, row.color, row.size].filter(Boolean).join(" ")}</p>
      <p className="print-tag-mrp">{money(row.mrp_paise)}</p>
      <p className="print-tag-barcode">{row.barcode}</p>
    </div>
  );
}

/** One or many rows — the toolbar's "Print selected tags" and a single row's
 *  "Print tag" button both land here, with the same copies-per-tag dial. */
export function TagPrintModal({ rows, onClose }: { rows: TagPrintRow[]; onClose: () => void }) {
  const [copies, setCopies] = useState(2);
  const many = rows.length > 1;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} data-testid="tag-print-modal" style={{ maxWidth: 420 }}>
        <div className="modal-head">
          <div>
            <p className="eyebrow"><Printer size={14} /> Print {many ? `${rows.length} tags` : "tag"}</p>
            <h3 className="h3">{many ? "Selected pieces" : rows[0]?.brand || "(unbranded)"}</h3>
          </div>
          <button type="button" className="btn btn-sm" onClick={onClose} data-testid="tag-print-close">
            <X size={14} /> Close
          </button>
        </div>

        <div className="card section-card no-print" style={{ marginTop: 14 }} data-testid="tag-preview">
          {rows.map((r) => (
            <div key={r.barcode} style={{ textAlign: "center", padding: many ? "6px 0" : 0 }}>
              <p className="eyebrow">{r.item || "Piece"}</p>
              <p style={{ fontWeight: 700 }}>{r.design} {r.color} {r.size}</p>
              <p className="mono muted-cell">{r.barcode}</p>
              <h2 className="h2" style={{ marginTop: 4 }}>{money(r.mrp_paise)}</h2>
            </div>
          ))}
          {many && <p className="muted-cell" style={{ marginTop: 4 }}>MRP incl. of all taxes, on every tag.</p>}
        </div>

        <label className="field no-print" style={{ marginTop: 14 }}>
          Copies per tag
          <input
            type="number"
            min={1}
            max={20}
            className="input"
            style={{ maxWidth: 100 }}
            value={copies}
            onChange={(e) => setCopies(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
            data-testid="tag-print-copies"
          />
        </label>

        <button
          type="button"
          className="btn btn-cta no-print"
          style={{ marginTop: 14, width: "100%" }}
          onClick={() => window.print()}
          data-testid="tag-print-confirm"
        >
          <Printer size={15} /> Print {rows.length * copies} label{rows.length * copies === 1 ? "" : "s"}
        </button>
        <p className="hint no-print">
          Opens your browser's print dialog — pick whichever printer is installed on this computer.
          Each 50mm × 50mm tag prints on its own label, {copies} cop{copies === 1 ? "y" : "ies"} each.
        </p>

        {/* Print-only: hidden on screen, the only thing `window.print()` sees. */}
        <div className="tag-print-sheet">
          {rows.flatMap((r) => Array.from({ length: copies }, (_, i) => <Tag key={`${r.barcode}-${i}`} row={r} />))}
        </div>
      </div>
    </div>
  );
}

export default TagPrintModal;
