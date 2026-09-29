import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, FileText, UserPlus } from "lucide-react";

import { usePositionedPopover } from "../../../shell/usePositionedPopover";
import type { TillDb } from "../../../till/db";
import { describeGstin, TAX_KIND_WORDS, taxKindFor } from "../../../till/gstin";
import { mobilePrefixes, searchCustomers } from "../../../till/lookup";
import type { TillCustomer, TillKnownCustomer } from "../../../till/types";

/** Stand-in width for the typeahead, before it has ever mounted
 *  (`usePositionedPopover`'s `fallbackSize`). Kept in step with
 *  `.bill-customer-float`'s width in Billing.css. */
const FLOAT_WIDTH = 300;

/** What the phone book last answered, and the number it answered *about*.
 *
 *  Paired rather than kept as a bare list because the read is asynchronous and
 *  the cashier keeps typing: rows found for `98352` must never be offered under
 *  `983521`, and - the worse half - an empty answer for `983` must never make
 *  `9835` say "not billed here before" before its own read has landed. */
interface Answer {
  typed: string;
  rows: TillKnownCustomer[];
}

/**
 * Who the bill is for - and, when they give a GSTIN, what kind of bill it is.
 *
 * Three digits into the mobile field the counter's own phone book answers
 * underneath it (#249, grill Q6/G-4): everybody KDPS has billed, synced down
 * with the rest of the till's world, searched offline because there is no
 * lookup endpoint by design. Picking a row is the whole interaction - name,
 * number and registration land on the bill in one tap, and a regular stops
 * being re-typed at every visit.
 *
 * A number nobody has billed before is not an error and is not a form. It
 * offers a name and nothing else, because a name is all this counter has ever
 * collected and asking for more at the till is how a queue starts. The bill
 * still closes with none of it filled in.
 *
 * A GSTIN turns a retail sale into a full tax invoice (#187, grill Q8): the
 * split printed on the customer's copy is derived here, offline, from the
 * buyer's state against the shop's, because it prints minutes before head office
 * hears about the bill and cannot wait for anybody. It sits behind a disclosure
 * because most bills are not business bills - but a bill that *has* one always
 * shows it, whether it came from the phone book, a restored draft or a resumed
 * hold: a field carrying a value the cashier cannot see is the one thing this
 * card is not allowed to do (Rule 5).
 *
 * The check on what they typed is *soft*, and visibly so. The counter says the
 * registration looks wrong and the bill closes anyway - the customer is standing
 * there holding the garment, and a mistyped character is a tax invoice head
 * office corrects, not a sale to decline. Saying it here is still worth doing,
 * because here is the one moment the customer can read their card out again.
 */
export function CustomerStrip({
  value,
  storeStateCode,
  db,
  locked,
  onChange,
}: {
  value: TillCustomer;
  /** Null when the counter has not learned which state it is in - see below. */
  storeStateCode: string | null;
  /** The counter's own database, holding the synced phone book (#245). Null
   *  before the till has opened one, which only reads as a typeahead that has
   *  nothing to say. */
  db: TillDb | null;
  locked: boolean;
  onChange: (v: TillCustomer) => void;
}) {
  const [answer, setAnswer] = useState<Answer | null>(null);
  /** The cashier is typing a number *now*. Without it a restored draft would
   *  pop its own customer's list open on mount, over a bill nobody asked about.
   *  Put down by picking, by billing a new customer, and by the hook's own
   *  outside-click and Escape. */
  const [asking, setAsking] = useState(false);
  /** "Business bill?" has been opened by hand. A GSTIN already on the bill
   *  opens it too - see `showGstin` - so this only has to carry the case where
   *  there is nothing to show yet. */
  const [askedForGstin, setAskedForGstin] = useState(false);
  /** The phone-book row this card was last filled from. Held because a name and
   *  a registration belong to a *number*, and the cashier can change the number
   *  after picking - five near-identical mobiles are five chances to tap the
   *  wrong one, and "that was my old number" is a sentence people say at
   *  counters. Left alone, the previous customer's GSTIN would stay on the card
   *  under somebody else's number, and a GSTIN is not decoration: it decides
   *  whether the paper says CGST + SGST or IGST, and names the registered buyer
   *  claiming credit against it. */
  const [filled, setFilled] = useState<TillKnownCustomer | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  // A counter that does not know its own state cannot say which tax a business
  // bill carries, and there is no safe guess: one wrong answer prints IGST on a
  // local buyer's copy while the books post CGST + SGST, and the other does it
  // the other way round. So the field closes and says why, rather than taking a
  // registration it would then charge the wrong tax on.
  const canBillB2b = storeStateCode !== null;
  const kind = canBillB2b ? taxKindFor(value.gstin, storeStateCode) : "none";
  const malformed = canBillB2b ? describeGstin(value.gstin) : "";
  const showGstin = askedForGstin || value.gstin.trim() !== "";

  const searchable = mobilePrefixes(value.mobile).length > 0;
  useEffect(() => {
    if (!db || !searchable) {
      setAnswer(null);
      return;
    }
    let live = true;
    void searchCustomers(db, value.mobile).then((rows) => {
      if (live) setAnswer({ typed: value.mobile, rows });
    });
    return () => {
      live = false;
    };
  }, [db, value.mobile, searchable]);

  const rows = answer && answer.typed === value.mobile ? answer.rows : null;
  const float = usePositionedPopover<HTMLInputElement>(
    asking && !locked && rows ? `customer:${value.mobile}:${rows.length}` : null,
    () => setAsking(false),
    FLOAT_WIDTH,
    "below",
  );

  /** Still the row this card was filled from - not one the cashier has since
   *  typed a different number over. */
  const from = filled && filled.mobile === value.mobile ? filled : null;

  /** One tap fills the card whole, rather than merging with what is in it. A
   *  GSTIN left over from whoever the cashier was billing a moment ago is
   *  exactly the cross-customer bleed `NO_CUSTOMER` exists to prevent, and
   *  "this bill is for that person" is an answer about all three fields. */
  function pick(row: TillKnownCustomer) {
    onChange({ name: row.name, mobile: row.mobile, gstin: row.gstin });
    setFilled(row);
    setAsking(false);
  }

  /** Retyping the number takes the phone book's answer back off with it - but
   *  only the parts still standing as the book gave them. A name the cashier
   *  typed over is theirs and survives; the name and registration that came
   *  with the old number do not, because they belong to that number. */
  function editMobile(mobile: string) {
    setAsking(true);
    if (!from || mobile === from.mobile) {
      onChange({ ...value, mobile });
      return;
    }
    setFilled(null);
    onChange({
      mobile,
      name: value.name === from.name ? "" : value.name,
      gstin: value.gstin === from.gstin ? "" : value.gstin,
    });
  }

  return (
    <section className="bill-customer-card">
      <div className="bill-customer-heading">
        <p className="eyebrow">Customer</p>
        <span>Optional</span>
      </div>
      <div className="bill-rail-tile bill-customer-tile">
        {/* Side by side rather than stacked: the rail has to hold this card and
            the payment card at 1366×768 without a scrollbar of its own
            (grill-decisions amendment 12's exit condition), and a mobile number
            and a first name are both short enough to read in half of 340px. */}
        <div className="bill-customer-fields">
          <div className="field">
            <label htmlFor="bill-mobile">Mobile</label>
            <input
              id="bill-mobile"
              className="input"
              data-testid="bill-mobile"
              ref={float.triggerRef}
              autoComplete="off"
              inputMode="tel"
              placeholder="Mobile"
              disabled={locked}
              value={value.mobile}
              onChange={(e) => editMobile(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="bill-customer-name">Name</label>
            <input
              id="bill-customer-name"
              className="input"
              data-testid="bill-customer-name"
              ref={nameRef}
              autoComplete="off"
              placeholder="Name"
              disabled={locked}
              value={value.name}
              onChange={(e) => onChange({ ...value, name: e.target.value })}
            />
          </div>
        </div>
        {from && (
          <p className="bill-known-customer" data-testid="bill-known-customer">
            <span /> Returning customer · details from the KDPS phone book
          </p>
        )}

        {!showGstin && (
          <button
            type="button"
            className="btn bill-business-toggle"
            data-testid="bill-business-open"
            aria-expanded={false}
            disabled={locked}
            onClick={() => setAskedForGstin(true)}
          >
            Need a GST business bill?
          </button>
        )}
        {showGstin && (
          <>
            <div className="field">
              <label htmlFor="bill-gstin">GSTIN (for a business bill)</label>
              <input
                id="bill-gstin"
                className="input mono"
                data-testid="bill-gstin"
                autoComplete="off"
                maxLength={15}
                spellCheck={false}
                disabled={locked || !canBillB2b}
                value={value.gstin}
                // Upper-cased as it is typed, so what the cashier reads back is what
                // prints and what the server stores - a GSTIN differing from itself by
                // case would flag every second business bill.
                onChange={(e) => onChange({ ...value, gstin: e.target.value.toUpperCase() })}
              />
            </div>
            {/* Closing the disclosure on a bill that carries a registration
                *clears* it rather than hiding it. "Not a business bill" is a
                statement about the bill, and a GSTIN still riding along
                invisibly would print a tax invoice the cashier thinks they
                cancelled. */}
            <button
              type="button"
              className="btn bill-business-toggle"
              data-testid="bill-business-close"
              aria-expanded
              disabled={locked}
              onClick={() => {
                setAskedForGstin(false);
                if (value.gstin) onChange({ ...value, gstin: "" });
              }}
            >
              Not a business bill
            </button>
            {!canBillB2b && (
              <p className="bill-alert" data-testid="bill-gstin-unavailable">
                <AlertTriangle size={15} /> This counter has not synced its own registration yet, so
                it cannot say whether a buyer is in this state. Sync from Till &amp; Sync to bill a
                business; a retail bill is unaffected.
              </p>
            )}
            {kind !== "none" && (
              <p className="ok-note" data-testid="bill-tax-kind">
                <FileText size={15} /> Tax invoice · {TAX_KIND_WORDS[kind]}
                {kind === "igst" ? " (buyer is out of state)" : ""}
              </p>
            )}
            {malformed && (
              <p className="bill-alert" data-testid="bill-gstin-warning">
                <AlertTriangle size={15} /> {malformed} The bill will still close - check the card.
              </p>
            )}
          </>
        )}
        {showGstin && (
          <p className="muted-cell bill-customer-note">The bill works without any of these.</p>
        )}
      </div>

      {/* Portaled and placed, like every other prompt on this screen (Q11:
          nothing pushes the layout) - and here the portal is doing a second
          job: the rail is its own scroll box, which per the CSS spec clips a
          descendant popover invisibly. */}
      {rows &&
        float.at &&
        createPortal(
          <div
            ref={float.popoverRef}
            className="card section-card bill-customer-float"
            data-testid="bill-customer-float"
            // Applied whole, never field by field - see `PopoverPlacement`.
            // The mobile field sits low on the rail, so this list is normally
            // anchored by the field's *top* edge and grows upward.
            style={{ ...float.at }}
          >
            {rows.length > 0 ? (
              <>
                {/* Not "billed *here* before": the phone book is all-KDPS by
                    ruling (grill Q6 - a Deoghar regular must be recognised in
                    Ranchi), so most of what a counter sees under a number was
                    billed at some other shop, and a label saying otherwise
                    tells the cashier something untrue about their own store. */}
                <p className="eyebrow">Billed before</p>
                <div className="bill-customer-rows">
                  {rows.map((row) => (
                    <button
                      key={row.mobile}
                      type="button"
                      className="btn bill-customer-row"
                      data-testid={`bill-customer-${row.mobile}`}
                      onClick={() => pick(row)}
                    >
                      <span>{row.name || "No name on file"}</span>
                      <span className="muted-cell mono">{row.mobile}</span>
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <>
                <p className="muted-cell bill-customer-none">
                  Nobody has been billed on this number before.
                </p>
                <button
                  type="button"
                  className="btn"
                  data-testid="bill-customer-new"
                  onClick={() => {
                    setAsking(false);
                    nameRef.current?.focus();
                  }}
                >
                  <UserPlus size={15} />
                  Bill to a new customer
                </button>
              </>
            )}
          </div>,
          document.body,
        )}
    </section>
  );
}
