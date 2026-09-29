import { useState } from "react";
import { CalendarClock } from "lucide-react";

import { api, apiErrorMessage } from "../../../lib/api";
import { isConnectionLost } from "../../../lib/auditLog";
import { formatINR } from "../../../lib/format";
import { RESERVATIONS_API } from "../../../lib/reservations";
import type { Reservation } from "../../../lib/reservations";
import type { ReservationPickup } from "../../../till/cart";
import { RESERVATION_OFFLINE_REFUSAL } from "../../../till/onlineOnly";

/**
 * Reservation pickup at the counter (store operations ticket 20, ST-ORD-1).
 *
 * Staff type the reservation number (or the voucher number, or the customer's
 * mobile); head office answers with the reserved pieces and the advance. The
 * pieces go onto the bill and the advance pays towards it. Online only: offline
 * the card says so, and a bill already carrying a reservation is refused at
 * Save & Print until the connection is back.
 */
export function ReservationPickupCard({
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
  onCollect: (reservation: Reservation) => void;
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
      setNote(RESERVATION_OFFLINE_REFUSAL);
      return;
    }
    setBusy(true);
    setNote("");
    try {
      const response = await api.get<{ reservations: Reservation[] }>(RESERVATIONS_API, {
        params: { store: storeCode, q },
      });
      const found = response.data.reservations;
      const active = found.find((r) => r.status === "active");
      if (active) {
        onCollect(active);
        setOpen(false);
        setTyped("");
      } else if (found[0]) {
        setNote(`${found[0].ref} is ${found[0].status}, so it cannot be collected.`);
      } else {
        setNote("No reservation at this store matches that.");
      }
    } catch (reason) {
      setNote(isConnectionLost(reason) ? RESERVATION_OFFLINE_REFUSAL : apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  if (current) {
    return (
      <section className="bill-consent" data-testid="pickup-card">
        <div className="bill-customer-heading">
          <p className="eyebrow">Reservation pickup</p>
          <span data-testid="pickup-ref">{current.ref}</span>
        </div>
        <p className="muted-cell bill-consent-note" data-testid="pickup-advance">
          {current.advance_paise > 0
            ? `Advance ${formatINR(current.advance_paise)} pays towards this bill.`
            : "No advance was paid."}
        </p>
        {!online && (
          <p className="bill-alert bill-consent-note" data-testid="pickup-offline">
            {RESERVATION_OFFLINE_REFUSAL}
          </p>
        )}
        <button
          type="button"
          className="btn"
          data-testid="pickup-clear"
          disabled={locked}
          onClick={onClear}
        >
          Take the reservation off this bill
        </button>
      </section>
    );
  }

  return (
    <section className="bill-consent" data-testid="pickup-start">
      {!open ? (
        <button
          type="button"
          className="btn bill-business-toggle"
          data-testid="pickup-open"
          disabled={locked}
          onClick={() => {
            setOpen(true);
            setNote(online ? "" : RESERVATION_OFFLINE_REFUSAL);
          }}
        >
          <CalendarClock size={15} /> Reservation pickup
        </button>
      ) : (
        <>
          <label className="field">
            <span>Reservation number, voucher number or mobile</span>
            <input
              className="input"
              data-testid="pickup-number"
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
              data-testid="pickup-find"
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
        <p className="bill-alert bill-consent-note" data-testid="pickup-note">
          {note}
        </p>
      )}
    </section>
  );
}
