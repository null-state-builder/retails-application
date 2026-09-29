import { useState } from "react";
import { Gift } from "lucide-react";

import { api, apiErrorMessage } from "../../../lib/api";
import { isConnectionLost } from "../../../lib/auditLog";
import { formatINR, rupeesToPaise } from "../../../lib/format";
import {
  GIFT_VOUCHERS_API,
  GIFT_VOUCHER_LOOKUP_API,
  emptySellDraft,
  sellProblem,
  voucherSlipHtml,
} from "../../../lib/giftVouchers";
import type { GiftVoucher, GiftVoucherSold, SellDraft } from "../../../lib/giftVouchers";
import { TENDERS, dayText } from "../../../lib/reservations";
import type { Tender } from "../../../lib/reservations";
import { GIFT_VOUCHER_OFFLINE_REFUSAL, normaliseVoucherNumber } from "../../../till/giftVoucher";
import type { GiftVoucherHeld, GiftVoucherUse } from "../../../till/giftVoucher";

/**
 * Gift vouchers at the counter (store operations ticket 19, ST-POS-4).
 *
 * **Take one.** Staff type the voucher's number and the code on its slip (the
 * number alone runs in sequence, B311); head office answers with what
 * it holds and its last day, or refuses (expired, used up, another GSTIN). It
 * then pays towards this bill up to what it holds; the rest stays on it.
 *
 * **Sell one.** A value, paid in cash, card or UPI. Head office issues the GV
 * number and a random code, and the slip prints both. No GST: GST is charged on the goods bought with
 * it (Circular 243/37/2024).
 *
 * Both are online only: offline the card says so, keeps what was typed, and a
 * bill already carrying a voucher is refused at Save & Print until the
 * connection is back. A sale whose answer did not come back - the line dropped,
 * a timeout, a server error - is sent again under the same id, so it is never
 * sold twice; only a refusal head office gave starts a new one.
 */
export function GiftVoucherCard({
  storeCode,
  online,
  locked,
  held,
  onTake,
  onRemove,
  onPrint,
}: {
  storeCode: string;
  online: boolean;
  locked: boolean;
  held: GiftVoucherUse[];
  onTake: (voucher: GiftVoucherHeld) => void;
  onRemove: (number: string) => void;
  onPrint: (html: string) => void;
}) {
  const [doing, setDoing] = useState<"" | "take" | "sell">("");
  const [typed, setTyped] = useState("");
  const [typedCode, setTypedCode] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<SellDraft>(emptySellDraft);
  // One sale's identity across retries: kept while the answer is lost.
  const [saleId, setSaleId] = useState(() => crypto.randomUUID());
  const [sold, setSold] = useState<GiftVoucherSold | null>(null);

  function open(which: "take" | "sell") {
    setDoing(which);
    setNote(online ? "" : GIFT_VOUCHER_OFFLINE_REFUSAL);
  }

  async function take() {
    const number = normaliseVoucherNumber(typed);
    const code = typedCode.trim();
    if (!number) return;
    if (!code) {
      setNote("Type the code printed on the voucher, under its number.");
      return;
    }
    if (held.some((voucher) => voucher.number === number)) {
      setNote(`${number} is already on this bill.`);
      return;
    }
    if (!navigator.onLine) {
      setNote(GIFT_VOUCHER_OFFLINE_REFUSAL);
      return;
    }
    setBusy(true);
    setNote("");
    try {
      const found = await api.get<GiftVoucher>(GIFT_VOUCHER_LOOKUP_API, {
        params: { store: storeCode, number, code },
      });
      onTake({
        number: found.data.number,
        code,
        balance_paise: found.data.balance_paise,
        valid_until: found.data.valid_until,
      });
      setTyped("");
      setTypedCode("");
      setDoing("");
    } catch (reason) {
      setNote(isConnectionLost(reason) ? GIFT_VOUCHER_OFFLINE_REFUSAL : apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function sell() {
    const problem = sellProblem(draft);
    if (problem) {
      setNote(problem);
      return;
    }
    if (!navigator.onLine) {
      setNote(GIFT_VOUCHER_OFFLINE_REFUSAL);
      return;
    }
    setBusy(true);
    setNote("");
    try {
      const response = await api.post<GiftVoucherSold>(GIFT_VOUCHERS_API, {
        id: saleId,
        store: storeCode,
        value_paise: rupeesToPaise(draft.value),
        mode: draft.mode,
        reference: draft.reference.trim(),
      });
      setSold(response.data);
      setDraft(emptySellDraft());
      setSaleId(crypto.randomUUID());
      setDoing("");
      onPrint(voucherSlipHtml(response.data));
    } catch (reason) {
      if (isConnectionLost(reason)) {
        // Kept as typed, under the same id: sending again cannot sell it twice.
        setNote(`${GIFT_VOUCHER_OFFLINE_REFUSAL} Press Sell voucher again once it is back.`);
      } else if (headOfficeRefused(reason)) {
        setSaleId(crypto.randomUUID());
        setNote(apiErrorMessage(reason));
      } else {
        // A timeout or a server error: it may have been sold. Same id again.
        setNote(`${apiErrorMessage(reason)} Press Sell voucher again: it will not be sold twice.`);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="bill-consent" data-testid="gift-voucher-card">
      <div className="bill-customer-heading">
        <p className="eyebrow">Gift vouchers</p>
      </div>
      {held.map((voucher) => (
        <div key={voucher.number} className="bill-consent-note" data-testid="gift-voucher-held">
          <span data-testid="gift-voucher-held-number">{voucher.number}</span>{" "}
          <span className="muted-cell" data-testid="gift-voucher-held-pays">
            pays {formatINR(voucher.pays_paise)} of {formatINR(voucher.balance_paise)}; use by{" "}
            {dayText(voucher.valid_until)}
          </span>{" "}
          <button
            type="button"
            className="btn"
            data-testid="gift-voucher-remove"
            disabled={locked}
            onClick={() => onRemove(voucher.number)}
          >
            Take off this bill
          </button>
        </div>
      ))}
      {held.length > 0 && !online && (
        <p className="bill-alert bill-consent-note" data-testid="gift-voucher-offline">
          {GIFT_VOUCHER_OFFLINE_REFUSAL}
        </p>
      )}
      {doing === "" && (
        <div className="toolbar">
          <button
            type="button"
            className="btn bill-business-toggle"
            data-testid="gift-voucher-take-open"
            disabled={locked}
            onClick={() => open("take")}
          >
            <Gift size={15} /> Take a gift voucher
          </button>
          <button
            type="button"
            className="btn bill-business-toggle"
            data-testid="gift-voucher-sell-open"
            disabled={locked}
            onClick={() => open("sell")}
          >
            <Gift size={15} /> Sell a gift voucher
          </button>
        </div>
      )}
      {doing === "take" && (
        <>
          <label className="field">
            <span>Voucher number</span>
            <input
              className="input"
              data-testid="gift-voucher-number"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Code on the voucher</span>
            <input
              className="input"
              autoComplete="off"
              data-testid="gift-voucher-code"
              value={typedCode}
              onChange={(e) => setTypedCode(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void take();
                }
              }}
            />
          </label>
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="gift-voucher-find"
              disabled={busy || !online || locked}
              onClick={() => void take()}
            >
              Find
            </button>
            <button type="button" className="btn" onClick={() => setDoing("")}>
              Close
            </button>
          </div>
        </>
      )}
      {doing === "sell" && (
        <>
          <label className="field">
            <span>Value (Rs)</span>
            <input
              className="input"
              inputMode="decimal"
              data-testid="gift-voucher-value"
              value={draft.value}
              onChange={(e) => setDraft({ ...draft, value: e.target.value })}
            />
          </label>
          <label className="field">
            <span>Paid by</span>
            <select
              className="input"
              data-testid="gift-voucher-mode"
              value={draft.mode}
              onChange={(e) => setDraft({ ...draft, mode: e.target.value as Tender })}
            >
              {TENDERS.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          {draft.mode !== "cash" && (
            <label className="field">
              <span>Reference (optional)</span>
              <input
                className="input"
                data-testid="gift-voucher-reference"
                value={draft.reference}
                onChange={(e) => setDraft({ ...draft, reference: e.target.value })}
              />
            </label>
          )}
          <p className="muted-cell bill-consent-note">No GST is charged on a gift voucher.</p>
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="gift-voucher-sell"
              disabled={busy || !online || locked}
              onClick={() => void sell()}
            >
              Sell voucher
            </button>
            <button type="button" className="btn" onClick={() => setDoing("")}>
              Close
            </button>
          </div>
        </>
      )}
      {sold && doing === "" && (
        <p className="bill-consent-note" data-testid="gift-voucher-sold">
          Sold {sold.number} (code {sold.code}) for {formatINR(sold.value_paise)}, use by{" "}
          {dayText(sold.valid_until)}.{" "}
          <button
            type="button"
            className="btn"
            data-testid="gift-voucher-reprint"
            onClick={() => onPrint(voucherSlipHtml(sold))}
          >
            Print again
          </button>
        </p>
      )}
      {note && (
        <p className="bill-alert bill-consent-note" data-testid="gift-voucher-note" role="alert">
          {note}
        </p>
      )}
    </section>
  );
}

/** Did head office answer with a refusal (a 4xx)? Then nothing was sold. */
function headOfficeRefused(reason: unknown): boolean {
  const status = (reason as { response?: { status?: number } }).response?.status;
  return typeof status === "number" && status >= 400 && status < 500;
}
