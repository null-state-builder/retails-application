"""Two-person application of user, role and permission changes (#131)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import Model
from django.utils import timezone
from rest_framework import serializers

from accounts.floors import describe_floors, floor_violations
from accounts.matrix import stored_row
from accounts.models import AccessChange, ActorPolicy, Role, User
from accounts.role_lists import ACCESS_ADMINISTRATORS
from accounts.serializers import (
    ActorPolicySerializer,
    AdminRoleSerializer,
    AdminUserSerializer,
    ApprovalPolicyAdminSerializer,
)
from approvals.models import ApprovalPolicy
from approvals.services import ApprovalError, ApprovalRightsError, request_approval


class AccessChangeError(ApprovalError):
    """A pending access change can no longer be applied safely."""


class AccessChangeRightsError(ApprovalRightsError):
    """The checker is outside the immutable access-administrator floor."""


#: Keyed on ``str`` (not ``AccessChange.Resource``) because callers - notably
#: ``propose_access_change`` below - index this with the ``resource: str`` they
#: were handed; ``Resource`` is a ``TextChoices`` (a ``str`` at runtime), so this
#: is the same mapping, just typed the way it is actually indexed.
SERIALIZERS: dict[str, type[serializers.ModelSerializer[Any]]] = {
    AccessChange.Resource.ROLE: AdminRoleSerializer,
    AccessChange.Resource.USER: AdminUserSerializer,
    AccessChange.Resource.ACTOR_POLICY: ActorPolicySerializer,
    AccessChange.Resource.APPROVAL_POLICY: ApprovalPolicyAdminSerializer,
}

#: Payload keys that are evidence rather than columns. The appliers below write
#: every key of a payload onto the row, so an annotation has to be visibly not a
#: field: the leading underscore says "kept for the audit trail, never applied".
#: `_cells` is the access matrix's per-cell diff (#173) - the old and new rung of
#: each section an edit moved, which the flat `section_access` blob cannot show.
ANNOTATION_PREFIX = "_"


def is_access_administrator(user: Any) -> bool:
    if not (user and getattr(user, "is_authenticated", False)):
        return False
    return getattr(getattr(user, "role", None), "code", "") in ACCESS_ADMINISTRATORS


def _json_payload(resource: str, validated: dict[str, Any]) -> dict[str, Any]:
    if resource != AccessChange.Resource.USER:
        return dict(validated)

    payload: dict[str, Any] = {}
    for key, value in validated.items():
        if key in {"role", "entity"}:
            payload[f"{key}_id"] = value.pk if value is not None else None
        elif key in {"stores", "brands"}:
            payload[f"{key[:-1]}_ids"] = [row.pk for row in value]
        elif key == "password":
            if value:
                payload["password_hash"] = make_password(value)
        else:
            payload[key] = value
    return payload


@transaction.atomic
def propose_access_change(
    *,
    resource: str,
    actor: User,
    data: dict[str, Any],
    target: Any = None,
    partial: bool = False,
    annotations: dict[str, Any] | None = None,
    summary: str | None = None,
) -> tuple[AccessChange, Any]:
    """Validate and freeze a Setup mutation, then ask a second admin to apply it.

    ``annotations`` are evidence carried alongside the columns and never written
    to the row (see ``ANNOTATION_PREFIX``); ``summary`` overrides the generic
    "Update role: X" line when the caller can say something more useful.
    """
    serializer_class = SERIALIZERS[resource]
    serializer = serializer_class(instance=target, data=data, partial=partial)
    serializer.is_valid(raise_exception=True)
    payload = _json_payload(resource, serializer.validated_data)
    for key, value in (annotations or {}).items():
        payload[f"{ANNOTATION_PREFIX}{key}"] = value
    operation = (
        AccessChange.Operation.UPDATE if target is not None else AccessChange.Operation.CREATE
    )
    target_name = (
        str(target) if target is not None else str(payload.get("name") or payload.get("code"))
    )
    line = (
        summary or f"{operation.title()} {resource.replace('_', ' ')}: {target_name or 'new row'}"
    )
    change = AccessChange.objects.create(
        resource=resource,
        operation=operation,
        target_id=getattr(target, "pk", None),
        payload=payload,
        summary=line[:240],
        created_by=actor,
    )
    approval = request_approval(
        change,
        kind="access_change",
        kind_label="Access change",
        title=change.summary,
        made_by=actor,
        requested_by=actor,
        approver_roles=list(ACCESS_ADMINISTRATORS),
    )
    return change, approval


def _target[M: Model](model: type[M], change: AccessChange) -> Any:
    if change.operation == AccessChange.Operation.CREATE:
        return model()
    try:
        # `objects`/`DoesNotExist` are real on every concrete Model subclass,
        # but django-stubs' plugin only re-adds them (they are stripped from
        # `Model` itself, see `django-stubs/db/models/base.pyi`) when the class
        # is named directly - a type parameter bound to `Model`, needed here
        # because this helper is shared across four unrelated resources,
        # doesn't qualify.
        query = model.objects.select_for_update()  # type: ignore[attr-defined]
        return query.get(pk=change.target_id)
    except model.DoesNotExist as exc:  # type: ignore[attr-defined]
        raise AccessChangeError("The row this access change targeted no longer exists.") from exc


def _columns(change: AccessChange) -> dict[str, Any]:
    """The payload minus its audit annotations - what actually lands on the row."""
    return {
        key: value for key, value in change.payload.items() if not key.startswith(ANNOTATION_PREFIX)
    }


def _apply_user(change: AccessChange) -> None:
    """The one resource whose payload is not just columns: two m2m sets and a
    password that was hashed at proposal time, never carried in the clear."""
    user = _target(User, change)
    payload = _columns(change)
    store_ids = payload.pop("store_ids", None)
    brand_ids = payload.pop("brand_ids", None)
    password_hash = payload.pop("password_hash", "")
    for key, value in payload.items():
        setattr(user, key, value)
    if password_hash:
        user.password = password_hash
    user.save()
    if store_ids is not None:
        user.stores.set(store_ids)
    if brand_ids is not None:
        user.brands.set(brand_ids)


def _apply_columns[M: Model](model: type[M]) -> Callable[[AccessChange], None]:
    """Every other resource is a row of plain columns the serializer validated."""

    def apply(change: AccessChange) -> None:
        row = _target(model, change)
        for key, value in _columns(change).items():
            setattr(row, key, value)
        row.save()

    return apply


def _apply_role(change: AccessChange) -> None:
    """A role is columns, checked twice - once when proposed, once on the way in.

    Validation at proposal time is not enough for this one resource. A proposal
    can sit in the inbox for days, and three things can move underneath it: the
    row it targets (another administrator's change lands first), the row's
    ``code`` (which is what the money floor keys on), and the floor itself
    (ratified in code, so a release can tighten it). Applying a frozen payload
    without asking again is how a floor gets crossed by a change that was legal
    when it was written.
    """
    role = _target(Role, change)
    payload = _columns(change)
    access = payload.get("section_access")
    code = str(payload.get("code") or role.code)

    before = change.payload.get(f"{ANNOTATION_PREFIX}before")
    if before is not None and stored_row(role) != before:
        raise AccessChangeError(
            f"{role.name} has changed since this was proposed. "
            "Open the access grid again and re-make the change on the current row."
        )

    for key, value in payload.items():
        setattr(role, key, value)

    crossed = floor_violations(code, access) if access is not None else []
    if crossed:
        raise AccessChangeError(
            "This change now crosses a money floor rule and cannot be applied. "
            + " ".join(describe_floors(crossed))
        )
    role.save()


#: One entry per resource, keyed the same way ``SERIALIZERS`` is, so proposing
#: and applying a new kind of access change is one pair of lines rather than a
#: branch somebody has to remember to add.
APPLIERS: dict[str, Callable[[AccessChange], None]] = {
    AccessChange.Resource.ROLE: _apply_role,
    AccessChange.Resource.USER: _apply_user,
    AccessChange.Resource.ACTOR_POLICY: _apply_columns(ActorPolicy),
    AccessChange.Resource.APPROVAL_POLICY: _apply_columns(ApprovalPolicy),
}


@transaction.atomic
def apply_access_change(change: AccessChange, *, actor: User) -> None:
    """Approval callback: apply once, naming the second person who applied it."""
    if not is_access_administrator(actor):
        raise AccessChangeRightsError("Only Owner or IT Admin may apply access changes.")
    locked = AccessChange.objects.select_for_update().get(pk=change.pk)
    if locked.applied_at is not None:
        raise AccessChangeError("This access change has already been applied.")
    try:
        apply = APPLIERS[locked.resource]
    except KeyError as exc:  # pragma: no cover - choices and the database constrain this
        raise AccessChangeError(f"Unsupported access resource {locked.resource!r}.") from exc
    apply(locked)
    locked.applied_by = actor
    locked.applied_at = timezone.now()
    # The password only ever had to survive until somebody applied it. Leaving
    # the hash in a row that is kept as evidence gives an offline attack a place
    # to start, and the audit trail is no poorer for its absence.
    locked.payload.pop("password_hash", None)
    locked.save(update_fields=["applied_by", "applied_at", "payload", "updated_at"])
