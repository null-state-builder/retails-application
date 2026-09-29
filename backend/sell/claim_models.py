"""Claims for brand-funded discounts (store operations PRD ST-BRD-3, ST-BRD-6; ticket 26).

At month end the brand-funded share of each discounted line (ticket 25's
``SaleLineFunding``) becomes a claim on the brand, one per brand and store for
the month. The brand settles it with its own commercial credit note, which has
no GST effect on our bills (Circular 251/08/2025): nothing here touches a bill,
issues a tax document or posts to the accounts.

Two tables:

* ``BrandClaim`` - one claim: the brand, the store, the month, the amount
  claimed (frozen when raised), and where it stands: raised, accepted by the
  brand, settled in full, or settled short with the difference kept. A claim
  raised while the brand's promotion-services agreement is Yes is flagged for
  Accounts (ST-BRD-6).
* ``BrandClaimPart`` - which funded part of which bill line a claim took in,
  with the brand's share as it stood. One row per part, ever, so a part is never
  claimed twice; a bill that syncs after a month's claim was raised is taken in
  by the next claim for that month.

The row has an integer key, so an alert can open it (``alerts.Alert.object_id``).
Each row belongs to one tenant: the column defaults to the connection's trusted
tenant, and migration 0039 puts both tables behind forced row-level security. The
same migration's trigger refuses any change to a settled claim, any change to a
part, and any delete.
"""

from __future__ import annotations

from django.db import models

from core.base import TimeStampedModel
from core.goods_base import CurrentTenant
from core.money import MoneyField


def _tenant() -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        "masters.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
        db_default=CurrentTenant(),
    )


class BrandClaim(TimeStampedModel):
    class Status(models.TextChoices):
        RAISED = "raised", "Raised"
        ACCEPTED = "accepted", "Accepted by the brand"
        SETTLED = "settled", "Settled"
        SETTLED_SHORT = "settled_short", "Settled short"

    class Kind(models.TextChoices):
        #: The only kind today; the claim register (R-FIN-013) holds others later.
        DISCOUNT = "discount", "Brand-funded discount"

    tenant = _tenant()
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.DISCOUNT)
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The first day of the month claimed (India business days).
    month = models.DateField()
    #: 1 for the month's claim; 2, 3 ... for bills that reached head office after it.
    sequence = models.PositiveSmallIntegerField(default=1)
    #: ``<store>-<brand>-<yyyymm>-<n>``: a reference for the brand, not a tax number.
    reference = models.CharField(max_length=60)
    #: The brand-funded share claimed: sold lines less pieces given back, in paise.
    amount_paise = MoneyField()
    #: How many funded parts it took in, and the pieces on them (sold less given back).
    parts = models.PositiveIntegerField(default=0)
    pieces = models.IntegerField(default=0)
    #: Brand-funded discount in the same month whose share is unknown: not claimed.
    unknown_paise = MoneyField(default=0)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.RAISED)
    #: Bumped by every change, so a stale screen is refused.
    revision = models.PositiveIntegerField(default=1)
    #: The promotion-services agreement was Yes on the month's last day (ST-BRD-6):
    #: the money may be payment for a service, not a discount. For Accounts.
    promotion_flag = models.BooleanField(default=False)
    promotion_version_id = models.UUIDField(null=True, blank=True)
    raised_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    accepted_on = models.DateField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: What the brand said, and how (their reference or e-mail), in Accounts' words.
    accepted_note = models.CharField(max_length=240, blank=True, default="")
    #: The brand's commercial credit note: no GST, no change to any bill.
    credit_note_number = models.CharField(max_length=60, blank=True, default="")
    credit_note_date = models.DateField(null=True, blank=True)
    settled_paise = MoneyField(null=True, blank=True)
    #: Claimed less settled, kept when the brand paid short.
    difference_paise = MoneyField(null=True, blank=True)
    difference_reason = models.CharField(max_length=240, blank=True, default="")
    settled_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "sell_brand_claim"
        ordering = ["-month", "store_id", "brand_id", "sequence"]
        indexes = [models.Index(fields=["month", "store"]), models.Index(fields=["status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "brand", "store", "month", "sequence"],
                name="uq_brandclaim_month_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_brandclaim_amount"
            ),
            # Settled means a credit note, and short means a difference with its reason.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["settled", "settled_short"])
                    | (
                        ~models.Q(credit_note_number="")
                        & models.Q(credit_note_date__isnull=False, settled_paise__isnull=False)
                    )
                ),
                name="ck_brandclaim_settled_note",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="settled_short")
                | (models.Q(difference_paise__gt=0) & ~models.Q(difference_reason="")),
                name="ck_brandclaim_short_reason",
            ),
        ]

    def __str__(self) -> str:
        return self.reference


class BrandClaimPart(models.Model):
    """One funded part of a bill line, taken in by one claim, ever.

    ``funding_id`` is the ``SaleLineFunding`` row. It is kept without a database
    foreign key so the part outlives any clean-up of the bill it came from; the
    figures it was claimed at are frozen here.
    """

    tenant = _tenant()
    claim = models.ForeignKey(BrandClaim, on_delete=models.PROTECT, related_name="claimed_parts")
    funding_id = models.BigIntegerField(unique=True)
    sale_id = models.BigIntegerField()
    line_id = models.BigIntegerField()
    #: The brand's share, signed: negative for a piece given back.
    brand_paise = MoneyField()
    #: The line's pieces, signed the same way.
    pieces = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sell_brand_claim_part"

    def __str__(self) -> str:
        return f"{self.funding_id} -> {self.claim_id}"
