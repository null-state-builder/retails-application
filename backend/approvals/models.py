"""The one generic approval record — maker-checker for the whole system (#70).

One table backs *every* approval flow (write-offs, V-flips, stock adjustments,
and later gap closures, stock requests, return-window overrides). A document
does not carry its own bespoke approval columns; it carries an ``Approval``
pointing at it, so a single inbox can list "everything waiting for you" without
knowing what any of those documents are.

Design notes
------------
* **Generic subject** — ``content_type`` + ``object_id`` point at any document.
  At most one *pending* approval per subject: a rejected request stays on the
  record and the maker may fix the draft and ask again, so a document carries
  the whole "asked → rejected → asked again → approved" history.
* **Snapshots, not joins** — ``kind_label``, ``title``, ``value_paise`` and
  ``approver_roles`` are written by the requesting module at request time
  (Rule 6: snapshot what the decision was taken against; Rule 12: who may
  approve is *data on the row*, not a branch in code). The inbox therefore
  renders without importing a single business model.
* **Maker ≠ checker is a DB constraint, not just a service check** — the kernel
  house style is defence-in-depth: the ORM raises the early, clean error, the
  database is the one that actually binds.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.db import models

from core.base import TimeStampedModel
from core.money import MoneyField


class ApprovalStatus(models.TextChoices):
    PENDING = "pending", "Waiting for approval"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    #: Below the policy tolerance — the document posts without a second person,
    #: but the row is still written so *every* wired document has one record
    #: saying who made it and why no checker was asked ("auto-adjust, logged").
    NOT_REQUIRED = "not_required", "No second person needed"


#: The two statuses that let a document post.
CLEARED_STATUSES = (ApprovalStatus.APPROVED, ApprovalStatus.NOT_REQUIRED)


class ApprovalRoute(TimeStampedModel):
    """The ordered chain a document family walks before it is approved (#172).

    One row per family, and the chain itself is **data** — a list of steps, each
    naming the roles that may clear it (Rule 12: who approves is data, not a
    branch in code). A family with no row keeps the single-step behaviour every
    other approval in the system already has; nothing is retrofitted onto it.

    Why a chain at all: a store's ask for another location's stock is sanity
    checked by its own manager before it reaches HO, and the Operations Head who
    would otherwise wait for that first tap can simply approve it — that is the
    ``later_step_may_short_circuit`` flag, and it is set on the *later* step,
    which is the one reaching back.
    """

    #: One step, as stored in ``steps``:
    #:
    #: ``order``                        1-based, for humans; position decides.
    #: ``roles``                        role codes that may clear this step.
    #: ``roles_from_policy``            instead of a list of its own, this step's
    #:                                  approvers are whatever the family's live
    #:                                  ``ApprovalPolicy`` says at this value.
    #:                                  Without it a route would freeze a second
    #:                                  copy of the approver list, and retuning
    #:                                  the policy in Setup would silently do
    #:                                  nothing (Rule 12 wants one editable
    #:                                  source, not two that can disagree).
    #: ``label``                        what the screen calls it.
    #: ``later_step_may_short_circuit`` this step's approvers may also close
    #:                                  every step still waiting below them.
    kind = models.CharField(
        max_length=32,
        unique=True,
        help_text="Document family this routes, e.g. 'stock_request'. Matches Approval.kind.",
    )
    steps = models.JSONField(
        default=list,
        help_text="Ordered steps: "
        "[{order, roles | roles_from_policy, label, later_step_may_short_circuit}].",
    )

    class Meta:
        db_table = "approvals_approvalroute"
        ordering = ["kind"]

    def __str__(self) -> str:
        return f"{self.kind} route ({self.step_count()} steps)"

    # -- reading the chain ---------------------------------------------------

    @staticmethod
    def _position(step: dict[str, Any]) -> int:
        """A step's place in the queue, from data that may be anything.

        ``order`` is hand-written into a migration or (one day) a Setup screen,
        so it can arrive as a string or missing entirely. A chain that raises on
        read would 500 every inbox in the system, which is a far worse answer
        than falling back to the order it was stored in.
        """
        try:
            return int(step.get("order", 0))
        except (TypeError, ValueError):
            return 0

    def ordered_steps(self) -> list[dict[str, Any]]:
        """The steps in the order they are walked.

        Sorted by ``order`` rather than trusted as stored: the list is editable
        data, and a chain read out of sequence would hand step 2's approver
        step 1's authority.
        """
        return sorted(self.steps or [], key=self._position)

    def step_count(self) -> int:
        return len(self.ordered_steps())

    def clamped_step(self, index: int) -> int:
        """``index``, brought back inside a chain that may have been shortened.

        A route is editable data, and dropping a step out of one leaves every
        request already past that point pointing at nothing. Without this they
        would deadlock — no step to act on, so no role can ever decide them
        again. Landing on the last step instead means the chain is *shorter*
        than the maker was promised, never longer, and it can still be finished.
        """
        return max(0, min(index, self.step_count() - 1))

    def label_at(self, index: int) -> str:
        steps = self.ordered_steps()
        if 0 <= index < len(steps):
            return str(steps[index].get("label") or f"Step {index + 1}")
        return ""

    def roles_at(self, index: int, value_paise: int | None = 0) -> list[str]:
        """Who may clear one step — its own list, or the live policy's.

        A ``roles_from_policy`` step defers to ``ApprovalPolicy`` so the value
        band and the Setup screen keep working on a routed family; a missing
        policy row yields nobody, which is the closed gate ``_policy`` already
        means elsewhere. An unset value reads as 0, which the policy escalates —
        the same fail-closed reading it gives an unpriced document.
        """
        steps = self.ordered_steps()
        if not 0 <= index < len(steps):
            return []
        step = steps[index]
        if step.get("roles_from_policy"):
            policy = ApprovalPolicy.objects.filter(kind=self.kind).first()
            return policy.approver_roles_for(value_paise or 0) if policy else []
        return [str(role) for role in step.get("roles") or []]

    def _reachable_steps(self, from_index: int) -> list[int]:
        """The steps someone standing at ``from_index`` may act on, in order.

        The one statement of the short-circuit rule: the step now waiting, plus
        any later step marked as allowed to close the ones below it. Both the
        "who may act" list and the "which step is this person deciding" lookup
        read it, so the rule cannot drift between them.
        """
        return [
            index
            for index, step in enumerate(self.ordered_steps())
            if index == from_index
            or (index > from_index and step.get("later_step_may_short_circuit"))
        ]

    def active_roles(self, from_index: int, value_paise: int | None = 0) -> list[str]:
        """Every role that may act *right now*, with the request at ``from_index``.

        This is what gets snapshotted onto ``Approval.approver_roles``, so the
        inbox query and the decide gate keep reading one list and never learn
        that routes exist.
        """
        roles: list[str] = []
        for index in self._reachable_steps(from_index):
            for role in self.roles_at(index, value_paise):
                if role not in roles:
                    roles.append(role)
        return roles

    def step_for_role(
        self, role_code: str, from_index: int, value_paise: int | None = 0
    ) -> int | None:
        """Which step ``role_code`` is deciding, with the request at ``from_index``.

        The current step if they are on it; otherwise the *earliest* later step
        that both lists them and is allowed to close the ones below. None means
        they are on no remaining step — a 403, not a silent no-op.
        """
        if not role_code:
            return None
        for index in self._reachable_steps(from_index):
            if role_code in self.roles_at(index, value_paise):
                return index
        return None


class Approval(TimeStampedModel):
    """One pending-or-decided approval against one document."""

    # --- what is being approved -------------------------------------------
    kind = models.CharField(
        max_length=32,
        help_text="Machine code of the document family, e.g. 'writeoff'. "
        "The client maps it to a route; the server never branches on it.",
    )
    kind_label = models.CharField(max_length=64)
    title = models.CharField(
        max_length=240,
        help_text="Snapshot one-liner shown in the inbox (store, lines, pieces).",
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.BigIntegerField()
    subject = GenericForeignKey("content_type", "object_id")

    # --- context used to route & scope the inbox ---------------------------
    store = models.ForeignKey(
        "masters.Store",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approvals",
        help_text="Scopes the inbox for store-scoped users (ADR-0003).",
    )
    brand = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="Scopes the inbox for brand-scoped users — a brand manager, "
        "whose boundary is brands and not stores (ADR-0003). Snapshotted as the "
        "brand *name*, like every other brand on a ledger row, so this table "
        "still imports no business model. Blank means the decision is not about "
        "one brand, and a brand-scoped user never sees it (#75).",
    )
    # Stable scope identity; historical brand text is display evidence only.
    brand_ref = models.ForeignKey(
        "masters.Brand", null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    value_paise = MoneyField(
        default=0,
        help_text="Value at stake, snapshotted — the input to value-banded approval.",
    )
    approver_roles = ArrayField(
        models.CharField(max_length=32),
        default=list,
        help_text="Role codes that may decide this one *right now*. Data, not "
        "code (Rule 12). On a routed approval this is the current step's roles "
        "plus any later step allowed to close it, rewritten as the chain "
        "advances — so the inbox query never has to know routes exist.",
    )

    # --- the chain, where the family has one (#172) -------------------------
    route = models.ForeignKey(
        ApprovalRoute,
        null=True,
        blank=True,
        # PROTECT, not SET_NULL: nulling a live route would turn a two-step
        # approval into a one-step one behind the maker's back — the next
        # approver holding the current step's roles would finalise it and every
        # remaining step would simply vanish. A route a request has walked is
        # part of that request's audit trail and outlives any retune.
        on_delete=models.PROTECT,
        related_name="approvals",
        help_text="The chain this request walks. Null is the ordinary "
        "single-step approval every other family uses.",
    )
    current_step = models.IntegerField(
        default=0,
        help_text="Zero-based position in the route. 0 on an unrouted approval, "
        "where it means nothing; on a routed one it is the step now waiting, and "
        "once approved it sits past the last step.",
    )
    # Immutable request-time authority evidence. Null historical rows are
    # intentionally inactive until their source is validated and resubmitted.
    authority_pin = models.JSONField(null=True, blank=True, editable=False)

    # --- maker -------------------------------------------------------------
    made_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="approvals_made",
        help_text="Who created the document. Snapshotted once and never "
        "rewritten, so re-asking after a rejection cannot launder the maker "
        "out of the way and let them approve their own document.",
    )
    requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="approvals_requested",
        help_text="Who asked, this time round. Usually the maker; after a "
        "rejection it may be whoever picked the document back up.",
    )

    # --- checker -----------------------------------------------------------
    status = models.CharField(
        max_length=16, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING
    )
    decided_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approvals_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(
        blank=True,
        default="",
        help_text="Required on reject; free for approve; the policy note on 'not_required'.",
    )

    class Meta:
        db_table = "approvals_approval"
        # Newest first, and never ambiguous: a document may hold several rows
        # once it has been rejected and asked again, and "the live one" is
        # always the newest — so ties must not be resolved by chance.
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="approval_status_recent_idx"),
        ]
        constraints = [
            # One *pending* request per document — a document is never in two
            # people's inboxes at once — while decided rows accumulate as the
            # document's history.
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                condition=models.Q(status=ApprovalStatus.PENDING),
                name="uq_approval_subject_pending",
            ),
            # Maker ≠ checker, enforced by the database on every wired document
            # type at once. NULL decided_by (still pending) satisfies both.
            # Two rows, because after a rejection the maker and the asker can be
            # two different people and *neither* may be the checker.
            models.CheckConstraint(
                condition=~models.Q(decided_by=models.F("requested_by")),
                name="approval_asker_is_not_checker",
            ),
            models.CheckConstraint(
                condition=~models.Q(decided_by=models.F("made_by")),
                name="approval_maker_is_not_checker",
            ),
            # A *person's* decision always carries its decider and its
            # timestamp; a row nobody has decided — still pending, or never
            # needed one — carries neither.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status__in=[ApprovalStatus.PENDING, ApprovalStatus.NOT_REQUIRED],
                        decided_by__isnull=True,
                        decided_at__isnull=True,
                    )
                    | models.Q(
                        status__in=[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED],
                        decided_by__isnull=False,
                        decided_at__isnull=False,
                    )
                ),
                name="approval_decision_is_complete",
            ),
            # "Reject requires a reason" — the acceptance criterion, in the DB.
            models.CheckConstraint(
                condition=~models.Q(status=ApprovalStatus.REJECTED) | ~models.Q(reason=""),
                name="approval_reject_has_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind_label} #{self.object_id} — {self.status}"

    def step_trail(self) -> list[dict[str, Any]]:
        """The chain as the Approvals screen shows it — one entry per step.

        Empty on an unrouted approval, which is the whole of "single-step
        behaviour untouched": a screen that renders nothing for an empty trail
        needs no branch for the families that have no chain.

        ``state`` is one of ``done`` (someone cleared it), ``rejected`` (the
        request died here), ``current`` (waiting on it now) and ``waiting``.
        """
        route = self.route
        if route is None:
            return []
        decisions = {d.step_order: d for d in self.step_decisions.all()}
        # A route shortened since this request was raised leaves ``current_step``
        # past the end; read it clamped so the trail still marks a live step
        # rather than showing a chain nobody is standing on.
        here = route.clamped_step(self.current_step)
        trail: list[dict[str, Any]] = []
        for index in range(route.step_count()):
            decision = decisions.get(index)
            if decision is not None:
                state = "done"
            elif self.status == ApprovalStatus.REJECTED and index == here:
                state = "rejected"
            elif self.status == ApprovalStatus.PENDING and index == here:
                state = "current"
            else:
                state = "waiting"
            trail.append(
                {
                    "order": index + 1,
                    "label": route.label_at(index),
                    "roles": route.roles_at(index, self.value_paise),
                    "state": state,
                    "decided_by_name": decision.decided_by_name if decision else "",
                    "decided_at": decision.decided_at if decision else None,
                    "short_circuited": bool(decision.short_circuited) if decision else False,
                }
            )
        if self.status == ApprovalStatus.REJECTED and not any(
            step["state"] == "rejected" for step in trail
        ):
            # A route edited after the request was raised can leave the step it
            # died on out of range; say so rather than showing a chain that
            # reads as though nothing ever refused it.
            for step in trail:
                if step["state"] == "waiting":
                    step["state"] = "rejected"
                    break
        return trail


class ApprovalStepDecision(TimeStampedModel):
    """Who cleared one step of a routed approval, and when (#172).

    A single ``Approval`` row can only carry one decider, and the kernel's
    ``approval_decision_is_complete`` constraint keeps that column empty while
    the request is still pending — correctly, since a chain that is half walked
    has not been decided by anybody. So the steps cleared along the way are kept
    here, one row each, and the inbox's step trail is read straight off them.

    ``short_circuited`` is the honest part: when the Operations Head approves
    directly, the store manager's step closes with *the Operations Head* named
    on it and a flag saying it was closed from above. Nobody is ever recorded as
    having approved something they did not touch.
    """

    approval = models.ForeignKey(Approval, on_delete=models.CASCADE, related_name="step_decisions")
    step_order = models.IntegerField(help_text="Zero-based position in the route at the time.")
    step_label = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="The step's name, snapshotted — the route is editable data (Rule 6).",
    )
    decided_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="approval_steps_decided",
    )
    decided_at = models.DateTimeField()
    short_circuited = models.BooleanField(
        default=False,
        help_text="Closed from a later step rather than cleared by its own approver.",
    )
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "approvals_step_decision"
        ordering = ["approval_id", "step_order"]
        constraints = [
            # A step is cleared once. Without this, a retry that half-failed
            # could write a second row and the trail would show two approvers
            # for one step.
            models.UniqueConstraint(
                fields=["approval", "step_order"], name="uq_approval_step_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.approval_id} step {self.step_order} — {self.decided_by_id}"

    @property
    def decided_by_name(self) -> str:
        user = self.decided_by
        return getattr(user, "full_name", "") or getattr(user, "username", "") or ""


class ApprovalPolicy(TimeStampedModel):
    """How much a document may be worth before a second person is needed, and
    who that person must be — one row per document family.

    From the count design: *"within a configurable tolerance → auto-adjust,
    logged, done. Above tolerance → … a named approver. Approval is
    value-banded: up to a configurable value the in-charge approves; above it,
    HO."* Both numbers and both role lists are **data** so the business can
    retune them in the admin without a release (Rule 12).

    The owning module supplies its own defaults on first use; nothing here
    imports a business model, so this stays the generic maker-checker spine.
    """

    kind = models.CharField(
        max_length=32,
        unique=True,
        help_text="Document family this governs, e.g. 'adjustment'.",
    )
    tolerance_paise = MoneyField(
        default=0,
        help_text="Value at stake at or below which no second person is asked — "
        "the document posts and the decision is logged. 0 disables the "
        "tolerance: every document of this kind needs a checker.",
    )
    band_paise = MoneyField(
        default=0,
        help_text="Up to this value the in-charge roles may approve; above it, "
        "only the escalated roles. 0 sends every one straight to HO.",
    )
    band_roles = ArrayField(
        models.CharField(max_length=32),
        default=list,
        help_text="Role codes that may approve within the band (in-charge + HO).",
    )
    escalated_roles = ArrayField(
        models.CharField(max_length=32),
        default=list,
        help_text="Role codes that may approve above the band (HO only).",
    )

    class Meta:
        db_table = "approvals_policy"
        ordering = ["kind"]
        verbose_name_plural = "approval policies"

    #: What each family is called in the business's own words. A Setup screen
    #: shows the *family*, not its slug — an owner retuning who signs off a
    #: return to brand should not have to read ``return_to_brand``. Kept here
    #: beside the codes rather than in the PWA so one name serves every surface,
    #: and it stays a plain dict so this module still imports nothing (ADR-0002).
    FAMILY_LABELS: ClassVar[dict[str, str]] = {
        "adjustment": "Stock adjustment",
        "booking": "Booking",
        "damage": "Damage flag",
        "offer": "Offer",
        "gap_closure": "Transfer gap closure",
        "pt_reverse": "PT reversal",
        "return_to_brand": "Return to brand",
        "stock_request": "Stock request",
        "transfer": "Transfer",
        "vflip": "V-flip",
        "writeoff": "Write-off",
    }

    def __str__(self) -> str:
        return f"{self.kind} policy"

    @property
    def label(self) -> str:
        """The family in the business's words; an unknown code reads as itself."""
        return self.FAMILY_LABELS.get(self.kind, self.kind.replace("_", " ").capitalize())

    # -- the two questions the policy answers --------------------------------

    @property
    def tolerance(self) -> int:
        """The threshold as a plain number — an unset column reads as 'no
        tolerance', which is the strict, fail-closed reading."""
        return self.tolerance_paise or 0

    @property
    def band(self) -> int:
        return self.band_paise or 0

    def needs_checker(self, value_paise: int) -> bool:
        """Is this one big enough to need a second person?

        A value of zero means *unknown*, not *free*: cost is enriched at post
        time on some documents, so a draft can honestly not know what it is
        worth yet. Unknown fails closed — it goes to a checker.
        """
        if self.tolerance <= 0 or value_paise <= 0:
            return True
        return value_paise > self.tolerance

    def approver_roles_for(self, value_paise: int) -> list[str]:
        """Who may clear it — the in-charge band, or HO. Unknown value (0)
        escalates, for the same reason."""
        if 0 < value_paise <= self.band:
            return list(self.band_roles)
        return list(self.escalated_roles)


# Goods-v1 approval requests/decisions (design §5.2, §5.6), registered with this app.
from approvals.goods_models import (  # noqa: E402, F401
    ActionDraft,
    ApprovalDecision,
    ApprovalRequest,
)
