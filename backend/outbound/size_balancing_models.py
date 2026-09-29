"""Size-balancing suggestions (store operations ticket 34, ST-TRF-1).

``SizeBalanceSuggestion`` - one transfer the system suggests from one store to
another, to fill sizes the receiving store's broken-size alerts (ticket 32) say
it lacks, out of what the sending store holds beyond 8 weeks of its own sales.

A suggestion is not a request and moves nothing. The daily check makes, keeps
and withdraws them; a person approves one, and it becomes an ordinary transfer
request (``outbound.TransferRequest``), or rejects it with a reason. The row keeps
the suggestion and the human decision apart, with the rule and its settings that
made it (overall PRD R-AN-007). No cost is on it: store roles read it, so each
line carries its MRP only.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import TenantOwned


class SizeBalanceSuggestion(TenantOwned):
    class State(models.TextChoices):
        PENDING = "pending", "Waiting for a decision"
        APPROVED = "approved", "Approved: a transfer request was raised"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn by the daily check"

    class Withdrawn(models.TextChoices):
        REPLACED = "replaced", "The daily check found a different transfer between these stores"
        NOT_NEEDED = "not_needed", "The sizes are no longer missing, or no longer spare there"
        NOT_CHECKED = (
            "not_checked",
            "Switched off, or no longer checked, at one of the two stores",
        )

    #: The store that lacks the sizes: it receives, and its people decide.
    destination_site = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="+"
    )
    #: The store that holds them beyond 8 weeks of its own sales: it would send.
    source_site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: ``[{alert_id, sku_id, brand, style_code, colour, category, size, qty,
    #: mrp_paise, source_available, source_sold, destination_sold}]``: one row per
    #: item, each naming the broken-size alert it fills.
    lines = models.JSONField()
    pieces = models.PositiveIntegerField()
    #: The lines' pieces at MRP, where known.
    mrp_paise = models.BigIntegerField()
    #: Pieces on lines whose MRP is not recorded (not in ``mrp_paise``).
    mrp_unknown_pieces = models.PositiveIntegerField(default=0)
    #: Which rule made it, and with which settings (R-AN-007: source, version).
    rule = models.CharField(max_length=40)
    rule_settings = models.JSONField(default=dict)
    made_at = models.DateTimeField()
    checked_at = models.DateTimeField()
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    #: When it stopped waiting: a person decided, or the daily check withdrew it.
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=240, blank=True)
    withdrawn_reason = models.CharField(max_length=12, choices=Withdrawn.choices, blank=True)
    #: The ordinary transfer request an approval raised (the measured outcome).
    transfer_request = models.ForeignKey(
        "outbound.TransferRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        ordering = ["made_at"]
        constraints = [
            # One suggestion waiting between two stores at a time; the daily check
            # replaces it rather than adding a second.
            models.UniqueConstraint(
                fields=["tenant", "destination_site", "source_site"],
                condition=models.Q(state="pending"),
                name="uq_sizebalance_pending_pair",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_site=models.F("destination_site")),
                name="ck_sizebalance_two_stores",
            ),
            models.CheckConstraint(
                condition=~models.Q(state="approved") | models.Q(transfer_request__isnull=False),
                name="ck_sizebalance_approved_raises",
            ),
            models.CheckConstraint(
                condition=~models.Q(state="rejected") | ~models.Q(reason=""),
                name="ck_sizebalance_rejected_says_why",
            ),
            models.CheckConstraint(
                condition=~models.Q(state="withdrawn") | ~models.Q(withdrawn_reason=""),
                name="ck_sizebalance_withdrawn_says_why",
            ),
        ]
        indexes = [
            models.Index(fields=["destination_site", "state"]),
            models.Index(fields=["source_site", "state"]),
        ]

    def __str__(self) -> str:
        return f"{self.pieces} piece(s) {self.source_site_id} -> {self.destination_site_id}"
