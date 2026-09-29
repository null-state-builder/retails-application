"""Setup > Audit Log (store operations PRD ST-OPS-3, ticket 02; overall PRD §10.6).

One read-only list of every recorded write. It reads the audit records that
already exist - each command's ``AuditEvent`` (who, what, when, where, before and
after) - and adds no second audit store and no way to change an entry.

Who may read it, and what they see, uses existing checks only (baseline B6):

* ``audit.view`` ("Read the audit trail") - the narrowest existing grant that
  reads other people's history. Today only the platform administrator and the
  owner role templates hold it.
* Stores: the stores the person may act at (``actionable_stores``, the same
  scope the menus read), narrowed to the stores their ``audit.view`` reaches,
  narrowed again to the stores where the ``audit-log`` feature is switched on
  (ST-OPS-6). An entry held at no store is shown only to a tenant-wide reader
  (``KDPS_AUDIT_LOG_SITELESS_TENANT_READERS``), the rule roles already follow.
* Values: secrets never; personal fields only with the ``personal`` field grant;
  cost and margin (any field whose name says cost, margin or layer value) only
  with the matching field grant **held at tenant or entity scope**. A grant held
  at one store or SBU - a store role - never reveals them. All of this is done
  here, on the server, before a value leaves.
* The person who made a change is named under the existing administrative
  history rule (``core.goods_administrative_history``): the reader's
  ``personal`` grant must cover that person.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from accounts.principal import AccessContext, GrantView
from core.goods_administrative_history import ActorVisibility, actor_visibility
from core.kernel_models import AuditEvent
from core.refusals import Refusal
from masters.models import Store
from masters.scoping import actionable_stores
from masters.store_feature_registry import StoreFeature
from masters.store_features import switch_states

FEATURE_KEY = "audit-log"
READ_ACTION = "audit.view"
EXPORT_ACTION = "audit.log.export"
NO_RECORD_TYPE = "other"

_CURSOR_MAX = 200
_SECRET_WORDS = ("credential", "password", "secret", "token")
_PERSONAL_FIELDS = frozenset({"address", "email", "mobile", "pan", "personal", "phone"})
#: A field name containing one of these needs that field grant (cost / margin).
_VALUE_WORDS: tuple[tuple[str, str], ...] = (
    ("cost", "cost"),
    ("margin", "margin"),
    ("layer_value", "layer_value"),
)
#: Scopes at which a cost or margin grant may reveal those values here. A store
#: or SBU grant is a store role's, and store roles never see cost or margin.
_VALUE_SCOPES = frozenset({"tenant", "entity"})


@dataclass(frozen=True)
class Filters:
    person: uuid.UUID | None = None
    store_id: int | None = None
    record_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "person": str(self.person) if self.person else None,
            "store_id": self.store_id,
            "record_type": self.record_type,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
        }


@dataclass(frozen=True)
class Scope:
    """What one reader may see: the stores, and whether store-less entries too."""

    access: AccessContext
    stores: list[Store]
    siteless: bool

    @property
    def store_ids(self) -> list[int]:
        return [store.pk for store in self.stores]


# -- who may read, and where ---------------------------------------------------


def _tenant_reader(access: AccessContext) -> bool:
    return any(g.scope_kind == "tenant" and READ_ACTION in g.actions for g in access.grants)


def reader_scope(access: AccessContext, audit_feature: StoreFeature) -> Scope:
    """The reader's stores, or a refusal that says why there are none.

    ``ACTION_DENIED`` without ``audit.view``; ``FEATURE_OFF`` when the audit log
    is switched off at every store the reader could otherwise see.
    """
    access.require_action(READ_ACTION)
    candidates = [
        store
        for store in actionable_stores(access.user)
        if access.can_at_store(READ_ACTION, store.pk)
    ]
    on = {state.site_id for state in switch_states(candidates, [audit_feature]) if state.enabled}
    stores = [store for store in candidates if store.pk in on]
    if not stores:
        raise Refusal(
            "FEATURE_OFF",
            f"{audit_feature.name} is switched off at every store you can see. "
            "Admin can switch it on in Setup, Feature Switches.",
            status=403,
        )
    siteless = bool(getattr(settings, "KDPS_AUDIT_LOG_SITELESS_TENANT_READERS", True))
    return Scope(access=access, stores=stores, siteless=siteless and _tenant_reader(access))


# -- filters and paging ----------------------------------------------------------


def _date(params: dict[str, str], key: str) -> date | None:
    raw = params.get(key, "").strip()
    if not raw:
        return None
    value = parse_date(raw) if len(raw) == 10 else None
    if value is None:
        raise Refusal("INVALID_REQUEST", f"{key} must be a date like 2026-09-27.")
    return value


def parse_filters(params: dict[str, str]) -> Filters:
    person_raw = params.get("person", "").strip()
    store_raw = params.get("store", "").strip()
    try:
        person = uuid.UUID(person_raw) if person_raw else None
    except ValueError:
        raise Refusal("INVALID_REQUEST", "person must be a person id.") from None
    try:
        store_id = int(store_raw) if store_raw else None
    except ValueError:
        raise Refusal("INVALID_REQUEST", "store must be a store id.") from None
    record_type = params.get("record_type", "").strip()[:60] or None
    filters = Filters(
        person=person,
        store_id=store_id,
        record_type=record_type,
        date_from=_date(params, "date_from"),
        date_to=_date(params, "date_to"),
    )
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise Refusal("INVALID_REQUEST", "The start date is after the end date.")
    return filters


#: A subject key names its record type before the first colon (``sale:7``,
#: ``store_feature:1:audit-log``). A key without one (a bare id) is "other".
_TYPED_KEY = r"^[a-z][a-z0-9_]{0,59}(:|$)"


def _record_type_expr() -> models.Case:
    return models.Case(
        models.When(
            subject_key__regex=_TYPED_KEY,
            then=models.Func(
                models.F("subject_key"),
                models.Value(":"),
                models.Value(1),
                function="split_part",
                output_field=models.CharField(),
            ),
        ),
        default=models.Value(NO_RECORD_TYPE),
        output_field=models.CharField(),
    )


def _day_start(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def scoped_events(scope: Scope) -> models.QuerySet[AuditEvent]:
    """Every entry the reader may see, before their own filters."""
    where = models.Q(site_id__in=scope.store_ids)
    if scope.siteless:
        where |= models.Q(site_id__isnull=True)
    return AuditEvent.objects.filter(tenant_id=scope.access.tenant_id).filter(where)


def filtered_events(scope: Scope, filters: Filters) -> models.QuerySet[AuditEvent]:
    rows = scoped_events(scope)
    if filters.person is not None:
        rows = rows.filter(actor_id=filters.person)
    if filters.store_id is not None:
        # A store outside the reader's scope simply matches nothing.
        rows = rows.filter(site_id=filters.store_id)
    if filters.record_type is not None:
        rows = rows.annotate(record_type=_record_type_expr()).filter(
            record_type=filters.record_type
        )
    if filters.date_from is not None:
        rows = rows.filter(recorded_at__gte=_day_start(filters.date_from))
    if filters.date_to is not None:
        rows = rows.filter(recorded_at__lt=_day_start(filters.date_to + timedelta(days=1)))
    return rows


def encode_cursor(event: AuditEvent) -> str:
    raw = json.dumps(
        {"r": event.recorded_at.isoformat(), "i": str(event.pk)}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def after_cursor(
    rows: models.QuerySet[AuditEvent], cursor: str | None
) -> models.QuerySet[AuditEvent]:
    if not cursor:
        return rows
    if len(cursor) > _CURSOR_MAX:
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    try:
        value = json.loads(base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()))
        recorded_at = parse_datetime(str(value["r"]))
        last_id = uuid.UUID(str(value["i"]))
    except (ValueError, KeyError, TypeError):
        raise Refusal("INVALID_REQUEST", "cursor is not valid.") from None
    if recorded_at is None or not timezone.is_aware(recorded_at):
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    return rows.filter(
        models.Q(recorded_at__lt=recorded_at) | models.Q(recorded_at=recorded_at, pk__lt=last_id)
    )


def ordered(rows: models.QuerySet[AuditEvent]) -> models.QuerySet[AuditEvent]:
    return rows.order_by("-recorded_at", "-pk")


# -- what a reader may see of the values -----------------------------------------


def _needed_field(name: str) -> str | None:
    """The field grant a value needs, or None when anyone reading the entry may see it."""
    lowered = name.lower()
    for word, grant in _VALUE_WORDS:
        if word in lowered:
            return grant
    if lowered in _PERSONAL_FIELDS:
        return "personal"
    return None


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in _SECRET_WORDS)


def visible_fields(access: AccessContext, site_id: int | None) -> set[str]:
    """The cost, margin and personal grants this reader holds over one entry.

    Cost, margin and layer value count only from a grant held at tenant or entity
    scope: a store or SBU grant is a store role's, and store roles never see them.
    """
    shown: set[str] = set()
    for grant in access.grants:
        if READ_ACTION not in grant.actions:
            continue
        if not access.reaches_site(grant, site_id):
            continue
        shown |= _grant_fields(grant)
    return shown


def _grant_fields(grant: GrantView) -> set[str]:
    fields = {"personal"} & set(grant.fields)
    # Tenant and entity grants are never brand-limited, so no brand check is needed here.
    if grant.scope_kind in _VALUE_SCOPES:
        fields |= {"cost", "margin", "layer_value"} & set(grant.fields)
    return fields


def _allowed(name: str, shown: set[str]) -> bool:
    if _is_secret(name):
        return False
    needed = _needed_field(name)
    return needed is None or needed in shown


def _clean(value: Any, shown: set[str]) -> Any:
    """``value`` with every hidden key removed, at any depth."""
    if isinstance(value, dict):
        return {
            str(key): _clean(inner, shown)
            for key, inner in value.items()
            if _allowed(str(key), shown)
        }
    if isinstance(value, list):
        return [_clean(inner, shown) for inner in value]
    return value


def _flatten(prefix: str, value: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(value, dict) and value:
        for key in sorted(value):
            _flatten(f"{prefix}.{key}" if prefix else str(key), value[key], out)
        return
    out.append({"field": prefix or "value", "redacted": False, "value": value})


def _is_safe_values(raw: Any) -> bool:
    return (
        isinstance(raw, list)
        and bool(raw)
        and all(isinstance(entry, dict) and "field" in entry for entry in raw)
    )


def shown_values(raw: Any, shown: set[str]) -> list[dict[str, Any]] | None:
    """One entry's before or after as ``[{field, value, redacted}]``, hidden fields gone.

    Reads both shapes the audit records hold: the established ``SafeAuditValues``
    list (``[{field, value, redacted}]``) and a plain object, flattened to dotted
    field names. A field is dropped when any part of its name needs a grant the
    reader lacks, and inner objects are cleaned the same way.
    """
    if raw is None:
        return None
    out: list[dict[str, Any]] = []
    if _is_safe_values(raw):
        for entry in raw:
            field = str(entry.get("field", ""))[:200]
            if not field or not all(_allowed(part, shown) for part in field.split(".")):
                continue
            redacted = bool(entry.get("redacted"))
            out.append(
                {
                    "field": field,
                    "redacted": redacted,
                    "value": None if redacted else _clean(entry.get("value"), shown),
                }
            )
        return out
    _flatten("", _clean(raw, shown), out)
    return out


# -- the rows ---------------------------------------------------------------------


def record_type(subject_key: str | None) -> str:
    """The same rule as ``_record_type_expr``, for one key already read."""
    if not subject_key or not re.match(_TYPED_KEY, subject_key):
        return NO_RECORD_TYPE
    return subject_key.split(":", 1)[0]


def entries(scope: Scope, events: list[AuditEvent]) -> list[dict[str, Any]]:
    actors = actor_visibility(scope.access, events)
    stores = {store.pk: store for store in scope.stores}
    grants_by_site: dict[int | None, set[str]] = {}
    out: list[dict[str, Any]] = []
    for event in events:
        if event.site_id not in grants_by_site:
            grants_by_site[event.site_id] = visible_fields(scope.access, event.site_id)
        shown = grants_by_site[event.site_id]
        store = stores.get(event.site_id) if event.site_id is not None else None
        out.append(
            {
                "id": str(event.pk),
                "recorded_at": event.recorded_at,
                "event_at": event.event_at,
                **_who(actors, event),
                "service_code": event.service_code,
                "store_id": event.site_id,
                "store_code": store.code if store else None,
                "store_name": store.name if store else None,
                "action": event.action,
                "record_type": record_type(event.subject_key),
                "record_key": event.subject_key,
                "outcome": event.outcome,
                "reason_code": event.reason_code,
                "before": shown_values(event.before, shown),
                "after": shown_values(event.after, shown),
            }
        )
    return out


def _who(actors: ActorVisibility, event: AuditEvent) -> dict[str, Any]:
    fields = actors.fields(event.actor_id)
    return {
        "actor_id": fields["actor_id"],
        "actor_name": fields["actor_name"],
        "actor_hidden": event.actor_id is not None and fields["actor_id"] is None,
    }


def options(scope: Scope) -> dict[str, Any]:
    """What the filters offer: the reader's stores, visible people and record types."""
    rows = scoped_events(scope)
    actor_ids = list(
        rows.exclude(actor_id__isnull=True).values_list("actor_id", flat=True).distinct()
    )
    actors = actor_visibility(scope.access, [SimpleNamespace(actor_id=a) for a in actor_ids])
    people = sorted(
        (
            {"id": str(actor_id), "name": actors.name_for(actor_id) or ""}
            for actor_id in actor_ids
            if actors.id_for(actor_id) is not None
        ),
        key=lambda person: (person["name"].lower(), person["id"]),
    )
    types = {
        kind or NO_RECORD_TYPE
        for kind in rows.annotate(record_type=_record_type_expr())
        .values_list("record_type", flat=True)
        .distinct()
    }
    return {
        "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in scope.stores],
        "people": people,
        "record_types": sorted(types),
    }


# -- the spreadsheet ------------------------------------------------------------------


EXPORT_COLUMNS = (
    "When",
    "Who",
    "Store",
    "What",
    "Record",
    "Outcome",
    "Reason",
    "Before",
    "After",
)


def _values_text(values: list[dict[str, Any]] | None) -> str:
    if values is None:
        return ""
    parts = []
    for entry in values:
        value = "(hidden)" if entry["redacted"] else entry["value"]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, default=str)
        parts.append(f"{entry['field']}: {value}")
    return "\n".join(parts)


def _cell_text(value: str) -> str:
    """A text cell a spreadsheet will not run as a formula."""
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = [list(EXPORT_COLUMNS)]
    local = timezone.get_current_timezone()
    for item in items:
        who = item["actor_name"] or (
            "(hidden)" if item["actor_hidden"] else (item["service_code"] or "")
        )
        store = f"{item['store_name']} ({item['store_code']})" if item["store_code"] else "No store"
        rows.append(
            [
                item["recorded_at"].astimezone(local).strftime("%Y-%m-%d %H:%M:%S"),
                _cell_text(who),
                _cell_text(store),
                _cell_text(item["action"]),
                _cell_text(item["record_key"] or ""),
                _cell_text(item["outcome"]),
                _cell_text(item["reason_code"] or ""),
                _cell_text(_values_text(item["before"])),
                _cell_text(_values_text(item["after"])),
            ]
        )
    return rows
