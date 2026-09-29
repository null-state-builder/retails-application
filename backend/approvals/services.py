"""Approval services — the one write-path for maker-checker (#70).

Every approval in the system is requested, decided and checked through these
four functions. Nothing else writes an ``Approval`` row, so the rules below hold
system-wide by construction rather than by each module remembering them:

* a document is never approved by the person who made it;
* a reject always carries a reason;
* a decision is made once — a decided approval is closed;
* a wired document does not post until its approval says approved — and where
  the ruling says the *approval* is what posts, the owning module's registered
  callback does it (``approvals.hooks``), so approvals still never learns what
  it is approving;
* how big a document must be before a checker is asked, and who that checker
  may be, is read from a policy row — data the business can retune (Rule 12).
"""

from __future__ import annotations

from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from approvals.hooks import run_on_approved, run_on_rejected
from approvals.models import (
    CLEARED_STATUSES,
    Approval,
    ApprovalRoute,
    ApprovalStatus,
    ApprovalStepDecision,
)

# Re-exported: it lives in ``approvals.names`` now that the model layer needs it
# for the step trail, and every caller learned it here.
from approvals.names import display_name as display_name
from masters.scoping import scope_by_store_or_brand


class ApprovalError(Exception):
    """Base for every maker-checker violation."""


class ApprovalRightsError(ApprovalError):
    """This user may not decide this one — a rights failure (403, not 400)."""


class SelfApprovalError(ApprovalRightsError):
    """The maker tried to be the checker."""


class NotAnApproverError(ApprovalRightsError):
    """The user's role is not among the approvers for this document."""


class ApprovalRequired(ApprovalError):
    """A wired document tried to post without a live approval."""


class AlreadyPendingError(ApprovalError):
    """Asked again while a request for the same document is still waiting."""


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def _create(
    subject: models.Model,
    *,
    status: str,
    kind: str,
    kind_label: str,
    title: str,
    made_by: Any,
    requested_by: Any,
    approver_roles: list[str],
    store: Any = None,
    brand: str = "",
    value_paise: int = 0,
    reason: str = "",
    route: ApprovalRoute | None = None,
) -> Approval:
    return Approval.objects.create(
        kind=kind,
        kind_label=kind_label,
        title=title,
        content_type=ContentType.objects.get_for_model(subject),
        object_id=subject.pk,
        store=store,
        brand=brand,
        value_paise=value_paise,
        approver_roles=list(approver_roles),
        made_by=made_by,
        requested_by=requested_by,
        status=status,
        reason=reason,
        route=route,
        current_step=0,
    )


def route_for(kind: str) -> ApprovalRoute | None:
    """The chain configured for this family, if it has one (#172).

    A family with no row is the ordinary single-step approval, which is every
    family but one today — so this returns None far more often than not, and
    that is the untouched path.
    """
    route = ApprovalRoute.objects.filter(kind=kind).first()
    return route if route is not None and route.step_count() else None


def request_approval(
    subject: models.Model,
    *,
    kind: str,
    kind_label: str,
    title: str,
    made_by: Any,
    requested_by: Any,
    approver_roles: list[str],
    store: Any = None,
    brand: str = "",
    value_paise: int = 0,
) -> Approval:
    """Put ``subject`` in the approvals inbox, waiting for someone else.

    Called by the module that owns the document, at draft-creation time, so the
    maker can never "forget" to seek approval — the document is born pending.
    Also the way back from a rejection: the document is fixed and asked again,
    which raises a *new* request beside the rejected one. ``made_by`` is the
    document's creator either way, so re-asking cannot move the maker aside.

    Where the family has a **route** (#172) the chain is attached here — the one
    write path for a pending row, so every module gets multi-step approval
    without knowing it exists. The route's first step then says who may act, in
    place of the ``approver_roles`` the caller's policy computed: a family with a
    chain is routed by the chain, and having two lists disagree about who
    approves step 1 is the one thing that must not be possible.
    """
    route = route_for(kind)
    if route is not None:
        approver_roles = route.active_roles(0, value_paise)
    try:
        with transaction.atomic():
            return _create(
                subject,
                status=ApprovalStatus.PENDING,
                kind=kind,
                kind_label=kind_label,
                title=title,
                made_by=made_by,
                requested_by=requested_by,
                approver_roles=approver_roles,
                store=store,
                brand=brand,
                value_paise=value_paise,
                route=route,
            )
    except IntegrityError as exc:
        # The partial unique index is the one that actually binds — two people
        # clicking "ask again" at the same moment race past any prior read.
        if "uq_approval_subject_pending" not in str(exc):
            raise
        raise AlreadyPendingError("This document is already waiting for approval.") from exc


def record_no_approval_needed(
    subject: models.Model,
    *,
    kind: str,
    kind_label: str,
    title: str,
    made_by: Any,
    requested_by: Any,
    store: Any = None,
    brand: str = "",
    value_paise: int = 0,
    reason: str,
) -> Approval:
    """Record that ``subject`` needed no second person, and why.

    Two rules reach here: it fell within its policy tolerance (too little at
    stake to ask), or the person raising it already held the rung this family
    would have asked. Either way "nobody was asked" is itself a fact worth
    keeping: the row carries who made it and which rule let it through, so a
    small adjustment is auditable exactly like a large one.
    """
    return _create(
        subject,
        status=ApprovalStatus.NOT_REQUIRED,
        kind=kind,
        kind_label=kind_label,
        title=title,
        made_by=made_by,
        requested_by=requested_by,
        approver_roles=[],
        store=store,
        brand=brand,
        value_paise=value_paise,
        reason=reason,
    )


def approvals_for(subject: models.Model) -> models.QuerySet[Approval]:
    """Every approval ever raised against ``subject``, newest first."""
    return (
        Approval.objects.filter(
            content_type=ContentType.objects.get_for_model(subject), object_id=subject.pk
        )
        .select_related("made_by", "requested_by", "decided_by", "route")
        .prefetch_related("step_decisions__decided_by")
    )


def approval_for(subject: models.Model) -> Approval | None:
    """The *live* approval against ``subject`` — the newest one, since a
    rejected request stays on the record after the maker asks again. None if
    the type isn't wired, or nothing has been asked yet."""
    return approvals_for(subject).first()


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------


@transaction.atomic
def decide(approval: Approval, *, actor: Any, action: str, reason: str = "") -> Approval:
    """Approve or reject, as ``actor``. The single enforcement point.

    Re-reads the row ``FOR UPDATE`` so two seniors clicking at the same moment
    cannot both decide it — the second gets the "already decided" error.

    On a **routed** approval (#172) an approve clears one step and the request
    stays pending until the last one falls; only then is it approved and only
    then does anything post. A reject ends it wherever it stands, as it always
    has. An unrouted approval takes the same path it always did.
    """
    if action not in ("approve", "reject"):
        raise ApprovalError("action must be 'approve' or 'reject'.")

    locked = Approval.objects.select_for_update().get(pk=approval.pk)

    if locked.status != ApprovalStatus.PENDING:
        raise ApprovalError(f"This request was already {locked.status}.")

    _refuse_deciding_your_own(locked, actor)

    if not can_decide(locked, actor):
        raise NotAnApproverError("Your role cannot decide this approval.")

    reason = (reason or "").strip()
    if action == "reject" and not reason:
        raise ApprovalError("A reason is required when rejecting.")

    step_index = _step_being_decided(locked, actor)

    if action == "approve" and locked.route_id:
        advanced = _advance_route(locked, actor=actor, step_index=step_index, note=reason)
        if advanced is not None:
            # Cleared a step, the chain goes on — still nobody's decision.
            return advanced

    if action == "reject" and locked.route_id and step_index is not None:
        # Say where it died: a later-step approver refusing ends it at *their*
        # step, not at whichever one happened to be waiting.
        locked.current_step = step_index

    locked.status = ApprovalStatus.APPROVED if action == "approve" else ApprovalStatus.REJECTED
    locked.decided_by = actor
    locked.decided_at = timezone.now()
    locked.reason = reason
    locked.save(
        update_fields=["status", "decided_by", "decided_at", "reason", "current_step", "updated_at"]
    )

    if locked.status == ApprovalStatus.APPROVED:
        # Where the ruling says the *approval* is what posts, this is where it
        # posts — inside the decision's transaction. See ``approvals.hooks``.
        run_on_approved(locked.subject, actor=actor)
    else:
        run_on_rejected(locked.subject, actor=actor, reason=reason)
    return locked


def _refuse_deciding_your_own(approval: Approval, actor: Any) -> None:
    """The maker is never the checker — the rule the warehouse team asked for,
    in one place, for every document type that uses this record.

    Both the person who made the document and the person who asked this time are
    barred: after a rejection those can be two different people, and letting a
    colleague re-raise a document so its author can approve it would be
    maker-checker with extra steps.

    On a chain the same rule holds *between* steps (#172): two steps means two
    people. A route may list one role on two of them, and one person walking a
    request from end to end is maker-checker with extra steps all over again.

    That between-steps rule is the one thing a superuser is let past, and only
    that one. Break-glass exists so a request can never become unclearable, and
    a superuser barred from step 2 by their own step 1 would be exactly that —
    stuck, with no second superuser to call. The two bars that matter, maker and
    asker, still hold for them, and the database holds them too.
    """
    actor_id = getattr(actor, "id", None)
    if approval.made_by_id == actor_id:
        raise SelfApprovalError("You cannot approve a document you created.")
    if approval.requested_by_id == actor_id:
        raise SelfApprovalError("You cannot approve a request you raised.")
    if (
        approval.route_id
        and not getattr(actor, "is_superuser", False)
        and approval.step_decisions.filter(decided_by=actor).exists()
    ):
        raise SelfApprovalError("You have already cleared a step on this request.")


def _step_being_decided(approval: Approval, actor: Any) -> int | None:
    """Which step of the route ``actor``'s approval lands on. None if unrouted.

    Not a gate — ``can_decide`` is the gate, and it has already run. This only
    places an allowed decision on the chain, which is why it never refuses: a
    second role check here could disagree with the first one and leave a request
    that nobody at all can decide.

    They can disagree, because the row's ``approver_roles`` is a snapshot and the
    route is live data somebody may have edited since. When the live route cannot
    place this approver — their step was dropped, or the chain was reordered —
    the decision lands on the step now waiting, which is the promise the row made
    when it reached their inbox.

    A superuser is break-glass and holds no business role, so they always act on
    the waiting step: reaching forward is a permission the *route* grants to a
    named role, not a way around the chain. They clear it one step per action,
    which reads honestly in the trail.

    ``current_step`` is read clamped, so a request left past the end of a route
    someone shortened is still decidable instead of deadlocked.
    """
    route = approval.route
    if route is None:
        return None
    here = route.clamped_step(approval.current_step)
    if getattr(actor, "is_superuser", False):
        return here
    role_code = getattr(getattr(actor, "role", None), "code", "")
    index = route.step_for_role(role_code, here, approval.value_paise)
    return here if index is None else index


def _advance_route(
    approval: Approval, *, actor: Any, step_index: int | None, note: str
) -> Approval | None:
    """Clear step ``step_index`` (and any it reaches back over) and move on.

    Returns the still-pending approval when the chain continues, or None when
    that was the last step and the caller should finish it as an approval in the
    ordinary way.
    """
    route = approval.route
    if route is None or step_index is None:
        return None

    now = timezone.now()
    # A step already cleared keeps the name of whoever cleared it. This only
    # arises when a route is shortened under a live request and ``current_step``
    # is clamped back onto ground already walked — but a step is cleared once,
    # the database says so, and rewriting history to say otherwise would be a
    # 500 at best and a forged trail at worst.
    already = set(approval.step_decisions.values_list("step_order", flat=True))
    # Everything between where the request stands and where this approver sits
    # closes *from above* — recorded with the actual actor and flagged, so the
    # trail never claims a manager approved something they never saw.
    for index in range(route.clamped_step(approval.current_step), step_index + 1):
        if index in already:
            continue
        ApprovalStepDecision.objects.create(
            approval=approval,
            step_order=index,
            step_label=route.label_at(index),
            decided_by=actor,
            decided_at=now,
            short_circuited=index < step_index,
            note=note if index == step_index else "",
        )

    next_step = step_index + 1
    approval.current_step = next_step
    if next_step >= route.step_count():
        return None

    approval.approver_roles = route.active_roles(next_step, approval.value_paise)
    approval.save(update_fields=["current_step", "approver_roles", "updated_at"])
    return approval


def holds_approver_role(user: Any, approver_roles: Any) -> bool:
    """Does ``user``'s role sit on a list of approver roles?

    The one spelling of "senior enough", so superuser, a user with no role and
    an empty list are reasoned about once. Used to ask whether someone may
    decide an existing request (``can_decide``) and, in ``outbound``, whether a
    maker already holds the rung the family would have asked (#138).
    """
    if getattr(user, "is_superuser", False):
        return True
    role_code = getattr(getattr(user, "role", None), "code", "")
    return bool(role_code) and role_code in (approver_roles or [])


def can_decide(approval: Approval, user: Any) -> bool:
    """May ``user``'s *role* decide this one? A role gate and nothing else.

    Two other gates exist and neither is here. ``decide`` bars the maker and the
    asker. Record scope (ADR-0003) is applied where the row is *found* — by
    ``inbox_for`` when listing, and by the decide view, which looks the approval
    up through ``scope_by_entitlement_or_brand`` so an out-of-scope pk is a 404
    before any of this runs; that is also what stops a brand manager deciding
    another brand's return (#75). Deciding uses the entitlement, not the top-bar
    unit: the switcher narrows what you read, never what you may act on.

    It is deliberately not re-checked at decision time, so a caller
    that hands ``decide`` a row it fetched some other way — a shell, a
    management command — is responsible for scoping it first.
    """
    return holds_approver_role(user, approval.approver_roles)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def inbox_for(user: Any) -> Any:
    """Everything waiting for ``user``'s decision, across every document type.

    Fail-closed on three axes: pending only, never one's own — neither made nor
    asked by this user, since a self-approval can never succeed and so is never
    offered — role-matched, and scoped by store or by brand depending on which
    of the two bounds the caller. An approval with no store is network-level:
    only unrestricted users see it, and a brand manager sees only rows carrying
    one of their own brands (#75).
    """
    qs = (
        Approval.objects.filter(status=ApprovalStatus.PENDING)
        .exclude(requested_by=user)
        .exclude(made_by=user)
        .select_related("store", "made_by", "requested_by", "decided_by", "route")
        .prefetch_related("step_decisions__decided_by")
    )
    qs = scope_by_store_or_brand(qs, user)
    if not getattr(user, "is_superuser", False):
        role_code = getattr(getattr(user, "role", None), "code", "")
        if not role_code:
            return qs.none()
        qs = qs.filter(approver_roles__contains=[role_code])
    return qs


# ---------------------------------------------------------------------------
# Post-time gate
# ---------------------------------------------------------------------------


def assert_approved(subject: models.Model) -> Approval:
    """Raise unless ``subject`` carries an approval decided by a second person.

    Called from the *posting* layer, not the view, so no caller — API, shell,
    management command — can post a wired document past its checker.
    """
    approval = approval_for(subject)
    if approval is None:
        raise ApprovalRequired("This document has no approval request; it cannot post.")
    if approval.status == ApprovalStatus.REJECTED:
        raise ApprovalRequired(
            f"Approval was rejected: {approval.reason} — fix the draft and ask again."
        )
    if approval.status not in CLEARED_STATUSES:
        raise ApprovalRequired("Waiting for approval by a second person.")
    return approval
