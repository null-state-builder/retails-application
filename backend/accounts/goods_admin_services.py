"""People, logins, roles, grants and privileged-change review (E061-E084, E214, E237).

Staff and their assignments are ordinary scoped changes (design §5.6): a staff code
is unique in the tenant, every assignment sits inside the actor's site authority,
primary periods never overlap and retirement closes what is still open while
keeping all history.

Logins, roles and grants are single-identity privileged changes (design §4.2,
Phase 1 §9.3): the actor confirms their password on the same session, the audit
subject names what changed so the privileged-change list can show it, and every
person whose access moved gets a new security epoch, which ends their sessions.
A different person later acknowledges the change; that acknowledgement is never
an approval and never undoes anything.

Segregation of duties compares ``HumanIdentity`` IDs, never role codes or login
names. Passwords are write-only: they are validated with Django's validators,
hashed onto the login and never returned, versioned or audited.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.actions import (
    ACTIONS,
    DISTINCT_IDENTITY_ACTIONS,
    FIELDS,
    PRIVILEGED_COMMAND_ACTIONS,
    ROLE_TEMPLATES,
    STEP_UP_ACTIONS,
)
from accounts.goods_api import (
    check_revision,
    page_limit,
    parse_int_id,
    parse_uuid,
    resource_dto,
)
from accounts.goods_models import HumanIdentity, RoleAssignment, RoleGrant, SecurityGuard, Staff, StaffAssignment
from accounts.goods_setup import GrantRequest, add_grant, is_tenant_staff
from accounts.models import Role, User
from accounts.principal import AccessContext, configured_role_access, effective_grants
from accounts.role_assignments import effective_assignments
from accounts.sessions import bump_security_epoch
from accounts.till_pin import may_hold_till_pin, may_reset_till_pin
from core.commands import CommandRun, LockRank
from core.kernel_models import AuditEvent, PrivilegedReview
from core.refusals import Refusal, issue
from masters.goods_models import (
    ConfigDraft,
    ConfigVersion,
    EffectiveVersionPeriod,
    MasterVersion,
    Sbu,
)
from masters.models import Brand, LegalEntity, Store

# -- command action names (the audit subject the privileged-change list reads) --
ACTION_STAFF_CREATE = "staff.create"
ACTION_STAFF_UPDATE = "staff.update"
ACTION_STAFF_RETIRE = "staff.retire"
ACTION_STAFF_ASSIGN = "staff.assign"
ACTION_USER_CREATE = "access.user.create"
ACTION_USER_UPDATE = "access.user.update"
ACTION_GRANT_CHANGE = "access.grant.change"
ACTION_ROLE_CREATE = "access.role.create"
ACTION_ROLE_UPDATE = "access.role.update"
ACTION_ROLE_ACCESS = "access.role.access"
ACTION_REVIEW = "access.privileged_change.review"

#: Every registered privileged command kind (``accounts.actions``), including the
#: export and recovery families owned elsewhere. Discovery is by kind, never by a
#: name prefix: change PRD §14.5 P6's temporary adapter is gone (GSA-T18).
PRIVILEGED_ACTIONS = frozenset(PRIVILEGED_COMMAND_ACTIONS)
#: The kinds this module writes are registered there too; a change that dropped one
#: off the review screen is caught by
#: ``tests/test_goods_exports.py::test_this_modules_own_kinds_are_all_registered``
#: rather than by an ``assert`` that ``python -O`` strips.

SCOPE_KINDS = ("tenant", "entity", "site", "sbu", "brand")
STAFF_KEYS = frozenset(
    {"human_id", "staff_code", "display_name", "mobile", "salesperson", "site_id", "effective_from", "registration_email"}
)
USER_KEYS = frozenset(
    {"human_id", "email", "display_name", "active", "password", "identity_email_confirmed"}
)
ROLE_KEYS = frozenset({"code", "name", "description", "active"})
GRANT_KEYS = frozenset(
    {
        "human_id",
        "role_id",
        "scope",
        "actions",
        "fields",
        "effective_from",
        "effective_to",
        "revokes",
    }
)
SCOPE_KEYS = frozenset({"scope_kind", "entity_id", "site_id", "sbu_id", "brand_id"})
SECRET_FIELDS = frozenset({"password", "password_hash", "token", "token_hash", "csrf_hash"})
ROLE_CODE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_GRANT_ITEMS = 100


# -- small input and output helpers -----------------------------------------------


def invalid(message: str, field_name: str | None = None) -> Refusal:
    issues = [issue("INVALID", message, field=field_name)] if field_name else None
    return Refusal("INVALID_REQUEST", message, issues=issues)


def access_invalid(message: str, issues: list[dict[str, Any]] | None = None) -> Refusal:
    return Refusal("ACCESS_INVALID", message, status=422, issues=issues)


def staff_invalid(message: str, field_name: str | None = None) -> Refusal:
    issues = [issue("STAFF_INVALID", message, field=field_name)] if field_name else None
    return Refusal("STAFF_INVALID", message, status=422, issues=issues)


def not_found(what: str = "record") -> Refusal:
    return Refusal("NOT_FOUND", f"That {what} was not found.")


def text_field(
    body: dict[str, Any], name: str, max_length: int, *, required: bool = False
) -> str | None:
    value = body.get(name)
    if value is None:
        if required:
            raise invalid(f"{name} is required.", name)
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise invalid(f"{name} must be text of 1 to {max_length} characters.", name)
    return value.strip()


def bool_field(body: dict[str, Any], name: str) -> bool | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise invalid(f"{name} must be true or false.", name)
    return value


def timestamp_field(body: dict[str, Any], name: str, *, required: bool = False) -> datetime | None:
    value = body.get(name)
    if value is None:
        if required:
            raise invalid(f"{name} is required.", name)
        return None
    parsed = parse_datetime(value) if isinstance(value, str) and len(value) <= 40 else None
    if parsed is None or timezone.is_naive(parsed):
        raise invalid(f"{name} must be a timestamp with a time zone.", name)
    return parsed


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def ref(value: Any) -> str | None:
    return None if value is None else str(value)


def audit_values(values: dict[str, Any], *, redacted: Iterable[str] = ()) -> list[dict[str, Any]]:
    """``SafeAuditValues``: secrets are never recorded, not even as a redacted marker."""
    hidden = set(redacted)
    out: list[dict[str, Any]] = []
    for name, value in values.items():
        if name in SECRET_FIELDS:
            continue
        if name in hidden:
            out.append({"field": name[:100], "redacted": True, "value": None})
        else:
            text = None if value is None else str(value)[:500]
            out.append({"field": name[:100], "redacted": False, "value": text})
    return out


def safe_values(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or str(entry.get("field")) in SECRET_FIELDS:
            continue
        redacted = bool(entry.get("redacted"))
        out.append(
            {
                "field": str(entry.get("field"))[:100],
                "redacted": redacted,
                "value": None if redacted else entry.get("value"),
            }
        )
    return out


def resolve_site(site_id: int) -> Store:
    site = Store.objects.filter(pk=site_id).first()
    if site is None:
        raise not_found("site")
    return site


def require_personal(access: AccessContext, site_id: int | None) -> None:
    actions, personal = {"staff.manage", "staff.retire"}, {"personal"}
    covered = (
        access.covers_store(actions, site_id, personal)
        if site_id is not None
        else access.covers_all(actions, [(None, None)], personal)
    )
    if not covered:
        raise Refusal("ACTION_DENIED", "You do not have permission to see or change personal data.")


# -- versions and locks ---------------------------------------------------------------


def current_revision(kind: str, target_key: str) -> int:
    top = MasterVersion.objects.filter(kind=kind, target_key=target_key).aggregate(
        top=Max("revision")
    )
    return int(top["top"] or 1)


def latest_payload(kind: str, target_key: str) -> dict[str, Any]:
    row = (
        MasterVersion.objects.filter(kind=kind, target_key=target_key).order_by("-revision").first()
    )
    return row.payload if row is not None and isinstance(row.payload, dict) else {}


def record_version(
    run: CommandRun,
    *,
    kind: str,
    target_key: str,
    revision: int,
    payload: dict[str, Any],
    reason_code: str | None = None,
    effective_from: datetime | None = None,
) -> Any:
    return run.record(
        MasterVersion(
            kind=kind,
            target_key=target_key[:100],
            revision=revision,
            payload=payload,
            reason_code=reason_code,
            effective_from=effective_from or run.now,
        )
    )


def lock_person(run: CommandRun, human_id: uuid.UUID) -> None:
    """The person's security guard: every access or assignment change serialises on it."""
    SecurityGuard.objects.get_or_create(tenant_id=run.tenant_id, human_id=human_id)
    run.lock(
        LockRank.SECURITY, SecurityGuard.objects.filter(tenant_id=run.tenant_id, human_id=human_id)
    )


# -- staff -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    assignment: StaffAssignment | None
    period: EffectiveVersionPeriod | None

    @property
    def site_id(self) -> int | None:
        return self.assignment.site_id if self.assignment is not None else None

    @property
    def effective_from(self) -> datetime | None:
        return self.assignment.effective_from if self.assignment is not None else None


NO_PLACEMENT = Placement(None, None)


@dataclass(frozen=True)
class StaffFields:
    present: frozenset[str]
    human_id: uuid.UUID | None = None
    staff_code: str | None = None
    registration_email: str | None = None
    display_name: str | None = None
    mobile: str | None = None
    salesperson: bool | None = None
    site_id: int | None = None
    effective_from: datetime | None = None

    def as_json(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "human_id": ref(self.human_id),
            "staff_code": self.staff_code,
            "registration_email": self.registration_email,
            "display_name": self.display_name,
            "mobile": self.mobile,
            "salesperson": self.salesperson,
            "site_id": ref(self.site_id),
            "effective_from": iso(self.effective_from),
        }
        return {key: values[key] for key in sorted(self.present)}


def parse_staff_fields(body: dict[str, Any]) -> StaffFields:
    mobile = None
    if body.get("mobile") is not None:
        mobile = text_field(body, "mobile", 30)
        if mobile is not None and not re.fullmatch(r"[0-9+()\- ]{3,30}", mobile):
            raise invalid("mobile must be a phone number.", "mobile")
    return StaffFields(
        present=frozenset(body),
        human_id=parse_uuid(body["human_id"], "human_id")
        if body.get("human_id") is not None
        else None,
        staff_code=text_field(body, "staff_code", 40),
        registration_email=text_field(body, "registration_email", 120),
        display_name=text_field(body, "display_name", 160),
        mobile=mobile,
        salesperson=bool_field(body, "salesperson"),
        site_id=parse_int_id(body["site_id"], "site_id")
        if body.get("site_id") is not None
        else None,
        effective_from=timestamp_field(body, "effective_from"),
    )


def assignment_scope_key(staff_id: uuid.UUID) -> str:
    return f"staff-primary:{staff_id}"


def placements(staff_ids: list[uuid.UUID]) -> dict[uuid.UUID, Placement]:
    """Each staff member's latest primary assignment and its current period."""
    rows = list(
        StaffAssignment.objects.filter(staff_id__in=staff_ids, primary=True).order_by(
            "effective_from", "recorded_at"
        )
    )
    periods = {
        period.target_id: period
        for period in EffectiveVersionPeriod.objects.filter(
            target_kind="assignment", target_id__in=[row.pk for row in rows]
        )
    }
    found: dict[uuid.UUID, Placement] = {}
    for row in rows:
        period = periods.get(row.pk)
        if period is None:
            continue  # a future assignment cancelled by retirement never took effect
        found[row.staff_id] = Placement(row, period)
    return found


def staff_visible(access: AccessContext, site_id: int | None) -> bool:
    sites = access.store_site_ids("staff.manage")
    return sites is None or (site_id is not None and site_id in sites)


def get_staff(access: AccessContext, staff_id: uuid.UUID) -> tuple[Staff, Placement]:
    access.require_action("staff.manage")
    staff = (
        Staff.objects.select_related("human")
        .filter(tenant_id=access.tenant_id, pk=staff_id)
        .first()
    )
    if staff is None:
        raise not_found("staff member")
    placement = placements([staff.pk]).get(staff.pk, NO_PLACEMENT)
    if not staff_visible(access, placement.site_id):
        raise not_found("staff member")
    return staff, placement


def list_staff(access: AccessContext, params: dict[str, str]) -> list[tuple[Staff, Placement]]:
    access.require_action("staff.manage")
    site_filter: int | None = None
    if params.get("site_id"):
        site_filter = parse_int_id(params["site_id"], "site_id")
        resolve_site(site_filter)
        if not access.can_at_store("staff.manage", site_filter):
            raise not_found("site")
    query = (params.get("q") or "").strip().lower()
    if len(query) > 100:
        raise invalid("q must be at most 100 characters.", "q")
    rows = list(
        Staff.objects.select_related("human")
        .filter(tenant_id=access.tenant_id)
        .order_by("human__staff_code", "id")
    )
    placed = placements([row.pk for row in rows])
    found: list[tuple[Staff, Placement]] = []
    for staff in rows:
        placement = placed.get(staff.pk, NO_PLACEMENT)
        if not staff_visible(access, placement.site_id):
            continue
        if site_filter is not None and placement.site_id != site_filter:
            continue
        names = f"{staff.human.staff_code} {staff.human.display_name}".lower()
        if query and query not in names:
            continue
        found.append((staff, placement))
    return found


def staff_dto(access: AccessContext, staff: Staff, placement: Placement) -> dict[str, Any]:
    site_id = placement.site_id
    data: dict[str, Any] = {
        "human_id": str(staff.human_id),
        "staff_code": staff.human.staff_code,
        "display_name": staff.human.display_name,
        "salesperson": staff.salesperson,
        "site_id": ref(site_id),
        "effective_from": iso(placement.effective_from),
    }
    if "personal" in access.field_grants(
        site_id=site_id, actions={"staff.manage"}, store_record=site_id is not None
    ):
        data["mobile"] = staff.mobile
    allowed: list[str] = []
    if staff.retired_at is None:
        if access.can_at_store("staff.manage", site_id):
            allowed += ["update", "assign"]
        if access.can_at_store("staff.retire", site_id):
            allowed.append("retire")
    return resource_dto(
        id=staff.pk,
        data=data,
        revision=staff.revision,
        state="retired" if staff.retired_at is not None else "active",
        context={"site_id": site_id},
        allowed_actions=allowed,
    )


def assignment_dto(staff: Staff, assignment_id: uuid.UUID) -> dict[str, Any]:
    row = StaffAssignment.objects.get(pk=assignment_id)
    period = EffectiveVersionPeriod.objects.filter(
        target_kind="assignment", target_id=row.pk
    ).first()
    effective_to = period.effective_to if period is not None else row.effective_to
    now = timezone.now()
    if effective_to is not None and effective_to <= now:
        state = "closed"
    elif row.effective_from > now:
        state = "scheduled"
    else:
        state = "effective"
    return resource_dto(
        id=row.pk,
        data={
            "staff_id": str(row.staff_id),
            "site_id": str(row.site_id),
            "effective_from": iso(row.effective_from),
            "effective_to": iso(effective_to),
            "primary": row.primary,
        },
        revision=staff.revision,
        state=state,
        context={"site_id": row.site_id},
    )


def _staff_code_taken(tenant_id: uuid.UUID, code: str, exclude: uuid.UUID | None) -> bool:
    rows = HumanIdentity.objects.filter(tenant_id=tenant_id, staff_code__iexact=code)
    if exclude is not None:
        rows = rows.exclude(pk=exclude)
    return rows.exists()


def _staff_payload(staff: Staff, human: HumanIdentity, site_id: int | None) -> dict[str, Any]:
    # Personal data stays on the projection; the version records only that it exists.
    return {
        "human_id": str(human.pk),
        "staff_code": human.staff_code,
        "display_name": human.display_name,
        "salesperson": staff.salesperson,
        "mobile_recorded": bool(staff.mobile),
        "site_id": ref(site_id),
        "retired_at": iso(staff.retired_at),
    }


def _staff_audit(staff: Staff, human: HumanIdentity, site_id: int | None) -> list[dict[str, Any]]:
    return audit_values(
        {
            "staff_code": human.staff_code,
            "display_name": human.display_name,
            "salesperson": staff.salesperson,
            "mobile": None,
            "site_id": site_id,
            "retired_at": iso(staff.retired_at),
        },
        redacted={"mobile"},
    )


def open_assignment(
    run: CommandRun, staff: Staff, site_id: int, effective_from: datetime
) -> StaffAssignment:
    assignment: StaffAssignment = run.record(
        StaffAssignment(
            staff_id=staff.pk, site_id=site_id, effective_from=effective_from, primary=True
        )
    )
    EffectiveVersionPeriod.objects.create(
        tenant_id=run.tenant_id,
        target_kind="assignment",
        target_id=assignment.pk,
        scope_key=assignment_scope_key(staff.pk),
        effective_from=effective_from,
    )
    return assignment


def move_assignment(
    run: CommandRun,
    staff: Staff,
    placement: Placement,
    *,
    site_id: int,
    effective_from: datetime,
    code: str,
) -> StaffAssignment:
    """Close the current primary period and open the next one; history is never rewritten."""
    if staff.retired_at is not None:
        raise Refusal(code, "A retired staff member cannot be reassigned.")
    start = placement.effective_from
    if start is not None:
        if effective_from <= start:
            raise Refusal(
                code, "The new assignment must start after the current assignment started."
            )
        if placement.site_id == site_id:
            raise Refusal(code, "This person is already assigned to that site.")
    later = EffectiveVersionPeriod.objects.filter(
        tenant_id=run.tenant_id,
        target_kind="assignment",
        scope_key=assignment_scope_key(staff.pk),
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=effective_from))
    for period in later:
        if period.effective_from >= effective_from:
            raise Refusal(code, "Another primary assignment already covers that time.")
        period.effective_to = effective_from
        period.save(update_fields=["effective_to"])
    return open_assignment(run, staff, site_id, effective_from)


def lock_staff(run: CommandRun, staff_id: uuid.UUID) -> Staff:
    human_id = (
        Staff.objects.filter(tenant_id=run.tenant_id, pk=staff_id)
        .values_list("human_id", flat=True)
        .first()
    )
    if human_id is None:
        raise not_found("staff member")
    lock_person(run, human_id)
    run.lock(LockRank.SECURITY, Staff.objects.filter(tenant_id=run.tenant_id, pk=staff_id))
    return Staff.objects.select_related("human").get(pk=staff_id)


def create_staff(run: CommandRun, *, fields: StaffFields) -> Staff:
    assert fields.staff_code and fields.display_name
    # Head-office staff have no site (GSA-T03): no assignment row at all, rather
    # than an assignment with an invented or wildcard site. The view has already
    # refused half a pair, so these two are absent or present together.
    assert (fields.site_id is None) == (fields.effective_from is None)
    run.advisory_lock(LockRank.SECURITY, [f"staff-code:{fields.staff_code.lower()}"])
    if fields.human_id is not None:
        lock_person(run, fields.human_id)
        existing = HumanIdentity.objects.filter(tenant_id=run.tenant_id, pk=fields.human_id).first()
        if existing is None or Staff.objects.filter(human_id=fields.human_id).exists():
            raise staff_invalid("That person is unknown or already has a staff record.", "human_id")
        # GSA-T03: a platform (X-PLT) or service (X-SVC) role alone does not make
        # someone tenant staff - refuse the same way as "unknown or already
        # staffed" so the refusal gives nothing away about which is true.
        role_codes = [g.role_code for g in effective_grants(fields.human_id)]
        if not is_tenant_staff(role_codes):
            raise staff_invalid("That person is unknown or already has a staff record.", "human_id")
        human = existing
    else:
        human = HumanIdentity(tenant_id=run.tenant_id)
    if _staff_code_taken(run.tenant_id, fields.staff_code, human.pk if fields.human_id else None):
        raise staff_invalid("Another person already uses that staff code.", "staff_code")
    from accounts.registration_services import claim_signup_code

    claim_signup_code(run.tenant_id, fields.staff_code, fields.registration_email, human.pk)
    human.staff_code = fields.staff_code
    human.display_name = fields.display_name
    human.save()
    SecurityGuard.objects.get_or_create(tenant_id=run.tenant_id, human_id=human.pk)
    staff = Staff.objects.create(
        tenant_id=run.tenant_id,
        human=human,
        mobile=fields.mobile,
        salesperson=bool(fields.salesperson),
        revision=1,
    )
    if fields.site_id is not None and fields.effective_from is not None:
        open_assignment(run, staff, fields.site_id, fields.effective_from)
    record_version(
        run,
        kind="staff",
        target_key=str(staff.pk),
        revision=1,
        payload=_staff_payload(staff, human, fields.site_id),
        effective_from=fields.effective_from,
    )
    run.audit_subject_key = f"staff:{staff.pk}"
    run.audit_site_id = fields.site_id
    run.audit_after = _staff_audit(staff, human, fields.site_id)
    return staff


def update_staff(
    run: CommandRun, *, staff_id: uuid.UUID, expected_revision: int | None, fields: StaffFields
) -> Staff:
    staff = lock_staff(run, staff_id)
    check_revision(expected_revision, staff.revision)
    if staff.retired_at is not None:
        raise staff_invalid("A retired staff member cannot be changed.")
    human = staff.human
    placement = placements([staff.pk]).get(staff.pk, NO_PLACEMENT)
    run.audit_before = _staff_audit(staff, human, placement.site_id)
    if fields.human_id is not None and fields.human_id != human.pk:
        raise staff_invalid("A staff record always belongs to the same person.", "human_id")
    if fields.staff_code is not None and fields.staff_code != human.staff_code:
        if _staff_code_taken(run.tenant_id, fields.staff_code, human.pk):
            raise staff_invalid("Another person already uses that staff code.", "staff_code")
        from accounts.registration_services import claim_signup_code

        claim_signup_code(run.tenant_id, fields.staff_code, fields.registration_email, human.pk)
        human.staff_code = fields.staff_code
    if fields.display_name is not None:
        human.display_name = fields.display_name
    if "mobile" in fields.present:
        staff.mobile = fields.mobile
    if fields.salesperson is not None:
        staff.salesperson = fields.salesperson
    site_id = placement.site_id
    moved = False
    if fields.site_id is not None and fields.site_id != placement.site_id:
        if fields.effective_from is None:
            raise staff_invalid("Moving a staff member needs effective_from.", "effective_from")
        move_assignment(
            run,
            staff,
            placement,
            site_id=fields.site_id,
            effective_from=fields.effective_from,
            code="STAFF_ASSIGNMENT_CONFLICT",
        )
        site_id, moved = fields.site_id, True
    elif fields.effective_from is not None and fields.effective_from != placement.effective_from:
        raise Refusal(
            "STAFF_ASSIGNMENT_CONFLICT",
            "An assignment's start is history; move the person with a new assignment instead.",
        )
    human.save()
    staff.revision += 1
    staff.save()
    record_version(
        run,
        kind="staff",
        target_key=str(staff.pk),
        revision=staff.revision,
        payload=_staff_payload(staff, human, site_id),
    )
    if moved:
        bump_security_epoch(human.pk, run.tenant_id)
    run.audit_subject_key = f"staff:{staff.pk}"
    run.audit_site_id = site_id
    run.audit_after = _staff_audit(staff, human, site_id)
    return staff


def assign_staff(
    run: CommandRun,
    *,
    staff_id: uuid.UUID,
    expected_revision: int | None,
    site_id: int,
    effective_from: datetime,
    reason_code: str,
) -> tuple[Staff, StaffAssignment]:
    staff = lock_staff(run, staff_id)
    check_revision(expected_revision, staff.revision)
    placement = placements([staff.pk]).get(staff.pk, NO_PLACEMENT)
    run.audit_before = audit_values({"site_id": placement.site_id})
    assignment = move_assignment(
        run,
        staff,
        placement,
        site_id=site_id,
        effective_from=effective_from,
        code="ASSIGNMENT_CONFLICT",
    )
    staff.revision += 1
    staff.save(update_fields=["revision"])
    record_version(
        run,
        kind="staff",
        target_key=str(staff.pk),
        revision=staff.revision,
        payload=_staff_payload(staff, staff.human, site_id),
        reason_code=reason_code,
        effective_from=effective_from,
    )
    bump_security_epoch(staff.human_id, run.tenant_id)
    run.audit_subject_key = f"staff:{staff.pk}"
    run.audit_site_id = site_id
    run.audit_after = audit_values(
        {"site_id": site_id, "effective_from": iso(effective_from), "reason_code": reason_code}
    )
    return staff, assignment


def retire_staff(
    run: CommandRun,
    *,
    access: AccessContext,
    staff_id: uuid.UUID,
    expected_revision: int | None,
    effective_at: datetime,
    reason_code: str,
) -> Staff:
    from alerts.goods_models import GoodsException

    staff = lock_staff(run, staff_id)
    check_revision(expected_revision, staff.revision)
    human = staff.human
    placement = placements([staff.pk]).get(staff.pk, NO_PLACEMENT)
    run.audit_before = _staff_audit(staff, human, placement.site_id)
    if staff.retired_at is not None:
        raise Refusal("RETIREMENT_BLOCKED", "This staff member is already retired.")
    periods = list(
        EffectiveVersionPeriod.objects.filter(
            tenant_id=run.tenant_id,
            target_kind="assignment",
            scope_key=assignment_scope_key(staff.pk),
        ).order_by("effective_from")
    )
    started = [period for period in periods if period.effective_from < effective_at]
    if periods and not started:
        raise Refusal(
            "RETIREMENT_BLOCKED",
            "Retirement must come after the person's first assignment started.",
        )
    current_site = placement.site_id
    if started:
        current_site = (
            StaffAssignment.objects.filter(pk=started[-1].target_id)
            .values_list("site_id", flat=True)
            .first()
        )
    owned = list(
        GoodsException.objects.filter(
            tenant_id=run.tenant_id, owner_human_id=human.pk, state="open"
        ).values_list("pk", flat=True)[:50]
    )
    if owned:
        raise Refusal(
            "RETIREMENT_BLOCKED",
            "This person still owns open exceptions. Hand them over before retiring them.",
            issues=[
                issue("HANDOVER_REQUIRED", f"Exception {pk} is still owned by this person.")
                for pk in owned
            ],
        )
    closed: list[str] = []
    cancelled: list[str] = []
    for period in periods:
        if period.effective_from >= effective_at:
            # A future assignment never takes effect: its evidence row keeps the history.
            cancelled.append(str(period.target_id))
            period.delete()
        elif period.effective_to is None or period.effective_to > effective_at:
            closed.append(str(period.target_id))
            period.effective_to = effective_at
            period.save(update_fields=["effective_to"])
    # Retirement ends application authority, including scheduled rows. Check the
    # actor against each complete scope before touching it; staff placement alone
    # must not let a local manager retire somebody's wider assignment.
    authority_rows = list(
        RoleAssignment.objects.select_for_update()
        .filter(tenant_id=run.tenant_id, human_id=human.pk)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=effective_at))
        .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gt=effective_at))
        .order_by("effective_from", "id")
    )
    for assignment in authority_rows:
        for site_id in ([None] if assignment.all_sites else assignment.site_ids):
            for brand_id in ([None] if assignment.all_brands else assignment.brand_ids):
                access.require("access.manage", site_id=site_id, brand_id=brand_id)
    revoked: list[str] = []
    for assignment in authority_rows:
        if assignment.effective_from >= effective_at:
            assignment.revoked_at = effective_at
            assignment.save(update_fields=["revoked_at"])
        else:
            assignment.effective_to = effective_at
            assignment.save(update_fields=["effective_to"])
        revoked.append(str(assignment.pk))
    staff.retired_at = effective_at
    staff.revision += 1
    staff.save(update_fields=["retired_at", "revision"])
    if effective_at <= run.now:
        human.active = False
        human.save(update_fields=["active"])
        User.objects.filter(human_id=human.pk).update(is_active=False)
    record_version(
        run,
        kind="staff",
        target_key=str(staff.pk),
        revision=staff.revision,
        payload={**_staff_payload(staff, human, current_site), "retired": True},
        reason_code=reason_code,
        effective_from=effective_at,
    )
    bump_security_epoch(human.pk, run.tenant_id)
    run.audit_subject_key = f"staff:{staff.pk}"
    run.audit_site_id = current_site
    run.audit_after = audit_values(
        {
            "retired_at": iso(effective_at),
            "reason_code": reason_code,
            "role_assignments_revoked": revoked,
            "assignments_closed": closed,
            "assignments_cancelled": cancelled,
        }
    )
    return staff


# -- logins --------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginFields:
    present: frozenset[str]
    human_id: uuid.UUID | None = None
    email: str | None = None
    display_name: str | None = None
    active: bool | None = None
    password: str | None = None
    #: GSA-T03/ticket 03A: the administrator's identity/email-ownership confirmation.
    #: Required, and must be true, whenever ``password`` is present - on a create or
    #: an assisted reset alike, since the ticket treats confirming the person and
    #: issuing them a temporary password as one bundled act.
    identity_email_confirmed: bool | None = None

    def fingerprint_input(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "human_id": ref(self.human_id),
            "email": self.email,
            "display_name": self.display_name,
            "active": self.active,
            # Design §6.2: never a password-derived value in command evidence. Only
            # that a password was sent - so a retry of the same command id with a
            # different password replays the first answer instead of conflicting.
            "password": self.password is not None,
            "identity_email_confirmed": self.identity_email_confirmed,
        }
        return {key: values[key] for key in sorted(self.present)}


def parse_login(body: dict[str, Any]) -> LoginFields:
    email = text_field(body, "email", 254)
    if email is not None:
        email = email.lower()
        try:
            validate_email(email)
        except ValidationError:
            raise invalid("email must be an email address.", "email") from None
    password = body.get("password")
    if password is not None and (not isinstance(password, str) or not 1 <= len(password) <= 1024):
        raise invalid("password must be text of 1 to 1024 characters.", "password")
    identity_email_confirmed = bool_field(body, "identity_email_confirmed")
    if identity_email_confirmed is not None and password is None:
        raise invalid(
            "identity_email_confirmed only means something when password is sent too.",
            "identity_email_confirmed",
        )
    if password is not None and identity_email_confirmed is not True:
        raise access_invalid(
            "Confirm this person's identity and ownership of the login email before "
            "issuing them a password.",
            [
                issue(
                    "IDENTITY_NOT_CONFIRMED",
                    "identity_email_confirmed must be true when password is sent.",
                    field="identity_email_confirmed",
                )
            ],
        )
    return LoginFields(
        present=frozenset(body),
        human_id=parse_uuid(body["human_id"], "human_id")
        if body.get("human_id") is not None
        else None,
        email=email,
        display_name=text_field(body, "display_name", 160),
        active=bool_field(body, "active"),
        password=password,
        identity_email_confirmed=identity_email_confirmed,
    )


def goods_user(tenant_id: uuid.UUID, pk: int) -> User:
    user: User | None = (
        User.objects.select_related("human")
        .filter(tenant_id=tenant_id, human__isnull=False, pk=pk)
        .first()
    )
    if user is None:
        raise not_found("login")
    return user


def list_users(access: AccessContext, params: dict[str, str]) -> list[User]:
    # Login identities and password resets are tenant-wide administration until
    # OQ-28 defines a safe delegated responsibility. A scoped assignment must
    # not reveal accounts elsewhere through an unfiltered list or count.
    access.require("access.manage")
    rows = User.objects.select_related("human").filter(
        tenant_id=access.tenant_id, human__isnull=False
    )
    query = (params.get("q") or "").strip()
    if len(query) > 100:
        raise invalid("q must be at most 100 characters.", "q")
    if query:
        rows = rows.filter(
            Q(email__icontains=query)
            | Q(human__display_name__icontains=query)
            | Q(human__staff_code__icontains=query)
        )
    if params.get("site_id"):
        site_id = parse_int_id(params["site_id"], "site_id")
        resolve_site(site_id)
        if not access.can("access.manage", site_id=site_id):
            raise not_found("site")
        now = timezone.now()
        humans = RoleAssignment.objects.filter(
            tenant_id=access.tenant_id,
            effective_from__lte=now,
            role__is_active=True,
            human__active=True,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now)).filter(
            Q(all_sites=True) | Q(site_ids__contains=[site_id])
        ).filter(Q(all_brands=True) | ~Q(brand_ids=[]))
        rows = rows.filter(human_id__in=humans.values("human_id"))
    return list(rows.order_by("email", "id"))


def user_dto(access: AccessContext, user: User, *, with_pin_state: bool = False) -> dict[str, Any]:
    human = user.human
    data: dict[str, Any] = {
        "human_id": ref(user.human_id),
        "email": user.email,
        "display_name": human.display_name if human is not None else user.full_name,
        "active": bool(user.is_active and human is not None and human.active),
        "must_change_password": bool(user.must_change_password),
    }
    if with_pin_state:
        # Store operations ticket 06: on the login's own page, whether there is a
        # counter PIN and whether this login may hold one - never what it is.
        data["has_till_pin"] = bool(user.till_pin_hash)
        data["may_hold_till_pin"] = may_hold_till_pin(user)
        # Whether the *reader* may set or clear it (Admin, B76).
        data["may_reset_till_pin"] = access.can("access.manage") and may_reset_till_pin(
            access.user
        )
    return resource_dto(
        id=user.pk,
        data=data,
        revision=current_revision("user", str(user.pk)),
        state="active" if data["active"] else "inactive",
        context={},
        allowed_actions=["update"] if access.can("access.manage") else [],
    )


def _user_payload(user: User, human: HumanIdentity) -> dict[str, Any]:
    return {
        "human_id": str(human.pk),
        "email": user.email,
        "display_name": human.display_name,
        "active": user.is_active,
    }


def apply_password(user: User, password: str | None) -> None:
    if password is None:
        user.set_unusable_password()
        return
    try:
        password_validation.validate_password(password, user=user)
    except ValidationError as exc:
        raise access_invalid(
            "That password does not meet the password rules.",
            [issue("PASSWORD_REJECTED", message, field="password") for message in exc.messages],
        ) from None
    user.set_password(password)


def create_user(run: CommandRun, *, login: LoginFields) -> User:
    assert login.human_id and login.email and login.display_name
    run.advisory_lock(LockRank.SECURITY, [f"login-email:{login.email}"])
    lock_person(run, login.human_id)
    human = HumanIdentity.objects.filter(tenant_id=run.tenant_id, pk=login.human_id).first()
    if human is None:
        raise not_found("person")
    from accounts.registration_services import validate_signup_login

    validate_signup_login(run.tenant_id, human.pk, login.email)
    problems: list[dict[str, Any]] = []
    if User.objects.filter(human_id=human.pk).exists():
        problems.append(
            issue("HUMAN_HAS_LOGIN", "This person already has a login.", field="human_id")
        )
    if User.objects.filter(
        Q(email__iexact=login.email) | Q(username__iexact=login.email[:60])
    ).exists():
        problems.append(
            issue("EMAIL_TAKEN", "Another login already uses this email.", field="email")
        )
    if not human.active:
        problems.append(issue("PERSON_INACTIVE", "This person is not active.", field="human_id"))
    if problems:
        raise access_invalid("This login cannot be created.", problems)
    active = True if login.active is None else login.active
    user = User(
        username=login.email[:60],
        full_name=login.display_name[:120],
        email=login.email,
        tenant_id=run.tenant_id,
        human_id=human.pk,
        is_active=active,
    )
    apply_password(user, login.password)
    if login.password is not None:
        user.must_change_password = True
    user.save()
    human.display_name = login.display_name
    human.save(update_fields=["display_name"])
    record_version(
        run, kind="user", target_key=str(user.pk), revision=1, payload=_user_payload(user, human)
    )
    bump_security_epoch(human.pk, run.tenant_id)
    run.audit_subject_key = f"user:{user.pk}"
    after: dict[str, Any] = {
        "human_id": human.pk,
        "email": login.email,
        "display_name": login.display_name,
        "active": active,
    }
    if login.password is not None:
        after["credential"] = None
        after["identity_email_confirmed"] = True
    run.audit_after = audit_values(after, redacted={"credential"})
    return user


def update_user(
    run: CommandRun,
    *,
    user_pk: int,
    expected_revision: int | None,
    login: LoginFields,
    actor_user_pk: int,
) -> User:
    human_id = (
        User.objects.filter(tenant_id=run.tenant_id, pk=user_pk, human__isnull=False)
        .values_list("human_id", flat=True)
        .first()
    )
    if human_id is None:
        raise not_found("login")
    if login.email is not None:
        run.advisory_lock(LockRank.SECURITY, [f"login-email:{login.email}"])
    lock_person(run, human_id)
    user: User = User.objects.select_related("human").get(pk=user_pk)
    human = user.human
    assert human is not None
    revision = current_revision("user", str(user.pk))
    check_revision(expected_revision, revision)
    run.audit_before = audit_values(
        {"email": user.email, "display_name": human.display_name, "active": user.is_active}
    )
    problems: list[dict[str, Any]] = []
    if login.human_id is not None and login.human_id != human.pk:
        problems.append(
            issue("HUMAN_FIXED", "A login always belongs to one person.", field="human_id")
        )
    changed_email = login.email is not None and login.email != (user.email or "").lower()
    if (
        changed_email
        and User.objects.filter(email__iexact=login.email).exclude(pk=user.pk).exists()
    ):
        problems.append(
            issue("EMAIL_TAKEN", "Another login already uses this email.", field="email")
        )
    if login.active is False and user.pk == actor_user_pk:
        problems.append(
            issue("SELF_LOCKOUT", "You cannot deactivate your own login.", field="active")
        )
    if problems:
        raise access_invalid("This login cannot be changed.", problems)
    after: dict[str, Any] = {}
    if changed_email and login.email is not None:
        user.email = login.email
        after["email"] = login.email
    if login.display_name is not None:
        human.display_name = login.display_name
        user.full_name = login.display_name[:120]
        after["display_name"] = login.display_name
    changed_active = login.active is not None and login.active != user.is_active
    if changed_active and login.active is not None:
        user.is_active = login.active
        after["active"] = login.active
    if login.password is not None:
        apply_password(user, login.password)
        user.must_change_password = True
        after["credential"] = None
        after["identity_email_confirmed"] = True
        # design §6.2: the confirmation event names the human, not only the login.
        after["human_id"] = human.pk
    user.save()
    human.save(update_fields=["display_name"])
    record_version(
        run,
        kind="user",
        target_key=str(user.pk),
        revision=revision + 1,
        payload=_user_payload(user, human),
    )
    if changed_email or changed_active or login.password is not None:
        bump_security_epoch(human.pk, run.tenant_id)
    run.audit_subject_key = f"user:{user.pk}"
    run.audit_after = audit_values(after, redacted={"credential"})
    return user


# -- grants ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantScope:
    scope_kind: str
    entity_id: int | None = None
    site_id: int | None = None
    sbu_id: uuid.UUID | None = None
    brand_id: int | None = None

    def key(self) -> str:
        ref_value = self.entity_id or self.site_id or self.sbu_id or self.brand_id or "-"
        return f"{self.scope_kind}:{ref_value}"

    def as_json(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "entity_id": ref(self.entity_id),
            "site_id": ref(self.site_id),
            "sbu_id": ref(self.sbu_id),
            "brand_id": ref(self.brand_id),
        }


@dataclass(frozen=True)
class GrantItem:
    human_id: uuid.UUID | None
    revokes: uuid.UUID | None
    role_id: int | None
    scope: GrantScope | None
    actions: tuple[str, ...]
    fields: tuple[str, ...]
    effective_from: datetime | None
    effective_to: datetime | None

    def as_json(self) -> dict[str, Any]:
        return {
            "human_id": ref(self.human_id),
            "revokes": ref(self.revokes),
            "role_id": ref(self.role_id),
            "scope": self.scope.as_json() if self.scope is not None else None,
            "actions": list(self.actions),
            "fields": list(self.fields),
            "effective_from": iso(self.effective_from),
            "effective_to": iso(self.effective_to),
        }


def scope_of(grant: RoleGrant) -> GrantScope:
    return GrantScope(
        scope_kind=grant.scope_kind,
        entity_id=grant.entity_id,
        site_id=grant.site_id,
        sbu_id=grant.sbu_id,
        brand_id=grant.brand_id,
    )


def parse_scope(raw: Any, name: str, *, kind_only: bool = False) -> GrantScope:
    if not isinstance(raw, dict) or set(raw) - SCOPE_KEYS:
        raise invalid(f"{name} must be a grant scope.", name)
    kind = raw.get("scope_kind")
    if kind not in SCOPE_KINDS:
        raise invalid(f"{name}.scope_kind must be one of {', '.join(SCOPE_KINDS)}.", name)
    named = {key for key in ("entity_id", "site_id", "sbu_id", "brand_id") if raw.get(key)}
    expected = set() if kind == "tenant" or kind_only else {f"{kind}_id"}
    if named != expected:
        raise access_invalid(
            "A grant scope names exactly the one reference its kind needs, or none for tenant.",
            [issue("SCOPE_INCONSISTENT", "scope references do not match scope_kind", field=name)],
        )
    return GrantScope(
        scope_kind=str(kind),
        entity_id=parse_int_id(raw["entity_id"], name) if "entity_id" in named else None,
        site_id=parse_int_id(raw["site_id"], name) if "site_id" in named else None,
        sbu_id=parse_uuid(raw["sbu_id"], name) if "sbu_id" in named else None,
        brand_id=parse_int_id(raw["brand_id"], name) if "brand_id" in named else None,
    )


def parse_names(raw: Any, name: str, limit: int) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > limit:
        raise invalid(f"{name} must be a list of at most {limit} names.", name)
    if any(not isinstance(item, str) or not item or len(item) > 100 for item in raw):
        raise invalid(f"{name} must contain names.", name)
    return tuple(sorted(set(raw)))


def parse_action_set(raw: Any, name: str, scope_kind: str | None) -> tuple[str, ...]:
    if not isinstance(raw, dict) or set(raw) - {"actions", "scope_kind"}:
        raise invalid(f"{name} must be an action set.", name)
    if raw.get("scope_kind") is not None and raw.get("scope_kind") != scope_kind:
        raise access_invalid(
            "An action set's scope kind must match its grant scope.",
            [issue("SCOPE_INCONSISTENT", "actions.scope_kind differs from scope", field=name)],
        )
    return parse_names(raw.get("actions"), name, 200)


def parse_grant_items(raw: Any, *, role_level: bool = False) -> list[GrantItem]:
    if not isinstance(raw, list) or len(raw) > MAX_GRANT_ITEMS or (not raw and not role_level):
        raise invalid(f"grants must be a list of 1 to {MAX_GRANT_ITEMS} grants.", "grants")
    items: list[GrantItem] = []
    for index, entry in enumerate(raw):
        label = f"grants[{index}]"
        if not isinstance(entry, dict) or set(entry) - GRANT_KEYS:
            raise invalid(f"{label} has unknown or missing fields.", label)
        human_id = parse_uuid(entry["human_id"], label) if entry.get("human_id") else None
        if entry.get("revokes") is not None:
            if role_level or set(entry) - {"revokes", "effective_from", "human_id"}:
                raise invalid(f"{label}: a revocation names only the grant it ends.", label)
            items.append(
                GrantItem(
                    human_id=human_id,
                    revokes=parse_uuid(entry["revokes"], label),
                    role_id=None,
                    scope=None,
                    actions=(),
                    fields=(),
                    effective_from=timestamp_field(entry, "effective_from"),
                    effective_to=None,
                )
            )
            continue
        scope = parse_scope(entry.get("scope"), f"{label}.scope", kind_only=role_level)
        effective_from = timestamp_field(entry, "effective_from", required=not role_level)
        items.append(
            GrantItem(
                human_id=human_id,
                revokes=None,
                role_id=parse_int_id(entry["role_id"], f"{label}.role_id")
                if entry.get("role_id") is not None
                else None,
                scope=scope,
                actions=parse_action_set(
                    entry.get("actions"), f"{label}.actions", scope.scope_kind
                ),
                fields=parse_names(entry.get("fields"), f"{label}.fields", 20),
                effective_from=effective_from,
                effective_to=timestamp_field(entry, "effective_to"),
            )
        )
    return items


def _has_tenant_manage(access: AccessContext) -> bool:
    return any(g.scope_kind == "tenant" and "access.manage" in g.actions for g in access.grants)


def require_tenant_manage(access: AccessContext) -> None:
    """Roles are shared by the whole tenant, so only a tenant-wide administrator changes them."""
    if not _has_tenant_manage(access):
        raise Refusal("ACTION_DENIED", "Only a tenant-wide administrator can change roles.")


def check_grant_scope(access: AccessContext, scope: GrantScope) -> None:
    """The actor may only grant inside their own access-management scope."""
    if scope.scope_kind == "tenant":
        if not _has_tenant_manage(access):
            raise Refusal("ACTION_DENIED", "Only a tenant-wide administrator grants tenant access.")
    elif scope.scope_kind == "entity":
        assert scope.entity_id is not None
        entity_ok = LegalEntity.objects.filter(pk=scope.entity_id).exists()
        own = _has_tenant_manage(access) or any(
            g.scope_kind == "entity"
            and g.entity_id == scope.entity_id
            and "access.manage" in g.actions
            for g in access.grants
        )
        if not entity_ok or not own:
            raise not_found("entity")
    elif scope.scope_kind == "site":
        assert scope.site_id is not None
        resolve_site(scope.site_id)
        access.require("access.manage", site_id=scope.site_id)
    elif scope.scope_kind == "sbu":
        assert scope.sbu_id is not None
        sbu = Sbu.objects.filter(tenant_id=access.tenant_id, pk=scope.sbu_id).first()
        if sbu is None:
            raise not_found("SBU")
        access.require("access.manage", site_id=sbu.site_id, brand_id=sbu.brand_id)
    else:
        assert scope.brand_id is not None
        if not Brand.objects.filter(pk=scope.brand_id).exists():
            raise not_found("brand")
        # A brand grant has no site limit: it reaches that brand at every entity's sites.
        if not _has_tenant_manage(access):
            raise Refusal(
                "ACTION_DENIED", "Only a tenant-wide administrator grants brand-wide access."
            )


def _covers_grant(access: AccessContext, grant: RoleGrant) -> bool:
    """Whether the actor's access-management scope includes every place ``grant`` reaches."""
    if _has_tenant_manage(access):
        return True
    if grant.scope_kind in ("tenant", "brand"):
        return False
    if grant.scope_kind == "entity":
        return any(
            g.scope_kind == "entity"
            and g.entity_id == grant.entity_id
            and "access.manage" in g.actions
            for g in access.grants
        )
    if grant.scope_kind == "site":
        return access.can("access.manage", site_id=grant.site_id)
    if grant.sbu is None:
        return False
    return access.can("access.manage", site_id=grant.sbu.site_id, brand_id=grant.sbu.brand_id)


def require_login_in_scope(access: AccessContext, user_pk: int) -> None:
    """A login is changed only if access.manage covers every current assignment cell.

    Explicit all-site/all-brand dimensions ask for that same future-proof reach
    from the actor. A login with no active assignment is tenant-wide for this
    purpose; otherwise a scoped administrator could take over an unassigned
    login and give it broader authority. Out-of-scope logins are hidden.
    """
    human_id = (
        User.objects.filter(tenant_id=access.tenant_id, pk=user_pk, human__isnull=False)
        .values_list("human_id", flat=True)
        .first()
    )
    if human_id is None:
        raise not_found("login")
    assignments = effective_assignments(human_id)
    if not assignments and not access.can("access.manage"):
        raise not_found("login")
    for assignment in assignments:
        site_ids = [None] if assignment.all_sites else assignment.site_ids
        brand_ids = [None] if assignment.all_brands else assignment.brand_ids
        for site_id in site_ids:
            for brand_id in brand_ids:
                if not access.can("access.manage", site_id=site_id, brand_id=brand_id):
                    raise not_found("login")
    staff = Staff.objects.filter(human_id=human_id).first()
    if staff is not None:
        placement = placements([staff.pk]).get(staff.pk, NO_PLACEMENT)
        if placement.site_id is not None and not access.can(
            "access.manage", site_id=placement.site_id
        ):
            raise not_found("login")


def check_item_scope(access: AccessContext, item: GrantItem, human_id: uuid.UUID) -> None:
    if item.revokes is not None:
        grant = RoleGrant.objects.filter(
            tenant_id=access.tenant_id, pk=item.revokes, human_id=human_id, revokes__isnull=True
        ).first()
        if grant is None:
            raise not_found("grant")
        check_grant_scope(access, scope_of(grant))
    elif item.scope is not None:
        check_grant_scope(access, item.scope)


def unrevoked_grants(
    tenant_id: uuid.UUID,
    *,
    human_id: uuid.UUID | None = None,
    sbu_id: uuid.UUID | None = None,
) -> list[RoleGrant]:
    rows = RoleGrant.objects.select_related("role", "sbu").filter(
        tenant_id=tenant_id, revokes__isnull=True
    )
    if human_id is not None:
        rows = rows.filter(human_id=human_id)
    if sbu_id is not None:
        rows = rows.filter(sbu_id=sbu_id)
    found = list(rows.order_by("effective_from", "id"))
    revoked = set(
        RoleGrant.objects.filter(
            tenant_id=tenant_id, revokes__in=[row.pk for row in found]
        ).values_list("revokes_id", flat=True)
    )
    return [row for row in found if row.pk not in revoked]


def live_grants(
    tenant_id: uuid.UUID, human_id: uuid.UUID | None = None
) -> list[tuple[RoleGrant, EffectiveVersionPeriod | None]]:
    """Grants not revoked and not yet ended (current and scheduled)."""
    now = timezone.now()
    rows = unrevoked_grants(tenant_id, human_id=human_id)
    periods = {
        period.target_id: period
        for period in EffectiveVersionPeriod.objects.filter(
            tenant_id=tenant_id, target_kind="grant", target_id__in=[row.pk for row in rows]
        )
    }
    found: list[tuple[RoleGrant, EffectiveVersionPeriod | None]] = []
    for row in rows:
        period = periods.get(row.pk)
        end = period.effective_to if period is not None else row.effective_to
        if end is not None and end <= now:
            continue
        found.append((row, period))
    return found


def grant_dto(grant: RoleGrant, period: EffectiveVersionPeriod | None) -> dict[str, Any]:
    actions = grant.action_set.get("actions", []) if isinstance(grant.action_set, dict) else []
    fields = grant.field_set if isinstance(grant.field_set, list) else []
    end = period.effective_to if period is not None else grant.effective_to
    return {
        "id": str(grant.pk),
        "human_id": str(grant.human_id),
        "role_id": str(grant.role_id),
        "role_code": grant.role.code,
        "scope": scope_of(grant).as_json(),
        "actions": {"actions": sorted(actions), "scope_kind": grant.scope_kind},
        "fields": sorted(fields),
        "effective_from": iso(grant.effective_from),
        "effective_to": iso(end),
    }


def grant_scope_key(human_id: uuid.UUID, role_code: str, scope: GrantScope) -> str:
    return f"grant:{human_id}:{role_code}:{scope.key()}"


def _overlaps(
    run: CommandRun,
    human_id: uuid.UUID,
    role: Role,
    scope: GrantScope,
    start: datetime,
    end: datetime | None,
) -> bool:
    window = Q(effective_to__isnull=True) | Q(effective_to__gt=start)
    periods = EffectiveVersionPeriod.objects.filter(
        tenant_id=run.tenant_id,
        target_kind="grant",
        scope_key=grant_scope_key(human_id, role.code, scope),
    ).filter(window)
    if end is not None:
        periods = periods.filter(effective_from__lt=end)
    if periods.exists():
        return True
    # Grants written without a period (deployment bootstrap) still count.
    refs: dict[str, Any] = {
        "entity_id": scope.entity_id,
        "site_id": scope.site_id,
        "sbu_id": scope.sbu_id,
        "brand_id": scope.brand_id,
    }
    same = RoleGrant.objects.filter(
        tenant_id=run.tenant_id,
        human_id=human_id,
        role_id=role.pk,
        scope_kind=scope.scope_kind,
        revokes__isnull=True,
        **refs,
    ).filter(window)
    if end is not None:
        same = same.filter(effective_from__lt=end)
    ids = list(same.values_list("pk", flat=True))
    if not ids:
        return False
    revoked = set(RoleGrant.objects.filter(revokes__in=ids).values_list("revokes_id", flat=True))
    with_period = set(
        EffectiveVersionPeriod.objects.filter(target_kind="grant", target_id__in=ids).values_list(
            "target_id", flat=True
        )
    )
    return any(pk not in revoked and pk not in with_period for pk in ids)


def role_maximum(role: Role) -> tuple[frozenset[str], frozenset[str], frozenset[str]] | None:
    """(actions, fields, scope kinds) a role may carry: its template, narrowed by E080."""
    template = ROLE_TEMPLATES.get(role.code)
    if template is None:
        return None
    configured = configured_role_access([role.code]).get(role.code)
    if configured is None:
        return template.actions, template.fields, template.scope_kinds
    kinds = template.scope_kinds & configured.scope_kinds if configured.scope_kinds else None
    return (
        template.actions & configured.actions,
        template.fields & configured.fields,
        kinds if kinds is not None else template.scope_kinds,
    )


def _add_item(
    run: CommandRun, human: HumanIdentity, item: GrantItem, label: str
) -> tuple[RoleGrant | None, list[dict[str, Any]]]:
    assert item.scope is not None
    role = Role.objects.filter(pk=item.role_id).first() if item.role_id is not None else None
    maximum = role_maximum(role) if role is not None and role.is_active else None
    if role is None or maximum is None:
        return None, [
            issue("ROLE_INVALID", "No active role with a registered maximum.", field=label)
        ]
    actions, fields, kinds = maximum
    problems: list[dict[str, Any]] = []
    if item.scope.scope_kind not in kinds:
        problems.append(
            issue("SCOPE_NOT_ALLOWED", f"{role.code} cannot be granted at that scope.", field=label)
        )
    unknown = [a for a in item.actions if a not in ACTIONS]
    beyond = [a for a in item.actions if a in ACTIONS and a not in actions]
    if not item.actions or unknown or beyond:
        detail = ", ".join(unknown + beyond) or "none named"
        problems.append(
            issue("ACTIONS_NOT_ALLOWED", f"Actions not allowed: {detail}.", field=label)
        )
    bad_fields = [f for f in item.fields if f not in FIELDS or f not in fields]
    if bad_fields:
        problems.append(
            issue(
                "FIELDS_NOT_ALLOWED", f"Fields not allowed: {', '.join(bad_fields)}.", field=label
            )
        )
    assert item.effective_from is not None
    if item.effective_to is not None and item.effective_to <= item.effective_from:
        problems.append(
            issue("PERIOD_INVALID", "effective_to must follow effective_from.", field=label)
        )
    if not problems and _overlaps(
        run, human.pk, role, item.scope, item.effective_from, item.effective_to
    ):
        problems.append(
            issue(
                "GRANT_OVERLAP", "This person already holds that role at that scope.", field=label
            )
        )
    if problems:
        return None, problems
    try:
        grant: RoleGrant = add_grant(
            run,
            human,
            GrantRequest(
                role_code=role.code,
                scope_kind=item.scope.scope_kind,
                entity_id=item.scope.entity_id,
                site_id=item.scope.site_id,
                sbu_id=item.scope.sbu_id,
                brand_id=item.scope.brand_id,
                actions=list(item.actions),
                fields=list(item.fields),
                effective_from=item.effective_from,
            ),
        )
    except Refusal as refused:
        return None, [issue("GRANT_INVALID", refused.message, field=label)]
    grant.effective_to = item.effective_to
    EffectiveVersionPeriod.objects.create(
        tenant_id=run.tenant_id,
        target_kind="grant",
        target_id=grant.pk,
        scope_key=grant_scope_key(human.pk, role.code, item.scope),
        effective_from=item.effective_from,
        effective_to=item.effective_to,
    )
    return grant, []


def revoke_grant(run: CommandRun, grant: RoleGrant, at: datetime) -> RoleGrant:
    """Revocation is a new grant row pointing at the one it ends; nothing is updated."""
    row: RoleGrant = run.record(
        RoleGrant(
            human_id=grant.human_id,
            role_id=grant.role_id,
            scope_kind=grant.scope_kind,
            entity_id=grant.entity_id,
            site_id=grant.site_id,
            sbu_id=grant.sbu_id,
            brand_id=grant.brand_id,
            effective_from=at,
            action_set={"actions": [], "scope_kind": grant.scope_kind},
            field_set=[],
            revokes_id=grant.pk,
        )
    )
    period = EffectiveVersionPeriod.objects.filter(
        tenant_id=run.tenant_id, target_kind="grant", target_id=grant.pk
    ).first()
    if period is not None:
        if period.effective_from >= at:
            period.delete()  # never became effective; the revocation row keeps the history
        elif period.effective_to is None or period.effective_to > at:
            period.effective_to = at
            period.save(update_fields=["effective_to"])
    return row


def _describe(grant: RoleGrant) -> str:
    actions = grant.action_set.get("actions", []) if isinstance(grant.action_set, dict) else []
    return (
        f"{grant.role.code if grant.role_id else ''} {scope_of(grant).key()} {len(actions)} actions"
    )


def change_grants(
    run: CommandRun,
    *,
    user_pk: int,
    expected_revision: int | None,
    items: list[GrantItem],
    reason_code: str,
) -> User:
    human_id = (
        User.objects.filter(tenant_id=run.tenant_id, pk=user_pk, human__isnull=False)
        .values_list("human_id", flat=True)
        .first()
    )
    if human_id is None:
        raise not_found("login")
    lock_person(run, human_id)
    user: User = User.objects.select_related("human").get(pk=user_pk)
    human = user.human
    assert human is not None
    revision = current_revision("user", str(user.pk))
    check_revision(expected_revision, revision)
    problems: list[dict[str, Any]] = []
    added: list[RoleGrant] = []
    revoked: list[RoleGrant] = []
    for index, item in enumerate(items):
        label = f"grants[{index}]"
        if item.human_id is not None and item.human_id != human.pk:
            problems.append(
                issue("HUMAN_MISMATCH", "A grant names a different person.", field=label)
            )
            continue
        if item.revokes is not None:
            target = (
                RoleGrant.objects.select_related("role")
                .filter(tenant_id=run.tenant_id, pk=item.revokes, human_id=human.pk)
                .filter(revokes__isnull=True)
                .first()
            )
            if target is None or RoleGrant.objects.filter(revokes_id=target.pk).exists():
                problems.append(issue("NOT_REVOCABLE", "That grant is not open.", field=label))
                continue
            revoke_grant(run, target, item.effective_from or run.now)
            revoked.append(target)
            continue
        grant, found = _add_item(run, human, item, label)
        problems += found
        if grant is not None:
            added.append(grant)
    if problems:
        raise access_invalid("Some grants cannot be applied.", problems)
    record_version(
        run,
        kind="user",
        target_key=str(user.pk),
        revision=revision + 1,
        payload={
            **_user_payload(user, human),
            "grants_added": [str(g.pk) for g in added],
            "grants_revoked": [str(g.pk) for g in revoked],
        },
        reason_code=reason_code,
    )
    bump_security_epoch(human.pk, run.tenant_id)
    run.audit_subject_key = f"user:{user.pk}"
    after: dict[str, Any] = {"human_id": human.pk, "reason_code": reason_code}
    for grant in added:
        after[f"grant.added.{grant.pk}"] = _describe(grant)
    for grant in revoked:
        after[f"grant.revoked.{grant.pk}"] = _describe(grant)
    run.audit_after = audit_values(after)
    return user


# -- roles and the access matrix ------------------------------------------------------


def role_data(role: Role) -> dict[str, Any]:
    return {
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "active": role.is_active,
    }


def role_dto(access: AccessContext, role: Role) -> dict[str, Any]:
    return resource_dto(
        id=role.pk,
        data=role_data(role),
        revision=current_revision("role", role.code),
        state="active" if role.is_active else "inactive",
        context={},
        # Role writes need tenant-wide access.manage (``require_tenant_manage``),
        # so a narrower administrator is not offered them.
        allowed_actions=["update"] if _has_tenant_manage(access) else [],
    )


def get_role(pk: int) -> Role:
    role = Role.objects.filter(pk=pk).first()
    if role is None:
        raise not_found("role")
    return role


def list_roles(access: AccessContext, params: dict[str, str]) -> list[Role]:
    access.require("access.manage")
    from accounts.role_assignments import INITIAL_ROLE_CODES

    query = (params.get("q") or "").strip()
    if len(query) > 100:
        raise invalid("q must be at most 100 characters.", "q")
    rows = Role.objects.filter(tenant_id=access.tenant_id).filter(
        Q(code__in=INITIAL_ROLE_CODES) | Q(is_system=False)
    )
    if query:
        rows = rows.filter(Q(code__icontains=query) | Q(name__icontains=query))
    return list(rows.order_by("code", "id"))


def parse_role(body: dict[str, Any]) -> dict[str, Any]:
    code = text_field(body, "code", 40)
    if code is not None and not ROLE_CODE.match(code):
        raise invalid("code may hold letters, digits, hyphens and underscores only.", "code")
    description = body.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 240):
        raise invalid("description must be text of at most 240 characters.", "description")
    parsed: dict[str, Any] = {
        "code": code,
        "name": text_field(body, "name", 80),
        "description": description,
        "active": bool_field(body, "active"),
    }
    return {key: parsed[key] for key in sorted(body)}


def _holders(tenant_id: uuid.UUID, role_id: int) -> set[uuid.UUID]:
    return set(
        RoleAssignment.objects.filter(tenant_id=tenant_id, role_id=role_id)
        .values_list("human_id", flat=True)
        .distinct()
    )


def create_role(run: CommandRun, *, fields: dict[str, Any]) -> Role:
    code = str(fields["code"])
    run.advisory_lock(LockRank.SECURITY, [f"role:{code.lower()}"])
    if Role.objects.filter(tenant_id=run.tenant_id, code__iexact=code).exists():
        raise access_invalid(
            "A role with that code already exists.",
            [issue("ROLE_CODE_TAKEN", "code is already used", field="code")],
        )
    role = Role.objects.create(
        tenant_id=run.tenant_id,
        code=code,
        name=str(fields["name"]),
        description=str(fields.get("description") or ""),
        is_active=True if fields.get("active") is None else bool(fields["active"]),
        section_access={},
        is_system=False,
    )
    record_version(run, kind="role", target_key=role.code, revision=1, payload=role_data(role))
    run.audit_subject_key = f"role:{role.code}"
    run.audit_after = audit_values(role_data(role))
    return role


def update_role(
    run: CommandRun, *, role_pk: int, expected_revision: int | None, fields: dict[str, Any]
) -> Role:
    role = get_role(role_pk)
    run.advisory_lock(LockRank.SECURITY, [f"role:{role.code.lower()}"])
    role.refresh_from_db()
    revision = current_revision("role", role.code)
    check_revision(expected_revision, revision)
    from accounts.role_assignments import INITIAL_ROLE_CODES

    if fields.get("active") is False and role.code in INITIAL_ROLE_CODES:
        raise Refusal("INITIAL_ROLE_REQUIRED", "The six initial role identities must remain active.")
    if fields.get("code") is not None and fields["code"] != role.code:
        raise access_invalid(
            "A role's code never changes; grants and templates refer to it.",
            [issue("ROLE_CODE_FIXED", "code cannot change", field="code")],
        )
    run.audit_before = audit_values(role_data(role))
    was_active = role.is_active
    if fields.get("name") is not None:
        role.name = str(fields["name"])
    if "description" in fields:
        role.description = str(fields.get("description") or "")
    if fields.get("active") is not None:
        role.is_active = bool(fields["active"])
    role.save()
    previous = latest_payload("role", role.code)
    payload = role_data(role)
    if isinstance(previous.get("access"), dict):
        payload["access"] = previous["access"]
    record_version(run, kind="role", target_key=role.code, revision=revision + 1, payload=payload)
    if was_active != role.is_active:
        for human_id in _holders(run.tenant_id, role.pk):
            bump_security_epoch(human_id, run.tenant_id)
    run.audit_subject_key = f"role:{role.code}"
    run.audit_after = audit_values(role_data(role))
    return role


def set_role_access(
    run: CommandRun, *, code: str, expected_revision: int | None, items: list[GrantItem]
) -> Role:
    role = Role.objects.filter(code=code).first()
    if role is None:
        raise not_found("role")
    run.advisory_lock(LockRank.SECURITY, [f"role:{code.lower()}"])
    revision = current_revision("role", role.code)
    check_revision(expected_revision, revision)
    template = ROLE_TEMPLATES.get(role.code)
    if template is None:
        raise access_invalid(
            "This role has no registered maximum, so it cannot carry goods actions.",
            [issue("ROLE_WITHOUT_TEMPLATE", "no registered maximum", field="code")],
        )
    problems: list[dict[str, Any]] = []
    actions: set[str] = set()
    fields: set[str] = set()
    kinds: set[str] = set()
    for index, item in enumerate(items):
        label = f"grants[{index}]"
        if item.human_id is not None or (item.role_id is not None and item.role_id != role.pk):
            problems.append(
                issue("NOT_THIS_ROLE", "Role access names no person or other role.", field=label)
            )
            continue
        assert item.scope is not None
        if item.scope.scope_kind not in template.scope_kinds:
            problems.append(
                issue("SCOPE_NOT_ALLOWED", "Scope kind beyond the role maximum.", field=label)
            )
        beyond = [a for a in item.actions if a not in template.actions]
        if beyond:
            problems.append(
                issue("ACTIONS_NOT_ALLOWED", f"Beyond maximum: {', '.join(beyond)}.", field=label)
            )
        wide = [f for f in item.fields if f not in template.fields]
        if wide:
            problems.append(
                issue("FIELDS_NOT_ALLOWED", f"Beyond maximum: {', '.join(wide)}.", field=label)
            )
        actions |= set(item.actions)
        fields |= set(item.fields)
        kinds.add(item.scope.scope_kind)
    if problems:
        raise access_invalid("This role access cannot be set.", problems)
    previous = latest_payload("role", role.code).get("access")
    block = {"actions": sorted(actions), "fields": sorted(fields), "scope_kinds": sorted(kinds)}
    record_version(
        run,
        kind="role",
        target_key=role.code,
        revision=revision + 1,
        payload={**role_data(role), "access": block},
    )
    for human_id in _holders(run.tenant_id, role.pk):
        bump_security_epoch(human_id, run.tenant_id)
    run.audit_subject_key = f"role:{role.code}"
    if isinstance(previous, dict):
        run.audit_before = audit_values(
            {
                "actions": ",".join(previous.get("actions") or []),
                "fields": ",".join(previous.get("fields") or []),
            }
        )
    run.audit_after = audit_values(
        {"actions": ",".join(block["actions"]), "fields": ",".join(block["fields"])}
    )
    return role


def _restrictions() -> list[dict[str, str]]:
    restrictions = [
        {"action": action, "reason": "Needs a fresh password confirmation on the same session."}
        for action in sorted(STEP_UP_ACTIONS)
    ]
    restrictions += [
        {"action": action, "reason": "The approver must be a different person from the maker."}
        for action in DISTINCT_IDENTITY_ACTIONS
    ]
    return restrictions


def _grant_in_actor_scope(access: AccessContext, grant: RoleGrant) -> bool:
    sites = access.site_ids("access.manage")
    if sites is None:
        return True
    if grant.scope_kind == "site":
        return grant.site_id in sites
    if grant.scope_kind == "sbu":
        return grant.sbu is not None and grant.sbu.site_id in sites
    if grant.scope_kind == "entity":
        return any(
            g.scope_kind == "entity"
            and g.entity_id == grant.entity_id
            and "access.manage" in g.actions
            for g in access.grants
        )
    return False


def _grant_covers_site(grant: RoleGrant, site: Store) -> bool:
    if grant.scope_kind in ("tenant", "brand"):
        return True
    if grant.scope_kind == "entity":
        return Store.objects.filter(pk=site.pk, gstin__legal_entity_id=grant.entity_id).exists()
    if grant.scope_kind == "site":
        return grant.site_id == site.pk
    return grant.sbu is not None and grant.sbu.site_id == site.pk


def access_matrix(access: AccessContext, params: dict[str, str]) -> dict[str, Any]:
    access.require_action("access.manage")
    site: Store | None = None
    if params.get("site_id"):
        site = resolve_site(parse_int_id(params["site_id"], "site_id"))
        if not access.can("access.manage", site_id=site.pk):
            raise not_found("site")
    role_filter: Role | None = None
    if params.get("role_id"):
        role_filter = get_role(parse_int_id(params["role_id"], "role_id"))
    roles = list(Role.objects.order_by("code", "id"))
    grants = []
    for grant, period in live_grants(access.tenant_id):
        if role_filter is not None and grant.role_id != role_filter.pk:
            continue
        if site is not None and not _grant_covers_site(grant, site):
            continue
        if _grant_in_actor_scope(access, grant):
            grants.append(grant_dto(grant, period))
    maxima = []
    for role in roles:
        maximum = role_maximum(role)
        if maximum is not None:
            maxima.append(
                {
                    "role_code": role.code,
                    "actions": sorted(maximum[0]),
                    "fields": sorted(maximum[1]),
                    "scope_kinds": sorted(maximum[2]),
                }
            )
    return {
        "roles": [{"id": str(role.pk), **role_data(role)} for role in roles],
        "grants": grants,
        "actions": [{"code": code, "label": label} for code, label in ACTIONS.items()],
        "restrictions": _restrictions(),
        "role_maxima": maxima,
    }


def admin_meta(access: AccessContext) -> dict[str, Any]:
    access.require_action("access.manage")
    sites = access.site_ids("access.manage")
    stores = Store.objects.all() if sites is None else Store.objects.filter(pk__in=sorted(sites))
    return {
        "sites": [
            {
                "id": str(store.pk),
                "record_contract": "legacy",
                "kind": "site",
                "number": store.code,
                "code": store.code,
                "name": store.name,
                "state": "active" if store.is_active else "inactive",
                "site_id": str(store.pk),
                "created_at": iso(store.created_at),
                "updated_at": iso(store.updated_at),
            }
            for store in stores.order_by("code")
        ],
    }


# -- policies (lists only; the per-item routes live with configuration) --------------

APPROVAL_PAYLOAD_KEYS = (
    "action",
    "roles",
    "purpose",
    "site_ids",
    "brand_ids",
    "require_distinct",
    "qty_max",
    "value_max",
    "step_up",
    "unknown_value",
)


def _approval_payload(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    return {key: source.get(key) for key in APPROVAL_PAYLOAD_KEYS}


def approval_policies(
    access: AccessContext, params: dict[str, str], *, sort_key: str
) -> list[dict[str, Any]]:
    """Effective approved ``ConfigPayload.approval`` versions plus open draft references."""
    access.require_action("access.manage")
    site_filter: str | None = None
    if params.get("site_id"):
        site_id = parse_int_id(params["site_id"], "site_id")
        resolve_site(site_id)
        if not access.can("access.manage", site_id=site_id):
            raise not_found("site")
        site_filter = str(site_id)
    from core.commands import database_now
    from masters.goods_config import candidate

    now = database_now()
    latest: dict[str, ConfigVersion] = {}
    for version in ConfigVersion.objects.filter(
        tenant_id=access.tenant_id, kind="approval"
    ).order_by("scope_key", "-version"):
        latest.setdefault(version.scope_key, version)
    items: list[tuple[str, dict[str, Any]]] = []
    for version in latest.values():
        data = _approval_payload(version.payload)
        dto = resource_dto(
            id=version.pk,
            data=data,
            revision=version.version,
            version=version.version,
            state=candidate(version).state(now),
            context={},
        )
        items.append((str(data.get(sort_key) or data.get("action") or ""), dto))
    drafts = ConfigDraft.objects.filter(tenant_id=access.tenant_id, kind="approval").exclude(
        state=ConfigDraft.State.APPROVED
    )
    for draft in drafts:
        data = _approval_payload(draft.payload)
        dto = resource_dto(
            id=draft.pk, data=data, revision=draft.revision, state=draft.state, context={}
        )
        items.append((str(data.get(sort_key) or data.get("action") or ""), dto))
    visible = []
    for _key, dto in sorted(items, key=lambda pair: (pair[0], pair[1]["id"])):
        site_ids = [str(s) for s in dto["data"].get("site_ids") or []]
        if site_filter is not None and site_ids and site_filter not in site_ids:
            continue
        visible.append(dto)
    return visible


# -- privileged changes -----------------------------------------------------------------


def privileged_filter() -> Q:
    return Q(action__in=sorted(PRIVILEGED_ACTIONS))


def is_privileged(action: str) -> bool:
    return action in PRIVILEGED_ACTIONS


#: E084's query: the shared list keys plus ``id``, one change by its audit event id.
PRIVILEGED_QUERY_KEYS = frozenset({"q", "cursor", "limit", "site_id", "id"})


def _privileged_cursor(last: AuditEvent) -> str:
    raw = json.dumps(
        {"r": last.recorded_at.isoformat(), "i": str(last.pk)}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _privileged_after(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    """A ``(recorded_at, id)`` keyset position, so a new change never shifts a page (03D)."""
    if not cursor:
        return None
    if len(cursor) > 200:
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
        recorded_at = parse_datetime(str(value["r"]))
        event_id = uuid.UUID(str(value["i"]))
    except (ValueError, KeyError, TypeError):
        raise Refusal("INVALID_REQUEST", "cursor is not valid.") from None
    if recorded_at is None or not timezone.is_aware(recorded_at):
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    return recorded_at, event_id


def privileged_page(
    access: AccessContext, params: dict[str, str]
) -> tuple[list[AuditEvent], str | None]:
    access.require_action("access.review")
    rows = AuditEvent.objects.filter(tenant_id=access.tenant_id, outcome="succeeded").filter(
        privileged_filter()
    )
    sites = access.site_ids("access.review")
    if sites is not None:
        rows = rows.filter(site_id__in=sorted(sites))
    if params.get("id"):
        # The follow-up's link: one change, still inside the reader's own scope.
        rows = rows.filter(pk=parse_uuid(params["id"], "id"))
    if params.get("site_id"):
        site_id = parse_int_id(params["site_id"], "site_id")
        resolve_site(site_id)
        if not access.can("access.review", site_id=site_id):
            raise not_found("site")
        rows = rows.filter(site_id=site_id)
    query = (params.get("q") or "").strip()
    if len(query) > 100:
        raise invalid("q must be at most 100 characters.", "q")
    if query:
        rows = rows.filter(Q(action__icontains=query) | Q(subject_key__icontains=query))
    after = _privileged_after(params.get("cursor"))
    if after is not None:
        rows = rows.filter(Q(recorded_at__lt=after[0]) | Q(recorded_at=after[0], pk__lt=after[1]))
    limit = page_limit(params)
    window = list(rows.order_by("-recorded_at", "-pk")[: limit + 1])
    cursor = _privileged_cursor(window[limit - 1]) if len(window) > limit else None
    return window[:limit], cursor


def privileged_dtos(access: AccessContext, events: list[AuditEvent]) -> list[dict[str, Any]]:
    reviews: dict[uuid.UUID, list[PrivilegedReview]] = {}
    for review in PrivilegedReview.objects.filter(
        audit_event_id__in=[event.pk for event in events]
    ).order_by("reviewed_at", "id"):
        reviews.setdefault(review.audit_event_id, []).append(review)
    people = {event.actor_id for event in events if event.actor_id is not None}
    people |= {r.reviewer_id for rows in reviews.values() for r in rows}
    names = dict(HumanIdentity.objects.filter(pk__in=people).values_list("pk", "display_name"))
    out = []
    for event in events:
        done = reviews.get(event.pk, [])
        reviewers = {review.reviewer_id for review in done}
        can_review = (
            access.can("access.review", site_id=event.site_id)
            and str(access.human_id) in (event.authority.get("independent_reviewers", []) if isinstance(event.authority, dict) else [])
            and event.actor_id != access.human_id
            and access.human_id not in reviewers
        )
        data = {
            "action": event.action,
            "subject_key": event.subject_key,
            "actor_id": ref(event.actor_id),
            "actor_name": names.get(event.actor_id) if event.actor_id else None,
            "service_code": event.service_code,
            "outcome": event.outcome,
            "reason_code": event.reason_code,
            "site_id": ref(event.site_id),
            "before": safe_values(event.before),
            "after": safe_values(event.after),
            "event_at": iso(event.event_at),
            "recorded_at": iso(event.recorded_at),
            "reviews": [
                {
                    "reviewer_id": str(review.reviewer_id),
                    "reviewer_name": names.get(review.reviewer_id),
                    "note": review.note,
                    "reviewed_at": iso(review.reviewed_at),
                }
                for review in done
            ],
        }
        out.append(
            resource_dto(
                id=event.pk,
                data=data,
                revision=1 + len(done),
                state="reviewed" if done else "unreviewed",
                context={"site_id": event.site_id},
                allowed_actions=["review"] if can_review else [],
            )
        )
    return out


def get_privileged_event(access: AccessContext, event_id: uuid.UUID) -> AuditEvent:
    event = AuditEvent.objects.filter(tenant_id=access.tenant_id, pk=event_id).first()
    if event is None or not access.can("access.review", site_id=event.site_id):
        raise not_found("change")
    return event


#: GSA-T03 "Access follow-up": the owned item a privileged change raises in the
#: shared exceptions centre until a different person reviews it.
FOLLOW_UP_KIND = "privileged_change_review"
#: The review route (E237), the one command that closes the follow-up.
FOLLOW_UP_RESOLUTION = "auth/admin/privileged-changes/{id}/review"


def open_privileged_follow_up(run: CommandRun) -> None:
    """Raise the owned review item for a succeeded privileged change (ticket 03D).

    Registered with the command kernel for every command, so a privileged kind
    registered in ``accounts.actions`` gets its follow-up without its handler
    having to remember one. The item names the change's own audit event, which
    is what the review reads; ordinary access edits raise nothing.
    """
    if not is_privileged(run.spec.action):
        return
    from alerts.goods_services import open_exception

    open_exception(
        run,
        kind=FOLLOW_UP_KIND,
        site_id=run.audit_site_id,
        subject_key=f"audit:{run.audit_event_id}",
        reason_code="PRIVILEGED_CHANGE_UNREVIEWED",
        source_event_key=run.audit_event_id,
        allowed_resolution_actions=[FOLLOW_UP_RESOLUTION],
    )


def review_change(run: CommandRun, *, event_id: uuid.UUID, note: str) -> PrivilegedReview:
    reviewer_id = run.principal.human_id
    run.advisory_lock(LockRank.SECURITY, [f"privileged-review:{event_id}"])
    event = AuditEvent.objects.filter(tenant_id=run.tenant_id, pk=event_id).first()
    if event is None:
        raise not_found("change")
    if reviewer_id is None or event.outcome != "succeeded" or not is_privileged(event.action):
        raise Refusal("REVIEW_INVALID", "Only a recorded privileged change can be reviewed.")
    if event.actor_id == reviewer_id:
        raise Refusal(
            "REVIEW_INVALID", "A different person from the one who made a change reviews it."
        )
    reviewers = event.authority.get("independent_reviewers") if isinstance(event.authority, dict) else None
    if not isinstance(reviewers, list) or str(reviewer_id) not in reviewers:
        raise Refusal("REVIEW_AUTHORITY_UNPROVEN", "This review needs authority established independently before the change.", status=403)
    if PrivilegedReview.objects.filter(audit_event_id=event.pk, reviewer_id=reviewer_id).exists():
        raise Refusal("REVIEW_INVALID", "You have already reviewed this change.")
    review: PrivilegedReview = run.record(
        PrivilegedReview(
            audit_event_id=event.pk, reviewer_id=reviewer_id, note=note, reviewed_at=run.now
        )
    )
    from alerts.goods_services import resolve_exceptions

    # The first distinct review closes the owned follow-up; a later one finds it closed.
    resolve_exceptions(
        run,
        kind=FOLLOW_UP_KIND,
        subject_key=f"audit:{event.pk}",
        reason_code="PRIVILEGED_CHANGE_REVIEWED",
        source_event_key=event.pk,
    )
    run.audit_subject_key = f"audit:{event.pk}"
    run.audit_site_id = event.site_id
    run.audit_after = audit_values({"audit_event_id": event.pk, "note": note})
    return review
