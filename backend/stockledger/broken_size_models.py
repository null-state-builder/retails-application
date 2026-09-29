"""Broken-size rules and alerts (store operations ticket 32, ST-INV-1).

* ``SizeRule`` - one category's core sizes and the share of them that may be
  missing before a style-colour is broken (40% to start). Set by a master-data
  steward; every change is one audited command with the rule before and after.
  Removing a rule switches it off; the row stays.
* ``BrokenSizeAlert`` - one style-colour at one store that is broken, from the
  day the daily check first found it until the day it stopped being so. It
  records when somebody acted on it and when it closed, so the success measure
  "broken-size alerts acted on within 7 days" can be read from it. The check
  opens, refreshes and closes these; a person only records the action taken.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import TenantOwned


class SizeRule(TenantOwned):
    #: The category's ITEM value as the steward typed it; blank is "No category".
    category = models.CharField(max_length=120, blank=True)
    #: The category as compared (``broken_size.size_key``): "Shirt" and "SHIRT" are one rule.
    category_key = models.CharField(max_length=120, blank=True)
    #: The core sizes, in the order the steward listed them.
    core_sizes = models.JSONField(default=list)
    #: A style-colour missing this share (whole percent) or more of the core sizes is broken.
    missing_percent = models.PositiveSmallIntegerField(default=40)
    active = models.BooleanField(default=True)
    revision = models.IntegerField(default=1)
    updated_at = models.DateTimeField()

    class Meta:
        ordering = ["category"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "category_key"], name="uq_sizerule_category"),
            models.CheckConstraint(
                condition=models.Q(missing_percent__gte=1, missing_percent__lte=100),
                name="ck_sizerule_percent",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category or 'No category'}: {self.missing_percent}% of {self.core_sizes}"


class BrokenSizeAlert(TenantOwned):
    class Action(models.TextChoices):
        TRANSFER = "transfer", "Asked for a transfer of the missing sizes"
        MARKDOWN = "markdown", "Asked for a markdown"
        OTHER = "other", "Something else (see the note)"

    class Closed(models.TextChoices):
        FIXED = "fixed", "Enough core sizes are back"
        RULE_CHANGED = "rule_changed", "Its category's rule changed and it is not broken under it"
        SOLD_OUT = "sold_out", "The style-colour has no stock here now"
        NO_RULE = "no_rule", "Its category has no rule now"
        SWITCHED_OFF = "switched_off", "The feature was switched off at the store"
        NOT_CHECKED = (
            "not_checked",
            "The store is no longer checked (closed, or not on the goods records)",
        )

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    style = models.ForeignKey("masters.Style", on_delete=models.PROTECT, related_name="+")
    #: Blank where the item's identity has no colour.
    colour = models.CharField(max_length=120, blank=True)
    # What the store was told, as last checked: kept on the row so the page, size
    # balancing and markdown suggestions read the same thing.
    brand = models.CharField(max_length=120, blank=True)
    style_code = models.CharField(max_length=120)
    category = models.CharField(max_length=120, blank=True)
    core_sizes = models.JSONField(default=list)
    missing_sizes = models.JSONField(default=list)
    #: Pieces per size held here: {"S": 2, "M": 1}.
    held = models.JSONField(default=dict)
    pieces = models.PositiveIntegerField()
    #: The rule's share when last checked, and the share missing.
    rule_percent = models.PositiveSmallIntegerField()
    missing_percent = models.PositiveSmallIntegerField()
    opened_at = models.DateTimeField()
    checked_at = models.DateTimeField()
    acted_at = models.DateTimeField(null=True, blank=True)
    acted_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    action = models.CharField(max_length=12, choices=Action.choices, blank=True)
    action_note = models.CharField(max_length=240, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=16, choices=Closed.choices, blank=True)

    class Meta:
        ordering = ["opened_at"]
        constraints = [
            # One open alert per style-colour at a store. A closed one is history:
            # if it breaks again, a new alert opens with its own 7 days.
            models.UniqueConstraint(
                fields=["tenant", "site", "style", "colour"],
                condition=models.Q(closed_at__isnull=True),
                name="uq_brokensizealert_open",
            ),
            models.CheckConstraint(
                condition=models.Q(acted_at__isnull=True) | ~models.Q(action=""),
                name="ck_brokensizealert_action_named",
            ),
            models.CheckConstraint(
                condition=models.Q(closed_at__isnull=True) | ~models.Q(closed_reason=""),
                name="ck_brokensizealert_close_reason",
            ),
        ]
        indexes = [models.Index(fields=["site", "opened_at"])]

    def __str__(self) -> str:
        return f"{self.style_code} {self.colour} at {self.site_id}: missing {self.missing_sizes}"
