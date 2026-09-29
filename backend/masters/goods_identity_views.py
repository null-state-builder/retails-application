"""Goods-v1 product master and identity endpoints (E041-E060, E090, E091).

Styles, SKUs and aliases: C-PMO (``product.master.manage``) creates effective
masters; C-WHO (``product.master.propose``) proposes pending ones bound to the PT
draft being prepared. Crosswalks: C-PMO manages, C-BUY proposes. Retirement is a
company-owner step-up action (``master.retire``). Barcode lookups return every candidate and an
identity pick records one person's choice; SKUs are never merged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.apps import apps
from django.db.models import Q, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    LIST_QUERY_KEYS,
    GoodsAPIView,
    business_body,
    check_query,
    check_revision,
    decode_cursor,
    encode_cursor,
    page,
    page_limit,
    paginate,
    parse_int_id,
    parse_meta,
    parse_uuid,
    resource_dto,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun, LockRank
from core.kernel_models import DraftRevision
from core.refusals import Refusal, issue
from masters.goods_identity_models import (
    GovernanceState,
    IdentityPick,
    ProductSku,
    SkuAlias,
    SourceCrosswalk,
    Style,
)
from masters.goods_identity_services import (
    ALIAS_TYPES,
    BRAND_ISSUER_PREFIX,
    CROSSWALK_KINDS,
    MANAGE_ACTION,
    PROPOSE_ACTION,
    IdentityProfile,
    alias_data,
    allocate_generated_value,
    attribute_dimensions,
    attribute_target,
    bounded_text,
    candidate_set_hash,
    candidates_for,
    check_vocabulary_refs,
    crosswalk_data,
    effective_configs,
    invalid,
    is_attribute_kind,
    is_usable,
    latest_proposal_request,
    lineage_revision_ids,
    master_invalid,
    open_proposal,
    opt_id,
    parse_attrs,
    parse_rule_issuer,
    parse_timestamp,
    preparing_revision,
    profile_context,
    profile_from_version,
    record_master_version,
    record_pick,
    resolve_alias,
    rule_brand,
    save_master,
    sku_data,
    sku_identity,
    style_data,
    sync_crosswalk_exception,
    ts,
)
from masters.goods_models import ConfigVersion
from masters.goods_models import MasterVersion as MasterVersionRow
from masters.models import Brand, Store

CROSSWALK_MANAGE = "crosswalk.manage"
CROSSWALK_PROPOSE = "crosswalk.propose"
RETIRE_ACTION = "master.retire"

#: Grants that may read product masters (tenant masters; brand grants narrow them).
READ_ACTIONS = frozenset(
    {
        MANAGE_ACTION,
        PROPOSE_ACTION,
        CROSSWALK_MANAGE,
        CROSSWALK_PROPOSE,
        "identity.resolve",
        "pt.view",
        "pt.prepare",
        "pt.prepare.opening",
        "stock.view",
        "receive.arrival",
        "stock.accept",
        "transfer.allocate",
    }
)

#: Receiver, preparer and allocator grants that may look codes up and pick (E090/E091).
LOOKUP_ACTIONS = frozenset(
    {
        MANAGE_ACTION,
        PROPOSE_ACTION,
        "identity.resolve",
        "receive.arrival",
        "stock.accept",
        "pt.prepare",
        "pt.prepare.opening",
        "transfer.allocate",
        "transfer.move",
        "count.run",
        "count.review",
        "label.print",
    }
)

CONTEXT_KEYS = frozenset(
    {"site_id", "issuer_key", "alias_type", "as_of", "profile_version_id", "subject_revision_id"}
)


def not_found(what: str = "record") -> Refusal:
    return Refusal("NOT_FOUND", f"That {what} was not found.")


# -- access helpers ------------------------------------------------------------


def holds_any(access: AccessContext, actions: frozenset[str] | set[str]) -> bool:
    """Recorded, so a command replays it before commit."""
    return access.holds_any(sorted(actions))


def require_any(access: AccessContext, actions: frozenset[str] | set[str]) -> None:
    if not holds_any(access, actions):
        raise Refusal("ACTION_DENIED", "You do not have permission for this action.")


def can_any(
    access: AccessContext,
    actions: frozenset[str] | set[str],
    *,
    site_id: int | None = None,
    brand_id: int | None = None,
) -> bool:
    return any(access.can(a, site_id=site_id, brand_id=brand_id) for a in actions)


def brand_scope(
    access: AccessContext, actions: frozenset[str] | set[str], site_id: int | None = None
) -> set[int] | None:
    """Brands the grants holding ``actions`` cover; ``None`` means every brand."""
    brands: set[int] = set()
    for grant in access.grants:
        if not grant.actions & actions:
            continue
        if site_id is not None and not access.reaches_site(grant, site_id):
            continue
        if grant.scope_kind == "brand" and grant.brand_id is not None:
            brands.add(grant.brand_id)
        elif grant.scope_kind == "sbu" and grant.sbu_brand_id is not None:
            brands.add(grant.sbu_brand_id)
        else:
            return None
    return brands


def reaches_brand_any(
    access: AccessContext, actions: frozenset[str] | set[str], brand_id: int
) -> bool:
    """A product master is a brand record held at no site: any grant reaching the brand counts."""
    return access.can_reach_brand(actions, brand_id)


def alias_reach_q(access: AccessContext, actions: frozenset[str] | set[str]) -> Q:
    """Aliases a reader may see, grant by grant: its brands, and a site alias only at its sites.

    A brand-wide alias is a product master (brand only). A site alias is a record at
    that site for that brand, so one grant must reach both - a site from one grant and
    a brand from another never combine.

    A grant that narrows neither dimension - tenant scope, every brand - reaches
    every alias there is, and that has to be said outright. An unrestricted
    grant builds an *empty* condition, and an empty ``Q`` means "add nothing" to
    Django, not "match everything": OR-ing it onto the starting "nothing" leaves
    the nothing standing, so the widest reader in the business used to see no
    alias at all while a site-scoped one saw plenty.
    """
    reach = Q(pk__in=[])
    for grant in access.grants:
        if not grant.actions & actions:
            continue
        limit = access.brand_limit(grant)
        brand_q = Q() if limit is None else Q(sku__style__brand_id=limit)
        if grant.scope_kind in ("tenant", "brand"):
            site_q = Q()
        else:
            sites = sorted(
                s
                for s in Store.objects.values_list("pk", flat=True)
                if access.reaches_site(grant, s)
            )
            site_q = Q(site__isnull=True) | Q(site_id__in=sites)
        clause = brand_q & site_q
        if not clause:
            return Q()
        reach |= clause
    return reach


def alias_visible(access: AccessContext, row: Any) -> bool:
    brand_id = row.sku.style.brand_id
    return any(
        grant.actions & READ_ACTIONS
        and access.reaches_brand(grant, brand_id)
        and (row.site_id is None or access.reaches_site(grant, row.site_id))
        for grant in access.grants
    )


def site_scope(access: AccessContext, actions: frozenset[str] | set[str]) -> set[int] | None:
    sites: set[int] = set()
    for action in actions:
        if not any(action in grant.actions for grant in access.grants):
            continue
        granted = access.site_reach(action)
        if granted is None:
            return None
        sites |= granted
    return sites


def require_owner(access: AccessContext, brand_id: int | None) -> None:
    """Retirement is an action (``master.retire``), so narrowed and partial grants count."""
    if not holds_any(access, {RETIRE_ACTION}):
        raise Refusal("ACTION_DENIED", "Only a company owner can retire product masters.")
    if not access.can(RETIRE_ACTION, brand_id=brand_id):
        raise not_found()


def proposer_targets(access: AccessContext, kind: str) -> list[uuid.UUID]:
    keys = MasterVersionRow.objects.filter(
        tenant_id=access.tenant_id, kind=kind, revision=1, actor_id=access.human_id
    ).values_list("target_key", flat=True)
    out: list[uuid.UUID] = []
    for key in keys:
        try:
            out.append(uuid.UUID(key))
        except ValueError:
            continue
    return out


def pending_filter(access: AccessContext, kind: str, manage: str) -> Q:
    if holds_any(access, {manage}):
        return Q()
    return ~Q(governance_state=GovernanceState.PENDING) | Q(pk__in=proposer_targets(access, kind))


def pending_visible(access: AccessContext, kind: str, manage: str, row: Any) -> bool:
    if row.governance_state != GovernanceState.PENDING or holds_any(access, {manage}):
        return True
    return row.pk in set(proposer_targets(access, kind))


def originating_site(row: Any) -> int | None:
    if getattr(row, "originating_revision_id", None) is None:
        return None
    site: int | None = (
        DraftRevision.objects.filter(pk=row.originating_revision_id)
        .values_list("document__site_id", flat=True)
        .first()
    )
    return site


def edit_authority(
    access: AccessContext, row: Any, *, brand_id: int | None, manage: str, propose: str
) -> None:
    """Managers edit any live master; a preparer edits only a proposal in their scope."""
    if access.can(manage, brand_id=brand_id):
        return
    if row.governance_state == GovernanceState.PENDING and holds_any(access, {propose}):
        site = originating_site(row)
        if access.can(propose, site_id=site, brand_id=brand_id):
            return
        raise not_found()
    if holds_any(access, {manage, propose}):
        raise (
            not_found()
            if holds_any(access, {manage})
            else Refusal(
                "ACTION_DENIED", "Only the product master owner can change a confirmed master."
            )
        )
    raise Refusal("ACTION_DENIED", "You do not have permission for this action.")


def creation_mode(access: AccessContext, *, manage: str, propose: str, originating: Any) -> str:
    if holds_any(access, {manage}):
        if originating is not None:
            raise invalid(
                "originating_revision_id is only sent when proposing a master.",
                field="originating_revision_id",
            )
        return "direct"
    if holds_any(access, {propose}):
        if originating in (None, ""):
            raise Refusal(
                "INVALID_REQUEST",
                "A proposal must cite the PT draft revision being prepared.",
                issues=[
                    issue(
                        "REQUIRED",
                        "originating_revision_id is required",
                        field="originating_revision_id",
                    )
                ],
            )
        return "propose"
    raise Refusal("ACTION_DENIED", "You do not have permission to create this master.")


def originating_revision(
    access: AccessContext, raw: Any, brand_id: int | None, site_id: int | None = None
) -> tuple[DraftRevision, int]:
    revision_id = parse_uuid(raw, "originating_revision_id")
    revision = (
        DraftRevision.objects.select_related("document")
        .filter(tenant_id=access.tenant_id, pk=revision_id, document__isnull=False)
        .first()
    )
    # A booking saved with no destination yet (GSA-T05) is never an originating
    # revision: identity choices bind to a PT revision or scan, which has a site.
    if revision is None or revision.document is None or revision.document.site_id is None:
        raise not_found("draft revision")
    document_site = revision.document.site_id
    access.require(PROPOSE_ACTION, site_id=document_site, brand_id=brand_id)
    if site_id is not None and site_id != document_site:
        access.require(PROPOSE_ACTION, site_id=site_id, brand_id=brand_id)
    return revision, document_site


# -- paging --------------------------------------------------------------------


def page_rows(queryset: QuerySet[Any], params: dict[str, str]) -> tuple[list[Any], str | None]:
    offset = decode_cursor(params.get("cursor"))
    limit = page_limit(params)
    rows = list(queryset[offset : offset + limit + 1])
    return rows[:limit], (encode_cursor(offset + limit) if len(rows) > limit else None)


def query_text(params: dict[str, str]) -> str:
    text = params.get("q", "").strip()
    if len(text) > 100:
        raise invalid("q must be at most 100 characters.", field="q")
    return text


def list_scope(
    access: AccessContext, params: dict[str, str]
) -> tuple[set[int] | None, int | None, int | None]:
    """Validate ListQuery scope filters: (brand scope, brand filter, site filter)."""
    require_any(access, READ_ACTIONS)
    brands = brand_scope(access, READ_ACTIONS)
    brand_id = parse_int_id(params["brand_id"], "brand_id") if params.get("brand_id") else None
    if brand_id is not None and brands is not None and brand_id not in brands:
        raise not_found()
    site_id = parse_int_id(params["site_id"], "site_id") if params.get("site_id") else None
    if site_id is not None:
        sites = site_scope(access, READ_ACTIONS)
        if sites is not None and site_id not in sites:
            raise not_found()
    if params.get("sbu_id"):
        parse_uuid(params["sbu_id"], "sbu_id")
    return brands, brand_id, site_id


def allowed_actions(
    access: AccessContext, row: Any, *, brand_id: int | None, manage: str, propose: str
) -> list[str]:
    actions: set[str] = set()
    if row.governance_state != GovernanceState.RETIRED:
        if access.can(manage, brand_id=brand_id) or (
            row.governance_state == GovernanceState.PENDING and holds_any(access, {propose})
        ):
            actions.add("update")
        if row.governance_state == GovernanceState.EFFECTIVE and access.can(
            RETIRE_ACTION, brand_id=brand_id
        ):
            actions.add("retire")
    return sorted(actions)


def create_result(kind: str, resource: dict[str, Any], row: Any) -> dict[str, Any]:
    request = latest_proposal_request(kind, row.pk) if row.originating_revision_id else None
    return {
        "master": resource,
        "governance_state": row.governance_state,
        "originating_revision_id": opt_id(row.originating_revision_id),
        "approval_request_id": opt_id(request.pk if request is not None else None),
    }


def retirement_target(row: Any) -> None:
    if row.governance_state == GovernanceState.PENDING:
        raise Refusal(
            "RETIREMENT_BLOCKED",
            "A pending proposal is decided through its approval, not retired.",
        )
    if row.governance_state == GovernanceState.RETIRED or getattr(row, "retired_at", None):
        raise Refusal("RETIREMENT_BLOCKED", "This master is already retired.")


def apply_retirement(run: CommandRun, row: Any, effective_at: datetime) -> None:
    row.retired_at = effective_at
    if effective_at <= run.now:
        row.governance_state = GovernanceState.RETIRED
    row.revision += 1
    row.save(update_fields=["retired_at", "governance_state", "revision"])


def retire_body(request: Request) -> tuple[str, datetime, dict[str, Any]]:
    body = business_body(
        request.data, {"reason_code", "effective_at"}, required=["reason_code", "effective_at"]
    )
    reason = bounded_text(body["reason_code"], "reason_code", 60)
    effective_at = parse_timestamp(body["effective_at"], "effective_at")
    return reason, effective_at, {"reason_code": reason, "effective_at": ts(effective_at)}


def _require_config_in_force(tenant_id: uuid.UUID, version_id: uuid.UUID) -> None:
    """A mapping cites a configuration version that exists and is in force now."""
    from core.commands import database_now
    from masters.goods_config import in_force

    version = ConfigVersion.objects.filter(tenant_id=tenant_id, pk=version_id).first()
    if version is None:
        raise not_found("configuration version")
    if not in_force(version, database_now()):
        raise master_invalid(
            "That configuration version is not in force.", field="config_version_id"
        )


def identity_profile(tenant_id: uuid.UUID, version_id: uuid.UUID, now: datetime) -> IdentityProfile:
    version = ConfigVersion.objects.filter(tenant_id=tenant_id, pk=version_id).first()
    if version is None:
        raise not_found("identity profile")
    profile = profile_from_version(version)
    if profile is None:
        raise master_invalid(
            "That configuration is not an identity profile.", field="profile_version_id"
        )
    if version.pk not in {v.pk for v in effective_configs(tenant_id, "identity_profile", now)}:
        raise master_invalid("That identity profile is not effective.", field="profile_version_id")
    return profile


# -- OpenAPI response shapes ---------------------------------------------------
#
# These views hand-build dict responses rather than run a DRF serializer, so
# drf-spectacular sees nothing without this. Every route in this module is a
# goods-v1 path, including ``skus/lookup``, which used to share its path
# template with the legacy registry lookup and so could not be described (#303);
# it now answers at ``/api/goods-v1/masters/skus/lookup`` alone.
# The envelopes are repeated rather than imported so this module documents its
# own contract, as ``masters/goods_views.py`` does for its own.

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}


def _dto_response(data_schema: dict[str, Any], description: str) -> dict[str, Any]:
    """The ``ResourceDTO<...>`` envelope of design §6.1 around one ``data`` schema."""
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


def _page_response(item_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": "Page<T> (design §6.1).",
        "properties": {
            "items": {"type": "array", "items": item_schema},
            "next_cursor": {"type": "string", "nullable": True},
        },
    }


def _create_response(resource: dict[str, Any], description: str) -> dict[str, Any]:
    """A proposable master's create answer: the resource plus its governance state."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "master": resource,
            "governance_state": {
                "type": "string",
                "enum": ["pending", "effective", "retired"],
            },
            "originating_revision_id": {"type": "string", "nullable": True},
            "approval_request_id": {"type": "string", "nullable": True},
        },
    }


ATTRS_SCHEMA = {
    "type": "array",
    "description": (
        "AttributeValues: exactly one of vocabulary_value_id, supplied_text or "
        "unknown per field. An omitted size stays unknown; an explicit Free Size "
        "is a mapped vocabulary value, never a blank."
    ),
    "items": {
        "type": "object",
        "properties": {
            "field_id": {"type": "string"},
            "vocabulary_value_id": {"type": "string", "nullable": True},
            "supplied_text": {"type": "string", "nullable": True},
            "unknown": {"type": "boolean"},
        },
    },
}

STYLE_DATA = {
    "type": "object",
    "properties": {
        "brand_id": {"type": "string"},
        "style_code": {"type": "string"},
        "profile_family": {"type": "string"},
        "attrs": ATTRS_SCHEMA,
    },
}

SKU_DATA = {
    "type": "object",
    "properties": {
        "style_id": {"type": "string"},
        "profile_version_id": {"type": "string", "nullable": True},
        "attrs": ATTRS_SCHEMA,
    },
}

ALIAS_DATA = {
    "type": "object",
    "properties": {
        "sku_id": {"type": "string"},
        "issuer_key": {"type": "string"},
        "alias_type": {"type": "string", "enum": ["barcode", "vendor_code", "generated"]},
        "value": {"type": "string"},
        "site_id": {"type": "string", "nullable": True},
        "effective_from": {"type": "string"},
        "effective_to": {"type": "string", "nullable": True},
    },
}

CROSSWALK_DATA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "issuer_key": {"type": "string"},
        "source_key": {"type": "string"},
        "target_key": {"type": "string"},
        "config_version_id": {"type": "string", "nullable": True},
    },
}

IDENTITY_RESOLUTION = {
    "type": "object",
    "description": (
        "IdentityResolutionDTO (design §6.1): every authorised candidate and the "
        "hash of that candidate set. `ambiguous` and `unknown` never choose a SKU."
    ),
    "properties": {
        "result": {
            "type": "string",
            "enum": ["resolved", "ambiguous", "unknown"],
        },
        "candidate_hash": {"type": "string"},
        "candidates": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "chosen_sku_id": {"type": "string", "nullable": True},
        "issues": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    },
}

STYLE_RESOURCE = _dto_response(STYLE_DATA, "ResourceDTO<MasterPayload.style>.")
SKU_RESOURCE = _dto_response(SKU_DATA, "ResourceDTO<MasterPayload.sku>.")
ALIAS_RESOURCE = _dto_response(ALIAS_DATA, "ResourceDTO<MasterPayload.alias>.")
CROSSWALK_RESOURCE = _dto_response(CROSSWALK_DATA, "ResourceDTO<MasterPayload.crosswalk>.")

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


# -- styles (E041-E045) ----------------------------------------------------------


def style_resource(access: AccessContext, row: Style) -> dict[str, Any]:
    return resource_dto(
        id=row.pk,
        data=style_data(row),
        revision=row.revision,
        state=row.governance_state,
        context={"brand_id": row.brand_id},
        allowed_actions=allowed_actions(
            access, row, brand_id=row.brand_id, manage=MANAGE_ACTION, propose=PROPOSE_ACTION
        ),
    )


def visible_style(access: AccessContext, pk: uuid.UUID) -> Style:
    require_any(access, READ_ACTIONS)
    row = Style.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    brands = brand_scope(access, READ_ACTIONS)
    if (
        row is None
        or (brands is not None and row.brand_id not in brands)
        or not pending_visible(access, "style", MANAGE_ACTION, row)
    ):
        raise not_found("style")
    return row


class StyleListCreateView(GoodsAPIView):
    """E041 list and E042 create."""

    @extend_schema(responses=_responses(200, _page_response(STYLE_RESOURCE), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, LIST_QUERY_KEYS | {"style_code", "profile_family"})
        brands, brand_id, _site = list_scope(access, params)
        queryset = Style.objects.filter(tenant_id=access.tenant_id).filter(
            pending_filter(access, "style", MANAGE_ACTION)
        )
        if brands is not None:
            queryset = queryset.filter(brand_id__in=sorted(brands))
        if brand_id is not None:
            queryset = queryset.filter(brand_id=brand_id)
        if params.get("style_code"):
            queryset = queryset.filter(style_code=params["style_code"])
        if params.get("profile_family"):
            queryset = queryset.filter(profile_family=params["profile_family"])
        text = query_text(params)
        if text:
            queryset = queryset.filter(style_code__icontains=text)
        rows, cursor = page_rows(queryset.order_by("style_code", "id"), params)
        return Response(page([style_resource(access, row) for row in rows], cursor))

    @extend_schema(
        responses=_responses(
            201, _create_response(STYLE_RESOURCE, "Created style."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"brand_id", "style_code", "profile_family", "attrs", "originating_revision_id"},
            required=["brand_id", "style_code", "profile_family"],
        )
        brand_id = parse_int_id(body["brand_id"], "brand_id")
        style_code = bounded_text(body["style_code"], "style_code", 120)
        family = bounded_text(body["profile_family"], "profile_family", 60)
        attrs = parse_attrs(body.get("attrs"))
        mode = creation_mode(
            access,
            manage=MANAGE_ACTION,
            propose=PROPOSE_ACTION,
            originating=body.get("originating_revision_id"),
        )
        if not Brand.objects.filter(pk=brand_id).exists():
            raise not_found("brand")
        revision: DraftRevision | None = None
        site_id: int | None = None
        if mode == "direct":
            access.require(MANAGE_ACTION, brand_id=brand_id)
        else:
            revision, site_id = originating_revision(
                access, body["originating_revision_id"], brand_id
            )
        clean = {
            "brand_id": brand_id,
            "style_code": style_code,
            "profile_family": family,
            "attrs": attrs,
            "originating_revision_id": opt_id(revision.pk if revision else None),
        }

        def handler(run: CommandRun) -> CommandResult:
            if revision is not None:
                preparing_revision(run, revision)
            problems = check_vocabulary_refs(run.tenant_id, attrs, run.now)
            if problems:
                raise Refusal(
                    "MASTER_INVALID",
                    "The style attributes are not valid.",
                    status=422,
                    issues=problems,
                )
            if Style.objects.filter(
                tenant_id=run.tenant_id,
                brand_id=brand_id,
                profile_family=family,
                style_code=style_code,
            ).exists():
                raise Refusal("MASTER_CONFLICT", "That style already exists for this brand.")
            row = Style(
                tenant_id=run.tenant_id,
                brand_id=brand_id,
                style_code=style_code,
                profile_family=family,
                attrs=attrs,
                governance_state=GovernanceState.PENDING if revision else GovernanceState.EFFECTIVE,
                originating_revision_id=revision.pk if revision else None,
            )
            if revision is not None:
                open_proposal(run, kind="style", row=row, site_id=site_id, brand_id=brand_id)
            save_master(row, conflict="That style already exists for this brand.")
            record_master_version(run, "style", row)
            run.audit_after = style_data(row)
            return CommandResult(resource_type="style", resource_id=str(row.pk), status_code=201)

        result = self.run_command(
            request,
            access=access,
            action="masters.style.create",
            meta=meta,
            business_input=clean,
            handler=handler,
            subject_key=f"style:{brand_id}:{style_code}"[:100],
            site_id=site_id,
        )
        row = Style.objects.get(pk=str(result.resource_id))
        return Response(
            create_result("style", style_resource(access, row), row), status=result.status_code
        )


class StyleDetailView(GoodsAPIView):
    """E043 detail and E044 correction."""

    @extend_schema(
        operation_id="goods_v1_masters_styles_detail",
        responses=_responses(200, STYLE_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        return Response(style_resource(access, visible_style(access, pk)))

    @extend_schema(responses=_responses(200, STYLE_RESOURCE, _WRITE_REFUSALS))
    def patch(self, request: Request, pk: uuid.UUID) -> Response:  # noqa: C901 - the contract's ordered refusal steps
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"brand_id", "style_code", "profile_family", "attrs"})
        if not body:
            raise invalid("Send at least one field to change.")
        current = visible_style(access, pk)
        edit_authority(
            access, current, brand_id=current.brand_id, manage=MANAGE_ACTION, propose=PROPOSE_ACTION
        )
        clean: dict[str, Any] = {}
        if "brand_id" in body:
            clean["brand_id"] = parse_int_id(body["brand_id"], "brand_id")
            if not Brand.objects.filter(pk=clean["brand_id"]).exists():
                raise not_found("brand")
            if not reaches_brand_any(access, {MANAGE_ACTION, PROPOSE_ACTION}, clean["brand_id"]):
                raise not_found("brand")
        if "style_code" in body:
            clean["style_code"] = bounded_text(body["style_code"], "style_code", 120)
        if "profile_family" in body:
            clean["profile_family"] = bounded_text(body["profile_family"], "profile_family", 60)
        if "attrs" in body:
            clean["attrs"] = parse_attrs(body["attrs"])

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(LockRank.DOCUMENT, Style.objects.filter(tenant_id=run.tenant_id, pk=pk))[
                0
            ]
            check_revision(meta.expected_revision, row.revision)
            if row.governance_state == GovernanceState.RETIRED or row.retired_at is not None:
                raise master_invalid("A retired style cannot be changed.")
            run.audit_before = style_data(row)
            changes_identity = any(
                name in clean and clean[name] != getattr(row, name)
                for name in ("brand_id", "style_code", "profile_family")
            )
            if changes_identity and ProductSku.objects.filter(style_id=row.pk).exists():
                raise master_invalid(
                    "This style already has SKUs; its brand, family and code define them. "
                    "Create a new style instead."
                )
            for name, value in clean.items():
                setattr(row, name, value)
            problems = check_vocabulary_refs(run.tenant_id, list(row.attrs or []), run.now)
            if "attrs" in clean and problems:
                raise Refusal(
                    "MASTER_INVALID",
                    "The style attributes are not valid.",
                    status=422,
                    issues=problems,
                )
            if (
                Style.objects.filter(
                    tenant_id=run.tenant_id,
                    brand_id=row.brand_id,
                    profile_family=row.profile_family,
                    style_code=row.style_code,
                )
                .exclude(pk=row.pk)
                .exists()
            ):
                raise Refusal("MASTER_CONFLICT", "That style already exists for this brand.")
            row.revision += 1
            save_master(
                row,
                conflict="That style already exists for this brand.",
                update_fields=["brand_id", "style_code", "profile_family", "attrs", "revision"],
            )
            record_master_version(run, "style", row)
            if row.governance_state == GovernanceState.PENDING:
                open_proposal(
                    run, kind="style", row=row, site_id=originating_site(row), brand_id=row.brand_id
                )
            run.audit_after = style_data(row)
            return CommandResult(resource_type="style", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.style.update",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"style:{pk}",
        )
        return Response(style_resource(access, Style.objects.get(pk=pk)), status=result.status_code)


class StyleRetireView(GoodsAPIView):
    """E045 retirement (company owner, step-up)."""

    @extend_schema(responses=_responses(200, STYLE_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        reason, effective_at, clean = retire_body(request)
        current = visible_style(access, pk)
        require_owner(access, current.brand_id)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(LockRank.DOCUMENT, Style.objects.filter(tenant_id=run.tenant_id, pk=pk))[
                0
            ]
            check_revision(meta.expected_revision, row.revision)
            retirement_target(row)
            if (
                ProductSku.objects.filter(style_id=row.pk)
                .exclude(governance_state=GovernanceState.RETIRED)
                .exists()
            ):
                raise Refusal("RETIREMENT_BLOCKED", "Retire this style's SKUs first.")
            run.audit_before = style_data(row)
            apply_retirement(run, row, effective_at)
            record_master_version(
                run, "style", row, retired=True, reason_code=reason, effective_from=effective_at
            )
            return CommandResult(resource_type="style", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.style.retire",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"style:{pk}",
        )
        return Response(style_resource(access, Style.objects.get(pk=pk)), status=result.status_code)


# -- SKUs (E046-E050) ------------------------------------------------------------


def sku_resource(access: AccessContext, row: ProductSku) -> dict[str, Any]:
    brand_id = row.style.brand_id
    return resource_dto(
        id=row.pk,
        data=sku_data(row),
        revision=row.revision,
        state=row.governance_state,
        context={"brand_id": brand_id},
        allowed_actions=allowed_actions(
            access, row, brand_id=brand_id, manage=MANAGE_ACTION, propose=PROPOSE_ACTION
        ),
    )


def visible_sku(access: AccessContext, pk: uuid.UUID) -> ProductSku:
    require_any(access, READ_ACTIONS)
    row = (
        ProductSku.objects.select_related("style").filter(tenant_id=access.tenant_id, pk=pk).first()
    )
    brands = brand_scope(access, READ_ACTIONS)
    if (
        row is None
        or (brands is not None and row.style.brand_id not in brands)
        or not pending_visible(access, "sku", MANAGE_ACTION, row)
    ):
        raise not_found("SKU")
    return row


class SkuListCreateView(GoodsAPIView):
    """E046 list and E047 create."""

    @extend_schema(responses=_responses(200, _page_response(SKU_RESOURCE), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, LIST_QUERY_KEYS | {"style_id", "profile_version_id"})
        brands, brand_id, _site = list_scope(access, params)
        queryset = (
            ProductSku.objects.select_related("style")
            .filter(tenant_id=access.tenant_id)
            .filter(pending_filter(access, "sku", MANAGE_ACTION))
        )
        if brands is not None:
            queryset = queryset.filter(style__brand_id__in=sorted(brands))
        if brand_id is not None:
            queryset = queryset.filter(style__brand_id=brand_id)
        if params.get("style_id"):
            queryset = queryset.filter(style_id=parse_uuid(params["style_id"], "style_id"))
        if params.get("profile_version_id"):
            queryset = queryset.filter(
                identity_profile_id=parse_uuid(params["profile_version_id"], "profile_version_id")
            )
        text = query_text(params)
        if text:
            queryset = queryset.filter(style__style_code__icontains=text)
        rows, cursor = page_rows(queryset.order_by("style__style_code", "id"), params)
        return Response(page([sku_resource(access, row) for row in rows], cursor))

    @extend_schema(
        responses=_responses(201, _create_response(SKU_RESOURCE, "Created SKU."), _WRITE_REFUSALS)
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"style_id", "profile_version_id", "attrs", "originating_revision_id"},
            required=["style_id", "profile_version_id"],
        )
        style_id = parse_uuid(body["style_id"], "style_id")
        profile_id = parse_uuid(body["profile_version_id"], "profile_version_id")
        attrs = parse_attrs(body.get("attrs"))
        mode = creation_mode(
            access,
            manage=MANAGE_ACTION,
            propose=PROPOSE_ACTION,
            originating=body.get("originating_revision_id"),
        )
        require_any(access, READ_ACTIONS)
        style = Style.objects.filter(tenant_id=access.tenant_id, pk=style_id).first()
        if style is None:
            raise not_found("style")
        revision: DraftRevision | None = None
        site_id: int | None = None
        if mode == "direct":
            access.require(MANAGE_ACTION, brand_id=style.brand_id)
        else:
            revision, site_id = originating_revision(
                access, body["originating_revision_id"], style.brand_id
            )
            lineage = lineage_revision_ids(access.tenant_id, revision.pk)
            if style.governance_state == GovernanceState.PENDING and (
                style.originating_revision_id not in set(lineage)
            ):
                raise not_found("style")
        clean = {
            "style_id": str(style_id),
            "profile_version_id": str(profile_id),
            "attrs": attrs,
            "originating_revision_id": opt_id(revision.pk if revision else None),
        }

        def handler(run: CommandRun) -> CommandResult:
            lineage: list[uuid.UUID] = []
            if revision is not None:
                preparing_revision(run, revision)
                lineage = lineage_revision_ids(run.tenant_id, revision.pk)
            locked_style = Style.objects.get(pk=style_id)
            if not is_usable(locked_style, run.now, lineage):
                if locked_style.governance_state == GovernanceState.PENDING:
                    raise master_invalid("Confirm the style first.", field="style_id")
                raise master_invalid("That style is retired.", field="style_id")
            profile = identity_profile(run.tenant_id, profile_id, run.now)
            key, stored = sku_identity(run.tenant_id, locked_style, profile, attrs, run.now)
            conflict = "A SKU with exactly these identity attributes already exists."
            if ProductSku.objects.filter(tenant_id=run.tenant_id, identity_key=key).exists():
                raise Refusal("MASTER_CONFLICT", conflict)
            row = ProductSku(
                tenant_id=run.tenant_id,
                style_id=locked_style.pk,
                identity_key=key,
                identity_profile_id=profile.version_id,
                attrs=stored,
                governance_state=GovernanceState.PENDING if revision else GovernanceState.EFFECTIVE,
                originating_revision_id=revision.pk if revision else None,
            )
            if revision is not None:
                open_proposal(
                    run, kind="sku", row=row, site_id=site_id, brand_id=locked_style.brand_id
                )
            save_master(row, conflict=conflict)
            record_master_version(run, "sku", row)
            run.audit_after = sku_data(row)
            return CommandResult(resource_type="sku", resource_id=str(row.pk), status_code=201)

        result = self.run_command(
            request,
            access=access,
            action="masters.sku.create",
            meta=meta,
            business_input=clean,
            handler=handler,
            subject_key=f"sku:{style_id}",
            site_id=site_id,
        )
        row = ProductSku.objects.select_related("style").get(pk=str(result.resource_id))
        return Response(
            create_result("sku", sku_resource(access, row), row), status=result.status_code
        )


class SkuDetailView(GoodsAPIView):
    """E048 detail and E049 correction (never an identity change)."""

    @extend_schema(
        operation_id="goods_v1_masters_skus_detail",
        responses=_responses(200, SKU_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        return Response(sku_resource(access, visible_sku(access, pk)))

    @extend_schema(responses=_responses(200, SKU_RESOURCE, _WRITE_REFUSALS))
    def patch(self, request: Request, pk: uuid.UUID) -> Response:  # noqa: C901 - the contract's ordered refusal steps
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"style_id", "profile_version_id", "attrs"})
        if not body:
            raise invalid("Send at least one field to change.")
        current = visible_sku(access, pk)
        edit_authority(
            access,
            current,
            brand_id=current.style.brand_id,
            manage=MANAGE_ACTION,
            propose=PROPOSE_ACTION,
        )
        clean: dict[str, Any] = {}
        if "style_id" in body:
            clean["style_id"] = str(parse_uuid(body["style_id"], "style_id"))
        if "profile_version_id" in body:
            clean["profile_version_id"] = str(
                parse_uuid(body["profile_version_id"], "profile_version_id")
            )
        if "attrs" in body:
            clean["attrs"] = parse_attrs(body["attrs"])

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(
                LockRank.DOCUMENT,
                ProductSku.objects.select_related("style").filter(tenant_id=run.tenant_id, pk=pk),
            )[0]
            check_revision(meta.expected_revision, row.revision)
            if row.governance_state == GovernanceState.RETIRED or row.retired_at is not None:
                raise master_invalid("A retired SKU cannot be changed.")
            if clean.get("style_id", str(row.style_id)) != str(row.style_id):
                raise master_invalid(
                    "Identity-defining SKU changes require a new SKU, not an edit.",
                    field="style_id",
                )
            profile_raw = clean.get("profile_version_id") or opt_id(row.identity_profile_id)
            if profile_raw is None:
                raise master_invalid(
                    "This SKU has no identity profile to validate against.",
                    field="profile_version_id",
                )
            profile = identity_profile(run.tenant_id, uuid.UUID(profile_raw), run.now)
            attrs = clean["attrs"] if "attrs" in clean else parse_attrs(list(row.attrs or []))
            key, stored = sku_identity(run.tenant_id, row.style, profile, attrs, run.now)
            if key != row.identity_key:
                raise master_invalid(
                    "Identity-defining SKU changes require a new SKU, not an edit.", field="attrs"
                )
            run.audit_before = sku_data(row)
            row.attrs = stored
            row.identity_profile_id = profile.version_id
            row.revision += 1
            save_master(
                row,
                conflict="A SKU with exactly these identity attributes already exists.",
                update_fields=["attrs", "identity_profile_id", "revision"],
            )
            record_master_version(run, "sku", row)
            if row.governance_state == GovernanceState.PENDING:
                open_proposal(
                    run,
                    kind="sku",
                    row=row,
                    site_id=originating_site(row),
                    brand_id=row.style.brand_id,
                )
            run.audit_after = sku_data(row)
            return CommandResult(resource_type="sku", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.sku.update",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"sku:{pk}",
        )
        row = ProductSku.objects.select_related("style").get(pk=pk)
        return Response(sku_resource(access, row), status=result.status_code)


class SkuRetireView(GoodsAPIView):
    """E050 retirement: refused while the SKU still has stock."""

    @extend_schema(responses=_responses(200, SKU_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        reason, effective_at, clean = retire_body(request)
        current = visible_sku(access, pk)
        require_owner(access, current.style.brand_id)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(
                LockRank.DOCUMENT, ProductSku.objects.filter(tenant_id=run.tenant_id, pk=pk)
            )[0]
            check_revision(meta.expected_revision, row.revision)
            retirement_target(row)
            positions = apps.get_model("stockledger", "Position")._default_manager
            if positions.filter(
                tenant_id=run.tenant_id, sku_id=row.pk, boundary__in=["physical", "transit"]
            ).exists():
                raise Refusal(
                    "RETIREMENT_BLOCKED", "This SKU still has stock on hand or in transit."
                )
            run.audit_before = sku_data(row)
            apply_retirement(run, row, effective_at)
            record_master_version(
                run, "sku", row, retired=True, reason_code=reason, effective_from=effective_at
            )
            return CommandResult(resource_type="sku", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.sku.retire",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"sku:{pk}",
        )
        row = ProductSku.objects.select_related("style").get(pk=pk)
        return Response(sku_resource(access, row), status=result.status_code)


# -- aliases (E051-E055) -----------------------------------------------------------


def alias_resource(access: AccessContext, row: SkuAlias) -> dict[str, Any]:
    brand_id = row.sku.style.brand_id
    return resource_dto(
        id=row.pk,
        data=alias_data(row),
        revision=row.revision,
        state=row.governance_state,
        context={"site_id": row.site_id, "brand_id": brand_id},
        allowed_actions=allowed_actions(
            access, row, brand_id=brand_id, manage=MANAGE_ACTION, propose=PROPOSE_ACTION
        ),
    )


def visible_alias(access: AccessContext, pk: uuid.UUID) -> SkuAlias:
    require_any(access, READ_ACTIONS)
    row = (
        SkuAlias.objects.select_related("sku__style")
        .filter(tenant_id=access.tenant_id, pk=pk)
        .first()
    )
    if (
        row is None
        or not alias_visible(access, row)
        or not pending_visible(access, "alias", MANAGE_ACTION, row)
    ):
        raise not_found("alias")
    return row


def overlapping_aliases(
    tenant_id: uuid.UUID,
    *,
    sku_id: uuid.UUID,
    issuer_key: str,
    alias_type: str,
    value: str,
    site_id: int | None,
    effective_from: datetime,
    effective_to: datetime | None,
) -> QuerySet[SkuAlias]:
    queryset = (
        SkuAlias.objects.filter(
            tenant_id=tenant_id,
            sku_id=sku_id,
            issuer_key=issuer_key,
            alias_type=alias_type,
            value=value,
        )
        .exclude(governance_state=GovernanceState.RETIRED)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=effective_from))
    )
    queryset = (
        queryset.filter(site__isnull=True) if site_id is None else queryset.filter(site_id=site_id)
    )
    if effective_to is not None:
        queryset = queryset.filter(effective_from__lt=effective_to)
    return queryset


ALIAS_CONFLICT = "This alias is already assigned to that SKU in an overlapping period."


class AliasListCreateView(GoodsAPIView):
    """E051 list and E052 create (supplied or generated variant)."""

    @extend_schema(responses=_responses(200, _page_response(ALIAS_RESOURCE), _READ_REFUSALS))
    def get(self, request: Request) -> Response:  # noqa: C901 - the contract's ordered refusal steps
        access = self.access(request)
        params = check_query(
            request,
            LIST_QUERY_KEYS
            | {"sku_id", "issuer_key", "alias_type", "value", "effective_from", "effective_to"},
        )
        brands, brand_id, site_id = list_scope(access, params)
        queryset = (
            SkuAlias.objects.select_related("sku__style")
            .filter(tenant_id=access.tenant_id)
            .filter(pending_filter(access, "alias", MANAGE_ACTION))
        )
        queryset = queryset.filter(alias_reach_q(access, READ_ACTIONS))
        if brand_id is not None:
            queryset = queryset.filter(sku__style__brand_id=brand_id)
        if site_id is not None:
            queryset = queryset.filter(site_id=site_id)
        if params.get("sku_id"):
            queryset = queryset.filter(sku_id=parse_uuid(params["sku_id"], "sku_id"))
        for name in ("issuer_key", "alias_type", "value"):
            if params.get(name):
                queryset = queryset.filter(**{name: params[name]})
        if params.get("effective_from"):
            queryset = queryset.filter(
                effective_from__gte=parse_timestamp(params["effective_from"], "effective_from")
            )
        if params.get("effective_to"):
            queryset = queryset.filter(
                effective_to__lte=parse_timestamp(params["effective_to"], "effective_to")
            )
        text = query_text(params)
        if text:
            queryset = queryset.filter(value__icontains=text)
        rows, cursor = page_rows(queryset.order_by("value", "effective_from", "id"), params)
        return Response(page([alias_resource(access, row) for row in rows], cursor))

    @extend_schema(
        responses=_responses(
            201, _create_response(ALIAS_RESOURCE, "Created alias."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request) -> Response:  # noqa: C901 - the contract's ordered refusal steps
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        data = request.data if isinstance(request.data, dict) else {}
        alias_type = data.get("alias_type")
        if alias_type not in ALIAS_TYPES:
            raise invalid(
                "alias_type must be barcode, vendor_code or generated.", field="alias_type"
            )
        if alias_type == "generated":
            body = business_body(
                request.data,
                {
                    "sku_id",
                    "alias_type",
                    "range_version_id",
                    "effective_from",
                    "site_id",
                    "originating_revision_id",
                },
                required=["sku_id", "range_version_id", "effective_from"],
            )
        else:
            body = business_body(
                request.data,
                {
                    "sku_id",
                    "issuer_key",
                    "alias_type",
                    "value",
                    "site_id",
                    "effective_from",
                    "effective_to",
                    "originating_revision_id",
                },
                required=["sku_id", "issuer_key", "value", "effective_from"],
            )
        sku_id = parse_uuid(body["sku_id"], "sku_id")
        site_id = parse_int_id(body["site_id"], "site_id") if body.get("site_id") else None
        effective_from = parse_timestamp(body["effective_from"], "effective_from")
        effective_to = (
            parse_timestamp(body["effective_to"], "effective_to")
            if body.get("effective_to")
            else None
        )
        if effective_to is not None and effective_to <= effective_from:
            raise invalid("effective_to must be after effective_from.", field="effective_to")
        issuer_key = (
            bounded_text(body["issuer_key"], "issuer_key", 100) if "issuer_key" in body else None
        )
        value = bounded_text(body["value"], "value", 128) if "value" in body else None
        range_id = (
            parse_uuid(body["range_version_id"], "range_version_id")
            if "range_version_id" in body
            else None
        )
        mode = creation_mode(
            access,
            manage=MANAGE_ACTION,
            propose=PROPOSE_ACTION,
            originating=body.get("originating_revision_id"),
        )
        require_any(access, READ_ACTIONS)
        sku = (
            ProductSku.objects.select_related("style")
            .filter(tenant_id=access.tenant_id, pk=sku_id)
            .first()
        )
        if sku is None:
            raise not_found("SKU")
        if site_id is not None and not Store.objects.filter(pk=site_id).exists():
            raise not_found("site")
        brand_id = sku.style.brand_id
        revision: DraftRevision | None = None
        document_site: int | None = None
        if mode == "direct":
            access.require(MANAGE_ACTION, site_id=site_id, brand_id=brand_id)
        else:
            revision, document_site = originating_revision(
                access, body["originating_revision_id"], brand_id, site_id
            )
            lineage = lineage_revision_ids(access.tenant_id, revision.pk)
            if sku.governance_state == GovernanceState.PENDING and (
                sku.originating_revision_id not in set(lineage)
            ):
                raise not_found("SKU")
        range_version: ConfigVersion | None = None
        if range_id is not None:
            range_version = ConfigVersion.objects.filter(
                tenant_id=access.tenant_id, pk=range_id
            ).first()
            if range_version is None:
                raise not_found("barcode range")
        clean = {
            "sku_id": str(sku_id),
            "alias_type": alias_type,
            "issuer_key": issuer_key,
            "value": value,
            "range_version_id": opt_id(range_id),
            "site_id": site_id,
            "effective_from": ts(effective_from),
            "effective_to": ts(effective_to),
            "originating_revision_id": opt_id(revision.pk if revision else None),
        }

        def handler(run: CommandRun) -> CommandResult:
            lineage: list[uuid.UUID] = []
            if revision is not None:
                preparing_revision(run, revision)
                lineage = lineage_revision_ids(run.tenant_id, revision.pk)
            target = ProductSku.objects.select_related("style").get(pk=sku_id)
            if not is_usable(target, run.now, lineage):
                if target.governance_state == GovernanceState.PENDING:
                    raise master_invalid("Confirm the SKU first.", field="sku_id")
                raise master_invalid("That SKU is retired.", field="sku_id")
            if range_version is not None:
                effective_ranges = {
                    v.pk for v in effective_configs(run.tenant_id, "barcode_range", run.now)
                }
                if (
                    range_version.kind != "barcode_range"
                    or range_version.pk not in effective_ranges
                ):
                    raise master_invalid(
                        "That is not an approved, effective barcode range.",
                        field="range_version_id",
                    )
                row_issuer, row_value = allocate_generated_value(run, range_version)
                config_version_id: uuid.UUID | None = range_version.pk
            else:
                assert issuer_key is not None and value is not None
                row_issuer, row_value = issuer_key, value
                config_version_id = target.identity_profile_id
            if overlapping_aliases(
                run.tenant_id,
                sku_id=target.pk,
                issuer_key=row_issuer,
                alias_type=str(alias_type),
                value=row_value,
                site_id=site_id,
                effective_from=effective_from,
                effective_to=effective_to,
            ).exists():
                raise Refusal("MASTER_CONFLICT", ALIAS_CONFLICT)
            row = SkuAlias(
                tenant_id=run.tenant_id,
                sku_id=target.pk,
                issuer_key=row_issuer,
                alias_type=str(alias_type),
                value=row_value,
                site_id=site_id,
                effective_from=effective_from,
                effective_to=effective_to,
                config_version_id=config_version_id,
                governance_state=GovernanceState.PENDING if revision else GovernanceState.EFFECTIVE,
                originating_revision_id=revision.pk if revision else None,
            )
            if revision is not None:
                open_proposal(
                    run,
                    kind="alias",
                    row=row,
                    site_id=site_id or document_site,
                    brand_id=target.style.brand_id,
                    new_subject=True,
                )
            save_master(row, conflict=ALIAS_CONFLICT)
            record_master_version(run, "alias", row, effective_from=effective_from)
            run.audit_after = alias_data(row)
            return CommandResult(resource_type="alias", resource_id=str(row.pk), status_code=201)

        result = self.run_command(
            request,
            access=access,
            action="masters.alias.create",
            meta=meta,
            business_input=clean,
            handler=handler,
            subject_key=f"alias:{sku_id}",
            site_id=site_id or document_site,
        )
        row = SkuAlias.objects.select_related("sku__style").get(pk=str(result.resource_id))
        return Response(
            create_result("alias", alias_resource(access, row), row), status=result.status_code
        )


def alias_mapping_used(row: SkuAlias) -> bool:
    """Whether a recorded scan or identity pick relied on this alias's current mapping."""
    scans = apps.get_model("inbound", "ScanObservation")
    return bool(
        scans.objects.filter(alias_value=row.value, sku_id=row.sku_id).exists()
        or IdentityPick.objects.filter(value=row.value, chosen_sku_id=row.sku_id).exists()
    )


def apply_alias_corrections(
    run: CommandRun, row: SkuAlias, proposed: dict[str, Any], existing: dict[str, Any]
) -> None:
    """E054 corrections of an alias's SKU, issuer, type, value or site (step 9)."""
    changed = {name for name, value in proposed.items() if value != existing[name]}
    identity_fields = changed & {"issuer_key", "value", "alias_type"}
    generated = SkuAlias.AliasType.GENERATED
    if identity_fields and generated in (row.alias_type, proposed.get("alias_type")):
        raise master_invalid(
            "A generated barcode keeps the issuer and value its range issued.",
            field=sorted(identity_fields)[0],
        )
    if "sku_id" in changed:
        if alias_mapping_used(row):
            raise master_invalid(
                "Scans or picks already used this alias for its SKU; retire it and "
                "create a new alias for the other SKU.",
                field="sku_id",
            )
        target = ProductSku.objects.get(pk=proposed["sku_id"])
        lineage = lineage_revision_ids(run.tenant_id, row.originating_revision_id)
        if not is_usable(target, run.now, lineage):
            raise master_invalid("That SKU cannot take an alias.", field="sku_id")
        row.sku_id = target.pk
    if "issuer_key" in changed:
        row.issuer_key = proposed["issuer_key"]
    if "value" in changed:
        row.value = proposed["value"]
    if "alias_type" in changed:
        row.alias_type = proposed["alias_type"]
    if "site_id" in changed:
        row.site_id = int(proposed["site_id"]) if proposed["site_id"] else None


class AliasDetailView(GoodsAPIView):
    """E053 detail and E054 correction: a new master version, never a silent re-point."""

    @extend_schema(
        operation_id="goods_v1_masters_aliases_detail",
        responses=_responses(200, ALIAS_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        return Response(alias_resource(access, visible_alias(access, pk)))

    @extend_schema(responses=_responses(200, ALIAS_RESOURCE, _WRITE_REFUSALS))
    def patch(self, request: Request, pk: uuid.UUID) -> Response:  # noqa: C901 - the contract's ordered refusal steps
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {
                "sku_id",
                "issuer_key",
                "alias_type",
                "value",
                "site_id",
                "effective_from",
                "effective_to",
            },
        )
        if not body:
            raise invalid("Send at least one field to change.")
        current = visible_alias(access, pk)
        edit_authority(
            access,
            current,
            brand_id=current.sku.style.brand_id,
            manage=MANAGE_ACTION,
            propose=PROPOSE_ACTION,
        )
        target_brand = current.sku.style.brand_id
        if "sku_id" in body:
            target = (
                ProductSku.objects.select_related("style")
                .filter(tenant_id=access.tenant_id, pk=parse_uuid(body["sku_id"], "sku_id"))
                .first()
            )
            if target is None:
                raise not_found("SKU")
            target_brand = target.style.brand_id
            edit_authority(
                access,
                current,
                brand_id=target_brand,
                manage=MANAGE_ACTION,
                propose=PROPOSE_ACTION,
            )
        if body.get("site_id"):
            new_site = parse_int_id(body["site_id"], "site_id")
            if not Store.objects.filter(pk=new_site).exists() or not can_any(
                access, {MANAGE_ACTION, PROPOSE_ACTION}, site_id=new_site, brand_id=target_brand
            ):
                raise not_found("site")
        proposed: dict[str, Any] = {}
        if "sku_id" in body:
            proposed["sku_id"] = str(parse_uuid(body["sku_id"], "sku_id"))
        if "site_id" in body:
            proposed["site_id"] = (
                str(parse_int_id(body["site_id"], "site_id")) if body["site_id"] else None
            )
        if "issuer_key" in body:
            proposed["issuer_key"] = bounded_text(body["issuer_key"], "issuer_key", 100)
        if "value" in body:
            proposed["value"] = bounded_text(body["value"], "value", 128)
        if "alias_type" in body:
            if body["alias_type"] not in ALIAS_TYPES:
                raise invalid("alias_type is not valid.", field="alias_type")
            proposed["alias_type"] = body["alias_type"]
        clean: dict[str, Any] = dict(proposed)
        if "effective_from" in body:
            clean["effective_from"] = ts(parse_timestamp(body["effective_from"], "effective_from"))
        if "effective_to" in body:
            clean["effective_to"] = (
                ts(parse_timestamp(body["effective_to"], "effective_to"))
                if body["effective_to"]
                else None
            )

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(
                LockRank.DOCUMENT,
                SkuAlias.objects.select_related("sku__style").filter(
                    tenant_id=run.tenant_id, pk=pk
                ),
            )[0]
            check_revision(meta.expected_revision, row.revision)
            if row.governance_state == GovernanceState.RETIRED:
                raise master_invalid("A retired alias cannot be changed.")
            existing = alias_data(row)
            apply_alias_corrections(run, row, proposed, existing)
            run.audit_before = existing
            if "effective_from" in clean:
                row.effective_from = parse_timestamp(clean["effective_from"], "effective_from")
            if "effective_to" in clean:
                row.effective_to = (
                    parse_timestamp(clean["effective_to"], "effective_to")
                    if clean["effective_to"]
                    else None
                )
            if row.effective_to is not None and row.effective_to <= row.effective_from:
                raise master_invalid(
                    "effective_to must be after effective_from.", field="effective_to"
                )
            if (
                overlapping_aliases(
                    run.tenant_id,
                    sku_id=row.sku_id,
                    issuer_key=row.issuer_key,
                    alias_type=row.alias_type,
                    value=row.value,
                    site_id=row.site_id,
                    effective_from=row.effective_from,
                    effective_to=row.effective_to,
                )
                .exclude(pk=row.pk)
                .exists()
            ):
                raise Refusal("MASTER_CONFLICT", ALIAS_CONFLICT)
            row.revision += 1
            save_master(
                row,
                conflict=ALIAS_CONFLICT,
                update_fields=[
                    "sku",
                    "issuer_key",
                    "alias_type",
                    "value",
                    "site",
                    "effective_from",
                    "effective_to",
                    "revision",
                ],
            )
            record_master_version(run, "alias", row, effective_from=row.effective_from)
            if row.governance_state == GovernanceState.PENDING:
                open_proposal(
                    run,
                    kind="alias",
                    row=row,
                    site_id=row.site_id or originating_site(row),
                    brand_id=row.sku.style.brand_id,
                )
            run.audit_after = alias_data(row)
            return CommandResult(resource_type="alias", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.alias.update",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"alias:{pk}",
            site_id=current.site_id,
        )
        row = SkuAlias.objects.select_related("sku__style").get(pk=pk)
        return Response(alias_resource(access, row), status=result.status_code)


class AliasRetireView(GoodsAPIView):
    """E055 retirement: closes the alias's effective period from ``effective_at``."""

    @extend_schema(responses=_responses(200, ALIAS_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        reason, effective_at, clean = retire_body(request)
        current = visible_alias(access, pk)
        require_owner(access, current.sku.style.brand_id)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(
                LockRank.DOCUMENT, SkuAlias.objects.filter(tenant_id=run.tenant_id, pk=pk)
            )[0]
            check_revision(meta.expected_revision, row.revision)
            retirement_target(row)
            if effective_at <= row.effective_from:
                raise invalid(
                    "effective_at must be after the alias became effective.", field="effective_at"
                )
            if row.effective_to is not None and row.effective_to <= effective_at:
                raise Refusal("RETIREMENT_BLOCKED", "This alias already ends before that time.")
            run.audit_before = alias_data(row)
            row.effective_to = effective_at
            if effective_at <= run.now:
                row.governance_state = GovernanceState.RETIRED
            row.revision += 1
            row.save(update_fields=["effective_to", "governance_state", "revision"])
            record_master_version(
                run, "alias", row, retired=True, reason_code=reason, effective_from=effective_at
            )
            return CommandResult(resource_type="alias", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.alias.retire",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"alias:{pk}",
            site_id=current.site_id,
        )
        row = SkuAlias.objects.select_related("sku__style").get(pk=pk)
        return Response(alias_resource(access, row), status=result.status_code)


# -- crosswalks (E056-E060) --------------------------------------------------------


def crosswalk_brand(row: SourceCrosswalk) -> int | None:
    """The brand a crosswalk belongs to for scope checks (its target, or its issuer's)."""
    return rule_brand(row.kind, row.issuer_key, row.target_key)


def require_rule_proposer(access: AccessContext, kind: str, brand_id: int | None) -> None:
    """Authority to *propose* a crosswalk (E057 without ``crosswalk.manage``).

    A crosswalk is a master held at no site, so a site-scoped grant never covers it
    by the ordinary record rule. A proposed attribute rule for one brand is the
    exception (OPS-16, Anand 23 September 2026): the Warehouse role proposes it from
    a PT grid cell, and any grant holding ``crosswalk.propose`` that reaches the
    brand may - as a product master is reached (``can_reach_brand``). It applies
    nowhere until the product-master owner confirms it. Every other proposal keeps
    the ordinary rule.
    """
    if brand_id is not None and is_attribute_kind(kind):
        if not access.can_reach_brand({CROSSWALK_PROPOSE}, brand_id):
            raise Refusal("NOT_FOUND", "That record was not found.")
        return
    access.require(CROSSWALK_PROPOSE, brand_id=brand_id)


def crosswalk_target_valid(kind: str, target_key: str, tenant_id: uuid.UUID | None = None) -> bool:
    """A crosswalk maps only to a live master of its kind (empty = still unresolved).

    An attribute rule maps only to an approved, unretired value of its vocabulary
    dimension (OPS-14), which needs the tenant to read.
    """
    if target_key == "":
        return True
    if kind == SourceCrosswalk.Kind.BRAND:
        return (
            target_key.isdigit()
            and Brand.objects.filter(pk=int(target_key), is_active=True).exists()
        )
    if kind == SourceCrosswalk.Kind.VENDOR:
        vendors = apps.get_model("vendors", "Vendor")._default_manager
        return target_key.isdigit() and vendors.filter(pk=int(target_key), is_active=True).exists()
    if is_attribute_kind(kind):
        from core.commands import database_now

        return tenant_id is not None and (
            attribute_target(tenant_id, kind, target_key, database_now()) is not None
        )
    return True


def check_crosswalk_kind(tenant_id: uuid.UUID, kind: str, issuer_key: str) -> None:
    """A master kind, or an attribute rule on a governed dimension with a known issuer."""
    from core.commands import database_now

    if not is_attribute_kind(kind):
        return
    if kind not in attribute_dimensions(tenant_id, database_now()):
        raise invalid(
            "kind must be vendor, brand, subbrand or a governed vocabulary dimension.",
            field="kind",
        )
    parsed = parse_rule_issuer(issuer_key)
    if parsed is None:
        raise invalid(
            "An attribute rule's issuer_key must be *, brand:<id>, keyword or item.",
            field="issuer_key",
        )
    if parsed[1] is not None and not Brand.objects.filter(pk=parsed[1], is_active=True).exists():
        raise master_invalid("issuer_key names no live brand.", field="issuer_key")


def crosswalk_resource(access: AccessContext, row: SourceCrosswalk) -> dict[str, Any]:
    brand_id = crosswalk_brand(row)
    return resource_dto(
        id=row.pk,
        data=crosswalk_data(row),
        revision=row.revision,
        state=row.governance_state,
        context={"brand_id": brand_id},
        allowed_actions=allowed_actions(
            access, row, brand_id=brand_id, manage=CROSSWALK_MANAGE, propose=CROSSWALK_PROPOSE
        ),
    )


def visible_crosswalk(access: AccessContext, pk: uuid.UUID) -> SourceCrosswalk:
    require_any(access, READ_ACTIONS)
    row = SourceCrosswalk.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    if row is None or not crosswalk_visible(access, row):
        raise not_found("crosswalk")
    return row


def crosswalk_visible(access: AccessContext, row: SourceCrosswalk) -> bool:
    brands = brand_scope(access, READ_ACTIONS)
    brand_id = crosswalk_brand(row)
    if brands is not None and brand_id is not None and brand_id not in brands:
        return False
    return pending_visible(access, "crosswalk", CROSSWALK_MANAGE, row)


CROSSWALK_CONFLICT = "That source key is already mapped for this issuer and configuration."


def crosswalk_fields(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "kind" in body or not partial:
        # A master kind or a vocabulary dimension; the dimension is checked against
        # the tenant's approved vocabulary by ``check_crosswalk_kind``.
        kind = body.get("kind")
        if not isinstance(kind, str) or not kind or len(kind) > 60 or kind != kind.strip():
            raise invalid(
                "kind must be vendor, brand, subbrand or a governed vocabulary dimension.",
                field="kind",
            )
        clean["kind"] = kind
    if "issuer_key" in body or not partial:
        clean["issuer_key"] = bounded_text(body.get("issuer_key"), "issuer_key", 100)
    if "source_key" in body or not partial:
        clean["source_key"] = bounded_text(body.get("source_key"), "source_key", 240)
    if "target_key" in body or not partial:
        if "target_key" not in body:
            raise invalid(
                "target_key is required (empty while still unresolved).", field="target_key"
            )
        clean["target_key"] = bounded_text(
            body.get("target_key"), "target_key", 100, allow_empty=True
        )
    if "config_version_id" in body or not partial:
        clean["config_version_id"] = str(
            parse_uuid(body.get("config_version_id"), "config_version_id")
        )
    return clean


class CrosswalkListCreateView(GoodsAPIView):
    """E056 list and E057 create (C-PMO effective, C-BUY proposal)."""

    @extend_schema(responses=_responses(200, _page_response(CROSSWALK_RESOURCE), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(
            request,
            LIST_QUERY_KEYS
            | {"kind", "issuer_key", "source_key", "target_key", "config_version_id"},
        )
        brands, brand_id, _site = list_scope(access, params)
        queryset = SourceCrosswalk.objects.filter(tenant_id=access.tenant_id).filter(
            pending_filter(access, "crosswalk", CROSSWALK_MANAGE)
        )
        if brands is not None:
            queryset = queryset.filter(
                ~Q(kind=SourceCrosswalk.Kind.BRAND)
                | Q(target_key="")
                | Q(target_key__in=[str(b) for b in sorted(brands)])
            ).filter(
                # An attribute rule read from one brand's files belongs to that brand.
                Q(kind__in=sorted(CROSSWALK_KINDS))
                | ~Q(issuer_key__startswith=BRAND_ISSUER_PREFIX)
                | Q(issuer_key__in=[f"{BRAND_ISSUER_PREFIX}{b}" for b in sorted(brands)])
            )
        if brand_id is not None:
            queryset = queryset.filter(kind=SourceCrosswalk.Kind.BRAND, target_key=str(brand_id))
        for name in ("kind", "issuer_key", "source_key", "target_key"):
            if params.get(name):
                queryset = queryset.filter(**{name: params[name]})
        if params.get("config_version_id"):
            queryset = queryset.filter(
                config_version_id=parse_uuid(params["config_version_id"], "config_version_id")
            )
        text = query_text(params)
        if text:
            queryset = queryset.filter(source_key__icontains=text)
        rows, cursor = page_rows(
            queryset.order_by("kind", "issuer_key", "source_key", "id"), params
        )
        return Response(page([crosswalk_resource(access, row) for row in rows], cursor))

    @extend_schema(responses=_responses(201, CROSSWALK_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"kind", "issuer_key", "source_key", "target_key", "config_version_id"},
            required=["kind", "issuer_key", "source_key", "config_version_id"],
        )
        clean = crosswalk_fields(body, partial=False)
        check_crosswalk_kind(access.tenant_id, clean["kind"], clean["issuer_key"])
        target_brand = rule_brand(clean["kind"], clean["issuer_key"], clean["target_key"])
        if holds_any(access, {CROSSWALK_MANAGE}):
            access.require(CROSSWALK_MANAGE, brand_id=target_brand)
            proposal = False
        elif holds_any(access, {CROSSWALK_PROPOSE}):
            require_rule_proposer(access, clean["kind"], target_brand)
            proposal = True
        else:
            raise Refusal("ACTION_DENIED", "You do not have permission to map source keys.")
        config_id = uuid.UUID(clean["config_version_id"])
        _require_config_in_force(access.tenant_id, config_id)

        def handler(run: CommandRun) -> CommandResult:
            if not crosswalk_target_valid(clean["kind"], clean["target_key"], run.tenant_id):
                raise master_invalid(
                    f"target_key is not a live {clean['kind']}.", field="target_key"
                )
            if SourceCrosswalk.objects.filter(
                tenant_id=run.tenant_id,
                kind=clean["kind"],
                issuer_key=clean["issuer_key"],
                source_key=clean["source_key"],
                config_version_id=config_id,
            ).exists():
                raise Refusal("MASTER_CONFLICT", CROSSWALK_CONFLICT)
            unresolved = clean["target_key"] == ""
            row = SourceCrosswalk(
                tenant_id=run.tenant_id,
                kind=clean["kind"],
                issuer_key=clean["issuer_key"],
                source_key=clean["source_key"],
                target_key=clean["target_key"],
                config_version_id=config_id,
                governance_state=GovernanceState.PENDING
                if proposal or unresolved
                else GovernanceState.EFFECTIVE,
            )
            save_master(row, conflict=CROSSWALK_CONFLICT)
            record_master_version(run, "crosswalk", row)
            sync_crosswalk_exception(run, row)
            run.audit_after = crosswalk_data(row)
            return CommandResult(
                resource_type="crosswalk", resource_id=str(row.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="masters.crosswalk.create",
            meta=meta,
            business_input=clean,
            handler=handler,
            subject_key=f"crosswalk:{clean['kind']}:{clean['source_key']}"[:100],
        )
        row = SourceCrosswalk.objects.get(pk=str(result.resource_id))
        return Response(crosswalk_resource(access, row), status=result.status_code)


class CrosswalkDetailView(GoodsAPIView):
    """E058 detail and E059 correction."""

    @extend_schema(
        operation_id="goods_v1_masters_crosswalks_detail",
        responses=_responses(200, CROSSWALK_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        return Response(crosswalk_resource(access, visible_crosswalk(access, pk)))

    @extend_schema(responses=_responses(200, CROSSWALK_RESOURCE, _WRITE_REFUSALS))
    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, {"kind", "issuer_key", "source_key", "target_key", "config_version_id"}
        )
        if not body:
            raise invalid("Send at least one field to change.")
        current = visible_crosswalk(access, pk)
        edit_authority(
            access,
            current,
            brand_id=crosswalk_brand(current),
            manage=CROSSWALK_MANAGE,
            propose=CROSSWALK_PROPOSE,
        )
        clean = crosswalk_fields(body, partial=True)
        if "config_version_id" in clean:
            _require_config_in_force(access.tenant_id, uuid.UUID(clean["config_version_id"]))
        merged = {
            name: clean.get(name, getattr(current, name))
            for name in ("kind", "issuer_key", "target_key")
        }
        check_crosswalk_kind(access.tenant_id, merged["kind"], merged["issuer_key"])
        moved_to = rule_brand(merged["kind"], merged["issuer_key"], merged["target_key"])
        if moved_to is not None and moved_to != crosswalk_brand(current):
            # A correction may not carry a mapping into a brand the editor cannot reach.
            edit_authority(
                access,
                current,
                brand_id=moved_to,
                manage=CROSSWALK_MANAGE,
                propose=CROSSWALK_PROPOSE,
            )

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(
                LockRank.DOCUMENT, SourceCrosswalk.objects.filter(tenant_id=run.tenant_id, pk=pk)
            )[0]
            check_revision(meta.expected_revision, row.revision)
            if row.governance_state == GovernanceState.RETIRED or row.retired_at is not None:
                raise master_invalid("A retired crosswalk cannot be changed.")
            run.audit_before = crosswalk_data(row)
            for name, value in clean.items():
                setattr(row, name, uuid.UUID(value) if name == "config_version_id" else value)
            if row.governance_state == GovernanceState.EFFECTIVE and row.target_key == "":
                raise master_invalid(
                    "A confirmed crosswalk must keep a target.", field="target_key"
                )
            if not crosswalk_target_valid(row.kind, row.target_key, run.tenant_id):
                raise master_invalid(f"target_key is not a live {row.kind}.", field="target_key")
            if (
                SourceCrosswalk.objects.filter(
                    tenant_id=run.tenant_id,
                    kind=row.kind,
                    issuer_key=row.issuer_key,
                    source_key=row.source_key,
                    config_version_id=row.config_version_id,
                )
                .exclude(pk=row.pk)
                .exists()
            ):
                raise Refusal("MASTER_CONFLICT", CROSSWALK_CONFLICT)
            row.revision += 1
            save_master(
                row,
                conflict=CROSSWALK_CONFLICT,
                update_fields=[
                    "kind",
                    "issuer_key",
                    "source_key",
                    "target_key",
                    "config_version_id",
                    "revision",
                ],
            )
            record_master_version(run, "crosswalk", row)
            sync_crosswalk_exception(run, row)
            run.audit_after = crosswalk_data(row)
            return CommandResult(resource_type="crosswalk", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.crosswalk.update",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"crosswalk:{pk}",
        )
        return Response(
            crosswalk_resource(access, SourceCrosswalk.objects.get(pk=pk)),
            status=result.status_code,
        )


class CrosswalkRetireView(GoodsAPIView):
    """E060 retirement."""

    @extend_schema(responses=_responses(200, CROSSWALK_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        reason, effective_at, clean = retire_body(request)
        current = visible_crosswalk(access, pk)
        require_owner(access, crosswalk_brand(current))
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(
                LockRank.DOCUMENT, SourceCrosswalk.objects.filter(tenant_id=run.tenant_id, pk=pk)
            )[0]
            check_revision(meta.expected_revision, row.revision)
            retirement_target(row)
            run.audit_before = crosswalk_data(row)
            apply_retirement(run, row, effective_at)
            record_master_version(
                run, "crosswalk", row, retired=True, reason_code=reason, effective_from=effective_at
            )
            return CommandResult(resource_type="crosswalk", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.crosswalk.retire",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"crosswalk:{pk}",
        )
        return Response(
            crosswalk_resource(access, SourceCrosswalk.objects.get(pk=pk)),
            status=result.status_code,
        )


# -- lookup and picks (E090, E091) -------------------------------------------------


class LookupScope:
    """The validated AliasContext of a lookup or pick, inside the caller's scope."""

    def __init__(self, access: AccessContext, raw: dict[str, Any], *, prefix: str = "") -> None:
        unknown = sorted(set(raw) - CONTEXT_KEYS)
        if unknown:
            raise invalid(f"Unknown context field(s): {', '.join(unknown)}.")
        for name in ("site_id", "issuer_key", "alias_type", "as_of", "profile_version_id"):
            if raw.get(name) in (None, ""):
                raise Refusal(
                    "INVALID_REQUEST",
                    f"{prefix}{name} is required.",
                    issues=[issue("REQUIRED", f"{name} is required", field=f"{prefix}{name}")],
                )
        self.site_id = parse_int_id(raw["site_id"], f"{prefix}site_id")
        self.issuer_key = bounded_text(raw["issuer_key"], f"{prefix}issuer_key", 100)
        if raw["alias_type"] not in ALIAS_TYPES:
            raise invalid(
                "alias_type must be barcode, vendor_code or generated.",
                field=f"{prefix}alias_type",
            )
        self.alias_type = str(raw["alias_type"])
        self.as_of = parse_timestamp(raw["as_of"], f"{prefix}as_of")
        profile_id = parse_uuid(raw["profile_version_id"], f"{prefix}profile_version_id")
        self.subject_revision_id = (
            parse_uuid(raw["subject_revision_id"], f"{prefix}subject_revision_id")
            if raw.get("subject_revision_id")
            else None
        )
        require_any(access, LOOKUP_ACTIONS)
        if not Store.objects.filter(pk=self.site_id).exists() or not can_any(
            access, LOOKUP_ACTIONS, site_id=self.site_id
        ):
            raise not_found("site")
        profile = profile_context(access.tenant_id, profile_id)
        if profile is None:
            raise not_found("profile")
        self.profile_version_id = profile_id
        self.profile_family = profile[1] or None
        self.brand_ids = brand_scope(access, LOOKUP_ACTIONS, self.site_id)
        if self.subject_revision_id is not None:
            require_revision_scope(access, self.subject_revision_id)

    def as_context(self) -> dict[str, Any]:
        return {
            "site_id": str(self.site_id),
            "issuer_key": self.issuer_key,
            "alias_type": self.alias_type,
            "as_of": ts(self.as_of),
            "profile_version_id": str(self.profile_version_id),
            "subject_revision_id": opt_id(self.subject_revision_id),
        }


def require_revision_scope(access: AccessContext, revision_id: uuid.UUID) -> None:
    site = (
        DraftRevision.objects.filter(tenant_id=access.tenant_id, pk=revision_id)
        .values_list("document__site_id", flat=True)
        .first()
    )
    if site is None or not can_any(access, LOOKUP_ACTIONS, site_id=site):
        raise not_found("draft revision")


class GoodsSkuLookupView(GoodsAPIView):
    """E090: every candidate SKU for a code; never an automatic choice."""

    @extend_schema(responses=_responses(200, IDENTITY_RESOLUTION, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, CONTEXT_KEYS | {"value"})
        if not params.get("value"):
            raise Refusal(
                "INVALID_REQUEST",
                "value is required.",
                issues=[issue("REQUIRED", "value is required", field="value")],
            )
        value = bounded_text(params["value"], "value", 128)
        scope = LookupScope(access, {k: v for k, v in params.items() if k in CONTEXT_KEYS})
        resolution = resolve_alias(
            access.tenant_id,
            value=value,
            site_id=scope.site_id,
            as_of=scope.as_of,
            issuer_key=scope.issuer_key,
            alias_type=scope.alias_type,
            subject_revision_id=scope.subject_revision_id,
            brand_ids=scope.brand_ids,
            profile_family=scope.profile_family,
        )
        return Response(resolution.as_dto())


class IdentityPickView(GoodsAPIView):
    """E091: record the chosen SKU against a draft revision or a scan."""

    @extend_schema(responses=_responses(200, IDENTITY_RESOLUTION, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {
                "context",
                "value",
                "candidate_hash",
                "chosen_sku_id",
                "subject_revision_id",
                "scan_event_id",
            },
            required=["context", "value", "candidate_hash", "chosen_sku_id"],
        )
        if not isinstance(body["context"], dict):
            raise invalid("context must be an AliasContext object.", field="context")
        value = bounded_text(body["value"], "value", 128)
        candidate_hash = str(body["candidate_hash"])
        if len(candidate_hash) != 64 or any(c not in "0123456789abcdef" for c in candidate_hash):
            raise invalid("candidate_hash must be a 64-character hash.", field="candidate_hash")
        chosen = parse_uuid(body["chosen_sku_id"], "chosen_sku_id")
        subject_revision_id = (
            parse_uuid(body["subject_revision_id"], "subject_revision_id")
            if body.get("subject_revision_id")
            else None
        )
        scan_event_id = (
            parse_uuid(body["scan_event_id"], "scan_event_id")
            if body.get("scan_event_id")
            else None
        )
        if (subject_revision_id is None) == (scan_event_id is None):
            raise invalid("Send exactly one of subject_revision_id or scan_event_id.")
        scope = LookupScope(access, body["context"], prefix="context.")
        if (
            subject_revision_id is not None
            and scope.subject_revision_id is not None
            and scope.subject_revision_id != subject_revision_id
        ):
            raise invalid(
                "context.subject_revision_id must match subject_revision_id.",
                field="context.subject_revision_id",
            )
        if subject_revision_id is not None:
            require_revision_scope(access, subject_revision_id)
        if scan_event_id is not None:
            scans = apps.get_model("inbound", "ScanObservation")._default_manager
            scan_site = (
                scans.filter(tenant_id=access.tenant_id, pk=scan_event_id)
                .values_list("site_id", flat=True)
                .first()
            )
            if scan_site is None or not can_any(access, LOOKUP_ACTIONS, site_id=scan_site):
                raise not_found("scan")
        context = scope.as_context()
        clean = {
            "context": context,
            "value": value,
            "candidate_hash": candidate_hash,
            "chosen_sku_id": str(chosen),
            "subject_revision_id": opt_id(subject_revision_id),
            "scan_event_id": opt_id(scan_event_id),
        }

        def handler(run: CommandRun) -> CommandResult:
            pick = record_pick(
                run,
                value=value,
                candidate_hash=candidate_hash,
                chosen_sku_id=chosen,
                subject_revision_id=subject_revision_id,
                scan_event_id=scan_event_id,
                context={**context, "as_of": scope.as_of},
                brand_ids=scope.brand_ids,
                profile_family=scope.profile_family,
            )
            return CommandResult(resource_type="identity_pick", resource_id=str(pick.pk))

        result = self.run_command(
            request,
            access=access,
            action="masters.identity.pick",
            meta=meta,
            business_input=clean,
            handler=handler,
            subject_key=f"identity-pick:{subject_revision_id or scan_event_id}",
            site_id=scope.site_id,
        )
        pick = IdentityPick.objects.get(pk=str(result.resource_id))
        ids = [str(item) for item in pick.candidates or []]
        candidates = candidates_for(access.tenant_id, ids)
        return Response(
            {
                "result": "resolved" if len(ids) == 1 else "ambiguous",
                "candidate_hash": pick.candidate_hash or candidate_set_hash(pick.value, ids),
                "candidates": candidates,
                "chosen_sku_id": str(pick.chosen_sku_id),
                "issues": [],
            },
            status=result.status_code,
        )


# -- new items waiting for the product-master owner ------------------------------

#: One approval a new item waits on: its style's, when the style is new too, and its
#: SKU's. Decided through E234 (``approvals/{id}/decide``) with these values.
ITEM_APPROVAL = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "part": {"type": "string", "enum": ["style", "sku"]},
        "revision": {"type": "integer"},
        "reviewed_hash": {"type": "string", "nullable": True},
        "can_decide": {"type": "boolean"},
    },
}

ITEM_PROPOSAL = {
    "type": "object",
    "description": (
        "A new item a PT proposed, waiting for the product-master owner (store and "
        "warehouse operations PRD §5.4). `approvals` are the pending E234 requests it "
        "waits on, the style's first; `style_shared` says another waiting item uses "
        "the same new style, so rejecting this one leaves the style for that one."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "kind": {"type": "string", "enum": ["sku", "style"]},
        "style_id": {"type": "string", "format": "uuid"},
        "style_code": {"type": "string"},
        "style_is_new": {"type": "boolean"},
        "style_shared": {"type": "boolean"},
        "brand_id": {"type": "string"},
        "brand_name": {"type": "string"},
        "profile_family": {"type": "string"},
        "describing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"field_id": {"type": "string"}, "label": {"type": "string"}},
            },
        },
        "pt": {
            "type": "object",
            "nullable": True,
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "number": {"type": "string", "nullable": True},
                "site_id": {"type": "string", "nullable": True},
            },
        },
        "proposed_by": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        },
        "proposed_at": {"type": "string", "format": "date-time", "nullable": True},
        "approvals": {"type": "array", "items": ITEM_APPROVAL},
        "can_decide": {"type": "boolean"},
    },
}


def _describing(attrs: list[Any], labels: dict[str, str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in attrs or []:
        if not isinstance(entry, dict) or not entry.get("field_id"):
            continue
        if entry.get("vocabulary_value_id"):
            value_id = str(entry["vocabulary_value_id"])
            label = labels.get(value_id, value_id)
        elif entry.get("supplied_text"):
            label = str(entry["supplied_text"])
        else:
            label = "unknown"
        out.append({"field_id": str(entry["field_id"]), "label": label})
    return out


def item_proposals(access: AccessContext) -> list[dict[str, Any]]:
    """The new styles and SKUs PTs proposed that still wait, as this person may see them.

    Visibility is E170's: the person who proposed it, or anyone whose grant covers the
    approval's action or ``approvals.view`` for its site and brand. Deciding is E169's:
    a grant covering the action, and never the proposer where the request needs two
    people. Nothing here decides anything; each approval is decided through E234.
    """
    from approvals.goods_models import ApprovalRequest
    from approvals.goods_services import subject_cells_many
    from masters.goods_identity_services import PROPOSAL_SUBJECT, value_labels

    requests = [
        row
        for row in ApprovalRequest.objects.select_related("maker")
        .filter(
            tenant_id=access.tenant_id,
            subject_kind=PROPOSAL_SUBJECT,
            state=ApprovalRequest.State.PENDING,
        )
        .filter(Q(subject_key__startswith="sku:") | Q(subject_key__startswith="style:"))
        .order_by("created_at", "id")
    ]
    cells = subject_cells_many(requests)
    visible = [
        row
        for row in requests
        if row.maker_id == access.human_id
        or access.covers_all({row.requested_action, "approvals.view"}, cells[row.pk])
    ]
    by_subject: dict[tuple[str, str], Any] = {}
    for row in visible:
        kind, _, raw = row.subject_key.partition(":")
        by_subject[(kind, raw)] = row

    def approval(part: str, row: Any) -> dict[str, Any]:
        distinct_block = row.require_distinct and row.maker_id == access.human_id
        return {
            "id": str(row.pk),
            "part": part,
            "revision": row.revision,
            "reviewed_hash": row.reviewed_hash,
            "can_decide": not distinct_block
            and access.covers_all({row.requested_action}, cells[row.pk]),
        }

    sku_ids = [raw for (kind, raw) in by_subject if kind == "sku"]
    skus = {
        str(row.pk): row
        for row in ProductSku.objects.select_related("style", "style__brand").filter(
            tenant_id=access.tenant_id, pk__in=sku_ids, governance_state=GovernanceState.PENDING
        )
    }
    style_ids = [raw for (kind, raw) in by_subject if kind == "style"]
    styles = {
        str(row.pk): row
        for row in Style.objects.select_related("brand").filter(
            tenant_id=access.tenant_id,
            pk__in=style_ids,
            governance_state=GovernanceState.PENDING,
        )
    }
    waiting_per_style: dict[str, int] = {}
    for sku in skus.values():
        waiting_per_style[str(sku.style_id)] = waiting_per_style.get(str(sku.style_id), 0) + 1
    origin_ids = [sku.originating_revision_id for sku in skus.values()] + [
        style.originating_revision_id for style in styles.values()
    ]
    origins = {
        row.pk: row
        for row in DraftRevision.objects.select_related("document").filter(
            pk__in=[pk for pk in origin_ids if pk is not None]
        )
    }
    labels = value_labels(access.tenant_id)

    def item(kind: str, row: Any, style: Style, request: Any) -> dict[str, Any]:
        style_key = str(style.pk)
        new_style = style_key in styles
        parts = []
        if new_style:
            parts.append(approval("style", by_subject[("style", style_key)]))
        if kind == "sku":
            parts.append(approval("sku", request))
        origin = origins.get(row.originating_revision_id)
        document = origin.document if origin is not None else None
        attrs = list(row.attrs or []) if kind == "sku" else []
        return {
            "id": str(row.pk),
            "kind": kind,
            "style_id": style_key,
            "style_code": style.style_code,
            "style_is_new": new_style,
            "style_shared": waiting_per_style.get(style_key, 0) > 1,
            "brand_id": str(style.brand_id),
            "brand_name": style.brand.name,
            "profile_family": style.profile_family,
            "describing": _describing([*attrs, *(style.attrs or [])], labels),
            "pt": {
                "id": str(document.pk),
                "number": document.official_number,
                "site_id": opt_id(document.site_id),
            }
            if document is not None
            else None,
            "proposed_by": {
                "id": str(request.maker_id),
                "name": getattr(request.maker, "display_name", ""),
            },
            "proposed_at": ts(request.created_at),
            "approvals": parts,
            "can_decide": all(part["can_decide"] for part in parts),
        }

    out: list[dict[str, Any]] = []
    for (kind, raw), request in by_subject.items():
        if kind == "sku" and raw in skus:
            out.append(item("sku", skus[raw], skus[raw].style, request))
        elif kind == "style" and raw in styles and waiting_per_style.get(raw, 0) == 0:
            # A new style no waiting item carries: offered on its own.
            out.append(item("style", styles[raw], styles[raw], request))
    return out


class ItemProposalListView(GoodsAPIView):
    """New items waiting for the product-master owner (PT Work -> Mapping rules)."""

    @extend_schema(responses=_responses(200, _page_response(ITEM_PROPOSAL), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, LIST_QUERY_KEYS)
        require_any(access, READ_ACTIONS)
        window, cursor = paginate(item_proposals(access), params)
        return Response(page(window, cursor))
