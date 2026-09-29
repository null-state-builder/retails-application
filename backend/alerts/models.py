"""One shared mechanism for time-based alerts that raise their own hand (#77).

Two checks ride it today — in-transit aging and return-window countdown — and
the shape is the point: a later alert kind (dead stock, another deadline) is
another check function returning the same ``AlertHit`` shape, not a new table
or a new screen. Modelled on ``approvals.Approval`` (#70) for the reason that
one works — every field the inbox needs is snapshotted onto the row, so it
renders without importing a business model.

An alert is not a decision. Nobody approves or rejects it, so it carries no
maker/checker — only a lifecycle: open while the condition holds, resolved the
run it stops holding. The daily job (``alerts.checks.run_alert_checks``) is
idempotent — re-running it the same day, or a day late, creates nothing new for
a condition already open, and quietly resolves what no longer applies.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models

from core.base import TimeStampedModel


class AlertKind(models.TextChoices):
    IN_TRANSIT_AGING = "in_transit_aging", "Transfer stuck in transit"
    RETURN_WINDOW = "return_window", "Return window closing"
    # Store operations ticket 04 (ST-CMP-5): a tax document number was refused
    # before issue (too long, a character not allowed, a series full, no prefix,
    # already used), or a number cancelled at month end turned up on a late bill.
    DOCUMENT_NUMBER = "document_number", "Document number problem"
    # Store operations ticket 33 (ST-INV-2): stock aged against its season - in
    # season with no sale for the policy's days, or from a season that has ended.
    STOCK_AGEING = "stock_ageing", "Stock ageing"
    # Store operations ticket 32 (ST-INV-1): style-colours at a store missing too
    # many of their category's core sizes. One per store, naming how many; each
    # style-colour's own alert is a ``stockledger.BrokenSizeAlert``.
    BROKEN_SIZE = "broken_size", "Broken sizes"
    # Store operations ticket 35 (ST-INV-3): a scheduled count is due today, or
    # was due and is still not done.
    COUNT_DUE = "count_due", "Count due"
    # Store operations ticket 41 (ST-MNY-2): a day-close cash count that did
    # not match the expected cash. Raised when the count is saved.
    CASH_VARIANCE = "cash_variance", "Cash variance"
    # Store operations ticket 26 (ST-BRD-6): a brand discount claim raised while
    # the brand's promotion-services agreement is Yes, for Accounts to check.
    BRAND_CLAIM_FLAGGED = "brand_claim_flagged", "Brand claim to check"
    # Store operations ticket 20 (ST-ORD-1, ST-OPS-2): a customer reservation is
    # about to reach its collect-by date.
    RESERVATION_EXPIRY = "reservation_expiry", "Reservation expiring"
    # Store operations ticket 24 (ST-BRD-5): SOR stock at 5 months from the brand's
    # dispatch date (or past 6, with no brand invoice), and SOR stock with no
    # dispatch date to age it from. One per site and state, naming how many.
    SOR_AGEING = "sor_ageing", "SOR ageing"
    # Store operations ticket 48 (ST-RPT-6, B10): a customer's phone number or a
    # staff member went over the month's cap on returns without a bill.
    NO_BILL_RETURN_CAP = "no_bill_return_cap", "No-bill return cap"
    # Store operations ticket 49 (ST-OPS-4): a store's checklist whose time passed
    # with items not ticked. One per store, list and day, while any is still open.
    CHECKLIST_MISSED = "checklist_missed", "Checklist missed"


class AlertStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class Alert(TimeStampedModel):
    """One open-or-resolved instance of a shared alert kind."""

    kind = models.CharField(max_length=32, choices=AlertKind.choices)
    kind_label = models.CharField(max_length=64)
    title = models.CharField(
        max_length=240,
        help_text="Snapshot one-liner the inbox shows — store, brand, days.",
    )
    dedupe_key = models.CharField(
        max_length=160,
        help_text="What makes this one instance of this kind — e.g. a transfer id, "
        "or a (brand, store, sku, threshold) tuple. The job upserts on "
        "(kind, dedupe_key) so a daily re-run never doubles an alert that is "
        "still true.",
    )
    store = models.ForeignKey(
        "masters.Store",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="alerts",
        help_text="Scopes the inbox for store-scoped users — the same rule as "
        "approvals (ADR-0003).",
    )
    brand = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="Scopes the inbox for a brand-scoped user. Blank means this "
        "alert is not about one brand.",
    )
    object_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="The document this alert is about, if it is about one — a "
        "transfer id for in-transit aging. The client maps kind → route, the "
        "same way it does for approvals; a return-window alert has none, "
        "because it names a holding rather than a document.",
    )
    due_date = models.DateField(
        null=True, blank=True, help_text="The deadline this alert is counting down to."
    )
    threshold_days = models.IntegerField(
        null=True,
        blank=True,
        help_text="Which configured threshold this crossing fired at.",
    )
    status = models.CharField(max_length=16, choices=AlertStatus.choices, default=AlertStatus.OPEN)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "alerts_alert"
        ordering = ["due_date", "-created_at"]
        constraints = [
            # One *open* row per (kind, dedupe_key) — the same still-true
            # condition is never listed twice. A resolved row is history, so a
            # later recurrence of the same key opens a fresh row rather than
            # reviving the old one.
            models.UniqueConstraint(
                fields=["kind", "dedupe_key"],
                condition=models.Q(status=AlertStatus.OPEN),
                name="uq_alert_open_kind_dedupe",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind_label}: {self.title}"


class AlertPolicy(TimeStampedModel):
    """The configurable thresholds one alert kind checks against (Rule 12).

    One row per kind. A migration gives each a starting number; an owner
    retunes it in the admin, never in code.
    """

    kind = models.CharField(max_length=32, unique=True, choices=AlertKind.choices)
    thresholds_days = ArrayField(
        models.IntegerField(),
        help_text="Days that fire this kind. In-transit aging has one (days "
        "overdue); return window has three (days left: 30/15/7).",
    )

    class Meta:
        db_table = "alerts_policy"
        ordering = ["kind"]
        verbose_name_plural = "alert policies"

    def __str__(self) -> str:
        return f"{self.kind} policy"


class AlertSeen(TimeStampedModel):
    """When this person last read their alerts (#226).

    The bell badges two counts side by side, and they clear in two different
    ways: an approval clears because somebody *decided* it, which the approval
    row already records, and an alert clears because somebody *read* it, which
    nothing recorded until now. Hence one stamp per person.

    Server-side rather than in the browser on purpose: a count kept in
    localStorage would come back on the next device and again after every
    logout, and "you have eleven unread" would be a different sentence on the
    phone and on the desk.

    Living in `alerts` rather than as a column on `accounts.User` is ADR-0002 -
    the stamp is alerts semantics, and `accounts` should not grow a cursor for
    every module that wants one. There is no backfill: an absent row means
    "never read", which is exactly right for everyone who exists today.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="alert_seen",
        help_text="One stamp per person; it dies with the account.",
    )
    seen_at = models.DateTimeField(
        help_text="Server clock at the moment they opened the Alerts tab - a "
        "client clock is not trusted to decide what counts as unread."
    )

    class Meta:
        db_table = "alerts_seen"

    def __str__(self) -> str:
        return f"{self.user} read alerts at {self.seen_at:%Y-%m-%d %H:%M}"


# Goods-v1 exceptions and notifications (design §5.7), registered with this app.
from alerts.goods_models import (  # noqa: E402, F401
    ExceptionEvent,
    GoodsException,
    GoodsNotification,
    NotificationAcknowledgement,
)
