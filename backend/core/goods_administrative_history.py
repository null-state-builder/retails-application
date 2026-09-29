"""E247's scoped, stable administrative-history reader.

Administrative facts are already append-only: ``AuditEvent`` records the
command outcome, ``MasterVersion`` records governed master snapshots, and
``SiteCapabilityEvent`` records readiness and closure decisions.  This module
turns those existing facts into one read-only stream; it deliberately does not
add a second mutable history projection.

Sites and organisation masters (ticket 02C) read their governed facts.  People,
logins and roles (ticket 03D) read their command outcomes instead, because the
outcome is what names a privileged change and what its review acknowledges.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import models
from django.db.models.functions import Cast, Concat
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.principal import AccessContext
from core.kernel_models import AuditEvent
from core.refusals import Refusal
from masters.goods_models import Location, MasterVersion, Sbu, SiteCapabilityEvent, Tenant
from masters.models import Gstin, LegalEntity, Store

_CURSOR_MAX = 200
_SECRET_FIELDS = frozenset({"credential", "password", "password_hash", "secret", "token"})
_PERSONAL_FIELDS = frozenset({"address", "email", "mobile", "pan", "personal"})
_FIELD_GRANTS = frozenset({"cost", "layer_value", "margin", "personal"})


@dataclass(frozen=True)
class Subject:
    kind: str
    id: str
    site_id: int | None = None
    entity_id: int | None = None
    master_kind: str | None = None
    #: People, logins and roles: the audit subject keys their commands write under,
    #: and the ``MasterVersion`` (kind, target) that numbers their revisions.
    audit_keys: tuple[str, ...] = ()
    version: tuple[str, str] | None = None


@dataclass(frozen=True)
class HistoryEvent:
    id: uuid.UUID
    recorded_at: datetime
    body: dict[str, Any]


@dataclass(frozen=True)
class Cursor:
    last_at: datetime
    last_id: uuid.UUID


@dataclass(frozen=True)
class ActorVisibility:
    access: AccessContext
    dimensions: dict[uuid.UUID, tuple[int | None, int | None]]
    names: dict[uuid.UUID, str]

    def id_for(self, actor_id: uuid.UUID | None) -> str | None:
        if actor_id is None:
            return None
        site_id, entity_id = self.dimensions.get(actor_id, (None, None))
        if "personal" not in self.access.field_grants(
            site_id=site_id, entity_id=entity_id, actions={"audit.view"}
        ):
            return None
        return str(actor_id)

    def name_for(self, actor_id: uuid.UUID | None) -> str | None:
        """The actor's name under exactly the rule that reveals their id."""
        if self.id_for(actor_id) is None:
            return None
        assert actor_id is not None
        return self.names.get(actor_id)

    def fields(self, actor_id: uuid.UUID | None) -> dict[str, str | None]:
        return {"actor_id": self.id_for(actor_id), "actor_name": self.name_for(actor_id)}


def _encode_cursor(subject: Subject, last: HistoryEvent) -> str:
    raw = json.dumps(
        {
            "s": f"{subject.kind}:{subject.id}",
            "r": last.recorded_at.isoformat(),
            "i": str(last.id),
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, subject: Subject) -> Cursor | None:
    if not cursor:
        return None
    if len(cursor) > _CURSOR_MAX:
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if value["s"] != f"{subject.kind}:{subject.id}":
            raise ValueError
        recorded_at = parse_datetime(str(value["r"]))
        event_id = uuid.UUID(str(value["i"]))
    except (ValueError, KeyError, TypeError):
        raise Refusal("INVALID_REQUEST", "cursor is not valid.") from None
    if recorded_at is None or not timezone.is_aware(recorded_at):
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    return Cursor(recorded_at, event_id)


def resolve_subject(access: AccessContext, subject_kind: str, subject_id: str) -> Subject:
    """Resolve a 02C setup subject or a 03D person, login or role inside the reader's scope."""
    # A reader who holds no `audit.view` anywhere is refused before any lookup,
    # so a real and a missing subject answer the same (02C-S1).
    access.require_action("audit.view")
    if subject_kind == "site":
        try:
            site_id = int(subject_id)
        except ValueError:
            raise Refusal("NOT_FOUND", "That record was not found.") from None
        site = (
            Store.objects.select_related("gstin__legal_entity")
            .filter(pk=site_id, tenant_id=access.tenant_id)
            .first()
        )
        if site is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        access.require("audit.view", site_id=site.pk, entity_id=site.gstin.legal_entity_id)
        return Subject("site", str(site.pk), site_id=site.pk, entity_id=site.gstin.legal_entity_id)
    if subject_kind == "staff":
        return _resolve_staff(access, subject_id)
    if subject_kind == "user":
        return _resolve_login(access, subject_id)
    if subject_kind == "role":
        return _resolve_role(access, subject_id)
    if subject_kind != "master":
        raise Refusal("NOT_FOUND", "That record was not found.")
    return _resolve_master(access, subject_id)


def _hidden() -> Refusal:
    return Refusal("NOT_FOUND", "That record was not found.")


def _entity_of(site_id: int | None) -> int | None:
    if site_id is None:
        return None
    return Store.objects.filter(pk=site_id).values_list("gstin__legal_entity_id", flat=True).first()


def _placed_site(tenant_id: uuid.UUID, human_id: uuid.UUID | None) -> tuple[str | None, int | None]:
    """The person's staff record and current placement site, if they have them."""
    from accounts.goods_admin_services import NO_PLACEMENT, placements
    from accounts.goods_models import Staff

    staff_id = (
        Staff.objects.filter(tenant_id=tenant_id, human_id=human_id)
        .values_list("pk", flat=True)
        .first()
    )
    if staff_id is None:
        return None, None
    return str(staff_id), placements([staff_id]).get(staff_id, NO_PLACEMENT).site_id


def _tenant_reader(access: AccessContext) -> bool:
    return any(g.scope_kind == "tenant" and "audit.view" in g.actions for g in access.grants)


def _reader_covers_grant(access: AccessContext, grant: Any) -> bool:
    """Whether the reader's `audit.view` reaches every place one of the login's grants does.

    A login is its whole authority, so its history is hidden from anyone who could
    not see all of it - the same rule E074 uses before a login may be changed.
    """
    sites = [None] if grant.all_sites else list(grant.site_ids)
    brands = [None] if grant.all_brands else list(grant.brand_ids)
    return access.covers_all(
        {"audit.view"},
        ((site, brand) for site in sites for brand in brands),
    )


def _resolve_staff(access: AccessContext, subject_id: str) -> Subject:
    from accounts.goods_models import Staff

    human_id = (
        Staff.objects.filter(tenant_id=access.tenant_id, pk=_uuid_id(subject_id))
        .values_list("human_id", flat=True)
        .first()
    )
    if human_id is None:
        raise _hidden()
    staff_id, site_id = _placed_site(access.tenant_id, human_id)
    # A head-office person, placed nowhere, is reached only by a tenant-wide
    # reader - the rule every site-less staff record follows (03B).
    if not access.can_at_store("audit.view", site_id):
        raise _hidden()
    return Subject(
        "staff",
        str(staff_id),
        site_id=site_id,
        entity_id=_entity_of(site_id),
        # A refused login attempt is audited under the person, not yet a login.
        audit_keys=(f"staff:{staff_id}", f"human:{human_id}"),
        version=("staff", str(staff_id)),
    )


def _resolve_login(access: AccessContext, subject_id: str) -> Subject:
    from accounts.principal import effective_grants
    from accounts.models import User

    user_id = _integer_id(subject_id)
    human_id = (
        User.objects.filter(tenant_id=access.tenant_id, pk=user_id, human__isnull=False)
        .values_list("human_id", flat=True)
        .first()
    )
    if human_id is None:
        raise _hidden()
    _, site_id = _placed_site(access.tenant_id, human_id)
    if not access.can_at_store("audit.view", site_id):
        raise _hidden()
    for grant in effective_grants(human_id):
        if not _reader_covers_grant(access, grant):
            raise _hidden()
    return Subject(
        "user",
        str(user_id),
        site_id=site_id,
        entity_id=_entity_of(site_id),
        audit_keys=(f"user:{user_id}",),
        version=("user", str(user_id)),
    )


def _resolve_role(access: AccessContext, subject_id: str) -> Subject:
    """Roles belong to the whole tenant, so only a tenant-wide reader sees their history."""
    from accounts.models import Role

    code = (
        Role.objects.filter(tenant_id=access.tenant_id, pk=_integer_id(subject_id))
        .values_list("code", flat=True)
        .first()
    )
    if code is None or not _tenant_reader(access):
        raise _hidden()
    return Subject(
        "role",
        subject_id,
        audit_keys=(f"role:{code}",),
        version=("role", str(code)),
    )


def _integer_id(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise Refusal("NOT_FOUND", "That record was not found.") from None


def _uuid_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise Refusal("NOT_FOUND", "That record was not found.") from None


def _resolve_master(access: AccessContext, subject_id: str) -> Subject:
    """Master identifiers are qualified (`entity:42`), never guessed by raw id."""
    family, separator, key = subject_id.partition(":")
    if not separator or not key:
        raise Refusal("NOT_FOUND", "That record was not found.")
    if family == "entity":
        entity_id = _integer_id(key)
        entity = LegalEntity.objects.filter(pk=entity_id, tenant_id=access.tenant_id).first()
        if entity is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        access.require("audit.view", entity_id=entity.pk)
        return Subject("master", subject_id, entity_id=entity.pk, master_kind=family)
    if family == "registration":
        registration_id = _integer_id(key)
        registration = (
            Gstin.objects.select_related("legal_entity")
            .filter(pk=registration_id, tenant_id=access.tenant_id)
            .first()
        )
        if registration is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        access.require("audit.view", entity_id=registration.legal_entity_id)
        return Subject(
            "master", subject_id, entity_id=registration.legal_entity_id, master_kind=family
        )
    if family == "tenant":
        tenant_id = _uuid_id(key)
        tenant = Tenant.objects.filter(pk=tenant_id).filter(pk=access.tenant_id).first()
        if tenant is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        access.require("audit.view")
        return Subject("master", subject_id, master_kind=family)
    raise Refusal("NOT_FOUND", "That record was not found.")


def _granted_fields(access: AccessContext, subject: Subject) -> set[str]:
    return access.field_grants(
        site_id=subject.site_id,
        entity_id=subject.entity_id,
        actions={"audit.view"},
    )


def _field_visible(name: str, grants: set[str]) -> bool:
    if name.lower() in _SECRET_FIELDS:
        return False
    required = "personal" if name in _PERSONAL_FIELDS else name if name in _FIELD_GRANTS else None
    return required is None or required in grants


def _safe_values(raw: Any, grants: set[str]) -> list[dict[str, Any]] | None:
    """Filter the established SafeAuditValues shape without leaking redacted data."""
    if not isinstance(raw, list):
        return None
    values = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        field = str(entry.get("field", ""))[:100]
        if not field or not _field_visible(field, grants):
            continue
        redacted = bool(entry.get("redacted"))
        values.append(
            {
                "field": field,
                "redacted": redacted,
                "value": None if redacted else entry.get("value"),
            }
        )
    return values


def _snapshot_values(raw: Any, grants: set[str]) -> list[dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    return [
        {"field": key, "redacted": False, "value": value}
        for key, value in sorted(raw.items())
        if _field_visible(str(key), grants)
    ]


def _history_rows(rows: models.QuerySet[Any], cursor: Cursor | None, limit: int) -> list[Any]:
    """Bound each evidence source before the in-memory cross-source merge."""
    if cursor is not None:
        rows = rows.filter(
            models.Q(recorded_at__lt=cursor.last_at)
            | models.Q(recorded_at=cursor.last_at, pk__lt=cursor.last_id)
        )
    return list(rows.order_by("-recorded_at", "-pk")[: limit + 1])


def _actor_visibility(access: AccessContext, evidence_rows: Iterable[Any]) -> ActorVisibility:
    """Resolve each actor once, under the reader's real people scope.

    People administration remains Ticket 03D's responsibility.  This reader
    only asks whether its already-effective `audit.view` and `personal` grant
    covers the actor's current placement.  An unplaced actor is therefore
    visible to a tenant-wide administrator but not manufactured into an
    entity/site-scoped person.
    """
    actor_ids = {row.actor_id for row in evidence_rows if row.actor_id is not None}
    if not actor_ids:
        return ActorVisibility(access, {}, {})
    from accounts.goods_admin_services import placements
    from accounts.goods_models import HumanIdentity, Staff

    staff_by_human = dict(
        Staff.objects.filter(tenant_id=access.tenant_id, human_id__in=actor_ids).values_list(
            "human_id", "pk"
        )
    )
    placements_by_staff = placements(list(staff_by_human.values()))
    site_ids = {
        placement.site_id for placement in placements_by_staff.values() if placement.site_id
    }
    entities_by_site = dict(
        Store.objects.filter(pk__in=site_ids).values_list("pk", "gstin__legal_entity_id")
    )
    dimensions = {
        human_id: (
            placement.site_id if (placement := placements_by_staff.get(staff_id)) else None,
            entities_by_site.get(placement.site_id) if placement and placement.site_id else None,
        )
        for human_id, staff_id in staff_by_human.items()
    }
    names = dict(
        HumanIdentity.objects.filter(tenant_id=access.tenant_id, pk__in=actor_ids).values_list(
            "pk", "display_name"
        )
    )
    return ActorVisibility(access, dimensions, names)


def actor_visibility(access: AccessContext, evidence_rows: Iterable[Any]) -> ActorVisibility:
    """Who made each change, under this module's one rule - for other readers of
    the same audit records (Setup > Audit Log, store operations ticket 02)."""
    return _actor_visibility(access, evidence_rows)


def _audit_events(
    rows: Iterable[AuditEvent],
    subject: Subject,
    grants: set[str],
    actors: ActorVisibility,
) -> Iterable[HistoryEvent]:
    for event in rows:
        yield HistoryEvent(
            id=event.pk,
            recorded_at=event.recorded_at,
            body={
                "id": str(event.pk),
                "subject_kind": subject.kind,
                "subject_id": subject.id,
                **actors.fields(event.actor_id),
                "recorded_at": event.recorded_at.isoformat(),
                "revision": None,
                "event_kind": event.action,
                "outcome": event.outcome,
                "reason_code": event.reason_code,
                "before": _safe_values(event.before, grants),
                "after": _safe_values(event.after, grants),
                "evidence_ids": [],
            },
        )


def _previous_master_payloads(
    access: AccessContext, rows: Iterable[MasterVersion]
) -> dict[tuple[str, str, int], Any]:
    previous_keys = {
        (row.kind, row.target_key, row.revision - 1) for row in rows if row.revision > 1
    }
    if not previous_keys:
        return {}
    predicate = models.Q()
    for kind, target_key, revision in previous_keys:
        predicate |= models.Q(kind=kind, target_key=target_key, revision=revision)
    return {
        (row.kind, row.target_key, row.revision): row.payload
        for row in MasterVersion.objects.filter(tenant_id=access.tenant_id).filter(predicate)
    }


def _master_version_events(
    rows: Iterable[MasterVersion],
    subject: Subject,
    grants: set[str],
    actors: ActorVisibility,
    previous_payloads: dict[tuple[str, str, int], Any],
) -> Iterable[HistoryEvent]:
    events: list[HistoryEvent] = []
    for row in sorted(rows, key=lambda row: (row.kind, row.target_key, row.recorded_at, row.pk)):
        previous = previous_payloads.get((row.kind, row.target_key, row.revision))
        outcome = "retired" if row.retired else "succeeded"
        event_name = "retired" if row.retired else "created" if row.revision == 1 else "changed"
        events.append(
            HistoryEvent(
                id=row.pk,
                recorded_at=row.recorded_at,
                body={
                    "id": str(row.pk),
                    "subject_kind": subject.kind,
                    "subject_id": subject.id,
                    **actors.fields(row.actor_id),
                    "recorded_at": row.recorded_at.isoformat(),
                    "revision": row.revision,
                    "event_kind": f"master.{row.kind}.{event_name}",
                    "outcome": outcome,
                    "reason_code": row.reason_code,
                    "before": _snapshot_values(previous, grants),
                    "after": _snapshot_values(row.payload, grants),
                    "evidence_ids": [],
                },
            )
        )
    return events


def _master_query(access: AccessContext, subject: Subject) -> models.QuerySet[MasterVersion]:
    if subject.master_kind is None or subject.master_kind == "tenant":
        return MasterVersion.objects.none()
    _, _, target_key = subject.id.partition(":")
    return MasterVersion.objects.filter(
        tenant_id=access.tenant_id, kind=subject.master_kind, target_key=target_key
    )


def _site_master_query(access: AccessContext, subject: Subject) -> models.QuerySet[MasterVersion]:
    if subject.site_id is None:
        return MasterVersion.objects.none()
    location_ids = (
        Location.objects.filter(site_id=subject.site_id)
        .annotate(history_key=Cast("pk", output_field=models.CharField()))
        .values("history_key")
    )
    sbu_ids = (
        Sbu.objects.filter(site_id=subject.site_id)
        .annotate(history_key=Cast("pk", output_field=models.CharField()))
        .values("history_key")
    )
    return MasterVersion.objects.filter(tenant_id=access.tenant_id).filter(
        models.Q(kind="site", target_key=str(subject.site_id))
        | models.Q(kind="location", target_key__in=location_ids)
        | models.Q(kind="sbu", target_key__in=sbu_ids)
    )


def _capability_query(
    access: AccessContext, subject: Subject
) -> models.QuerySet[SiteCapabilityEvent]:
    if subject.site_id is None:
        return SiteCapabilityEvent.objects.none()
    return SiteCapabilityEvent.objects.filter(tenant_id=access.tenant_id, site_id=subject.site_id)


def _audit_query(
    access: AccessContext, subject: Subject, fact_queries: Iterable[models.QuerySet[Any]]
) -> models.QuerySet[AuditEvent]:
    """The command outcomes that belong to this subject, twins of facts excluded.

    A site's setup commands carry the site on the indexed ``AuditEvent.site``
    key, whichever subject key they audit under (the site master, the site
    itself, its locations or SBUs); other goods commands at the site are not
    its administrative history.  Entity and registration commands audit under
    ``entity:<id>`` and ``registration:<id>``.  A succeeded command also wrote
    its governed fact (master version or capability event) in the same
    instant; that fact is shown instead, and the exclusion reads the whole
    subject rather than the current page, so a twin cannot resurface on a
    later page (02C-S2).
    """
    rows = AuditEvent.objects.filter(tenant_id=access.tenant_id)
    if subject.kind == "site":
        rows = rows.filter(site_id=subject.site_id).filter(
            models.Q(subject_key__in=["master:site", f"site:{subject.site_id}"])
            | models.Q(subject_key__startswith="location:")
            | models.Q(subject_key__startswith="sbu:")
        )
    elif subject.master_kind == "tenant":
        rows = rows.filter(subject_key="tenant")
    else:
        rows = rows.filter(subject_key=subject.id)
    for facts in fact_queries:
        rows = rows.exclude(command_key_id__in=facts.values("command_key_id"))
    return rows


def _site_master_events(
    rows: Iterable[MasterVersion],
    subject: Subject,
    grants: set[str],
    actors: ActorVisibility,
    previous_payloads: dict[tuple[str, str, int], Any],
) -> Iterable[HistoryEvent]:
    return _master_version_events(rows, subject, grants, actors, previous_payloads)


def _site_capability_events(
    rows: Iterable[SiteCapabilityEvent],
    subject: Subject,
    grants: set[str],
    actors: ActorVisibility,
) -> Iterable[HistoryEvent]:
    events: list[HistoryEvent] = []
    for event in rows:
        events.append(
            HistoryEvent(
                id=event.pk,
                recorded_at=event.recorded_at,
                body={
                    "id": str(event.pk),
                    "subject_kind": "site",
                    "subject_id": str(subject.site_id),
                    **actors.fields(event.actor_id),
                    "recorded_at": event.recorded_at.isoformat(),
                    "revision": event.site_revision,
                    "event_kind": f"site.{event.operation}.{event.outcome}",
                    "outcome": event.outcome,
                    "reason_code": event.reason_code,
                    "before": None,
                    "after": _snapshot_values({"checks": event.checks}, grants),
                    "evidence_ids": [str(event.evidence_id)] if event.evidence_id else [],
                },
            )
        )
    return events


def _access_audit_query(access: AccessContext, subject: Subject) -> models.QuerySet[AuditEvent]:
    """A person's, login's or role's command outcomes, and the reviews of its privileged ones.

    A review is audited under the change it acknowledges (``audit:<id>``); it
    belongs in the reviewed subject's history, with its own distinct actor, so
    viewing a history never stands in for that acknowledgement (03D).
    """
    from accounts.goods_admin_services import ACTION_REVIEW, PRIVILEGED_ACTIONS

    own = AuditEvent.objects.filter(tenant_id=access.tenant_id, subject_key__in=subject.audit_keys)
    reviewed = (
        own.filter(action__in=sorted(PRIVILEGED_ACTIONS), outcome="succeeded")
        .annotate(review_key=Concat(models.Value("audit:"), Cast("pk", models.CharField())))
        .values("review_key")
    )
    return AuditEvent.objects.filter(tenant_id=access.tenant_id).filter(
        models.Q(subject_key__in=subject.audit_keys)
        | models.Q(action=ACTION_REVIEW, outcome="succeeded", subject_key__in=reviewed)
    )


def _revisions(
    access: AccessContext, subject: Subject, rows: Iterable[AuditEvent]
) -> dict[uuid.UUID, int]:
    """The subject revision each command wrote, read from its governed version twin."""
    if subject.version is None:
        return {}
    kind, target_key = subject.version
    return dict(
        MasterVersion.objects.filter(
            tenant_id=access.tenant_id,
            kind=kind,
            target_key=target_key,
            command_key_id__in=[row.command_key_id for row in rows],
        ).values_list("command_key_id", "revision")
    )


def _page(subject: Subject, events: list[HistoryEvent], limit: int) -> dict[str, Any]:
    events.sort(key=lambda event: (event.recorded_at, event.id), reverse=True)
    window = events[: limit + 1]
    page = window[:limit]
    return {
        "items": [event.body for event in page],
        "next_cursor": _encode_cursor(subject, page[-1]) if len(window) > limit else None,
        "as_of": timezone.now().isoformat(),
    }


def _access_history(
    access: AccessContext, subject: Subject, after: Cursor | None, limit: int
) -> dict[str, Any]:
    grants = _granted_fields(access, subject)
    rows = _history_rows(_access_audit_query(access, subject), after, limit)
    actors = _actor_visibility(access, rows)
    revisions = _revisions(access, subject, rows)
    events = list(_audit_events(rows, subject, grants, actors))
    for event, row in zip(events, rows, strict=True):
        event.body["revision"] = revisions.get(row.command_key_id)
    return _page(subject, events, limit)


def administrative_history(
    access: AccessContext, subject: Subject, cursor: str | None, limit: int
) -> dict[str, Any]:
    """One stable keyset page. Reads have no command, audit row or mutation."""
    after = _decode_cursor(cursor, subject)
    if subject.audit_keys:
        return _access_history(access, subject, after, limit)
    grants = _granted_fields(access, subject)
    capability_query = _capability_query(access, subject)
    master_query = _master_query(access, subject)
    site_master_query = _site_master_query(access, subject)
    capability_rows = _history_rows(capability_query, after, limit)
    master_rows = _history_rows(master_query, after, limit)
    site_master_rows = _history_rows(site_master_query, after, limit)
    audit_rows = _history_rows(
        _audit_query(access, subject, [capability_query, master_query, site_master_query]),
        after,
        limit,
    )
    all_master_rows = [*master_rows, *site_master_rows]
    actors = _actor_visibility(access, [*audit_rows, *all_master_rows, *capability_rows])
    previous_payloads = _previous_master_payloads(access, all_master_rows)
    events = [
        *_audit_events(audit_rows, subject, grants, actors),
        *_master_version_events(master_rows, subject, grants, actors, previous_payloads),
        *_site_master_events(site_master_rows, subject, grants, actors, previous_payloads),
        *_site_capability_events(capability_rows, subject, grants, actors),
    ]
    return _page(subject, events, limit)
