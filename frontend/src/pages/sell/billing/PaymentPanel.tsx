import type { ReactNode } from "react";
import { Banknote, CheckCircle2, CreditCard, QrCode, Smartphone } from "lucide-react";

import { formatINR, Money } from "../../../lib/format";
import type { priceCart } from "../../../till/cart";
import {
  allocateAll,
  balanceStandingOf,
  canFillTenderRest,
  cashChips,
  prefillFor,
  restTenderPatch,
} from "../../../till/tender";
import type { Payment, PaymentField, TenderMode, TenderSplit } from "../../../till/tender";
import { RupeeInput } from "./RupeeInput";

/** One tender row's offer: the balance if the row is standing empty, nothing if
 *  somebody has already filled it. Written once so a fifth row cannot get the
 *  emptiness test subtly wrong - the three modes each spell "empty" their own
 *  way (`null` for cash, nought for the rest). */
function offerIf(empty: boolean, owed: number): number | null {
  return empty ? owed : null;
}

/**
 * The three incoming tender modes and what is still unallocated (#246, store and
 * warehouse operations PRD §9.1).
 *
 * **A bill starts owing all of itself.** No row is filled and no tender is
 * implied: the panel opens on "To allocate ₹1,499", and one tap on **All cash**,
 * **All UPI** or **All card** puts the whole bill on that mode. Cash is no longer
 * the silent balance. The ordinary all-cash sale costs one tap, and in exchange
 * no bill can ever close as a cash sale nobody said was one.
 *
 * There is no split-payment *mode*, and never was: the rows are the split. The
 * arithmetic a cashier used to do in their head sits between them - tapping an
 * empty row offers the remainder (`prefillFor`), typing over it splits, and one
 * colour-coded line says whether money is still to be allocated, still coming in,
 * or going back out.
 *
 * `returnReady` is false while the counter is in return mode but no original
 * bill has been loaded yet - the tender inputs are disabled and a short
 * explanation replaces the normal "To pay" state.
 */
export function PaymentPanel({
  bill,
  payment,
  locked,
  returnReady = true,
  onChange,
  onShowQr,
}: {
  bill: ReturnType<typeof priceCart>;
  payment: Payment;
  locked: boolean;
  /** False when return mode is active but no original bill has been found yet.
   *  The tile is visible but tender inputs are disabled. */
  returnReady?: boolean;
  onChange: (patch: Partial<Payment>) => void;
  /** Open the QR charge card against whatever the UPI row is taking (#248). */
  onShowQr: () => void;
}) {
  const { split } = bill;
  const owed = prefillFor(split);
  const disabled = locked || !returnReady;
  // The cash row is what the customer is handing notes against, so the chips are
  // read off it rather than off what is still unallocated.
  const chips = cashChips(split.cash_paise);
  // Hidden when the bill takes no cash at all - but never while it holds a
  // figure somebody typed, which would put a control out of reach (the standing
  // rule behind grill-decisions amendment 12: never hide a control) and would
  // hide the very box the "cash received, but this bill takes none" refusal is
  // asking a person to clear.
  const showReceived = split.cash_paise > 0 || split.cash_received_paise !== null;

  /** One "All …" tap: the whole bill on one mode, everything else cleared. */
  function allocate(mode: TenderMode) {
    // The bill less any bank offer (ticket 11): the bank pays that part.
    onChange(allocateAll(payment, bill.payable_paise, mode));
  }

  /** A box holding something that is not an amount. Kept on the payment so the
   *  one close-validation can speak for every box at once. */
  function flagInvalid(field: PaymentField, invalid: boolean) {
    const rest = payment.invalid.filter((name) => name !== field);
    onChange({ invalid: invalid ? [...rest, field] : rest });
  }

  return (
    <section
      className="bill-payment-panel"
      data-return-ready={returnReady ? undefined : "false"}
    >
      <div className="bill-payment-heading">
        <p className="eyebrow">To pay</p>
        <p className="bill-due" data-testid="bill-due">
          {returnReady ? (
            <Money paise={bill.payable_paise} />
          ) : (
            <>Nothing yet &middot; ₹0</>
          )}
        </p>
      </div>

      {/* Return mode before original bill is found: brief explanation. */}
      {!returnReady && (
        <p className="bill-payment-return-hint">
          Scan or find the original bill first to enable payment.
        </p>
      )}

      {/* The whole bill on one mode, one tap each - the ordinary sale. */}
      <div className="bill-allocate-all" role="group" aria-label="Allocate the whole bill">
        {(["cash", "upi", "card"] as const).map((mode) => (
          <button
            key={mode}
            type="button"
            className={`btn bill-allocate${payment.mode === mode ? " btn-cta" : ""}`}
            data-testid={`bill-all-${mode}`}
            aria-pressed={payment.mode === mode}
            disabled={disabled || bill.payable_paise <= 0}
            onClick={() => allocate(mode)}
          >
            {mode === "cash" ? "All cash" : mode === "upi" ? "All UPI" : "All card"}
          </button>
        ))}
      </div>

      {/* One connected tile: Cash, UPI, Card separated by internal dividers. */}
      <div className="bill-rail-tile bill-payment-tile">
        <div className="bill-payment-stack">
          <div className="bill-payment-method bill-cash-block">
            <TenderRow
              testId="bill-cash"
              label="Cash"
              icon={<Banknote size={16} />}
              paise={split.cash_paise}
              // Empty in the sense the prefill cares about: nobody has put a
              // figure on this row yet.
              prefillPaise={offerIf(payment.cash_paise === null, owed)}
              locked={disabled}
              invalid={payment.invalid.includes("cash")}
              onInvalid={(bad) => flagInvalid("cash", bad)}
              onChange={(paise) => onChange({ cash_paise: paise, mode: null })}
            />
            <CashChips
              chips={chips}
              locked={disabled}
              onPick={(paise) => onChange({ cash_received_paise: paise })}
            />
            {showReceived && (
              <TenderRow
                testId="bill-cash-received"
                label="Cash received"
                paise={split.cash_received_paise ?? 0}
                // Never prefilled: what the customer physically handed over is the
                // one figure on this panel the till has no business guessing - the
                // chips are how it is offered, one deliberate tap at a time.
                locked={disabled}
                quiet
                // Blank means exact; a typed nought means nothing was received,
                // and the two must not collapse into one another on the way in.
                blankIsExact={payment.cash_received_paise === null}
                invalid={payment.invalid.includes("cash_received")}
                onInvalid={(bad) => flagInvalid("cash_received", bad)}
                onChange={(paise) => onChange({ cash_received_paise: paise })}
              />
            )}
          </div>
          <hr className="bill-tile-divider" />
          <div className="bill-payment-method bill-upi-block">
            <TenderRow
              testId="bill-upi"
              label="UPI"
              icon={<Smartphone size={16} />}
              paise={split.upi_paise}
              prefillPaise={offerIf(split.upi_paise === 0, owed)}
              locked={disabled}
              invalid={payment.invalid.includes("upi")}
              onInvalid={(bad) => flagInvalid("upi", bad)}
              onChange={(paise) => onChange({ upi_paise: paise ?? 0, mode: null })}
              action={
                <RestButton
                  disabled={disabled || !canFillTenderRest(split, "upi")}
                  onClick={() => onChange({ ...restTenderPatch(split, "upi"), mode: null })}
                />
              }
            />
            {(split.upi_paise > 0 || split.upi_confirmed) && (
              <UpiProof
                confirmed={split.upi_confirmed}
                locked={locked || split.upi_confirmed !== null}
                onShowQr={onShowQr}
              />
            )}
          </div>
          <hr className="bill-tile-divider" />
          <div className="bill-payment-method">
            <TenderRow
              testId="bill-card"
              label="Card"
              icon={<CreditCard size={16} />}
              paise={split.card_paise}
              prefillPaise={offerIf(split.card_paise === 0, owed)}
              locked={disabled}
              invalid={payment.invalid.includes("card")}
              onInvalid={(bad) => flagInvalid("card", bad)}
              onChange={(paise) => onChange({ card_paise: paise ?? 0, mode: null })}
              action={
                <RestButton
                  disabled={disabled || !canFillTenderRest(split, "card")}
                  onClick={() => onChange({ ...restTenderPatch(split, "card"), mode: null })}
                />
              }
            />
            {split.card_paise > 0 && (
              <p className="bill-tender-proof" data-testid="bill-card-proof">
                Recorded manually
              </p>
            )}
          </div>
        </div>
        {/* Balance footer inside the tile */}
        <BalanceLine split={split} />
      </div>
    </section>
  );
}

/**
 * The one line that says where the money stands - red while the bill is not yet
 * allocated or short, green when there is change to hand back, and never both at
 * once (#246).
 *
 * It replaced a "Still to pay" row and a "Change" row that were on screen
 * together, each answering with a ₹0 for most of the bill's life. Two figures
 * that are nearly always nought teach a cashier to stop reading them, which is
 * the opposite of what the one number that matters is for.
 *
 * Which of the six it is, and in what words, is `balanceStandingOf`'s - a rule
 * with its own tests, because the green line is an instruction to open the
 * drawer and this component is only allowed to draw it.
 */
function BalanceLine({ split }: { split: TenderSplit }) {
  const { tone, says, paise } = balanceStandingOf(split);
  return (
    <p
      className={`bill-balance-line is-${tone}`}
      data-testid="bill-balance-line"
      data-tone={tone}
    >
      <span>{says}</span>
      <span data-testid="bill-balance">
        <Money paise={paise} />
      </span>
    </p>
  );
}

/**
 * The quick-cash chips (grill Q4): what the customer is most likely to hand
 * over for the cash half of this bill, one tap each.
 *
 * They record `cash_received_paise` and nothing else - the tender rows are
 * untouched, so a chip can never change what the bill takes, only what the
 * change line answers. Exact is first because it is the common one and because
 * it closes that line to nought.
 */
function CashChips({
  chips,
  locked,
  onPick,
}: {
  chips: number[];
  locked: boolean;
  onPick: (paise: number) => void;
}) {
  if (!chips.length) return null;
  return (
    <div className="bill-chips" data-testid="bill-cash-chips">
      {chips.map((paise, index) => (
        <button
          key={paise}
          type="button"
          className="btn bill-chip"
          data-testid={`bill-cash-chip-${index}`}
          // `formatINR`, never `paise / 100`: a screen reader should hear the
          // same Indian-grouped figure the chip shows, and money is never
          // divided by a hundred on the way out of a paise integer.
          aria-label={`Cash received ${formatINR(paise)}${index === 0 ? " - the exact amount" : ""}`}
          disabled={locked}
          onClick={() => onPick(paise)}
        >
          {index === 0 && <span className="bill-chip-tag">Exact</span>}
          <Money paise={paise} />
        </button>
      ))}
    </div>
  );
}

/**
 * How the UPI row is being proved (#248, grill Q5).
 *
 * Either the bank said so through the charge card - and then it says which
 * reference, because a stamp with nothing behind it is not a proof - or it is
 * the cashier's own word, which is what "show QR" is offered instead of. Both
 * are allowed, forever: billing never stops on the internet, and the control on
 * a vouched-for payment is that the day close shows the two totals apart. The
 * row says which of the two it is in words, on the screen and on the bill.
 *
 * A row the bank has already confirmed cannot be charged again from here, and
 * that is a money guard rather than tidiness: with real hardware behind the
 * adapter, a second "Show QR" against a payment that has already gone through
 * is a second collection from the same customer. The way back is to change what
 * the row is taking - editing the box drops the stamp on its own
 * (`confirmedUpiOf`) and the button comes live again with it.
 */
function UpiProof({
  confirmed,
  locked,
  onShowQr,
}: {
  confirmed: TenderSplit["upi_confirmed"];
  locked: boolean;
  onShowQr: () => void;
}) {
  return (
    <div className="bill-upi-proof">
      <button
        type="button"
        className="btn bill-upi-open"
        data-testid="bill-upi-show-qr"
        disabled={locked}
        onClick={onShowQr}
      >
        <QrCode size={14} />
        Show QR
      </button>
      {confirmed ? (
        <span className="bill-upi-confirmed" data-testid="bill-upi-confirmed">
          <CheckCircle2 size={13} />
          Bank confirmed · {confirmed}
        </span>
      ) : (
        <span className="bill-tender-proof" data-testid="bill-upi-manual">
          Recorded manually
        </span>
      )}
    </div>
  );
}

/** One mode's amount. `quiet` is the cash-received row, which is a note to self
 *  about the drawer rather than a tender, and reads as one. */
function TenderRow({
  testId,
  label,
  icon,
  action,
  paise,
  quiet,
  blankIsExact,
  invalid,
  prefillPaise = null,
  locked,
  onInvalid,
  onChange,
}: {
  testId: string;
  label: string;
  icon?: ReactNode;
  action?: ReactNode;
  paise: number;
  quiet?: boolean;
  /** The cash-received row standing blank, which means "exactly the cash
   *  tender" rather than nought. Said out loud, because the two are different
   *  bills and only one of them can close. */
  blankIsExact?: boolean;
  invalid?: boolean;
  prefillPaise?: number | null;
  locked: boolean;
  onInvalid?: (invalid: boolean) => void;
  onChange: (paise: number | null) => void;
}) {
  return (
    <div className={quiet ? "bill-tender is-quiet" : "bill-tender"}>
      <label htmlFor={testId}>
        {icon && <span className="bill-tender-icon">{icon}</span>}
        {label}
        {blankIsExact && (
          <span className="muted-cell" data-testid={`${testId}-exact`}>
            exact
          </span>
        )}
      </label>
      <span className="bill-tender-action">{action}</span>
      <RupeeInput
        testId={testId}
        label={label}
        paise={paise}
        locked={locked}
        placeholder={blankIsExact ? "exact" : "0"}
        prefillPaise={prefillPaise}
        onInvalid={onInvalid}
        onChange={onChange}
      />
      {invalid && (
        <span className="bill-tender-invalid" data-testid={`${testId}-invalid`}>
          Not an amount
        </span>
      )}
    </div>
  );
}

function RestButton({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  return (
    <button type="button" className="btn bill-tender-rest" disabled={disabled} onClick={onClick}>
      Rest
    </button>
  );
}
