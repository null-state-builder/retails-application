// A manager's counter PIN, seen by Admin (store operations ticket 06, B76).
//
// Admin can set a manager's PIN - it works at once, on every till after its
// next sync - or clear it. Nobody ever sees a PIN: the card only says "set" or
// "not set", the typed PIN goes straight to the server, which keeps its hash,
// and the box is emptied as soon as the answer comes back. The server decides
// who may do this (Admin only, the login in scope, a fresh password); the card
// only hides what the server has said this reader cannot do.
import { useState } from "react";
import { KeyRound } from "lucide-react";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { Feedback, useResourceDoc, useStepUp } from "../lib/goodsScreen";

interface PinState {
  has_till_pin?: boolean;
  may_hold_till_pin?: boolean;
  may_reset_till_pin?: boolean;
}

export function CounterPinCard({ userId }: { userId: number | string }) {
  const login = useResourceDoc<PinState>(`/goods-v1/auth/admin/users/${userId}`);
  const stepUp = useStepUp();
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  const data = login.doc?.data;
  if (!data || !(data.has_till_pin || data.may_hold_till_pin)) return null;
  const mayChange = Boolean(data.may_reset_till_pin);

  async function run(call: () => Promise<unknown>, said: string) {
    setError("");
    setOk("");
    // The box is emptied once the call ends, whether it worked, failed or the
    // password prompt was cancelled: a PIN never lingers on screen, even if
    // that means typing it again after a cancelled prompt.
    try {
      await stepUp.guarded(call);
      setOk(said);
      login.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
    setPin("");
  }

  function setCounterPin() {
    const typed = pin;
    void run(
      () =>
        api.post(`/goods-v1/auth/admin/users/${userId}/till-pin`, { pin: typed, ...goodsMeta() }),
      "Counter PIN set. It works now, and on each till after its next sync.",
    );
  }

  function resetCounterPin() {
    void run(
      () => api.post(`/goods-v1/auth/admin/users/${userId}/till-pin/reset`, goodsMeta()),
      "Counter PIN cleared. The tills drop it on their next sync.",
    );
  }

  return (
    <div className="pa-counter-pin" data-testid="pa-counter-pin">
      <p className="muted-cell">
        Counter PIN:{" "}
        <b data-testid="pa-counter-pin-state">{data.has_till_pin ? "set" : "not set"}</b>. A manager
        can set their own from Till &amp; Sync. Nobody can see a PIN once it is set.
      </p>
      {stepUp.dialog}
      <Feedback error={error} ok={ok} />
      {mayChange && data.may_hold_till_pin && (
        <div className="form-grid">
          <label htmlFor={`counter-pin-${userId}`} className="muted-cell">
            New counter PIN (4 to 6 digits)
          </label>
          <input
            id={`counter-pin-${userId}`}
            className="input"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            maxLength={6}
            value={pin}
            onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))}
            data-testid="pa-counter-pin-new"
          />
          <button
            className="btn btn-sm"
            onClick={setCounterPin}
            disabled={pin.length < 4}
            data-testid="pa-counter-pin-set"
          >
            <KeyRound size={13} /> Set counter PIN
          </button>
        </div>
      )}
      {mayChange && data.has_till_pin && (
        <button className="btn btn-sm" onClick={resetCounterPin} data-testid="pa-counter-pin-reset">
          <KeyRound size={13} /> Reset counter PIN
        </button>
      )}
    </div>
  );
}
