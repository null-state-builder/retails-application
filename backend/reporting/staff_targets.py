"""Each salesperson's monthly target (store operations ticket 46; overall PRD R-HR-004).

"Maintain authorised per-staff periodic sales targets ... keep target setting
responsibility configurable": a target is set by whoever sets store targets -
``money: manage`` in the access matrix (#171), which Admin can move - for a
store in their scope, where the staff report is switched on, for someone on
that store's staff list.

Every change is one command (``TARGET_ACTION``, subject ``staff_target:<id>``)
whose audit record holds the number before and after. The editor sends the
``revision`` it saw (0 for none yet); a change made over a newer one is refused
(``REVISION_SUPERSEDED``), and a change resent with its ``command_id`` after a
dropped connection answers what it saved, once.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.goods_models import Staff
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal, issue
from masters.models import StaffTarget, Store
from masters.store_features import is_feature_on, require_feature
from reporting.base import principal_for, viewer_stores
from reporting.staff_report import FEATURE_KEY
from sell.services.salespeople import ever_placed_at, staff_list

TARGET_ACTION = "reports.staff_target.set"
#: The largest target accepted: Rs 1,000 crore, far past any person's month.
MAX_TARGET_PAISE = 10**12

_MONTH = re.compile(r"^(\d{4})-(\d{2})(?:-01)?$")


def parse_month(raw: Any) -> date:
    """``YYYY-MM`` (or that month's first day) as the month's first day."""
    match = _MONTH.match(str(raw or "").strip())
    if match is None:
        raise Refusal(
            "INVALID_REQUEST",
            "month must be a month, YYYY-MM.",
            issues=[issue("INVALID", "month must be YYYY-MM", field="month")],
            status=400,
        )
    try:
        return date(int(match[1]), int(match[2]), 1)
    except ValueError:
        raise Refusal("INVALID_REQUEST", "month must be a real month.", status=400) from None


def _store(user: Any, code: Any) -> Store:
    wanted = str(code or "").strip().upper()
    if not wanted:
        raise Refusal("INVALID_REQUEST", "Choose a store.", status=400)
    store = next((s for s in viewer_stores(user) if s.code.upper() == wanted), None)
    if store is None:
        raise Refusal("SCOPE_DENIED", f"Store {wanted} is not one of your stores.", status=403)
    return store


def _staff(store: Store, raw: Any) -> Staff:
    try:
        staff_id = uuid.UUID(str(raw))
    except (TypeError, ValueError):
        raise Refusal(
            "INVALID_REQUEST", "staff_id must be a staff record's id.", status=400
        ) from None
    if not ever_placed_at(store, {staff_id}):
        raise Refusal(
            "NOT_ON_STAFF_LIST",
            f"That person is not on {store.name}'s staff list, so they have no target there.",
            status=422,
        )
    return Staff.objects.select_related("human").get(pk=staff_id)


def _whole(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Refusal(
            "INVALID_REQUEST",
            f"{field} must be a whole number, nought or more.",
            issues=[issue("INVALID", f"{field} must be a whole number >= 0", field=field)],
            status=400,
        )
    if maximum is not None and value > maximum:
        raise Refusal("INVALID_REQUEST", f"{field} is too large.", status=400)
    return value


def people(user: Any, params: Any) -> dict[str, Any]:
    """The store's salespeople and their target for one month (the editor's rows).

    Reading is never refused for the switch (ticket 01 refuses only new work);
    ``switched_on`` says whether a change would be taken.
    """
    store = _store(user, params.get("store"))
    month = parse_month(params.get("month") or timezone.localdate().strftime("%Y-%m"))
    set_for = {
        row.staff_id: row
        for row in StaffTarget.objects.filter(store=store, month=month).select_related(
            "staff__human"
        )
    }
    rows = [
        {
            "staff_id": str(row.staff_id),
            "name": row.display_name,
            "code": row.staff_code,
            "active": row.active,
            "target_paise": int(set_for[row.staff_id].target_paise)
            if row.staff_id in set_for
            else None,
            "revision": set_for[row.staff_id].revision if row.staff_id in set_for else 0,
        }
        for row in staff_list(store)
        if row.salesperson or row.staff_id in set_for
    ]
    return {
        "store": store.code,
        "month": month.isoformat(),
        "switched_on": is_feature_on(store, FEATURE_KEY),
        "people": rows,
    }


@dataclass(frozen=True)
class SetTarget:
    command_id: uuid.UUID
    store: Store
    staff: Staff
    month: date
    target_paise: int
    revision: int


def parse(user: Any, data: Any) -> SetTarget:
    if not isinstance(data, dict):
        raise Refusal("INVALID_REQUEST", "The request body must be a JSON object.", status=400)
    allowed = {"command_id", "store", "staff_id", "month", "target_paise", "revision"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown field(s): {', '.join(unknown)}.", status=400)
    try:
        command_id = uuid.UUID(str(data.get("command_id")))
    except (TypeError, ValueError):
        raise Refusal("INVALID_REQUEST", "A command_id UUID is required.", status=400) from None
    month = parse_month(data.get("month"))
    target = _whole(data.get("target_paise"), "target_paise", maximum=MAX_TARGET_PAISE)
    revision = _whole(data.get("revision"), "revision")
    store = _store(user, data.get("store"))
    return SetTarget(
        command_id=command_id,
        store=store,
        staff=_staff(store, data.get("staff_id")),
        month=month,
        target_paise=target,
        revision=revision,
    )


def _audit(asked: SetTarget, target_paise: int | None, revision: int) -> dict[str, Any]:
    return {
        "store": asked.store.code,
        "staff": asked.staff.human.staff_code,
        "name": asked.staff.human.display_name,
        "month": asked.month.isoformat(),
        "target_paise": target_paise,
        "revision": revision,
    }


def set_target(user: Any, asked: SetTarget) -> dict[str, Any]:
    """Set one person's target for one month at one store; the audited write."""
    business_input = {
        "store": asked.store.pk,
        "staff_id": str(asked.staff.pk),
        "month": asked.month.isoformat(),
        "target_paise": asked.target_paise,
        "revision": asked.revision,
    }

    def handler(run: CommandRun) -> CommandResult:
        # Inside the command, so a resend of a change already made answers what it
        # made even if the switch has gone off since.
        require_feature(asked.store, FEATURE_KEY)
        row = (
            StaffTarget.objects.select_for_update()
            .filter(store=asked.store, staff=asked.staff, month=asked.month)
            .first()
        )
        current = row.revision if row else 0
        if asked.revision != current:
            raise Refusal(
                "REVISION_SUPERSEDED",
                "Someone changed this target after you loaded it. Reload and review the "
                "current number.",
            )
        run.audit_before = _audit(asked, int(row.target_paise) if row else None, current)
        if row is None:
            try:
                with transaction.atomic():
                    row = StaffTarget.objects.create(
                        store=asked.store,
                        staff=asked.staff,
                        month=asked.month,
                        target_paise=asked.target_paise,
                        revision=1,
                        set_by_id=getattr(user, "pk", None),
                    )
            except IntegrityError:
                raise Refusal(
                    "REVISION_SUPERSEDED",
                    "Someone set this target at the same moment. Reload and review the "
                    "current number.",
                ) from None
        else:
            row.target_paise = asked.target_paise
            row.revision = current + 1
            row.set_by_id = getattr(user, "pk", None)
            row.save(update_fields=["target_paise", "revision", "set_by", "updated_at"])
        run.audit_subject_key = f"staff_target:{row.pk}"
        run.audit_after = _audit(asked, int(row.target_paise), row.revision)
        return CommandResult(
            resource_type="staff_target", resource_id=str(row.pk), revision=row.revision
        )

    result = execute_command(
        principal_for(user, asked.store.tenant_id),
        CommandSpec(
            TARGET_ACTION,
            asked.command_id,
            business_input,
            subject_key="staff_target:new",
            site_id=asked.store.pk,
        ),
        handler,
    )
    # What this command saved, even when a resend finds a newer change since.
    return {
        "id": int(str(result.resource_id)),
        "store": asked.store.code,
        "staff_id": str(asked.staff.pk),
        "name": asked.staff.human.display_name,
        "month": asked.month.isoformat(),
        "target_paise": asked.target_paise,
        "revision": result.revision,
    }
