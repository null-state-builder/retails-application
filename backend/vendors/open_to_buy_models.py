"""Open-to-buy budgets and the Owner's approvals of bookings over them (ST-BUY-1; ticket 39).

A budget is money at cost that may be spent on one brand in one season, at one
site or across the whole company. Open-to-buy is that budget less the open
bookings and the goods received against them (``vendors.open_to_buy``). A
booking that would go over it is confirmed only after the Owner approves it in
the approvals inbox; each time the buyer asks, one ``OpenToBuyAsk`` is written.

Both tables have integer keys because the shared approvals spine
(``approvals.Approval``) points at its subject by an integer id, and the budget
row is audited by the same kind of key. Each row belongs to one tenant: the
column defaults to the connection's trusted tenant, and migration 0011 puts both
tables behind forced row-level security. Its trigger refuses any delete, and any
change to an ask once written.
"""

from __future__ import annotations

from django.db import models

from core.base import TimeStampedModel
from core.goods_base import CurrentTenant


def _tenant() -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        "masters.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
        db_default=CurrentTenant(),
    )


class OpenToBuyBudget(TimeStampedModel):
    tenant = _tenant()
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    season = models.ForeignKey("masters.Season", on_delete=models.PROTECT, related_name="+")
    #: The site the budget is for. Null: the whole company.
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Money at cost, in whole paise.
    budget_paise = models.BigIntegerField()
    #: Bumped by every change, so a stale screen is refused.
    revision = models.PositiveIntegerField(default=1)
    set_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "vendors_open_to_buy_budget"
        ordering = ["brand_id", "season_id", "site_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "brand", "season", "site"],
                condition=models.Q(site__isnull=False),
                name="uq_otb_budget_site",
            ),
            models.UniqueConstraint(
                fields=["tenant", "brand", "season"],
                condition=models.Q(site__isnull=True),
                name="uq_otb_budget_company",
            ),
            models.CheckConstraint(
                condition=models.Q(budget_paise__gte=0), name="ck_otb_budget_not_negative"
            ),
        ]

    def __str__(self) -> str:
        return f"Open-to-buy {self.brand_id}/{self.season_id}/{self.site_id or 'company'}"


class OpenToBuyAsk(TimeStampedModel):
    """One request to the Owner to confirm a booking that goes over open-to-buy.

    It covers exactly the draft it was asked for (``draft_hash``): a draft changed
    after asking is asked about again. The Owner's decision is the approvals
    spine's row for this ask; nothing here is changed once written.
    """

    tenant = _tenant()
    booking = models.ForeignKey(
        "vendors.GoodsBooking", on_delete=models.PROTECT, related_name="open_to_buy_asks"
    )
    #: The booking draft's content hash when the Owner was asked.
    draft_hash = models.CharField(max_length=64)
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    season = models.ForeignKey("masters.Season", on_delete=models.PROTECT, related_name="+")
    #: The booking's home site, when it has one (the inbox shows it).
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Each budget the booking goes over, as ``vendors.open_to_buy.Over`` (paise as text).
    figures = models.JSONField(default=list)
    #: The most it goes over any one budget by, in paise (0 when only a cost is missing).
    over_paise = models.BigIntegerField(default=0)
    asked_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "vendors_open_to_buy_ask"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["booking", "created_at"])]

    def __str__(self) -> str:
        return f"Open-to-buy ask {self.pk} for {self.booking_id}"
