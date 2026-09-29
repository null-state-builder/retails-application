"""Brand terms, dated and approved (store operations PRD ST-BRD-1, ST-BRD-6; ticket 23).

Three append-only tables. Nothing here is ever changed or deleted by the
application: a change is a new row, so a result worked out under one version can
always be worked out again the same way (overall PRD R-BUY-006, "never change
original ... to apply new commercial terms").

* ``BrandTermsVersion`` - one brand's commercial terms for one season, from a
  date: the model (SOR, outright, consignment or concession), margin %, return
  allowance %, the brand's share of discount funding, and payment days. A figure
  left blank is **unknown**, never zero (D9, overall PRD §1.5).
* ``BrandPromotionVersion`` - one brand's "promotion-services agreement: yes/no",
  from a date. With none approved the answer is No (ST-BRD-6's default).
* ``BrandTermsDecision`` - the Owner's approval or rejection of one proposed row,
  or the proposer withdrawing it. A proposed row counts only once approved; one
  decision per row, ever.

A brand with no approved terms in force for a season has an **unknown** model.
Nothing is assumed: in particular, the two axes on ``masters.Brand`` (which
default to Outright) are not read as the brand's terms.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from core.goods_base import APPEND_ONLY, TenantOwned


class CommercialModel(models.TextChoices):
    SOR = "sor", "SOR"
    OUTRIGHT = "outright", "Outright"
    CONSIGNMENT = "consignment", "Consignment"
    CONCESSION = "concession", "Concession"


class BrandTermsVersion(TenantOwned):
    PROTECTION: ClassVar[str] = APPEND_ONLY

    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    season = models.ForeignKey("masters.Season", on_delete=models.PROTECT, related_name="+")
    #: 1, 2, 3 ... per brand and season, counting every proposal (approved or not).
    version = models.PositiveIntegerField()
    #: The first day (India) the terms apply to, once approved.
    applies_from = models.DateField()
    model = models.CharField(max_length=12, choices=CommercialModel.choices)
    #: Percentages as agreed, two decimals, 0-100. Null is unknown, never 0.
    margin_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    return_allowance_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    #: The brand's share of a discount it funds, in % (the rest is KDPS's).
    discount_funding_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    payment_days = models.PositiveIntegerField(null=True, blank=True)
    #: Why the change, in the proposer's words.
    note = models.CharField(max_length=240)
    actor = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "brand", "season", "version"],
                name="uq_brandtermsversion_number",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="ck_brandtermsversion_from_one"
            ),
        ]

    def __str__(self) -> str:
        return f"Terms v{self.version} (from {self.applies_from})"


class BrandPromotionVersion(TenantOwned):
    PROTECTION: ClassVar[str] = APPEND_ONLY

    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    #: 1, 2, 3 ... per brand, counting every proposal.
    version = models.PositiveIntegerField()
    applies_from = models.DateField()
    agreement = models.BooleanField()
    note = models.CharField(max_length=240)
    actor = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "brand", "version"], name="uq_brandpromotionversion_number"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="ck_brandpromotionversion_from_one"
            ),
        ]

    def __str__(self) -> str:
        return f"Promotion services v{self.version} (from {self.applies_from})"


class BrandTermsDecision(TenantOwned):
    """What became of one proposed row. ``created_at`` is when it was decided."""

    PROTECTION: ClassVar[str] = APPEND_ONLY

    class Kind(models.TextChoices):
        TERMS = "terms", "Terms"
        PROMOTION = "promotion", "Promotion-services agreement"

    class Outcome(models.TextChoices):
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    subject_kind = models.CharField(max_length=12, choices=Kind.choices)
    #: The ``BrandTermsVersion`` or ``BrandPromotionVersion`` decided.
    subject_id = models.UUIDField()
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    note = models.CharField(max_length=240, blank=True, default="")
    actor = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "subject_kind", "subject_id"],
                name="uq_brandtermsdecision_one_per_row",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject_kind} {self.subject_id}: {self.outcome}"
