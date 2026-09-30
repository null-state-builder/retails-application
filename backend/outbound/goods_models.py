"""Goods-v1 transfers, movements and non-trading counts (design §3.3, §5.2).

A transfer's plan is its transfer PT: approval reserves exact source portions,
dispatch consumes that reservation into transit, and destination checking and
acceptance happen in repeatable partial steps that keep every original
allocation's evidence. Movements (bin moves, holds, releases, adjustments,
write-offs, shrinkage, RTV) and counts post through the same operational engine.
Legacy transfer, movement and stocktake rows remain history.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned
from core.goods_fields import PortionField


class GoodsTransfer(TenantOwned):
    class State(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"
        APPROVED = "approved"
        DISPATCHING = "dispatching"
        COMPLETED = "completed"
        CANCELLED = "cancelled"

    class Custody(models.TextChoices):
        #: Accepted, good, unheld, unreserved stock (R-INV-004's ordinary source).
        ORDINARY = "ordinary"
        #: Recorded stock standing in the source's quarantine under a hold: the
        #: controlled custody transfer of overall PRD §15.2.1 rule 10 (goods
        #: ticket 13D). It stays held at every site and is never made available.
        QUARANTINE = "quarantine"
        #: Damaged pre-PT custody identified from its GRN, moving between a store
        #: and a warehouse (overall PRD §15.2.1 rule 10, goods ticket 13E). No PT
        #: covers it, so it has no SKU guarantee, cost or value; it travels on the
        #: transfer's own document, never on a transfer PT, and stays held.
        PRE_PT = "pre_pt", "Pre-PT custody"

    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="goods_transfer"
    )
    #: Which source pool the movement takes from, fixed when it is drafted.
    custody = models.CharField(max_length=12, choices=Custody.choices, default=Custody.ORDINARY)
    source_site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    destination_site = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="+"
    )
    #: Where this movement has got to (OPS-06). The request that may have asked
    #: for it is its own record below, because a request is raised by the
    #: receiving site and may be refused, drafted differently, or never drafted
    #: at all - it is not an early state of a transfer that already exists.
    state = models.CharField(max_length=12, choices=State.choices, default=State.DRAFT)
    corrective_for = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="corrections"
    )
    transport = models.JSONField(default=dict)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    # E-way evidence belongs to each shipment, not to the movement (goods
    # ticket 13B): see ``TransferDispatch.eway_at_dispatch``.

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(source_site=models.F("destination_site")),
                name="ck_goodstransfer_sites",
            ),
        ]
        indexes = [
            models.Index(fields=["source_site"]),
            models.Index(fields=["destination_site"]),
            models.Index(fields=["state"]),
        ]


class TransferRequest(TenantOwned):
    """A site asking for stock from another site (OPS-06; transfers PRD §2).

    A request is not a movement. Nothing is reserved, nothing moves, and the
    site that would send the goods is free to draft something different or
    nothing at all. It exists so the asking is a record with an author and a
    time rather than a phone call, and so the draft it becomes can point back
    at what was actually asked for.
    """

    class State(models.TextChoices):
        OPEN = "open"
        DRAFTED = "drafted"
        CLOSED = "closed"

    source_site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    destination_site = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="+"
    )
    requested_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    requested_at = models.DateTimeField()
    #: ``[{line_key, sku_id, qty, note}]`` exactly as the asking site wrote it.
    lines = models.JSONField()
    note = models.CharField(max_length=500, null=True, blank=True)
    state = models.CharField(max_length=8, choices=State.choices, default=State.OPEN)
    transfer = models.ForeignKey(
        GoodsTransfer, null=True, blank=True, on_delete=models.PROTECT, related_name="requests"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(source_site=models.F("destination_site")),
                name="ck_transferrequest_sites",
            ),
        ]
        indexes = [
            models.Index(fields=["source_site", "state"]),
            models.Index(fields=["destination_site", "state"]),
        ]


class TransferDispatch(TenantOwned):
    """One actual shipment against one approved transfer (transfers PRD §4, §5).

    Several of these may fulfil one approved movement. Each carries its own
    departure evidence, its own whole-shipment count at the destination and its
    own outcome, and none of them is closed by anything that happens to another.

    ``lines`` freezes what physically left: per approved line, the exact lot
    portions consumed out of reservation, the location each piece stood at, and
    its origin - so a return to source can put every piece back where it was
    and a count can never be told a quantity the shipment did not carry.
    """

    class State(models.TextChoices):
        IN_TRANSIT = "in_transit"
        COUNTED = "counted"
        ACCEPTED = "accepted"
        #: Some of the shipment is back at the source and the rest is still
        #: unaccounted for - on the road, or lost - and stays in transit as
        #: owned, unresolved work (goods ticket 13C). Never counted at the
        #: destination: a delivery that failed is not also received.
        PARTLY_RETURNED = "partly_returned"
        RETURNED_TO_SOURCE = "returned_to_source"

    transfer = models.ForeignKey(GoodsTransfer, on_delete=models.PROTECT, related_name="dispatches")
    sequence_no = models.IntegerField()
    dispatched_at = models.DateTimeField()
    recorded_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    transport = models.JSONField(default=dict)
    lines = models.JSONField()
    state = models.CharField(max_length=20, choices=State.choices, default=State.IN_TRANSIT)
    #: Whether an e-way reference went with the shipment when it left (goods
    #: ticket 13B; transfers PRD §8). Written once, at dispatch, and never
    #: rewritten: a reference attached later is its own event and does not turn
    #: "not present at dispatch" into "present". Null only for a shipment that
    #: left before this was recorded at all.
    eway_at_dispatch = models.CharField(
        max_length=12,
        choices=[("present", "present"), ("not_present", "not_present")],
        null=True,
        blank=True,
    )
    #: The physical arrival at the destination (design E147), recorded as its own
    #: step. It releases nothing: the pieces stay in transit until the count.
    arrived_at = models.DateTimeField(null=True, blank=True)
    arrival_recorded_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    counted_at = models.DateTimeField(null=True, blank=True)
    counted_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: What the destination actually found, per line, and what was missing.
    #: Written once by the count and never rewritten.
    count = models.JSONField(default=dict, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    #: When the last piece of the shipment was back at the source.
    returned_at = models.DateTimeField(null=True, blank=True)
    #: Why the delivery failed, as the first return receipt said it.
    return_reason = models.CharField(max_length=500, null=True, blank=True)
    #: How many pieces the return receipts have brought back so far (goods
    #: ticket 13C). What is left - quantity less this - is still in transit.
    returned_qty = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "sequence_no"], name="uq_transferdispatch_sequence"
            ),
        ]
        indexes = [models.Index(fields=["transfer", "state"]), models.Index(fields=["state"])]


class DispatchPreparation(TenantOwned):
    """The scanning that decides what one shipment carries (design E242-E244, GSA-T13).

    Goods ticket 13B adapts the design's full-movement preparation to the
    selected shipment: the source site scans the pieces it is about to send,
    and the dispatch that follows carries exactly what was scanned - no more,
    and never beyond what is still reserved. Preparing moves no stock and
    reserves nothing; it is evidence of who scanned what, and when.

    One preparation is open per transfer at a time, bound to the exact approved
    version it was opened against. Anyone with the dispatch grant at the
    source may resume it from another device. It ends one of two ways and is
    never deleted: ``dispatched`` (it became ``dispatch``) or ``invalidated``
    (the balance was cancelled, the approval it was bound to is no longer the
    live one, or somebody started the shipment again).
    """

    class State(models.TextChoices):
        OPEN = "open"
        DISPATCHED = "dispatched"
        INVALIDATED = "invalidated"

    transfer = models.ForeignKey(
        GoodsTransfer, on_delete=models.PROTECT, related_name="preparations"
    )
    source_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=12, choices=State.choices, default=State.OPEN)
    revision = models.IntegerField(default=1)
    content_hash = models.CharField(max_length=64)
    opened_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    opened_at = models.DateTimeField()
    last_activity_at = models.DateTimeField()
    dispatch = models.OneToOneField(
        TransferDispatch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="preparation",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    invalidated_reason = models.CharField(max_length=40, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transfer"],
                condition=models.Q(state="open"),
                name="uq_dispatchpreparation_one_open",
            ),
        ]
        indexes = [models.Index(fields=["transfer", "state"])]


class DispatchScan(EvidenceRow):
    """One acknowledged scan into a dispatch preparation. Never rewritten.

    ``scan_key`` binds the whole observation: sending the same key again with
    the same content counts once, and with different content is refused.
    ``origin_id`` is set only when the scanner named the origin; otherwise the
    dispatch takes the oldest reserved pieces of the line, and the shipment's
    own frozen lines still record the exact origin of every piece that left.
    """

    preparation = models.ForeignKey(
        DispatchPreparation, on_delete=models.PROTECT, related_name="scans"
    )
    scan_key = models.UUIDField()
    line_key = models.UUIDField()
    origin_id = models.UUIDField(null=True, blank=True)
    qty = models.IntegerField()
    alias_value = models.CharField(max_length=128)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["preparation", "scan_key"], name="uq_dispatchscan_key"),
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_dispatchscan_qty"),
        ]
        indexes = [models.Index(fields=["preparation", "recorded_at"])]


class TransferReturn(EvidenceRow):
    """One actual receipt at the source of goods from a failed delivery (goods ticket 13C).

    Transfers PRD §6: what came back is recorded against the original movement
    and shipment - how many of each line, in what condition, received at which
    site, by whom, when it happened and when it was recorded. Only what is
    physically back is accounted for; anything the receipt does not name stays
    in transit under the transfer, unresolved. Never rewritten.

    ``receipt_key`` binds the whole receipt: sending the same key again with the
    same content has one effect, and with different content is refused.
    """

    dispatch = models.ForeignKey(TransferDispatch, on_delete=models.PROTECT, related_name="returns")
    receipt_key = models.UUIDField()
    #: Where the goods physically came back to. Always the transfer's source.
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=500)
    #: The source site's own receiving evidence: a gate entry, a returned LR.
    evidence_reference = models.CharField(max_length=100, null=True, blank=True)
    note = models.CharField(max_length=500, null=True, blank=True)
    #: ``[{line_key, good, damaged}]`` exactly as the source found them.
    lines = models.JSONField()
    quantity = models.IntegerField()
    content_hash = models.CharField(max_length=64)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["dispatch", "receipt_key"], name="uq_transferreturn_key"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="ck_transferreturn_qty"
            ),
        ]
        indexes = [models.Index(fields=["dispatch", "recorded_at"])]


class TransferEvent(EvidenceRow):
    class Kind(models.TextChoices):
        SUBMITTED = "submitted"
        APPROVED = "approved"
        DISPATCH = "dispatch"
        ARRIVAL = "arrival"
        CHECK = "check"
        ACCEPT = "accept"
        PUTAWAY = "putaway"
        RETURNED = "returned"
        COMPLETED = "completed"
        EWAY_ADDED = "eway_added"
        EWAY_VERIFIED = "eway_verified"
        CANCELLED = "cancelled"
        #: A damage report from this transfer's arrival count was rejected (ticket 12B).
        DAMAGE_REJECTED = "damage_rejected"
        #: Goods ticket 14: the destination proposed that missing pieces of one
        #: counted shipment are a transit shortage, and the Owner decided it.
        SHORTAGE_PROPOSED = "shortage_proposed"
        SHORTAGE_RESOLVED = "shortage_resolved"
        SHORTAGE_REJECTED = "shortage_rejected"
        #: Goods ticket 16: excess observed at the destination (goods nobody sent,
        #: or wrong goods in place of expected ones) is resolved by a corrective
        #: transfer from the source - proposed with its source evidence, matched
        #: to the observation on its source-confirmed dispatch/arrival pair, or
        #: withdrawn when its outstanding balance is cancelled before that.
        CORRECTIVE_PROPOSED = "corrective_proposed"
        CORRECTIVE_MATCHED = "corrective_matched"
        CORRECTIVE_WITHDRAWN = "corrective_withdrawn"

    transfer = models.ForeignKey(GoodsTransfer, on_delete=models.PROTECT, related_name="events")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    actual_at = models.DateTimeField()
    details = models.JSONField()
    journal_batch = models.ForeignKey(
        "stockledger.JournalBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["transfer", "recorded_at"])]


class GapResolution(TenantOwned):
    """One proposed resolution of a transfer gap, and the decision on it (design E149).

    Goods ticket 14 builds the ``short`` kind for a counted shipment: the
    destination proposes that named missing pieces of one shipment are a transit
    shortage, with its check, reason, evidence and follow-up; a different person
    (the Owner, GSA-R01) approves or rejects it. Until approval the pieces stay
    in transit. Approval numbers the GAP document and posts P13 against exactly
    the frozen ranges of the original shipment - nothing else, and only once.

    Goods ticket 16 builds the ``excess`` kind: the source proposes a corrective
    transfer (``corrective_transfer``) for an exact interval of an excess
    observation the count recorded, the Owner's approval of that transfer
    numbers this GAP document too, and the corrective confirmation matches the
    interval once (``stockledger.CustodyMatch``). A wrong-goods observation's
    short-expected and excess-observed decisions share one ``pairing_key``.
    """

    class Kind(models.TextChoices):
        SHORT = "short"
        DAMAGE = "damage"
        EXCESS = "excess"

    class State(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        #: Goods ticket 16: an excess-observed decision whose corrective transfer
        #: had its outstanding balance cancelled before it was confirmed. Nothing
        #: was matched; the observation is free for another route.
        WITHDRAWN = "withdrawn"

    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="gap_resolution"
    )
    transfer = models.ForeignKey(
        GoodsTransfer, on_delete=models.PROTECT, related_name="gap_resolutions"
    )
    kind = models.CharField(max_length=8, choices=Kind.choices)
    pairing_key = models.UUIDField(null=True, blank=True)
    payload = models.JSONField()
    related_movement = models.ForeignKey(
        "core.DocumentIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    corrective_transfer = models.ForeignKey(
        GoodsTransfer, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The one shipment whose missing pieces this resolves (goods ticket 14).
    dispatch = models.ForeignKey(
        TransferDispatch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="gap_resolutions",
    )
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    #: The frozen quantity the proposal names; its exact ranges are in ``payload``.
    quantity = models.IntegerField(default=0)
    proposed_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    proposed_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["transfer", "pairing_key"]),
            models.Index(fields=["dispatch", "state"]),
        ]


class GoodsMovement(TenantOwned):
    class Kind(models.TextChoices):
        BIN_MOVE = "bin_move"
        HOLD = "hold"
        RELEASE = "release"
        ADJUSTMENT_UP = "adjustment_up"
        ADJUSTMENT_DOWN = "adjustment_down"
        WRITEOFF = "writeoff"
        SHRINKAGE = "shrinkage"
        RTV = "rtv"
        TRANSIT_SHORTAGE = "transit_shortage"
        RTV_CANCEL = "rtv_cancel"
        #: Goods ticket 15D: quarantined pieces actually destroyed or handed over
        #: for scrap/recycling, recorded and approved as one numbered disposal.
        DISPOSAL = "disposal"

    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="goods_movement"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    source_document = models.ForeignKey(
        "core.DocumentIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    count = models.ForeignKey(
        "outbound.GoodsStocktake",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="movements",
    )
    #: Where a return to vendor stands (quarantine outcomes PRD §4, goods ticket
    #: 15B). Null until the Owner approves it: a draft or submitted RTV is only a
    #: proposal. ``initiated`` while any approved balance still waits to leave;
    #: ``completed`` once every approved piece was handed over; ``closed_partially_returned``
    #: once part left and the whole remaining balance was explicitly withdrawn;
    #: ``cancelled`` once the whole balance was withdrawn before anything left.
    #: A shipment still awaiting vendor receipt, or with an unresolved
    #: acknowledgement shortfall, keeps it ``initiated`` (goods ticket 15F).
    rtv_state = models.CharField(
        max_length=30,
        choices=[
            ("initiated", "initiated"),
            ("completed", "completed"),
            ("closed_partially_returned", "closed_partially_returned"),
            ("cancelled", "cancelled"),
        ],
        null=True,
        blank=True,
    )


class RtvEvent(EvidenceRow):
    """One physical or balance fact on an approved return to vendor (goods ticket 15B).

    A ``pickup`` is a confirmed handover to the vendor or its representative:
    who collected, when it actually happened (``event_at``), when it was recorded,
    the exact pieces that left per line and the evidence supplied, plus the
    reasons recorded against every piece left behind (quarantine outcomes §5). A
    ``withdrawal`` explicitly takes pieces off the pending balance, releasing only
    their reservation, with its reason. A reason recorded at a pickup never
    withdraws anything by itself. Never rewritten.

    Goods ticket 15F adds the shipped route. A ``shipment`` is one dispatch for
    delivery to the vendor: its pieces leave exactly as a pickup's do, but
    departure is not vendor receipt. Every later fact about that shipment names
    it (``shipment``): an ``acknowledgement`` is one evidence-backed, cumulative
    snapshot of what the vendor says it received per line (posting nothing); a
    ``source_return`` records pieces that actually came back to the source after
    a failed delivery, with their condition; a ``putaway`` accepts returned good
    pieces back into storage; ``eway_attached`` and ``eway_verified`` are the
    shipment's movement-document evidence, kept apart from physical completion.

    Goods ticket 15H (goods PRD §14.10 GSA-R07) closes a persistent vendor
    acknowledgement shortfall. A ``shortfall_proposal`` is the prepared closure
    of one short-acknowledged shipment - the exact unacknowledged pieces, their
    value at recorded layer cost, the reason and evidence - waiting for the
    Owner's approval; it moves nothing. A ``shortfall_closure`` is that approval
    applied: exactly those pieces leave the ``returned`` boundary for
    ``consumed`` as a recognised shortfall, once per shipment.
    """

    class Kind(models.TextChoices):
        PICKUP = "pickup"
        WITHDRAWAL = "withdrawal"
        SHIPMENT = "shipment"
        ACKNOWLEDGEMENT = "acknowledgement"
        SOURCE_RETURN = "source_return"
        PUTAWAY = "putaway"
        EWAY_ATTACHED = "eway_attached"
        EWAY_VERIFIED = "eway_verified"
        SHORTFALL_PROPOSAL = "shortfall_proposal"
        SHORTFALL_CLOSURE = "shortfall_closure"

    movement = models.ForeignKey(GoodsMovement, on_delete=models.PROTECT, related_name="rtv_events")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    #: The shipment a later fact is about (goods ticket 15F); null otherwise.
    shipment = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="shipment_facts"
    )
    sequence_no = models.IntegerField()
    quantity = models.IntegerField()
    #: ``[{line_key, qty, portions: [{lot_id, lower, upper, origin_id}]}]``. A
    #: shipment's portions also keep each piece's ``condition``,
    #: ``source_location_id`` and the ``holds`` that ended as it left; a source
    #: return's lines add ``good``/``damaged`` and each portion's new address; an
    #: acknowledgement's lines are ``{line_key, qty}`` only.
    lines = models.JSONField()
    #: Pickup: ``collected_by``, ``evidence_reference``, ``evidence_ids``,
    #: ``evidence_note``, ``left_behind``. Withdrawal: ``reason``, ``remark``.
    #: Shipment: ``carrier``, evidence, ``eway_reference``, ``eway_at_dispatch``.
    #: Acknowledgement: ``recipient_reference``, evidence, ``shortfall_qty``.
    #: Source return: ``reason``, evidence, ``damage_report_id``. E-way: ``reference``,
    #: ``note``. Shortfall proposal and closure: ``reason``, evidence,
    #: ``reviewed_hash``, ``value_paise`` (null when unknown), ``value_basis``,
    #: ``approval_request_id``; a closure adds ``proposal_id`` and ``prepared_by``.
    details = models.JSONField()
    journal_batch = models.ForeignKey(
        "stockledger.JournalBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["movement", "sequence_no"], name="uq_rtvevent_sequence"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0)
                | models.Q(
                    kind__in=["acknowledgement", "eway_attached", "eway_verified"],
                    quantity=0,
                ),
                name="ck_rtvevent_qty",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=[
                        "acknowledgement",
                        "source_return",
                        "putaway",
                        "eway_attached",
                        "eway_verified",
                        "shortfall_proposal",
                        "shortfall_closure",
                    ],
                    shipment__isnull=False,
                )
                | models.Q(kind__in=["pickup", "withdrawal", "shipment"], shipment__isnull=True),
                name="ck_rtvevent_shipment_link",
            ),
            # Goods ticket 15H: a shipment's shortfall is recognised at most once.
            models.UniqueConstraint(
                fields=["shipment"],
                condition=models.Q(kind="shortfall_closure"),
                name="uq_rtvevent_one_shortfall_closure",
            ),
        ]
        indexes = [models.Index(fields=["movement", "recorded_at"])]


class DamageReport(TenantOwned):
    """One report that goods are damaged, waiting for a second person to decide it.

    Reporting damage already quarantines the goods - the hold is placed by the
    command that reported them, and availability drops in that same transaction.
    This row is the evidence that a report is *open*: who said it, over which
    pieces, and what a different authorised person later decided about it.

    Exactly one source: the mark-damaged movement that reported it, the
    receiving damage disposition that did, or - since OPS-06 - the transfer
    dispatch whose destination count found the damage. ``release_movement`` is filled only
    when a rejection gave the quantity back, and the report itself is never
    rewritten - its state, reviewer, time and reason are appended to it.

    ``closed`` is not a review decision: nobody confirmed or rejected the damage.
    A pending report is closed when every piece it covers has gone back to the
    vendor on an RTV pickup (Anand's 15B decision 3), with no reviewer and a
    "Returned to vendor" note in ``review_reason``.
    """

    class State(models.TextChoices):
        PENDING = "pending"
        CONFIRMED = "confirmed"
        REJECTED = "rejected"
        CLOSED = "closed"

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    movement = models.ForeignKey(
        "core.DocumentIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    disposition = models.ForeignKey(
        "inbound.Disposition", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    dispatch = models.ForeignKey(
        TransferDispatch, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reporter = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    reported_at = models.DateTimeField()
    quantity = models.IntegerField()
    #: The lots, SKUs and exact portions the report covers, as the reporting
    #: command froze them, with the address each piece had before it was
    #: quarantined. A rejection gives that address back; nothing is re-selected.
    lines = models.JSONField()
    reason_code = models.CharField(max_length=60)
    evidence_id = models.UUIDField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    reviewer = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.CharField(max_length=500, null=True, blank=True)
    release_movement = models.ForeignKey(
        "core.DocumentIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="ck_damagereport_quantity"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    movement__isnull=False, disposition__isnull=True, dispatch__isnull=True
                )
                | models.Q(movement__isnull=True, disposition__isnull=False, dispatch__isnull=True)
                | models.Q(movement__isnull=True, disposition__isnull=True, dispatch__isnull=False),
                name="ck_damagereport_one_source",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "state"]),
            models.Index(fields=["state", "reported_at"]),
        ]


class GoodsStocktake(TenantOwned):
    class State(models.TextChoices):
        REQUESTED = "requested"
        OPEN = "open"
        REVIEW = "review"
        CLOSED = "closed"
        CANCELLED = "cancelled"

    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="goods_stocktake"
    )
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    scope = models.JSONField()
    non_trading_event = models.ForeignKey(
        "masters.SiteCapabilityEvent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    till_pause_evidence = models.JSONField(default=dict)
    frozen_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.REQUESTED)
    last_activity_at = models.DateTimeField()
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            # Goods ticket 17: one site, one count at a time. The site guard's
            # freeze is a single value, so a second unfinished count at the same
            # site could only ever share - and then release - the first one's.
            models.UniqueConstraint(
                fields=["site"],
                condition=models.Q(state__in=("requested", "open", "review")),
                name="uq_stocktake_one_unfinished",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "state"]),
            models.Index(fields=["state", "last_activity_at"]),
        ]


class GoodsCountPass(TenantOwned):
    class State(models.TextChoices):
        OPEN = "open"
        SUBMITTED = "submitted"
        SELECTED = "selected"
        SUPERSEDED = "superseded"

    stocktake = models.ForeignKey(GoodsStocktake, on_delete=models.PROTECT, related_name="passes")
    counter = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    entry_user = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    pass_no = models.IntegerField()
    replaces = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    scope = models.JSONField(default=dict)
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    observation_hash = models.CharField(max_length=64, null=True, blank=True)
    revision = models.IntegerField(default=1)
    #: Goods ticket 17. When the counter last did anything to this pass; a pass
    #: idle for 24 hours is stale and needs an explicit resume (GSA-T17).
    last_activity_at = models.DateTimeField(null=True, blank=True)
    #: Set when the stale check found the pass idle and opened its owned work;
    #: cleared by an explicit resume. Never completes the pass.
    stale_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    #: Why a recount was asked for (a recount pass only).
    reason_code = models.CharField(max_length=60, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["stocktake", "counter", "pass_no"], name="uq_countpass_counter_no"
            ),
            # A counter carries one unfinished pass of a count at a time, so
            # "continue my count" always has exactly one answer.
            models.UniqueConstraint(
                fields=["stocktake", "counter"],
                condition=models.Q(state="open"),
                name="uq_countpass_one_open",
            ),
        ]
        indexes = [models.Index(fields=["stocktake", "state"])]


class CountAffirmation(EvidenceRow):
    """GSA-T17: "I have counted the whole assigned area", bound to what was counted.

    Appended once, when the pass is submitted. It names the pass, the person, the
    hash of the pass's assigned scope and the hash and revision of the exact
    observations the counter reviewed - so an affirmation can never be carried
    onto a later scan, a resumed pass or a recount, each of which needs its own.
    """

    count_pass = models.ForeignKey(
        GoodsCountPass, on_delete=models.PROTECT, related_name="affirmations"
    )
    counter = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    scope_hash = models.CharField(max_length=64)
    observation_hash = models.CharField(max_length=64)
    observation_revision = models.IntegerField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["count_pass"], name="uq_countaffirmation_pass"),
        ]


class CountSnapshot(EvidenceRow):
    stocktake = models.ForeignKey(GoodsStocktake, on_delete=models.PROTECT, related_name="snapshot")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    lot = models.ForeignKey("stockledger.CustodyLot", on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    address = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["stocktake", "lot", "portion"], name="uq_countsnapshot_portion"
            ),
        ]


class CountDecision(EvidenceRow):
    stocktake = models.ForeignKey(
        GoodsStocktake, on_delete=models.PROTECT, related_name="decisions"
    )
    observation_hash = models.CharField(max_length=64)
    variances = models.JSONField()
    movement = models.ForeignKey(
        "core.DocumentIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approver = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    stale_reason = models.CharField(max_length=500, null=True, blank=True)


class CountSelection(EvidenceRow):
    decision = models.ForeignKey(CountDecision, on_delete=models.PROTECT, related_name="selections")
    count_pass = models.ForeignKey(GoodsCountPass, on_delete=models.PROTECT, related_name="+")
    scope = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["decision", "count_pass"], name="uq_countselection"),
        ]
        indexes = [models.Index(fields=["count_pass"])]
