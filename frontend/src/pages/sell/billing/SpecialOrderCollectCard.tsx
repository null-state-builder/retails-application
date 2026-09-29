import { useState } from "react";
import { PackageCheck } from "lucide-react";

import { api, apiErrorMessage } from "../../../lib/api";
import { isConnectionLost } from "../../../lib/auditLog";
import { formatINR } from "../../../lib/format";
import { SPECIAL_ORDERS_API, collectable, statusChip } from "../../../lib/specialOrders";
import type { SpecialOrder } from "../../../lib/specialOrders";
import type { ReservationPickup } from "../../../till/cart";
import { SPECIAL_ORDER_OFFLINE_REFUSAL } from "../../../till/onlineOnly";

/**
 * Special order collection at the counter (store operations ticket 21, ST-ORD-2).
 *
 * Staff type the order number (or the voucher number, or the customer's
 * mobile); head office answers with the order. The piece that arrived is
 * scanned onto the bill and the advance pays towards it. Online only: offline
 * the card says so, and a bill already carrying an order is refused at Save &
 * Print until the connection is back.
 */
export function SpecialOrderCollectCard({
  storeCode,
  online,
  locked,
  current,
  onCollect,
  onClear,
}: {
  storeCode: string;
  online: boolean;
  locked: boolean;
  current: ReservationPickup | null;
  onCollect: (order: SpecialOrder) => void;
  onClear: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function find() {
    const q = typed.trim();
    if (!q) return;
    if (!navigator.onLine) {
      setNote(SPECIAL_ORDER_OFFLINE_REFUSAL);
      return;
    }
    setBusy(true);
    setNote("");
    try {
      const response = await api.get<{ special_orders: SpecialOrder[] }>(SPECIAL_ORDERS_API, {
        params: { store: storeCode, q },
      });
      const found = response.data.special_orders;
      const ready = found.find(collectable);
      if (ready) {
        onCollect(ready);
        setOpen(false);
        setTyped("");
      } else if (found.length) {
        setNote(
          `${found[0].ref} is ${statusChip(found[0].status).label.toLowerCase()}, so it cannot be collected yet. Tell the customer first on the Special orders screen.`,
        );
      } else {
        setNote("No special order at this store matches that.");
      }
    } catch (reason) {
      setNote(isConnectionLost(reason) ? SPECIAL_ORDER_OFFLINE_REFUSAL : apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  if (current) {
    return (
      <section className="bill-consent" data-testid="special-collect-card">
        <div className="bill-customer-heading">
          <p className="eyebrow">Special order collection</p>
          <span data-testid="special-collect-ref">{current.ref}</span>
        </div>
        <p className="muted-cell bill-consent-note" data-testid="special-collect-advance">
          {current.advance_paise > 0
            ? `Advance ${formatINR(current.advance_paise)} pays towards this bill.`
            : "No advance was paid."}
        </p>
        {!online && (
          <p className="bill-alert bill-consent-note" data-testid="special-collect-offline">
            {SPECIAL_ORDER_OFFLINE_REFUSAL}
          </p>
        )}
        <button
          type="button"
          className="btn"
          data-testid="special-collect-clear"
          disabled={locked}
          onClick={onClear}
        >
          Take the special order off this bill
        </button>
      </section>
    );
  }

  return (
    <section className="bill-consent" data-testid="special-collect-start">
      {!open ? (
        <button
          type="button"
          className="btn bill-business-toggle"
          data-testid="special-collect-open"
          disabled={locked}
          onClick={() => {
            setOpen(true);
            setNote(online ? "" : SPECIAL_ORDER_OFFLINE_REFUSAL);
          }}
        >
          <PackageCheck size={15} /> Special order collection
        </button>
      ) : (
        <>
          <label className="field">
            <span>Order number, voucher number or mobile</span>
            <input
              className="input"
              data-testid="special-collect-number"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void find();
                }
              }}
            />
          </label>
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="special-collect-find"
              disabled={busy || !online}
              onClick={() => void find()}
            >
              Find
            </button>
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              Close
            </button>
          </div>
        </>
      )}
      {note && (
        <p className="bill-alert bill-consent-note" data-testid="special-collect-note">
          {note}
        </p>
      )}
    </section>
  );
}
