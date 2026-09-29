import { useState } from "react";
import type { ReactNode } from "react";
import { Building2, FileText, HandCoins, Plus as PlusIcon, Wallet } from "lucide-react";

import { Money } from "../../../../lib/format";
import { RupeeInput } from "../RupeeInput";
import { DUE_DAY_CHOICES, creditRefusal, creditRoomPaise } from "./usePlusPreview";
import type { MoreMode, Plus } from "./usePlusPreview";

const MODES: { mode: MoreMode; label: string; icon: ReactNode }[] = [
  { mode: "bank", label: "Bank transfer", icon: <Building2 size={16} aria-hidden /> },
  { mode: "cheque", label: "Cheque", icon: <FileText size={16} aria-hidden /> },
  { mode: "storeCredit", label: "Store credit", icon: <Wallet size={16} aria-hidden /> },
  { mode: "credit", label: "On account", icon: <HandCoins size={16} aria-hidden /> },
];

const PAISE_KEY = {
  bank: "bankPaise",
  cheque: "chequePaise",
  storeCredit: "storeCreditPaise",
  credit: "creditPaise",
} as const;

/** More ways to pay, next to cash, UPI and card. Same row shape as those. */
export function MoreTenders({ plus, payablePaise, locked }: { plus: Plus; payablePaise: number; locked: boolean }) {
  const { state } = plus;
  const [menu, setMenu] = useState(false);
  const closed = MODES.filter((m) => !state.openModes.includes(m.mode));
  const owed = Math.max(
    0,
    payablePaise - state.bankPaise - state.chequePaise - state.storeCreditPaise - state.creditPaise,
  );

  function open(mode: MoreMode) {
    plus.patch({ openModes: [...state.openModes, mode] });
    setMenu(false);
  }
  function close(mode: MoreMode) {
    plus.patch({ openModes: state.openModes.filter((m) => m !== mode), [PAISE_KEY[mode]]: 0 });
  }

  return (
    <section className="plus-card" data-testid="plus-more">
      <header className="plus-head">
        <h3 className="eyebrow">More ways to pay</h3>
        {state.openModes.length > 0 && <span>Still to allocate <Money paise={owed} /></span>}
      </header>

      {state.openModes.map((mode) => {
        const meta = MODES.find((m) => m.mode === mode)!;
        return (
          <div className="plus-tender" key={mode} data-testid={`plus-tender-${mode}`}>
            <div className="bill-tender">
              <label><span className="bill-tender-icon">{meta.icon}</span> {meta.label}</label>
              <button type="button" className="bill-tender-rest" disabled={locked || owed === 0} onClick={() => plus.patch({ [PAISE_KEY[mode]]: state[PAISE_KEY[mode]] + owed })}>
                Rest
              </button>
              <RupeeInput
                placeholder="0"
                testId={`plus-${mode}-amount`}
                label={`${meta.label} amount`}
                paise={state[PAISE_KEY[mode]]}
                locked={locked}
                onChange={(p) => plus.patch({ [PAISE_KEY[mode]]: p ?? 0 })}
              />
            </div>
            <Details mode={mode} plus={plus} locked={locked} />
            <button type="button" className="plus-link plus-remove" onClick={() => close(mode)}>Remove {meta.label.toLowerCase()}</button>
          </div>
        );
      })}

      {closed.length > 0 && (
        <div className="plus-add">
          <button type="button" className="btn plus-btn plus-wide" data-testid="plus-more-open" aria-expanded={menu} disabled={locked} onClick={() => setMenu(!menu)}>
            <PlusIcon size={16} aria-hidden /> More ways to pay
          </button>
          {menu && (
            <ul className="plus-menu" role="menu">
              {closed.map((m) => (
                <li key={m.mode} role="none">
                  <button type="button" role="menuitem" data-testid={`plus-add-${m.mode}`} onClick={() => open(m.mode)}>
                    {m.icon} {m.label}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function Details({ mode, plus, locked }: { mode: MoreMode; plus: Plus; locked: boolean }) {
  const { customer, creditPaise, dueDays, pinOk } = plus.state;
  const [postDated, setPostDated] = useState(false);
  if (mode === "bank") {
    return (
      <div className="plus-fields">
        <input className="input plus-input" placeholder="Bank" aria-label="Bank" disabled={locked} />
        <input className="input plus-input mono" placeholder="Reference no." aria-label="Reference number" disabled={locked} />
        <input className="input plus-input" type="date" aria-label="Transfer date" disabled={locked} />
        <p className="plus-note">Recorded manually. Check the money is in the account.</p>
      </div>
    );
  }
  if (mode === "cheque") {
    return (
      <div className="plus-fields">
        <input className="input plus-input mono" placeholder="Cheque no." aria-label="Cheque number" disabled={locked} />
        <input className="input plus-input" placeholder="Bank" aria-label="Cheque bank" disabled={locked} />
        <input className="input plus-input" type="date" aria-label="Cheque date" disabled={locked} />
        <label className="plus-check">
          <input type="checkbox" checked={postDated} onChange={(e) => setPostDated(e.target.checked)} />
          <span>Post-dated cheque</span>
        </label>
      </div>
    );
  }
  if (mode === "storeCredit") {
    return customer ? (
      <p className="plus-note">Store credit left: <Money paise={customer.storeCreditPaise} /></p>
    ) : (
      <p className="plus-note">Store credit belongs to a customer. Add the customer first.</p>
    );
  }
  const refusal = creditRefusal(customer, creditPaise, pinOk);
  const limit = customer?.creditLimitPaise ?? 0;
  const used = (customer?.creditUsedPaise ?? 0) + creditPaise;
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 100;
  return (
    <div className="plus-credit" data-testid="plus-credit">
      {customer && (
        <>
          <div className="plus-bar" role="img" aria-label={`Credit used ${pct}% of limit`}>
            <span className={used > limit ? "is-over" : ""} style={{ width: `${pct}%` }} />
          </div>
          <p className="plus-note">
            <Money paise={used} /> of <Money paise={limit} /> limit · <Money paise={creditRoomPaise(customer)} /> left before this bill
          </p>
        </>
      )}
      <div className="plus-due" role="group" aria-label="Due in">
        <span>Due in</span>
        {DUE_DAY_CHOICES.map((d) => (
          <button key={d} type="button" className={d === dueDays ? "is-on" : ""} aria-pressed={d === dueDays} onClick={() => plus.patch({ dueDays: d })}>
            {d} days
          </button>
        ))}
      </div>
      {refusal && (
        <p className="plus-refusal" role="alert" data-testid="plus-credit-refusal">{refusal}</p>
      )}
      {refusal && customer && (
        <button type="button" className="btn plus-btn" data-testid="plus-credit-pin" onClick={() => plus.patch({ pinOk: true })}>
          Manager PIN (demo: approve)
        </button>
      )}
      {pinOk && <p className="plus-note">Approved by a manager (demo).</p>}
    </div>
  );
}

/** Take a payment against what a customer owes. Bottom sheet on a phone. */
export function ReceivePaymentSheet({ plus }: { plus: Plus }) {
  const { customer } = plus.state;
  const [paise, setPaise] = useState(0);
  const [done, setDone] = useState(false);
  if (!customer || !plus.state.receiveOpen) return null;
  return (
    <div className="plus-sheet-wrap" role="dialog" aria-modal="true" aria-label="Receive payment" data-testid="plus-receive">
      <div className="plus-sheet">
        <h3>Receive payment from {customer.name}</h3>
        <p className="plus-note">Owes <Money paise={customer.creditUsedPaise} /></p>
        <RupeeInput placeholder="0" testId="plus-receive-amount" label="Amount received" paise={paise} locked={false} onChange={(p) => setPaise(p ?? 0)} />
        <p className="plus-note">Cash, UPI, card or bank transfer. Demo only, nothing is saved.</p>
        {done && <p className="plus-wait">Recorded in the preview.</p>}
        <div className="plus-row">
          <button type="button" className="btn plus-btn" onClick={() => plus.patch({ receiveOpen: false })}>Close</button>
          <button type="button" className="btn btn-primary plus-btn" disabled={paise <= 0} onClick={() => setDone(true)}>Record</button>
        </div>
      </div>
    </div>
  );
}
