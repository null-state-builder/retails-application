"""The Sale and everything a bill drags behind it (D10, the till's documents).

The sale is the busiest money document in the system and the only one whose
number is assigned by the *writer* rather than by the server: the till bills
offline, so the bill is printed and in the customer's hand before the server ever
hears about it. `Sale.mint_number()` is where that lands - it accepts the number
the till brought instead of allocating one, and the kernel
(`VoucherSeries.accept_external`) decides whether it may be used.

Two shapes here are worth reading before the rest:

* **A posted document is immutable, in the database, by trigger.** That is why
  the credit note stores its face value and nothing else: a balance that goes
  down cannot live on a posted document, so `remaining_paise` is derived from
  the redemption rows appended against it. The document is the fact; the
  redemptions are the ledger of what happened to it.
* **Flags are rows, not columns.** Everything the business can absorb - a hole in
  the numbering, an unrecognised credit note, a bill whose tax disagrees with
  today's slab - becomes a `ContinuityFlag` on the store's queue and the bill
  still lands (Rule 8, flag don't block).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db import models
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.base import TimeStampedModel
from core.documents import DocStatus, Document, DocumentEditError, MintedNumber, VoucherSeries
from core.money import MoneyField
from masters.models import Gstin

#: Doc types this app has minted. `SAL` is till-assigned (see the kernel's
#: `EXTERNAL_NUMBER_DOC_TYPES`); `CRN` and `SRT` were server-allocated. The
#: latter two stay because their historical models and series remain readable.
SALE_DOC_TYPE = "SAL"
CREDIT_NOTE_DOC_TYPE = "CRN"
RETURN_DOC_TYPE = "SRT"


class SellPolicy(TimeStampedModel):
    """The shop floor's money dials, as data rather than as constants (Rule 12).

    One row, because each dial is one decision head office makes for the chain and
    none of them varies by store today. It is read on every accept, so it is
    deliberately tiny.

    ``manual_discount_cap_percent`` is the absolute limit on the part of a line's
    discount that the rulebook did not produce. Head office changes the dial; the
    counter never routes around it through a manager PIN.

    ``credit_note_validity_days`` is how long a note issued here stays spendable.
    Six months is the Indian norm and the grill recorded it as data, not a rule.

    ``uncosted_aging_days`` is how long a line sold before its paperwork may sit in
    the costing queue before somebody is told about it (#186). The queue drains
    itself when the PT lands, so the dial is not a deadline - it is the point past
    which "the paperwork is on its way" stops being a fair description. Three days
    is a starting number, not a ruling: nothing in the corpus fixes one, and it is
    a dial precisely so head office can move it without a deploy (Rule 12).

    ``return_window_days`` is how long after a bill a piece may be brought back
    without a manager saying so explicitly (#184). Grill Q7 puts the window in
    data rather than in code, and past it the return is not refused - it takes the
    manager's *second* answer, the window override, and lands flagged. Thirty days
    is a starting number for the same reason the one above is: nothing in the
    corpus fixes a customer-facing window (the 60-120 days in the domain facts are
    the *brand's* return terms, which are a different clock entirely), and head
    office moves this one without a release.

    ``return_review_count`` is how many pieces one seller may take back in a day
    before the daily check says so (#188). Returns are the best-documented theft
    channel at any counter, which is why grill Q7 asks for them to be counted per
    employee - but counting is not accusing, and the flag it raises is a row on a
    list, never a refusal. Five is a starting number in exactly the sense the
    ageing dial's three days is: nothing in the corpus fixes one, and a store with
    a genuinely returns-heavy Saturday moves it rather than learning to ignore the
    list.
    """

    SINGLETON_PK = 1

    manual_discount_cap_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("10.00"),
        help_text="Maximum manual discount, as a percent of MRP.",
    )
    manual_discount_on_offer_lines = models.BooleanField(
        default=False,
        help_text="Whether a rulebook-discounted line may also carry a manual discount.",
    )
    credit_note_validity_days = models.IntegerField(
        default=180,
        help_text="How many days a credit note issued at the counter stays spendable.",
    )
    uncosted_aging_days = models.IntegerField(
        default=3,
        help_text="How many days a sold-before-inward line may wait to be costed "
        "before the store's exception list carries it.",
    )
    return_window_days = models.IntegerField(
        default=30,
        help_text="How many days after a bill a piece comes back without a manager "
        "having to override the window.",
    )
    return_review_count = models.IntegerField(
        default=5,
        help_text="How many pieces one seller may take back in a day before the "
        "daily check puts their name on the store's exception list.",
    )
    cached_bill_days = models.IntegerField(
        default=30,
        help_text="How many days of this store's own bills the till caches, so an "
        "exchange can be taken offline against one (OPS-09, PRD §10.3).",
    )

    class Meta:
        db_table = "sell_policy"
        verbose_name_plural = "sell policies"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="ck_sellpolicy_singleton"),
            models.CheckConstraint(
                condition=models.Q(manual_discount_cap_percent__gte=0)
                & models.Q(manual_discount_cap_percent__lte=100),
                name="ck_sellpolicy_cap_is_a_percent",
            ),
            models.CheckConstraint(
                condition=models.Q(credit_note_validity_days__gt=0),
                name="ck_sellpolicy_validity_is_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(uncosted_aging_days__gte=0),
                name="ck_sellpolicy_aging_is_not_negative",
            ),
            # Nought is a legitimate setting - "every return needs the window
            # override" - so the floor is nought and not one.
            models.CheckConstraint(
                condition=models.Q(return_window_days__gte=0),
                name="ck_sellpolicy_window_is_not_negative",
            ),
            # Nought would mean "flag every seller who took anything back", which
            # is a list nobody reads and therefore no control at all.
            models.CheckConstraint(
                condition=models.Q(return_review_count__gt=0),
                name="ck_sellpolicy_return_review_is_positive",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"cap {self.manual_discount_cap_percent}% · "
            f"manual on offers {'on' if self.manual_discount_on_offer_lines else 'off'} · "
            f"credit notes valid {self.credit_note_validity_days}d · "
            f"uncosted flagged after {self.uncosted_aging_days}d · "
            f"returns inside {self.return_window_days}d · "
            f"returns reviewed above {self.return_review_count} a seller a day"
        )

    def as_till_policy(self) -> dict[str, str | bool]:
        """The small, exact policy shape both policy readers publish."""
        return {
            "manual_discount_cap_percent": f"{self.manual_discount_cap_percent:.2f}",
            "manual_discount_on_offer_lines": self.manual_discount_on_offer_lines,
        }

    @classmethod
    def current(cls) -> SellPolicy:
        """The live dials, creating the row at its defaults if it is somehow gone.

        Fail-safe rather than fail-closed on purpose: the defaults are the ruled
        chain policy, with manual-on-offer disabled.
        """
        row, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return row


class SalespersonMatch(models.Model):
    """One row of the old salesperson table, kept after that table went (ticket 07, §29).

    The till's salesperson list is the staff list now. The old table's rows could
    not simply go with it: every bill sold before the move names one of them, and
    history is never rewritten. So each old row is frozen here - its id, store,
    code and name exactly as they were - with the staff record it was matched to.

    A match is made only by the rule in `services.salesperson_move`, which cannot
    guess (``manage.py match_salespeople``), or by Admin on the Salesperson Matches
    screen - each one a command audited with the values before and after. A row
    with no staff record yet is "unmatched" and waits for Admin. Past sale lines point at
    this row and never change; only the staff link here is filled in.

    The primary key *is* the old row's id, because a till that queued a bill
    before the move still sends that number (`accept`), and it must land on the
    same person.
    """

    class Rule(models.TextChoices):
        UNMATCHED = "", "Not matched yet"
        CODE = "code", "Same code at this store"
        NAME = "name", "Same name at this store"
        ADMIN = "admin", "Chosen by Admin"

    id = models.IntegerField(primary_key=True)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    code = models.CharField(max_length=16)
    name = models.CharField(max_length=120)
    was_active = models.BooleanField(default=True)
    staff = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    rule = models.CharField(max_length=8, choices=Rule.choices, blank=True, default="")
    matched_at = models.DateTimeField(null=True, blank=True)
    matched_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.IntegerField(default=1)

    class Meta:
        db_table = "sell_salesperson_match"
        ordering = ["store_id", "code", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(staff__isnull=True, rule="")
                    | (models.Q(staff__isnull=False) & ~models.Q(rule=""))
                ),
                name="ck_salespersonmatch_rule_iff_staff",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class CreditNote(Document):
    """What the counter hands back instead of cash (grill Q7).

    Face value only. `remaining_paise` and `status` are **derived**, not stored,
    and that is a consequence of the kernel rather than a preference: a submitted
    document may not be UPDATEd - the FSM trigger refuses every column change but
    the move to cancelled - so a balance that falls as the note is spent cannot
    live here. It lives where it actually happens, in the redemption rows, and
    the note reads its own balance off them. The same argument that makes ledgers
    append-only makes this the right shape anyway.

    Same-store redemption only in v1 (grill Q4): the note names its issuing store
    and the accept pipeline refuses one from anywhere else.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SPENT = "spent", "Spent"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="credit_notes_issued"
    )
    fy = models.CharField(max_length=7)
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_mobile = models.CharField(max_length=15, blank=True, default="")
    value_paise = MoneyField(help_text="Face value at issue, in integer paise. Never changes.")
    expires_on = models.DateField(
        help_text="Validity is data (grill Q7); set from SellPolicy at issue."
    )
    source_sale = models.ForeignKey(
        "sell.Sale",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_notes_issued",
        help_text="The bill that issued it - an exchange whose returns exceeded its sales.",
    )
    source_return = models.ForeignKey(
        "sell.Return",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_notes_issued",
        help_text="The plain return that issued it. Exactly one of the two sources "
        "is ever set - a note is handed over either at the end of a bill or at the "
        "end of a return, and never both.",
    )
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(Document.Meta):
        db_table = "sell_credit_note"
        ordering = ["-created_at"]
        constraints = [
            *Document.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(value_paise__gt=0), name="ck_creditnote_value_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.doc_number or f"CreditNote(draft #{self.pk})"

    @property
    def doc_type(self) -> str:
        return CREDIT_NOTE_DOC_TYPE

    def series_lookup(self) -> tuple[str, str, str]:
        return self.fy, self.store.code, CREDIT_NOTE_DOC_TYPE

    @property
    def spent_paise(self) -> int:
        """What has been spent off this note, on bills that still stand.

        A redemption against a bill that has since been cancelled did not happen:
        the cancel mirrored that bill's `Dr CREDIT_NOTE_LIABILITY` leg, so the
        books have already handed the money back to the note, and counting the
        redemption as well would show a note as spent while the ledger carried the
        liability for it. Same rule, same reason, as the returned-quantity ceiling
        in `sell.services.refunds`.
        """
        return int(
            self.redemptions.exclude(sale__docstatus=DocStatus.CANCELLED).aggregate(
                total=models.Sum("amount_paise")
            )["total"]
            or 0
        )

    @property
    def remaining_paise(self) -> int:
        return int(self.value_paise or 0) - self.spent_paise

    #: What `with_balance` annotates the balance as. Not `remaining_paise`, because
    #: Django refuses to annotate over a property (there is no setter), and not a
    #: name each caller invents, because then the balance would be spelled twice.
    BALANCE = "remaining"

    @classmethod
    def with_balance(cls, rows: models.QuerySet[CreditNote]) -> models.QuerySet[CreditNote]:
        """`rows` with each note's balance worked out in SQL rather than per note.

        The same subtraction `remaining_paise` does, and the only other place it is
        spelled: a *list* of notes (the till's dataset asks for every one this store
        issued) must not pay a query per row for its balance, and the fix for that
        must not be a second copy of the arithmetic somewhere else.

        Read it off `note.remaining` (`CreditNote.BALANCE`) and hand it to
        `status_at`, rather than reading `note.status`, which would go back to the
        database for the balance the row already carries.
        """
        return rows.annotate(
            **{
                cls.BALANCE: models.F("value_paise")
                - Coalesce(
                    models.Sum(
                        "redemptions__amount_paise",
                        # The cancelled-bill rule from `spent_paise`, spelled the
                        # only other place the balance is worked out. A note whose
                        # only spend was on a cancelled bill is open again, and
                        # both readers have to say so or the list and the row
                        # disagree.
                        filter=~models.Q(redemptions__sale__docstatus=DocStatus.CANCELLED),
                    ),
                    models.Value(0),
                )
            }
        )

    @property
    def status(self) -> str:
        return self.status_at(self.remaining_paise, timezone.localdate())

    def status_at(self, remaining_paise: int, today: date) -> str:
        """`status`, told its two inputs instead of fetching them.

        The rule lives here and only here, but a *list* of notes must not pay a
        query per note for its balance (the till's dataset asks for every open one
        this store issued). So the caller that already has the balance in hand -
        annotated by `with_balance` - and the day it is asking about hands both in,
        and gets the same answer the property gives.
        """
        if self.docstatus == DocStatus.CANCELLED:
            return self.Status.CANCELLED
        if remaining_paise <= 0:
            return self.Status.SPENT
        if self.expires_on < today:
            return self.Status.EXPIRED
        return self.Status.OPEN


class Sale(Document):
    """One bill. The till assigns its number; the server accepts it exactly once.

    `(store, fy, till_seq)` is the till's key and is unique here as well as in the
    rendered `doc_number`, so a second writer on one series is refused by the
    database rather than by a check somebody could forget to run.
    """

    class Origin(models.TextChoices):
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"
        PAPER = "paper", "Re-entered from a paper bill"

    class B2bTaxKind(models.TextChoices):
        NONE = "none", "Not a B2B bill"
        CGST_SGST = "cgst_sgst", "CGST + SGST (same state)"
        IGST = "igst", "IGST (different state)"

    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="sales")
    fy = models.CharField(max_length=7)
    till_seq = models.IntegerField(help_text="The number the till assigned, before syncing.")
    origin = models.CharField(max_length=8, choices=Origin.choices, default=Origin.OFFLINE)
    billed_at = models.DateTimeField(help_text="The till's clock at Save & Print.")
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_mobile = models.CharField(max_length=15, blank=True, default="", db_index=True)
    buyer_gstin = models.CharField(
        max_length=15, blank=True, default="", help_text="Present = a B2B tax invoice."
    )
    b2b_tax_kind = models.CharField(
        max_length=10, choices=B2bTaxKind.choices, default=B2bTaxKind.NONE
    )
    gross_paise = MoneyField(default=0)
    discount_paise = MoneyField(default=0)
    net_paise = MoneyField(
        default=0,
        help_text="What the customer pays. May be negative - an exchange whose "
        "returns outweigh its sales issues a credit note for the difference.",
    )
    gst_paise = MoneyField(default=0)
    round_paise = MoneyField(default=0)
    #: What the customer physically handed over in notes, as the counter recorded
    #: it. Presentation only, and deliberately not a tender: what posts to CASH is
    #: what the bill *took*, and the difference is change out of the drawer. The
    #: reprint needs it, because a receipt that re-derived the change from the
    #: cash tender would print no change line on a sale that gave change.
    #:
    #: Null is the blank box, which means the customer handed over exactly the
    #: cash tender - and it is also every bill printed before this field existed.
    cash_received_paise = MoneyField(null=True, blank=True, default=None)
    exchange_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="exchanges"
    )
    # The manager's tap at the counter (#182). It sits on the bill because the
    # remaining bill-level exception is not a line: an unrecognised credit note is
    # a *tender*, and the lines it helped pay for are ordinary lines. A daily check
    # reading "somebody took an unknown note
    # here" with no name against it would be looking at the one place the
    # counter's second eye was supposed to leave a mark.
    override_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_authorised",
        help_text="The manager who authorised whatever on this bill needed it.",
    )
    override_kind = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="What was authorised - one of `sell.serializers.OVERRIDE_KINDS`: "
        "credit_note when a manager accepted a note the till could not verify.",
    )
    override_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The till's clock when the manager's PIN was accepted, which is "
        "not the same moment as Save & Print.",
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="sales_billed"
    )
    #: How the bill reads on the shop floor: `{StoreCode}-{CounterID}-{Seq}`
    #: (R-POS-005, OPS-09). The device owns it, so it arrives with the bill and is
    #: checked rather than minted here.
    #:
    #: It sits **beside** `doc_number` and does not replace it. `doc_number` is the
    #: Tally join key, whose rendered form freezes the moment a store's external
    #: series starts counting (`core.documents.VoucherSeries`) - re-rendering it
    #: with a counter id would turn one bill into two keys. A store with no
    #: registered till leaves this blank and reads by `doc_number`, as it always has.
    till_number = models.CharField(max_length=40, blank=True, default="", db_index=True)
    #: A fingerprint of the payload this bill was accepted from, so a second upload
    #: under the same key can be told apart from a different bill wearing it
    #: (PRD §10.5). Blank on every bill accepted before OPS-09, which is read as
    #: "we cannot tell" and replays as it always did.
    payload_fingerprint = models.CharField(max_length=64, blank=True, default="")
    #: The tax settings version this bill was taxed under, as the till reported it
    #: (store operations ticket 03, R-POS-013). Version 1 is the slab table every
    #: bill before versioning used, so existing rows are 1. Written once, at
    #: acceptance, and never changed; a later version affects only newer bills.
    tax_setting_version = models.PositiveIntegerField(default=1)
    #: The till priced this bill with GST after discount (store operations
    #: ticket 11, ST-CMP-1): offers resolved that way, bank offers as payments.
    #: The server judges the bill by what it says here, never by the store's
    #: switch today; a bill saying otherwise than the switch is flagged. False on
    #: every bill before the ticket and wherever the switch is off (B5).
    gst_after_discount = models.BooleanField(default=False)
    #: The tax invoice number in the new series, ``XXX/26-27/n`` (store operations
    #: ticket 04, ST-CMP-5), taken by the till from a number block the server
    #: issued. Empty on every bill made before the new format starts, or where the
    #: store's switch is off: those read by ``doc_number``, as they always have.
    #: Beside ``doc_number`` rather than instead of it, for the reason
    #: ``till_number`` is: ``doc_number`` is the Tally join key and its rendered
    #: form is frozen once the store's series has started counting.
    tax_invoice_number = models.CharField(max_length=16, null=True, blank=True, unique=True)
    #: The till took the pieces coming back on this bill by ticket 13's rules
    #: (store operations §6 ST-CMP-2): reversed at their own bill's rate and
    #: value, no tax reduction after the credit-note deadline, the bank's part
    #: of a bank-offer piece reversed against the bank (B60), and a credit note
    #: issued beside the new invoice. The server judges the bill by what it says
    #: here and flags a bill that differs from the store's switch. False on every
    #: bill before the ticket and wherever the switch is off (B5).
    return_tax = models.BooleanField(default=False)

    class Meta(Document.Meta):
        db_table = "sell_sale"
        ordering = ["-billed_at", "-id"]
        constraints = [
            *Document.Meta.constraints,
            models.UniqueConstraint(
                fields=["store", "fy", "till_seq"], name="uq_sale_store_fy_seq"
            ),
            models.CheckConstraint(
                condition=models.Q(till_seq__gt=0), name="ck_sale_till_seq_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["store", "billed_at"], name="sale_store_billed_idx"),
            models.Index(fields=["store", "origin"], name="sale_store_origin_idx"),
        ]

    def __str__(self) -> str:
        return self.doc_number or f"Sale(draft #{self.pk})"

    @property
    def doc_type(self) -> str:
        return SALE_DOC_TYPE

    @property
    def gstin(self) -> Gstin:
        """The registration the bill was raised under (the store's).

        A property rather than a column: a store belongs to exactly one GSTIN and
        the store is already on the bill, so storing it again would be a second
        copy of one fact. It exists because `post_entries` snapshots
        `doc.store`/`doc.gstin` onto every leg, and Bihar and Jharkhand are
        separate registered persons - a value leg that could not say which one it
        belonged to would be no use to either return.
        """
        return self.store.gstin

    def series_lookup(self) -> tuple[str, str, str]:
        return self.fy, self.store.code, SALE_DOC_TYPE

    def mint_number(self) -> MintedNumber:
        """Take the number the till already printed, rather than allocating one.

        The kernel decides whether it may be used and reports what it jumped over;
        the accept pipeline reads that off `post()` and raises the hole flag.
        """
        fy, store_code, doc_type = self.series_lookup()
        accepted = VoucherSeries.accept_external(
            fy=fy, store_code=store_code, doc_type=doc_type, seq=self.till_seq
        )
        return MintedNumber(
            series=accepted.series, doc_number=accepted.doc_number, accepted=accepted
        )


class SaleLine(TimeStampedModel):
    """One line of a bill, with the piece described as it was at billing (Rule 3).

    `direction` carries the sign - `qty` is always positive. A `return` line is
    the exchange leg: a piece coming back inside the same bill, priced at what the
    customer actually paid for it on the original (D2), never at today's price.
    """

    class Direction(models.TextChoices):
        SALE = "sale", "Sold"
        RETURN = "return", "Returned (exchange leg)"

    class Kind(models.TextChoices):
        #: A piece of stock, the only kind before ticket 22.
        GOODS = "goods", "Goods"
        #: Store operations ticket 22 (ST-ORD-3): a paid alteration - a service,
        #: no stock, its own line at 5% under SAC 9988 (baseline, CA to confirm).
        ALTERATION = "alteration", "Alteration charge"

    class Condition(models.TextChoices):
        GOOD = "good", "Good - back on the shelf"
        DAMAGED = "damaged", "Damaged - into quarantine"

    class CostingStatus(models.TextChoices):
        POSTED = "posted", "Costed"
        DEFERRED = "deferred", "Waiting on the paperwork"

    class CostBook(models.TextChoices):
        """Which book this piece's cost came out of, snapshotted at billing.

        Not derived on demand, and that is the point. An exchange unwinds a
        posting that already happened, so it has to unwind the one that actually
        happened - and a brand's commercial model is editable master data. A brand
        flipped from SOR to Outright between the sale and the exchange would
        otherwise reverse a brand-owned piece into our own inventory and strand
        what the brand is still owed. The trial balance would stay at zero and the
        subledger tie would still pass, which is exactly why this is a column and
        not a lookup (Rule 3).

        Blank means the books could not place the piece at all: nothing posted, and
        the line waits in `DeferredCosting`.
        """

        OWN = "own", "KDPS-owned - out of our own inventory"
        BRAND = "brand", "Brand-owned - against what we owe the brand"

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="lines")
    line_no = models.IntegerField()
    direction = models.CharField(max_length=8, choices=Direction.choices, default=Direction.SALE)
    #: Goods, or (ticket 22) an alteration charge: a service with no piece behind
    #: it, so no stock, cost, offer or salesperson split touches it.
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.GOODS)
    barcode = models.CharField(max_length=64, db_index=True)
    season = models.CharField(max_length=120, blank=True, default="")
    # The seven merchandising dims, snapshotted at billing.
    design = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    brand = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    hsn = models.CharField(max_length=24, blank=True, default="")
    qty = models.IntegerField()
    mrp_paise = MoneyField(default=0)
    disc_paise = MoneyField(default=0)
    net_paise = MoneyField(
        default=0, help_text="GST-inclusive line value; on a return leg, the refund."
    )
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    gst_paise = MoneyField(default=0)
    #: Ticket 48 (ST-RPT-6): the part of a sold line's discount that the server's
    #: own rulebook does not explain - the manual discount, as the cap check at
    #: acceptance worked it out, never the till's `offer_evidence`. Null on a
    #: return leg and on lines written before the ticket.
    manual_disc_paise = MoneyField(null=True, blank=True)
    #: Ticket 12 (§6 principle 2, R-POS-013): the tax settings version this line
    #: was taxed under and the rule of it that applied, frozen with the line. A
    #: sold line takes the version its bill recorded; an exchange leg keeps its
    #: original line's. Null on lines written before the ticket (see the bill's).
    tax_setting_version = models.PositiveIntegerField(null=True, blank=True)
    #: ``slab`` (version 1), ``price_line``, ``flat_rate`` (the rate schedule),
    #: ``no_rule`` (the version's "no rule" rate, flagged) or ``unknown`` (a
    #: version this server does not hold). Blank before the ticket.
    tax_rule_kind = models.CharField(max_length=12, blank=True, default="")
    #: The HSN prefix of the rule that applied; blank for the slab table, no rule,
    #: or a rule covering every HSN.
    tax_rule_hsn_prefix = models.CharField(max_length=8, blank=True, default="")
    #: Ticket 13 (B60), a return leg only: the part of this leg's value the bank
    #: offer paid on the original bill. The customer is credited the value less
    #: this; the bank's part is reversed against the bank offer receivable. Nought
    #: on every other line.
    bank_offer_paise = MoneyField(default=0)
    unit_cost_paise = MoneyField(
        default=0,
        help_text="Cost of record from the cohort, frozen at billing. Zero only on a "
        "line the books cannot price yet (sold before inward).",
    )
    #: Who sold this line: a staff record active at the store (ticket 07). Every
    #: line sold since the till moved to the staff list carries it.
    salesperson = models.ForeignKey(
        "accounts.Staff", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: A line sold before the move, or queued by a till that had not heard of it,
    #: names a row of the old salesperson table instead - kept as it was.
    salesperson_match = models.ForeignKey(
        SalespersonMatch, null=True, blank=True, on_delete=models.PROTECT, related_name="lines"
    )
    #: The seller's code and name as the bill was made, frozen with the line
    #: (overall PRD §8.4): a later rename changes the staff list, not this bill.
    salesperson_code = models.CharField(max_length=40, blank=True, default="")
    salesperson_name = models.CharField(max_length=160, blank=True, default="")
    offer = models.ForeignKey(
        "offers.Offer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sale_lines",
        help_text="The brand-layer rule that won this line. An add-on that stacked "
        "on top is in the evidence, not here: this is 'which offer was this sold "
        "under', which has exactly one answer.",
    )
    offer_evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text="Which rule won, what it beat, and by how much (B3). Kept beside "
        "the FK rather than replaced by it: the rule can be ended and replaced, and "
        "what this bill was priced under has to stay readable afterwards (Rule 3).",
    )
    manual_desc = models.CharField(
        max_length=200, blank=True, default="", help_text="A line the scan could not resolve."
    )
    sold_before_inward = models.BooleanField(default=False)
    costing_status = models.CharField(
        max_length=8, choices=CostingStatus.choices, default=CostingStatus.POSTED
    )
    cost_book = models.CharField(max_length=8, choices=CostBook.choices, blank=True, default="")
    cost_vendor = models.ForeignKey(
        "vendors.Vendor",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sale_lines_accrued",
        help_text="Who the brand-owned piece is settled with, frozen at billing. "
        "PROTECT because a return reverses the accrual against this row.",
    )
    return_reason = models.CharField(max_length=40, blank=True, default="")
    condition = models.CharField(max_length=8, choices=Condition.choices, blank=True, default="")
    original_line = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="returned_by",
        help_text="The line on the original bill this exchange leg gives back.",
    )
    override_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sale_lines_overridden",
        help_text="The manager whose OK let this line's discount past the cap (H3).",
    )
    goods_allocations = models.JSONField(
        default=list,
        blank=True,
        help_text="Which goods pieces this line actually consumed, at a goods-v1 store: "
        "one entry per receipt lot portion, with the origin it was valued under and the "
        "cost frozen on that origin. Written once, at billing, and never recomputed - the "
        "bill keeps its own origin outcome (overall PRD R-POS-009), and an exchange "
        "against this line gives back these exact pieces at these exact costs. Empty at a "
        "legacy store, where the cohort carries the cost instead.",
    )

    class Meta:
        db_table = "sell_sale_line"
        ordering = ["sale_id", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["sale", "line_no"], name="uq_saleline_sale_line_no"),
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_saleline_qty_positive"),
            # A sold piece always has a name against it (Rule 10); a returned one
            # is credited to whoever sold it originally, not to whoever took it back.
            # The name is a staff record, or - before the move - an old row.
            models.CheckConstraint(
                condition=~models.Q(direction="sale")
                | models.Q(salesperson__isnull=False)
                | models.Q(salesperson_match__isnull=False),
                name="ck_saleline_sale_has_salesperson",
            ),
            models.CheckConstraint(
                condition=models.Q(salesperson__isnull=True)
                | models.Q(salesperson_match__isnull=True),
                name="ck_saleline_one_salesperson_source",
            ),
            models.CheckConstraint(
                condition=models.Q(bank_offer_paise=0)
                | (
                    models.Q(direction="return")
                    & models.Q(bank_offer_paise__gt=0)
                    & models.Q(bank_offer_paise__lte=models.F("net_paise"))
                ),
                name="ck_saleline_bank_offer_on_return_leg",
            ),
            # Ticket 22: an alteration charge is one sold service at its own code,
            # never discounted and never given back as a piece.
            models.CheckConstraint(
                condition=models.Q(kind="goods")
                | models.Q(kind="alteration", direction="sale", qty=1, disc_paise=0, hsn="9988"),
                name="ck_saleline_alteration_shape",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.sale_id}/{self.line_no} · {self.barcode} × {self.qty}"

    @property
    def is_return(self) -> bool:
        return self.direction == self.Direction.RETURN

    @property
    def is_alteration(self) -> bool:
        return self.kind == self.Kind.ALTERATION

    @property
    def value_paise(self) -> int:
        """What this line is worth on the bill - the price paid, or the refund.

        Historical standalone-return lines use the same word, so old posting
        records stay comparable across both tables. `direction` already carries
        the sign, so this is a magnitude on both.
        """
        return int(self.net_paise or 0)

    @property
    def cost_paise(self) -> int:
        """What this line's pieces cost the books, at the rate frozen on it."""
        return int(self.unit_cost_paise or 0) * int(self.qty)


class SaleLineShare(TimeStampedModel):
    """One salesperson's share of a line split between two (ticket 08, ST-POS-2).

    Only a split line has these rows; every other line is its own salesperson's
    whole value, read off `SaleLine.salesperson`. A split sold line has two rows,
    position 1 being the line's own salesperson. A return leg against a split
    line has two rows too, giving back each person's part of the refund in the
    same proportion (`sell.services.split_shares`), so a person's net is their
    sale shares less their return shares - read directly, never re-derived.

    `value_paise` is a magnitude, like `SaleLine.net_paise`: the line's
    `direction` carries the sign. The person's code and name are frozen as the
    bill was made (overall PRD §8.4).
    """

    line = models.ForeignKey(SaleLine, on_delete=models.CASCADE, related_name="shares")
    position = models.PositiveSmallIntegerField()
    salesperson = models.ForeignKey("accounts.Staff", on_delete=models.PROTECT, related_name="+")
    salesperson_code = models.CharField(max_length=40, blank=True, default="")
    salesperson_name = models.CharField(max_length=160, blank=True, default="")
    percent = models.PositiveSmallIntegerField()
    value_paise = MoneyField(default=0)

    class Meta:
        db_table = "sell_sale_line_share"
        ordering = ["line_id", "position"]
        constraints = [
            models.UniqueConstraint(fields=["line", "position"], name="uq_saleshare_line_position"),
            models.UniqueConstraint(
                fields=["line", "salesperson"], name="uq_saleshare_line_salesperson"
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=2),
                name="ck_saleshare_position",
            ),
            models.CheckConstraint(
                condition=models.Q(percent__gte=1, percent__lte=99),
                name="ck_saleshare_percent",
            ),
            models.CheckConstraint(
                condition=models.Q(value_paise__gte=0), name="ck_saleshare_value_positive"
            ),
        ]
        indexes = [models.Index(fields=["salesperson"], name="saleshare_salesperson_idx")]

    def __str__(self) -> str:
        return f"{self.line_id}#{self.position} · {self.percent}%"


class SaleLineFunding(TimeStampedModel):
    """Who pays one part of a line's discount: the brand and KDPS (ticket 25, ST-OFR-2).

    One row per part: each offer the line's discount came from (the winning brand
    offer and any add-on stacked on it), plus the rest of the discount that no
    offer gave (a manual discount, KDPS's own). Worked out on the server when the
    bill arrives - an offline bill when it syncs - from the offer's funder and the
    brand terms in force on the bill date (`sell.services.discount_funding`), and
    never worked out again: a later change to terms or to an offer never alters it.

    A return leg against a funded line has rows too, giving back each part in the
    same proportion, so a line returned whole nets to nothing. `discount_paise`,
    `brand_paise` and `kdps_paise` are magnitudes; the line's `direction` carries
    the sign. Unknown is both shares null with the reason in `unknown_reason`,
    never a guess (D9).
    """

    class Funder(models.TextChoices):
        BRAND = "brand", "The brand, by its terms"
        KDPS = "kdps", "KDPS"

    class Unknown(models.TextChoices):
        MODEL = "model_unknown", "The brand has no approved terms for this season"
        SHARE = "share_unknown", "The brand's terms leave its discount share unknown"
        BRAND = "brand_unknown", "The line's brand is not one brand in the brand list"
        SEASON = "season_unknown", "The line's season is not in the season list"
        OFFER = "offer_unknown", "The offer the bill names is not on the books"
        PARTS = "parts_disagree", "The bill's offers add up to more than the line's discount"
        UNVERIFIED = (
            "offer_unverified",
            "The till's offer savings are more than the offers give on the server's reading",
        )

    line = models.ForeignKey(SaleLine, on_delete=models.CASCADE, related_name="funding")
    #: 1, 2, 3 ... within the line: the winning offer, its add-ons, then the rest.
    part = models.PositiveSmallIntegerField()
    #: The offer this part came from; null for the part no offer gave.
    offer = models.ForeignKey(
        "offers.Offer", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Frozen as the bill was made, so a renamed or ended offer still reads.
    offer_name = models.CharField(max_length=160, blank=True, default="")
    #: Who the offer said funds it, frozen; KDPS for the part no offer gave; blank
    #: only where the bill names an offer that is not on the books.
    funder = models.CharField(max_length=8, choices=Funder.choices, blank=True, default="")
    discount_paise = MoneyField(default=0)
    brand_paise = MoneyField(null=True, blank=True)
    kdps_paise = MoneyField(null=True, blank=True)
    unknown_reason = models.CharField(
        max_length=16, choices=Unknown.choices, blank=True, default=""
    )
    #: The brand and season the terms were read for, where the line named ones on
    #: the books; the approved terms version and the brand's share % used.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    season_ref = models.ForeignKey(
        "masters.Season", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    terms_version_id = models.UUIDField(null=True, blank=True)
    funding_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "sell_sale_line_funding"
        ordering = ["line_id", "part"]
        constraints = [
            models.UniqueConstraint(fields=["line", "part"], name="uq_salefunding_line_part"),
            models.CheckConstraint(
                condition=models.Q(part__gte=1), name="ck_salefunding_part_from_one"
            ),
            models.CheckConstraint(
                condition=models.Q(discount_paise__gt=0), name="ck_salefunding_discount_positive"
            ),
            # Known: both shares, adding to the part, and no reason. Unknown: neither
            # share, and a reason. Nothing in between.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        unknown_reason="",
                        brand_paise__isnull=False,
                        kdps_paise__isnull=False,
                        brand_paise__gte=0,
                        kdps_paise__gte=0,
                        discount_paise=models.F("brand_paise") + models.F("kdps_paise"),
                    )
                    | (
                        ~models.Q(unknown_reason="")
                        & models.Q(brand_paise__isnull=True, kdps_paise__isnull=True)
                    )
                ),
                name="ck_salefunding_known_or_unknown",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.line_id}#{self.part} · {self.funder}"


class SaleLineMarginShare(TimeStampedModel):
    """One sold line's split between the brand and KDPS (ticket 27, ST-BRD-4).

    Only lines of an SOR or concession brand are split: KDPS keeps the brand's
    margin % of the line's sale value (after discount, without GST), half up to
    the paisa (B9), and the rest is the brand's. Worked out on the server when the
    bill arrives - an offline bill when it syncs - from the brand terms in force on
    the bill date (`sell.services.margin_share`), and never worked out again: a
    later change to terms never alters it. An outright or consignment line has no
    row. A line whose brand's model or margin is not known has a row with both
    shares null and the reason, so it is listed, never split by a guess (D9).

    A return leg against a line with a row has one too, giving back its part by
    pieces, so a line returned whole nets to nothing. `value_paise`, `kdps_paise`
    and `brand_paise` are magnitudes; the line's `direction` carries the sign.
    Every figure is an estimate until OQ-50 decides the formula.
    """

    class Model(models.TextChoices):
        SOR = "sor", "SOR"
        CONCESSION = "concession", "Concession"

    class Unknown(models.TextChoices):
        MODEL = "model_unknown", "The brand has no approved terms for this season"
        MARGIN = "margin_unknown", "The brand's terms leave its margin unknown"
        BRAND = "brand_unknown", "The line's brand is not one brand in the brand list"
        SEASON = "season_unknown", "The line's season is not in the season list"

    line = models.OneToOneField(SaleLine, on_delete=models.CASCADE, related_name="margin_share")
    #: The model the terms named; blank where it is not known.
    model = models.CharField(max_length=12, choices=Model.choices, blank=True, default="")
    #: What the line sold for after discount, without GST: the amount split.
    value_paise = MoneyField(default=0)
    #: KDPS's margin % from the terms used; null where not known.
    margin_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    kdps_paise = MoneyField(null=True, blank=True)
    brand_paise = MoneyField(null=True, blank=True)
    unknown_reason = models.CharField(
        max_length=16, choices=Unknown.choices, blank=True, default=""
    )
    #: The brand and season the terms were read for, where the line named ones on
    #: the books, and the approved terms version used.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    season_ref = models.ForeignKey(
        "masters.Season", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    terms_version_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "sell_sale_line_margin_share"
        ordering = ["line_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(value_paise__gte=0), name="ck_salemargin_value_not_negative"
            ),
            # Known: a model, a margin, both shares adding to the value, no reason.
            # Unknown: neither share, and a reason. Nothing in between.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        unknown_reason="",
                        model__in=["sor", "concession"],
                        margin_percent__isnull=False,
                        kdps_paise__isnull=False,
                        brand_paise__isnull=False,
                        kdps_paise__gte=0,
                        brand_paise__gte=0,
                        value_paise=models.F("kdps_paise") + models.F("brand_paise"),
                    )
                    | (
                        ~models.Q(unknown_reason="")
                        & models.Q(kdps_paise__isnull=True, brand_paise__isnull=True)
                    )
                ),
                name="ck_salemargin_known_or_unknown",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.line_id} · {self.model or self.unknown_reason}"


class SaleTender(TimeStampedModel):
    """How the bill was paid. The rows sum to `Sale.net_paise` (checked at accept)."""

    class Mode(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        UPI = "upi", "UPI"
        CREDIT_NOTE = "credit_note", "Credit note"
        # Store operations ticket 11 (§6, ST-POS-1): a bank instant discount is a
        # payment the bank makes, not a price cut, so it does not lower the
        # taxable value. No money reaches the drawer (baseline, CA to confirm).
        BANK_OFFER = "bank_offer", "Bank offer"
        # Store operations ticket 20 (ST-ORD-1): a customer reservation's advance,
        # used on its pickup bill - or, ticket 21, a special order's on its
        # collection bill. The money reached the drawer when the advance was
        # taken, so none moves now; the customer advance it was held as is given
        # up instead.
        ADVANCE = "advance", "Advance paid earlier"
        # Store operations ticket 19 (ST-POS-4): a gift voucher, sold earlier as
        # its own document. The money reached the drawer when it was sold; used
        # now, it gives up the voucher liability. GST is on the goods, as ever.
        GIFT_VOUCHER = "gift_voucher", "Gift voucher"

    class UpiState(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        MANUAL = "manual", "Manual"

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="tenders")
    mode = models.CharField(max_length=12, choices=Mode.choices)
    amount_paise = MoneyField()
    credit_note = models.ForeignKey(
        CreditNote,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tenders",
        help_text="Set when the note was recognised. A note the till could not verify "
        "is taken on a manager's OK and flagged, so this stays empty there.",
    )
    #: How the money was proven: `confirmed` (the bank answered, and the
    #: acquirer's reference is stored alongside) or `manual` (the cashier vouched
    #: - QR soundbox, static QR, no internet - and there is nothing trustworthy to
    #: record). Blank on every non-UPI tender. `confirmed` cannot legitimately
    #: reach the server yet - the QR charge card and its mock adapter are #248 -
    #: but the pipeline accepts it correctly for when they land.
    upi_state = models.CharField(max_length=10, choices=UpiState.choices, blank=True, default="")
    #: The acquirer's transaction reference. Only ever set alongside `confirmed`
    #: - a manual entry has nothing trustworthy to record.
    upi_reference = models.CharField(max_length=64, blank=True, default="")
    #: The bank offer this tender is, when it is one and head office knows it
    #: (ticket 11). Empty on every other tender.
    offer = models.ForeignKey(
        "offers.Offer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The customer reservation whose advance this tender is (ticket 20). Only on
    #: that mode, and always on it.
    reservation = models.ForeignKey(
        "sell.CustomerReservation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    #: The special order whose advance this tender is (ticket 21). An advance
    #: tender names exactly one of the two.
    special_order = models.ForeignKey(
        "sell.SpecialOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    #: The gift voucher this tender spends (ticket 19). Only on that mode, and
    #: always on it.
    gift_voucher = models.ForeignKey(
        "sell.GiftVoucher",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tenders",
    )

    class Meta:
        db_table = "sell_sale_tender"
        ordering = ["sale_id", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_saletender_amount_positive"
            ),
            # One direction only: a note may only be named by a credit-note tender.
            # The reverse does not hold - an unverified note is accepted on a
            # manager's OK and has no row to point at.
            models.CheckConstraint(
                condition=models.Q(credit_note__isnull=True) | models.Q(mode="credit_note"),
                name="ck_saletender_note_only_on_note_mode",
            ),
            models.CheckConstraint(
                condition=models.Q(offer__isnull=True) | models.Q(mode="bank_offer"),
                name="ck_saletender_offer_only_on_bank_offer",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(mode="advance")
                    & (
                        models.Q(reservation__isnull=False, special_order__isnull=True)
                        | models.Q(reservation__isnull=True, special_order__isnull=False)
                    )
                )
                | (
                    ~models.Q(mode="advance")
                    & models.Q(reservation__isnull=True, special_order__isnull=True)
                ),
                name="ck_saletender_holder_iff_advance",
            ),
            models.CheckConstraint(
                condition=models.Q(mode="gift_voucher", gift_voucher__isnull=False)
                | (~models.Q(mode="gift_voucher") & models.Q(gift_voucher__isnull=True)),
                name="ck_saletender_voucher_iff_gift_voucher",
            ),
            models.CheckConstraint(
                condition=models.Q(upi_state__in=["", "confirmed", "manual"]),
                name="ck_saletender_upi_state_values",
            ),
            # Every UPI tender is stamped, no other tender is - said as an "iff"
            # rather than two one-way rules so neither half can drift from the
            # other.
            models.CheckConstraint(
                condition=(models.Q(mode="upi") & ~models.Q(upi_state=""))
                | (~models.Q(mode="upi") & models.Q(upi_state="")),
                name="ck_saletender_upi_state_iff_upi",
            ),
            # A reference can only ride a confirmed stamp. The required-when-
            # confirmed half (an empty reference on a confirmed tender is also
            # wrong) is enforced at the API layer, where the human message lives.
            models.CheckConstraint(
                condition=models.Q(upi_reference="") | models.Q(upi_state="confirmed"),
                name="ck_saletender_reference_confirmed_only",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_mode_display()} {self.amount_paise}"


class CreditNoteRedemption(TimeStampedModel):
    """One spend against a credit note. The note's balance is the sum of these."""

    credit_note = models.ForeignKey(
        CreditNote, on_delete=models.PROTECT, related_name="redemptions"
    )
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="credit_note_spends")
    amount_paise = MoneyField()

    class Meta:
        db_table = "sell_credit_note_redemption"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["credit_note", "sale"], name="uq_redemption_note_sale"),
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_redemption_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.credit_note_id} −{self.amount_paise}"


class ExchangeCreditNote(TimeStampedModel):
    """The credit note an exchange issues beside its new tax invoice (ticket 13).

    Store operations PRD §6 ST-CMP-2 (baseline, CA to confirm): an exchange is a
    credit note for the pieces coming back and a new tax invoice for the pieces
    going out, printed together on one slip. The invoice is the bill itself; this
    is the credit note. Its lines are the bill's own return legs, so nothing here
    repeats them: it carries its number, what it reverses and why.

    Not the store-credit ``CreditNote`` above, which is a balance a customer can
    spend and counts in the day's credit notes issued. This one is a tax
    document: its value was settled against the new invoice on the same bill.

    Written once, when the bill is accepted, and never changed (§6 principle 3:
    an issued document is corrected only by another document): nothing in the
    code updates or deletes a row, and the bill it rides on is itself immutable.
    """

    class Series(models.TextChoices):
        #: The new format, ``XXX/CN/2627/n`` (ticket 04), where it applies.
        NEW = "CN", "Credit note series"
        #: Today's credit-note numbering, ``26-27/DEO/CRN/n``.
        TODAY = "CRN", "Today's credit-note numbering"

    sale = models.OneToOneField(Sale, on_delete=models.PROTECT, related_name="exchange_credit_note")
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The bill the pieces were bought on; null for a paper-era original.
    original_sale = models.ForeignKey(
        Sale, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The GSTIN whose tax it reduces (the store's, at issue).
    gstin = models.CharField(max_length=15)
    #: Null only when the number could not be issued; the bill is then flagged.
    number = models.CharField(max_length=128, null=True, blank=True, unique=True)
    series = models.CharField(max_length=3, choices=Series.choices)
    issued_on = models.DateField()
    value_paise = MoneyField(help_text="What the pieces coming back were worth: Σ leg values.")
    gst_paise = MoneyField(default=0, help_text="The tax it reverses; nought when late.")
    bank_offer_paise = MoneyField(default=0, help_text="B60: the bank's part, reversed to it.")
    #: After the credit-note deadline: the value was given, with no tax reduction.
    late = models.BooleanField(default=False)
    #: The deadline the server worked out for the original bill (B10).
    deadline = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "sell_exchange_credit_note"
        ordering = ["-issued_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(value_paise__gt=0), name="ck_exchangecn_value_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(gst_paise__gte=0)
                & models.Q(gst_paise__lte=models.F("value_paise")),
                name="ck_exchangecn_gst_within_value",
            ),
            models.CheckConstraint(
                condition=models.Q(bank_offer_paise__gte=0)
                & models.Q(bank_offer_paise__lte=models.F("value_paise")),
                name="ck_exchangecn_bank_within_value",
            ),
            models.CheckConstraint(
                condition=~models.Q(late=True) | models.Q(gst_paise=0),
                name="ck_exchangecn_late_reduces_no_tax",
            ),
        ]

    def __str__(self) -> str:
        return self.number or f"ExchangeCreditNote(unnumbered #{self.pk})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # An issued document is never edited (overall PRD §8.3): written once.
        if not self._state.adding:
            raise DocumentEditError("an issued credit note is never changed")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise DocumentEditError("an issued credit note is never deleted")

    @property
    def taxable_paise(self) -> int:
        return int(self.value_paise) - int(self.gst_paise)

    @property
    def credit_paise(self) -> int:
        """What the customer was credited: the value less the bank's part."""
        return int(self.value_paise) - int(self.bank_offer_paise)


class GiftPiece(TimeStampedModel):
    """A sold line given away free, as a different item: gift stock (ticket 14).

    Store operations PRD ST-CMP-7 (baseline, CA to confirm): stock given away
    with no payment is tagged as a gift when it leaves stock, so Accounts can
    reverse the input tax credit on it (CGST Act s.17(5)(h)). A buy 2 get 1
    piece is part of the sale and is never tagged.

    Written once, when the bill is accepted at a store whose switch is on, and
    never changed: what the piece cost and the input tax on it are frozen here
    from its receipt layers (baseline B11), or left unknown with the reason why -
    never guessed (overall PRD §8.4).
    """

    class Source(models.TextChoices):
        #: The bill earned a gift offer for this barcode (the offers engine).
        GIFT_OFFER = "gift_offer", "Gift with purchase"
        #: Given with no payment and no gift offer: still stock given away.
        FREE = "free", "Given free, no gift offer"

    class Missing(models.TextChoices):
        NONE = "", "Known"
        #: No receipt layer is recorded for the piece (a store not on goods stock,
        #: or a piece sold before its receipt was posted).
        NO_LAYER = "no_layer", "No receipt layer recorded"
        #: A layer has no price-ticket line, or its line has no input tax rate.
        NO_RATE = "no_rate", "No input tax rate on its receipt"
        #: A layer was received under another GSTIN: the input tax of the transfer
        #: into this one is not held.
        OTHER_GSTIN = "other_gstin", "Received under another GSTIN"

    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="gift_pieces")
    line = models.OneToOneField(SaleLine, on_delete=models.PROTECT, related_name="gift")
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The business day the piece left stock, India time.
    day = models.DateField()
    #: The GSTIN the piece left stock under (its store's, when tagged).
    gstin = models.CharField(max_length=15, blank=True, default="")
    qty = models.PositiveIntegerField()
    source = models.CharField(max_length=12, choices=Source.choices)
    offer = models.ForeignKey(
        "offers.Offer", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: The gift offer's name as it was, kept when the offer is later ended.
    offer_name = models.CharField(max_length=200, blank=True, default="")
    #: What the pieces cost: from their receipt layers, else the line's cost of
    #: record. Null where no cost is known at all.
    cost_paise = MoneyField(null=True, blank=True)
    #: The input tax credit to reverse; null when unknown (``missing`` says why).
    itc_paise = MoneyField(null=True, blank=True)
    missing = models.CharField(max_length=12, choices=Missing.choices, blank=True, default="")
    #: One entry per receipt layer the pieces came from: the origin, pieces,
    #: cost, its input tax rate and the credit on it.
    layers = models.JSONField(default=list, blank=True)
    #: The bill's tax settings version, whose options said whether a gift with
    #: purchase is gift stock (§6 principle 2).
    tax_setting_version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "sell_gift_piece"
        ordering = ["-day", "-id"]
        indexes = [models.Index(fields=["store", "day"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_giftpiece_qty"),
            models.CheckConstraint(
                condition=models.Q(missing="") | models.Q(itc_paise__isnull=True),
                name="ck_giftpiece_missing_has_no_credit",
            ),
        ]

    def __str__(self) -> str:
        return f"GiftPiece({self.sale_id}:{self.line_id})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Frozen when the piece left stock, like the bill it rides on.
        if not self._state.adding:
            raise DocumentEditError("a gift tag is never changed")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise DocumentEditError("a gift tag is never deleted")


class Return(Document):
    """A historical standalone return created before the counter unified (#273).

    New return activity is recorded as return legs on a `Sale`; these rows stay
    because past documents, ledgers, credit notes, and returned-quantity ceilings
    still point at them. `original_sale` remains PROTECT for the same reason.
    """

    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="returns")
    fy = models.CharField(max_length=7)
    original_sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="plain_returns")
    returned_at = models.DateTimeField(help_text="When the counter took the piece back.")
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_mobile = models.CharField(max_length=15, blank=True, default="")
    window_override = models.BooleanField(
        default=False,
        help_text="The manager took this back past the return window in SellPolicy. "
        "A second, explicit answer - not something the ordinary override covers.",
    )
    override_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="returns_authorised",
        help_text="The manager who stood behind this return. PROTECT because the "
        "name is the evidence.",
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="returns_taken"
    )

    class Meta(Document.Meta):
        db_table = "sell_return"
        ordering = ["-returned_at", "-id"]
        constraints = [*Document.Meta.constraints]
        indexes = [
            models.Index(fields=["store", "returned_at"], name="return_store_taken_idx"),
        ]

    def __str__(self) -> str:
        return self.doc_number or f"Return(draft #{self.pk})"

    @property
    def doc_type(self) -> str:
        return RETURN_DOC_TYPE

    @property
    def gstin(self) -> Gstin:
        """The registration the return posts under - the store's, as a sale's is.

        `post_entries` snapshots this onto every leg, and Bihar and Jharkhand are
        separate registered persons: a reversal of a Bihar sale has to land in
        Bihar's books whichever counter it was taken at.
        """
        return self.store.gstin

    def series_lookup(self) -> tuple[str, str, str]:
        return self.fy, self.store.code, RETURN_DOC_TYPE


class ReturnLine(TimeStampedModel):
    """One historical piece off a bill, given back on a standalone return.

    Everything money-shaped here is copied off the line it gives back rather than
    worked out again (D2, Rule 3). What the customer gets is what they paid -
    never today's price - and which book the cost came out of is the book the
    *sale* posted to, whatever the brand's terms have become since.
    """

    return_doc = models.ForeignKey(Return, on_delete=models.CASCADE, related_name="lines")
    line_no = models.IntegerField()
    original_line = models.ForeignKey(
        SaleLine, on_delete=models.PROTECT, related_name="plain_returned_by"
    )
    barcode = models.CharField(max_length=64, db_index=True)
    season = models.CharField(max_length=120, blank=True, default="")
    # The same seven merchandising dims a bill line snapshots, for the same
    # reason: a ledger row read years later must not depend on the masters still
    # saying what they said.
    design = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    size = models.CharField(max_length=24, blank=True, default="")
    brand = models.CharField(max_length=120, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")
    hsn = models.CharField(max_length=24, blank=True, default="")
    qty = models.IntegerField()
    refund_paise = MoneyField(
        default=0, help_text="What the customer actually paid for this quantity (D2)."
    )
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    gst_paise = MoneyField(
        default=0, help_text="The tax inside the refund, at the rate the bill charged."
    )
    unit_cost_paise = MoneyField(
        default=0, help_text="The cost frozen on the original line. Zero when it was never priced."
    )
    cost_book = models.CharField(
        max_length=8, choices=SaleLine.CostBook.choices, blank=True, default=""
    )
    cost_vendor = models.ForeignKey(
        "vendors.Vendor",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="return_lines_reversed",
    )
    reason = models.CharField(max_length=40, blank=True, default="")
    condition = models.CharField(
        max_length=8, choices=SaleLine.Condition.choices, default=SaleLine.Condition.GOOD
    )

    class Meta:
        db_table = "sell_return_line"
        ordering = ["return_doc_id", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["return_doc", "line_no"], name="uq_returnline_doc_line_no"
            ),
            models.CheckConstraint(
                condition=models.Q(qty__gt=0), name="ck_returnline_qty_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.return_doc_id}/{self.line_no} · {self.barcode} × {self.qty}"

    @property
    def is_return(self) -> bool:
        """Always. It exists so a return line and a bill's own exchange leg can be
        handed to the same stock and costing code, which asks exactly this."""
        return True

    @property
    def value_paise(self) -> int:
        """What this line is worth - see `SaleLine.value_paise`."""
        return int(self.refund_paise or 0)

    @property
    def cost_paise(self) -> int:
        return int(self.unit_cost_paise or 0) * int(self.qty)


class ContinuityFlag(TimeStampedModel):
    """Something a human should look at, on a bill that was taken anyway.

    The sell face of the exception-queue pattern (`TransferReceiptException`). Rule
    8 in one table: the counter is never the place a problem is argued out, so
    everything the business can absorb lands here and shows on the store's action
    queue instead of refusing the customer.

    **Which day a flag belongs to.** Most flags are about a bill, and a bill has
    a day - the one the counter printed it on. The nightly check (#188) raises
    two that are about no bill at all: a hole is a bill that never arrived, and a
    seller's return count is about a store's afternoon. Those carry `day` in
    their details, because the check runs in the small hours of the *next*
    morning and `created_at` would file yesterday's finding under today. See
    `for_day`.
    """

    #: The details key a bill-less flag uses to say which day it is about
    #: (ISO). Nothing with a `sale` needs it - the bill already knows.
    DAY_KEY = "day"

    class Kind(models.TextChoices):
        NUMBER_HOLE = "number_hole", "Bills missing before this one"
        CN_UNVERIFIED = "cn_unverified", "Credit note taken without verification"
        RETURN_ORIG_MISSING = "return_orig_missing", "Returned against a bill we do not hold"
        OFFER_MISMATCH = "offer_mismatch", "Offer applied differs from the rulebook"
        GST_MISMATCH = "gst_mismatch", "Tax charged differs from the dated slab"
        AGED_UNCOSTED = "aged_uncosted", "Sold before inward, still unpriced"
        # #187. Not in db-design's original six: the B2B ticket asks for the
        # buyer's GSTIN to be "validated softly (flag, not block)", and the five
        # existing kinds all mean something else. Folding it into GST_MISMATCH
        # would put "a character of this registration is mistyped" and "the tax
        # on this bill is not the dated slab's" in one bucket, and head office
        # answers those two with entirely different work.
        GSTIN_INVALID = "gstin_invalid", "The buyer's GSTIN is not well formed"
        # Change PRD §10.2 (Anand, 25 September 2026): a bill numbered inside the
        # counter's transfer pause. Only an old or altered device can print one;
        # it is kept, because it is money that changed hands, and flagged.
        BILLED_WHILE_PAUSED = "billed_while_paused", "Billed while the counter was paused"
        # #184. Two historical standalone-return flags retained with their rows.
        #
        # A **late** return is one taken past the window in `SellPolicy`. The
        # `window_override` column on the document says a manager answered for
        # it; this row is what puts it on the store's morning queue, because the
        # window is the one policy at this counter a manager can set aside on
        # their own say-so and the whole point of grill Q7 is that returns are
        # watched.
        #
        # An **uncosted** return is a piece given back before the books could
        # ever price it - sold before its paperwork arrived (#186) and returned
        # before the PT landed. The money reverses, the stock comes back, and
        # there is no cost event to unwind because none was ever posted. It is
        # flagged rather than deferred: `DeferredCosting` hangs off a `SaleLine`
        # and a plain return has none, and posting a guess at what the piece cost
        # is the one outcome worse than telling somebody.
        RETURN_LATE = "return_late", "Taken back after the return window closed"
        RETURN_UNCOSTED = "return_uncosted", "Given back before the books could price it"
        # #188. Grill Q7 asks for returns to be counted per employee in the daily
        # check, and none of the kinds above means "one person took an unusual
        # number of pieces back today". It is deliberately its own kind rather
        # than a note on a bill: the finding is about a *pattern across* bills,
        # so there is no one bill to hang it on and no one bill that answers it.
        EMPLOYEE_RETURNS = "employee_returns", "One seller took back an unusual number"
        # Store operations ticket 03. The till taxed a bill under a different tax
        # settings version than the one in force for it (an offline counter that
        # had not received a new version yet, or a version this server does not
        # know), and a sold line whose HSN no rule of the version in force covers,
        # so it took that version's "no rule" rate. Both are flags: the bill stands.
        TAX_VERSION_MISMATCH = "tax_version_mismatch", "Taxed under another tax version"
        TAX_RULE_MISSING = "tax_rule_missing", "No tax rule for an item's HSN"
        # Store operations ticket 04 (ST-CMP-5). A bill dated on or after the new
        # number format's start, at a store where it is on, that arrived with no
        # number in the new series (re-entered from paper, or an old till), and a
        # bill whose number is not one head office can vouch for: outside every
        # block issued to this store, already on another bill, or cancelled at
        # month end before the bill arrived. The bill stands either way.
        INVOICE_NUMBER_MISSING = "invoice_number_missing", "No number in the new invoice series"
        INVOICE_NUMBER_PROBLEM = "invoice_number_problem", "Invoice number needs checking"
        # Store operations ticket 06 (overall PRD §10.2). The manager who approved
        # an override is the cashier who billed it. The till refuses that where
        # the store's switch is on, so only an old till or a bill queued offline
        # before the switch changed can bring one; it is printed, so it is kept.
        OVERRIDE_SELF_APPROVED = "override_self_approved", "Override approved by the cashier"
        # Store operations ticket 08 (ST-POS-2). A bill with a line split between
        # two salespeople arrived from a store whose split switch is off. The
        # till offers no split there, so only a bill queued offline before the
        # switch went off (or an old till) brings one. It is printed, so it is
        # kept with its shares, and flagged.
        SPLIT_WHILE_OFF = "split_while_off", "Split sale from a store with it off"
        # Store operations ticket 11 (ST-CMP-1). A bill priced with GST after
        # discount at a store whose switch is off, or the other way round (an old
        # till, or a bill queued before the switch changed); and a bill priced
        # after discount whose figures the server, working it out the same way,
        # does not reach to the paisa. The bill stands either way.
        AFTER_DISCOUNT_MISMATCH = "after_discount_mismatch", "Discount tax rules differ from switch"
        TILL_SERVER_MISMATCH = "till_server_mismatch", "Till and server differ on the bill"
        # Store operations ticket 13 (ST-CMP-2; baselines, CA to confirm). A piece
        # taken back after its credit-note deadline: its value was given, with no
        # tax reduction (B10). A bill whose return rules differ from the store's
        # switch, or whose till judged the deadline or the bank offer's part
        # differently from the server (an offline till holding an older tax
        # version). A bank-offer piece taken back, whose bank part was reversed
        # against the bank (B60), for Accounts. A credit note the server could not
        # number. Each is a flag; the bill stands.
        LATE_CREDIT_NOTE = "late_credit_note", "Taken back after the credit-note deadline"
        RETURN_TAX_MISMATCH = "return_tax_mismatch", "Return tax rules differ from the switch"
        CN_DEADLINE_MISMATCH = "cn_deadline_mismatch", "Till and server differ on the deadline"
        BANK_SHARE_RETURNED = "bank_share_returned", "Bank offer part reversed on a return"
        BANK_SHARE_MISMATCH = "bank_share_mismatch", "Till and server differ on the bank part"
        CREDIT_NOTE_NUMBER = "credit_note_number", "Credit note number needs checking"
        # Ticket 22: a garment the store holds on an open alteration job card came
        # back on an exchange. The bill stands (it is printed); staff settle the
        # job card, and what that means for custody waits on OQ-49.
        RETURNED_ON_JOB_CARD = "returned_on_job_card", "Garment on an open job card came back"
        # Store operations ticket 48 (ST-RPT-6, B10). A return without a bill that
        # took its customer's phone number or its cashier past the month's cap.
        # The bill stands: going over a cap never blocks it.
        NO_BILL_RETURN_CAP = "no_bill_return_cap", "Over the no-bill return cap"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        IGNORED = "ignored", "Ignored"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="continuity_flags"
    )
    sale = models.ForeignKey(
        Sale, null=True, blank=True, on_delete=models.SET_NULL, related_name="flags"
    )
    details = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    cleared_note = models.CharField(
        max_length=240,
        blank=True,
        default="",
        help_text="What the person who cleared this said about it. Its own column "
        "rather than a key in `details`, because `details` is the finding - "
        "written by a machine, rewritten on every nightly run - and a person's "
        "sentence about it must not be something a later pass can overwrite.",
    )
    resolved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sell_continuity_flag"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store", "status"], name="continuityflag_store_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} · {self.store_id}"

    @classmethod
    def for_day(
        cls, rows: models.QuerySet[ContinuityFlag], day: date
    ) -> models.QuerySet[ContinuityFlag]:
        """`rows` narrowed to the flags that belong to `day`.

        A flag about a bill belongs to the day the bill was printed. A flag about
        no bill says which day it is about in its details (`DAY_KEY`); one that
        somehow does not is filed by when it was raised, which is all there is to
        go on.

        The rule lives here rather than in the two screens that ask it, because
        "which day is this exception about" has to have one answer - the Money
        section counts them and the Dashboard's queue deliberately does not, and
        a second copy of the rule is how those two start disagreeing.
        """
        return rows.filter(
            models.Q(sale__billed_at__date=day)
            | models.Q(sale__isnull=True, **{f"details__{cls.DAY_KEY}": day.isoformat()})
            | (
                models.Q(sale__isnull=True, created_at__date=day)
                & ~models.Q(details__has_key=cls.DAY_KEY)
            )
        )


class DeferredCosting(TimeStampedModel):
    """A sold line the books cannot price yet (grill Q5, sold-before-inward).

    Rule 5 says no posting at zero value and Rule 8 says no blocked sale; both
    hold because the line waits here instead. When the GRN/PT prices its cohort,
    the sweep posts the cost event and the stock leg against the original bill.
    """

    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting on inward"
        POSTED = "posted", "Posted"
        # #184. The piece came back before anything ever priced it, so there is
        # no cost to post and never will be: the sale's cost event and the
        # return's reversal would be the same figure in opposite directions, and
        # the honest total of the pair is nothing at all. Left as a row rather
        # than deleted, because the queue is the record that a bill's cost was
        # dealt with - and "given back" is one of the ways it can be.
        RETURNED = "returned", "Given back before it could be priced"
        # #220. The bill itself was cancelled, so there is no cost to post and
        # never will be - and a row left waiting would be released by the next PT
        # that priced the cohort, posting a cost event and taking a piece off a
        # shelf for a bill the books say never happened. Left as a row for the
        # same reason `returned` is: the queue is the record of how a bill's cost
        # was dealt with, and "the bill went away" is one of the ways.
        CANCELLED = "cancelled", "The bill was cancelled"

    class Reason(models.TextChoices):
        """What the books are actually waiting for.

        Three different waits, and the sweep has to tell them apart. An
        `unpriced` piece has no cost of record, so nothing has moved for it yet -
        not the cost event and not the stock leg either, since a piece that was
        never inwarded cannot come off a shelf it was never on. The other two
        *are* on the shelf and *did* leave it: their stock is already posted and
        only the value is missing, so re-posting the movement would take the piece
        out twice.

        They also drain through different doors, which is why `sell.signals` has
        two. An `unpriced` row is released by the PT that prices its cohort. The
        other two are waiting on **master data**, not on an inward - somebody
        adding the brand, or naming its supplier - so no PT will ever release them
        and a brand or supplier edit is what does. Until one arrives they sit in
        the queue: visible, aged by the daily check, and deliberately not posted,
        because a guess about whose stock it was is the one outcome worse than a
        wait.

        A reason is not a fact about the past, either. A line waiting for its price
        can be priced by a PT and still not be placeable, at which point the sweep
        re-labels it as waiting on the masters instead. What the row *has* moved is
        never read off here - see `sell.services.costing_sweep`.
        """

        UNPRICED = "unpriced", "No cost of record - sold before inward"
        MODEL_UNKNOWN = "model_unknown", "The masters do not know this brand"
        VENDOR_UNKNOWN = "vendor_unknown", "Brand-owned, but no supplier of record"

    sale_line = models.OneToOneField(
        SaleLine, on_delete=models.CASCADE, related_name="deferred_costing"
    )
    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="deferred_costings"
    )
    barcode = models.CharField(max_length=64, db_index=True)
    season = models.CharField(max_length=120, blank=True, default="")
    qty = models.IntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.WAITING)
    reason = models.CharField(max_length=16, choices=Reason.choices, default=Reason.UNPRICED)
    posted_doc_number = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        db_table = "sell_deferred_costing"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.barcode} × {self.qty} ({self.status} · {self.reason})"


class HeldBill(TimeStampedModel):
    """A cart the counter put down to serve the next customer (grill Q13).

    The one row in this app that is **not** a fact about money. A hold moves no
    stock, takes no bill number and posts nothing: it is a cart, and a cart is
    just what somebody is thinking about buying.

    It is also the one row the till owns rather than the server. The counter's
    IndexedDB is authoritative - a hold has to survive a dead line, and the
    person who parked it is standing in front of it - and this table exists so
    the Dashboard can say "2 bills on hold" without a manager walking to the
    counter to look. That is why the push replaces the store's whole list rather
    than adding to it: a hold resumed at the counter has to *disappear* here, and
    the till has no per-hold delete to send.

    `payload` is the cart as the till holds it, opaque to the server on purpose.
    Repricing a kept bill happens at the counter on retrieval, against that day's
    rules, so the server has no reason to understand what is inside - and reading
    it as if it meant something would make a mirror into a second opinion.
    """

    class ExpiresPolicy(models.TextChoices):
        TODAY = "today", "Expires at day close unless the store keeps it"
        KEPT = "kept", "The store chose to carry it forward"

    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="held_bills")
    held_uuid = models.UUIDField()
    label = models.CharField(max_length=120, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    held_at = models.DateTimeField()
    expires_policy = models.CharField(
        max_length=8, choices=ExpiresPolicy.choices, default=ExpiresPolicy.TODAY
    )

    class Meta:
        db_table = "sell_held_bill"
        ordering = ["held_at", "id"]
        constraints = [
            # Unique **per store**, not across the estate. db-design says
            # "held_uuid UUID unique" and that is a key the till mints, so estate
            # -wide uniqueness looks free - but it makes the store part of the
            # key optional at the upsert, and an upsert that finds a row by uuid
            # alone would move another store's hold onto this counter. Scoping
            # the constraint is what makes the scoped lookup the only one the
            # database will accept.
            models.UniqueConstraint(fields=["store", "held_uuid"], name="uq_heldbill_store_uuid"),
        ]
        indexes = [
            models.Index(fields=["store", "held_at"], name="heldbill_store_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.label or 'Held bill'} · {self.store_id}"


class RegisterHandover(TimeStampedModel):
    """A manager moving a store's bill series onto a different machine (#189).

    The till owns its counter, so a dead or replaced machine takes the counter
    with it (grill Q1). The recovery is deliberate rather than automatic: a named
    manager says "this store is billing from a new device now", gives a reason,
    and the server answers with the number to resume from and the bills it never
    received - which the store re-enters from their printed copies.

    Why the act is recorded at all, when the numbering would work without it:
    every hole this leaves behind is a bill somebody has to go and find on paper,
    and a store that cannot say *when* the machine changed cannot tell a hole
    that has an explanation from one that does not. So this is an audit row in
    the `AccessChange` sense - who, when, why, and what the frontier was at the
    time - and nothing reads it back into a decision.

    It is append-only in practice: nothing in the product updates or deletes a
    row here, because a handover is something that happened.
    """

    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="register_handovers"
    )
    fy = models.CharField(max_length=7)
    reason = models.CharField(max_length=240)
    #: The frontier at the moment of the handover - what the server had accepted
    #: from the old machine. Stored rather than recomputed: the whole value of the
    #: row is that it says what was true then, and by tomorrow it will not be.
    last_accepted_seq = models.IntegerField()
    #: How many numbers below that frontier had never arrived. The list itself is
    #: not stored - it is derivable, it can be five thousand long, and what a
    #: person asks of this row a month later is "how bad was it".
    hole_count = models.IntegerField(default=0)
    actor = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="register_handovers"
    )

    class Meta:
        db_table = "sell_register_handover"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["store", "-created_at"], name="handover_store_idx"),
        ]

    def __str__(self) -> str:
        return f"Handover {self.store_id} · {self.fy} · from {self.last_accepted_seq}"


class RegisteredTill(TimeStampedModel):
    """The one counter a store is allowed to bill from (OPS-02, PRD §3.1 and §10.1).

    A store has **one active** till at a time. Registering it is a named act by a
    named person: the till's number series belongs to that counter, so two
    machines quietly sharing one series is the failure this row exists to prevent.

    Retired tills stay here (OPS-09). A replaced device is not deleted, it is
    switched off - which is what makes "a counter id is never reused" a fact this
    table can answer rather than a promise somebody has to keep. The next device
    at a store takes the next unused counter id, so a bill printed on the old
    machine and one printed on the new can never read the same.

    `authority_until` is the end of the window the till may keep billing offline
    for. OPS-09 issues and renews it, 24 hours at a time, and only while the till
    is online. A till whose window has passed is registered, not licensed.
    """

    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="registered_tills"
    )
    #: The counter's short name on the shop floor, e.g. `T1`.
    counter_id = models.CharField(max_length=12)
    registered_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="registered_tills"
    )
    registered_at = models.DateTimeField(default=timezone.now)
    active = models.BooleanField(default=True)
    #: When this device stopped being the store's counter. Empty while it is.
    retired_at = models.DateTimeField(null=True, blank=True)
    #: End of the current offline billing authority; empty until OPS-09 issues one.
    authority_until = models.DateTimeField(null=True, blank=True)
    #: `{StoreCode}-{CounterID}` — the prefix this till's own numbering carries.
    series_prefix = models.CharField(max_length=32)
    #: What the device holds to say it is this till and not another one. Minted at
    #: registration, replaced when the device is. It is a device identity, not a
    #: credential: the session is what authenticates, and a copied token buys
    #: nothing the session does not already allow. Written down as such because
    #: R-POS-004 forbids calling browser storage tamper-proof.
    device_token = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "sell_registered_till"
        ordering = ["store_id", "-registered_at"]
        constraints = [
            # One *active* counter per store, enforced rather than assumed
            # (R-POS-005). History is unconstrained, which is the point.
            models.UniqueConstraint(
                fields=["store"],
                condition=models.Q(active=True),
                name="uq_registered_till_active_store",
            ),
            models.UniqueConstraint(
                fields=["store", "counter_id"], name="uq_registered_till_counter"
            ),
        ]

    def __str__(self) -> str:
        return f"Till {self.series_prefix}"


class TillAllocation(TimeStampedModel):
    """What a till took offline with it, and nobody else may take away (PRD §10.2).

    Going offline protects the quantities the till was handed: the eligible shelf
    it received and the returnable pieces of the bills it cached. This row is that
    protection, issued with the working-set version the snapshot was read at.

    Deliberately **whole-snapshot rather than per-SKU**. A per-piece counter would
    have to be kept in step with every sale, every acceptance and every reversal,
    and the day it fell out of step it would either hide stock from the shop or
    protect a piece that had already gone. The store's till is one counter with
    one shelf; "this store's stock is spoken for until its counter has reconciled"
    is the true sentence, and it is a sentence this row can hold on its own.

    Release is explicit, by the store's own person with a reason (`sell: operate`;
    Anand, 25 September 2026). **Expiry never
    releases** - the whole hazard is a quantity that may already have been sold on
    a device nobody has heard from, and a window closing says nothing about that.

    A newer snapshot *supersedes* an older one rather than releasing it: the store
    stays protected without a gap, under the version the till is now holding.
    """

    till = models.ForeignKey(RegisteredTill, on_delete=models.PROTECT, related_name="allocations")
    #: The `WorkingSetVersion` the protected snapshot was read at.
    version = models.BigIntegerField()
    issued_at = models.DateTimeField(default=timezone.now)
    #: How much was protected, in round numbers - for a person reading the row
    #: back, never for arithmetic. The authority on what was sent is the dataset
    #: the till holds; this is the covering note.
    summary = models.JSONField(default=dict, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="till_allocation_releases",
    )
    #: Why it stopped protecting: the manager's reason, or `superseded`.
    release_reason = models.CharField(max_length=240, blank=True, default="")

    class Meta:
        db_table = "sell_till_allocation"
        ordering = ["-issued_at", "-id"]
        indexes = [
            models.Index(fields=["till", "released_at"], name="tillalloc_live_idx"),
        ]

    def __str__(self) -> str:
        return f"Allocation {self.till_id} @ v{self.version}"


class TillPause(TimeStampedModel):
    """The counter's transfer window: no new bill until a fresh copy of the shelf.

    Anand, 25 September 2026 (change PRD §10.2). Releasing a `TillAllocation`
    is the second half of one act whose first half is the counter pausing itself.
    The till writes its own pause to its local database first, and only then asks
    for the release; the server records this row in the same transaction as the
    release. From then until the counter has resumed *and* taken a fresh dataset,
    no bill may be finalised - which is what lets the store's stock be promised
    elsewhere without a clock running on whoever approves it.

    `(fy, next_seq)` is the position the counter said it would bill next, in the
    financial year *it* is counting in - never head office's, because the two
    clocks straddle 1 April. It is what closes the release's old blind spot - a
    bill printed after the last upload leaves no hole - and it marks where the
    window starts in the bill series. Positions order by year, then number: a
    bill at or after the start while the pause stands, or before
    `(resumed_fy, resumed_next_seq)` once it has ended, was billed inside the
    window and is flagged (`ContinuityFlag.Kind.BILLED_WHILE_PAUSED`), never
    refused.

    One live pause per till; ended pauses are kept.
    """

    till = models.ForeignKey(RegisteredTill, on_delete=models.PROTECT, related_name="pauses")
    #: The allocation this pause let go of.
    allocation = models.OneToOneField(
        TillAllocation, on_delete=models.PROTECT, related_name="pause"
    )
    fy = models.CharField(max_length=7)
    next_seq = models.IntegerField()
    paused_at = models.DateTimeField(default=timezone.now)
    paused_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="till_pauses"
    )
    reason = models.CharField(max_length=240)
    resumed_at = models.DateTimeField(null=True, blank=True)
    resumed_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="till_resumes",
    )
    #: The position the counter said it would bill next when it resumed. Bills
    #: from `(fy, next_seq)` up to, not including, this were numbered inside the
    #: window - which may run across 1 April.
    resumed_fy = models.CharField(max_length=7, blank=True, default="")
    resumed_next_seq = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "sell_till_pause"
        ordering = ["-paused_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["till"],
                condition=models.Q(resumed_at__isnull=True),
                name="tillpause_one_live_per_till",
            ),
        ]

    def __str__(self) -> str:
        return f"Pause {self.till_id} from {self.fy}/#{self.next_seq}"


class TillNumberBlock(TimeStampedModel):
    """A run of tax invoice numbers handed to one counter (ticket 04, ST-CMP-5).

    A till bills offline, so it has to hold its invoice numbers before the network
    goes: head office issues it a block in the new series (``XXX/26-27/n``) for one
    month, and the counter takes them in order at Save & Print. The display number
    (``DEO-T1-74``, R-POS-005) is untouched.

    A block is for **one calendar month**. At month end every number in it that no
    bill used is recorded as cancelled, for GSTR-1 Table 13 (baseline, CA to
    confirm), and the block is closed. A number is never handed out twice: the
    series counter only moves forward, and a block is sent to the device once. A
    counter that loses its database is given a fresh block, and the numbers of the
    one it lost are cancelled at month end - a visible gap, never a reuse.
    """

    till = models.ForeignKey(RegisteredTill, on_delete=models.PROTECT, related_name="number_blocks")
    prefix = models.ForeignKey("masters.DocumentPrefix", on_delete=models.PROTECT, related_name="+")
    #: The prefix's code when the block was issued. A prefix is fixed once used,
    #: so this never disagrees with ``prefix``; it is here so the block reads
    #: without a second query.
    prefix_code = models.CharField(max_length=3)
    fy = models.CharField(max_length=5)
    #: The first day of the month the block is for.
    month = models.DateField()
    first_n = models.BigIntegerField()
    last_n = models.BigIntegerField()
    #: Month end has recorded this block's unused numbers as cancelled.
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_count = models.IntegerField(default=0)

    class Meta:
        db_table = "sell_till_number_block"
        ordering = ["month", "first_n"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(first_n__gte=1) & models.Q(last_n__gte=models.F("first_n")),
                name="ck_till_number_block_range",
            ),
        ]
        indexes = [
            models.Index(fields=["till", "month"], name="till_block_month_idx"),
            models.Index(fields=["prefix", "fy", "first_n"], name="till_block_range_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.prefix_code}/{self.fy}/{self.first_n}-{self.last_n} ({self.month:%Y-%m})"


class QuarantinedUpload(TimeStampedModel):
    """A bill that arrived under a key another bill already holds (PRD §10.5).

    The same `idempotency_uuid` with *different contents* is not a retry, it is two
    different bills claiming one identity - which no amount of retrying will mend
    and which nothing may quietly pick a winner for. The upload is refused
    (`IDEMPOTENCY_CONFLICT`), the till halts its queue naming the bill, and what
    arrived is kept here so a person can see both sides.

    Nothing reads this back into a decision. It is evidence, like `RegisterHandover`.
    """

    idempotency_uuid = models.UUIDField()
    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="quarantined_uploads"
    )
    #: The bill that already holds the key. Null only if it has since been purged.
    first_sale = models.ForeignKey(
        "sell.Sale", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Exactly what arrived, as it arrived.
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "sell_quarantined_upload"
        ordering = ["-received_at", "-id"]
        indexes = [
            models.Index(fields=["store", "-received_at"], name="quarantupload_store_idx"),
        ]

    def __str__(self) -> str:
        return f"Quarantined upload {self.idempotency_uuid}"


class WorkingSetVersion(TimeStampedModel):
    """How many times this store's goods stock has moved (OPS-07, PRD §8 and §10.3).

    A counting number per store, raised by every goods stock posting made there.
    The till quotes the version it last saw; when the store's number has moved on,
    the dataset sends the whole stock section again rather than a window of it.

    A counter rather than a timestamp, deliberately. The legacy delta is a
    watermark over `updated_at`, and a watermark can step over a row that was
    stamped before a request and committed after it - the hole the legacy cursor
    laps backwards to make unlikely. Goods stock has no such column to watch at
    all: a piece becomes sellable when an acceptance event names it, and that
    fact is spread across positions, holds and reservations with no single
    "changed at" to read. A number that only ever goes up, raised inside the same
    transaction as the posting that moved the goods, cannot be read too early: a
    till on an old number gets everything, and a till on the current number is
    provably current.

    Which is also why there is no per-row delta here and none is promised. The
    version is the whole answer, and the section it guards is sent whole.
    """

    store = models.OneToOneField(
        "masters.Store", on_delete=models.PROTECT, related_name="working_set_version"
    )
    version = models.BigIntegerField(default=1)

    class Meta:
        db_table = "sell_working_set_version"
        ordering = ["store_id"]

    def __str__(self) -> str:
        return f"{self.store_id} @ v{self.version}"


class IrnQueueItem(TimeStampedModel):
    """A B2B bill waiting for head office to raise its IRN (grill Q8).

    Above the e-invoice threshold every GSTIN-bearing counter sale must receive an
    IRN within 30 days or it is invalid and the customer loses input credit. The
    deadline is data (Rule 11) and it is head office's duty, not the store's.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        FAILED = "failed", "Failed"

    sale = models.OneToOneField(Sale, on_delete=models.CASCADE, related_name="irn_queue_item")
    due_on = models.DateField(help_text="billed_at + 30 days.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    irn = models.CharField(max_length=64, blank=True, default="")
    handled_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sell_irn_queue"
        ordering = ["due_on", "id"]

    def __str__(self) -> str:
        return f"IRN due {self.due_on} · {self.sale_id}"


class ConsentAnswer(models.Model):
    """One consent answer about one mobile number (store operations ticket 15).

    Store operations PRD §6 ST-CMP-6: the counter asks two separate questions,
    each off until the customer says yes - **send my bill** and **send me
    offers** - and the customer answers them on the customer display. Before
    offers they are asked whether they are under 18; if yes, offers stay off.
    The customer can withdraw either answer at the counter at any time.

    Every answer is its own row and a row is never changed: a withdrawal is a
    new row saying no. What stands for a number is its newest answer by the time
    it was given (``answered_at``), so an answer given offline and synced later
    keeps its place and never overrides a later one. Nothing in the code updates
    a row; the only delete is erasure at the customer's request (ticket 16),
    which removes every answer for the number in that company.

    The primary key is the till's own id for the answer, so a till replaying its
    offline queue can never record one answer twice.

    Not tied to a bill: an answer is about the number, and a withdrawal needs no
    bill at all. The phone number itself stays optional on every bill.
    """

    class Question(models.TextChoices):
        BILL = "bill", "Send my bill"
        OFFERS = "offers", "Send me offers"

    class How(models.TextChoices):
        #: The customer tapped it on the customer display.
        DISPLAY = "display", "On the customer display"
        #: Staff recorded it at the counter: only ever a withdrawal.
        COUNTER = "counter", "At the counter"

    id = models.UUIDField(primary_key=True, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The bare 10-digit mobile, normalised as the customer master is.
    mobile = models.CharField(max_length=15)
    question = models.CharField(max_length=8, choices=Question.choices)
    given = models.BooleanField(help_text="Yes (consent given) or no (refused or withdrawn).")
    how = models.CharField(max_length=8, choices=How.choices)
    #: The customer's answer to "are you under 18?", asked before offers only.
    under_18 = models.BooleanField(null=True, blank=True)
    #: The consent wording version the question was asked in (``masters.consent_wording``).
    wording_version = models.PositiveIntegerField()
    #: When the customer answered, by the till's clock - kept through an offline sync.
    answered_at = models.DateTimeField()
    #: The counter that took it (the till's counter id).
    till_number = models.CharField(max_length=64, blank=True, default="")
    #: The login that recorded it at the till (B41: the login that synced it).
    staff = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sell_consent_answer"
        ordering = ["-answered_at", "-received_at"]
        indexes = [
            models.Index(
                fields=["mobile", "question", "answered_at"], name="sell_consent_newest_idx"
            ),
        ]
        constraints = [
            # Staff can never say yes for the customer: a yes comes only from the
            # customer display.
            models.CheckConstraint(
                condition=models.Q(given=False) | models.Q(how="display"),
                name="ck_consent_yes_only_on_display",
            ),
            # Offers need a "no" to the under-18 question first.
            models.CheckConstraint(
                condition=~models.Q(question="offers", given=True)
                | models.Q(under_18__isnull=False, under_18=False),
                name="ck_consent_offers_not_under_18",
            ),
            models.CheckConstraint(
                condition=~models.Q(question="bill") | models.Q(under_18__isnull=True),
                name="ck_consent_bill_asks_no_age",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.question} {'yes' if self.given else 'no'} ({self.answered_at:%Y-%m-%d})"


class CustomerErasure(models.Model):
    """That a customer was erased, and when - never who (store operations ticket 16).

    Store operations PRD §8 ST-CUS-1: at the customer's request staff erase what
    is held about them - the profile, their consent answers (and, once they
    exist, saved sizes and marketing history). Bills are tax records and stay.

    Every till holds a copy of the customer list and learns changes by
    watermark, so it has to be told that a row went. A row here says only that
    one did, and when; it holds no number, name or id. A till whose cursor is
    older than an erasure is sent the customer list whole and replaces its copy
    (``sell.services.dataset``), so the erased row leaves the till too.

    The audit record of the erasure (``sell.customer_rights.erase``) says who
    did it and at which store; the number there is masked.
    """

    id = models.UUIDField(primary_key=True, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    erased_at = models.DateTimeField()

    class Meta:
        db_table = "sell_customer_erasure"
        ordering = ["-erased_at"]
        indexes = [models.Index(fields=["erased_at"], name="sell_cust_erasure_at_idx")]

    def __str__(self) -> str:
        return f"customer erased {self.erased_at:%Y-%m-%d %H:%M}"


class SavedSize(models.Model):
    """One size a customer was learned or corrected to, for one brand and category
    (store operations ticket 18, ST-CUS-2).

    The last size bought per brand and category is learned from bills; staff may
    correct it with the customer's agreement. Every learning and every correction
    is its own row and a row is never changed: what stands for a brand and
    category is the newest row by ``as_of`` - the time the bill was made, or the
    time of the correction - so an offline bill synced late never outranks a newer
    bill or a correction made after it (``sell.services.saved_sizes``).

    Rows belong to the customer record: a record merged into another keeps its
    rows and they are read as the kept customer's; a move to a new number keeps
    them too. Erasure and the retention clean-up remove them with the profile.
    Read only within the company (tenant) of the store that recorded them.
    """

    class Source(models.TextChoices):
        #: Learned from a bill when it reached head office.
        BILL = "bill", "Learned from a bill"
        #: Corrected by staff at the counter, with the customer's agreement.
        STAFF = "staff", "Corrected by staff"

    #: For a correction, the till's own id (a replay never records it twice);
    #: for a learning, derived from the bill and the brand and category.
    id = models.UUIDField(primary_key=True, editable=False)
    customer = models.ForeignKey(
        "masters.Customer", on_delete=models.CASCADE, related_name="saved_sizes"
    )
    #: Where it was learned or corrected; its company is whose it is.
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: As spelled on the bill (or as the correction named it).
    brand = models.CharField(max_length=120)
    category = models.CharField(max_length=120)
    #: Upper case, spaces collapsed: "Mufti " and "MUFTI" are one brand.
    brand_key = models.CharField(max_length=120)
    category_key = models.CharField(max_length=120)
    size = models.CharField(max_length=24)
    source = models.CharField(max_length=8, choices=Source.choices)
    #: When the bill was made, or when the correction was recorded.
    as_of = models.DateTimeField()
    #: The bill it was learned from; null for a correction.
    sale = models.ForeignKey(
        Sale, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: The login that corrected it at the till, or that synced the bill.
    staff = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sell_saved_size"
        ordering = ["-as_of", "-recorded_at"]
        indexes = [
            models.Index(
                fields=["customer", "brand_key", "category_key", "as_of"],
                name="sell_saved_size_newest_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.brand} {self.category}: {self.size}"


# Ticket 41: the day-close cash count and the cash that leaves the drawer.
# Ticket 22: alteration job cards and the goods the store holds for a customer.
from sell.alteration_models import (  # noqa: E402, F401
    AlterationJob,
    BilledRetainedCustody,
)
from sell.cash_models import CashCount, CashCountBill, CashMovement  # noqa: E402, F401

# Ticket 26: claims for brand-funded discounts.
from sell.claim_models import BrandClaim, BrandClaimPart  # noqa: E402, F401

# Ticket 19: gift vouchers and every change to what they hold.
from sell.gift_voucher_models import GiftVoucher, GiftVoucherMovement  # noqa: E402, F401

# Ticket 42: the store's petty cash box, its top-ups and its spends.
from sell.petty_cash_models import (  # noqa: E402, F401
    PettyCashFloat,
    PettyCashSpend,
    PettyCashTopUp,
)

# Ticket 20: customer reservations and their advances.
from sell.reservation_models import (  # noqa: E402, F401
    AdvanceMovement,
    CustomerReservation,
    ReceiptVoucher,
    ReservationPiece,
)

# Ticket 21: special orders (their advances are ticket 20's tables).
from sell.special_order_models import SpecialOrder  # noqa: E402, F401
