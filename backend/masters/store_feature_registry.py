"""Every store-operations feature that can be switched per store (ST-OPS-6).

Registering a feature is one entry in ``FEATURES`` below. Each ticket of the
store operations PRD adds its own entry and nothing else here:

    StoreFeature(
        key="split-sale",                 # stable, never renamed
        name="Split sale between two salespeople",
        gate="CA sign-off",               # an OPEN gate, or None
        has_modes=False,                  # True: manual or connected provider
        default_on=False,                 # B3: off unless the ticket says so (B4)
    ),

``gate`` names a gate that is still open. While it is listed here the feature
cannot be switched on for a real store (a store whose tenant is not synthetic,
baseline B1), and the server treats it as off there whatever was stored. Closing
a gate is Anand's decision and a separate change (B2): nothing in this module or
behind any screen closes one.

Gate the requests of a feature with ``masters.store_features.require_feature``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

#: Ticket 05's key, spelled once: the dataset reads it for the till.
ONLINE_ONLY_REFUSALS = "online-only-refusals"
MANAGER_PIN_OVERRIDES = "manager-pin-overrides"
STAFF_LIST = "staff-list"
SPLIT_SALE = "split-sale"
CUSTOMER_DISPLAY = "customer-display"
SALES_REPORT = "sales-report"
GST_AFTER_DISCOUNT = "gst-after-discount"
HSN_ON_EVERY_ITEM = "hsn-on-every-item"
EXCHANGE_RETURN_TAX = "exchange-return-tax"
CUSTOMER_CONSENT = "customer-consent"
CUSTOMER_RIGHTS = "customer-rights"
CUSTOMER_MERGE_RETENTION = "customer-merge-retention"
SAVED_SIZES = "saved-sizes"
BRAND_TERMS = "brand-terms"
OFFER_SIMULATION = "offer-simulation"
OFFER_RETURN = "offer-return"
SEASON_AGEING = "season-ageing"
BROKEN_SIZE = "broken-size"
THREE_WAY_MATCH = "three-way-match"
GST_REPORTS = "gst-reports"
GIFT_STOCK_ITC = "gift-stock-itc"
DISCOUNT_FUNDING_SPLIT = "discount-funding-split"
SCHEDULED_COUNTS = "scheduled-counts"
DEBIT_NOTE_DRAFT = "debit-note-draft"
CASH_COUNT = "cash-count"
SHRINKAGE_REPORT = "shrinkage-report"
INVENTORY_REPORT = "inventory-report"
MARGIN_SHARE = "margin-share"
PETTY_CASH = "petty-cash"
SIZE_BALANCING = "size-balancing"
TRANSFER_DOCUMENTS = "transfer-documents"
BRAND_DISCOUNT_CLAIMS = "brand-discount-claims"
CUSTOMER_RESERVATION = "customer-reservation"
SOR_AGEING = "sor-ageing"
BRAND_PAYABLES = "brand-payables"
OPEN_TO_BUY = "open-to-buy"
SIZE_CURVE = "size-curve"
EXCEPTIONS_REPORT = "exceptions-report"
BRAND_PERFORMANCE_REPORT = "brand-performance-report"
STAFF_REPORT = "staff-performance-report"
STORE_CHECKLISTS = "store-checklists"
BRAND_REPORTS = "brand-reports"
#: Ticket 20's gate (§32): the forfeiture wording waits for the CA.
RESERVATION_GATE = "CA sign-off of the reservation forfeiture wording"
SPECIAL_ORDERS = "special-orders"
#: Ticket 21: it forfeits an advance by ticket 20's policy under wording of its
#: own, so it waits for the same CA sign-off (B168, Anand to confirm).
SPECIAL_ORDER_GATE = "CA sign-off of the special order forfeiture wording"
ALTERATIONS = "alterations"
GIFT_VOUCHERS = "gift-vouchers"
#: Ticket 22's gate: custody waits on OQ-49 and the 5% line on the CA (§32).
ALTERATIONS_GATE = "OQ-49 for custody, and CA sign-off"


@dataclass(frozen=True)
class StoreFeature:
    key: str
    name: str
    #: The open gate this feature waits for, by name ("CA sign-off", "OQ-08").
    gate: str | None = None
    #: Whether the feature runs in manual mode or through a connected provider.
    has_modes: bool = False
    #: What a store gets before Admin changes anything (B3: off; B4: ticket 05 on).
    default_on: bool = False
    description: str = ""


#: The registered features, in the order the Feature Switches page lists them.
FEATURES: tuple[StoreFeature, ...] = (
    # Ticket 02 (ST-OPS-3): Setup > Audit Log. No gate. Off until Admin turns it on (B3).
    StoreFeature(
        key="audit-log",
        name="Audit log",
        description="Setup, Audit Log: a read-only list of every recorded change at this store.",
    ),
    # Ticket 03 (§6, ST-CMP-1, ST-CMP-3): bills taxed by the saved per-HSN
    # settings. Off, a store keeps version 1 - the slab table - exactly as
    # before (B5). Its CA sign-off gate was closed on 1 October 2026, when KDPS
    # approved the per-HSN rates for the first-store pilot.
    StoreFeature(
        key="tax-settings",
        name="Versioned tax settings",
        description=(
            "Bills here are taxed by the saved tax settings for each HSN. "
            "Off, the store keeps the tax slab table (version 1)."
        ),
    ),
    # Ticket 04 (ST-CMP-5): the store's own 3-letter prefix and the new number
    # series. No gate. Off, the store keeps today's bill number (B3); on, bills
    # dated on or after the start date (a 1 April, B8) take the new format.
    StoreFeature(
        key="document-series",
        name="Store prefix and document series",
        description=(
            "Bills from the start date (a 1 April) are numbered XXX/26-27/n with this "
            "site's prefix, and an offline till bills from a block of those numbers. "
            "Off, bills keep today's number."
        ),
    ),
    # Ticket 05 (ST-POS-5, ST-CMP-4 D8, section 28): what an offline till must not
    # do. No gate, and on for every store from the start (B4) - it only refuses
    # risky offline actions. Off, the till behaves exactly as it did before.
    StoreFeature(
        key=ONLINE_ONLY_REFUSALS,
        name="Online-only refusals at the till",
        default_on=True,
        description=(
            "While offline, the till refuses a credit note as payment, a bill with the "
            "buyer's GSTIN and an exchange against such a bill, with one clear message. "
            "Off, the till allows them offline as before."
        ),
    ),
    # Ticket 06 (section 4, Q7; ST-MNY-2; ST-RPT-6): every counter override
    # needs a manager's own PIN, from somebody the till could hold a PIN for,
    # never the cashier themselves, and each one is recorded in the audit log.
    # No gate. Off (B3), the till and the server behave exactly as before.
    StoreFeature(
        key=MANAGER_PIN_OVERRIDES,
        name="Manager's own PIN on every override",
        description=(
            "An override at the till needs the PIN of a manager of this store who is "
            "not the cashier, and each one is recorded with the manager, cashier, till, "
            "time and what was approved. Off, overrides work as before."
        ),
    ),
    StoreFeature(
        key=STAFF_LIST,
        name="Staff list per store",
        description=(
            "HRMS, Staff List: who is assigned to this store, who is active, and who "
            "sells at the till. The till's salesperson picker reads the staff list at "
            "every store whether this is on or off; off only hides this screen."
        ),
    ),
    # Ticket 08 (ST-POS-2, B8, B14): a sale line shared between two salespeople
    # by whole percentages. No gate. Off (B3), the till offers no split; a split
    # bill made before the switch went off is kept and flagged, never refused.
    StoreFeature(
        key=SPLIT_SALE,
        name="Split sale between two salespeople",
        description=(
            "At the till, a line can be shared between two salespeople by percentage "
            "(each 1% to 99%, adding to 100%). Reports and incentives credit each "
            "person their share. Off, the till offers no split."
        ),
    ),
    # Ticket 09 (ST-POS-6): a second browser window at the till that shows the
    # customer their bill as it is built. No gate. Off (B3), the till offers no
    # "Customer display" button and the display window refuses to open.
    StoreFeature(
        key=CUSTOMER_DISPLAY,
        name="Customer display",
        description=(
            "At the till, staff open a second window facing the customer. It shows the "
            "lines, offers, savings, total, the UPI amount and a thank-you, and never "
            "cost, PIN prompts or staff messages. Off, the till offers no customer display."
        ),
    ),
    # Ticket 10 (ST-RPT-1): Reports, Sales, read from the reporting store kept
    # apart from billing. No gate (conversion, which waits on OQ-15, is simply
    # not shown). Off (B3), the report is hidden and refused at that store.
    StoreFeature(
        key=SALES_REPORT,
        name="Sales report",
        description=(
            "Reports, Sales: bills, pieces, value, average selling price, average bill, "
            "units per bill, discount and target achievement, by day, store, brand, "
            "category, salesperson and tender, with an Excel export. It reads a copy of "
            "the bills kept apart from billing, so it never slows a bill."
        ),
    ),
    # Ticket 11 (§6 ST-CMP-1): tax on each piece's price after discount, worked
    # out the same way on the till and the server. Gated on the CA's sign-off,
    # so never on at a real store (B1, B2). Off (B3), bills are priced and taxed
    # exactly as before (B5).
    StoreFeature(
        key=GST_AFTER_DISCOUNT,
        name="GST after discount",
        gate="CA sign-off",
        description=(
            "Each piece is taxed on its own price after discount. A buy 2 get 1 price is "
            "spread over the pieces by MRP, a bank instant discount is recorded as a "
            "'bank offer' payment that does not lower the taxable value, and the till "
            "and the server must agree to the paisa (a difference flags the bill). "
            "Off, bills are priced as before."
        ),
    ),
    StoreFeature(
        key=HSN_ON_EVERY_ITEM,
        name="HSN on every item",
        # CA sign-off gate closed with the per-HSN rates on 1 October 2026.
        description=(
            "A PT for this site cannot be approved while any line has no HSN; the "
            "refusal names the lines. Stock, Items with no HSN lists the pieces here "
            "that still have none, for fixing. The till never stops a bill for a "
            'missing HSN: the line takes the "no rule" rate and the bill is flagged. '
            "Off, PT approval and the till work as before."
        ),
    ),
    # Ticket 13 (§6 ST-CMP-2): a returned piece reversed at its own bill's rate
    # and value, a credit note printed beside the new invoice, returns kept within
    # the issuing GSTIN and the credit-note deadline respected. Gated on the CA's
    # sign-off, so never on at a real store (B1, B2). Off, returns and exchanges
    # work exactly as before (B5).
    StoreFeature(
        key=EXCHANGE_RETURN_TAX,
        name="Exchange and return tax",
        gate="CA sign-off",
        description=(
            "A piece coming back is reversed at the rate and value on its own bill, and an "
            "exchange prints a credit note and the new tax invoice together on one slip. A "
            "bill from another GSTIN is refused, saying where it can be returned. After the "
            "credit-note deadline the value is given with no tax reduction, and flagged. A "
            "bank-offer piece credits only what the customer paid. Off, returns and "
            "exchanges work as before."
        ),
    ),
    # Ticket 15 (§6 ST-CMP-6): the customer answers "send my bill" and "send me
    # offers" themselves on the customer display, with an under-18 check first.
    # No gate. Off (B3), the till asks nothing and records nothing; the phone
    # number stays optional either way.
    StoreFeature(
        key=CUSTOMER_CONSENT,
        name="Customer consent",
        description=(
            "Once a mobile number is on the bill, the customer answers two separate "
            'questions on the customer display: "send my bill" and "send me offers". Both '
            "start off. Before offers they are asked whether they are under 18; if yes, "
            "offers stay off. Staff can withdraw either answer at the counter but can never "
            "say yes for the customer. Each answer keeps its time, till, staff member and "
            "wording version, and answers given offline sync later. Off, nothing is asked."
        ),
    ),
    # Ticket 16 (ST-CUS-1, §5.1): the Customers menu - the customer list, each
    # customer's page, and the rights screen where staff act on the customer's
    # request (show, correct, withdraw, erase). No gate. Off (B3), the menu is
    # hidden and the server refuses it; nothing held is changed by the switch.
    StoreFeature(
        key=CUSTOMER_RIGHTS,
        name="Customer page and rights",
        description=(
            "Customers menu: find a customer, see their name, number, GSTIN, both "
            "consents and the bills they bought on. At the customer's request staff "
            "show what is held, correct it, withdraw a consent or erase it; bills stay. "
            "Each action is recorded. Needs a connection. Off, the menu is hidden."
        ),
    ),
    # Ticket 17 (ST-CUS-1 merge and retention, §23): on the customer page, merge
    # two records for one person and move a record to a new number, both with the
    # customer present; and the nightly clean-up of customers with no purchase for
    # the retention period. One switch, gated on the period Anand has only
    # proposed, so none of it runs at a real store until he confirms it (B12).
    StoreFeature(
        key=CUSTOMER_MERGE_RETENTION,
        name="Customer merge, new number and retention",
        gate="Customer data retention: 3 years with no purchase, still proposed (§23)",
        description=(
            "On the customer page, with the customer present: merge two records for "
            "one person, keeping both histories, or move a record to a new number. Each "
            "night, a customer with no purchase for the retention period (a setting, "
            "36 months) has their profile removed; bills are never touched. Needs the "
            "customer page on too. Off, neither is offered and nobody is removed."
        ),
    ),
    # Ticket 18 (ST-CUS-2): the customer's usual size per brand and category,
    # learned from their bills and shown on the till. No gate. Off (B3), nothing is
    # learned or shown and the server refuses it; what was learned stays.
    StoreFeature(
        key=SAVED_SIZES,
        name="Saved size per brand",
        description=(
            "The last size a customer bought per brand and category is learned from "
            "their bills and shown on the till when their number is on the bill. Staff "
            "can correct it with the customer's agreement; each change is recorded. "
            "Reading and correcting need a connection. Erased with the customer's profile."
        ),
    ),
    # Ticket 23 (ST-BRD-1, ST-BRD-6): Brands, Terms - each brand's dated terms per
    # season and its promotion-services setting, proposed by the Brand Manager and
    # approved by the Owner. No gate: recording terms decides nothing yet, and
    # Anand must fill them in before P1 goes live (D9). The gated effects (claims
    # flagged for a promotion agreement, SOR effects under OQ-26) are the later
    # tickets'. Off (B3), the Brands menu is hidden and changes are refused.
    StoreFeature(
        key=BRAND_TERMS,
        name="Brand terms",
        description=(
            "Brands, Terms: each brand's commercial model, margin, return allowance, "
            "discount-funding share and payment days per season, from a date, and its "
            "promotion-services agreement (default No). The Brand Manager proposes, the "
            "Owner approves. A brand with no approved model shows as unknown. Off, the "
            "Brands menu is hidden and no change can be proposed."
        ),
    ),
    # Ticket 30 (ST-OFR-3, §16): before approval, a draft offer is priced against
    # real past bills to estimate what it would cost. It reads a copy of the
    # bills kept apart from billing (§17). No gate. Off (B3), the estimate is
    # hidden and refused for this store's bills.
    StoreFeature(
        key=OFFER_SIMULATION,
        name="Offer simulation",
        description=(
            "Offers: before a draft offer is approved, it is priced against this store's "
            "real bills from the last 4 weeks and the same 4 weeks last year, showing the "
            "estimated discount, pieces affected and margin effect, labelled as an estimate. "
            "It reads a copy of the bills kept apart from billing, so it never slows a bill. "
            "Off, this store's bills are left out of every estimate."
        ),
    ),
    # Ticket 31 (ST-OFR-1): what an offer earned and cost once it has run, against
    # a baseline period the viewer states. No gate. Off (B3), this store's bills
    # are left out, and an offer with no store switched on shows nothing.
    StoreFeature(
        key=OFFER_RETURN,
        name="Return on each offer",
        description=(
            "Offers: once an offer has run, its page shows the sales, pieces, discount given, "
            "gross margin and brand-funded part of the goods it covers at this store, against "
            "a baseline period the viewer chooses. Margin and the brand-funded part are shown "
            "to head office only. It reads copies of the bills kept apart from billing, so it "
            "never slows a bill. Off, this store's bills are left out."
        ),
    ),
    # Ticket 33 (ST-INV-2): stock aged against its season, with an alert per store.
    # No gate. Off (B3), the page is hidden and refused at that store and the
    # daily check raises no ageing alert for it.
    StoreFeature(
        key=SEASON_AGEING,
        name="Season-aware stock ageing",
        description=(
            "Stock, Stock Ageing: each piece here with the days since it first arrived "
            "in the company and the days at this store. In-season stock is flagged after "
            "90 days with no sale; stock whose season has ended counts as aged from the "
            "day it ended; stock of the unknown historical season is its own group. Both "
            "kinds of ageing show in Alerts. Off, there is no page and no ageing alert."
        ),
    ),
    # Ticket 32 (§11 ST-INV-1): a style-colour missing too many of its category's
    # core sizes while it still has stock. No gate. Off (B3): no page, and the
    # daily check raises no broken-size alert for the store and closes any open.
    StoreFeature(
        key=BROKEN_SIZE,
        name="Broken-size alerts",
        description=(
            "Stock, Broken Sizes: a style-colour here missing 40% or more of its "
            "category's core sizes while it still has stock is raised in Alerts. The core "
            "sizes and the share are set per category. Each alert records when it was "
            "acted on and when it closed, for the 7-day measure. Off, there is no page "
            "and no broken-size alert."
        ),
    ),
    # Ticket 37 (§10 ST-REC-1): at receiving, each line compares what was booked,
    # invoiced and counted. No gate. Off (B3), receiving works exactly as before:
    # no comparison, no cost on the invoice, no exception.
    StoreFeature(
        key=THREE_WAY_MATCH,
        name="Three-way match at receiving",
        description=(
            "Each received line shows the booked, invoiced and counted quantity and cost "
            "as match, short, excess or cost differs, within 0 pieces and Rs 1 a line. A "
            "mismatch opens an exception for the buyer and never stops counting. Store "
            "roles see quantities only. Off, receiving works as before."
        ),
    ),
    # Ticket 47 (ST-RPT-5): Reports, GST, read from the reporting store kept
    # apart from billing, for Accounts to file. No gate. Off (B3), the report is
    # hidden and refused at that store.
    StoreFeature(
        key=GST_REPORTS,
        name="GST reports",
        description=(
            "Reports, GST: outward supplies by rate and HSN with B2B and B2C apart, "
            "B2B invoices with their IRN status, credit notes, and the documents issued "
            "and cancelled in each series for GSTR-1 Table 13 (unused offline numbers "
            "included), with an Excel export. Only Accounts and Owner read it. It is "
            "prepared for filing, not a filing."
        ),
    ),
    # Ticket 14 (ST-CMP-7): a piece given away free as a different item is tagged
    # as a gift when its bill is accepted, and Reports, Gift Stock lists the input
    # tax credit to reverse on it. Gated (CA sign-off): treating a gift-with-purchase
    # as a gift is a baseline. Off (B3), nothing is tagged and the report is refused.
    StoreFeature(
        key=GIFT_STOCK_ITC,
        name="Gift stock and input tax credit",
        gate="CA sign-off",
        description=(
            "A piece given away here with no payment, as a different item (a gift with "
            "purchase), is tagged as a gift when its bill is accepted; buy 2 get 1 pieces "
            "are part of the sale and are not. Reports, Gift Stock lists the gift pieces "
            "each month with the input tax credit to reverse, per GSTIN, with an Excel "
            "export. Only Accounts and Owner see cost and credit. Off, nothing is tagged."
        ),
    ),
    # Ticket 25 (ST-OFR-2, §16): every discounted line records the brand's share
    # and KDPS's share of its discount, from the offer and the brand terms in force
    # on the bill date, worked out on the server when the bill arrives (offline
    # bills when they sync). No gate. Off (B3), no share is recorded for this
    # store's bills and the report is hidden and refused here.
    StoreFeature(
        key=DISCOUNT_FUNDING_SPLIT,
        name="Discount funding split",
        description=(
            "Every discounted line at this store records how much of its discount the "
            "brand funds and how much KDPS funds, from the offer and the brand terms in "
            "force on the bill date. Offline bills get theirs when they reach head office. "
            "Where the brand's model or share is unknown, the split says unknown. Reports, "
            "Discount Funding shows it per offer, brand and store, to Accounts and Owner "
            "only. Off, nothing is recorded for this store's bills."
        ),
    ),
    # Ticket 35 (ST-INV-3): the Owner sets when each store counts, the count due
    # today is a task on Today, and a missed one is owned work. Off (B3), the
    # schedule is hidden and refused and nothing is raised at that store. Gated
    # (B81): a real store's blind count cannot run yet - goods-v1 counts run only
    # at a site declared non-trading (trading-site counts are goods stage 2) - so
    # every period would be raised as missed. Demo stores can switch it on.
    StoreFeature(
        key=SCHEDULED_COUNTS,
        name="Scheduled counts",
        gate="Counts at trading stores (goods stage 2)",
        description=(
            "Stock Count, Count Schedule: the Owner sets how often each brand (or the "
            "whole store) is counted. The count due today shows on Today and opens the "
            "store's blind count. A count not done by its day raises an exception and a "
            "count-due alert. Off, the schedule is hidden and nothing is raised."
        ),
    ),
    # Ticket 38 (ST-REC-3): an approved receiving shortage drafts a debit note
    # to the vendor. No gate: drafting and issuing post nothing; posting through
    # the accounting export is a separate step that waits for OQ-47 and is not
    # built. Off (B3), no note is drafted at that site and nothing new is done to
    # its notes; the notes already there stay readable.
    StoreFeature(
        key=DEBIT_NOTE_DRAFT,
        name="Debit note draft for shortages",
        description=(
            "When a receiving shortage is accepted (invoiced but never received), a debit "
            "note to the vendor is drafted at the invoice's cost plus tax, linked to the GRN "
            "and the vendor's invoice. Accounts reviews it, the Owner approves it in the "
            "approvals inbox, and Accounts issues it with head office's DN number. It is not "
            "posted to the accounts: that waits for OQ-47."
        ),
    ),
    # Ticket 41 (ST-MNY-2, R-POS-011, R-FIN-015): the day-close cash count at
    # the till, by note and coin, against the expected cash; a difference is an
    # owned exception confirmed with a manager's own PIN, never a balancing
    # entry. Deposits and handovers name both sides. Gated on OQ-08 (cash
    # custodians), so never on at a real store (B1, B2). Off (B3), nothing is
    # counted or recorded; counts already saved stay readable.
    StoreFeature(
        key=CASH_COUNT,
        name="Cash count by note",
        gate="OQ-08",
        description=(
            "Sell, Cash Count: at day close the cashier counts the drawer by note and coin "
            "(Rs 500, 200, 100, 50, 20, 10 and coins) against the expected cash: opening + "
            "cash sales - cash refunds - deposits and handovers - petty cash spent. A "
            "difference needs a manager's own PIN and opens a cash-variance exception and "
            "alert; it never books a balancing entry. A deposit or handover records who gave "
            "the cash and who received it. Off, nothing is counted or recorded."
        ),
    ),
    # Ticket 44 (ST-INV-4): pieces and cost lost to shrinkage as a share of sales.
    # No gate. Off until Admin turns it on (B3).
    StoreFeature(
        key=SHRINKAGE_REPORT,
        name="Shrinkage report",
        description=(
            "Reports, Shrinkage: pieces and cost lost to approved shrinkage adjustments "
            "and write-offs typed as shrinkage, by store, brand, category and month, as "
            "a share of sales, with an Excel export. Store roles see pieces, never cost."
        ),
    ),
    # Ticket 43 (ST-RPT-2): sell-through, weeks of cover, GMROI and dead stock.
    # No gate. Off until Admin turns it on (B3).
    StoreFeature(
        key=INVENTORY_REPORT,
        name="Inventory report",
        description=(
            "Reports, Inventory: sell-through, weeks of cover, GMROI (outright brands "
            "only) and in-season dead stock, by store, brand, category and season, with "
            "an Excel export. Store roles see pieces, never cost, margin or GMROI."
        ),
    ),
    # Ticket 27 (ST-BRD-4, §15): each sale of an SOR or concession brand records
    # its split between the brand and KDPS, from the brand terms in force on the
    # bill date, worked out on the server when the bill arrives. No gate: every
    # figure is labelled an estimate until OQ-50 is decided, and nothing is posted
    # or owed by it (SOR payables wait on OQ-26, ticket 28). Off (B3), nothing is
    # recorded for this store's bills and the statement leaves the store out.
    StoreFeature(
        key=MARGIN_SHARE,
        name="Margin share statement",
        description=(
            "Every sale of an SOR or concession brand at this store records its split "
            "between the brand and KDPS, from the brand's margin % in the terms in force "
            "on the bill date. Offline bills get theirs when they reach head office. A "
            "brand whose model or margin is unknown is listed, not split. Brands, Margin "
            "Share gives a monthly statement per brand, labelled an estimate, to Accounts "
            "and Owner only. Off, nothing is recorded for this store's bills."
        ),
    ),
    # Ticket 42 (ST-MNY-3, R-FIN-014, R-FIN-015): each store's petty cash box,
    # its named custodian, top-ups from the till or head office, and spends with
    # an expense head and a bill photo. A spend over the limit waits for the
    # Owner in the approvals inbox. Gated on OQ-03 (the expense-head catalogue)
    # and OQ-08 (cash custodians), so never on at a real store (B1, B2). Off
    # (B3), nothing new is recorded; what is saved stays readable.
    StoreFeature(
        key=PETTY_CASH,
        name="Petty cash",
        gate="OQ-03 and OQ-08",
        description=(
            "Money, Petty Cash: the store's petty cash box is kept at a set float by a named "
            "custodian. It is topped up from the till or from head office. Each spend records "
            "an expense head, the amount and a photo of the bill; a spend with no photo opens "
            "an exception. A spend over Rs 2,000 waits for the Owner in the approvals inbox. "
            "Money spent comes off the expected cash at the day-close count. Off, nothing new "
            "is recorded."
        ),
    ),
    # Ticket 34 (ST-TRF-1): transfers suggested to fill a store's broken sizes from
    # another store's surplus, for a person to approve. No gate. Off until Admin
    # turns it on (B3). Both stores need it on; the store being helped also needs
    # broken-size alerts (ticket 32), since those are what it fills.
    StoreFeature(
        key=SIZE_BALANCING,
        name="Size-balancing suggestions",
        description=(
            "Stock, Size Balancing: when a broken-size alert here lacks a size that "
            "another store holds beyond 8 weeks of its sales, a transfer is suggested, "
            "if it moves at least 3 pieces or Rs 3,000 at MRP. A person approves it, and "
            "it becomes an ordinary transfer request, or rejects it with a reason. Off, "
            "nothing is suggested to or from this store."
        ),
    ),
    # Ticket 36 (ST-TRF-2): each dispatch carries its document - a delivery
    # challan within one GSTIN, a tax invoice between two - numbered in the
    # sending site's series. The sending site's switch decides. No gate (the
    # PRD lists none). Off (B3), a shipment leaves exactly as before, with no
    # document; documents already issued stay readable.
    StoreFeature(
        key=TRANSFER_DOCUMENTS,
        name="Transfer documents",
        description=(
            "Transfers, Dispatch: each shipment this site sends carries a delivery challan "
            "(XXX/DC/2627/n) when the other site has the same GSTIN, or a tax invoice "
            "(XXX/26-27/n) from this GSTIN to the other one when it does not, printed with "
            "the dispatch. Off, shipments leave with no document from the system."
        ),
    ),
    # Ticket 26 (ST-BRD-3, ST-BRD-6): at month end the brand-funded share of the
    # discounts (ticket 25) becomes a claim per brand and store, settled through
    # the brand's commercial credit note (no GST effect on our bills). Gated on
    # the CA's sign-off of the promotion-services flag (section 32), so never on
    # at a real store (B1, B2). Off (B3), no claim is raised or moved on here;
    # claims already raised stay readable.
    StoreFeature(
        key=BRAND_DISCOUNT_CLAIMS,
        name="Brand discount claims",
        gate="CA sign-off",
        description=(
            "Brands, Claims: at month end the brand-funded share of this store's "
            "discounts becomes a claim on each brand. Accounts records the brand "
            "accepting it and the brand's commercial credit note that settles it, or "
            "settles it short with the difference kept. No GST effect on our bills. A "
            "brand with a promotion-services agreement has every claim flagged for "
            "Accounts, with an alert. Off, no claim is raised or moved on here."
        ),
    ),
    # Ticket 20 (ST-ORD-1): staff reserve specific pieces for a named customer,
    # with an optional advance and an RV receipt voucher. Gated (§32): the
    # forfeiture wording waits for the CA, so it can be on only at a demo store.
    # Off (B3), no new reservation can be made and Customer Orders is hidden;
    # reservations already made are still collected, cancelled and expired.
    StoreFeature(
        key=CUSTOMER_RESERVATION,
        name="Customer reservations",
        gate=RESERVATION_GATE,
        description=(
            "Customer Orders, Reservations: staff hold specific pieces for a named customer "
            "for 7 days (3 while an end-of-season sale is running), with an optional advance "
            "in cash, card or UPI and a receipt voucher (RV series, no GST). The pieces leave "
            "the counter until collected. On pickup the advance pays towards the bill; on "
            "expiry or cancellation the pieces come back and the advance is refunded or kept "
            "by policy. Online only."
        ),
    ),
    # Ticket 21 (ST-ORD-2): for an item no store has, staff record what the
    # customer wants, with an optional advance and an RV receipt voucher, and
    # track it from asked to collected. Gated (B168): its forfeiture wording
    # waits for the CA, as ticket 20's does. Off (B3), no new special order
    # is taken and its menu line is hidden; orders already taken still move on,
    # are collected, cancelled and refunded.
    StoreFeature(
        key=SPECIAL_ORDERS,
        name="Special orders",
        gate=SPECIAL_ORDER_GATE,
        description=(
            "Customer Orders, Special orders: for an item no store has, staff record the "
            "customer, brand, style, size and colour, with an optional advance in cash, card "
            "or UPI and a receipt voucher (RV series, no GST). The order becomes a transfer "
            "request or a booking line and is tracked through asked, ordered, arrived, "
            "customer told and collected. On collection the advance pays towards the bill; "
            "on cancellation it is refunded or kept by the reservation policy. Online only."
        ),
    ),
    # Ticket 22 (ST-ORD-3): a job card per alteration, linked to the garment's bill
    # line, and a paid alteration as its own bill line at 5% (SAC 9988). Gated on
    # OQ-49 (billed-retained custody) and the CA's sign-off of the 5% line, so it
    # can be on only at a demo store. Off (B3), the till offers no alteration
    # charge and no new job card can be made; open job cards still move on.
    StoreFeature(
        key=ALTERATIONS,
        name="Alterations",
        gate=ALTERATIONS_GATE,
        description=(
            "Customer Orders, Alterations: a job card for each alteration, linked to the "
            "garment's bill line, with what to alter, measurements, the tailor (in-house or "
            "outside), the promised date, any charge and the status. A paid alteration is its "
            "own bill line at the till at 5% under SAC 9988; a free one has no charge and no "
            "tax. The job card records when the customer was told it is ready, and the store "
            "holds the garment in billed-retained custody until handover. Job cards are "
            "online only; the charge line bills offline like any line."
        ),
    ),
    # Ticket 24 (ST-BRD-5, §15): SOR stock is flagged at 5 months from the brand's
    # dispatch date, a month before the brand must invoice it. No gate (the PRD
    # lists none). Off (B3): receiving asks for no dispatch date, the page is
    # hidden and refused, and the daily check raises no SOR alert for the site.
    StoreFeature(
        key=SOR_AGEING,
        name="SOR ageing alert",
        description=(
            "Brands, SOR Ageing: receiving records the brand's dispatch date, and each "
            "piece of a brand whose terms say SOR is flagged 5 months after it, a month "
            "before the brand must invoice it (6 months). Settle it by selling it, returning "
            "it to the brand, or recording the brand's invoice. Pieces with no dispatch date, "
            "and pieces of brands whose model is unknown, are listed, never guessed. Both "
            "show in Alerts."
        ),
    ),
    # Ticket 28 (ST-MNY-4, R-FIN-010): per outright brand or vendor, what is owed,
    # paid and due and how old it is, from the vendor invoices and payments
    # Accounts records. No gate: the PRD's gate (OQ-26) is for SOR, which is not
    # here. Off (B3), nothing new is recorded or paid for this store; what is
    # recorded stays readable.
    StoreFeature(
        key=BRAND_PAYABLES,
        name="Brand payables (outright)",
        description=(
            "Money, Payables: Accounts records each outright brand's vendor invoices "
            "received at this store, with their total including tax, and the payments "
            "made against them. Per brand or vendor it shows what is owed, paid and "
            "due, and how long past due. The due date comes from the payment days in "
            "the brand's terms. SOR and consignment are not included yet (P4). Brands "
            "with no recorded model are listed apart. Owner and Accounts only. Off, "
            "nothing new is recorded or paid here."
        ),
    ),
    # Ticket 39 (ST-BUY-1, R-BUY-005): a buying budget at cost per brand, season
    # and site (or the whole company). A booking that would go over what is left
    # is confirmed only after the Owner approves it in the approvals inbox. No
    # gate (the PRD lists none). Off (B3), bookings to this site are confirmed
    # exactly as before; budgets already set stay readable.
    StoreFeature(
        key=OPEN_TO_BUY,
        name="Open-to-buy",
        description=(
            "Booking, Open-to-Buy: a buying budget at cost for each brand and season, at this "
            "site or across the company. Open-to-buy is the budget less open bookings and the "
            "goods received against them. A booking to this site that goes over it needs the "
            "Owner's approval in the approvals inbox before it is confirmed. Off, bookings are "
            "confirmed as before."
        ),
    ),
    # Ticket 40 (ST-BUY-2): a booking line entered as a style total gets its sizes
    # filled from the store's size curve, learnt from its sales in the same season
    # last year. No gate. Off (B3), no curve is offered and none is filled at that
    # store; bookings are made as before.
    StoreFeature(
        key=SIZE_CURVE,
        name="Size curve for bookings",
        description=(
            "On a new booking, a line entered as a style total can have its sizes filled "
            "from this store's size curve: how the brand's category sold by size here in "
            "the same season last year (SS from last SS, AW from last AW). The buyer can "
            "change every size. Where there is no history, no curve is offered and the "
            "screen says so. Online only."
        ),
    ),
    # Ticket 48 (ST-RPT-6, B10): the counter's exceptions per store and staff
    # member, and the caps on returns without a bill. No gate. Off until Admin
    # turns it on (B3); off, no cap is judged and the report is refused.
    StoreFeature(
        key=EXCEPTIONS_REPORT,
        name="Exceptions report and no-bill return caps",
        description=(
            "Reports, Exceptions: cancelled bills, manual discounts, manager PIN uses by "
            "manager, cash variances, bill-number holes, returns without a bill and late "
            "syncs, per store and staff member, with an Excel export. Returns without a bill "
            "are capped at 2 per phone number and 5 per staff member a month: going over "
            "flags the bill and raises an alert, and never stops it."
        ),
    ),
    # Ticket 45 (ST-RPT-3, R-FIN-023): per brand and store, how it sells, what
    # it earns and how old its stock is. No gate, as ticket 27 (B111): margin,
    # commission and GMROI are labelled estimates until OQ-50 is decided. Off
    # until Admin turns it on (B3).
    StoreFeature(
        key=BRAND_PERFORMANCE_REPORT,
        name="Brand performance report",
        description=(
            "Reports, Brand Performance: per brand and store, sales, discount depth, "
            "returns, sell-through and stock age, and for Owner and Accounts the margin "
            "(outright) or commission (SOR and concession) and GMROI (outright only), "
            "labelled estimates until OQ-50 is decided. With an Excel export. Store roles "
            "never see margin, commission or GMROI."
        ),
    ),
    # Ticket 46 (ST-RPT-4, R-HR-004): each salesperson's results from split shares,
    # and their monthly targets. No gate. Off until Admin turns it on (B3); off,
    # the report is refused and no target is set there, and targets already set
    # are kept.
    StoreFeature(
        key=STAFF_REPORT,
        name="Staff performance report",
        description=(
            "Reports, Staff Performance: each salesperson's sales, bills, units per bill, "
            "average bill value, returns against their sales and target achievement, using "
            "split shares, with an Excel export. A person sees their own results; the manager "
            "sees the team. The manager sets each person's monthly target."
        ),
    ),
    # Ticket 49 (ST-OPS-4): Admin's checklist templates, each store's list for
    # the day on Today, and the items missed. No gate. Off until Admin turns it
    # on (B3); off, the store gets no list, nothing is judged missed, and no new
    # template is set where it is off at every store.
    StoreFeature(
        key=STORE_CHECKLISTS,
        name="Store task checklists",
        description=(
            "Admin sets checklist templates (for example opening, closing, a weekly display "
            "check and the monthly count) under Setup, Task Checklists. Each store gets the day's "
            "list on Today; staff tick each item, with a photo if they want. An item not ticked "
            "by its time is missed: it shows on Today, in the exceptions report and as a "
            "checklist-missed alert. The site-readiness checklist is separate and unchanged."
        ),
    ),
    # Ticket 19 (ST-POS-4): gift vouchers sold and spent at the till, online only.
    # No gate (the PRD lists none). Off (B3), the till neither sells nor takes a
    # voucher here; vouchers already sold still expire and stay listed.
    StoreFeature(
        key=GIFT_VOUCHERS,
        name="Gift vouchers",
        description=(
            "The till sells gift vouchers as their own document (GV series) with a value, "
            "paid in cash, card or UPI, usable for 12 months, and takes them as payment in "
            "parts, the balance staying on the voucher. No GST when sold: GST is charged on "
            "the goods bought with it, and none on a value that expires unused (Circular "
            "243/37/2024). Usable at any store under the same GSTIN that has this switch on. "
            "The Gift vouchers screen lists them, expired ones too. Online only."
        ),
    ),
    # Ticket 29 (ST-BRD-2): each brand's Sale and SOH reports in its own saved
    # layout, made monthly and on demand. No gate. Off until Admin turns it on (B3);
    # off, no report is made for the store, monthly or on demand.
    StoreFeature(
        key=BRAND_REPORTS,
        name="Brand reports (Sale and SOH in each brand's format)",
        description=(
            "Brands, Reports: each brand's Sale and SOH report for this store, laid out in "
            "the brand's saved layout (columns, order, headers, formats, file type) and "
            "made as .xlsx or .csv. The KDPS Sale and SOH sheets are the first layouts. "
            "Made on demand, and for last month by itself once the month has ended. "
            "Accounts saves the layouts."
        ),
    ),
)


#: Demo-only features for the browser suite (``KDPS_STORE_FEATURE_DEMO_PROBES``).
#: They do nothing but answer the probe endpoint, so switching can be proved end
#: to end before a real feature exists.
DEMO_PROBES: tuple[StoreFeature, ...] = (
    StoreFeature(
        key="demo-probe",
        name="Demo probe (test only)",
        has_modes=True,
        description="Shows the Feature check line. Test data only.",
    ),
    StoreFeature(
        key="demo-gated-probe",
        name="Demo gated probe (test only)",
        gate="Demo gate (test only)",
        description="Waits on a gate that stays open. Test data only.",
    ),
)


def registered_features() -> tuple[StoreFeature, ...]:
    """Every feature a store can switch, demo probes included where enabled."""
    probes = DEMO_PROBES if getattr(settings, "KDPS_STORE_FEATURE_DEMO_PROBES", False) else ()
    features = FEATURES + probes
    keys = [feature.key for feature in features]
    if len(keys) != len(set(keys)):  # pragma: no cover - programmer error
        raise ValueError(f"duplicate store feature keys in {keys}")
    return features


def open_gates() -> list[str]:
    """Every gate a registered feature still waits for, by name."""
    return sorted({f.gate for f in registered_features() if f.gate})
