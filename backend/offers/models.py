"""The rulebook (D5). One row per offer, in one open shape.

D5 Q1 rejected eight separate forms for eight patterns, and it rejected a
free-form rule script too. What is left is the middle: **trigger + reward +
dials**, where a new pattern is a new combination of values rather than a new
table and a release. That is Rule 12 applied to the thing that changes fastest
in this business - a brand can invent a mechanic in a WhatsApp message on a
Tuesday, and the counter has to price it on the Wednesday.

The consequence is that most of an offer lives in four JSON columns. They are not
a shrug: `offers/resolution.py` is the schema, written as an engine and pinned by
twelve golden carts, and the serializer below refuses a shape the engine could
not read. What the columns buy is that adding "buy 2 get 2" needed no migration.

Two rules about the row itself, both money-critical:

* **A live rule is never edited in place.** The till has already cached it, and a
  bill printed under yesterday's wording must stay explicable. Changing a running
  offer means ending it and starting another (`end_and_replace` below), which is
  the same documents-snapshot discipline every posted document in this system
  obeys.
* **A new store is never auto-enrolled** (D5 Q4). `store_scope` therefore holds a
  *list of stores*, resolved when the offer is written, even when the author
  chose "all". A wildcard evaluated at billing time would quietly opt a shop that
  opened last week into a promotion nobody costed it into.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.conf import settings
from django.db import models

from core.base import TimeStampedModel
from offers.resolution import Rule


class OfferQuerySet(models.QuerySet["Offer"]):
    def live_on(self, day: date) -> OfferQuerySet:
        """Rules a counter should be pricing with on `day`.

        `status` and the dates are two different questions and both are asked:
        an approved rule whose start date has not arrived is not running, and a
        live rule whose end date has passed is not running either, whoever last
        touched the row.
        """
        return self.filter(
            status=Offer.Status.LIVE,
            starts_on__lte=day,
        ).filter(models.Q(ends_on__isnull=True) | models.Q(ends_on__gte=day))

    def for_store(self, store_code: str) -> OfferQuerySet:
        return self.filter(store_scope__stores__contains=[store_code.upper()])


class Offer(TimeStampedModel):
    """One rule: what must happen, what the customer gets, and where it applies."""

    class Layer(models.TextChoices):
        BRAND = "brand", "Brand offer"
        STOREWIDE = "storewide", "Storewide add-on"
        BANK = "bank", "Bank / tender add-on"

    class Funder(models.TextChoices):
        BRAND = "brand", "Funded by the brand"
        KDPS = "kdps", "Funded by KDPS"

    class Trigger(models.TextChoices):
        NONE = "none", "No condition"
        SPEND = "spend", "Spend threshold"
        QTY = "qty", "Quantity threshold"
        GROUP = "group", "Buy X get Y"

    class Reward(models.TextChoices):
        PCT_OFF = "pct_off", "Percent off"
        AMT_OFF = "amt_off", "Amount off"
        ITEM_FREE = "item_free", "Item free"
        FIXED_PRICE = "fixed_price", "Fixed price"
        GIFT = "gift", "Gift"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        LIVE = "live", "Live"
        ENDED = "ended", "Ended"

    class Mode(models.TextChoices):
        REGULAR = "regular", "Regular"
        EOSS = "eoss", "End-of-season sale"

    name = models.CharField(max_length=160)
    mode = models.CharField(
        max_length=8,
        choices=Mode.choices,
        default=Mode.REGULAR,
        help_text="A tag for margin-attribution reporting only (D5): an EOSS offer "
        "runs through the exact same engine as any other. Nothing is reclaimed "
        "from a brand because a rule is tagged EOSS - it only marks the report.",
    )
    brand = models.ForeignKey(
        "masters.Brand",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="offers",
        help_text="Empty means the rule is not one brand's - a storewide or KDPS offer.",
    )
    funder = models.CharField(max_length=8, choices=Funder.choices, default=Funder.BRAND)
    layer = models.CharField(max_length=12, choices=Layer.choices, default=Layer.BRAND)

    trigger_type = models.CharField(max_length=8, choices=Trigger.choices, default=Trigger.NONE)
    trigger_config = models.JSONField(default=dict, blank=True)
    reward_type = models.CharField(max_length=12, choices=Reward.choices)
    reward_config = models.JSONField(default=dict, blank=True)
    item_scope = models.JSONField(default=dict, blank=True)
    store_scope = models.JSONField(
        default=dict,
        blank=True,
        help_text="{'kind': 'all'|'specific', 'stores': ['DEO', ...]} - always a list, "
        "resolved when the offer was written, so a new store is never auto-enrolled.",
    )

    starts_on = models.DateField()
    ends_on = models.DateField(
        null=True, blank=True, help_text="Empty means it rolls until somebody stops it (D5 Q5)."
    )
    combinable = models.BooleanField(
        default=False,
        help_text="Stacking is opt-in, per offer. Every new offer defaults to non-combining.",
    )
    priority = models.IntegerField(
        default=100, help_text="Lower wins a tie on rupees. Then the id does."
    )
    is_fallback = models.BooleanField(
        default=False, help_text="The named default that fills a gap in a brand's timeline (D5 Q5)."
    )

    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offers_approved",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offers_authored",
    )
    #: What this rule replaced, when it was written to end-and-replace a live one.
    replaces = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replaced_by"
    )

    objects: models.Manager[Offer] = OfferQuerySet.as_manager()

    class Meta:
        ordering = ["priority", "id"]
        indexes = [
            models.Index(fields=["layer", "starts_on", "ends_on"], name="offers_window_idx"),
            models.Index(fields=["brand"], name="offers_brand_idx"),
            # The dataset delta reads this and nothing else on the busy path.
            models.Index(fields=["updated_at"], name="offers_synced_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_on__isnull=True)
                | models.Q(ends_on__gte=models.F("starts_on")),
                name="offer_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="live") | models.Q(approved_by__isnull=False),
                name="offer_live_only_after_approval",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug convenience
        return f"{self.name} ({self.get_layer_display()})"

    @property
    def brand_name(self) -> str:
        return self.brand.name if self.brand is not None else ""

    def as_rule(self) -> Rule:
        """This row in the engine's own terms - the one place that mapping lives.

        Two callers with different reasons: the accept pipeline re-prices a bill
        against the rules that were running when it printed, and the vector
        loader turns the same shape back out of JSON. A second copy of this
        mapping is a field that silently stops reaching the engine.
        """
        return Rule(
            id=self.id,
            name=self.name,
            layer=self.layer,
            brand=self.brand_name,
            trigger_type=self.trigger_type,
            trigger_config=self.trigger_config or {},
            reward_type=self.reward_type,
            reward_config=self.reward_config or {},
            item_scope=self.item_scope or {},
            starts_on=self.starts_on,
            ends_on=self.ends_on,
            combinable=self.combinable,
            priority=self.priority,
        )

    def as_rule_payload(self) -> dict[str, Any]:
        """The rule as it rides to the till, and as the server re-reads it.

        Exactly the fields `offers.resolution.Rule` needs and not one more. The
        till is a shop-floor device: it gets the rulebook, never the margin
        behind it, which is why `funder` is absent here and present on the
        authoring screen.
        """
        return {
            "id": self.id,
            "name": self.name,
            "layer": self.layer,
            "brand": self.brand_name,
            "trigger_type": self.trigger_type,
            "trigger_config": self.trigger_config or {},
            "reward_type": self.reward_type,
            "reward_config": self.reward_config or {},
            "item_scope": self.item_scope or {},
            "starts_on": self.starts_on.isoformat(),
            "ends_on": self.ends_on.isoformat() if self.ends_on else None,
            "combinable": self.combinable,
            "priority": self.priority,
        }


# ---------------------------------------------------------------------------
# EOSS planning - a sell-through-triggered markdown ladder (D5, docs/05-offers).
#
# EOSS is not a new discount mechanic: "the same offer patterns, tagged" is the
# whole design. What was missing was the *decision* in front of it - when is a
# style behind its clearance target, how deep should the markdown go, and is
# there still margin room to give it. These three models are that decision,
# kept deliberately rule-based (no ML): a per-brand sell-through target curve,
# a per-brand markdown ladder, and the recommendation a human approves or
# rejects before it ever becomes a live `Offer`.
# ---------------------------------------------------------------------------


class SellThroughTarget(TimeStampedModel):
    """What percent of a brand's buy should be sold by week N of the season.

    `brand=None` is the network default curve, read by any brand that has not
    negotiated its own. A shallow, early curve (few tables) is preferred to a
    steep one - the research behind this slice found late, deep markdowns cost
    more margin than early, shallow ones.
    """

    brand = models.ForeignKey(
        "masters.Brand",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="sell_through_targets",
        help_text="Empty = the network default curve.",
    )
    week_number = models.PositiveIntegerField(help_text="Weeks since the style first arrived.")
    target_pct = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ["brand_id", "week_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["brand", "week_number"], name="uq_sellthrough_brand_week"
            ),
        ]

    def __str__(self) -> str:
        brand_code = self.brand.code if self.brand else "default"
        return f"{brand_code} wk{self.week_number} → {self.target_pct}%"


class EossLadderStep(TimeStampedModel):
    """One rung of a brand's markdown ladder: past this trigger, offer this much.

    Ordered by `step_no`; the engine walks the ladder and takes the deepest
    rung whose trigger the style has actually crossed - "act early and shallow"
    is enforced by the data (small steps first), never by code.
    """

    class Trigger(models.TextChoices):
        SELL_THROUGH_GAP = "gap", "Points behind the sell-through target"
        AGE_WEEKS = "age", "Weeks in stock"

    brand = models.ForeignKey(
        "masters.Brand",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="eoss_ladder_steps",
        help_text="Empty = the network default ladder.",
    )
    step_no = models.PositiveIntegerField()
    trigger_type = models.CharField(
        max_length=8, choices=Trigger.choices, default=Trigger.SELL_THROUGH_GAP
    )
    trigger_value = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text=(
            "Points behind target (gap) or weeks in stock (age), whichever trigger_type says."
        ),
    )
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ["brand_id", "step_no"]
        constraints = [
            models.UniqueConstraint(fields=["brand", "step_no"], name="uq_eoss_ladder_brand_step"),
        ]

    def __str__(self) -> str:
        brand_code = self.brand.code if self.brand else "default"
        return f"{brand_code} step {self.step_no} → {self.discount_pct}%"


class EossRecommendation(TimeStampedModel):
    """One style-colour's markdown recommendation for one season.

    Recomputed by `offers.eoss_engine.generate_recommendations` from real stock
    and sales positions - never typed by hand. A row already decided (approved
    or rejected) is left alone by the next run, so re-running never overwrites
    a human's call.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    season = models.ForeignKey(
        "masters.Season", on_delete=models.CASCADE, related_name="eoss_recommendations"
    )
    brand = models.ForeignKey(
        "masters.Brand",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="eoss_recommendations",
    )
    design = models.CharField(max_length=120, blank=True, default="")
    color = models.CharField(max_length=60, blank=True, default="")
    item = models.CharField(max_length=120, blank=True, default="")

    weeks_in_stock = models.IntegerField(default=0)
    on_hand_qty = models.IntegerField(default=0)
    sold_qty = models.IntegerField(default=0)
    sell_through_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    target_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    gap_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    broken_size_run = models.BooleanField(default=False)
    margin_floor_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="The deepest discount possible before the price falls below unit cost.",
    )
    recommended_discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    reason = models.TextField(blank=True, default="")

    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    decided_discount_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    offer = models.ForeignKey(
        Offer, null=True, blank=True, on_delete=models.SET_NULL, related_name="eoss_recommendation"
    )

    class Meta:
        ordering = ["-gap_pct", "design"]
        constraints = [
            models.UniqueConstraint(
                fields=["season", "brand", "design", "color"], name="uq_eoss_reco_style"
            ),
        ]
        indexes = [
            models.Index(fields=["season", "status"], name="eoss_reco_season_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.design} {self.color} ({self.season.code}) → {self.recommended_discount_pct}%"
