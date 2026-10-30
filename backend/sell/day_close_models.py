"""Closing a store's business day (store operations, R-POS-011 / R-FIN-015).

One table, ``DayClose``: one row per store and business day, written by
``sell.services.day_close`` and nothing else. It records two things a person
does at the end of the day:

* **The day summary was checked.** Who ticked it, when, and the day's bills as
  they stood at that moment (``summary_facts``: how many, the last one the
  server held, and what they came to). A bill arriving after the tick changes
  those facts, so the close asks for the summary to be read again.
* **The day was closed.** Who was at the counter, the manager whose own PIN
  confirmed it, when, the cash count it relied on, and the day's figures frozen
  at that moment (``facts``). After the close the row is never changed again.

Nothing here touches a bill, a tender, a count or a ledger: the close is a
statement about documents that already exist, so historical documents and
totals read exactly as they did before it.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class DayClose(models.Model):
    """One store's business day: its summary tick and, once done, its close."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The store's trading day (`sell.services.cash_count.business_day`).
    business_day = models.DateField()
    summary_checked_at = models.DateTimeField(null=True, blank=True)
    summary_checked_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The day's bills at the tick: {"bills", "frontier", "net_paise"}.
    summary_facts = models.JSONField(default=dict, blank=True)
    #: The server's time at the close. Empty while the day is open.
    closed_at = models.DateTimeField(null=True, blank=True)
    #: The login at the counter that asked for the close.
    closed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The manager of this store whose own PIN confirmed the close.
    confirmed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The day's cash count the close relied on (itself never changed).
    cash_count = models.OneToOneField(
        "sell.CashCount",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="day_close",
    )
    #: The day's figures frozen at the close: bills, tenders, net and frontier.
    facts = models.JSONField(default=dict, blank=True)
    till_number = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "sell_day_close"
        ordering = ["-business_day"]
        constraints = [
            models.UniqueConstraint(fields=["store", "business_day"], name="uq_day_close_day"),
            # A closed day names everything the close relied on.
            models.CheckConstraint(
                condition=Q(closed_at__isnull=True)
                | (
                    Q(closed_by__isnull=False)
                    & Q(confirmed_by__isnull=False)
                    & Q(cash_count__isnull=False)
                    & Q(summary_checked_at__isnull=False)
                ),
                name="ck_day_close_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.business_day} {'closed' if self.closed_at else 'open'}"
