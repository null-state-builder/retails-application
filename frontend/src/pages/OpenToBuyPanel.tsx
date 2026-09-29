import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { STAGE_CHIP, STAGE_LABEL, nextStep, type OtbCheck } from "../lib/openToBuy";
import { OversTable } from "./OpenToBuy";

/** What open-to-buy says about one draft booking (ticket 39), on the booking page.
 *
 *  Drawn for a buyer who sees cost. Nothing shows where open-to-buy does not
 *  apply (switched off where the booking goes, or no budget). Over it, the buyer
 *  asks the Owner here; the Owner decides in the approvals inbox; the booking's
 *  own Confirm button then numbers it. */
export function OpenToBuyPanel({
  bookingId,
  revision,
  reviewedHash,
  nonce,
}: {
  bookingId: string;
  revision: number;
  reviewedHash: string;
  nonce: number;
}) {
  const [check, setCheck] = useState<OtbCheck | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  /** The ask whose answer was lost: pressing again replays it, never asks twice. */
  const pending = useRef<{ revision: number; hash: string; commandId: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const answer = await api.get<OtbCheck>(`/goods-v1/bookings/${bookingId}/open-to-buy`);
      setLost(false);
      setCheck(answer.data);
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      // A login refused here (no cost over this booking) is simply not shown it.
      else setCheck(null);
    }
  }, [bookingId]);

  useEffect(() => {
    void load();
  }, [load, nonce, revision]);

  async function askOwner() {
    if (!navigator.onLine) {
      setLost(true);
      return;
    }
    setBusy(true);
    setError("");
    setDone("");
    const same =
      pending.current !== null &&
      pending.current.revision === revision &&
      pending.current.hash === reviewedHash;
    const commandId = same && pending.current ? pending.current.commandId : crypto.randomUUID();
    pending.current = { revision, hash: reviewedHash, commandId };
    try {
      const answer = await api.post<OtbCheck>(
        `/goods-v1/bookings/${bookingId}/open-to-buy/request-approval`,
        { reviewed_hash: reviewedHash, ...goodsMeta(revision, commandId) },
      );
      pending.current = null;
      setCheck(answer.data);
      setDone("Sent to the Owner. They decide in the approvals inbox.");
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
      } else {
        pending.current = null;
        setError(apiErrorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  if (lost && !check) {
    return (
      <p className="warn-note" data-testid="otb-panel-offline" role="status">
        Open-to-buy cannot be read without a connection.{" "}
        <button type="button" className="btn" onClick={() => void load()}>
          Try again
        </button>
      </p>
    );
  }
  if (!check || !check.applies) return null;

  return (
    <section className="card section-card" data-testid="otb-panel">
      <div className="toolbar">
        <h2 className="h3">Open-to-buy</h2>
        <span
          className={`chip ${check.over ? (STAGE_CHIP[check.stage] ?? "") : "chip-green"}`}
          data-testid="otb-panel-stage"
        >
          {check.over ? (STAGE_LABEL[check.stage] ?? check.stage) : "Within open-to-buy"}
        </span>
      </div>
      <p data-testid="otb-panel-next">{nextStep(check)}</p>
      {lost && (
        <p className="warn-note" data-testid="otb-panel-offline" role="status">
          The connection dropped. Nothing was lost; press again when it is back.
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="otb-panel-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="otb-panel-done">
          {done}
        </p>
      )}
      {check.over && (
        <OversTable overs={check.overs} budgets={check.budgets} testId="otb-panel-overs" />
      )}
      {check.ask?.approval?.status === "rejected" && check.ask.approval.reason && (
        <p className="muted" data-testid="otb-panel-reason">
          The Owner said: {check.ask.approval.reason}
        </p>
      )}
      <div className="otb-actions">
        {check.allowed_actions.includes("ask_owner") && (
          <button
            type="button"
            className="btn btn-cta"
            data-testid="otb-ask-owner"
            disabled={busy}
            onClick={() => void askOwner()}
          >
            Ask the Owner to approve
          </button>
        )}
        {check.ask && (
          <Link
            className="btn"
            to={`/booking/open-to-buy/${check.ask.id}`}
            data-testid="otb-panel-ask"
          >
            Open the request
          </Link>
        )}
      </div>
    </section>
  );
}
