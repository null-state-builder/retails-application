"""A store's stored choice for one switchable feature (ST-OPS-6).

No row means the feature's registered default. A row is written only when Admin
changes a switch, and it is never deleted: switching a feature off changes this
row and nothing else, so every record the feature made stays where it is. Each
change's before and after values live in its ``AuditEvent``.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import TenantOwned


class StoreFeatureSwitch(TenantOwned):
    class Mode(models.TextChoices):
        MANUAL = "manual"
        CONNECTED = "connected"

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    feature_key = models.CharField(max_length=60)
    enabled = models.BooleanField()
    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.MANUAL)
    revision = models.IntegerField(default=1)
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "site", "feature_key"], name="uq_storefeatureswitch_site_key"
            ),
        ]
