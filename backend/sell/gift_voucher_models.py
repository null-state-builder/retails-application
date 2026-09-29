"""Gift vouchers (store operations ticket 19, ST-POS-4).

A gift voucher is sold at the till as its own document, with a number from the
store's GV series (ST-CMP-5), a value and a last day it can be used (12 months).
It is a bearer voucher: whoever holds the number may spend it.

Two records, each answering one question:

* ``GiftVoucher`` - what was sold: the number, the value, how it was paid and
  the last day. Written once and never changed.
* ``GiftVoucherMovement`` - every change to what it still holds: issued, used
  on a bill, or written off when it expired unused. Append-only, in the database
  too, so the balance is the sum of the rows, never an edited total.

A voucher is neither goods nor services (Circular 243/37/2024): no GST when it is
sold, GST on the goods bought with it, and none on a value that expires unused.
"""

from __future__ import annotations

from typing import Any

from django.db import models

from core.documents import Document, MintedNumber
from core.money import MoneyField

#: The gift voucher's GL document type (ST-CMP-5 series GV).
GIFT_VOUCHER_DOC_TYPE = "GV"


class GiftVoucher(Document):
    """One gift voucher, as sold. Its number is issued from the store's GV series
    (``masters.document_series.issue_number``) before it is posted;
    ``mint_number`` only hands it over."""

    # The till's own id for the sale is ``idempotency_uuid`` (the document's
    # third key), so a replay of the same sale is the same voucher.
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=32, unique=True)
    #: The random check code printed on the slip beside the number. The number
    #: runs in sequence, so it alone cannot be what lets someone spend it (B311).
    code = models.CharField(max_length=12)
    issued_on = models.DateField()
    #: The last day it can be used, inclusive.
    valid_until = models.DateField()
    value_paise = MoneyField()
    #: How the customer paid for it: cash, card or UPI.
    mode = models.CharField(max_length=8)
    reference = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(Document.Meta):
        db_table = "sell_gift_voucher"
        ordering = ["-issued_on", "-created_at"]
        indexes = [models.Index(fields=["store", "valid_until"], name="sell_gv_store_until_idx")]
        constraints = [
            *Document.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(value_paise__gt=0), name="ck_giftvoucher_value_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(mode__in=["cash", "card", "upi"]), name="ck_giftvoucher_mode"
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__gte=models.F("issued_on")),
                name="ck_giftvoucher_valid_after_issue",
            ),
        ]

    def __str__(self) -> str:
        return self.number

    @property
    def doc_type(self) -> str:
        return GIFT_VOUCHER_DOC_TYPE

    def mint_number(self) -> MintedNumber:
        # The number was issued from the GV series before posting; nothing to allocate.
        return MintedNumber(series=None, doc_number=self.number)  # type: ignore[arg-type]


class GiftVoucherMovement(models.Model):
    """One change to what a gift voucher holds. Append-only (migration 0047)."""

    class Kind(models.TextChoices):
        ISSUED = "issued", "Sold"
        USED = "used", "Used on a bill"
        EXPIRED = "expired", "Expired unused"

    voucher = models.ForeignKey(GiftVoucher, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    amount_paise = MoneyField()
    #: The bill it was used on; only on a use.
    sale = models.ForeignKey(
        "sell.Sale", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Where it was used or written off (a voucher can be used at another store
    #: under the same GSTIN).
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField()
    by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "sell_gift_voucher_movement"
        ordering = ["voucher_id", "at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_gvmovement_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(sale__isnull=False, kind="used")
                | (models.Q(sale__isnull=True) & ~models.Q(kind="used")),
                name="ck_gvmovement_sale_iff_used",
            ),
            # A voucher is sold once and written off once.
            models.UniqueConstraint(
                fields=["voucher", "kind"],
                condition=models.Q(kind__in=["issued", "expired"]),
                name="uq_gvmovement_issued_expired_once",
            ),
            # One use per voucher per bill.
            models.UniqueConstraint(
                fields=["voucher", "sale"],
                condition=models.Q(kind="used"),
                name="uq_gvmovement_one_use_per_bill",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.amount_paise}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValueError("a gift voucher movement is never changed; add a new one")
        super().save(*args, **kwargs)
