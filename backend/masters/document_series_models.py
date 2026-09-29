"""Site prefixes and document number series (store operations ticket 04, ST-CMP-5).

Every tax document number is ``XXX/...`` where ``XXX`` is a fixed 3-letter
prefix, separate from the store code (Q8, Q10). A store and every other site
that sends goods (the warehouse) has one; head office has one per GSTIN, used
for debit notes. Prefixes are unique across the company.

Four tables:

* ``DocumentPrefix`` - who owns which 3 letters. The code cannot change once a
  number has been taken from it (``masters.document_series.prefix_in_use``).
* ``NumberingSetting`` - the date the new format starts (a 1 April, B8) and the
  size of the number blocks an offline till is given. One row per company; no
  row means the defaults.
* ``DocumentSeriesCounter`` - the next number of one series (prefix, document
  kind, financial year). Advanced only by ``allocate`` in one statement, so it
  is gap-free and never goes back.
* ``IssuedDocumentNumber`` - every number that has been used or cancelled. The
  unique number is what makes "never reused" a property of the table. A
  cancelled row (an unused number from an offline till's block, closed at month
  end) is what GSTR-1 Table 13 reads (baseline, CA to confirm).
"""

from __future__ import annotations

from django.db import models

from core.goods_base import TenantOwned


class DocumentPrefix(TenantOwned):
    """Three capital letters owned by one site, or by head office for one GSTIN."""

    code = models.CharField(max_length=3)
    #: A store or warehouse (``Store.store_type``). Empty for a head-office prefix.
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Head office's prefix for this GSTIN (debit notes). Empty for a site prefix.
    gstin = models.ForeignKey(
        "masters.Gstin", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.IntegerField(default=1)
    updated_at = models.DateTimeField()

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_documentprefix_code"),
            models.UniqueConstraint(
                fields=["tenant", "site"],
                condition=models.Q(site__isnull=False),
                name="uq_documentprefix_site",
            ),
            models.UniqueConstraint(
                fields=["tenant", "gstin"],
                condition=models.Q(gstin__isnull=False),
                name="uq_documentprefix_gstin",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(site__isnull=False, gstin__isnull=True)
                    | models.Q(site__isnull=True, gstin__isnull=False)
                ),
                name="ck_documentprefix_one_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[A-Z]{3}$"),
                name="ck_documentprefix_three_letters",
            ),
        ]

    def __str__(self) -> str:
        return self.code


class NumberingSetting(TenantOwned):
    """When the new number format starts, and how big a till's number block is."""

    #: Always a 1 April (§6 principle 5). Until then every bill keeps today's format.
    new_format_from = models.DateField()
    #: How many invoice numbers one offline block holds (a setting, not a fixed value).
    till_block_size = models.PositiveIntegerField()
    revision = models.IntegerField(default=1)
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant"], name="uq_numberingsetting_tenant"),
            models.CheckConstraint(
                condition=models.Q(new_format_from__month=4, new_format_from__day=1),
                name="ck_numberingsetting_first_april",
            ),
            models.CheckConstraint(
                condition=models.Q(till_block_size__gte=1),
                name="ck_numberingsetting_block_size",
            ),
        ]

    def __str__(self) -> str:
        return f"New number format from {self.new_format_from}"


class DocumentSeries(models.TextChoices):
    """The six series of ST-CMP-5. The value is the code a number carries."""

    TAX_INVOICE = "INV", "Tax invoice"
    CREDIT_NOTE = "CN", "Credit note"
    RECEIPT_VOUCHER = "RV", "Receipt voucher"
    GIFT_VOUCHER = "GV", "Gift voucher"
    DELIVERY_CHALLAN = "DC", "Delivery challan"
    DEBIT_NOTE = "DN", "Debit note"


class DocumentSeriesCounter(TenantOwned):
    """The next number of one series in one financial year."""

    prefix = models.ForeignKey(DocumentPrefix, on_delete=models.PROTECT, related_name="counters")
    series = models.CharField(max_length=3, choices=DocumentSeries.choices)
    #: ``26-27``.
    fy = models.CharField(max_length=5)
    next_n = models.BigIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "prefix", "series", "fy"], name="uq_documentseriescounter_scope"
            ),
            models.CheckConstraint(
                condition=models.Q(next_n__gte=1), name="ck_documentseriescounter_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.prefix_id} {self.series} {self.fy} -> {self.next_n}"


class IssuedDocumentNumber(TenantOwned):
    """One number that has been used on a document, or cancelled unused."""

    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        CANCELLED = "cancelled", "Cancelled"

    number = models.CharField(max_length=16)
    prefix = models.ForeignKey(DocumentPrefix, on_delete=models.PROTECT, related_name="numbers")
    series = models.CharField(max_length=3, choices=DocumentSeries.choices)
    fy = models.CharField(max_length=5)
    n = models.BigIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices)
    #: What used it, e.g. ``sale`` and the bill's id. Empty on a cancelled number.
    document_type = models.CharField(max_length=40, blank=True, default="")
    document_ref = models.CharField(max_length=64, blank=True, default="")
    #: The document's date; for a cancelled number, the day it was cancelled.
    issued_on = models.DateField()
    #: The first day of the month the number is reported in (GSTR-1 Table 13).
    period = models.DateField()
    #: Why a number was cancelled, or what happened to it since.
    reason = models.CharField(max_length=240, blank=True, default="")
    updated_at = models.DateTimeField()

    class Meta:
        ordering = ["prefix", "series", "fy", "n"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uq_issueddocumentnumber"),
            models.UniqueConstraint(
                fields=["tenant", "prefix", "series", "fy", "n"],
                name="uq_issueddocumentnumber_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status", "period"], name="issueddocno_period_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.number} ({self.status})"
