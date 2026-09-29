"""Goods-v1 masters endpoints: organisation, sites, locations, readiness and
configuration (design §6, E006-E020, E026-E040, E066-E069, E085-E089, E215-E225).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db import IntegrityError
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.actions import ACTIONS, TENANT_MASTER_READ_ACTIONS
from accounts.goods_api import (
    LIST_QUERY_KEYS,
    PAGE_PARAMETERS,
    GoodsAPIView,
    business_body,
    check_query,
    check_reviewed_hash,
    page,
    paginate,
    parse_int_id,
    parse_meta,
    parse_uuid,
    resource_dto,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun, LockRank, database_now
from core.refusals import Refusal, issue
from masters.goods_models import (
    ConfigDraft,
    ConfigVersion,
    EffectiveVersionPeriod,
    Location,
    Sbu,
    SiteCapabilityEvent,
    SiteGuard,
    Tenant,
)
from masters.goods_services import (
    APPROVAL_ACTIONS,
    BRAND_FIELDS,
    ENTITY_FIELDS,
    PERMITTED_OPERATIONS,
    REGISTRATION_FIELDS,
    SEASON_FIELDS,
    SITE_FIELDS,
    SITE_TYPES,
    SUBBRAND_FIELDS,
    append_master_version,
    apply_readiness_approval,
    backdate_impact,
    bump_revision,
    closure_items,
    compute_readiness_checks,
    config_draft_data,
    config_draft_hash,
    config_scope_key,
    config_subject_key,
    current_revision,
    ensure_site_sbus,
    ensure_system_locations,
    is_backdated,
    latest_master_version,
    latest_versions_by_kind,
    location_holds_stock,
    lock_revision,
    master_state,
    match_decisions,
    parse_day,
    parse_decisions,
    parse_dt,
    readiness_dto,
    readiness_residuals,
    site_guard_for,
    start_of_today,
    start_revision,
    sync_setup_exceptions,
    trading_not_excluded,
    validate_config_payload,
    validate_gstin_number,
)
from masters.models import Brand, Gstin, LegalEntity, Season, Store

# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def _rid(result: CommandResult) -> str:
    """A just-committed command's resource id - always set by our own handlers."""
    assert result.resource_id is not None
    return result.resource_id


def _human(run: CommandRun) -> uuid.UUID:
    """The acting human - our step-up-gated actions never run as a bare service."""
    assert run.principal.human_id is not None
    return run.principal.human_id


def _reason_and_effective(body: dict[str, Any]) -> tuple[str, Any]:
    reason = body.get("reason_code")
    if not reason:
        raise Refusal(
            "INVALID_REQUEST",
            "reason_code is required.",
            issues=[issue("REQUIRED", "reason_code is required", field="reason_code")],
        )
    effective = parse_dt(body.get("effective_at"), "effective_at", required=False) or timezone.now()
    return str(reason), effective


def _require_one_field(body: dict[str, Any]) -> None:
    if not body:
        raise Refusal("INVALID_REQUEST", "At least one field is required.")


def _entity_scope(access: AccessContext, action: str) -> set[int] | None:
    """Entities where ``action`` is granted; ``None`` means every entity of the tenant."""
    entities: set[int] = set()
    for grant in access.grants:
        if action not in grant.actions:
            continue
        if grant.scope_kind == "tenant":
            return None
        if grant.scope_kind == "entity" and grant.entity_id is not None:
            entities.add(grant.entity_id)
    return entities


def _require_entity(access: AccessContext, action: str, entity_id: int | None) -> None:
    """ACTION_DENIED when never granted; NOT_FOUND for an entity outside the grant's scope."""
    if not any(action in grant.actions for grant in access.grants):
        raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
    entities = _entity_scope(access, action)
    if entities is not None and entity_id not in entities:
        raise Refusal("NOT_FOUND", "That record was not found.")


def _master_brand_scope(access: AccessContext) -> set[int] | None:
    """Brands the caller's master-read grants cover; ``None`` means every brand."""
    brands: set[int] = set()
    for grant in access.grants:
        if not grant.actions & TENANT_MASTER_READ_ACTIONS:
            continue
        if grant.scope_kind == "brand" and grant.brand_id is not None:
            brands.add(grant.brand_id)
        elif grant.scope_kind == "sbu" and grant.sbu_brand_id is not None:
            brands.add(grant.sbu_brand_id)
        else:
            return None
    return brands


#: The one refusal envelope every goods-v1 route answers with (design §6.1),
#: repeated here (rather than imported) so this module documents its own
#: contract independently of ptmapper's.
REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
    },
}


def _resource_response(
    data_schema: dict[str, Any], description: str = "ResourceDTO<data>."
) -> dict[str, Any]:
    """The `ResourceDTO<...>` envelope (design §6.1) around a per-endpoint ``data``
    schema, spelled out for drf-spectacular: these views hand-build dict
    responses rather than run through a serializer, so nothing is introspectable
    without this (ticket 02's "every new or changed operation documents its
    responses").
    """
    return {
        "type": "object",
        "description": description,
        "properties": {
            "id": {"type": "string"},
            "record_contract": {"type": "string", "enum": ["goods-v1"]},
            "revision": {"type": "integer"},
            "content_hash": {"type": "string"},
            "state": {"type": "string"},
            "number": {"type": "string", "nullable": True},
            "version": {"type": "integer", "nullable": True},
            "context": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string", "nullable": True},
                    "entity_id": {"type": "string", "nullable": True},
                    "brand_id": {"type": "string", "nullable": True},
                    "sbu_id": {"type": "string", "nullable": True},
                },
            },
            "data": data_schema,
            "allowed_actions": {"type": "array", "items": {"type": "string"}},
        },
    }


#: Page<T> (design §6.1): a cursor-paged list envelope.
def _page_response(item_schema: dict[str, Any], description: str = "Page<T>.") -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "items": {"type": "array", "items": item_schema},
            "next_cursor": {"type": "string", "nullable": True},
        },
    }


ENTITY_DATA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "pan": {"type": "string", "nullable": True},
        "address": {"type": "object", "nullable": True, "additionalProperties": True},
        "books_code": {"type": "string", "nullable": True},
    },
}

REGISTRATION_DATA = {
    "type": "object",
    "properties": {
        "entity_id": {"type": "string"},
        "gstin": {"type": "string"},
        "state_code": {"type": "string"},
        "state_name": {"type": "string"},
        "effective_from": {"type": "string", "format": "date-time", "nullable": True},
    },
}

SITE_DATA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "city": {"type": "string", "nullable": True},
        "state": {"type": "string", "nullable": True},
        "country": {"type": "string"},
        "type": {"type": "string", "enum": sorted(SITE_TYPES)},
        "entity_id": {"type": "string", "nullable": True},
        "registration_id": {"type": "string", "nullable": True},
        "counter_count": {"type": "integer"},
        "partner_ref": {"type": "string", "nullable": True},
        "opening_date": {"type": "string", "format": "date", "nullable": True},
        "linked_warehouse_id": {"type": "string", "nullable": True},
        "permitted_operations": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(PERMITTED_OPERATIONS)},
        },
        "brand_ids": {"type": "array", "items": {"type": "integer"}},
    },
}

LOCATION_DATA = {
    "type": "object",
    "properties": {
        "site_id": {"type": "string"},
        "parent_id": {"type": "string", "nullable": True},
        "name": {"type": "string"},
        "kind": {"type": "string"},
        "system": {"type": "boolean"},
    },
}

SBU_ITEM = {
    "type": "object",
    "properties": {
        "site_id": {"type": "string"},
        "brand_id": {"type": "string", "nullable": True},
        "code": {"type": "string"},
        "retired_at": {"type": "string", "format": "date-time", "nullable": True},
    },
}

TENANT_DATA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "timezone": {"type": "string"},
        "currency": {"type": "string"},
        "locale": {"type": "string"},
        "business_profile_version_id": {"type": "string", "nullable": True},
    },
}

#: ReadinessDTO (design §5.8): two independent checklists live inside one
#: `checks` array (each item's `key` names which); `residuals` is the closure
#: view — numbers or null, never an invented zero (GSA-T02).
READINESS_DATA = {
    "type": "object",
    "properties": {
        "site_id": {"type": "string"},
        "lifecycle": {"type": "string"},
        "opening_setup_ready": {"type": "boolean"},
        "goods_ready": {"type": "boolean"},
        "sell_ready": {"type": "boolean"},
        "non_trading_confirmed": {"type": "boolean"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "required": {"type": "boolean"},
                    "overridable": {"type": "boolean"},
                    "reason": {"type": "string", "nullable": True},
                    "decision": {"type": "object", "nullable": True, "additionalProperties": True},
                },
            },
        },
        "residuals": {
            "type": "object",
            "properties": {
                "physical_qty": {"type": "integer", "nullable": True},
                "transit_qty": {"type": "integer", "nullable": True},
                "unvalued_qty": {"type": "integer", "nullable": True},
                "reserved_qty": {"type": "integer", "nullable": True},
                "open_exception_ids": {"type": "array", "items": {"type": "string"}},
                "completeness": {"type": "string", "enum": ["complete", "partial", "unknown"]},
                "reasons": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "cash_assessment": {"type": "string", "enum": ["not_assessed"]},
            },
        },
    },
}


def _require_master_read(access: AccessContext) -> set[int] | None:
    """The authenticated read grant for tenant masters (E026/E031/E036 and details)."""
    if not access.all_actions() & TENANT_MASTER_READ_ACTIONS:
        raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
    return _master_brand_scope(access)


def _parent_brand(payload: dict[str, Any] | None) -> int | None:
    raw = (payload or {}).get("parent_id")
    return int(str(raw)) if raw not in (None, "") and str(raw).isdigit() else None


class _MasterCRUD:
    """Shared plumbing for legacy-anchored masters (entity/registration/site/brand/season)."""

    kind: str
    family: str
    fields: frozenset[str]

    def dto(
        self, tenant_id: uuid.UUID, target_key: str, fallback: dict[str, Any]
    ) -> dict[str, Any]:
        latest = latest_master_version(tenant_id, self.kind, target_key)
        if latest is not None:
            return {
                "payload": latest.payload,
                "state": master_state(latest),
                "revision": latest.revision,
            }
        return {
            "payload": fallback,
            "state": "active",
            "revision": current_revision(self.family, target_key) or 1,
        }


# ==========================================================================
# Entities (E006-E010)
# ==========================================================================


def _entity_fallback(entity: LegalEntity) -> dict[str, Any]:
    return {
        "code": entity.code,
        "name": entity.name,
        "pan": entity.pan,
        "address": None,
        "books_code": None,
    }


def _refuse_taken_pan(pan: str, *, exclude_pk: int | None) -> None:
    """A nonblank PAN identifies one legal entity in the tenant (change PRD §14.2).

    Checked before writing: a unique-key error inside the command's transaction
    would poison it and surface as an internal error instead of this conflict.
    Row-level security scopes the lookup to the bound tenant.
    """
    if not pan:
        return
    clash = LegalEntity.objects.filter(pan=pan)
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    if clash.exists():
        raise Refusal("MASTER_CONFLICT", "Another legal entity already uses that PAN.")


#: Design E006 step 5: a buyer preparing a booking needs to *choose* the legal
#: entity, so they may read the reference fields that let a person recognise one
#: - and nothing else. Registration, tax, address, banking, configuration and
#: history stay behind `org.entity.manage` (GSA-T05).
BOOKING_ENTITY_ACTION = "booking.manage"


def _booking_entity_scope(access: AccessContext) -> set[int] | None:
    """Entities a booking grant narrows to; ``None`` when it narrows to none.

    Only an entity-scoped grant names entities. A tenant-, site-, brand- or
    SBU-scoped `booking.manage` says nothing about which legal entities may be
    booked for, so it narrows nothing here and the caller sees every active one.
    """
    entities: set[int] = set()
    for grant in access.grants:
        if BOOKING_ENTITY_ACTION not in grant.actions:
            continue
        if grant.scope_kind != "entity" or grant.entity_id is None:
            return None
        entities.add(grant.entity_id)
    return entities


#: What a buyer's narrow entity read answers. Ticket 08A could not emit this -
#: `/masters/entities` was path-shadowed by the legacy list through the old
#: contract dispatcher, and a path holds one operation per method (#303). The
#: goods read now has its own path, so `GET /api/goods-v1/masters/entities`
#: describes both shapes: this one for a buyer, `ENTITY_DATA` for an entity
#: administrator.
BOOKING_ENTITY_DATA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The reference fields of a legal entity a buyer may book for (E006 step "
        "5): enough to recognise one, and nothing more. Registration, tax, "
        "address, banking and history stay behind org.entity.manage."
    ),
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "state": {"type": "string", "enum": ["active", "inactive"]},
    },
}


#: `MasterPayload.brand` and `MasterPayload.season` share one shape; the two
#: master families differ only in which model backs them.
BRAND_LIKE_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "parent_id": {"type": "integer", "nullable": True},
        # Seasons only: the one explicit "unknown historical season" (OPS-03).
        "historical_unknown": {"type": "boolean"},
    },
}

#: `MasterPayload.subbrand` — a `MasterVersion`-only master with no legacy row.
SUBBRAND_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "parent_id": {"type": "integer", "nullable": True},
    },
}

#: One row of E066's routing directory: identity only, never operating detail.
ROUTING_LOCATION_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "code": {"type": "string"},
        "name": {"type": "string"},
        "type": {"type": "string"},
        "registration_classification": {"type": "string", "nullable": True},
    },
}

#: E215's counts. Only the kinds the caller could actually list appear, so the
#: array is short for a narrow grant rather than padded with zeroes.
SUMMARY_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "Scoped master counts (E215).",
    "properties": {
        "counts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["entities", "registrations", "sites", "brands", "seasons"],
                    },
                    "count": {"type": "integer"},
                },
            },
        },
        "as_of": {"type": "string", "format": "date-time"},
    },
}

#: E222-E225's actor and approval policy payload. `state` is `unset` when no
#: draft or version exists yet, and the id is then a stable name-derived UUID
#: rather than a row that was invented to answer the read.
POLICY_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "roles": {"type": "array", "items": {"type": "string"}},
        "purpose": {"type": "string", "nullable": True},
        "site_ids": {"type": "array", "items": {"type": "integer"}},
        "brand_ids": {"type": "array", "items": {"type": "integer"}},
        "require_distinct": {"type": "boolean"},
        "qty_max": {"type": "integer", "nullable": True},
        "value_max": {"type": "string", "nullable": True},
        "step_up": {"type": "boolean"},
        "unknown_value": {"type": "string", "nullable": True},
    },
}

#: `GET /masters/entities` answers one of two shapes, decided by the caller's own
#: grant rather than by anything in the request: an entity administrator sees the
#: full master payload, a buyer sees the reference fields alone (GSA-T05).
ENTITY_READ_DATA: dict[str, Any] = {"anyOf": [ENTITY_DATA, BOOKING_ENTITY_DATA]}


class GoodsEntityListCreateView(GoodsAPIView):
    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status", str, enum=["active", "retired"], description="Which entity state to list."
            ),
            OpenApiParameter(
                "q",
                str,
                required=False,
                description="Case-insensitive substring match against code or name (GSA-T05).",
            ),
            *PAGE_PARAMETERS,
        ],
        responses={
            200: _page_response(
                _resource_response(ENTITY_READ_DATA, "ResourceDTO<MasterPayload.entity>."),
                "Page<ResourceDTO<MasterPayload.entity>>.",
            ),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        manages = access.holds("org.entity.manage")
        reviews_history = access.holds("audit.view")
        if not manages and not reviews_history and not access.holds(BOOKING_ENTITY_ACTION):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        params = check_query(request, {"q", "cursor", "limit", "status"})
        status = params.get("status", "active")
        if status not in {"active", "retired"}:
            raise Refusal("INVALID_REQUEST", "status must be active or retired.")
        rows = list(LegalEntity.objects.filter(is_active=status == "active").order_by("code"))
        # `_entity_scope` narrows only for a tenant- or entity-scoped grant. A
        # buyer's `booking.manage` is commonly granted at brand or site scope,
        # which narrows no entity at all - answering an empty list there would
        # leave them unable to book anything.
        entities = (
            _entity_scope(access, "org.entity.manage")
            if manages
            else _entity_scope(access, "audit.view")
            if reviews_history
            else _booking_entity_scope(access)
        )
        if entities is not None:
            rows = [row for row in rows if row.pk in entities]
        # A growing reference list is searched and paged, not raised to a bigger
        # single page and hoped over (GSA-T05).
        query = (params.get("q") or "").strip().casefold()
        if query:
            rows = [
                row for row in rows if query in row.code.casefold() or query in row.name.casefold()
            ]
        window, cursor = paginate(rows, params)
        if not manages:
            return Response(
                page(
                    [
                        resource_dto(
                            id=entity.pk,
                            data={
                                "code": entity.code,
                                "name": entity.name,
                                "state": "active" if entity.is_active else "inactive",
                            },
                            revision=1,
                            state="active" if entity.is_active else "inactive",
                            context={"entity_id": entity.pk},
                        )
                        for entity in window
                    ],
                    cursor,
                )
            )
        items = []
        for entity in window:
            info = _MasterCRUD()
            info.kind, info.family = "entity", "master:entity"
            d = info.dto(access.tenant_id, str(entity.pk), _entity_fallback(entity))
            items.append(
                resource_dto(
                    id=entity.pk,
                    data=d["payload"],
                    revision=d["revision"],
                    state=d["state"],
                    context={},
                )
            )
        return Response(page(items, cursor))

    @extend_schema(
        responses={
            201: _resource_response(ENTITY_DATA, "ResourceDTO<MasterPayload.entity>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, ENTITY_FIELDS, required=["code", "name"])
        access.require_action("org.entity.manage")
        if _entity_scope(access, "org.entity.manage") is not None:
            raise Refusal(
                "ACTION_DENIED", "Only a tenant-wide administrator can create a legal entity."
            )

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            if LegalEntity.objects.filter(code=body["code"]).exists():
                raise Refusal(
                    "MASTER_CONFLICT", f"An entity with code {body['code']} already exists."
                )
            _refuse_taken_pan(str(body.get("pan") or ""), exclude_pk=None)
            try:
                entity = LegalEntity.objects.create(
                    code=str(body["code"]), name=str(body["name"]), pan=str(body.get("pan") or "")
                )
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That entity code is already in use.") from exc
            run.audit_subject_key = f"entity:{entity.pk}"
            start_revision(run.tenant_id, "master:entity", str(entity.pk))
            append_master_version(
                run, kind="entity", target_key=str(entity.pk), revision=1, payload=body
            )
            run.audit_after = [{"field": "code", "redacted": False, "value": body["code"]}]
            return CommandResult(
                resource_type="entity", resource_id=str(entity.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="org.entity.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:entity",
        )
        entity = LegalEntity.objects.get(pk=_rid(result))
        info = _MasterCRUD()
        info.kind, info.family = "entity", "master:entity"
        d = info.dto(access.tenant_id, str(entity.pk), _entity_fallback(entity))
        return Response(
            resource_dto(
                id=entity.pk,
                data=d["payload"],
                revision=d["revision"],
                state=d["state"],
                context={},
            ),
            status=result.status_code,
        )


class GoodsEntityDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_masters_entities_detail",
        responses={
            200: _resource_response(ENTITY_DATA, "ResourceDTO<MasterPayload.entity>."),
            401: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        _require_entity(access, "org.entity.manage", pk)
        entity = LegalEntity.objects.filter(pk=pk).first()
        if entity is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        info = _MasterCRUD()
        info.kind, info.family = "entity", "master:entity"
        d = info.dto(access.tenant_id, str(entity.pk), _entity_fallback(entity))
        return Response(
            resource_dto(
                id=entity.pk,
                data=d["payload"],
                revision=d["revision"],
                state=d["state"],
                context={},
            )
        )

    @extend_schema(
        responses={
            200: _resource_response(ENTITY_DATA, "ResourceDTO<MasterPayload.entity>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, ENTITY_FIELDS)
        _require_one_field(body)
        _require_entity(access, "org.entity.manage", pk)
        entity = LegalEntity.objects.filter(pk=pk).first()
        if entity is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        def handler(run: CommandRun) -> CommandResult:
            # History reads a refused attempt under its own entity (ticket 02C).
            run.audit_subject_key = f"entity:{pk}"
            access.require_step_up()
            head = lock_revision(run, "master:entity", str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            latest = latest_master_version(run.tenant_id, "entity", str(pk))
            merged = {**(latest.payload if latest else _entity_fallback(entity)), **body}
            if not merged.get("code") or not merged.get("name"):
                raise Refusal("MASTER_INVALID", "code and name are required.")
            entity.code = str(merged["code"])
            entity.name = str(merged["name"])
            entity.pan = str(merged.get("pan") or "")
            _refuse_taken_pan(entity.pan, exclude_pk=entity.pk)
            try:
                entity.save(update_fields=["code", "name", "pan"])
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That entity code is already in use.") from exc
            revision = bump_revision(head)
            append_master_version(
                run, kind="entity", target_key=str(pk), revision=revision, payload=merged
            )
            return CommandResult(resource_type="entity", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.entity.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:entity",
        )
        entity.refresh_from_db()
        info = _MasterCRUD()
        info.kind, info.family = "entity", "master:entity"
        d = info.dto(access.tenant_id, str(entity.pk), _entity_fallback(entity))
        return Response(
            resource_dto(
                id=entity.pk,
                data=d["payload"],
                revision=d["revision"],
                state=d["state"],
                context={},
            ),
            status=result.status_code,
        )


class GoodsEntityRetireView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(ENTITY_DATA, "ResourceDTO<MasterPayload.entity>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason_code", "effective_at"})
        reason_code, effective_at = _reason_and_effective(body)
        _require_entity(access, "master.retire", pk)
        entity = LegalEntity.objects.filter(pk=pk).first()
        if entity is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        def handler(run: CommandRun) -> CommandResult:
            # History reads a refused attempt under its own entity (ticket 02C).
            run.audit_subject_key = f"entity:{pk}"
            access.require_step_up()
            head = lock_revision(run, "master:entity", str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            if Store.objects.filter(gstin__legal_entity_id=pk, is_active=True).exists():
                raise Refusal("RETIREMENT_BLOCKED", "This entity still has active sites.")
            entity.is_active = False
            entity.save(update_fields=["is_active"])
            latest = latest_master_version(run.tenant_id, "entity", str(pk))
            payload = latest.payload if latest else _entity_fallback(entity)
            revision = bump_revision(head)
            append_master_version(
                run,
                kind="entity",
                target_key=str(pk),
                revision=revision,
                payload=payload,
                effective_from=effective_at,
                retired=True,
                reason_code=reason_code,
            )
            return CommandResult(resource_type="entity", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.entity.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:entity",
        )
        entity.refresh_from_db()
        info = _MasterCRUD()
        info.kind, info.family = "entity", "master:entity"
        d = info.dto(access.tenant_id, str(entity.pk), _entity_fallback(entity))
        return Response(
            resource_dto(
                id=entity.pk,
                data=d["payload"],
                revision=d["revision"],
                state=d["state"],
                context={},
            ),
            status=result.status_code,
        )


# ==========================================================================
# Registrations / GSTINs (E011-E015)
# ==========================================================================


def _registration_fallback(g: Gstin) -> dict[str, Any]:
    return {
        "entity_id": str(g.legal_entity_id),
        "gstin": g.gstin,
        "state_code": g.state_code,
        "state_name": g.state_name,
        "effective_from": None,
    }


class GoodsRegistrationListCreateView(GoodsAPIView):
    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status",
                str,
                enum=["active", "retired"],
                description="Which registration state to list.",
            ),
            OpenApiParameter(
                "entity_id",
                int,
                required=False,
                description="Only registrations belonging to this legal entity.",
            ),
            OpenApiParameter(
                "q",
                str,
                required=False,
                description=(
                    "Case-insensitive substring match against the GSTIN, its state "
                    "name/code, and its legal entity's code/name (GSA-T05)."
                ),
            ),
            *PAGE_PARAMETERS,
        ],
        responses={
            200: _page_response(
                _resource_response(REGISTRATION_DATA, "ResourceDTO<MasterPayload.registration>."),
                "Page<ResourceDTO<MasterPayload.registration>>.",
            ),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        manages = access.holds("org.entity.manage")
        reviews_history = access.holds("audit.view")
        if not manages and not reviews_history:
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        params = check_query(request, {"q", "cursor", "limit", "status", "entity_id"})
        status = params.get("status", "active")
        if status not in {"active", "retired"}:
            raise Refusal("INVALID_REQUEST", "status must be active or retired.")
        rows = list(
            Gstin.objects.select_related("legal_entity")
            .filter(is_active=status == "active")
            .order_by("gstin")
        )
        entities = _entity_scope(access, "org.entity.manage" if manages else "audit.view")
        if entities is not None:
            rows = [row for row in rows if row.legal_entity_id in entities]
        if params.get("entity_id"):
            entity_filter = parse_int_id(params["entity_id"], "entity_id")
            rows = [row for row in rows if row.legal_entity_id == entity_filter]
        # A growing reference list is searched and paged, not raised to a bigger
        # single page and hoped over (GSA-T05, ticket 02D).
        query = (params.get("q") or "").strip().casefold()
        if query:
            rows = [
                row
                for row in rows
                if query in row.gstin.casefold()
                or query in row.state_name.casefold()
                or query in row.state_code.casefold()
                or query in row.legal_entity.code.casefold()
                or query in row.legal_entity.name.casefold()
            ]
        window, cursor = paginate(rows, params)
        items = []
        for g in window:
            d = _MasterCRUD()
            d.kind, d.family = "registration", "master:registration"
            dto = d.dto(access.tenant_id, str(g.pk), _registration_fallback(g))
            items.append(
                resource_dto(
                    id=g.pk,
                    data=dto["payload"],
                    revision=dto["revision"],
                    state=dto["state"],
                    context={"entity_id": g.legal_entity_id},
                )
            )
        return Response(page(items, cursor))

    @extend_schema(
        responses={
            201: _resource_response(REGISTRATION_DATA, "ResourceDTO<MasterPayload.registration>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, REGISTRATION_FIELDS, required=list(REGISTRATION_FIELDS))
        _require_entity(access, "org.entity.manage", parse_int_id(body["entity_id"], "entity_id"))

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            entity = LegalEntity.objects.filter(
                pk=parse_int_id(body["entity_id"], "entity_id")
            ).first()
            if entity is None:
                raise Refusal("MASTER_INVALID", "entity_id does not name a known entity.")
            validate_gstin_number(str(body["gstin"]), str(body["state_code"]))
            if Gstin.objects.filter(gstin=str(body["gstin"]).upper()).exists():
                raise Refusal("MASTER_CONFLICT", "That GSTIN is already registered.")
            try:
                g = Gstin.objects.create(
                    legal_entity=entity,
                    gstin=str(body["gstin"]).upper(),
                    state_code=str(body["state_code"]),
                    state_name=str(body["state_name"]),
                )
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That GSTIN is already registered.") from exc
            run.audit_subject_key = f"registration:{g.pk}"
            start_revision(run.tenant_id, "master:registration", str(g.pk))
            append_master_version(
                run, kind="registration", target_key=str(g.pk), revision=1, payload=body
            )
            return CommandResult(
                resource_type="registration", resource_id=str(g.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="org.entity.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:registration",
        )
        g = Gstin.objects.select_related("legal_entity").get(pk=_rid(result))
        d = _MasterCRUD()
        d.kind, d.family = "registration", "master:registration"
        dto = d.dto(access.tenant_id, str(g.pk), _registration_fallback(g))
        return Response(
            resource_dto(
                id=g.pk,
                data=dto["payload"],
                revision=dto["revision"],
                state=dto["state"],
                context={"entity_id": g.legal_entity_id},
            ),
            status=result.status_code,
        )


class GoodsRegistrationDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_masters_gstins_detail",
        responses={
            200: _resource_response(REGISTRATION_DATA, "ResourceDTO<MasterPayload.registration>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        g = Gstin.objects.select_related("legal_entity").filter(pk=pk).first()
        _require_entity(access, "org.entity.manage", g.legal_entity_id if g else None)
        if g is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        d = _MasterCRUD()
        d.kind, d.family = "registration", "master:registration"
        dto = d.dto(access.tenant_id, str(g.pk), _registration_fallback(g))
        return Response(
            resource_dto(
                id=g.pk,
                data=dto["payload"],
                revision=dto["revision"],
                state=dto["state"],
                context={"entity_id": g.legal_entity_id},
            )
        )

    @extend_schema(
        responses={
            200: _resource_response(REGISTRATION_DATA, "ResourceDTO<MasterPayload.registration>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, REGISTRATION_FIELDS)
        _require_one_field(body)
        g = Gstin.objects.select_related("legal_entity").filter(pk=pk).first()
        _require_entity(access, "org.entity.manage", g.legal_entity_id if g else None)
        if g is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        if body.get("entity_id") is not None:
            _require_entity(
                access, "org.entity.manage", parse_int_id(body["entity_id"], "entity_id")
            )

        def handler(run: CommandRun) -> CommandResult:
            # History reads a refused attempt under its own registration (ticket 02C).
            run.audit_subject_key = f"registration:{pk}"
            access.require_step_up()
            head = lock_revision(run, "master:registration", str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            latest = latest_master_version(run.tenant_id, "registration", str(pk))
            merged = {**(latest.payload if latest else _registration_fallback(g)), **body}
            validate_gstin_number(str(merged["gstin"]), str(merged["state_code"]))
            g.gstin = str(merged["gstin"]).upper()
            g.state_code = str(merged["state_code"])
            g.state_name = str(merged["state_name"])
            try:
                g.save(update_fields=["gstin", "state_code", "state_name"])
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That GSTIN is already registered.") from exc
            revision = bump_revision(head)
            append_master_version(
                run, kind="registration", target_key=str(pk), revision=revision, payload=merged
            )
            return CommandResult(resource_type="registration", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.entity.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:registration",
        )
        g.refresh_from_db()
        d = _MasterCRUD()
        d.kind, d.family = "registration", "master:registration"
        dto = d.dto(access.tenant_id, str(g.pk), _registration_fallback(g))
        return Response(
            resource_dto(
                id=g.pk,
                data=dto["payload"],
                revision=dto["revision"],
                state=dto["state"],
                context={"entity_id": g.legal_entity_id},
            ),
            status=result.status_code,
        )


class GoodsRegistrationRetireView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(REGISTRATION_DATA, "ResourceDTO<MasterPayload.registration>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason_code", "effective_at"})
        reason_code, effective_at = _reason_and_effective(body)
        g = Gstin.objects.select_related("legal_entity").filter(pk=pk).first()
        _require_entity(access, "master.retire", g.legal_entity_id if g else None)
        if g is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        def handler(run: CommandRun) -> CommandResult:
            # History reads a refused attempt under its own registration (ticket 02C).
            run.audit_subject_key = f"registration:{pk}"
            access.require_step_up()
            head = lock_revision(run, "master:registration", str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            if Store.objects.filter(gstin_id=pk, is_active=True).exists():
                raise Refusal("RETIREMENT_BLOCKED", "This registration still has active sites.")
            g.is_active = False
            g.save(update_fields=["is_active"])
            latest = latest_master_version(run.tenant_id, "registration", str(pk))
            payload = latest.payload if latest else _registration_fallback(g)
            revision = bump_revision(head)
            append_master_version(
                run,
                kind="registration",
                target_key=str(pk),
                revision=revision,
                payload=payload,
                effective_from=effective_at,
                retired=True,
                reason_code=reason_code,
            )
            return CommandResult(resource_type="registration", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.entity.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:registration",
        )
        g.refresh_from_db()
        d = _MasterCRUD()
        d.kind, d.family = "registration", "master:registration"
        dto = d.dto(access.tenant_id, str(g.pk), _registration_fallback(g))
        return Response(
            resource_dto(
                id=g.pk,
                data=dto["payload"],
                revision=dto["revision"],
                state=dto["state"],
                context={"entity_id": g.legal_entity_id},
            ),
            status=result.status_code,
        )


# ==========================================================================
# Sites / Stores (E016-E020)
# ==========================================================================


def _site_fallback(store: Store) -> dict[str, Any]:
    return {
        "code": store.code,
        "name": store.name,
        "aliases": [],
        "city": store.city,
        "state": store.gstin.state_name if store.gstin_id else None,
        "country": "IN",
        "type": store.store_type,
        "entity_id": str(store.gstin.legal_entity_id) if store.gstin_id else None,
        "registration_id": str(store.gstin_id) if store.gstin_id else None,
        "counter_count": 0,
        "partner_ref": None,
        "opening_date": None,
        "linked_warehouse_id": None,
        "permitted_operations": [],
        "brand_ids": [],
    }


def _site_dto_body(access: AccessContext, store: Store) -> dict[str, Any]:
    d = _MasterCRUD()
    d.kind, d.family = "site", "master:site"
    dto = d.dto(access.tenant_id, str(store.pk), _site_fallback(store))
    return resource_dto(
        id=store.pk,
        data=dto["payload"],
        revision=dto["revision"],
        state=dto["state"],
        context={
            "site_id": store.pk,
            "entity_id": store.gstin.legal_entity_id if store.gstin_id else None,
        },
    )


def _validate_site_payload(body: dict[str, Any]) -> tuple[LegalEntity, Gstin]:
    if body.get("type") not in SITE_TYPES:
        raise Refusal(
            "MASTER_INVALID",
            f"type must be one of {sorted(SITE_TYPES)}.",
            issues=[issue("INVALID", "unknown site type", field="type")],
        )
    ops = body.get("permitted_operations") or []
    if not isinstance(ops, list) or any(op not in PERMITTED_OPERATIONS for op in ops):
        raise Refusal("MASTER_INVALID", "permitted_operations names an unknown operation.")
    entity = LegalEntity.objects.filter(pk=parse_int_id(body["entity_id"], "entity_id")).first()
    if entity is None:
        raise Refusal("MASTER_INVALID", "entity_id does not name a known entity.")
    registration = Gstin.objects.filter(
        pk=parse_int_id(body["registration_id"], "registration_id")
    ).first()
    if registration is None or registration.legal_entity_id != entity.pk:
        raise Refusal("MASTER_INVALID", "registration_id must belong to entity_id.")
    return entity, registration


class GoodsSiteListCreateView(GoodsAPIView):
    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status", str, enum=["active", "retired"], description="Which site state to list."
            )
        ],
        responses={
            200: _page_response(
                _resource_response(SITE_DATA, "ResourceDTO<MasterPayload.site>."),
                "Page<ResourceDTO<MasterPayload.site>>.",
            ),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        access.require_action("org.site.manage")
        params = check_query(request, {*LIST_QUERY_KEYS, "status"})
        # A retired site keeps its history (ticket 02C); listing it is the way back in.
        status = params.get("status", "active")
        if status not in {"active", "retired"}:
            raise Refusal("INVALID_REQUEST", "status must be active or retired.")
        rows = list(
            Store.objects.select_related("gstin")
            .filter(
                access.site_filter("org.site.manage", field_name="pk"),
                is_active=status == "active",
            )
            .order_by("code")
        )
        window, cursor = paginate(rows, params)
        return Response(page([_site_dto_body(access, s) for s in window], cursor))

    @extend_schema(
        responses={
            201: _resource_response(SITE_DATA, "ResourceDTO<MasterPayload.site>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            SITE_FIELDS,
            required=["code", "name", "type", "entity_id", "registration_id"],
        )
        _require_entity(access, "org.site.manage", parse_int_id(body["entity_id"], "entity_id"))

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            entity, registration = _validate_site_payload(body)
            if Store.objects.filter(code=body["code"]).exists():
                raise Refusal("MASTER_CONFLICT", f"A site with code {body['code']} already exists.")
            try:
                store = Store.objects.create(
                    code=str(body["code"]),
                    name=str(body["name"]),
                    store_type=str(body["type"])[:12],
                    gstin=registration,
                    city=str(body.get("city") or ""),
                )
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That site code is already in use.") from exc
            start_revision(run.tenant_id, "master:site", str(store.pk))
            append_master_version(
                run, kind="site", target_key=str(store.pk), revision=1, payload=body
            )
            SiteGuard.objects.create(
                tenant_id=run.tenant_id,
                site=store,
                lifecycle=SiteGuard.Lifecycle.PLANNED,
                stock_contract=SiteGuard.StockContract.GOODS_V1,
            )
            ensure_system_locations(run.tenant_id, store)
            brand_ids = [parse_int_id(b, "brand_ids") for b in (body.get("brand_ids") or [])]
            ensure_site_sbus(run.tenant_id, store, brand_ids)
            run.audit_after = [{"field": "code", "redacted": False, "value": body["code"]}]
            return CommandResult(resource_type="site", resource_id=str(store.pk), status_code=201)

        result = self.run_command(
            request,
            access=access,
            action="org.site.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:site",
        )
        store = Store.objects.select_related("gstin").get(pk=_rid(result))
        return Response(_site_dto_body(access, store), status=result.status_code)


class GoodsSiteDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_masters_stores_detail",
        responses={
            200: _resource_response(SITE_DATA, "ResourceDTO<MasterPayload.site>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        access.require("org.site.manage", site_id=pk)
        store = Store.objects.select_related("gstin").filter(pk=pk).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        return Response(_site_dto_body(access, store))

    @extend_schema(
        responses={
            200: _resource_response(SITE_DATA, "ResourceDTO<MasterPayload.site>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, SITE_FIELDS)
        _require_one_field(body)
        access.require("org.site.manage", site_id=pk)
        store = Store.objects.select_related("gstin").filter(pk=pk).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        def handler(run: CommandRun) -> CommandResult:
            head = lock_revision(run, "master:site", str(pk), rank=LockRank.SITE)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            access.require_step_up()
            latest = latest_master_version(run.tenant_id, "site", str(pk))
            merged = {**(latest.payload if latest else _site_fallback(store)), **body}
            entity, _registration = _validate_site_payload(merged)
            _require_entity(access, "org.site.manage", entity.pk)
            store.code = str(merged["code"])
            store.name = str(merged["name"])
            store.store_type = str(merged["type"])[:12]
            store.city = str(merged.get("city") or "")
            try:
                store.save(update_fields=["code", "name", "store_type", "city"])
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That site code is already in use.") from exc
            revision = bump_revision(head)
            append_master_version(
                run, kind="site", target_key=str(pk), revision=revision, payload=merged
            )
            return CommandResult(resource_type="site", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.site.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:site",
            site_id=pk,
        )
        store.refresh_from_db()
        return Response(_site_dto_body(access, store), status=result.status_code)


class GoodsSiteRetireView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(SITE_DATA, "ResourceDTO<MasterPayload.site>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason_code", "effective_at"})
        reason_code, effective_at = _reason_and_effective(body)
        access.require("master.retire", site_id=pk)
        store = Store.objects.select_related("gstin").filter(pk=pk).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            head = lock_revision(run, "master:site", str(pk), rank=LockRank.SITE)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            from stockledger.goods_models import Position

            if Position.objects.filter(site=store).exists():
                raise Refusal("RETIREMENT_BLOCKED", "This site still has stock positions.")
            store.is_active = False
            store.save(update_fields=["is_active"])
            latest = latest_master_version(run.tenant_id, "site", str(pk))
            payload = latest.payload if latest else _site_fallback(store)
            revision = bump_revision(head)
            append_master_version(
                run,
                kind="site",
                target_key=str(pk),
                revision=revision,
                payload=payload,
                effective_from=effective_at,
                retired=True,
                reason_code=reason_code,
            )
            guard = site_guard_for(store)
            if guard is not None:
                guard.lifecycle = SiteGuard.Lifecycle.CLOSED
                guard.save(update_fields=["lifecycle"])
            return CommandResult(resource_type="site", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.site.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:site",
            site_id=pk,
        )
        store.refresh_from_db()
        return Response(_site_dto_body(access, store), status=result.status_code)


# ==========================================================================
# Locations directory (E066) and site locations (E067-E068, E219-E221)
# ==========================================================================


class GoodsLocationDirectoryView(GoodsAPIView):
    """E066: destination routing directory — minimal identity only."""

    @extend_schema(
        responses={
            200: _page_response(ROUTING_LOCATION_ITEM, "Page<RoutingLocationDTO> (E066)."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"source_site_id", "q", "cursor", "limit"})
        source_site_id = params.get("source_site_id")
        if not source_site_id:
            raise Refusal("INVALID_REQUEST", "source_site_id is required.")
        site_id = parse_int_id(source_site_id, "source_site_id")
        # Routing needs only the source site's identity, so any grant reaching it counts.
        access.require_action("org.site.route")
        if not access.can_reach_site("org.site.route", site_id):
            raise Refusal("NOT_FOUND", "That record was not found.")
        q = (params.get("q") or "").strip()
        rows = Store.objects.select_related("gstin").filter(is_active=True).order_by("code")
        if q:
            rows = rows.filter(code__icontains=q)
        items = [
            {
                "id": str(s.pk),
                "code": s.code,
                "name": s.name,
                "type": s.store_type,
                "registration_classification": s.gstin.state_code if s.gstin_id else None,
            }
            for s in rows
        ]
        window, cursor = paginate(items, params)
        return Response(page(window, cursor))


LOCATION_WRITE_ACTIONS = frozenset(
    {"org.location.manage", "org.location.store.manage", "org.location.warehouse_bin.manage"}
)
# A receiver needs the list to put goods away, and a movement drafter needs it to
# choose where goods go (GSA-T12): someone the server will let move stock into a
# location must be able to see which locations that site has.
LOCATION_READ_ACTIONS = frozenset(
    {"org.site.manage", "receive.arrival", "movement.draft", *LOCATION_WRITE_ACTIONS}
)


def _location_authorised(access: AccessContext, site_id: int) -> None:
    """Scoped site reader for location and SBU reads (E067, E218, E219 GET)."""
    if any(access.can_at_store(action, site_id) for action in LOCATION_READ_ACTIONS):
        return
    if not (access.all_actions() & LOCATION_READ_ACTIONS):
        raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
    raise Refusal("NOT_FOUND", "That record was not found.")


def _location_writable(access: AccessContext, store: Store, kinds: list[str]) -> None:
    """Location writes (E068, E220, E221): C-OWN in scope; M-STR at an assigned store;
    C-WHO for bins at an assigned warehouse. A receiving grant alone never writes."""
    held = access.all_actions() & LOCATION_WRITE_ACTIONS
    if not held:
        raise Refusal("ACTION_DENIED", "You do not have permission to change locations.")
    site_id = store.pk
    if access.can_at_store("org.location.manage", site_id):
        return
    if store.store_type == "store" and access.can_at_store("org.location.store.manage", site_id):
        return
    if store.store_type == "warehouse" and access.can_at_store(
        "org.location.warehouse_bin.manage", site_id
    ):
        # Zone -> rack -> bin is a typical warehouse layout (GSA-T02); a bin
        # manager's authority runs to both, never to a whole-site kind change.
        if all(kind in (Location.Kind.BIN, Location.Kind.RACK) for kind in kinds):
            return
        raise Refusal("ACTION_DENIED", "At a warehouse you may only manage bins and racks.")
    if any(access.can_at_store(action, site_id) for action in held):
        raise Refusal("ACTION_DENIED", "Your location authority does not cover this kind of site.")
    raise Refusal("NOT_FOUND", "That record was not found.")


def _location_dto(location: Location) -> dict[str, Any]:
    return resource_dto(
        id=location.pk,
        data={
            "site_id": str(location.site_id),
            "parent_id": str(location.parent_id) if location.parent_id else None,
            "name": location.name,
            "kind": location.kind,
            "system": location.system,
        },
        revision=location.revision,
        state="retired" if location.retired_at else "active",
        context={"site_id": location.site_id},
    )


class GoodsSiteLocationListCreateView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _page_response(
                _resource_response(LOCATION_DATA, "ResourceDTO<MasterPayload.location>."),
                "Page<ResourceDTO<MasterPayload.location>>.",
            ),
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request, site_id: int) -> Response:
        access = self.access(request)
        _location_authorised(access, site_id)
        store = Store.objects.filter(pk=site_id).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        params = check_query(request)
        rows = list(Location.objects.filter(site=store).order_by("kind", "name"))
        window, cursor = paginate(rows, params)
        return Response(page([_location_dto(loc) for loc in window], cursor))

    @extend_schema(
        responses={
            201: _resource_response(LOCATION_DATA, "ResourceDTO<MasterPayload.location>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, site_id: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"parent_id", "name", "kind"}, required=["name", "kind"])
        _location_authorised(access, site_id)
        store = Store.objects.filter(pk=site_id).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        _location_writable(access, store, [str(body["kind"])])

        def handler(run: CommandRun) -> CommandResult:
            site_head = lock_revision(run, "master:site", str(site_id), rank=LockRank.SITE)
            if site_head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this site after you loaded it."
                )
            if body["kind"] not in dict(Location.Kind.choices):
                raise Refusal("LOCATION_INVALID", "kind is not a recognised location kind.")
            parent = None
            if body.get("parent_id"):
                parent = Location.objects.filter(
                    pk=parse_uuid(body["parent_id"], "parent_id"), site=store
                ).first()
                if parent is None:
                    raise Refusal(
                        "LOCATION_INVALID", "parent_id does not name a location at this site."
                    )
            try:
                location = Location.objects.create(
                    tenant_id=run.tenant_id,
                    site=store,
                    parent=parent,
                    name=str(body["name"]),
                    kind=str(body["kind"]),
                    system=False,
                )
            except IntegrityError as exc:
                raise Refusal(
                    "LOCATION_INVALID", "That location name is already in use here."
                ) from exc
            version = append_master_version(
                run, kind="location", target_key=str(location.pk), revision=1, payload=body
            )
            location.active_version = version
            return CommandResult(
                resource_type="location", resource_id=str(location.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="org.site.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"site:{site_id}",
            site_id=site_id,
        )
        location = Location.objects.get(pk=_rid(result))
        return Response(_location_dto(location), status=result.status_code)


class GoodsSiteLocationDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_masters_stores_locations_detail",
        responses={
            200: _resource_response(LOCATION_DATA, "ResourceDTO<MasterPayload.location>."),
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, site_id: int, pk: uuid.UUID) -> Response:
        access = self.access(request)
        _location_authorised(access, site_id)
        location = Location.objects.filter(pk=pk, site_id=site_id).first()
        if location is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        return Response(_location_dto(location))

    @extend_schema(
        responses={
            200: _resource_response(LOCATION_DATA, "ResourceDTO<MasterPayload.location>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, site_id: int, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"parent_id", "name", "kind", "reason_code", "retire"})
        _require_one_field(body)
        _location_authorised(access, site_id)
        location = Location.objects.select_related("site").filter(pk=pk, site_id=site_id).first()
        if location is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        _location_writable(
            access,
            location.site,
            [location.kind, *([str(body["kind"])] if body.get("kind") is not None else [])],
        )

        def handler(run: CommandRun) -> CommandResult:
            locked = run.lock(LockRank.SITE, Location.objects.filter(pk=pk))
            if not locked:
                raise Refusal("NOT_FOUND", "That record was not found.")
            row = locked[0]
            if row.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            wants_retire = bool(body.get("retire"))
            if wants_retire:
                if row.system:
                    raise Refusal("RETIREMENT_BLOCKED", "A system location cannot be retired.")
                if location_holds_stock(row):
                    raise Refusal("RETIREMENT_BLOCKED", "This location still holds stock.")
                row.retired_at = run.now
            if body.get("kind") is not None and body["kind"] not in dict(Location.Kind.choices):
                raise Refusal("LOCATION_INVALID", "kind is not a recognised location kind.")
            if row.system and (
                (body.get("kind") is not None and body["kind"] != row.kind)
                or (
                    "parent_id" in body
                    and body["parent_id"] != (str(row.parent_id) if row.parent_id else None)
                )
            ):
                # GSA-T02: a system location's display name may change; its kind
                # and place in the tree may not - it cannot be moved.
                raise Refusal(
                    "LOCATION_INVALID",
                    "A system location's kind and place in the tree cannot change; "
                    "only its name may be edited.",
                )
            if "name" in body:
                row.name = str(body["name"])
            if "kind" in body:
                row.kind = str(body["kind"])
            if "parent_id" in body:
                parent = None
                if body["parent_id"]:
                    parent = Location.objects.filter(
                        pk=parse_uuid(body["parent_id"], "parent_id"), site_id=site_id
                    ).first()
                    if parent is None:
                        raise Refusal(
                            "LOCATION_INVALID", "parent_id does not name a location at this site."
                        )
                row.parent = parent
            row.revision += 1
            try:
                row.save()
            except IntegrityError as exc:
                raise Refusal(
                    "LOCATION_INVALID", "That location name is already in use here."
                ) from exc
            payload = {
                "site_id": str(row.site_id),
                "parent_id": str(row.parent_id) if row.parent_id else None,
                "name": row.name,
                "kind": row.kind,
            }
            append_master_version(
                run,
                kind="location",
                target_key=str(row.pk),
                revision=row.revision,
                payload=payload,
                retired=wants_retire,
                reason_code=body.get("reason_code"),
            )
            return CommandResult(resource_type="location", resource_id=str(row.pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.site.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"location:{pk}",
            site_id=site_id,
        )
        location.refresh_from_db()
        return Response(_location_dto(location), status=result.status_code)


class GoodsSiteLocationRetireView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(LOCATION_DATA, "ResourceDTO<MasterPayload.location>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, site_id: int, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason_code"}, required=["reason_code"])
        _location_authorised(access, site_id)
        location = Location.objects.select_related("site").filter(pk=pk, site_id=site_id).first()
        if location is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        _location_writable(access, location.site, [location.kind])

        def handler(run: CommandRun) -> CommandResult:
            locked = run.lock(LockRank.SITE, Location.objects.filter(pk=pk))
            if not locked:
                raise Refusal("NOT_FOUND", "That record was not found.")
            row = locked[0]
            if row.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            if row.system:
                raise Refusal("RETIREMENT_BLOCKED", "A system location cannot be retired.")
            if location_holds_stock(row):
                raise Refusal("RETIREMENT_BLOCKED", "This location still holds stock.")
            row.retired_at = run.now
            row.revision += 1
            row.save()
            append_master_version(
                run,
                kind="location",
                target_key=str(row.pk),
                revision=row.revision,
                payload={
                    "site_id": str(row.site_id),
                    "parent_id": str(row.parent_id) if row.parent_id else None,
                    "name": row.name,
                    "kind": row.kind,
                },
                retired=True,
                reason_code=body["reason_code"],
            )
            return CommandResult(resource_type="location", resource_id=str(row.pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="org.site.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"location:{pk}",
            site_id=site_id,
        )
        location.refresh_from_db()
        return Response(_location_dto(location), status=result.status_code)


# ==========================================================================
# Readiness (E069)
# ==========================================================================

READINESS_ACTIONS = frozenset(
    {
        "check",
        "approve_opening_setup",
        "revoke_opening_setup",
        "approve_goods",
        "revoke_goods",
        "confirm_non_trading",
        "start_closing",
        "approve_closed",
    }
)
#: The two that decide the site's residuals rather than fixing its setup.
_CLOSING_ACTIONS = frozenset({"start_closing", "approve_closed"})
_STEP_UP_ACTIONS = frozenset(
    {
        "approve_opening_setup",
        "revoke_opening_setup",
        "approve_goods",
        "revoke_goods",
        "confirm_non_trading",
        "start_closing",
        "approve_closed",
    }
)


class GoodsSiteReadinessView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(READINESS_DATA, "ResourceDTO<ReadinessDTO>."),
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request, pk: int) -> Response:
        """A read-only view of the site's current readiness (design §5.8's
        ReadinessDTO), so a screen can render it - and learn the SiteGuard
        revision an action must quote - without submitting a command. E069
        itself defines only the mutating POST; this GET adds no write path and
        computes checks the same way `action=check` does, just without one."""
        access = self.access(request)
        store = Store.objects.select_related("gstin").filter(pk=pk).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        # Reading readiness needs either authority the POST actions split
        # between: C-STO's plain check or C-OWN's approve (E069's own access
        # note). A reader with neither gets the same NOT_FOUND a wrong site
        # would, matching every other masters read in this module.
        if not any(
            access.can_at_store(action, pk)
            for action in ("org.site.lifecycle.run", "org.site.lifecycle.approve")
        ):
            raise Refusal("NOT_FOUND", "That record was not found.")
        guard = site_guard_for(store)
        if guard is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        checks = compute_readiness_checks(store, database_now())
        return Response(
            resource_dto(
                id=pk,
                data=readiness_dto(store, guard, checks),
                revision=guard.revision,
                state=guard.lifecycle,
                context={"site_id": pk},
            )
        )

    @extend_schema(
        responses={
            200: _resource_response(READINESS_DATA, "ResourceDTO<ReadinessDTO>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {
                "action",
                "reason_code",
                "evidence_id",
                "checks",
                "closure_date",
                "residual_decisions",
            },
            required=["action"],
        )
        action = body["action"]
        if action not in READINESS_ACTIONS:
            raise Refusal("INVALID_REQUEST", f"action must be one of {sorted(READINESS_ACTIONS)}.")
        store = Store.objects.select_related("gstin").filter(pk=pk).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        if action == "check":
            access.require("org.site.lifecycle.run", site_id=pk)
        else:
            access.require("org.site.lifecycle.approve", site_id=pk)

        evidence_id = _site_evidence(access, store, body.get("evidence_id"))

        def handler(run: CommandRun) -> CommandResult:
            if action in _STEP_UP_ACTIONS:
                access.require_step_up()
            guard_rows = run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=pk))
            if not guard_rows:
                raise Refusal("NOT_FOUND", "That record was not found.")
            guard = guard_rows[0]
            if guard.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED",
                    "Someone changed this site's readiness after you loaded it.",
                )
            checks = compute_readiness_checks(store, run.now)
            # GSA-T04: opening the site's setup gaps is where they are discovered,
            # and where they are discovered fixed - from inside this command,
            # never swept for later.
            #
            # Not while closing, though. `start_closing` and `approve_closed`
            # decide every open exception at the site further down this same
            # handler, so raising fresh ones here would refuse the closure over
            # rows the caller could not have seen and cannot have decided. A site
            # being shut down does not need its setup fixed either.
            if action not in _CLOSING_ACTIONS:
                sync_setup_exceptions(run, store, checks)
            if action == "check":
                return CommandResult(
                    resource_type="readiness", resource_id=str(pk), status_code=200
                )
            if action in APPROVAL_ACTIONS:
                # Opening a capability is the one thing the development seed
                # does too, so it lives in one place both go through.
                apply_readiness_approval(
                    run,
                    store=store,
                    guard=guard,
                    action=action,
                    checks=checks,
                    reason_code=body.get("reason_code") or "",
                    residual_decisions=body.get("residual_decisions"),
                    approver_id=_human(run),
                    evidence_id=evidence_id,
                )
                return CommandResult(
                    resource_type="readiness", resource_id=str(pk), status_code=200
                )
            recorded = checks
            if action == "revoke_opening_setup":
                guard.opening_setup_ready = False
            elif action == "revoke_goods":
                guard.goods_ready = False
            elif action == "confirm_non_trading":
                _confirm_non_trading(store, guard, evidence_id)
                guard.non_trading_confirmed = True
            elif action in ("start_closing", "approve_closed"):
                decided = match_decisions(
                    closure_items(
                        readiness_residuals(store), stock_allowed=action == "start_closing"
                    ),
                    parse_decisions(body.get("residual_decisions")),
                    code="CLOSURE_UNRESOLVED",
                    message="Each residual needs its own reasoned owner decision.",
                )
                recorded = checks + [
                    {
                        "key": f"residual:{item['code']}",
                        **{k: v for k, v in item.items() if k != "code"},
                    }
                    for item in decided
                ]
                if action == "start_closing":
                    guard.lifecycle = SiteGuard.Lifecycle.CLOSING
                    guard.closure_date = parse_day(body.get("closure_date"), "closure_date")
                else:
                    guard.lifecycle = SiteGuard.Lifecycle.CLOSED
                    guard.goods_ready = False
            event = run.record(
                SiteCapabilityEvent(
                    site=store,
                    operation=_operation_for(action),
                    outcome="revoked" if action.startswith("revoke") else "approved",
                    site_revision=guard.revision,
                    checks=recorded,
                    reason_code=body.get("reason_code") or "",
                    evidence_id=evidence_id,
                    approver_id=_human(run),
                )
            )
            guard.capability_event = event
            guard.revision += 1
            guard.save()
            return CommandResult(resource_type="readiness", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action=f"org.site.lifecycle.{'run' if action == 'check' else 'approve'}",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"site:{pk}",
            site_id=pk,
        )
        guard = site_guard_for(store)
        assert guard is not None
        checks = compute_readiness_checks(store, database_now())
        return Response(
            resource_dto(
                id=pk,
                data=readiness_dto(store, guard, checks),
                revision=guard.revision,
                state=guard.lifecycle,
                context={"site_id": pk},
            ),
            status=result.status_code,
        )


def _site_evidence(access: AccessContext, store: Store, raw: Any) -> uuid.UUID | None:
    """Evidence cited for a readiness action: readable by the caller and declared for this site."""
    from files.goods_models import EvidenceObject
    from files.goods_services import readable_by

    if not raw:
        return None
    evidence_id = parse_uuid(raw, "evidence_id")
    evidence = EvidenceObject.objects.filter(tenant_id=access.tenant_id, pk=evidence_id).first()
    declared = {str(s) for s in ((evidence.scope or {}) if evidence else {}).get("site_ids") or []}
    if evidence is None or not readable_by(access, evidence) or str(store.pk) not in declared:
        raise Refusal("NOT_FOUND", "That evidence file was not found.")
    return evidence_id


def _confirm_non_trading(store: Store, guard: SiteGuard, evidence_id: uuid.UUID | None) -> None:
    """Affirmative evidence for this site and no known or unknown trading.

    The confirmation proves only that the site does not trade: it never sets selling,
    enrols a device or settles trading history.
    """
    if evidence_id is None:
        raise Refusal("TRADING_NOT_EXCLUDED", "Affirmative non-trading evidence is required.")
    if guard.sell_ready:
        raise Refusal("TRADING_NOT_EXCLUDED", "This site is still sell-ready.")
    problems = trading_not_excluded(store)
    if problems:
        raise Refusal(
            "TRADING_NOT_EXCLUDED",
            "Trading at this site is not ruled out.",
            issues=problems,
        )


def _operation_for(action: str) -> str:
    return {
        "approve_opening_setup": "opening_setup",
        "revoke_opening_setup": "opening_setup",
        "approve_goods": "goods",
        "revoke_goods": "goods",
        "confirm_non_trading": "non_trading",
        "start_closing": "closing",
        "approve_closed": "closed",
    }.get(action, "goods")


# ==========================================================================
# Site SBUs (E218)
# ==========================================================================


def _sbu_dto(access: AccessContext, sbu: Sbu) -> dict[str, Any]:
    """ResourceDTO<Sbu> (design §5.8 allowlist: site_id, brand_id, code, retired_at)."""
    allowed = (
        ["retire"]
        if sbu.retired_at is None
        and access.holds("master.retire")
        and access.can("master.retire", site_id=sbu.site_id, brand_id=sbu.brand_id)
        else []
    )
    return resource_dto(
        id=sbu.pk,
        data={
            "site_id": str(sbu.site_id),
            "brand_id": str(sbu.brand_id) if sbu.brand_id else None,
            "code": sbu.code,
            "retired_at": sbu.retired_at.isoformat() if sbu.retired_at else None,
        },
        revision=sbu.revision,
        state="retired" if sbu.retired_at else "active",
        context={"site_id": sbu.site_id, "brand_id": sbu.brand_id, "sbu_id": sbu.pk},
        allowed_actions=allowed,
    )


class GoodsSiteSbuListView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _page_response(
                _resource_response(SBU_ITEM, "ResourceDTO<Sbu>."), "Page<ResourceDTO<Sbu>>."
            ),
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request, site_id: int) -> Response:
        access = self.access(request)
        _location_authorised(access, site_id)
        store = Store.objects.filter(pk=site_id).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        params = check_query(request)
        rows = list(Sbu.objects.filter(site=store).order_by("code"))
        window, cursor = paginate(rows, params)
        return Response(page([_sbu_dto(access, s) for s in window], cursor))


#: E246 request body: why the SBU is retired and the exact content reviewed.
SBU_RETIRE_REQUEST = {
    "type": "object",
    "required": [
        "command_id",
        "contract_version",
        "expected_revision",
        "reason_code",
        "reviewed_hash",
    ],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer", "minimum": 1},
        "reason_code": {"type": "string", "maxLength": 60},
        "reviewed_hash": {"type": "string", "minLength": 64, "maxLength": 64},
    },
    "additionalProperties": False,
}


class GoodsSiteSbuRetireView(GoodsAPIView):
    """E246: C-OWN retires one site SBU after a fresh password confirmation.

    Refusals: ACTION_DENIED (no retire grant), NOT_FOUND (SBU outside scope, or not
    at that site), INVALID_REQUEST (body), STEP_UP_REQUIRED, REVISION_SUPERSEDED
    (stale expected_revision or reviewed_hash), COMMAND_CONFLICT (a reused command
    id with a different body), SBU_RETIREMENT_BLOCKED (already retired, or any
    residual still refers to it; ``details.issues`` names each one). There is no
    residual override.
    """

    @extend_schema(
        request={"application/json": SBU_RETIRE_REQUEST},
        responses={
            200: _resource_response(SBU_ITEM, "ResourceDTO<Sbu>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        },
    )
    def post(self, request: Request, site_id: int, pk: uuid.UUID) -> Response:
        from masters.goods_sbu import (
            end_sbu_grants,
            lock_grant_holders,
            retirement_blockers,
            sbu_residuals,
        )

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"reason_code", "reviewed_hash"},
            required=["reason_code", "reviewed_hash"],
        )
        reason_code = body["reason_code"]
        if not isinstance(reason_code, str) or len(reason_code) > 60:
            raise Refusal(
                "INVALID_REQUEST",
                "reason_code must be text of at most 60 characters.",
                issues=[issue("INVALID", "reason_code is invalid", field="reason_code")],
            )
        reviewed_hash = body["reviewed_hash"]
        if not isinstance(reviewed_hash, str) or len(reviewed_hash) != 64:
            raise Refusal(
                "INVALID_REQUEST",
                "reviewed_hash must be the 64-character content hash you reviewed.",
                issues=[issue("INVALID", "reviewed_hash is invalid", field="reviewed_hash")],
            )
        _location_authorised(access, site_id)
        sbu = Sbu.objects.filter(pk=pk, site_id=site_id).first()
        if sbu is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        access.require("master.retire", site_id=site_id, brand_id=sbu.brand_id)

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            # The holders of grants scoped to this SBU, at the security rank below
            # the site: their grants end with the retirement, as any access change.
            lock_grant_holders(run, pk)
            # Then the site guard: receiving, acceptance, movement and approval
            # commands lock it too, and writing it below makes any stock or
            # document change that committed meanwhile a serialization conflict,
            # so the residuals read here cannot be overtaken before this commits.
            guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site_id))
            locked = run.lock(LockRank.SITE, Sbu.objects.filter(pk=pk, site_id=site_id))
            if not locked:
                raise Refusal("NOT_FOUND", "That record was not found.")
            row = locked[0]
            if row.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            check_reviewed_hash(reviewed_hash, _sbu_dto(access, row)["content_hash"])
            if row.retired_at is not None:
                raise Refusal(
                    "SBU_RETIREMENT_BLOCKED", "This business unit is already retired.", status=409
                )
            blockers = retirement_blockers(sbu_residuals(row))
            if blockers:
                raise Refusal(
                    "SBU_RETIREMENT_BLOCKED",
                    "This business unit still has stock, documents or exceptions that must "
                    "be handled first.",
                    status=409,
                    issues=blockers,
                )
            row.retired_at = run.now
            row.revision += 1
            row.save(update_fields=["retired_at", "revision"])
            # Change PRD P5: grants scoped to the unit close rather than block.
            grants_ended = end_sbu_grants(run, row)
            for guard in guards:
                guard.revision += 1
                guard.save(update_fields=["revision"])
            append_master_version(
                run,
                kind="sbu",
                target_key=str(row.pk),
                revision=row.revision,
                payload={
                    "site_id": str(row.site_id),
                    "brand_id": str(row.brand_id) if row.brand_id else None,
                    "code": row.code,
                    "grants_ended": grants_ended,
                },
                retired=True,
                reason_code=reason_code,
            )
            run.audit_after = {
                "sbu_id": str(row.pk),
                "retired_at": row.retired_at.isoformat(),
                "reason_code": reason_code,
                "grants_ended": grants_ended,
            }
            return CommandResult(resource_type="sbu", resource_id=str(row.pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="master.retire",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"sbu:{pk}",
            site_id=site_id,
            reviewed_hash=reviewed_hash,
        )
        sbu.refresh_from_db()
        return Response(_sbu_dto(access, sbu), status=result.status_code)


# ==========================================================================
# Brands / Seasons / Subbrands (E026-E040)
# ==========================================================================


def _brand_season_fallback(row: Any) -> dict[str, Any]:
    return {"code": row.code, "name": row.name, "parent_id": None}


def _brand_season_payload(row: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """A season answers whether it is *the* unknown historical season (OPS-03).

    Read straight off the row rather than out of a master version's payload:
    the flag is the database's own fact, set once by the foundation seed and
    kept single by its constraint, not a field an editor may type. Screens need
    it so they can offer and label the season by meaning rather than by code.
    """
    if isinstance(row, Season):
        return {**payload, "historical_unknown": bool(row.historical_unknown)}
    return payload


class _BrandLikeListCreateView(GoodsAPIView):
    kind: str
    family: str
    model: Any
    fields: frozenset[str]
    brand_scoped: bool = False

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "q",
                str,
                required=False,
                description="Case-insensitive substring match against code or name (GSA-T05).",
            ),
            *PAGE_PARAMETERS,
        ],
        responses={
            200: _page_response(
                _resource_response(BRAND_LIKE_DATA, "ResourceDTO<MasterPayload.brand|season>."),
                "Page<ResourceDTO<MasterPayload.brand|season>>.",
            ),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        brands = _require_master_read(access)
        params = check_query(request)
        rows = list(self.model.objects.all().order_by("code"))
        if self.brand_scoped and brands is not None:
            rows = [row for row in rows if row.pk in brands]
        # A growing reference list is searched and paged, not raised to a bigger
        # single page and hoped over (GSA-T05, ticket 02D).
        query = (params.get("q") or "").strip().casefold()
        if query:
            rows = [
                row for row in rows if query in row.code.casefold() or query in row.name.casefold()
            ]
        window, cursor = paginate(rows, params)
        items = []
        for row in window:
            d = _MasterCRUD()
            d.kind, d.family = self.kind, self.family
            dto = d.dto(access.tenant_id, str(row.pk), _brand_season_fallback(row))
            items.append(
                resource_dto(
                    id=row.pk,
                    data=_brand_season_payload(row, dto["payload"]),
                    revision=dto["revision"],
                    state=dto["state"],
                    context={},
                )
            )
        return Response(page(items, cursor))

    @extend_schema(
        responses={
            201: _resource_response(BRAND_LIKE_DATA, "ResourceDTO<MasterPayload.brand|season>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, self.fields, required=["code", "name"])
        access.require("vendor.manage")

        def handler(run: CommandRun) -> CommandResult:
            if self.model.objects.filter(code=body["code"]).exists():
                raise Refusal(
                    "MASTER_CONFLICT", f"A {self.kind} with code {body['code']} already exists."
                )
            if body.get("parent_id"):
                if not Brand.objects.filter(
                    pk=parse_int_id(body["parent_id"], "parent_id")
                ).exists():
                    raise Refusal("MASTER_INVALID", "parent_id does not name a known brand.")
            try:
                row = self.model.objects.create(code=str(body["code"]), name=str(body["name"]))
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That code is already in use.") from exc
            start_revision(run.tenant_id, self.family, str(row.pk))
            append_master_version(
                run, kind=self.kind, target_key=str(row.pk), revision=1, payload=body
            )
            return CommandResult(resource_type=self.kind, resource_id=str(row.pk), status_code=201)

        result = self.run_command(
            request,
            access=access,
            action="vendor.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=self.family,
        )
        row = self.model.objects.get(pk=_rid(result))
        d = _MasterCRUD()
        d.kind, d.family = self.kind, self.family
        dto = d.dto(access.tenant_id, str(row.pk), _brand_season_fallback(row))
        return Response(
            resource_dto(
                id=row.pk,
                data=_brand_season_payload(row, dto["payload"]),
                revision=dto["revision"],
                state=dto["state"],
                context={},
            ),
            status=result.status_code,
        )


class GoodsBrandListCreateView(_BrandLikeListCreateView):
    kind, family, model, fields = "brand", "master:brand", Brand, BRAND_FIELDS
    brand_scoped = True


class GoodsSeasonListCreateView(_BrandLikeListCreateView):
    kind, family, model, fields = "season", "master:season", Season, SEASON_FIELDS


class _BrandLikeDetailView(GoodsAPIView):
    kind: str
    family: str
    model: Any
    fields: frozenset[str]
    brand_scoped: bool = False

    @extend_schema(
        responses={
            200: _resource_response(BRAND_LIKE_DATA, "ResourceDTO<MasterPayload.brand|season>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        brands = _require_master_read(access)
        row = self.model.objects.filter(pk=pk).first()
        if row is not None and self.brand_scoped and brands is not None and row.pk not in brands:
            row = None
        if row is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        d = _MasterCRUD()
        d.kind, d.family = self.kind, self.family
        dto = d.dto(access.tenant_id, str(row.pk), _brand_season_fallback(row))
        return Response(
            resource_dto(
                id=row.pk,
                data=_brand_season_payload(row, dto["payload"]),
                revision=dto["revision"],
                state=dto["state"],
                context={},
            )
        )

    @extend_schema(
        responses={
            200: _resource_response(BRAND_LIKE_DATA, "ResourceDTO<MasterPayload.brand|season>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, self.fields)
        _require_one_field(body)
        access.require("vendor.manage", brand_id=pk if self.brand_scoped else None)
        row = self.model.objects.filter(pk=pk).first()
        if row is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        def handler(run: CommandRun) -> CommandResult:
            head = lock_revision(run, self.family, str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            latest = latest_master_version(run.tenant_id, self.kind, str(pk))
            merged = {**(latest.payload if latest else _brand_season_fallback(row)), **body}
            if not merged.get("code") or not merged.get("name"):
                raise Refusal("MASTER_INVALID", "code and name are required.")
            row.code = str(merged["code"])
            row.name = str(merged["name"])
            try:
                row.save(update_fields=["code", "name"])
            except IntegrityError as exc:
                raise Refusal("MASTER_CONFLICT", "That code is already in use.") from exc
            revision = bump_revision(head)
            append_master_version(
                run, kind=self.kind, target_key=str(pk), revision=revision, payload=merged
            )
            return CommandResult(resource_type=self.kind, resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="vendor.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=self.family,
        )
        row.refresh_from_db()
        d = _MasterCRUD()
        d.kind, d.family = self.kind, self.family
        dto = d.dto(access.tenant_id, str(row.pk), _brand_season_fallback(row))
        return Response(
            resource_dto(
                id=row.pk,
                data=_brand_season_payload(row, dto["payload"]),
                revision=dto["revision"],
                state=dto["state"],
                context={},
            ),
            status=result.status_code,
        )


@extend_schema_view(get=extend_schema(operation_id="goods_v1_masters_brands_detail"))
class GoodsBrandDetailView(_BrandLikeDetailView):
    kind, family, model, fields = "brand", "master:brand", Brand, BRAND_FIELDS
    brand_scoped = True


@extend_schema_view(get=extend_schema(operation_id="goods_v1_masters_seasons_detail"))
class GoodsSeasonDetailView(_BrandLikeDetailView):
    kind, family, model, fields = "season", "master:season", Season, SEASON_FIELDS


class _BrandLikeRetireView(GoodsAPIView):
    kind: str
    family: str
    model: Any
    has_is_active: bool = True
    brand_scoped: bool = False

    @extend_schema(
        responses={
            200: _resource_response(BRAND_LIKE_DATA, "ResourceDTO<MasterPayload.brand|season>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason_code", "effective_at"})
        reason_code, effective_at = _reason_and_effective(body)
        access.require("master.retire", brand_id=pk if self.brand_scoped else None)
        row = self.model.objects.filter(pk=pk).first()
        if row is None:
            raise Refusal("NOT_FOUND", "That record was not found.")

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            head = lock_revision(run, self.family, str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            if self.has_is_active:
                row.is_active = False
                row.save(update_fields=["is_active"])
            latest = latest_master_version(run.tenant_id, self.kind, str(pk))
            payload = latest.payload if latest else _brand_season_fallback(row)
            revision = bump_revision(head)
            append_master_version(
                run,
                kind=self.kind,
                target_key=str(pk),
                revision=revision,
                payload=payload,
                effective_from=effective_at,
                retired=True,
                reason_code=reason_code,
            )
            return CommandResult(resource_type=self.kind, resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="vendor.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=self.family,
        )
        row.refresh_from_db()
        d = _MasterCRUD()
        d.kind, d.family = self.kind, self.family
        dto = d.dto(access.tenant_id, str(row.pk), _brand_season_fallback(row))
        return Response(
            resource_dto(
                id=row.pk,
                data=_brand_season_payload(row, dto["payload"]),
                revision=dto["revision"],
                state=dto["state"],
                context={},
            ),
            status=result.status_code,
        )


class GoodsBrandRetireView(_BrandLikeRetireView):
    kind, family, model = "brand", "master:brand", Brand
    brand_scoped = True


class GoodsSeasonRetireView(_BrandLikeRetireView):
    kind, family, model, has_is_active = "season", "master:season", Season, False


# --- Subbrands: no legacy identity row, MasterVersion-only (design §5.5) -----


class GoodsSubbrandListCreateView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _page_response(
                _resource_response(SUBBRAND_DATA, "ResourceDTO<MasterPayload.subbrand>."),
                "Page<ResourceDTO<MasterPayload.subbrand>>.",
            ),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        brands = _require_master_read(access)
        params = check_query(request)
        rows = sorted(
            latest_versions_by_kind(access.tenant_id, "subbrand"), key=lambda r: r.target_key
        )
        if brands is not None:
            rows = [r for r in rows if _parent_brand(r.payload) in (None, *brands)]
        window, cursor = paginate(rows, params)
        items = [
            resource_dto(
                id=uuid.UUID(row.target_key),
                data=row.payload,
                revision=row.revision,
                state=master_state(row),
                context={},
            )
            for row in window
        ]
        return Response(page(items, cursor))

    @extend_schema(
        responses={
            201: _resource_response(SUBBRAND_DATA, "ResourceDTO<MasterPayload.subbrand>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, SUBBRAND_FIELDS, required=["code", "name"])
        access.require("vendor.manage", brand_id=_parent_brand(body))

        def handler(run: CommandRun) -> CommandResult:
            if (
                body.get("parent_id")
                and not Brand.objects.filter(
                    pk=parse_int_id(body["parent_id"], "parent_id")
                ).exists()
            ):
                raise Refusal("MASTER_INVALID", "parent_id does not name a known brand.")
            existing = [
                r
                for r in latest_versions_by_kind(run.tenant_id, "subbrand")
                if not r.retired and r.payload.get("code") == body["code"]
            ]
            if existing:
                raise Refusal(
                    "MASTER_CONFLICT", f"A subbrand with code {body['code']} already exists."
                )
            target_key = str(uuid.uuid4())
            start_revision(run.tenant_id, "master:subbrand", target_key)
            append_master_version(
                run, kind="subbrand", target_key=target_key, revision=1, payload=body
            )
            return CommandResult(resource_type="subbrand", resource_id=target_key, status_code=201)

        result = self.run_command(
            request,
            access=access,
            action="vendor.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:subbrand",
        )
        latest = latest_master_version(access.tenant_id, "subbrand", result.resource_id or "")
        assert latest is not None
        return Response(
            resource_dto(
                id=uuid.UUID(latest.target_key),
                data=latest.payload,
                revision=latest.revision,
                state=master_state(latest),
                context={},
            ),
            status=result.status_code,
        )


class GoodsSubbrandDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_masters_subbrands_detail",
        responses={
            200: _resource_response(SUBBRAND_DATA, "ResourceDTO<MasterPayload.subbrand>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        brands = _require_master_read(access)
        latest = latest_master_version(access.tenant_id, "subbrand", str(pk))
        if (
            latest is not None
            and brands is not None
            and _parent_brand(latest.payload) not in (None, *brands)
        ):
            latest = None
        if latest is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        return Response(
            resource_dto(
                id=pk,
                data=latest.payload,
                revision=latest.revision,
                state=master_state(latest),
                context={},
            )
        )

    @extend_schema(
        responses={
            200: _resource_response(SUBBRAND_DATA, "ResourceDTO<MasterPayload.subbrand>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, SUBBRAND_FIELDS)
        _require_one_field(body)
        current = latest_master_version(access.tenant_id, "subbrand", str(pk))
        access.require(
            "vendor.manage", brand_id=_parent_brand(current.payload if current else None)
        )
        if body.get("parent_id") not in (None, ""):
            access.require("vendor.manage", brand_id=_parent_brand(body))

        def handler(run: CommandRun) -> CommandResult:
            head = lock_revision(run, "master:subbrand", str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            latest = latest_master_version(run.tenant_id, "subbrand", str(pk))
            if latest is None:
                raise Refusal("NOT_FOUND", "That record was not found.")
            merged = {**latest.payload, **body}
            if not merged.get("code") or not merged.get("name"):
                raise Refusal("MASTER_INVALID", "code and name are required.")
            revision = bump_revision(head)
            append_master_version(
                run, kind="subbrand", target_key=str(pk), revision=revision, payload=merged
            )
            return CommandResult(resource_type="subbrand", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="vendor.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:subbrand",
        )
        latest = latest_master_version(access.tenant_id, "subbrand", str(pk))
        assert latest is not None
        return Response(
            resource_dto(
                id=pk,
                data=latest.payload,
                revision=latest.revision,
                state=master_state(latest),
                context={},
            ),
            status=result.status_code,
        )


class GoodsSubbrandRetireView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(SUBBRAND_DATA, "ResourceDTO<MasterPayload.subbrand>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason_code", "effective_at"})
        reason_code, effective_at = _reason_and_effective(body)
        current = latest_master_version(access.tenant_id, "subbrand", str(pk))
        access.require(
            "master.retire", brand_id=_parent_brand(current.payload if current else None)
        )

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            head = lock_revision(run, "master:subbrand", str(pk), rank=LockRank.DRAFT)
            if head.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this record after you loaded it."
                )
            latest = latest_master_version(run.tenant_id, "subbrand", str(pk))
            if latest is None:
                raise Refusal("NOT_FOUND", "That record was not found.")
            revision = bump_revision(head)
            append_master_version(
                run,
                kind="subbrand",
                target_key=str(pk),
                revision=revision,
                payload=latest.payload,
                effective_from=effective_at,
                retired=True,
                reason_code=reason_code,
            )
            return CommandResult(resource_type="subbrand", resource_id=str(pk), status_code=200)

        result = self.run_command(
            request,
            access=access,
            action="vendor.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="master:subbrand",
        )
        latest = latest_master_version(access.tenant_id, "subbrand", str(pk))
        assert latest is not None
        return Response(
            resource_dto(
                id=pk,
                data=latest.payload,
                revision=latest.revision,
                state=master_state(latest),
                context={},
            ),
            status=result.status_code,
        )


# ==========================================================================
# Configurations (E085-E089)
# ==========================================================================


def _version_states(drafts: list[ConfigDraft]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Each draft line's approved versions with the state they are really in.

    E085 step 5 reads "drafts/effective/superseded versions", and change PRD §14.7
    rule 17 requires the screen to show each version's real state - a draft row's
    own ``draft``/``submitted``/``approved`` says nothing about whether the version
    it produced is effective, scheduled, ended or withdrawn. The version ``id`` is
    here too, so a document that pinned a version can be shown which one by name
    instead of by opaque UUID.
    """
    from masters.goods_config import candidates

    now = database_now()
    if not drafts:
        return {}
    tenant_id = drafts[0].tenant_id
    rows = list(
        ConfigVersion.objects.filter(
            tenant_id=tenant_id, draft_id__in=[d.pk for d in drafts]
        ).order_by("version")
    )
    reasons = dict(
        EffectiveVersionPeriod.objects.filter(
            tenant_id=tenant_id,
            target_kind=EffectiveVersionPeriod.TargetKind.CONFIGURATION,
            target_id__in=[row.pk for row in rows],
        ).values_list("target_id", "withdrawn_reason")
    )
    out: dict[uuid.UUID, list[dict[str, Any]]] = {d.pk: [] for d in drafts}
    for found in candidates(rows):
        out.setdefault(found.version.draft_id, []).append(
            {
                "id": str(found.version.pk),
                "version": found.version.version,
                "state": found.state(now),
                "effective_from": found.effective_from.isoformat(),
                "effective_to": found.effective_to.isoformat() if found.effective_to else None,
                "withdrawn_at": found.withdrawn_at.isoformat() if found.withdrawn_at else None,
                "withdrawn_reason": reasons.get(found.version.pk) or None,
            }
        )
    return out


def _backdate_preview(draft: ConfigDraft) -> dict[str, Any]:
    """E088 step 10's impact list, computed for review *before* submission (M5).

    Submission recomputes and enforces the same thing; this only lets the editor
    show what a backdated start would affect, and why it would be refused, rather
    than making the person submit to find out.
    """
    now = database_now()
    # One read of the tenant's day boundary for both checks.
    today = start_of_today(draft.tenant_id, now)
    backdated = is_backdated(draft.tenant_id, draft, now, today)
    try:
        impact = backdate_impact(draft.tenant_id, draft, now, today)
    except Refusal as refusal:
        # Only the backdate rule is an answer this read may carry. Anything else
        # is a real refusal and must stay one, not be folded into a 200.
        if refusal.code != "BACKDATE_INVALID":
            raise
        return {
            "is_backdated": backdated,
            "impact": [],
            "blocked": {
                "code": refusal.code,
                "message": refusal.message,
                "issues": refusal.issues,
            },
        }
    return {"is_backdated": backdated, "impact": impact, "blocked": None}


def _config_dto(
    draft: ConfigDraft,
    *,
    version: ConfigVersion | None = None,
    versions: list[dict[str, Any]] | None = None,
    backdate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if version is not None:
        from masters.goods_config import candidate

        period = candidate(version)
        return resource_dto(
            id=draft.pk,
            data={
                "kind": version.kind,
                "scope": version.scope,
                "payload": version.payload,
                "effective_from": period.effective_from.isoformat(),
                "effective_to": period.effective_to.isoformat() if period.effective_to else None,
            },
            revision=draft.revision,
            state=period.state(database_now()),
            context={},
            version=version.version,
        )
    # `content` keeps the reviewed hash on exactly the reviewable draft content
    # (`config_draft_data`), so the read-only version/backdate facts added to
    # `data` for the editor cannot move the hash E088 compares against.
    reviewable = config_draft_data(draft)
    return resource_dto(
        id=draft.pk,
        data={
            **reviewable,
            "versions": versions if versions is not None else [],
            **({} if backdate is None else {"backdate": backdate}),
        },
        content=reviewable,
        revision=draft.revision,
        state=draft.state,
        context={},
    )


def _config_period(
    body: dict[str, Any], row: ConfigDraft | None = None
) -> tuple[datetime, datetime | None]:
    """``[effective_from, effective_to)`` from a draft body; the end must follow the start."""
    effective_from = (
        parse_dt(body["effective_from"], "effective_from")
        if body.get("effective_from") or row is None
        else row.effective_from
    )
    assert effective_from is not None
    if "effective_to" in body or row is None:
        effective_to = parse_dt(body.get("effective_to"), "effective_to", required=False)
    else:
        effective_to = row.effective_to
    if effective_to is not None and effective_to <= effective_from:
        raise Refusal(
            "CONFIG_INVALID",
            "effective_to must be after effective_from.",
            status=422,
            issues=[issue("INVALID", "must be after effective_from", field="effective_to")],
        )
    return effective_from, effective_to


#: One approved version of a configuration line, with the state it is really in
#: (change PRD §14.7 rule 17).
CONFIG_VERSION_STATE = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "version": {"type": "integer"},
        "state": {
            "type": "string",
            "enum": ["effective", "scheduled", "ended", "withdrawn"],
        },
        "effective_from": {"type": "string"},
        "effective_to": {"type": "string", "nullable": True},
        "withdrawn_at": {"type": "string", "nullable": True},
        "withdrawn_reason": {"type": "string", "nullable": True},
    },
}

CONFIG_DATA = {
    "type": "object",
    "description": (
        "ConfigPayload draft content plus its approved versions' real states. "
        "`content_hash` covers only the reviewable draft content "
        "(kind/scope/payload/effective_from/effective_to)."
    ),
    "properties": {
        "kind": {"type": "string"},
        "scope": {"type": "object", "additionalProperties": True},
        "payload": {"type": "object", "additionalProperties": True},
        "effective_from": {"type": "string"},
        "effective_to": {"type": "string", "nullable": True},
        "versions": {"type": "array", "items": CONFIG_VERSION_STATE},
        "backdate": {
            "type": "object",
            "description": "Detail read only: E088 step 10's impact list, previewed.",
            "properties": {
                "is_backdated": {"type": "boolean"},
                "impact": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "resource_key": {"type": "string"},
                            "effect": {"type": "string"},
                            "official_unchanged": {"type": "boolean"},
                        },
                    },
                },
                "blocked": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "issues": {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
        },
    },
}


#: A PT preparer or approver must be able to see which profile is approved
#: (E122/E123's `profile_version_id`) to prepare or review a PT at all —
#: reading a configuration was otherwise gated to the people who draft or
#: approve one (GSA-T06: PT preparation and approval screens, a small
#: necessary read-path widening reported to Anand). Deliberately narrower
#: than `config.draft`/`config.approve`: it reaches only `kind="profile"`,
#: never `rates`/`tax_rates` — a plain preparer has no cost/margin grant, and
#: those two kinds' payloads are exactly the tenant's cost percentages
#: (`_config_dto` redacts nothing by field, only by whether the caller may
#: read the draft at all).
CONFIG_READ_ACTIONS = (
    "pt.prepare",
    "pt.prepare.opening",
    "pt.approve.receipt",
    "pt.approve.opening",
    "pt.approve.transfer",
    "pt.reversal.request",
    "pt.reversal.approve",
)
CONFIG_READ_KINDS = frozenset({"profile"})

#: Which identity profile a scanned code is read under is a fact about the tenant,
#: not a choice a receiver at a dock makes - but E090/E091 need it named. So anyone
#: who may resolve a code at all may read the *identity profile* kind, and only that
#: kind. It carries governed vocabulary and family, never a rate, tax or cost figure.
IDENTITY_CONFIG_KINDS = frozenset({"identity_profile"})
IDENTITY_CONFIG_READ_ACTIONS = (
    "identity.resolve",
    "receive.arrival",
    "stock.accept",
    "count.run",
    "count.review",
    "label.print",
    "transfer.allocate",
    "transfer.move",
    *CONFIG_READ_ACTIONS,
)

#: Which pinned label profile a print job submits as `template_version_id`
#: (E182) is a fact about the tenant's printer setup, not a configuration
#: choice the printing screen makes - but it has to be named from *something*.
#: So anyone who may print a label at all may read the *label* kind, and only
#: that kind (ticket 11, same shape as `IDENTITY_CONFIG_KINDS` above).
LABEL_CONFIG_KINDS = frozenset({"label"})
LABEL_CONFIG_READ_ACTIONS = ("label.print",)


def _can_read_configuration(access: AccessContext, kind: str | None) -> bool:
    if access.holds("config.draft") or access.holds("config.approve"):
        return True
    if kind in CONFIG_READ_KINDS:
        return any(access.holds(action) for action in CONFIG_READ_ACTIONS)
    if kind in IDENTITY_CONFIG_KINDS:
        return any(access.holds(action) for action in IDENTITY_CONFIG_READ_ACTIONS)
    if kind in LABEL_CONFIG_KINDS:
        return any(access.holds(action) for action in LABEL_CONFIG_READ_ACTIONS)
    return False


def _label_row_reachable(access: AccessContext, row: ConfigDraft) -> bool:
    """Unlike `identity_profile` (tenant-wide by contract, so nothing to leak
    across), a `label` profile is genuinely per-site. `_can_read_configuration`
    only asks whether the caller holds `label.print` *somewhere*; without this,
    a printer at one site would list every other site's label profile too. A
    tenant-wide label scope (no named sites) stays visible to anyone who may
    print at all - there is no narrower site to check it against."""
    site_ids = row.scope.get("site_ids") or []
    if not site_ids:
        return True
    return any(access.can_reach_site("label.print", int(site_id)) for site_id in site_ids)


class GoodsConfigurationListCreateView(GoodsAPIView):
    """E085 list and E086 draft create."""

    @extend_schema(
        responses={
            200: _page_response(_resource_response(CONFIG_DATA, "ResourceDTO<ConfigPayload>.")),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(
            request,
            {"q", "cursor", "limit", "site_id", "brand_id", "sbu_id", "kind"},
        )
        kind = params.get("kind")
        if not _can_read_configuration(access, kind):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        rows = list(ConfigDraft.objects.filter(tenant_id=access.tenant_id).order_by("-created_at"))
        if kind:
            rows = [r for r in rows if r.kind == kind]
        if kind in LABEL_CONFIG_KINDS and not (
            access.holds("config.draft") or access.holds("config.approve")
        ):
            rows = [r for r in rows if _label_row_reachable(access, r)]
        window, cursor = paginate(rows, params)
        states = _version_states(window)
        return Response(
            page([_config_dto(d, versions=states.get(d.pk, [])) for d in window], cursor)
        )

    @extend_schema(
        responses={
            201: _resource_response(CONFIG_DATA, "ResourceDTO<ConfigPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
            503: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"kind", "scope", "effective_from", "effective_to", "payload", "reason_code"},
            required=["kind", "scope", "effective_from", "payload"],
        )
        access.require_action("config.draft")

        def handler(run: CommandRun) -> CommandResult:
            from masters.goods_config import check_scope_references, normalise_scope

            effective_from, effective_to = _config_period(body)
            scope = normalise_scope(body["scope"])
            check_scope_references(scope)
            scope_key = config_scope_key(scope)
            validate_config_payload(
                str(body["kind"]),
                body["payload"],
                tenant_id=run.tenant_id,
                scope_key=scope_key,
                as_of=effective_from,
                scope=scope,
            )
            draft = ConfigDraft.objects.create(
                tenant_id=run.tenant_id,
                kind=str(body["kind"]),
                scope=scope,
                scope_key=scope_key,
                payload=body["payload"],
                effective_from=effective_from,
                effective_to=effective_to,
                maker_id=_human(run),
            )
            run.audit_subject_key = config_subject_key(draft.pk)
            return CommandResult(
                resource_type="configuration", resource_id=str(draft.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="config.draft",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="configuration",
        )
        draft = ConfigDraft.objects.get(pk=_rid(result))
        return Response(_config_dto(draft), status=result.status_code)


class GoodsConfigurationDetailView(GoodsAPIView):
    """E089 detail (draft or one immutable version) and E087 draft correction."""

    @extend_schema(
        operation_id="goods_v1_masters_configurations_detail",
        responses={
            200: _resource_response(CONFIG_DATA, "ResourceDTO<ConfigPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        full_access = access.holds("config.draft") or access.holds("config.approve")
        if not full_access and not any(access.holds(a) for a in CONFIG_READ_ACTIONS):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        draft = ConfigDraft.objects.filter(pk=pk, tenant_id=access.tenant_id).first()
        if draft is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        # A PT-only reader reaches no kind but `profile` (CONFIG_READ_KINDS) —
        # `rates`/`tax_rates` are the tenant's cost percentages, and a plain
        # preparer holds no cost/margin grant. Hidden the same way a missing
        # id is, not a distinguishable refusal.
        if not full_access and draft.kind not in CONFIG_READ_KINDS:
            raise Refusal("NOT_FOUND", "That record was not found.")
        version_param = request.query_params.get("version")
        if version_param:
            version = ConfigVersion.objects.filter(draft=draft, version=int(version_param)).first()
            if version is None:
                raise Refusal("NOT_FOUND", "That record was not found.")
            return Response(_config_dto(draft, version=version))
        return Response(
            _config_dto(
                draft,
                versions=_version_states([draft])[draft.pk],
                backdate=_backdate_preview(draft),
            )
        )

    @extend_schema(
        responses={
            200: _resource_response(CONFIG_DATA, "ResourceDTO<ConfigPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
            503: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"payload", "effective_from", "effective_to", "reason_code"},
            required=["payload"],
        )
        access.require_action("config.draft")
        draft = ConfigDraft.objects.filter(pk=pk, tenant_id=access.tenant_id).first()
        if draft is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        if draft.maker_id != access.human_id and not access.holds("config.approve"):
            raise Refusal(
                "ACTION_DENIED", "Only the drafter or a configuration steward may edit this."
            )

        def handler(run: CommandRun) -> CommandResult:
            from approvals.goods_services import supersede_pending

            # An edit invalidates any earlier submission (E087 step 10), so an approver
            # can never freeze content they did not review. The pending request is
            # locked at DOCUMENT rank, below this draft's own DRAFT guard.
            supersede_pending(run, "configuration", config_subject_key(pk), "config.approve")
            locked = run.lock(LockRank.DRAFT, ConfigDraft.objects.filter(pk=pk))
            if not locked:
                raise Refusal("NOT_FOUND", "That record was not found.")
            row = locked[0]
            if row.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this draft after you loaded it."
                )
            if row.state == ConfigDraft.State.APPROVED:
                raise Refusal(
                    "OFFICIAL_IMMUTABLE", "An approved configuration version cannot be edited."
                )
            effective_from, effective_to = _config_period(body, row)
            validate_config_payload(
                row.kind,
                body["payload"],
                tenant_id=run.tenant_id,
                scope_key=row.scope_key,
                as_of=effective_from,
                scope=row.scope,
            )
            row.payload = body["payload"]
            row.effective_from = effective_from
            row.effective_to = effective_to
            row.revision += 1
            row.state = ConfigDraft.State.DRAFT
            row.save()
            return CommandResult(
                resource_type="configuration", resource_id=str(row.pk), status_code=200
            )

        result = self.run_command(
            request,
            access=access,
            action="config.draft",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=config_subject_key(pk),
        )
        draft.refresh_from_db()
        return Response(_config_dto(draft), status=result.status_code)


class GoodsConfigurationSubmitView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(CONFIG_DATA, "ResourceDTO<ConfigPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
            503: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reviewed_hash"}, required=["reviewed_hash"])
        access.require_action("config.draft")
        draft = ConfigDraft.objects.filter(pk=pk, tenant_id=access.tenant_id).first()
        if draft is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        if draft.maker_id != access.human_id:
            raise Refusal(
                "ACTION_DENIED", "Only the person who drafted this configuration can submit it."
            )

        def handler(run: CommandRun) -> CommandResult:
            from approvals.goods_services import create_request

            # `create_request` locks ApprovalRequest rows at rank DOCUMENT, below
            # this draft's own rank DRAFT (design §4.1's fixed ordering never goes
            # down within one command) - so it runs first, off a plain read, and
            # only the draft's own guard is taken under an explicit lock.
            unlocked = ConfigDraft.objects.filter(pk=pk).first()
            if unlocked is None:
                raise Refusal("NOT_FOUND", "That record was not found.")
            if unlocked.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this draft after you loaded it."
                )
            current_hash = config_draft_hash(unlocked)
            if body["reviewed_hash"] != current_hash:
                raise Refusal(
                    "REVISION_SUPERSEDED", "What you reviewed is no longer the current draft."
                )
            validate_config_payload(
                unlocked.kind,
                unlocked.payload,
                tenant_id=run.tenant_id,
                scope_key=unlocked.scope_key,
                as_of=unlocked.effective_from,
                scope=unlocked.scope,
            )
            # A backdate is refused when it would re-time an approved version; its
            # impact list is recomputed and frozen on the version at approval.
            backdate_impact(run.tenant_id, unlocked, run.now)
            # An overlap with another approved version is refused now, and again under
            # lock when the version is approved.
            from masters.goods_config import refuse_conflicts

            refuse_conflicts(
                run.tenant_id,
                kind=unlocked.kind,
                payload=unlocked.payload,
                scope=unlocked.scope,
                effective_from=unlocked.effective_from,
                effective_to=unlocked.effective_to,
                draft_id=unlocked.pk,
            )
            request_row = create_request(
                run,
                subject_kind="configuration",
                subject_key=config_subject_key(pk),
                revision=unlocked.revision,
                reviewed_hash=current_hash,
                requested_action="config.approve",
                site_id=None,
                reconciliation=None,
                title=f"Configuration {unlocked.kind}",
            )
            locked = run.lock(LockRank.DRAFT, ConfigDraft.objects.filter(pk=pk))
            if not locked:
                raise Refusal("NOT_FOUND", "That record was not found.")
            row = locked[0]
            if row.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed this draft after you loaded it."
                )
            row.state = ConfigDraft.State.SUBMITTED
            row.save(update_fields=["state"])
            return CommandResult(
                resource_type="configuration",
                resource_id=str(row.pk),
                status_code=200,
                event_ids=[str(request_row.pk)],
            )

        result = self.run_command(
            request,
            access=access,
            action="config.draft",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=config_subject_key(pk),
            reviewed_hash=body["reviewed_hash"],
        )
        draft.refresh_from_db()
        return Response(_config_dto(draft), status=result.status_code)


# ==========================================================================
# Summary / Tenant (E215-E217)
# ==========================================================================


class GoodsSummaryView(GoodsAPIView):
    """E215: counts only masters the caller could list, inside the same filters."""

    @extend_schema(
        responses={
            200: SUMMARY_RESPONSE,
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        held = access.all_actions()
        reads_entities = "org.entity.manage" in held
        reads_sites = "org.site.manage" in held
        reads_masters = bool(held & TENANT_MASTER_READ_ACTIONS)
        if not (reads_entities or reads_sites or reads_masters):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        site: Store | None = None
        if params.get("site_id"):
            site_id = parse_int_id(params["site_id"], "site_id")
            site = Store.objects.filter(pk=site_id).first()
            if site is None or not access.can("org.site.manage", site_id=site_id):
                raise Refusal("NOT_FOUND", "That record was not found.")
        counts: list[dict[str, Any]] = []
        if reads_entities:
            entities = LegalEntity.objects.filter(is_active=True)
            registrations = Gstin.objects.filter(is_active=True)
            entity_ids = _entity_scope(access, "org.entity.manage")
            if entity_ids is not None:
                entities = entities.filter(pk__in=sorted(entity_ids))
                registrations = registrations.filter(legal_entity_id__in=sorted(entity_ids))
            if site is not None:
                entities = entities.filter(gstins__stores=site)
                registrations = registrations.filter(pk=site.gstin_id)
            counts.append({"kind": "entities", "count": entities.distinct().count()})
            counts.append({"kind": "registrations", "count": registrations.count()})
        if reads_sites:
            sites = Store.objects.filter(
                access.site_filter("org.site.manage", field_name="pk"), is_active=True
            )
            if site is not None:
                sites = sites.filter(pk=site.pk)
            counts.append({"kind": "sites", "count": sites.count()})
        if reads_masters:
            brands = Brand.objects.filter(is_active=True)
            brand_ids = _master_brand_scope(access)
            if brand_ids is not None:
                brands = brands.filter(pk__in=sorted(brand_ids))
            counts.append({"kind": "brands", "count": brands.count()})
            counts.append({"kind": "seasons", "count": Season.objects.count()})
        return Response({"counts": counts, "as_of": timezone.now().isoformat()})


def _tenant_audit_values(tenant: Tenant) -> list[dict[str, Any]]:
    """The tenant profile's permitted before/after evidence for E247."""
    return [
        {"field": "code", "redacted": False, "value": tenant.code},
        {"field": "name", "redacted": False, "value": tenant.name},
        {"field": "timezone", "redacted": False, "value": tenant.timezone},
        {"field": "currency", "redacted": False, "value": tenant.currency},
        {"field": "locale", "redacted": False, "value": tenant.locale},
        {
            "field": "business_profile_version_id",
            "redacted": False,
            "value": str(tenant.business_profile_version_id)
            if tenant.business_profile_version_id
            else None,
        },
    ]


class GoodsTenantView(GoodsAPIView):
    @extend_schema(
        responses={
            200: _resource_response(TENANT_DATA, "ResourceDTO<TenantInput>."),
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        access.require("org.tenant.manage")
        tenant = Tenant.objects.get(pk=access.tenant_id)
        return Response(
            resource_dto(
                id=tenant.pk,
                data={
                    "code": tenant.code,
                    "name": tenant.name,
                    "timezone": tenant.timezone,
                    "currency": tenant.currency,
                    "locale": tenant.locale,
                    "business_profile_version_id": str(tenant.business_profile_version_id)
                    if tenant.business_profile_version_id
                    else None,
                },
                revision=tenant.revision,
                state="active",
                context={},
            )
        )

    @extend_schema(
        responses={
            200: _resource_response(TENANT_DATA, "ResourceDTO<TenantInput>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"code", "name", "timezone", "currency", "locale", "business_profile_version_id"},
            required=["code", "name", "timezone", "currency", "locale"],
        )
        access.require("org.tenant.manage")

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            locked = run.lock(LockRank.SECURITY, Tenant.objects.filter(pk=access.tenant_id))
            tenant = locked[0]
            if tenant.revision != meta.expected_revision:
                raise Refusal(
                    "REVISION_SUPERSEDED", "Someone changed the tenant profile after you loaded it."
                )
            if body["code"] != tenant.code:
                raise Refusal(
                    "TENANT_BINDING_IMMUTABLE", "The tenant/deployment code cannot be changed."
                )
            run.audit_before = _tenant_audit_values(tenant)
            tenant.name = str(body["name"])
            tenant.timezone = str(body["timezone"])
            tenant.currency = str(body["currency"])
            tenant.locale = str(body["locale"])
            tenant.revision += 1
            tenant.save()
            run.audit_after = _tenant_audit_values(tenant)
            return CommandResult(
                resource_type="tenant", resource_id=str(tenant.pk), status_code=200
            )

        result = self.run_command(
            request,
            access=access,
            action="org.tenant.manage",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key="tenant",
        )
        tenant = Tenant.objects.get(pk=access.tenant_id)
        return Response(
            resource_dto(
                id=tenant.pk,
                data={
                    "code": tenant.code,
                    "name": tenant.name,
                    "timezone": tenant.timezone,
                    "currency": tenant.currency,
                    "locale": tenant.locale,
                    "business_profile_version_id": str(tenant.business_profile_version_id)
                    if tenant.business_profile_version_id
                    else None,
                },
                revision=tenant.revision,
                state="active",
                context={},
            ),
            status=result.status_code,
        )


def _require_config_authority(access: AccessContext, version: ConfigVersion) -> None:
    """``config.approve`` over every site and brand the version reaches (a tenant-wide
    version needs a tenant-wide grant)."""
    from masters.goods_config import read_scope

    scope = read_scope(version.scope)
    sites: list[int | None] = [int(s) for s in scope["site_ids"]] or [None]
    brands: list[int | None] = [int(b) for b in scope["brand_ids"]] or [None]
    entity = int(scope["entity_id"]) if scope["entity_id"] else None
    cells = {(site, brand) for site in sites for brand in brands}
    if not access.covers_all({"config.approve"}, cells, entity_id=entity):
        raise Refusal("ACTION_DENIED", "This configuration reaches beyond your authority.")


class GoodsConfigurationWithdrawView(GoodsAPIView):
    """Withdraw an approved version for invalidity (change PRD §14.4).

    A withdrawn version stops every later use, including drafts and approval requests
    that pinned it; they are refused or found stale rather than moved to another version.
    Withdrawal only narrows what is in force, so one configuration approver with a fresh
    password confirmation records it.
    """

    @extend_schema(
        responses={
            200: _resource_response(CONFIG_DATA, "ResourceDTO<ConfigPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            503: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, {"version", "reason_code"}, required=["version", "reason_code"]
        )
        number = body["version"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise Refusal("INVALID_REQUEST", "version must be a positive integer.")
        reason = str(body["reason_code"])
        if not reason or len(reason) > 60:
            raise Refusal("INVALID_REQUEST", "reason_code must be 1 to 60 characters.")
        access.require_action("config.approve")
        draft = ConfigDraft.objects.filter(pk=pk, tenant_id=access.tenant_id).first()
        version = (
            ConfigVersion.objects.filter(draft=draft, version=number).first() if draft else None
        )
        if draft is None or version is None:
            raise Refusal("NOT_FOUND", "That record was not found.")
        _require_config_authority(access, version)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            from masters.goods_config import withdraw

            withdraw(run, version, reason_code=reason)
            run.audit_subject_key = config_subject_key(pk)
            run.audit_after = {"config_version_id": str(version.pk), "reason_code": reason}
            return CommandResult(
                resource_type="configuration", resource_id=str(draft.pk), version=version.version
            )

        result = self.run_command(
            request,
            access=access,
            action="config.withdraw",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=config_subject_key(pk),
        )
        return Response(_config_dto(draft, version=version), status=result.status_code)


# ==========================================================================
# Actor / approval policies (E222-E225)
# ==========================================================================


def _policy_scope_key(prefix: str, name: str) -> str:
    return f"{prefix}:{name}"[:500]


def _policy_dto(
    draft: ConfigDraft | None, version: ConfigVersion | None, name: str
) -> dict[str, Any]:
    if version is not None:
        from core.commands import database_now
        from masters.goods_config import candidate

        return resource_dto(
            id=version.pk,
            data=version.payload,
            revision=version.version,
            state=candidate(version).state(database_now()),
            context={},
        )
    if draft is not None:
        return resource_dto(
            id=draft.pk, data=draft.payload, revision=draft.revision, state=draft.state, context={}
        )
    return resource_dto(
        id=uuid.uuid5(uuid.NAMESPACE_URL, name), data={}, revision=0, state="unset", context={}
    )


POLICY_FIELDS = frozenset(
    {
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
    }
)


def _require_policy_admin(access: AccessContext, *, drafting: bool = False) -> None:
    """E222-E225 belong to X-PLT/C-OWN, the access administrators - not every drafter."""
    access.require_action("access.manage")
    if drafting:
        access.require_action("config.draft")


def _policy_scope(payload: dict[str, Any]) -> dict[str, Any]:
    """The one ``ConfigScope`` a policy written at E223/E225 applies to, read from its payload."""
    from masters.goods_config import check_scope_references, normalise_scope

    site_ids = payload.get("site_ids") if isinstance(payload.get("site_ids"), list) else []
    brand_ids = payload.get("brand_ids") if isinstance(payload.get("brand_ids"), list) else []
    kind = "sites" if site_ids else "brands" if brand_ids else "tenant"
    purpose = payload.get("purpose")
    scope = normalise_scope(
        {
            "scope_kind": kind,
            "site_ids": site_ids,
            "brand_ids": brand_ids,
            "purposes": [purpose] if purpose else [],
        }
    )
    check_scope_references(scope)
    return scope


def _registered_policy_name(name: str, what: str) -> None:
    if name not in ACTIONS:
        raise Refusal(
            "CONFIG_INVALID",
            f"{name} is not a registered {what}.",
            status=422,
            issues=[issue("INVALID", f"unregistered {what}", field=what)],
        )


def _write_policy_draft(
    run: CommandRun,
    *,
    expected_revision: int | None,
    scope_key: str,
    body: dict[str, Any],
    default: dict[str, Any],
    fixed: dict[str, Any],
) -> ConfigDraft:
    """Create or revise the open policy draft; an approved version is never edited in place."""
    from approvals.goods_services import supersede_pending

    open_draft = (
        ConfigDraft.objects.filter(tenant_id=run.tenant_id, scope_key=scope_key)
        .exclude(state=ConfigDraft.State.APPROVED)
        .order_by("-revision")
        .first()
    )
    approved = _current_policy_version(run.tenant_id, scope_key)
    base = open_draft.payload if open_draft else (approved.payload if approved else default)
    payload = {"site_ids": [], "brand_ids": [], **base, **body, **fixed}
    scope = _policy_scope(payload)
    validate_config_payload("approval", payload, tenant_id=run.tenant_id, scope=scope)
    if open_draft is None:
        row = ConfigDraft.objects.create(
            tenant_id=run.tenant_id,
            kind="approval",
            scope=scope,
            scope_key=scope_key,
            payload=payload,
            effective_from=run.now,
            maker_id=_human(run),
        )
    else:
        supersede_pending(run, "configuration", config_subject_key(open_draft.pk), "config.approve")
        row = run.lock(LockRank.DRAFT, ConfigDraft.objects.filter(pk=open_draft.pk))[0]
        if row.revision != expected_revision:
            raise Refusal("REVISION_SUPERSEDED", "Someone changed this policy after you loaded it.")
        row.payload = payload
        row.scope = scope
        row.effective_from = run.now
        row.revision += 1
        row.state = ConfigDraft.State.DRAFT
        row.save()
    run.audit_subject_key = config_subject_key(row.pk)
    return row


class GoodsActorPolicyDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_auth_admin_actor_policies_detail",
        responses={
            200: _resource_response(POLICY_DATA, "ResourceDTO<PolicyPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, action: str) -> Response:
        access = self.access(request)
        _require_policy_admin(access)
        _registered_policy_name(action, "action")
        scope_key = _policy_scope_key("approval:actor", action)
        version = _current_policy_version(access.tenant_id, scope_key)
        draft = None if version else _latest_policy_draft(access.tenant_id, scope_key)
        return Response(_policy_dto(draft, version, action))

    @extend_schema(
        responses={
            200: _resource_response(POLICY_DATA, "ResourceDTO<PolicyPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, action: str) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, POLICY_FIELDS)
        _require_one_field(body)
        _require_policy_admin(access, drafting=True)
        _registered_policy_name(action, "action")
        scope_key = _policy_scope_key("approval:actor", action)

        def handler(run: CommandRun) -> CommandResult:
            row = _write_policy_draft(
                run,
                expected_revision=meta.expected_revision,
                scope_key=scope_key,
                body=body,
                default={"action": action},
                fixed={"action": action},
            )
            return CommandResult(
                resource_type="actor_policy", resource_id=str(row.pk), status_code=200
            )

        result = self.run_command(
            request,
            access=access,
            action="config.draft",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=scope_key,
        )
        draft = ConfigDraft.objects.get(pk=_rid(result))
        return Response(_policy_dto(draft, None, action), status=result.status_code)


class GoodsApprovalPolicyDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_auth_admin_approval_policies_detail",
        responses={
            200: _resource_response(POLICY_DATA, "ResourceDTO<PolicyPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, kind: str) -> Response:
        access = self.access(request)
        _require_policy_admin(access)
        _registered_policy_name(kind, "kind")
        scope_key = _policy_scope_key("approval:kind", kind)
        version = _current_policy_version(access.tenant_id, scope_key)
        draft = None if version else _latest_policy_draft(access.tenant_id, scope_key)
        return Response(_policy_dto(draft, version, kind))

    @extend_schema(
        responses={
            200: _resource_response(POLICY_DATA, "ResourceDTO<PolicyPayload>."),
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def patch(self, request: Request, kind: str) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, POLICY_FIELDS)
        _require_one_field(body)
        _require_policy_admin(access, drafting=True)
        _registered_policy_name(kind, "kind")
        scope_key = _policy_scope_key("approval:kind", kind)

        def handler(run: CommandRun) -> CommandResult:
            row = _write_policy_draft(
                run,
                expected_revision=meta.expected_revision,
                scope_key=scope_key,
                body=body,
                default={"action": kind},
                fixed={"action": kind},
            )
            return CommandResult(
                resource_type="approval_policy", resource_id=str(row.pk), status_code=200
            )

        result = self.run_command(
            request,
            access=access,
            action="config.draft",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=scope_key,
        )
        draft = ConfigDraft.objects.get(pk=_rid(result))
        return Response(_policy_dto(draft, None, kind), status=result.status_code)


def _latest_policy_draft(tenant_id: uuid.UUID, scope_key: str) -> ConfigDraft | None:
    return (
        ConfigDraft.objects.filter(tenant_id=tenant_id, scope_key=scope_key)
        .order_by("-revision")
        .first()
    )


def _current_policy_version(tenant_id: uuid.UUID, scope_key: str) -> ConfigVersion | None:
    return (
        ConfigVersion.objects.filter(tenant_id=tenant_id, scope_key=scope_key)
        .order_by("-version")
        .first()
    )
