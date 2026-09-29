"""Size-curve fills (store operations ticket 40, ST-BUY-2).

``SizeCurveFill`` - one style total a buyer asked the size curve to split into
sizes, with the curve it used and the split it gave. Written once by the fill
command and never changed: the split is a proposal, and the buyer changes the
booking line itself, which keeps its own history. The row is the evidence of
what the curve said (overall PRD R-AN-007: the rule, its source and its
output kept apart from the human's decision).
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from core.goods_base import APPEND_ONLY, TenantOwned


class SizeCurveFill(TenantOwned):
    PROTECTION: ClassVar[str] = APPEND_ONLY

    #: The store whose history the curve came from: the line's destination.
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    #: The booking's season.
    season = models.ForeignKey("masters.Season", on_delete=models.PROTECT, related_name="+")
    #: The season the curve learnt from: the same season last year (SS25 for SS26).
    reference_season = models.CharField(max_length=24)
    #: The category, as the store's bills spell it (the item's ITEM value).
    category = models.CharField(max_length=120)
    style_code = models.CharField(max_length=120)
    #: The style total the buyer typed.
    total = models.PositiveIntegerField()
    #: ``[{size, pieces}]``: pieces sold per size in the reference season.
    history = models.JSONField()
    #: ``[{size, qty}]``: the total split over those sizes; adds up to ``total``.
    split = models.JSONField()
    made_at = models.DateTimeField()
    made_by = models.ForeignKey(
        "accounts.HumanIdentity",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        ordering = ["made_at"]
        indexes = [models.Index(fields=["store", "brand", "season"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(total__gt=0), name="ck_sizecurvefill_total"),
        ]
