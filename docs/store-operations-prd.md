# RetailsOps — Store Operations Product Requirements Document

## 1. Document status

| Field | Value |
| --- | --- |
| Edition | Standalone edition, 29 September 2026, preserving the store operations enhancement decisions recorded on 27 September 2026 |
| Product owner | Anand Kumar |
| Scope | Everything a store runs on in a day: tax and customer-data compliance, billing, customers, customer orders, receiving, stock, transfers, booking, money, brand settlement, offers, reports, people, and setup and control. |
| Decision status | Anand's recorded decisions and the section 22.3 phase placements were confirmed for SO-01 on 29 September 2026. Tax baselines remain unconfirmed until CA sign-off. Proposed values and policy gates retain their pending status; a phase assignment is not activation approval. |
| Authority | This document specifies store-specific detail within RetailsOps' broader product requirements. The approved scoped decisions are recorded in sections 21–22 and their reconciled application in section 34. The main PRD remains the complete-product authority. |
| Gates | A decision Anand records closes that gate for this document's scope. It does not close unrelated policy or compliance dependencies. |
| Reference retailer | KDPS Lifestyle Pvt Ltd, operating in Bihar and Jharkhand, is the reference deployment. General business rules apply to the configured retailer. |
| Technology direction | Django/Python, React/TypeScript PWA and PostgreSQL. No change of stack is selected. |
| Legal and cost references | Legal citations, tax baselines and historical prices are reproduced as product requirements from the source decisions, not newly verified legal advice or current quotations. CA approval and re-quotation requirements remain in force. |

## 2. How to read this document

- Each requirement has a stable ID `ST-<area>-<n>`.
- **Phase** is the delivery phase from section 25. The assignments confirmed for SO-01 are listed in section 22.3; phase placement is not activation approval.
- **Gate** is the decision needed before a feature goes live in a real store. “—” means no additional gate is named in that row; shared controls and applicable tax sign-offs still apply.
- **(proposed)** marks a number or rule not yet confirmed. Section 23 preserves the numerical decisions.
- **(baseline, CA to confirm)** marks a tax choice used for implementation and test cases pending approval for live use. Section 32 lists the sign-offs.
- Money is kept in paise. Nothing is rounded except where a rule says so.
- This document states required behaviour. The [SO-01 requirement register](planning/store-operations-requirement-register.md) holds implementation and verification assessments; section 34 identifies specific known conflicts without treating source presence as proof of completion.
- Section 33 explains policy gate IDs and domain terms without requiring another document.

## 3. The store day

This is the whole picture. Every section below is one or more steps of it.

| When | Step | What happens | Sections |
| --- | --- | --- | --- |
| Morning | Open | Staff check in. The float is counted. The till renews its authority and syncs. Today's tasks and alerts show on the home screen. | 9, 14, 18, 19 |
| Day | Receive | Deliveries are counted, matched to the booking and invoice, labelled and accepted. | 10 |
| Day | Sell | Bills are made, offers apply and payment is taken. The e-bill is sent. Returns and exchanges happen. Customer reservations, special orders and alterations are taken. | 6, 7, 8, 9, 16 |
| Day | Move | Transfers come in and go out with their documents. Size-balancing and replenishment suggestions wait for approval. | 11, 12 |
| Any day | Check | A scheduled count is due. Broken-size, ageing and SOR-ageing alerts are worked. | 11, 15 |
| Night | Close | The X- and Z-report run. Cash is counted by note. Cash is deposited or handed over. Petty cash is squared. The day summary is ready. | 14 |
| Head office | Review | Card and UPI settlements are matched to the bank. The IRN queue is cleared. GST work, the gift-stock credit report, brand reports and claims, and staff incentives are done. | 6, 14, 15, 17, 18 |

## 4. Who does what in a store

The six initial roles are Owner, Store Person, Warehouse, Brand Manager, Accounts and Admin.

| Work | Who does it |
| --- | --- |
| Billing, returns, exchanges, customer orders, counts, receiving at the store | Store Person |
| Counter overrides (price, discount above the cap, cash variance sign-off) | A store manager, using their **own personal PIN**. The override records which manager approved it. |
| Approvals named in this PRD (bookings over open-to-buy, petty cash over the limit, debit notes, offers) | Owner, as a different person from the one who asked |
| Settlements, payables, debit notes, GST and IRN work, gift-stock credit reversal, flagged brand claims | Accounts |
| Offers, brand terms, brand reports and claims | Brand Manager, with Owner approval for offers and other governed approvals |
| Setup, schedules, checklists, report layouts, tax settings | Admin |

**Store Manager role (Anand, 27 September 2026, Q7).**
- Every manager gets a personal PIN now.
- A Store Manager role is added inside the same permission system before the multi-store rollout, after OQ-28 defines its authority and separation rules (section 30).
- Each override must retain the identity of its approving manager. PINs must be stored as hashes.

## 5. Screens and UX

### 5.1 Store menu

| Menu | What is in it |
| --- | --- |
| Today | Tasks due, alerts, the day's numbers |
| Sell | Billing, Bills, Till & Cash |
| Customers | Customer list, customer page, customer rights, campaigns, back-in-stock requests |
| Customer Orders | Reservations, special orders, alterations, home delivery |
| Receive Goods | Count deliveries, match evidence, label and accept goods |
| Transfers | Requests, approvals, dispatch, receipt, suggestions and transfer documents |
| Stock | Stock, Counts, Damage, Return to Brand, Alerts |
| Booking | Bookings, open-to-buy, size curves |
| Money | Cash, Bank, Settlements, Petty Cash, Payables, partner billing |
| Brands | Terms, Brand Reports, Claims, Margin Share |
| Offers & Price | Offers, approvals, price book, markdowns, return on discount and simulation |
| Reports | Sales, Stock, Brands, Staff, GST, Exceptions |
| People | Attendance, Roster, Leave, Incentives, Registers, Payroll Export |
| Setup | Configuration, Audit Log, Task Checklists, Feature Switches and Tax Settings |

One permission system applies: the main PRD's six initial roles and section levels, each user's site and brand scope, and the applicable workflow step rules. No separate goods grant or permission bridge authorises store work. The full action and field matrix remains OQ-28. Store Person does not see Brands, cost, margin or payables.

### 5.2 UX rules for store screens

These rules apply to all store operations screens.

1. **The counter comes first.** Billing opens in one tap from anywhere. Every billing step can be done with the scanner and keyboard alone.
2. **Floor work runs on a phone.** Counts, stock look-up, attendance, reservations, alterations and tasks each have a real layout at 375 px wide, not a squeezed desktop page.
3. **Back-office work is desktop-first.** This covers money, brands, reports and setup.
4. **One job per screen.** The screen shows what to do next. Status is a chip plus words, never colour alone.
5. **Internet status is plain.**
   - The till works offline for permitted B2C billing, and ST-HR-2 permits phone attendance capture for later upload. Other store actions need the internet unless section 28 explicitly permits them. B2B bills, credit notes, vouchers and reservations remain online only.
   - Every screen or action that needs the internet says so and refuses cleanly.
   - Nothing the person typed is lost when the connection drops.
6. **Manual mode looks deliberate.** Where a provider is not connected yet (section 25), the manual or file-upload step is a proper screen, not a warning.
7. **Every new screen has automated end-to-end acceptance coverage** of its main journey, refusals and connection-loss behaviour.

## 6. Compliance: tax, invoices and customer data

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-CMP-1 | GST on each item's price after discount, with the discount split the same way on the till and the server | P1 | CA sign-off (section 32) |
| ST-CMP-2 | Separate tax for the returned item and the new item in an exchange | P1 | CA sign-off (section 32) |
| ST-CMP-3 | Tax rate from the HSN code plus the price, for every item including accessories | P1 | CA sign-off (section 32) |
| ST-CMP-4 | IRN for B2B bills and credit notes, a 30-day countdown and a daily check; B2B bills online only | P2, then P3 | CA sign-off (section 32) |
| ST-CMP-5 | New series every 1 April, a bill-number block for each offline till, and separate series for other documents | P1 | — |
| ST-CMP-6 | Customer phone optional, with separate consent for the e-bill and for marketing | P1 | — |
| ST-CMP-7 | Gift stock tagged, with a monthly report of input tax credit to reverse | P1 | CA sign-off (section 32) |

### Tax baselines

**The principles (Anand, 27 September 2026):**
1. Every tax and document choice in this section is a **versioned configuration setting**.
2. Every bill records the rule version it was made under. A change applies only to new bills.
3. An issued document is never edited. It is corrected only with a credit note or a debit note.
4. Where the law is unclear, the baseline is the **conservative choice**: the one that charges more tax. Relaxing a rule later is safe. Tightening it later can mean back tax plus interest.
5. The invoice number format can change only on 1 April.

**The baselines.** Each one is (baseline, CA to confirm). None is confirmed.

| Item | Baseline | Setting |
| --- | --- | --- |
| ₹2,500 test | Price ÷ 1.05 ≤ ₹2,500 means 5%. Otherwise 18%. The line is an MRP of ₹2,625. | Rate rule per HSN |
| Buy 2 get 1 | The price is spread across all pieces in proportion to MRP | Allocation method |
| Bank instant discount | Does not reduce the taxable value. It is recorded as a "bank offer" payment. | Per offer: "reduces value yes/no" |
| Exchange | A credit note and a new tax invoice, printed together on one slip | — |
| Returns | Allowed only within the same GSTIN. A return across GSTINs is refused with a clear message. | Cross-GSTIN returns on/off |
| Alterations | A paid alteration is its own line at 5% (SAC 9988). A free alteration has no charge and no tax. | — |
| Rounding | Tax is rounded to the paisa on each line. The invoice total is rounded to the nearest rupee, with a round-off line. | — |
| Accessories HSN | Taken from the brand's invoice or PT. The rate comes from the CBIC rate schedule. An item with no HSN is stopped at PT approval. | — |
| Unused offline number blocks | Reported as cancelled in GSTR-1 Table 13 at month end | — |

**Default facts, used until Anand confirms them (section 32):**

| Fact | Default |
| --- | --- |
| Aggregate turnover (AATO) | ₹10 crore or more, so e-invoicing and the 30-day rule are on |
| GSTINs | One per state. Stores in the same state share it. |
| Brand model | None. A brand with no recorded model shows as **unknown** (Anand, 27 September 2026, D9). |

### ST-CMP-1: GST after discount

Acceptance requires verification that the threshold and rates are configuration, as specified by D3.

**Prices include GST (Anand, 27 September 2026; baseline, CA to confirm).**
- The rate is chosen from the value before tax. For a piece whose price after discount is P, the 5% rate applies when P ÷ 1.05 ≤ ₹2,500.00. Otherwise 18% applies.
- The line is ₹2,625.00, which is ₹2,500.00 before tax at 5%.
- The 5% / 18% split and the ₹2,500 line are configuration for the item's HSN, never code.

**How the discount is spread:**
- A bill-level discount is spread across lines by value, to the paisa, with the spare paisa placed by largest remainder.
- The till and the server must reach the same result. A difference raises a bill flag and does not itself block an otherwise valid bill; a specific legal or approved policy restriction still governs its affected action.

**Buy 2 get 1 (Anand, 27 September 2026; baseline, CA to confirm):**
- Buy 2 get 1 is not a free supply. It is several goods sold for one price (Circular 92/11/2019).
- The price is spread across all the pieces in proportion to their MRP.
- Each piece takes its rate from its own share. There is no highest-rate rule.
- Input tax credit is not reversed.
- A gift that is a different item, given with no payment, is not part of this spread. It is handled by ST-CMP-7.

**Golden cases requiring CA sign-off:**
- MRP ₹2,625.00 → 5%. MRP ₹2,624.99 → 5%. MRP ₹2,625.01 → 18%.
- A bill discount that moves a piece across the line.
- A buy 2 get 1 bill spread by MRP, where one piece's share falls on the other side of the line from its MRP.
- A bill with a bank instant discount, where the taxable value does not change.
- The round-off line.

### ST-CMP-2: Exchanges and returns

- The returned piece's tax is reversed at the rate and value on its original bill.
- The new piece is taxed at today's rule and today's value.
- The exchange produces a credit note and a new tax invoice, printed together on one slip when issue is permitted (baseline, CA to confirm). Where B2B IRN is required, the applicable document stays pending and nothing prints or leaves before IRN capture (ST-CMP-4). The customer pays, or is credited, the difference.
- Returns and exchanges are allowed only within the GSTIN that issued the original bill (baseline, CA to confirm). A return at a store under another GSTIN is refused with a clear message saying where it can be returned.

**Returns after the credit-note deadline (Anand, 27 September 2026):**
- The deadline for a credit note that reduces tax is **30 November after the end of the financial year, or the date the annual return is filed, whichever is earlier** (CGST Act s.34(2)).
- A return after that date gets its refund or exchange value with **no tax reduction**, and it is flagged.

### ST-CMP-3: Rate from HSN and price

- Every sellable item carries an HSN, taken from its official PT. An item with no HSN is stopped at PT approval, never at the counter.
- Accessories (belts, bags, wallets, perfumes and the like) take their HSN from the brand's invoice or PT, and their rate from the CBIC rate schedule, even when the rate does not depend on price (baseline, CA to confirm).
- The till uses the rule version it holds and records that version on the bill.

### ST-CMP-4: IRN

- Applies only to entities where e-invoicing is switched on. That setting is configuration per legal entity. By default it is on (AATO default above).
- Covers B2B bills (buyer GSTIN present) and credit notes issued against them.

**An invoice without an IRN is not valid where e-invoicing applies (CGST Rules 48(5)).**
- B2B bills are therefore **online only**. An offline till cannot issue a B2B tax invoice. It shows a clear message instead: what cannot be done, why, and what to do (sell it as B2C, or wait for the connection).
- The same applies to a credit note against a B2B bill.
- Nothing prints "IRN to follow".
- This departs from acceptance item 6 (D8).

**P2 (manual):**
- Accounts types in the IRN and acknowledgement number from the portal.
- The queue shows the days left out of 30 from the document date.
- A check every morning lists documents without an IRN, sorted by days left, and alerts Accounts at **23, 10 and 3 days left** (Anand, 27 September 2026).
- **How the counter makes a B2B bill in P2 (Anand, 27 September 2026, Q11; baseline, CA to confirm):**
  - The B2B bill is saved as **pending**. No tax invoice prints yet.
  - Accounts generates the IRN on the portal and types it in. Only then does the tax invoice print.
  - The goods never leave without the tax invoice. The customer waits or collects later.

**P3 (connected), Anand, 27 September 2026:**
- The server raises the IRN as the B2B bill is made, and the bill prints with it.

**B2C bills:**
- The dynamic QR on B2C invoices is **not required**. It applies only above ₹500 crore turnover.

### ST-CMP-5: Numbering

- The series starts again on 1 April. Each till keeps its own display block, for example `DEO-T1-74`.
- Every tax document number must be unique in its financial year, at most 16 characters long, and use only letters, digits, `-` and `/` (CGST Rules 46, 50, 53 and 55).
- The format can change only on 1 April (Tax baselines, principle 5).

**Series (Anand, Q8, Q10 and 27 September 2026).** `XXX` is a fixed 3-letter prefix. It is separate from the store code.
- Every store has its own, so each Deoghar store gets a different one.
- Every other site that sends a delivery challan, such as the warehouse, has its own too.
- Head office has one per GSTIN, used for debit notes.

| Document | Format | Room |
| --- | --- | --- |
| Tax invoice | `XXX/26-27/n` | Up to 999,999 per store per year |
| Credit note | `XXX/CN/2627/n` | Up to 9,999 per store per year |
| Receipt voucher (advances, ST-ORD-1) | `XXX/RV/2627/n` | Up to 9,999 per store per year |
| Gift voucher | `XXX/GV/2627/n` | Up to 9,999 per store per year |
| Delivery challan (ST-TRF-2) | `XXX/DC/2627/n`, with the sending site's prefix | Up to 9,999 per site per year |
| Debit note (ST-REC-3) | `XXX/DN/2627/n`, with head office's prefix for that GSTIN | Up to 9,999 per GSTIN per year |

- Unused numbers in an offline till's block are reported as cancelled in GSTR-1 Table 13 at month end (baseline, CA to confirm).

### ST-CMP-6: Customer consent

- The phone number is optional. A bill is never blocked for lack of it.
- The counter asks two separate questions, each off until the customer says yes: **send my bill** and **send me offers**.

**The customer confirms marketing consent themselves (Anand, 27 September 2026):**
- They confirm on the customer display (ST-POS-6), or by an OTP once SMS is connected in P3.
- Staff cannot tick it for them.

**Under 18 (Anand, 27 September 2026):**
- Before marketing consent, the customer is asked whether they are under 18.
- Customers identified as under 18 are excluded from marketing throughout the completed store milestone. A future parental-consent capability needs a separate campaign-policy decision before it can change this rule.

**Recording and withdrawal:**
- Each answer records the time, till, staff member and wording version.
- The customer can withdraw either answer at any time: at the counter, on the customer rights screen (ST-CUS-1), or by replying STOP. The withdrawal takes effect at once.
- This follows the Digital Personal Data Protection Act, 2023.

### ST-CMP-7: Gift stock and input tax credit

- Stock given away as a free gift (a different item, with no payment) is tagged as a gift when it leaves stock. An example is a gift-with-purchase from the offers engine.
- A monthly report lists the input tax credit to reverse on those pieces (CGST Act s.17(5)(h)). Accounts uses it.
- Treating a gift-with-purchase as a gift, rather than as part of the sale price, is (baseline, CA to confirm).

## 7. POS and billing

Required billing foundations:
- Scan billing, held bills, returns and exchanges.
- Payment split and bills.
- Manager PIN, offline selling, till authority and sync.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-POS-1 | Dynamic UPI QR and card machine linked to the till, with automatic payment matching | P2, then P3 | Provider choice |
| ST-POS-2 | Salesperson tagged on each item | P1 | — |
| ST-POS-3 | E-bill on WhatsApp or SMS | P2, then P3 | Provider choice |
| ST-POS-4 | Gift vouchers | P1 | — |
| ST-POS-5 | Credit notes accepted as payment, online only | P1 | — |
| ST-POS-6 | Customer-facing display | P1 | — |

### ST-POS-1: UPI and card

- **P2 (manual).** Card and UPI are recorded by hand, visibly marked **manually recorded**.
- **P3 (connected).**
  - The till sends the exact amount due to the provider. A UPI QR for that amount appears on the till and on the customer display. The card machine receives the same amount.
  - The provider's answer marks the tender **confirmed**, with its reference and the amount actually taken.
  - With no answer, or a different amount, the tender falls back to manually recorded. The two are never merged.
- An offline till always records card and UPI by hand.
- A bank instant discount is recorded as a "bank offer" payment, not a price cut (Tax baselines).
- Payment integration supports a simulated provider for acceptance testing.

### ST-POS-2: Salesperson

- Every sale line records a salesperson.
- The list comes from the staff list (ST-HR-1): people active at this store today.
- The bill carries a default salesperson, and it can be changed per line.

**Split sale (Anand, 27 September 2026):**
- A line can be split between two salespeople by percentage.
- The two shares add up to 100%.
- Incentives and staff reports use the shares.

**One staff list:**
- Any separate salesperson register is consolidated into the staff list.
- Historical attribution is kept (section 29).

### ST-POS-3: E-bill

- Sent only with the **send my bill** consent (ST-CMP-6). It goes after the bill syncs, and an offline bill queues its message.
- **P2 (manual).**
  - The message is queued. Staff can show a share link (a QR on the customer display) or send it from the store phone.
  - The link is short-lived and opens only that bill.
- **P3 (connected).**
  - It is sent through the chosen provider.
  - SMS needs a DLT-registered sender and template. WhatsApp needs an approved template through a Business Solution Provider.
- Delivery status shows on the bill. A failed send never touches the bill.

### ST-POS-4: Gift vouchers

- A voucher is issued at the till as its own document, with a number in the `GV` series (ST-CMP-5), a value and an expiry of **12 months**.
- It can be used in parts, and the balance stays on it.
- Issue and use are online only (section 28).
- **Vouchers are neither goods nor services (Circular 243/37/2024).**
  - GST is charged only when the voucher is used, on the goods bought with it.
  - No GST is due on the value of expired, unused vouchers.
- Every change to the balance is a record, never an edited total.

### ST-POS-5: Credit notes as payment

- **Online only (Anand, Q9).** An offline till refuses every credit note, known or unknown, with a clear message (section 28).
- The till refuses offline credit-note use before bill finalisation; flagging an unknown credit note after acceptance is insufficient. It can offer a permitted tender for an otherwise valid B2C bill.
- The completed store milestone also includes active **online store credit** as a return remedy and payment instrument where eligibility is approved. SO-09 must settle issuance eligibility, validity and expiry, customer identification, partial redemption, store scope, recovery, reconciliation and applicable OQ-45 controls before activation. This inclusion does not approve a cash refund or a no-bill return.

### ST-POS-6: Customer display

- A second screen facing the customer shows the lines, the offers applied, the savings, the total, the UPI QR, the consent questions (ST-CMP-6) and a thank-you.
- It never shows cost, PIN prompts or staff-only messages.
- It runs as a second browser window from the same till. No hardware other than a screen is needed.

## 8. Customers

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-CUS-1 | Customer record with history, consents, rights, merge and retention | P1 | — |
| ST-CUS-2 | Saved size per brand | P1 | — |
| ST-CUS-3 | Loyalty points and tiers | P4 | OQ-47 |
| ST-CUS-4 | WhatsApp campaigns to opted-in customers | P2, then P3 | Provider choice |
| ST-CUS-5 | Back-in-stock alerts | P2, then P3 | Provider choice |

### ST-CUS-1: Customer record

- The phone number is the key.
- The record holds a name, an optional GSTIN for B2B, the two consents (ST-CMP-6) and purchase history taken from bills.

**Customer rights screen (Anand, 27 September 2026).** At the customer's request, staff can:
- Show what is held about them.
- Correct it.
- Withdraw consent.
- Erase it.
- Each action is recorded.

**Merge and new phone number (Anand, 27 September 2026):**
- Two records for the same person can be merged, keeping both histories.
- A changed phone number moves the record to the new number.
- Both actions need the customer present and are recorded.

**Retention (Anand; 3 years is proposed):**
- A customer with no purchase for 3 years has their profile erased or made anonymous.
- **Tax records are kept for their legal period:** 72 months from the due date of the annual return for that year, or longer while an appeal or investigation is open (CGST Act s.36).
- The name, GSTIN and address printed on a tax invoice stay with that invoice. Erasure removes the customer profile, sizes, consents and marketing history, not the invoice.

### ST-CUS-2: Saved size

- The last size bought per brand and category is learned from bills. Staff may correct it with the customer's agreement.
- It shows on the till when the customer is added to a bill.

### ST-CUS-3: Loyalty (P4)

- Points are earned on the amount paid, excluding voucher and credit-note tenders.
- Tiers are set by spend over the last 12 months.
- Earn rate, point value, tiers and expiry are configuration Anand sets.
- There is no enrolment offline. Redemption is online only.
- Unused points are a liability. It goes live only after OQ-47.

### ST-CUS-4: Campaigns

- Campaigns go only to customers with the **send me offers** consent who are not marked under 18.
- The audience can be filtered by brand bought, size, last visit, store and tier.
- Messages use approved templates only. Every send is logged. An opt-out is honoured at once.
- **P2 (manual):** the audience list is exported for sending outside the app, and the send is logged back in.

### ST-CUS-5: Back-in-stock

- Staff log a request for a customer: item, size and store. The customer's agreement to be contacted about it is recorded on the request.
- When that size is accepted at that store, staff get a task (P2) and, from P3, the customer gets a message.
- A request lapses after **30 days**.

## 9. Customer orders

The term is **customer reservation** (Anand, Q2). “Hold” is reserved for stock states.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-ORD-1 | Customer reservation with advance payment | P1 | CA sign-off of wording (section 32) |
| ST-ORD-2 | Special orders | P1 | — |
| ST-ORD-3 | Alteration tracking | P1 | OQ-49 for custody |
| ST-ORD-4 | Order in store, deliver to home | P4 | OQ-49 |

### ST-ORD-1: Customer reservation (Anand, Q3)

- Staff reserve specific accepted pieces for a named customer. The pieces leave ATS as a stock reservation. It is not a sale.
- A reservation lasts **7 days**, or **3 days while a sale period is running**.
- An advance may be taken in any tender. It is held as a customer advance.

**Tax on the advance:**
- No GST is due on an advance for goods (Notification 66/2017-CT).
- A **receipt voucher** is still issued for it (`RV` series, ST-CMP-5).

**Pickup, expiry and forfeiture:**
- On pickup, a normal bill is made and the advance is used as a tender.
- On expiry or cancellation, the pieces return to ATS. Whether the advance is refunded or kept follows a policy Anand sets.
- Where it is kept, the reservation terms say: **"The advance is forfeited if the item is not collected by [date]."** It is never called a cancellation fee (Circular 178/10/2022). This wording is (baseline, CA to confirm).
- No GST is charged on a forfeited advance.

Reservations are online only.

### ST-ORD-2: Special orders

- For an item no store has: record the customer, brand, style, size, colour and an optional advance (with a receipt voucher).
- The request becomes a booking line or a transfer request.
- Its status moves through asked → ordered → arrived → customer told → collected or cancelled.

### ST-ORD-3: Alterations

- A job card is linked to the bill line. It records what to alter, measurements, the tailor (in-house or outside), the promised date, any charge and the status.
- A paid alteration is its own bill line at 5% (SAC 9988). A free alteration has no charge and no tax (baseline, CA to confirm).
- The customer is told when the item is ready.
- While the store holds the garment it is in billed-retained custody.

### ST-ORD-4: Home delivery (P4)

- The customer pays at the store for an item held at another site. The item reaches the customer's address, and custody is tracked until handover.
- Still to design: whether the bill is made at the site holding the stock or at the selling store after a transfer. This waits on OQ-49.

## 10. Receiving

Receiving records physical counts separately from commercial evidence and acceptance. A booking is purchase intent, not proof of receipt. Goods must be accepted before they enter available-to-sell stock.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-REC-1 | Three-way match of booking, brand invoice and goods received | P1 | — |
| ST-REC-2 | ITC check against GSTR-2B | P2, then P3 | — |
| ST-REC-3 | Automatic debit note draft for shortages | P1 | OQ-47 for posting |

### ST-REC-1: Three-way match

- Each line shows booked, invoiced and counted quantity and cost as match, short, excess or cost differs.
- The tolerance is **0 pieces and ₹1 per line**.
- A mismatch raises an owned exception. It never stops counting.

### ST-REC-2: GSTR-2B check

- The invoice's GSTIN, number, date, taxable value and tax are recorded at receiving.
- **P2 (file):** Accounts downloads the month's GSTR-2B JSON from the GST portal and uploads it.
- **P3 (connected):** the file is fetched through the GSTR-2B API.
- Every received invoice then shows **found**, **not found** or **amount differs**.
- Unmatched invoices are listed with vendor and age. This never blocks receiving.

### ST-REC-3: Debit note draft

- When a shortage is approved (invoiced but never received), the system drafts a debit note to the vendor at invoice cost plus tax, linked to the GRN and the vendor claim.
- Accounts prepares and reviews the draft. A different person holding Owner authority approves it. Accounts then issues the centrally numbered debit note. The draft and approval cannot be mistaken for an issued document.
- Accounting posting and voucher-export implementation wait for OQ-47.

## 11. Stock

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-INV-1 | Broken-size alerts | P1 | — |
| ST-INV-2 | Stock-ageing alerts | P1 | — |
| ST-INV-3 | Scheduled cycle counts | P1 | — |
| ST-INV-4 | Shrinkage report | P1 | — |
| ST-INV-5 | Min and max stock levels, with replenishment | P4 | Policy |

### ST-INV-1: Broken size (Anand, 27 September 2026)

- An alert fires when a style-colour at a store is missing **40% or more of its core sizes** while it still has stock.
- Core sizes and the percentage are configured per category.
- The alert feeds size balancing (ST-TRF-1) and markdown suggestions.

### ST-INV-2: Ageing, season-aware (Anand, 27 September 2026)

- Age is measured against the item's season, not as flat days.
- In-season stock is flagged after **90 days with no sale**.
- Stock whose season has ended counts as aged from the day its season ended.
- Each piece also shows days since it first arrived in the company (from its origin) and days at this store.
- Unknown-historical-season stock is shown as its own group, never guessed.

### ST-INV-3: Scheduled counts

- The Owner sets a schedule per store, for example each brand once a month.
- The count due today shows as a task and uses a blind count, where the expected quantity is hidden during counting.
- A missed count raises an exception.
- A count at a trading store freezes sales at that store. Starting it requires online confirmation that every till has synced and stopped finalising sales; no offline till may keep selling during the count. Sales resume only after the count closes or is cancelled and its freeze is released. This settles OQ-57 for store counts.

### ST-INV-4: Shrinkage report

- Shows pieces and cost lost to shrinkage, by store, brand, category and period, as a share of sales.
- Built from approved count adjustments and write-offs typed as shrinkage.

### ST-INV-5: Min and max (P4)

- Applies only to items flagged `replenishable`. The flag is set per item or category.
- Each store has a minimum and maximum per style-size, set by hand or proposed from its sales rate.
- When available plus in-transit stock falls below the minimum, the system suggests a transfer up to the maximum. A person approves it.
- This is a plain rule, not a forecast model.

## 12. Transfers

The transfer workflow covers request, two-person approval with reservation, dispatch by scan, e-way, receipt, shortage, excess, and return to source.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-TRF-1 | Suggested transfers to balance sizes between stores | P1 | — |
| ST-TRF-2 | Transfer documents: delivery challan within a GSTIN, tax invoice between GSTINs | P1 | — |

### ST-TRF-1: Size balancing

- Finds a size a store lacks (ST-INV-1) that another store holds beyond **8 weeks of its sales**.
- A suggestion is made only if it moves **at least 3 pieces or ₹3,000 at MRP** (Anand, 27 September 2026). MRP is used because store roles cannot see cost.
- Once a person approves a suggestion, it becomes an ordinary transfer request.

### ST-TRF-2: Transfer documents

- **Within the same GSTIN:** each dispatch carries a delivery challan, numbered in at most 16 characters (CGST Rules 55).
- **Between different GSTINs** (for example KDPS’s Bihar ↔ Jharkhand movement): each dispatch carries a tax invoice from the sending GSTIN to the receiving GSTIN.
- Which document applies is worked out from the two sites' GSTINs, never from their state names.

## 13. Booking and buying

Booking means purchase intent from a vendor. It supports approval, links to receipts, corrections and closure.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-BUY-1 | Open-to-buy budget | P1 | — |
| ST-BUY-2 | Size-curve planning | P1 | — |

### ST-BUY-1: Open-to-buy

- A budget is set per brand, season and store (or the whole company) at cost.
- Open-to-buy is the budget minus open bookings minus goods received.
- A booking that goes over it needs Owner approval. Only roles allowed to see cost see this screen.

### ST-BUY-2: Size curve (Anand, 27 September 2026)

- Each brand and category at each store has a size split, taken from the **same season last year** (SS from last SS, AW from last AW).
- When a booking line is entered as a style total, the curve fills in the sizes. The buyer can change them.

## 14. Money

Money operations include day summary, store targets, cash by tender, vendor ledger, cash ledger, and bank statement import and match.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-MNY-1 | UPI and card settlements matched to bank statements | P2, then P3 | OQ-24 |
| ST-MNY-2 | Cash handover with a count by note and coin | P1 | OQ-08 |
| ST-MNY-3 | Petty cash | P1 | OQ-03, OQ-08 |
| ST-MNY-4 | Brand payables | P1 for outright; P4 for SOR | OQ-26 for SOR |

### ST-MNY-1: Settlement matching

- **P2 (file):** the provider's settlement report is uploaded as CSV (terminal, date, gross, fee, net and UTR).
- **P3 (connected):** the report is fetched from the provider.
- The report is matched to the till's tenders, then to the bank credit. Fees are kept separately.
- Unmatched items are listed with their age.

### ST-MNY-2: Cash count

- At the Z-report, the cashier counts each note and coin: ₹500, 200, 100, 50, 20, 10 and coins.
- Expected cash at the named custodian and cutoff = opening cash + cash sale collections + customer advances, dues recovery, top-ups and other authorised cash receipts − actual cash refunds, deposits, head-office transfers, petty spending and other authorised cash payments. Use gross cash collections so a refund is not subtracted twice; a non-cash refund is not a cash outflow.
- A variance is an owned exception, confirmed with the manager's personal PIN, never a balancing entry.
- A handover or deposit records both sides. Internal custody transfers change each custodian's balance but cancel within a consolidated boundary containing both; cash in transit retains an explicit custodian or transit state. OQ-03 and OQ-08 still govern expense heads and custody policy.

### ST-MNY-3: Petty cash

- Each store has a petty cash float with a named custodian.
- Each spend records an expense head, amount and bill photo. A missing photo raises an exception.
- The float is topped up from the till or head office.
- A spend over **₹2,000** needs Owner approval.

### ST-MNY-4: Brand payables

- Per brand or vendor: owed, paid, due and ageing.
- Outright brands come in P1.
- For SOR and consignment, the amount owed arises on sale under the brand's terms (ST-BRD-1). That part goes live in P4, after OQ-26.

General Payments, Collections, Expenses and accounting-package screens are not selected for delivery by this store operations increment; the explicitly specified money features remain in scope.

## 15. Brand settlement

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-BRD-1 | Terms stored per brand: SOR, outright, consignment or concession | P1 | OQ-26 for SOR effects |
| ST-BRD-5 | SOR ageing alert at 5 months from the brand's dispatch date | P1 | — |
| ST-BRD-2 | Automatic Sale and SOH reports in each brand's format | P1 | — |
| ST-BRD-3 | Claims for brand-funded discounts, settled through the brand's commercial credit notes | P1 | — |
| ST-BRD-6 | Per-brand setting: promotion-services agreement yes/no | P1 | CA sign-off (section 32) |
| ST-BRD-4 | Margin-share calculation | P1 | OQ-50 before it is called final |

### ST-BRD-1: Brand terms

- Terms are effective-dated per brand and season: the commercial model, margin %, return allowance, discount-funding share and payment days.
- The supported commercial models are SOR, consignment, outright and concession.
- A brand with no recorded model shows as **unknown**. Nothing is assumed (D9).
- Brands with an unknown model are listed for Anand to fill in before P1 goes live. Work that depends on the model (funding split, claims, margin share, SOR ageing, payables, GMROI) treats the brand as unknown and says so.

### ST-BRD-5: SOR ageing

- An alert fires when SOR stock reaches **5 months from the brand's dispatch date**.
- The brand must invoice at the time of supply or at 6 months, whichever is earlier (CGST Act s.31(7)).
- The alert gives a month to settle each piece: sell it, return it, or get the brand's invoice.
- The brand's dispatch date is recorded at receiving for SOR goods.

### ST-BRD-2: Brand reports

- Each brand has a saved layout: columns, order, headers, formats and file type. The KDPS Sale and SOH sheets are the first layouts.
- Reports are made monthly and on demand, per store, and state their basis and as-of time.

### ST-BRD-3: Discount claims

- At month end, the claim per brand is the sum of the brand-funded share on each discounted line (ST-OFR-2).
- Its status moves through raised → accepted → settled, or settled short with the difference kept.
- **Claims are settled through the brand's commercial credit notes, with no GST effect on our bills** (Circular 251/08/2025).

### ST-BRD-6: Promotion-services agreement

- Each brand has a setting: **"promotion-services agreement: yes/no"**. The default is **No**.
- If it is Yes, every claim for that brand is flagged for Accounts. Money paid under such an agreement may be payment for a service, not a discount.

### ST-BRD-4: Margin share

- For SOR and concession brands, each sale's split between the brand and the retailer comes from the terms.
- A monthly statement goes to each brand. It is labelled an estimate until OQ-50 is decided.

## 16. Offers and price

Required offer capabilities:
- The offer builder with approval.
- The rules engine (buy 2 get 1, slabs, free gift, stacking).
- Running offers, the discount report and the price book.
- End-of-season markdowns.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-OFR-1 | Return on each discount | P1 | — |
| ST-OFR-2 | Split of brand-funded and own-funded discount | P1 | — |
| ST-OFR-3 | Offer simulation before launch | P1 | — |

Each offer also carries the tax settings from section 6: whether a bank instant discount reduces value (default No), and the allocation method for buy 2 get 1 (default by MRP).

### ST-OFR-1: Return on discount

- After an offer runs, it shows sales, pieces, discount given, gross margin and the brand-funded part.
- It compares against a stated baseline period.

### ST-OFR-2: Funding split

- Every discounted line records the brand's share and the retailer's share.
- These are reported per offer, brand and store.

### ST-OFR-3: Simulation (Anand, 27 September 2026)

- Before approval, the offer is run against real bills from **the last 4 weeks** and from **the same period last year**.
- It shows the estimated discount, pieces affected and margin effect, labelled as an estimate.

## 17. Reports

These rules apply to every report:
- It states its basis, its as-of time and any missing data.
- It runs away from the billing and receiving path.
- It exports to `.xlsx` and is limited to the viewer's stores.
- It hides cost and margin from store roles.

The day summary, discount report and store dashboard move under Reports.

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-RPT-1 | Sales | P1 | OQ-15 for conversion |
| ST-RPT-2 | Inventory: sell-through, weeks of cover, GMROI, dead stock | P1 | — |
| ST-RPT-3 | Brand performance | P1 | OQ-50 before it is called final |
| ST-RPT-4 | Staff performance | P1 | — |
| ST-RPT-5 | GST reports | P1 | — |
| ST-RPT-6 | Exceptions: voids, price overrides, PIN use, cash variance, no-bill returns | P1 | — |

### ST-RPT-1: Sales

- By day, store, brand, category, salesperson and tender.
- Shows bills, pieces, value, average selling price, average bill value, units per bill, discount % and target achievement.

### ST-RPT-2: Inventory

- **Sell-through** is sold ÷ received for the season or the chosen period.
- **Weeks of cover** is on hand ÷ average weekly sales over the last 4 weeks.
- **GMROI** is gross margin ÷ average stock at cost, **for outright brands only**. SOR brands show sell-through and margin % instead.
- **Dead stock** is season-aware (ST-INV-2): in-season stock with no sale in 90 days.

### ST-RPT-3: Brand performance

- Per brand and store: sales, margin or commission, sell-through, discount depth, returns and stock age.
- GMROI is shown only for outright brands (ST-RPT-2).

### ST-RPT-4: Staff performance

- Per salesperson, using split shares (ST-POS-2): sales, bills, units per bill, average bill value, returns against their sales, and target achievement.

### ST-RPT-5: GST reports

- Outward summary by rate and HSN, B2B and B2C split, credit notes, IRN status, and documents issued and cancelled for GSTR-1 Table 13 (including unused offline numbers).
- It is prepared for Accounts to file. It is not a filing.

### ST-RPT-6: Exceptions

- Per store and staff member: cancelled bills, price overrides and manual discounts, manager PIN uses by manager, cash variances, bill-number holes, returns without a bill and late syncs.

**Returns without a bill (Anand, 27 September 2026):**
- They are capped at **2 per phone number** and **5 per staff member** per month.
- Going over a cap raises a flag; the cap alone does not block a separate valid bill or an otherwise eligible return. OQ-44 still determines whether a no-bill return is allowed at all (acceptance item 6).
- Whether a no-bill return is allowed at all follows the configured customer return policy (section 33.3) and the no-bill eligibility gate OQ-44.

Bill flags and nightly checks feed the exception report.

## 18. People

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-HR-1 | Staff list per store | P1 | — |
| ST-HR-2 | Selfie attendance inside the store geofence | P2 | — |
| ST-HR-3 | Shift roster, leave and overtime | P2 | OQ-19 |
| ST-HR-4 | Sales incentives calculated from POS data | P2 | OQ-02, OQ-19 |
| ST-HR-5 | Shops & Establishments registers | P4 | Forms confirmed |
| ST-HR-6 | Payroll export to a separate payroll tool | P2 | OQ-19 |

### ST-HR-1: Staff list

- Maintain staff records and their store assignments.
- The till's salesperson list, incentives, staff reports and attendance all read from this one list.

### ST-HR-2: Selfie attendance (Anand, Q5)

- Staff check in and out on a phone. The check-in records a photo and the phone's location.
- Inside the store radius (**150 m**, configurable per store, because GPS drifts in malls), it is accepted. Outside, it is recorded and flagged for review, not refused.
- **The photo is evidence only. There is no face matching.**
- Offline check-ins are kept on the phone and sent later with their original time.
- Photos are deleted after **90 days**.
- Before first use, each employee sees a privacy notice for the photo and location, and their acknowledgement is recorded.

### ST-HR-3: Roster, leave and overtime

- A weekly roster per store.
- Leave requests with approval. Leave is different from absence.
- Overtime is time worked past the shift and its grace period, approved by the manager.
- Rules are versioned. Raw check-ins are never changed.

### ST-HR-4: Incentives

- Calculated monthly per salesperson from the lines they are tagged on, using split shares (ST-POS-2), net of returns, and the approved slabs.
- Each person sees their own result. The manager sees the team.

### ST-HR-5: S&E registers (P4)

- Each store's applicable Shops and Establishments registers (Bihar and Jharkhand for KDPS) (muster, wages, leave, overtime) are produced from attendance and payroll-export data.
- They go live once compliance confirms the exact forms.

### ST-HR-6: Payroll export (Anand, Q4)

- A monthly file per staff member: payable days, leave, overtime hours, incentives and known deductions, in the payroll tool's format.
- In-app payroll calculation and payslips come later, and stay in the full product.

## 19. Setup and control

| ID | Requirement | Phase | Gate |
| --- | --- | --- | --- |
| ST-OPS-1 | Approvals inbox | P1 | — |
| ST-OPS-2 | Alerts centre | P1 | OQ-18 for routing |
| ST-OPS-3 | Audit log | P1 | — |
| ST-OPS-4 | Store task checklists | P1 | — |
| ST-OPS-5 | Remove duplicate old screens | P1 | — |
| ST-OPS-6 | Feature switches per store | P1 | — |

### ST-OPS-1: Approvals

- Every approval lands in one shared approvals inbox.

### ST-OPS-2: Alerts

- Adds these alert kinds:
  - broken size, ageing and SOR ageing
  - IRN days left
  - count due
  - cash variance
  - back-in-stock
  - reservation expiry
  - checklist missed
  - no-bill return cap
  - flagged brand claim

### ST-OPS-3: Audit log

- Every write records who, what, when, where, and the values before and after (acceptance item 5).
- Every change to a tax setting records its new version and the date it applies from.
- One read-only page shows the log, filters by person, store, record type and date, and exports.
- The audit page complements history available on each record.

### ST-OPS-4: Task checklists

- Admin sets templates (opening, closing, weekly display check, monthly count).
- Each store gets today's list on the Today screen. Staff tick items, with an optional photo.
- Missed items show on Today and in the exception report.
- The site-readiness checklist stays separate.

### ST-OPS-5: Remove old screens (Anand, D5)

- Once the replacement covers each workflow, retire duplicate transfer, count, return-to-brand, vendor and master screens, their obsolete implementation and demonstration records. Users have one supported route for each job. This is the approved consolidation decision (D5).

### ST-OPS-6: Feature switches

- Admin switches each feature on or off per store (acceptance item 8).
- Each feature also has a switch between manual mode and connected provider (section 25).
- Every change is recorded.
- A feature whose gate is still open cannot be switched on for a real store.

## 20. What must come first

- **The staff list (ST-HR-1)** comes before the salesperson list from staff, split shares, incentives, staff reports and attendance.
- **The customer record and consent (ST-CUS-1, ST-CMP-6)** come before the e-bill, loyalty, campaigns, back-in-stock alerts and reservations.
- **The customer display (ST-POS-6)** comes before customer-confirmed consent.
- **Versioned tax settings (section 6)** come before any tax logic is changed.
- **Store prefixes and number series (ST-CMP-5)** come before gift vouchers, receipt vouchers, the new credit-note format and delivery challans. The new format starts on a 1 April.
- **Brand terms (ST-BRD-1)** come before the funding split, claims, margin share, SOR ageing, brand payables and brand performance.
- **Broken-size alerts (ST-INV-1)** come before size balancing (ST-TRF-1).
- **Feature switches (ST-OPS-6)** come before any pilot.
- **Reporting data kept apart from billing** comes before the heavy reports.

## 21. Approved scoped decisions

| ID | Decision |
| --- | --- |
| D1 | Credit notes need a live balance and are online only. The till refuses their use offline before finalising the bill. |
| D2 | Document numbers must respect the 16-character limit; use the formats in ST-CMP-5 and Q8/Q10. |
| D3 | The ₹2,500 tax threshold and applicable rates are versioned configuration, never hard-coded. Verify this as an acceptance condition. |
| D4 | Payroll export is the current delivery scope; in-app payroll and payslips remain later product scope. |
| D5 | Duplicate legacy screens and demonstration records are retired once their replacement covers each workflow (ST-OPS-5). |
| D6 | Anand's recorded decision closes the relevant gate within this store operations scope (Q1). |
| D7 | Use “customer reservation”; “hold” remains a stock-state term (Q2). |
| D8 | B2B invoices and credit notes against them require online IRN handling. Where e-invoicing applies, the B2B sale remains pending until its IRN is captured; no tax invoice prints and no goods leave before issue. This restriction does not block a separate valid B2C sale using permitted tender. |
| D9 | Missing brand models stay unknown. Never assume outright or another commercial term. Anand supplies missing brand models before P1 goes live. |

## 22. Decisions

### 22.1 Anand's answers (27 September 2026)

| # | Question | Answer |
| --- | --- | --- |
| Q1 | Can this PRD close its own gates? | Yes, within this document’s store operations scope. |
| Q2 | Name for a hold with advance | Customer reservation |
| Q3 | Reservation or sale? | A stock reservation, with the advance held as a customer advance and a receipt voucher issued |
| Q4 | Payroll | Export for now, with in-app payroll later |
| Q5 | Selfie attendance | Photo only, no face matching |
| Q6 | Providers | Picked in P3. Until then, P2 manual modes cover these features. |
| Q7 | Store Manager role | A personal PIN for each manager now, and a Store Manager role before the multi-store rollout |
| Q8 | Invoice number | `XXX/26-27/n` |
| Q9 | Offline credit notes | Online only |
| Q10 | Other series and store prefix | Each store gets a fixed 3-letter prefix, separate from its store code. Credit notes, receipt vouchers and gift vouchers use `XXX/CN/2627/n`, `XXX/RV/2627/n` and `XXX/GV/2627/n`, which allow up to 9,999 of each per store per year. Tax invoices stay `XXX/26-27/n`. |
| Q11 | B2B bill in P2, with the IRN typed in by hand | Saved as pending; the tax invoice prints only once Accounts types in the IRN; goods leave only with it (baseline, CA to confirm) |
| D9 | Brand with no recorded model | Unknown, never assumed. Anand fills those brands in before P1 goes live. |
| — | Delivery challan and debit note series | `XXX/DC/2627/n` per sending site; `XXX/DN/2627/n` per GSTIN from head office |

### 22.2 Decisions embedded in requirements

The detailed requirements preserve Anand's tax, privacy, business-rule and structure decisions of 27 September 2026. These include tax configuration and baselines, customer rights and consent, reservations, vouchers, gift-stock credit reversal, transfer documents, SOR ageing and promotion-services agreements. Their confirmation status is stated alongside each rule; section 32 retains the full CA sign-off list.

### 22.3 Phase assignments — confirmed for SO-01 on 29 September 2026

These phase placements were not explicitly named in the original phase table and were confirmed for delivery sequencing on 29 September 2026. A phase assignment does not close any CA, policy, provider or activation gate:

| Item | Placed in |
| --- | --- |
| Back-in-stock alerts (ST-CUS-5) | P2 (staff task and manual message), then P3 |
| Customer rights, merge and retention (ST-CUS-1) | P1, with the customer record |
| Credit notes online only (ST-POS-5), old-screen removal (ST-OPS-5), feature switches (ST-OPS-6) | P1 |
| Brand payables for outright brands (ST-MNY-4) | P1 (only SOR is in P4) |
| OTP for marketing consent | P3, with SMS. Until then the customer display is used. |
| Transfer documents (ST-TRF-2) and the promotion-services setting (ST-BRD-6) | P1 |

### 22.4 Still open

The direct questions Q11 and D9 were answered on 27 September 2026, and section 22.3 phase placements were confirmed on 29 September 2026. CA sign-offs, the proposed retention period, provider choices and the named policy gates remain open where stated.

## 23. Numbers

| Item | Value | Status |
| --- | --- | --- |
| IRN alerts | 23, 10 and 3 days left | Confirmed |
| Gift voucher expiry | 12 months | Confirmed |
| Back-in-stock request lapses | 30 days | Confirmed |
| Customer reservation length | 7 days; 3 days during a sale period | Confirmed |
| Broken size | 40% or more of core sizes missing, set per category | Confirmed |
| Ageing and dead stock | Season-aware; 90 days with no sale in-season | Confirmed |
| Past-season stock aged from the end of its season | — | Confirmed |
| Size balancing | More than 8 weeks of cover, and at least 3 pieces or ₹3,000 | Confirmed |
| Size balancing ₹3,000 measured at MRP | — | Confirmed |
| Three-way match tolerance | 0 pieces, ₹1 per line | Confirmed |
| Petty cash approval above | ₹2,000 per spend | Confirmed |
| Offer simulation | Last 4 weeks plus the same period last year | Confirmed |
| Size curve | Same season last year | Confirmed |
| Attendance radius | 150 m, configurable per store | Confirmed |
| Attendance photo kept | 90 days | Confirmed |
| Customer data retention | 3 years with no purchase | Proposed by Anand |
| No-bill return caps | 2 per phone number and 5 per staff member per month | Confirmed |
| SOR ageing alert | 5 months from the brand's dispatch date | Confirmed |

## 24. Acceptance

A requirement is done when all of these are true:

1. It works end to end with persisted operational data and follows section 5.
2. Role and store scope are enforced on the server.
3. Automated end-to-end acceptance checks cover the main path, refusals, offline behaviour and connection loss.
4. Every money or tax result passes golden test cases. **Tax test cases use the baselines in section 6 until the CA signs them off (section 32).**
5. Every write creates an audit record with before and after values.
6. A prohibited action is refused before finalisation: for example offline credit-note, voucher or store-credit use; an ineligible return; or issue of a B2B tax invoice before its required IRN. The till offers a permitted path for an otherwise valid B2C sale, and ordinary report, message and provider failures do not block that sale. Applicable legal and policy restrictions still govern the action attempted.
7. A feature that depends on an external service works in manual mode and has a mock provider for tests.
8. It can be switched on per store with a feature switch (ST-OPS-6).
9. Its data migrations have been checked against production-sized data.
10. It meets the success-measure target for its area where one applies (section 26).
11. Its gate is closed before it goes live in a real store.

## 25. Phasing

**Rule (Anand, 27 September 2026):**
- Every eligible operational feature is built fully, with a **manual or file-upload mode** wherever an external service will later plug in.
- Connecting the service later is just a switch (ST-OPS-6).
- Most decision-gated operational features may be built with a safe inactive state before activation approval. OQ-47 is different: accounting-module recognition, posting, mappings and voucher-export implementation wait for its closure. Operational records, debit-note drafts and permitted non-accounting exports may proceed without implying an approved accounting result.

| Phase | Scope |
| --- | --- |
| **P1: Internal only** | Tax logic and settings (ST-CMP-1, 2, 3, 5, 7), consent capture (ST-CMP-6), staff list and salesperson, all reports, exceptions, audit log, alerts, stock alerts, cycle counts, shrinkage, size balancing, transfer documents, cash count, petty cash, brand terms, SOR ageing, brand report layouts, claims and the promotion-services setting, margin share, offer return, split and simulation, customer record and sizes, reservations, special orders, alterations, checklists, customer display, gift vouchers, three-way match, debit note draft, open-to-buy, size curves. Also confirmed here (22.3): customer rights, credit notes online only, old-screen removal, feature switches, and payables for outright brands. Online store credit belongs to the completed store milestone after SO-09 controls and OQ-45 approval, not to unconditional P1 activation. |
| **P2: Built with a manual or file mode** | IRN queue (IRN typed in by hand; B2B bill pending until then, Q11), UPI and card (recorded by hand), settlement matching (CSV upload), GSTR-2B check (JSON downloaded from the portal and uploaded), e-bill and campaigns (queued, with a share link), back-in-stock (staff task), selfie attendance, roster, leave and overtime, incentives, payroll export file |
| **P3: Connect providers** | Payment gateway and card machine, WhatsApp BSP and SMS DLT (with OTP consent), IRP/GSP API (IRN at the counter), GSTR-2B API |
| **P4: Waiting on policy** | Loyalty (OQ-47), home delivery (OQ-49), min/max replenishment, Shops & Establishments registers (forms), brand payables for SOR (OQ-26) |

## 26. Success measures

| Area | Measure | Target |
| --- | --- | --- |
| Tax | Till and server tax mismatches | 0 per month |
| Tax | B2B documents past 30 days without an IRN | 0 |
| Tax | Document numbers breaking Rules 46, 50, 53 or 55 | 0 |
| Tax | SOR stock past 6 months from dispatch without the brand's invoice | 0 |
| Billing | Time to complete a 3-item bill with UPI | ≤ 45 s |
| Billing | Offline bills synced within 1 hour of reconnecting | ≥ 99% |
| Money | UPI and card payments matched automatically | ≥ 95% |
| Money | Cash variance per store per month | ≤ ₹500 |
| Brands | Time to produce the monthly brand Sale and SOH reports | < 10 min (from days) |
| Brands | Brand-funded discount claimed back | ≥ 98% of what is due |
| Stock | Broken-size alerts acted on within 7 days | ≥ 80% |
| Stock | Count accuracy (pieces) | ≥ 99% |
| Stock | Season-end sell-through | +5 points over baseline |
| Customers | Bills with a customer attached | ≥ 60% |
| Customers | Marketing opt-in rate | Tracked, no target |
| People | Attendance captured digitally | 100% of staff |
| Reports | At least 95 of 100 standard requests on the approved reference dataset | ≤ 3 s per request (main PRD Q-03) |
| Reports | Visible one-store, one-month report load on supported hardware and connection | ≤ 5 s |

Each measure states its baseline and its source before its phase goes live.

## 27. Speed and reliability targets

| What | Target |
| --- | --- |
| Complete a 3-item bill with UPI, from first scan to receipt | ≤ 45 s |
| Offline bills synced after reconnecting | ≥ 99% within 1 hour; sync starts without anyone pressing anything |
| Standard report request | At least 95 of 100 complete within 3 s on the approved reference dataset (main PRD Q-03) |
| Visible standard report load | ≤ 5 s for one store and one month |
| A valid B2C bill never waits on | A report, an export, a message send, a provider call or an alert. Applicable B2B tax-invoice issue and goods release wait for IRN capture (D8). |

The three-second request target uses the approved reference dataset; the five-second visible-load target uses supported hardware and a normal store connection. Measure them separately and keep report work isolated from billing.

## 28. Offline behaviour during peaks

- The till keeps billing B2C through a sale-day rush with no connection, with local bill capture, till authority and automatic synchronisation after reconnection.
- ST-HR-2 also permits offline phone check-in/out capture, retained through restart and uploaded later with original event and recording times under the approved attendance profile. This does not extend offline authority to other store screens.
- These stay **online only**:
  - Gift vouchers, loyalty, credit notes and store credit (Q9 and section 33.3).
  - B2B bills, and credit notes against them (ST-CMP-4, D8).
- When the till is offline, trying one of these refuses that action before finalisation and shows one clear message: what cannot be done, why, and what to do instead. An otherwise valid B2C sale can continue with permitted tender. For example: "Take another payment, or wait for the connection", or "Bill this as a normal sale, or wait for the connection to issue a GST invoice".
- Customer reservations, special orders and the customer rights screen are online only too, and say so.

## 29. Migration: salesperson to staff list

- Every existing salesperson record is matched to a staff record at the same store.
- Rows with no match are listed for Admin to resolve. Nothing is guessed.
- Every past sale line keeps the salesperson it was sold under. History is never rewritten.
- The till reads only the staff list after migration. The separate salesperson register is then retired (ST-OPS-5).
- The migration is rehearsed on production-sized data first (acceptance item 9).

## 30. Rollout

1. **Pilot store.** Anand picks one store. Features are switched on there first (ST-OPS-6).
2. **Training.** A short guide and a hands-on session for each role before its features go live. The counter gets a one-page sheet for manual modes and offline messages.
3. **Watch.** For the first two weeks, the success measures (section 26) and exceptions (ST-RPT-6) are reviewed daily.
4. **Rollback.** Any feature can be switched off per store without losing records. Bills made while it was on stay valid. A switch to manual mode preserves every legal, offline and permission refusal; it cannot reactivate a prohibited action.
5. **Multi-store rollout.** The Store Manager role (Q7) must exist first. Stores are then added a few at a time.

## 31. Running costs

These are approximate figures from 2025. They must be re-quoted before P3.

| Item | What it costs | Paid for |
| --- | --- | --- |
| SMS DLT registration | About ₹5,900 once per entity, plus template registration | E-bill and OTP by SMS |
| SMS | About ₹0.15–0.25 per message | E-bill, OTP, back-in-stock |
| WhatsApp (through a BSP) | About ₹0.12 per utility message and ₹0.78 per marketing message (Meta India rates), plus the BSP's monthly fee | E-bill, back-in-stock, campaigns |
| UPI payments | No MDR on UPI to merchants today, but the provider may charge for QR devices, soundboxes or its platform | ST-POS-1 |
| Card payments | Merchant discount rate per card type (roughly 0.4–2%), plus card machine rent | ST-POS-1 |
| IRP / GSP API | Per-call or yearly plan, depending on the GSP | ST-CMP-4 in P3 |
| GSTR-2B API | Through the GSP, depending on the plan | ST-REC-2 in P3 |

In P1 and P2 none of these apply. Manual modes cost only staff time.

## 32. CA sign-off list

Each item uses its baseline until the CA signs it off. None is signed off yet.

| Item | Baseline | Status |
| --- | --- | --- |
| The ₹2,500 test method | Price ÷ 1.05 ≤ ₹2,500 means 5%, otherwise 18%; the line is an MRP of ₹2,625 | Open |
| The buy 2 get 1 split | The price is spread across all pieces in proportion to MRP; each piece takes its rate from its own share; no ITC reversal | Open |
| Bank instant discount | Does not reduce taxable value; recorded as a "bank offer" payment | Open |
| Exchange documents | A credit note and a new tax invoice, printed together on one slip | Open |
| Cross-GSTIN returns | Refused; returns only within the issuing GSTIN | Open |
| Alterations | A paid alteration is its own line at 5% (SAC 9988); a free alteration has no charge and no tax | Open |
| Rounding | Tax to the paisa per line; the total to the nearest rupee with a round-off line | Open |
| Accessories HSN master list | HSN from the brand's invoice or PT; rate from the CBIC rate schedule | Open |
| Table 13 reporting of unused numbers | Unused offline numbers reported as cancelled in GSTR-1 Table 13 at month end | Open |
| Reservation forfeiture wording | "The advance is forfeited if the item is not collected by [date]"; never a cancellation fee; no GST on forfeiture | Open |
| Brand promotion agreements | Default No; if Yes, claims are flagged for Accounts | Open |
| B2B bills in P2 | Saved as pending; the tax invoice prints only after the IRN is typed in; goods leave only with the invoice | Open |

**AATO and GSTINs: Anand to confirm** (section 6 default facts). **Brand models:** Anand fills in each brand whose model is unknown (D9).

## 33. Terms, policy gates and inherited operating context

### 33.1 Terms

- **ATS (available to sell):** accepted stock eligible for sale after reservations and other restrictions. A customer reservation removes pieces from ATS without making a sale.
- **Booking:** purchase intent from a vendor; it is distinct from receiving goods and from a customer reservation.
- **PT:** the merchandise preparation record used to establish item identity and attributes, including HSN, before approval for operational scanning. PT approval is not by itself acceptance into sellable stock.
- **GRN (Goods Receipt Note):** physical quantity and condition at the arrival site, not value or sellable stock. Good quantity stays in receiving hold until PT approval and physical acceptance.
- **Billed-retained custody:** customer-purchased goods physically held by the store after sale. They are outside ATS and cannot be sold again, allocated or transferred as retailer stock, or returned to a vendor. Track customer, bill line, item, quantity, location, expected pickup/delivery and final outcome.
- **SOR:** sale-or-return commercial arrangement. **SOH:** stock on hand. Commercial labels alone do not establish ownership or liabilities; approved brand terms govern their effects.
- **MRP:** maximum retail price. **HSN:** goods classification for tax. **SAC:** service classification for tax.
- **GSTIN:** GST registration identifier. **IRN:** invoice reference number. **IRP/GSP:** invoice registration portal / GST service provider. **ITC:** input tax credit. **AATO:** aggregate annual turnover.
- **UPI:** Unified Payments Interface. **UTR:** payment transfer reference. **BSP:** WhatsApp Business Solution Provider. **DLT:** SMS sender/template registration system. **OTP:** one-time password.
- **X-report:** interim till summary. **Z-report:** end-of-day till closure summary. **GMROI:** gross margin return on inventory investment, calculated as specified in ST-RPT-2.
- **SS/AW:** spring–summer / autumn–winter seasons. **S&E:** Shops and Establishments.

### 33.2 Policy gates

Gate IDs are retained for traceability. A gate blocks its affected capability, not unrelated work. Decisions in this document close only the portion they explicitly answer.

| Gate | Decision or input still needed where referenced |
| --- | --- |
| OQ-02 | Approved incentive slabs and the definition/formula of any retailer-specific incentive factor. |
| OQ-03 | Expense-head catalogue for the retailer. |
| OQ-08 | Cash custodians and cash-pickup policy, including till, petty-cash and consolidated store boundaries. |
| OQ-15 | Footfall source, whether manual or hardware, or explicit absence; conversion cannot be presented without its basis. |
| OQ-18 | Alert routing, acknowledgement, escalation and service-level timing. |
| OQ-19 | Salary structures and incentive source data needed for overtime, incentives and payroll export. |
| OQ-24 | Representative bank statements for each supported account and import layout. |
| OQ-26 | Commercial ownership, settlement, commission, buy-out, return, support and payment rules for the relevant brand model. Recorded model names alone do not settle these effects. |
| OQ-28 | The full role/action/field/site/brand matrix and journeys remain open. The six-role, single-permission-system baseline is settled; a separate grant bridge is not approved. |
| OQ-29 | Real-tenant opening stock approval requires recorded migration sources, mapping, quality and external-book reconciliation. OQ-54's season rule is already answered and does not close this gate. |
| OQ-44 | No-bill return eligibility, cost origin, stock condition and zero-price promotion/tax/funding treatment. The reporting caps in ST-RPT-6 do not grant eligibility. |
| OQ-45 | SO-09 must settle the controls applicable to active online store credit, including issuance, identity, validity, partial redemption, store scope, recovery and reconciliation. Refund/tender restoration and concurrent offline return rules remain open where applicable. |
| OQ-47 | Accounting boundary, recognition/posting rules, account mappings, voucher export scope and posting acknowledgements. This blocks accounting-module implementation, not permitted operational records or non-accounting exports. The source also cites this gate for loyalty liability; it does not specify the outstanding loyalty decision more precisely. |
| OQ-49 | Customer identity, custody location, pickup/delivery timing, cancellation, returns, loss/damage and uncollected-goods policy for billed-retained goods and home delivery. |
| OQ-50 | Profitability formulas, commission/funding treatment, expense allocation and when estimates can be called final. |
| OQ-57 | Answered on 29 September 2026: trading-store counts freeze sales after online confirmation that all tills have synced and stopped finalising; ST-INV-3 governs reopening. |
| Providers | Payment, messaging and statutory-service providers are chosen in P3; manual/file modes precede connection. |
| Replenishment policy | Approve the min/max replenishment policy before P4 activation. |
| S&E forms | Compliance confirms the exact applicable register forms before activation. |

Additional unresolved inputs are the reservation advance refund/forfeiture policy, proposed three-year customer retention period, AATO and GSTIN defaults, unknown brand models, and the CA checklist. The section 22.3 phase assignments are confirmed; they close none of these inputs.

### 33.3 Customer return policy context

A legal entity has a default return policy; an explicit store override takes precedence for that store. Ordinary and defective-item returns have separate configurable remedies and time limits. Available remedies are refund, exchange, store credit or refusal.

For bill-backed returns, eligible value is the amount paid for the returned quantity, less prior returns; it is not today's MRP. Replacement goods use current prices and offers. The customer pays a higher replacement value. For a lower value, policy chooses refund of the difference, store credit or refusal of the lower-value exchange. Exchange is not restricted to the same SKU unless explicitly configured. The completed store milestone includes active online store credit for an eligible return and online redemption, after SO-09 specifies and proves the section 33.2/OQ-45 controls. Store-credit validity and redemption controls are configured; refund methods follow approved tender rules and do not create an automatic right to cash. No-bill returns remain gated by OQ-44.

A request is evaluated for applicable policy, ordinary/defective case, time limit, permitted remedy, remaining paid value and physical condition. A sold quantity cannot be returned twice and remedies cannot exceed its remaining eligible value. Customer remedy and stock condition are separate: accepted undamaged goods become eligible stock only after condition acceptance; damaged goods enter quarantine immediately and remain unavailable to sell. Refusal does not accept goods into store custody. No-bill eligibility and unresolved offline/concurrent-return or tender-restoration policies remain gated.

### 33.4 Cash and operating boundaries

Cash closure uses an explicit custody boundary and cutoff: expected cash equals opening cash plus recorded cash inflows minus recorded cash outflows. Include collections, recovery, top-ups and authorised receipts, and actual refunds, deposits, transfers and authorised spending. Do not subtract a refund twice or treat non-cash refunds as cash outflows. Both sides of internal custody transfers are linked; they cancel only within a consolidated boundary containing both custodians. In-transit cash retains custody status. Pending or unsynced evidence cannot be represented as centrally reconciled cash.

Receiving, transfers, quarantine and billing remain distinct operational steps. A mismatch does not stop counting, a label or PT does not independently make stock sellable, and quarantine cannot be treated as saleable stock. Transfers retain their approval, reservation, scan-dispatch, transport evidence, receiving discrepancy and return-to-source journey. Commercial terms are recorded separately from physical movement.

## 34. SO-01 decision reconciliation (29 September 2026)

These decisions settle the identified source tensions and align this store increment with the main PRD. They do not close the gates named in sections 32–33 or approve pilot activation.

| Topic | Authoritative rule and remaining gate |
| --- | --- |
| Offline scope | Permitted B2C till work and ST-HR-2 phone attendance capture may run offline. Attendance retains original timestamps and uploads later under the approved profile (OQ-53); other store actions require an explicit offline rule. |
| Billing refusals | Refuse a prohibited tender, return or B2B issue before finalisation. Offer a permitted route for an otherwise valid B2C bill. Pending B2B documents do not print or release goods before required IRN capture; CA sign-off remains open. |
| Debit-note authority | Accounts prepares and reviews; a distinct Owner approves; Accounts issues the central number. Accounting posting waits for OQ-47. |
| Cash boundary | Use the full named-custodian/cutoff calculation in ST-MNY-2 and section 33.4, including advances, recovery, top-ups and linked custody transfers without double-counting. OQ-03/OQ-08 remain open. |
| Accounting timing | OQ-47 blocks accounting-module implementation, mappings and voucher exports. Permitted operational records, drafts and non-accounting exports may proceed without representing them as posted books. |
| Marketing for minors | Customers identified as under 18 receive no marketing in the completed store milestone. A future parental-consent path needs a separate campaign-policy decision. |
| Store credit | Active online issuance and redemption belong to the completed store milestone, replacing the exchange-only/no-store-credit default for that milestone. SO-09 and applicable OQ-45 decisions must close before activation; no cash refund or no-bill-return entitlement follows. |
| Trading-store counts | OQ-57 is answered: freeze sales for the affected store, confirm all tills are synced and stopped online before count start, and reopen after closure or cancellation. |
| Reports | Both targets apply: 95/100 standard requests within three seconds on the approved reference dataset and visible one-store/one-month load within five seconds on supported hardware and connection. Reporting must not delay billing. |
| Opening stock | OQ-54 is answered in the main PRD's season rule. Real-tenant opening approval remains blocked by OQ-29 migration sources and reconciliation. |
| Permissions | The main PRD requires one six-role permission system with site/brand scope and step rules. A separate section-permission or goods-grant bridge in current code is an SO-03 implementation gap, not approved policy. OQ-28 still governs the full matrix. |

The listed golden tax cases still require CA approval. Source implementation labels and superseded examples are not evidence of completed behaviour.
