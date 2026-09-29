"""Store task checklists (store operations PRD ST-OPS-4; ticket 49).

**Templates.** Admin sets each checklist: a name, its items, how often it is due
(every day, one day of the week, or one day of the month - the last day of a
shorter month) and, if it wants one, the time of day it is due by (India time;
none is the end of the day). A template is the chain's: every store where the
``store-checklists`` switch is on gets it. An item may open one of a few existing
screens, so "the monthly count" links to the count schedule (ticket 35) rather
than repeating it.

**Today.** A store's list for the day is every live template due today, with
each item ticked or not. Staff tick an item, with a photo if they want one. A
tick is written once. An item may still be ticked late, for a due day up to
``KDPS_CHECKLIST_MISSED_DAYS`` back; a late tick is marked late.

**Missed.** A list whose time has passed with items not ticked is missed, for
each of those items. Only a time that passed while the switch was on at that
store, and after the template was set or last changed, is ever judged: a store
cannot miss a list it could not see. Today shows what is still to do; the
worker's check records each missed list once (``ChecklistMiss``, for the
exceptions report) and keeps one checklist-missed alert open per list while any
of its missed items is still not ticked and can still be ticked: up to
``KDPS_CHECKLIST_MISSED_DAYS`` back. Once a missed list is recorded, Today lists
its items from that record, so a later change to the template (or to the
switch) never hides an item the alert still names, and it can still be ticked.
Ticking the last one closes the alert in the same command; the worker keeps the
rest matching.

Where the switch is off the store gets no list, nothing is judged, and no tick
is taken; what was already recorded stays.

The site-readiness checklist is separate and not touched here.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from accounts.permissions import user_can
from accounts.role_lists import CHECKLIST_TEMPLATE_EDITOR_ROLES
from accounts.sections import CAP_MANAGE, CAP_OPERATE, CAP_VIEW
from alerts.checks import AlertHit, sync_kind
from alerts.models import Alert, AlertKind, AlertStatus
from core.canonical import sha256_hex
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.offbox import OffboxError, get_store
from core.refusals import Refusal, issue
from core.tenancy import current_tenant_id
from masters.models import Store
from masters.store_feature_models import StoreFeatureSwitch
from masters.store_feature_registry import STORE_CHECKLISTS
from masters.store_features import feature, switch_states
from storefront.checklist_models import (
    ChecklistEvery,
    ChecklistMiss,
    ChecklistTemplate,
    ChecklistTick,
)

FEATURE_KEY = STORE_CHECKLISTS

SET_ACTION = "store.checklist_template.set"
CHANGE_ACTION = "store.checklist_template.change"
STOP_ACTION = "store.checklist_template.stop"
TICK_ACTION = "store.checklist.tick"
CHECK_ACTION = "store.checklist.check"
CHECK_SERVICE = "checklist-check"

#: The existing screens an item may open, and what the template page calls them.
OPENS: dict[str, str] = {
    "": "Nothing",
    "count_schedule": "Count schedule",
    "cash_count": "Cash count",
}

MAX_NAME = 80
MAX_ITEM_TEXT = 200
MAX_ITEMS = 30

#: The largest photo taken with a tick. A phone photo is a few megabytes.
MAX_PHOTO_BYTES = 10_000_000
#: A tick's photo is a picture, told by its first bytes, never its name.
_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
)

_TICKED = "ALREADY_TICKED"


def missed_days() -> int:
    """How many days back a missed item stays on Today and can be ticked late."""
    return int(getattr(settings, "KDPS_CHECKLIST_MISSED_DAYS", 7))


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


def may_read_templates(user: Any) -> bool:
    return bool(getattr(user, "is_superuser", False)) or user_can(user, "setup", CAP_VIEW)


def may_edit_templates(user: Any) -> bool:
    """Admin: ``setup: manage`` and a declared editor role (or break-glass)."""
    if getattr(user, "is_superuser", False):
        return True
    code = getattr(getattr(user, "role", None), "code", "")
    return user_can(user, "setup", CAP_MANAGE) and code in CHECKLIST_TEMPLATE_EDITOR_ROLES


def may_tick(user: Any) -> bool:
    """The store's own staff at the counter: ``sell: operate`` or higher."""
    return bool(getattr(user, "is_superuser", False)) or user_can(user, "sell", CAP_OPERATE)


# ---------------------------------------------------------------------------
# The rule: when a list is due, and by when
# ---------------------------------------------------------------------------


def is_due_on(template: ChecklistTemplate, day: date) -> bool:
    if template.every == ChecklistEvery.DAY:
        return True
    if template.every == ChecklistEvery.WEEK:
        return day.weekday() == template.weekday
    last = calendar.monthrange(day.year, day.month)[1]
    return day.day == min(int(template.day_of_month or 1), last)


def due_moment(template: ChecklistTemplate, day: date) -> datetime:
    """When the list for ``day`` is due: its time that day, or the day's end."""
    if template.due_by is not None:
        return timezone.make_aware(datetime.combine(day, template.due_by))
    return timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min))


def due_days(template: ChecklistTemplate, first: date, last: date) -> list[date]:
    """The days from ``first`` to ``last`` (both included) the list is due, oldest first."""
    days = []
    day = first
    while day <= last:
        if is_due_on(template, day):
            days.append(day)
        day += timedelta(days=1)
    return days


def window(now: datetime) -> tuple[date, date]:
    """The due days a tick may be for, and a miss still shows: the last few, and today."""
    today = timezone.localdate(now)
    return today - timedelta(days=missed_days()), today


# ---------------------------------------------------------------------------
# The switch, and since when it has been on
# ---------------------------------------------------------------------------


def switched_on(stores: list[Store]) -> list[Store]:
    states = switch_states(stores, [feature(FEATURE_KEY)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def on_since(stores: Iterable[Store]) -> dict[int, datetime]:
    """When Admin last changed each store's switch (it is on now, so: turned it on).

    A store with no row has the registered default, which is off (B3), so it is
    never here unless test data turned it on without a row; then it counts as on
    since the start of time and every due time in the window is judged.
    """
    ids = [store.pk for store in stores]
    rows = StoreFeatureSwitch.objects.filter(site_id__in=ids, feature_key=FEATURE_KEY)
    return {site_id: at for site_id, at in rows.values_list("site_id", "updated_at")}


def judged(template: ChecklistTemplate, day: date, since: datetime | None) -> bool:
    """May the list for ``day`` be judged missed? Only if its time came after the
    template was set or changed, and after the switch went on at the store."""
    moment = due_moment(template, day)
    if moment <= template.judged_after:
        return False
    return since is None or moment > since


def live_templates() -> list[ChecklistTemplate]:
    return list(ChecklistTemplate.objects.filter(active=True).order_by("name", "id"))


# ---------------------------------------------------------------------------
# Today: the store's lists, and what is still to do from the last few days
# ---------------------------------------------------------------------------


def item_ids(template: ChecklistTemplate) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in template.items}


def _ticks(store: Store, first: date, last: date) -> dict[tuple[Any, str, date], ChecklistTick]:
    rows = ChecklistTick.objects.filter(
        store=store, due_on__gte=first, due_on__lte=last
    ).select_related("ticked_by")
    return {(row.template_id, row.item_id, row.due_on): row for row in rows}


def tick_json(tick: ChecklistTick) -> dict[str, Any]:
    by = tick.ticked_by
    return {
        "id": tick.pk,
        "by": (getattr(by, "full_name", "") or getattr(by, "username", "")) if by else "",
        "at": tick.ticked_at,
        "late": tick.late,
        "has_photo": tick.has_photo,
    }


def _list_json(
    template: ChecklistTemplate,
    day: date,
    ticks: dict[tuple[Any, str, date], ChecklistTick],
    now: datetime,
    since: datetime | None,
    *,
    open_only: bool,
) -> dict[str, Any]:
    passed = now >= due_moment(template, day) and judged(template, day, since)
    items = []
    for item in template.items:
        done = ticks.get((template.pk, str(item["id"]), day))
        if open_only and done is not None:
            continue
        items.append(
            {
                "id": str(item["id"]),
                "text": item["text"],
                "opens": item.get("opens", ""),
                "tick": tick_json(done) if done else None,
                "missed": passed and done is None,
            }
        )
    return {
        "template_id": template.pk,
        "name": template.name,
        "due_on": day,
        "due_by": template.due_by.strftime("%H:%M") if template.due_by else None,
        "passed": passed,
        "items": items,
    }


def _miss_json(miss: Missed) -> dict[str, Any]:
    """A recorded missed list, with the items still to tick, named as recorded."""
    opens = {key: item.get("opens", "") for key, item in item_ids(miss.template).items()}
    return {
        "template_id": miss.template.pk,
        "name": miss.template.name,
        "due_on": miss.due_on,
        "due_by": miss.template.due_by.strftime("%H:%M") if miss.template.due_by else None,
        "passed": True,
        "items": [
            {
                "id": item["id"],
                "text": item["text"],
                "opens": opens.get(item["id"], ""),
                "tick": None,
                "missed": True,
            }
            for item in miss.items
        ],
    }


@dataclass(frozen=True)
class Today:
    lists: list[dict[str, Any]]
    missed: list[dict[str, Any]]

    @property
    def missed_items(self) -> int:
        """Items whose time has passed and are still not ticked: today's and earlier."""
        today = sum(1 for row in self.lists for item in row["items"] if item["missed"])
        return today + sum(len(row["items"]) for row in self.missed)


def today_at(store: Store, now: datetime | None = None) -> Today:
    """The store's lists due today, and the lists from the last few days whose
    time passed with items still not ticked (only those items). Switch on only."""
    now = now or timezone.now()
    if not switched_on([store]):
        return Today([], [])
    first, today = window(now)
    since = on_since([store]).get(store.pk)
    ticks = _ticks(store, first, today)
    recorded = [miss for miss in open_misses(now, [store.pk]) if miss.due_on < today]
    seen = {(miss.template.pk, miss.due_on) for miss in recorded}
    lists: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = [_miss_json(miss) for miss in recorded]
    for template in live_templates():
        for day in due_days(template, first, today):
            if day == today:
                lists.append(_list_json(template, day, ticks, now, since, open_only=False))
                continue
            if (template.pk, day) in seen or not judged(template, day, since):
                continue
            row = _list_json(template, day, ticks, now, since, open_only=True)
            if row["items"]:
                missed.append(row)
    missed.sort(key=lambda row: (row["due_on"], row["name"]), reverse=True)
    return Today(lists, missed)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def template_json(template: ChecklistTemplate) -> dict[str, Any]:
    return {
        "id": template.pk,
        "name": template.name,
        "every": template.every,
        "weekday": template.weekday,
        "day_of_month": template.day_of_month,
        "due_by": template.due_by.strftime("%H:%M") if template.due_by else None,
        "items": [
            {"id": str(item["id"]), "text": item["text"], "opens": item.get("opens", "")}
            for item in template.items
        ],
        "active": template.active,
        "revision": template.revision,
    }


def audit_json(template: ChecklistTemplate) -> dict[str, Any]:
    body = template_json(template)
    body["id"] = str(template.pk)
    return body


def _invalid(message: str, field: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


@dataclass(frozen=True)
class TemplateInput:
    name: str
    every: str
    weekday: int | None
    day_of_month: int | None
    due_by: time | None
    items: list[dict[str, Any]]

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "every": self.every,
            "weekday": self.weekday,
            "day_of_month": self.day_of_month,
            "due_by": self.due_by.strftime("%H:%M") if self.due_by else None,
            "items": self.items,
        }


def clean_template(body: dict[str, Any], kept: ChecklistTemplate | None = None) -> TemplateInput:
    """What Admin sent, checked. An item sent with the id of one of ``kept``'s
    items keeps that id; any other item gets a new one, made from the request's
    ``command_id`` so a retry of the same request names its items the same."""
    seed = _client_uuid(body.get("command_id"), "command_id")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        raise _invalid(f"A checklist needs a name of up to {MAX_NAME} characters.", "name")
    every = body.get("every")
    if every not in ChecklistEvery.values:
        raise _invalid("every must be day, week or month.", "every")
    weekday = day_of_month = None
    if every == ChecklistEvery.WEEK:
        weekday = _int_in(body.get("weekday"), 0, 6, "weekday", "Choose the day of the week.")
    if every == ChecklistEvery.MONTH:
        day_of_month = _int_in(
            body.get("day_of_month"), 1, 31, "day_of_month", "Choose a day of the month, 1 to 31."
        )
    due_by = _due_by(body.get("due_by"))
    items = _clean_items(body.get("items"), set(item_ids(kept)) if kept else set(), seed)
    return TemplateInput(name, str(every), weekday, day_of_month, due_by, items)


def _clean_items(raw: Any, known: set[str], seed: uuid.UUID) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise _invalid("A checklist needs at least one item.", "items")
    if len(raw) > MAX_ITEMS:
        raise _invalid(f"A checklist can have up to {MAX_ITEMS} items.", "items")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise _invalid("Each item is its words and, if wanted, a screen it opens.", "items")
        text = str(entry.get("text") or "").strip()
        if not text or len(text) > MAX_ITEM_TEXT:
            raise _invalid(f"Each item needs words, up to {MAX_ITEM_TEXT} characters.", "items")
        opens = str(entry.get("opens") or "")
        if opens not in OPENS:
            raise _invalid("An item opens the count schedule, the cash count, or nothing.", "items")
        item_id = str(entry.get("id") or "")
        if item_id not in known or item_id in seen:
            item_id = str(uuid.uuid5(seed, f"item:{len(items)}"))
        seen.add(item_id)
        items.append({"id": item_id, "text": text, "opens": opens})
    return items


def _int_in(raw: Any, low: int, high: int, field: str, message: str) -> int:
    if isinstance(raw, bool):
        raise _invalid(message, field)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _invalid(message, field) from None
    if not low <= value <= high or str(raw).strip() != str(value):
        raise _invalid(message, field)
    return value


def _due_by(raw: Any) -> time | None:
    if raw in (None, ""):
        return None
    try:
        parsed = time.fromisoformat(str(raw))
    except ValueError:
        raise _invalid(
            "due_by is a time of day (HH:MM), or empty for the end of the day.", "due_by"
        ) from None
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise _invalid(
            "due_by is a time of day (HH:MM), or empty for the end of the day.", "due_by"
        )
    return parsed


def _principal(actor: Any, tenant_id: uuid.UUID | None = None) -> Principal:
    """The person, or - for a login that is not a person - the store's login."""
    tenant = tenant_id or getattr(actor, "tenant_id", None) or current_tenant_id()
    if tenant is None:  # pragma: no cover - every request binds the deployment's tenant
        raise Refusal("SCOPE_DENIED", "No tenant is bound to this request.", status=403)
    human_id = getattr(actor, "human_id", None)
    user_id = getattr(actor, "pk", None)
    if human_id is not None:
        return Principal(tenant_id=tenant, human_id=human_id, user_id=user_id)
    return Principal(tenant_id=tenant, service_code="store-login", user_id=user_id)


def _client_uuid(raw: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        raise _invalid(f"A {field} UUID is required.", field) from None


def _command_id(action: str, raw: Any) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{action}:{_client_uuid(raw, 'command_id')}")


def _duplicate_name(name: str) -> Refusal:
    return Refusal(
        "CHECKLIST_EXISTS",
        f"A checklist called {name} is already set. Change that one instead.",
        status=409,
    )


def _lock_templates(run: CommandRun) -> None:
    run.advisory_lock(LockRank.DOCUMENT, ["checklist-templates"])


def set_template(actor: Any, command_id: Any, data: TemplateInput) -> ChecklistTemplate:
    def handler(run: CommandRun) -> CommandResult:
        _lock_templates(run)
        if ChecklistTemplate.objects.filter(active=True, name__iexact=data.name).exists():
            raise _duplicate_name(data.name)
        run.audit_before = {"name": data.name, "active": False}
        try:
            row = ChecklistTemplate.objects.create(
                name=data.name,
                every=data.every,
                weekday=data.weekday,
                day_of_month=data.day_of_month,
                due_by=data.due_by,
                items=data.items,
                judged_after=run.now,
                created_by=actor,
                created_at=run.now,
                updated_at=run.now,
            )
        except IntegrityError:  # pragma: no cover - the lock above keeps this out
            raise _duplicate_name(data.name) from None
        run.audit_subject_key = f"checklist_template:{row.pk}"
        run.audit_after = audit_json(row)
        return CommandResult(
            resource_type="checklist_template",
            resource_id=str(row.pk),
            revision=row.revision,
            status_code=201,
        )

    result = execute_command(
        _principal(actor),
        CommandSpec(
            action=SET_ACTION,
            command_id=_command_id(SET_ACTION, command_id),
            business_input=data.as_json(),
            subject_key="checklist_template:new",
        ),
        handler,
    )
    return ChecklistTemplate.objects.get(pk=str(result.resource_id))


def _locked(run: CommandRun, pk: Any) -> ChecklistTemplate:
    rows = run.lock(LockRank.DOCUMENT, ChecklistTemplate.objects.filter(pk=pk))
    if not rows:
        raise Refusal("NOT_FOUND", "That checklist was not found.", status=404)
    row: ChecklistTemplate = rows[0]
    return row


def _check_revision(expected: Any, current: int) -> None:
    if expected is None or isinstance(expected, bool) or str(expected) != str(current):
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Someone changed this checklist after you loaded it. Reload and look again.",
            status=409,
        )


def change_template(
    actor: Any, template: ChecklistTemplate, command_id: Any, expected: Any, data: TemplateInput
) -> ChecklistTemplate:
    def handler(run: CommandRun) -> CommandResult:
        _lock_templates(run)
        row = _locked(run, template.pk)
        _check_revision(expected, row.revision)
        if not row.active:
            raise Refusal("CHECKLIST_STOPPED", "This checklist has been stopped.", status=409)
        if (
            ChecklistTemplate.objects.filter(active=True, name__iexact=data.name)
            .exclude(pk=row.pk)
            .exists()
        ):
            raise _duplicate_name(data.name)
        run.audit_before = audit_json(row)
        row.name = data.name
        row.every = data.every
        row.weekday = data.weekday
        row.day_of_month = data.day_of_month
        row.due_by = data.due_by
        row.items = data.items
        row.judged_after = run.now
        row.revision += 1
        row.updated_at = run.now
        row.save()
        run.audit_after = audit_json(row)
        return CommandResult(
            resource_type="checklist_template", resource_id=str(row.pk), revision=row.revision
        )

    execute_command(
        _principal(actor),
        CommandSpec(
            action=CHANGE_ACTION,
            command_id=_command_id(CHANGE_ACTION, command_id),
            business_input={
                "template_id": str(template.pk),
                "expected_revision": expected,
                **data.as_json(),
            },
            resource_ids=[str(template.pk)],
            subject_key=f"checklist_template:{template.pk}",
        ),
        handler,
    )
    return ChecklistTemplate.objects.get(pk=template.pk)


def stop_template(
    actor: Any, template: ChecklistTemplate, command_id: Any, expected: Any
) -> ChecklistTemplate:
    """Stop a checklist: no store gets it from now on, and its open alerts close.
    Its ticks and misses stay."""

    def handler(run: CommandRun) -> CommandResult:
        row = _locked(run, template.pk)
        _check_revision(expected, row.revision)
        if not row.active:
            raise Refusal("CHECKLIST_STOPPED", "This checklist is already stopped.", status=409)
        run.audit_before = audit_json(row)
        row.active = False
        row.revision += 1
        row.updated_at = run.now
        row.save(update_fields=["active", "revision", "updated_at"])
        open_alerts = Alert.objects.filter(
            kind=AlertKind.CHECKLIST_MISSED,
            status=AlertStatus.OPEN,
            dedupe_key__contains=f":{row.pk}:",
        )
        closed = sorted(open_alerts.values_list("dedupe_key", flat=True))
        open_alerts.update(status=AlertStatus.RESOLVED, resolved_at=run.now)
        run.audit_after = {**audit_json(row), "alerts_closed": closed}
        return CommandResult(
            resource_type="checklist_template", resource_id=str(row.pk), revision=row.revision
        )

    execute_command(
        _principal(actor),
        CommandSpec(
            action=STOP_ACTION,
            command_id=_command_id(STOP_ACTION, command_id),
            business_input={"template_id": str(template.pk), "expected_revision": expected},
            resource_ids=[str(template.pk)],
            subject_key=f"checklist_template:{template.pk}",
        ),
        handler,
    )
    return ChecklistTemplate.objects.get(pk=template.pk)


# ---------------------------------------------------------------------------
# Ticks, and their photos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Photo:
    """A tick's photo as it arrived: bytes, and what its first bytes say it is."""

    data: bytes
    media_type: str

    @property
    def sha256(self) -> str:
        return sha256_hex(self.data)


def read_photo(upload: Any) -> Photo | None:
    """The uploaded photo, checked; ``None`` when no file was sent."""
    if upload is None:
        return None
    size = getattr(upload, "size", None)
    if size is not None and size > MAX_PHOTO_BYTES:
        raise Refusal("FILE_TOO_LARGE", "A photo can be up to 10 MB.", status=413)
    data = upload.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise Refusal("FILE_TOO_LARGE", "A photo can be up to 10 MB.", status=413)
    if not data:
        raise Refusal("FILE_INVALID", "The photo is empty.", status=422)
    media_type = next((kind for kind, head in _SIGNATURES if data.startswith(head)), None)
    if media_type is None:
        raise Refusal("FILE_INVALID", "A photo must be a JPEG or PNG picture.", status=422)
    return Photo(data=data, media_type=media_type)


def _store_photo(store: Store, client_id: uuid.UUID, photo: Photo) -> str:
    """Write the photo to the write-once store before any row names it."""
    key = f"checklists/{store.pk}/{client_id}/{photo.sha256}"
    try:
        stored = get_store().put(key, photo.data)
    except OffboxError as exc:
        raise Refusal(
            "EVIDENCE_UNAVAILABLE", "The photo could not be stored; nothing was saved."
        ) from exc
    if stored.sha256 != photo.sha256 or stored.size != len(photo.data):
        raise Refusal("EVIDENCE_UNAVAILABLE", "The stored photo could not be confirmed.")
    return stored.key


def photo_bytes(tick: ChecklistTick) -> bytes:
    """The stored photo, checked against the hash the row kept."""
    try:
        data = get_store().get(tick.photo_key)
    except OffboxError as exc:
        raise Refusal("EVIDENCE_UNAVAILABLE", "The photo is not available now.") from exc
    if sha256_hex(data) != tick.photo_sha256:
        raise Refusal("EVIDENCE_UNAVAILABLE", "The stored photo does not match its record.")
    return data


def tick_facts(tick: ChecklistTick) -> dict[str, Any]:
    return {
        "ticked": True,
        "store": tick.store.code,
        "checklist": tick.template.name,
        "item": tick.item_text,
        "due_on": tick.due_on.isoformat(),
        "late": tick.late,
        "ticked_by_user_id": tick.ticked_by_id,
        "photo_sha256": tick.photo_sha256 or None,
    }


@dataclass(frozen=True)
class Ticked:
    tick: ChecklistTick
    created: bool


def tick(
    store: Store,
    actor: Any,
    *,
    client_id: Any,
    template_id: Any,
    item_id: Any,
    due_on: date,
    photo: Photo | None,
    now: datetime | None = None,
) -> Ticked:
    """Tick one item of one store's list for ``due_on``. Written once: the same
    request again answers with its tick; another person's tick of the same item
    is refused, naming who ticked it."""
    client = _client_uuid(client_id, "client_id")
    template = _template(template_id)
    miss = ChecklistMiss.objects.filter(store=store, template=template, due_on=due_on).first()
    item = _item(template, miss, item_id)
    replay = _replay(client, store, template, str(item["id"]), due_on)
    if replay is not None:
        return Ticked(replay, created=False)
    _check_tickable(store, template, due_on, now or timezone.now(), recorded=miss is not None)
    key = _store_photo(store, client, photo) if photo is not None else ""
    holder: dict[str, ChecklistTick] = {}

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(
            LockRank.DOCUMENT, [f"checklist:{store.pk}:{template.pk}:{item['id']}:{due_on}"]
        )
        taken = (
            ChecklistTick.objects.filter(
                store=store, template=template, item_id=str(item["id"]), due_on=due_on
            )
            .select_related("ticked_by")
            .first()
        )
        if taken is not None:
            who = tick_json(taken)["by"] or "someone"
            raise Refusal(
                _TICKED, f"{who} has already ticked this, at {_clock(taken.ticked_at)}.", status=409
            )
        row = ChecklistTick(
            client_id=client,
            store=store,
            template=template,
            item_id=str(item["id"]),
            item_text=item["text"],
            due_on=due_on,
            ticked_by=actor,
            ticked_at=run.now,
            late=run.now > due_moment(template, due_on),
        )
        if photo is not None:
            row.photo_key = key
            row.photo_sha256 = photo.sha256
            row.photo_media_type = photo.media_type
            row.photo_size = len(photo.data)
        row.save()
        holder["row"] = row
        run.audit_subject_key = f"checklist:{store.pk}:{template.pk}:{due_on.isoformat()}"
        run.audit_site_id = store.pk
        run.audit_before = {
            "ticked": False,
            "store": store.code,
            "checklist": template.name,
            "item": item["text"],
            "due_on": due_on.isoformat(),
        }
        run.audit_after = {**tick_facts(row), "alert_closed": _close_if_done(run, miss)}
        return CommandResult(
            resource_type="checklist_tick", resource_id=str(row.pk), status_code=201
        )

    execute_command(
        _principal(actor, store.tenant_id),
        CommandSpec(
            action=TICK_ACTION,
            command_id=_command_id(TICK_ACTION, client),
            business_input={
                "store_id": store.pk,
                "template_id": str(template.pk),
                "item_id": str(item["id"]),
                "due_on": due_on.isoformat(),
                "photo_sha256": photo.sha256 if photo is not None else None,
            },
            site_id=store.pk,
            subject_key=f"checklist:{store.pk}:{template.pk}:{due_on.isoformat()}",
        ),
        handler,
    )
    row = ChecklistTick.objects.select_related("ticked_by").get(client_id=client)
    return Ticked(row, created="row" in holder)


def _close_if_done(run: CommandRun, miss: ChecklistMiss | None) -> str | None:
    """Ticking the last missed item of a recorded list closes its alert here, in
    the tick's own command. Only a close, never an insert: the worker's check
    opens alerts and keeps their words up to date."""
    if miss is None:
        return None
    ticked = set(
        ChecklistTick.objects.filter(
            store_id=miss.store_id, template_id=miss.template_id, due_on=miss.due_on
        ).values_list("item_id", flat=True)
    )
    if any(item["id"] not in ticked for item in miss.items):
        return None
    key = _miss_key(miss.store_id, miss.template_id, miss.due_on)
    Alert.objects.filter(
        kind=AlertKind.CHECKLIST_MISSED, status=AlertStatus.OPEN, dedupe_key=key
    ).update(status=AlertStatus.RESOLVED, resolved_at=run.now)
    return key


def _template(template_id: Any) -> ChecklistTemplate:
    pk = _uuid_or_none(template_id)
    template = ChecklistTemplate.objects.filter(pk=pk).first() if pk else None
    if template is None:
        raise Refusal("NOT_FOUND", "That checklist was not found.", status=404)
    return template


def _item(template: ChecklistTemplate, miss: ChecklistMiss | None, item_id: Any) -> dict[str, Any]:
    """The item on the checklist, or - for a list recorded as missed - on that
    record, so an item taken off the checklist since can still be ticked."""
    item = item_ids(template).get(str(item_id))
    if item is None and miss is not None:
        item = next((i for i in miss.items if i["id"] == str(item_id)), None)
    if item is None:
        raise Refusal("NOT_FOUND", "That item is not on this checklist.", status=404)
    return item


def _replay(
    client: uuid.UUID, store: Store, template: ChecklistTemplate, item_id: str, due_on: date
) -> ChecklistTick | None:
    """The tick this same request already saved, if it did."""
    row = ChecklistTick.objects.filter(client_id=client).select_related("ticked_by").first()
    if row is None:
        return None
    if (row.store_id, row.template_id, row.item_id, row.due_on) != (
        store.pk,
        template.pk,
        item_id,
        due_on,
    ):
        raise Refusal(
            "COMMAND_CONFLICT", "This tick was already sent for something else.", status=409
        )
    return row


def _check_tickable(
    store: Store, template: ChecklistTemplate, due_on: date, now: datetime, *, recorded: bool
) -> None:
    """Today's list, or an earlier one the store was shown: recorded as missed, or
    judged (its time came while the switch was on and after the last change)."""
    if not template.active:
        raise Refusal("CHECKLIST_STOPPED", "This checklist has been stopped.", status=409)
    first, today = window(now)
    shown = due_on == today or recorded or judged(template, due_on, on_since([store]).get(store.pk))
    if not first <= due_on <= today or not is_due_on(template, due_on) or not shown:
        raise Refusal(
            "NOT_DUE",
            f"{template.name} is not a list to tick for {due_on:%d %b %Y}: an item is ticked for "
            f"a day it is due, today or up to {missed_days()} days back.",
            status=409,
        )


def _uuid_or_none(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _clock(at: datetime) -> str:
    return timezone.localtime(at).strftime("%H:%M")


# ---------------------------------------------------------------------------
# The worker's check: record what was missed, and keep the alerts matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Missed:
    store: Store
    template: ChecklistTemplate
    due_on: date
    items: list[dict[str, str]]


def find_missed(stores: list[Store], now: datetime) -> list[Missed]:
    """Every list in the window whose time passed (while judged) with items not
    ticked by then, at these stores, not recorded yet."""
    first, today = window(now)
    candidates = _passed(stores, now)
    if not candidates:
        return []
    recorded = set(
        ChecklistMiss.objects.filter(
            store__in=stores, due_on__gte=first, due_on__lte=today
        ).values_list("store_id", "template_id", "due_on")
    )
    ticked = {
        (row.store_id, row.template_id, row.item_id, row.due_on): row.ticked_at
        for row in ChecklistTick.objects.filter(
            store__in=stores, due_on__gte=first, due_on__lte=today
        ).only("store_id", "template_id", "item_id", "due_on", "ticked_at")
    }
    found = []
    for store, template, day in candidates:
        if (store.pk, template.pk, day) in recorded:
            continue
        moment = due_moment(template, day)
        items = []
        for item in template.items:
            at = ticked.get((store.pk, template.pk, str(item["id"]), day))
            if at is None or at > moment:
                items.append({"id": str(item["id"]), "text": item["text"]})
        if items:
            found.append(Missed(store, template, day, items))
    return found


def _passed(stores: list[Store], now: datetime) -> list[tuple[Store, ChecklistTemplate, date]]:
    """Every list in the window whose time has passed and may be judged."""
    first, today = window(now)
    since = on_since(stores)
    templates = live_templates()
    return [
        (store, template, day)
        for store in stores
        for template in templates
        for day in due_days(template, first, today)
        if due_moment(template, day) <= now and judged(template, day, since.get(store.pk))
    ]


def run_check(tenant_id: uuid.UUID, now: datetime | None = None) -> dict[str, int]:
    """Record each list missed since the last run, and keep the checklist-missed
    alerts matching what is still not ticked. Safe to run as often as it likes."""
    now = now or timezone.now()
    stores = switched_on(list(Store.objects.filter(is_active=True, tenant_id=tenant_id)))
    missed = find_missed(stores, now)
    recorded = _record(tenant_id, missed) if missed else 0
    alerts = sync_alerts(now)
    return {"recorded": recorded, "alerts": alerts}


def _record(tenant_id: uuid.UUID, missed: list[Missed]) -> int:
    written: list[str] = []

    def handler(run: CommandRun) -> CommandResult:
        # The worker and a hand-run check may overlap: one at a time, and what the
        # other already recorded is not recorded again.
        run.advisory_lock(LockRank.DOCUMENT, ["checklist:check"])
        for miss in missed:
            _, made = ChecklistMiss.objects.get_or_create(
                store=miss.store,
                template=miss.template,
                due_on=miss.due_on,
                defaults={"items": miss.items, "found_at": run.now},
            )
            if made:
                written.append(_miss_key(miss.store.pk, miss.template.pk, miss.due_on))
        run.audit_subject_key = "checklist:check"
        run.audit_before = {"recorded": []}
        run.audit_after = {"recorded": written}
        return CommandResult(resource_type="checklist_check")

    execute_command(
        Principal(tenant_id=tenant_id, service_code=CHECK_SERVICE),
        CommandSpec(
            action=CHECK_ACTION,
            command_id=uuid.uuid4(),
            business_input={
                "missed": sorted(_miss_key(m.store.pk, m.template.pk, m.due_on) for m in missed)
            },
            subject_key="checklist:check",
        ),
        handler,
    )
    return len(written)


def _miss_key(store_id: int, template_id: Any, due_on: date) -> str:
    return f"checklist_missed:{store_id}:{template_id}:{due_on.isoformat()}"


def open_misses(now: datetime | None = None, store_ids: list[int] | None = None) -> list[Missed]:
    """Recorded misses of live checklists that can still be ticked (due within the
    window), with the items still not ticked. A miss whose items were all ticked
    late drops out; so does one older than the window, which can no longer be
    ticked - it stays in the exceptions report."""
    now = now or timezone.now()
    first, today = window(now)
    rows = ChecklistMiss.objects.filter(
        due_on__gte=first, due_on__lte=today, template__active=True
    ).select_related("store", "template")
    if store_ids is not None:
        rows = rows.filter(store_id__in=store_ids)
    rows_list = list(rows)
    if not rows_list:
        return []
    ticked = set(
        ChecklistTick.objects.filter(
            Q(store_id__in={row.store_id for row in rows_list}),
            due_on__gte=first,
            due_on__lte=today,
        ).values_list("store_id", "template_id", "item_id", "due_on")
    )
    out = []
    for row in rows_list:
        left = [
            item
            for item in row.items
            if (row.store_id, row.template_id, item["id"], row.due_on) not in ticked
        ]
        if left:
            out.append(Missed(row.store, row.template, row.due_on, left))
    return out


def sync_alerts(now: datetime | None = None) -> int:
    """One checklist-missed alert per recorded miss still open; the rest resolve."""
    hits = [
        AlertHit(
            dedupe_key=_miss_key(miss.store.pk, miss.template.pk, miss.due_on),
            title=(
                f"{miss.store.code}: {_items(len(miss.items))} of {miss.template.name} "
                f"missed on {miss.due_on:%d %b %Y}"
            ),
            store_id=miss.store.pk,
            brand="",
            object_id=miss.store.pk,
            due_date=miss.due_on,
            threshold_days=None,
        )
        for miss in open_misses(now)
    ]
    sync_kind(AlertKind.CHECKLIST_MISSED, AlertKind.CHECKLIST_MISSED.label, hits)
    return len(hits)


def _items(count: int) -> str:
    return "1 item" if count == 1 else f"{count} items"
