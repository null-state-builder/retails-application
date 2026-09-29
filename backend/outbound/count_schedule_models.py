"""A store's count schedule (store operations PRD ST-INV-3; ticket 35).

One row says "count this at this store every week, month or quarter, from this
day". The Owner sets it. A brand of null means the whole store. Changing a row
bumps its revision; stopping it clears ``active``. Nothing is ever deleted: each
change's before and after values live in its ``AuditEvent``.

The count itself is the store's existing blind count; this row only says when it
is due. Whether it was done is read from those counts, never recorded here.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import TenantOwned


class CountEvery(models.TextChoices):
    WEEK = "week", "Every week"
    MONTH = "month", "Every month"
    QUARTER = "quarter", "Every 3 months"


class CountSchedule(TenantOwned):
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The brand to count; null counts the whole store.
    brand = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    every = models.CharField(max_length=8, choices=CountEvery.choices)
    #: The first day a count is due; the next ones follow from it.
    first_due_on = models.DateField()
    #: The day (India) the schedule was set or last changed. A due date before it
    #: is never judged missed: a change must not make a past day late.
    judged_from = models.DateField()
    active = models.BooleanField(default=True)
    revision = models.IntegerField(default=1)
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            # One live schedule per store and brand (and one for the whole store).
            models.UniqueConstraint(
                fields=["tenant", "site", "brand"],
                condition=models.Q(active=True),
                nulls_distinct=False,
                name="uq_countschedule_active_scope",
            ),
        ]
        indexes = [models.Index(fields=["site", "active"])]
