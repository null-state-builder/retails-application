"""The day-close cash count and the cash that leaves the drawer (ticket 41, ST-MNY-2).

Two tables, both written once and never changed (overall PRD R-FIN-015):

* ``CashCount`` - the Z-report count of one store's drawer, by note and coin,
  against the cash the system expected to be there. A difference is kept as a
  variance on the count and opens an owned exception. It is never booked as a
  balancing entry: nothing here writes to any ledger.
* ``CashCountBill`` - which bills a count took in. A bill is in at most one
  count (the database says so), so no bill is counted twice, and a bill that
  reaches the server after a count - a slow sync, a second device, a clock
  running behind - is simply taken in by the next one. Bills are chosen by
  whether they are counted yet, never by the till's clock.
* ``CashMovement`` - cash taken out of the drawer between counts: a bank
  deposit or a handover to a named person. Each row names both sides - who
  gave the cash out of the drawer, and who received it (the bank, or the
  person) with the slip or receipt number that proves it.

The primary keys are the till's own ids, so a request sent twice over a bad
line records one row.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from core.money import MoneyField


class CashCount(models.Model):
    """One store's drawer counted at day close (the Z-report).

    The count covers everything since the store's previous count (``previous``),
    or, for the store's first count, the business day from midnight with an
    opening float the cashier declared (``opening_declared``). Every figure the
    expected cash was worked out from is kept on the row, so the count reads
    the same years later whatever happens to the bills behind it.
    """

    id = models.UUIDField(primary_key=True, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The store's trading day this count closes (India date at the count).
    business_day = models.DateField()
    #: The server's time at the count: the cut-off for bills and movements.
    counted_at = models.DateTimeField()
    #: Movements recorded after this time (the previous count; the first count:
    #: midnight) and up to ``counted_at`` are in this count. Bills are taken in
    #: by ``CashCountBill``, not by time.
    window_from = models.DateTimeField()
    previous = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="next"
    )
    opening_paise = MoneyField()
    #: True on a store's first count: the opening was typed by the cashier, not
    #: carried from an earlier count.
    opening_declared = models.BooleanField(default=False)
    cash_sales_paise = MoneyField()
    cash_refunds_paise = MoneyField(default=0)
    #: Deposits and handovers in the window.
    movements_paise = MoneyField(default=0)
    #: Petty cash spent in the window (ticket 42).
    petty_cash_paise = MoneyField(default=0)
    #: Petty cash brought from head office in the window (ticket 42): new cash
    #: in the store. A top-up from the till moves cash inside it and is not here.
    petty_top_ups_paise = MoneyField(default=0)
    expected_paise = MoneyField()
    #: Pieces of each note, by face value in rupees: {"500": 3, "200": 0, ...}.
    notes = models.JSONField()
    coins_paise = MoneyField(default=0)
    counted_paise = MoneyField()
    #: Counted minus expected: negative is short, positive is over.
    variance_paise = MoneyField()
    counted_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    #: The manager whose own PIN confirmed a variance. Empty when there is none.
    approved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: When the manager typed their PIN, by the till's clock.
    approved_at = models.DateTimeField(null=True, blank=True)
    till_number = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "sell_cash_count"
        ordering = ["-counted_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "business_day"], name="uq_cash_count_day"),
            models.CheckConstraint(
                condition=Q(variance_paise=F("counted_paise") - F("expected_paise")),
                name="ck_cash_count_variance",
            ),
            models.CheckConstraint(
                condition=Q(counted_paise__gte=0) & Q(coins_paise__gte=0) & Q(opening_paise__gte=0),
                name="ck_cash_count_not_negative",
            ),
            # A variance is confirmed by a manager, never by the person who counted.
            models.CheckConstraint(
                condition=Q(variance_paise=0) | Q(approved_by__isnull=False),
                name="ck_cash_count_variance_approved",
            ),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True) | ~Q(approved_by=F("counted_by")),
                name="ck_cash_count_not_self_approved",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.business_day} {self.variance_paise:+d}"


class CashCountBill(models.Model):
    """One bill a count took in. A bill is in one count at most."""

    count = models.ForeignKey(CashCount, on_delete=models.CASCADE, related_name="bills")
    sale = models.OneToOneField(
        "sell.Sale", on_delete=models.PROTECT, related_name="cash_count_link"
    )

    class Meta:
        db_table = "sell_cash_count_bill"

    def __str__(self) -> str:
        return f"{self.count_id} / {self.sale_id}"


class CashMovement(models.Model):
    """Cash taken out of a store's drawer, with both sides named."""

    class Kind(models.TextChoices):
        #: Paid into the bank. The receiving side is the bank and its slip.
        DEPOSIT = "deposit", "Bank deposit"
        #: Handed to a named person (head office collection). The receiving
        #: side is that person and the receipt they signed.
        HANDOVER = "handover", "Handover"

    id = models.UUIDField(primary_key=True, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount_paise = MoneyField()
    #: The giving side: the login that took the cash out of the drawer.
    given_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    #: The receiving side: the bank (deposit) or the person (handover).
    received_by = models.CharField(max_length=120)
    #: The proof on the receiving side: the deposit slip or the receipt number.
    reference = models.CharField(max_length=64)
    recorded_at = models.DateTimeField()
    till_number = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "sell_cash_movement"
        ordering = ["-recorded_at"]
        indexes = [
            models.Index(fields=["store", "recorded_at"], name="sell_cash_move_store_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_paise__gt=0), name="ck_cash_movement_positive"
            ),
            models.CheckConstraint(
                condition=~Q(received_by="") & ~Q(reference=""),
                name="ck_cash_movement_both_sides",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.amount_paise} to {self.received_by}"
