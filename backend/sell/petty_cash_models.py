"""A store's petty cash (store operations ticket 42, ST-MNY-3).

Three tables (overall PRD R-FIN-014, R-FIN-015):

* ``PettyCashFloat`` - one per store: the amount the petty cash box is kept at,
  and the named person who holds it (the custodian).
* ``PettyCashTopUp`` - cash put into the box, from the store's till or from
  head office. Both sides are named: who gave it (the till login, or the head
  office person and their voucher) and the custodian who received it. Written
  once.
* ``PettyCashSpend`` - cash paid out of the box: an expense head, an amount, what
  it was for and a photo of the bill. A spend over the approval limit waits for
  the Owner (a different person) in the approvals inbox; it counts as spent only
  once approved. The integer key is the approvals spine's (``Approval.object_id``);
  ``client_id`` is the screen's own id, so a request sent twice saves one row.

The bill photo's bytes are in the write-once store (``core.offbox``), never in
this database; the row keeps its key and SHA-256.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.money import MoneyField


class PettyCashFloat(models.Model):
    """The store's petty cash box: the amount it is kept at, and who holds it."""

    store = models.OneToOneField(
        "masters.Store", on_delete=models.PROTECT, primary_key=True, related_name="+"
    )
    float_paise = MoneyField()
    custodian = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField(default=1)
    set_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    set_at = models.DateTimeField()

    class Meta:
        db_table = "sell_petty_cash_float"
        constraints = [
            models.CheckConstraint(condition=Q(float_paise__gt=0), name="ck_petty_float_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.store_id} float {self.float_paise}"


class PettyCashTopUp(models.Model):
    """Cash put into the petty cash box, with both sides named."""

    class Source(models.TextChoices):
        #: Out of the store's till drawer. Inside the store's cash, so it changes
        #: nothing the day-close count expects (R-FIN-015: an internal transfer).
        TILL = "till", "From the till"
        #: Brought from head office by a named person. New cash in the store.
        HEAD_OFFICE = "head_office", "From head office"

    id = models.UUIDField(primary_key=True, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=12, choices=Source.choices)
    amount_paise = MoneyField()
    #: The receiving side: the custodian holding the box when it was topped up.
    custodian = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    #: The login that recorded it; for a till top-up, the giving side too.
    recorded_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    #: The giving side of a head office top-up: the person who brought the cash.
    given_by_name = models.CharField(max_length=120, blank=True, default="")
    #: The head office voucher or receipt number.
    reference = models.CharField(max_length=64, blank=True, default="")
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "sell_petty_cash_top_up"
        ordering = ["-recorded_at"]
        indexes = [
            models.Index(fields=["store", "recorded_at"], name="sell_petty_topup_store_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount_paise__gt=0), name="ck_petty_topup_positive"),
            models.CheckConstraint(
                condition=Q(source="till") | (~Q(given_by_name="") & ~Q(reference="")),
                name="ck_petty_topup_ho_both_sides",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source} {self.amount_paise}"


class PettyCashSpend(models.Model):
    """Cash paid out of the petty cash box against a bill."""

    class Status(models.TextChoices):
        #: At or under the limit: spent when recorded.
        SPENT = "spent", "Spent"
        #: Over the limit: waiting for the Owner in the approvals inbox.
        WAITING = "waiting", "Waiting for the Owner"
        #: Over the limit and approved: spent when the Owner approved it.
        APPROVED = "approved", "Approved"
        #: Over the limit and refused: never spent.
        REJECTED = "rejected", "Rejected"

    id = models.BigAutoField(primary_key=True)
    client_id = models.UUIDField(unique=True, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    head = models.CharField(max_length=60)
    amount_paise = MoneyField()
    #: What the money was for, in a few words.
    note = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices)
    #: When the cash counts as gone from the box (and from the day-close count).
    #: Empty while waiting and when rejected.
    counts_from = models.DateTimeField(null=True, blank=True)
    custodian = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    recorded_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField()
    decided_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=2000, blank=True, default="")
    #: The bill photo in the write-once store. Empty until one is attached.
    bill_key = models.CharField(max_length=500, blank=True, default="")
    bill_sha256 = models.CharField(max_length=64, blank=True, default="")
    bill_media_type = models.CharField(max_length=100, blank=True, default="")
    bill_size = models.PositiveIntegerField(default=0)
    bill_filename = models.CharField(max_length=255, blank=True, default="")
    bill_attached_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    bill_attached_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sell_petty_cash_spend"
        ordering = ["-recorded_at", "-id"]
        indexes = [
            models.Index(fields=["store", "counts_from"], name="sell_petty_spend_counts_idx"),
            models.Index(fields=["store", "recorded_at"], name="sell_petty_spend_store_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount_paise__gt=0), name="ck_petty_spend_positive"),
            # Spent cash has a moment it left the box; waiting or rejected has none.
            models.CheckConstraint(
                condition=(
                    Q(status__in=["spent", "approved"], counts_from__isnull=False)
                    | Q(status__in=["waiting", "rejected"], counts_from__isnull=True)
                ),
                name="ck_petty_spend_counts_from",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status__in=["approved", "rejected"], decided_by__isnull=False)
                    | Q(status__in=["spent", "waiting"], decided_by__isnull=True)
                ),
                name="ck_petty_spend_decided",
            ),
            # The Owner who decides is never the person who recorded the spend.
            models.CheckConstraint(
                condition=Q(decided_by__isnull=True) | ~Q(decided_by=models.F("recorded_by")),
                name="ck_petty_spend_not_self_decided",
            ),
            models.CheckConstraint(
                condition=Q(bill_key="", bill_sha256="")
                | (~Q(bill_key="") & ~Q(bill_sha256="") & Q(bill_attached_at__isnull=False)),
                name="ck_petty_spend_bill_whole",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.head} {self.amount_paise} {self.status}"

    @property
    def has_bill(self) -> bool:
        return bool(self.bill_key)
