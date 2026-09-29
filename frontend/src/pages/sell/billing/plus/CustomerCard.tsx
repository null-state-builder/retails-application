import { useState } from "react";
import { Award, HandCoins, Ruler, UserPlus, UserRound, X } from "lucide-react";

import { Money } from "../../../../lib/format";
import { DEMO_CUSTOMERS, creditRoomPaise, normaliseMobile } from "./usePlusPreview";
import type { Plus, PlusCustomer } from "./usePlusPreview";

/**
 * The customer on the bill, with what matters at the counter: points, what they
 * owe, their saved size. A number that is not known offers "Register".
 * Marketing consent is never ticked by staff (PRD ST-CMP-6): the customer says
 * yes on the display.
 */
export function CustomerCard({ plus, locked }: { plus: Plus; locked: boolean }) {
  const { customer, registering } = plus.state;
  const [typed, setTyped] = useState("");
  const mobile = normaliseMobile(typed);
  const known = DEMO_CUSTOMERS.find((c) => c.mobile === mobile) ?? null;

  if (customer) return <Found customer={customer} plus={plus} locked={locked} />;
  if (registering) return <Register plus={plus} start={mobile} />;

  return (
    <section className="plus-card" data-testid="plus-customer">
      <header className="plus-head">
        <h3 className="eyebrow">Customer</h3>
        <span>Optional. A bill never waits for it.</span>
      </header>
      <div className="plus-row">
        <input
          className="input plus-input mono"
          inputMode="numeric"
          placeholder="Mobile number"
          aria-label="Customer mobile number"
          data-testid="plus-mobile"
          disabled={locked}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
        />
        <button
          type="button"
          className="btn plus-btn"
          data-testid="plus-find"
          disabled={locked || mobile.length < 10}
          onClick={() => (known ? plus.pickCustomer(known) : plus.patch({ registering: true }))}
        >
          {known ? "Add" : "Find"}
        </button>
      </div>
      {mobile.length === 10 && !known && (
        <p className="plus-note">
          New number.{" "}
          <button type="button" className="plus-link" data-testid="plus-register-open" onClick={() => plus.patch({ registering: true })}>
            Register this customer
          </button>
        </p>
      )}
      <p className="plus-demo">
        <span>Demo customers</span>
        {DEMO_CUSTOMERS.map((c) => (
          <button key={c.mobile} type="button" disabled={locked} onClick={() => plus.pickCustomer(c)}>
            {c.name}
          </button>
        ))}
      </p>
      <p className="plus-earn-hint">
        <Award size={14} aria-hidden /> Add a customer to earn and use points.
      </p>
    </section>
  );
}

function Found({ customer, plus, locked }: { customer: PlusCustomer; plus: Plus; locked: boolean }) {
  const over = customer.creditUsedPaise >= customer.creditLimitPaise && customer.creditUsedPaise > 0;
  return (
    <section className="plus-card" data-testid="plus-customer">
      <header className="plus-head">
        <h3 className="eyebrow">Customer</h3>
        <button type="button" className="plus-x" aria-label="Remove customer" disabled={locked} onClick={() => plus.pickCustomer(null)}>
          <X size={16} aria-hidden />
        </button>
      </header>
      <div className="plus-who">
        <span className="plus-avatar" aria-hidden><UserRound size={18} /></span>
        <div>
          <strong data-testid="plus-name">{customer.name}</strong>
          <span className="mono">{customer.mobile}</span>
        </div>
        <span className={`plus-tier is-${customer.tier.toLowerCase()}`}>{customer.tier}</span>
      </div>
      <ul className="plus-chips">
        <li data-testid="plus-points-chip"><Award size={14} aria-hidden /> {customer.points.toLocaleString("en-IN")} points</li>
        {customer.creditUsedPaise > 0 && (
          <li className={over ? "is-warn" : ""} data-testid="plus-due-chip">
            <HandCoins size={14} aria-hidden /> Owes <Money paise={customer.creditUsedPaise} />
          </li>
        )}
        {customer.savedSize && (
          <li><Ruler size={14} aria-hidden /> {customer.savedSize}</li>
        )}
      </ul>
      {customer.creditUsedPaise > 0 && (
        <button type="button" className="plus-link" data-testid="plus-receive-open" disabled={locked} onClick={() => plus.patch({ receiveOpen: true })}>
          Receive payment (<Money paise={creditRoomPaise(customer)} />{" "}credit left)
        </button>
      )}
    </section>
  );
}

function Register({ plus, start }: { plus: Plus; start: string }) {
  const [name, setName] = useState("");
  const [mobile, setMobile] = useState(start);
  const [birthday, setBirthday] = useState("");
  const [sendBill, setSendBill] = useState(false);
  const ready = name.trim().length > 1 && normaliseMobile(mobile).length === 10;
  return (
    <section className="plus-card" data-testid="plus-register">
      <header className="plus-head">
        <h3 className="eyebrow">Register customer</h3>
        <button type="button" className="plus-x" aria-label="Cancel" onClick={() => plus.patch({ registering: false })}>
          <X size={16} aria-hidden />
        </button>
      </header>
      <div className="plus-fields">
        <input className="input plus-input" placeholder="Name" aria-label="Customer name" data-testid="plus-reg-name" value={name} onChange={(e) => setName(e.target.value)} />
        <input className="input plus-input mono" inputMode="numeric" placeholder="Mobile" aria-label="Mobile number" data-testid="plus-reg-mobile" value={mobile} onChange={(e) => setMobile(e.target.value)} />
        <input className="input plus-input" type="date" aria-label="Birthday (optional)" title="Birthday (optional)" value={birthday} onChange={(e) => setBirthday(e.target.value)} />
      </div>
      <label className="plus-check">
        <input type="checkbox" checked={sendBill} onChange={(e) => setSendBill(e.target.checked)} />
        <span>The customer says: send my bill to my phone</span>
      </label>
      <p className="plus-wait">
        <span className="plus-dot" aria-hidden /> Offers: the customer says yes on the display. Staff cannot tick it.
      </p>
      <button
        type="button"
        className="btn btn-primary plus-btn plus-wide"
        data-testid="plus-reg-save"
        disabled={!ready}
        onClick={() =>
          plus.pickCustomer({
            name: name.trim(),
            mobile: normaliseMobile(mobile),
            tier: "Silver",
            points: 0,
            creditLimitPaise: 0,
            creditUsedPaise: 0,
            storeCreditPaise: 0,
            savedSize: null,
          })
        }
      >
        <UserPlus size={16} aria-hidden /> Register and add to bill
      </button>
    </section>
  );
}
