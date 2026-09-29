"""Goods-v1 PT vocabulary and crosswalk work (E226-E231).

These routes read the authorised vocabulary and crosswalks or create a governed
proposal only. A crosswalk is a master mapping (vendor, brand, subbrand) or an
attribute rule for a PT column (kind = a vocabulary dimension; OPS-14), and the
review and proposal lists carry both. E229 offers exact candidates and, since
store and warehouse operations PRD §5.4, deterministic close matches marked as
suggestions with their reason; nothing is ever chosen automatically and no read
writes. A preparer may propose a crosswalk target; only the product master owner
confirms or rejects it. These answer under ``/api/goods-v1/ptmapper/`` alone, which
is what lets the emitted schema describe them at all (#303); the legacy readers
were deleted (OPS-18).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    check_revision,
    page,
    paginate,
    parse_meta,
    parse_uuid,
    resource_dto,
)
from accounts.principal import AccessContext
from core.canonical import content_hash
from core.commands import CommandResult, CommandRun, LockRank
from core.kernel_models import DraftRevision
from core.refusals import Refusal, issue
from masters.goods_identity_models import GovernanceState, SourceCrosswalk
from masters.goods_identity_services import (
    CROSSWALK_KINDS,
    MANAGE_ACTION,
    PROPOSE_ACTION,
    VocabularyValue,
    attribute_dimensions,
    bounded_text,
    crosswalk_data,
    invalid,
    is_attribute_kind,
    normalise_text,
    opt_id,
    profile_context,
    record_master_version,
    rule_brand,
    value_labels,
    vocabulary,
)
from masters.goods_identity_views import (
    CROSSWALK_MANAGE,
    CROSSWALK_PROPOSE,
    brand_scope,
    can_any,
    crosswalk_brand,
    crosswalk_target_valid,
    holds_any,
    query_text,
    require_any,
)
from masters.models import Brand
from ptmapper.goods_mapper import clean_for_dimension
from ptmapper.goods_rulebook import RuleValue, close_matches

#: C-PMO/C-WHO preparers and crosswalk stewards (design E226-E231).
VOCAB_ACTIONS = frozenset(
    {
        MANAGE_ACTION,
        PROPOSE_ACTION,
        CROSSWALK_MANAGE,
        CROSSWALK_PROPOSE,
        "pt.prepare",
        "pt.prepare.opening",
        "identity.resolve",
    }
)
PROPOSER_ACTIONS = frozenset(
    {CROSSWALK_PROPOSE, CROSSWALK_MANAGE, PROPOSE_ACTION, "pt.prepare", "pt.prepare.opening"}
)
#: Preparing the tenant's first profile is configuration drafting (C-PMO, C-OWN).
FIRST_PROFILE_ACTION = "config.draft"
#: The buyer's booking-reference read (GSA-T05); no configuration authority.
BOOKING_ACTION = "booking.manage"
BOOKING_PURPOSE = "booking"
QUERY_KEYS = frozenset(
    {
        "profile_version_id",
        "dimension",
        "subject_revision_id",
        "q",
        "cursor",
        "limit",
    }
)
#: E226 alone takes `purpose`; the crosswalk readers keep the closed query above.
CONTROLLED_KEYS = QUERY_KEYS | {"purpose"}


class VocabularyQuery:
    """The closed query of E226-E229, resolved inside the caller's scope.

    ``controlled`` is E226, the one read that answers without a profile. The
    first profile is *chosen from* the vocabulary, so demanding the profile
    would make the tenant's opening configuration unreachable; and a buyer
    naming ``purpose=booking`` needs the same effective size and colour without
    ever holding configuration authority. Both still name one dimension, and a
    dimension nothing governs is ``NOT_FOUND`` — never an empty page, which a
    profile editor would read as the approved "unrestricted" answer.
    """

    def __init__(
        self, access: AccessContext, request: Request, *, crosswalks: bool, controlled: bool = False
    ) -> None:
        self.params = check_query(request, CONTROLLED_KEYS if controlled else QUERY_KEYS)
        self.purpose = self._purpose(controlled)
        readers = set(VOCAB_ACTIONS) | ({BOOKING_ACTION} if self.purpose else set())
        require_any(access, readers)
        self.dimension = (
            bounded_text(self.params["dimension"], "dimension", 60)
            if self.params.get("dimension")
            else None
        )
        if controlled and self.dimension is None:
            # E226 names one dimension, with or without a profile (design E226 input).
            raise Refusal(
                "INVALID_REQUEST",
                "dimension is required.",
                issues=[issue("REQUIRED", "dimension is required", field="dimension")],
            )
        self.q = query_text(self.params)
        if self.params.get("subject_revision_id"):
            revision_id = parse_uuid(self.params["subject_revision_id"], "subject_revision_id")
            site = (
                DraftRevision.objects.filter(tenant_id=access.tenant_id, pk=revision_id)
                .values_list("document__site_id", flat=True)
                .first()
            )
            if site is None or not can_any(access, readers, site_id=site):
                raise Refusal("NOT_FOUND", "That draft revision was not found.")
        if controlled and not self.params.get("profile_version_id"):
            self._without_profile(access)
            return
        if not self.params.get("profile_version_id"):
            raise Refusal(
                "INVALID_REQUEST",
                "profile_version_id is required.",
                issues=[
                    issue("REQUIRED", "profile_version_id is required", field="profile_version_id")
                ],
            )
        profile_id = parse_uuid(self.params["profile_version_id"], "profile_version_id")
        context = profile_context(access.tenant_id, profile_id)
        if context is None:
            raise Refusal("NOT_FOUND", "That profile was not found.")
        self.profile_version_id: uuid.UUID | None = profile_id
        self.dimensions = context[2]
        if self.dimension is not None:
            # The review and proposal lists carry master crosswalks and attribute
            # rules for every governed dimension (PT Work -> Mapping rules, OPS-14).
            known = (
                CROSSWALK_KINDS | attribute_dimensions(access.tenant_id, timezone.now())
                if crosswalks
                else set(self.dimensions)
            )
            if known and self.dimension not in known:
                raise Refusal("NOT_FOUND", "That dimension is not part of this profile.")
        self.brands = brand_scope(access, VOCAB_ACTIONS)

    def _purpose(self, controlled: bool) -> str | None:
        raw = self.params.get("purpose") if controlled else None
        if not raw:
            return None
        if raw != BOOKING_PURPOSE:
            raise Refusal(
                "INVALID_REQUEST",
                f"purpose must be {BOOKING_PURPOSE}.",
                issues=[issue("INVALID", "unknown purpose", field="purpose")],
            )
        return BOOKING_PURPOSE

    def _without_profile(self, access: AccessContext) -> None:
        """E226 with no profile: the first profile's own vocabulary, or a booking's.

        Each of the two callers is gated on its own authority. Neither is the
        weaker of the two: naming ``purpose=booking`` must not hand a preparer
        the read that drafting configuration is what earns.
        """
        needed = {BOOKING_ACTION} if self.purpose else {FIRST_PROFILE_ACTION}
        if not holds_any(access, needed):
            # A preparer reads the vocabulary a profile pinned. Reading it with no
            # profile at all is preparing the profile, which is configuration work.
            raise Refusal(
                "ACTION_DENIED",
                "Reading vocabulary without a profile takes booking or configuration authority.",
            )
        assert self.dimension is not None  # E226 required it above.
        if self.dimension not in vocabulary(access.tenant_id, timezone.now(), self.dimension):
            # Nothing governs this dimension. An empty page would say something
            # else - that it is governed and has nothing selectable - and a
            # profile editor could read that as the approved "unrestricted" list.
            raise Refusal("NOT_FOUND", "No vocabulary is in force for that dimension.")
        self.profile_version_id = None
        self.dimensions = (self.dimension,)
        self.brands = brand_scope(access, VOCAB_ACTIONS)


REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}

#: One governed vocabulary choice (E226-E229). ``value_choice`` and
#: ``crosswalk_choice`` answer the same shape deliberately: a screen offering a
#: value and a screen offering a mapping candidate read the same row type.
#: ``source_key``, ``issuer_key``, ``target_id`` and ``target_label`` are filled
#: only by a crosswalk. ``match`` is filled only by E229: ``exact`` or
#: ``suggestion`` (a close match a person may accept; ``reason`` says why).
VOCAB_CHOICE: dict[str, Any] = {
    "type": "object",
    "description": "VocabularyChoiceDTO (E226-E229).",
    "properties": {
        "id": {"type": "string"},
        "dimension": {"type": "string"},
        "value": {"type": "string"},
        "label": {"type": "string"},
        "state": {
            "type": "string",
            "enum": ["effective", "retired", "unresolved", "proposed", "rejected"],
        },
        "source_key": {"type": "string", "nullable": True},
        "issuer_key": {"type": "string", "nullable": True},
        "target_id": {"type": "string", "nullable": True},
        "target_label": {"type": "string", "nullable": True},
        "profile_version_id": {"type": "string", "nullable": True},
        "revision": {"type": "integer"},
        "match": {"type": "string", "enum": ["exact", "suggestion"], "nullable": True},
        "reason": {"type": "string", "nullable": True},
    },
    # Every field is always answered; the nullable ones carry null, not absence.
    "required": [
        "id",
        "dimension",
        "value",
        "label",
        "state",
        "source_key",
        "issuer_key",
        "target_id",
        "target_label",
        "profile_version_id",
        "revision",
        "match",
        "reason",
    ],
}

VOCAB_PAGE: dict[str, Any] = {
    "type": "object",
    "description": "Page<VocabularyChoiceDTO>.",
    "properties": {
        "items": {"type": "array", "items": VOCAB_CHOICE},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
    "required": ["items", "next_cursor", "as_of"],
}

VOCAB_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<VocabularyChoiceDTO> for the decided crosswalk (E230, E231).",
    "properties": {
        "id": {"type": "string"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "state": {"type": "string"},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "context": {"type": "object", "additionalProperties": True},
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
        "data": VOCAB_CHOICE,
    },
}

#: E226's complete input contract. `profile_version_id` is the only optional-by
#: -design field: without it the caller is preparing the first profile (needing
#: `config.draft`) or reading booking choices (`purpose=booking`), and then
#: `dimension` is required instead.
CONTROLLED_PARAMETERS = [
    OpenApiParameter(
        "profile_version_id",
        str,
        description=(
            "An effective identity or PT profile version. Omit it to read the governed "
            "vocabulary itself, before any profile exists; that read takes `config.draft`, "
            "or `booking.manage` with `purpose=booking`."
        ),
    ),
    OpenApiParameter(
        "dimension",
        str,
        required=True,
        description=(
            "One governed dimension, at most 60 characters. A dimension no vocabulary "
            "governs is NOT_FOUND, never an empty page."
        ),
    ),
    OpenApiParameter(
        "purpose",
        str,
        enum=[BOOKING_PURPOSE],
        description=(
            "`booking` reads the effective choices a booking needs, including size and "
            "colour, without configuration-draft or approval authority. Retired values "
            "are left out. Any other value is INVALID_REQUEST."
        ),
    ),
    OpenApiParameter(
        "subject_revision_id",
        str,
        description="The draft revision this read is for; it must be in the caller's scope.",
    ),
    OpenApiParameter(
        "q", str, description="Case-insensitive search of value key and label; max 100 characters."
    ),
    OpenApiParameter("cursor", str, description="Opaque cursor for the next page."),
    OpenApiParameter("limit", int, description="Page size, 1 to 100."),
]

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


def value_choice(
    value: VocabularyValue,
    profile_version_id: uuid.UUID | None,
    *,
    match: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(value.id),
        "dimension": value.dimension,
        "value": value.value_key,
        "label": value.label,
        "state": "retired" if value.retired else "effective",
        "source_key": None,
        "issuer_key": None,
        "target_id": None,
        "target_label": None,
        "profile_version_id": opt_id(profile_version_id),
        "revision": value.version,
        "match": match,
        "reason": reason,
    }


def target_labels(tenant_id: uuid.UUID, rows: list[SourceCrosswalk]) -> dict[str, str]:
    """What each crosswalk target is called: a vocabulary value's label or a brand's name."""
    labels: dict[str, str] = {}
    if any(is_attribute_kind(row.kind) and row.target_key for row in rows):
        labels.update(value_labels(tenant_id))
    brand_ids = {
        int(row.target_key) for row in rows if row.kind == "brand" and row.target_key.isdigit()
    }
    if brand_ids:
        labels.update(
            (f"brand:{pk}", name)
            for pk, name in Brand.objects.filter(pk__in=brand_ids).values_list("pk", "name")
        )
    return labels


def crosswalk_choice(
    row: SourceCrosswalk,
    labels: dict[str, str] | None = None,
    *,
    match: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if row.governance_state == GovernanceState.PENDING:
        state = "unresolved" if row.target_key == "" else "proposed"
    else:
        state = str(row.governance_state)
    label_key = f"brand:{row.target_key}" if row.kind == "brand" else row.target_key
    return {
        "id": str(row.pk),
        "dimension": row.kind,
        "value": row.source_key,
        "label": row.source_key,
        "state": state,
        "source_key": row.source_key,
        "issuer_key": row.issuer_key,
        "target_id": row.target_key or None,
        "target_label": (labels or {}).get(label_key) if row.target_key else None,
        "profile_version_id": opt_id(row.config_version_id),
        "revision": row.revision,
        "match": match,
        "reason": reason,
    }


def in_brand_scope(brands: set[int] | None, row: SourceCrosswalk) -> bool:
    brand_id = crosswalk_brand(row)
    return brands is None or brand_id is None or brand_id in brands


def vocabulary_items(access: AccessContext, query: VocabularyQuery) -> list[VocabularyValue]:
    values = vocabulary(access.tenant_id, timezone.now())
    out: list[VocabularyValue] = []
    for dimension in sorted(values):
        if query.dimension is not None and dimension != query.dimension:
            continue
        if query.dimensions and dimension not in query.dimensions:
            continue
        out.extend(values[dimension])
    if query.purpose == BOOKING_PURPOSE:
        # A booking is written now, so a retired value is not a choice for it.
        out = [value for value in out if not value.retired]
    return out


class GoodsControlledValuesView(GoodsAPIView):
    """E226: approved vocabulary values for the profile's dimensions.

    Also the read the first profile is chosen from, and the buyer's booking
    reference read; see :class:`VocabularyQuery`.
    """

    @extend_schema(
        parameters=CONTROLLED_PARAMETERS,
        responses=_responses(200, VOCAB_PAGE, _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        query = VocabularyQuery(access, request, crosswalks=False, controlled=True)
        needle = query.q.casefold()
        items = [
            value_choice(value, query.profile_version_id)
            for value in vocabulary_items(access, query)
            if not needle
            or needle in value.value_key.casefold()
            or needle in value.label.casefold()
        ]
        window, cursor = paginate(items, query.params)
        return Response(page(window, cursor))


def pending_crosswalks(
    access: AccessContext, query: VocabularyQuery, *, unresolved: bool
) -> list[dict[str, Any]]:
    queryset = SourceCrosswalk.objects.filter(
        tenant_id=access.tenant_id, governance_state=GovernanceState.PENDING
    )
    queryset = queryset.filter(target_key="") if unresolved else queryset.exclude(target_key="")
    if query.dimension is not None:
        queryset = queryset.filter(kind=query.dimension)
    if query.q:
        queryset = queryset.filter(source_key__icontains=query.q)
    rows = [
        row
        for row in queryset.order_by("kind", "issuer_key", "source_key", "id")
        if in_brand_scope(query.brands, row)
    ]
    labels = target_labels(access.tenant_id, rows)
    return [crosswalk_choice(row, labels) for row in rows]


class GoodsReviewListView(GoodsAPIView):
    """E227: source keys still waiting for a crosswalk target (master or attribute rule)."""

    @extend_schema(responses=_responses(200, VOCAB_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        query = VocabularyQuery(access, request, crosswalks=True)
        window, cursor = paginate(pending_crosswalks(access, query, unresolved=True), query.params)
        return Response(page(window, cursor))


class GoodsProposalListView(GoodsAPIView):
    """E228: proposed crosswalk targets (master or attribute rule) awaiting the owner."""

    @extend_schema(responses=_responses(200, VOCAB_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        query = VocabularyQuery(access, request, crosswalks=True)
        window, cursor = paginate(pending_crosswalks(access, query, unresolved=False), query.params)
        return Response(page(window, cursor))


class GoodsSuggestView(GoodsAPIView):
    """E229: mapping candidates for one source text. Reads only; nothing is applied.

    Exact candidates first (``match: exact``): confirmed crosswalks and attribute
    rules whose source key is the text, then approved values whose key or label
    is it. Then close matches (``match: suggestion``, store and warehouse
    operations PRD §5.4): approved values equal after normalising, a known
    abbreviation (``NVY`` -> ``NAVY``) or a close spelling, each with its
    ``reason``. Deterministic - no AI, no ranking model - and a person accepts
    any of them; none is ever chosen for them.
    """

    @extend_schema(responses=_responses(200, VOCAB_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        query = VocabularyQuery(access, request, crosswalks=False)
        needle = normalise_text(query.q)
        items: list[dict[str, Any]] = []
        if needle:
            now = timezone.now()
            crosswalks = SourceCrosswalk.objects.filter(
                tenant_id=access.tenant_id,
                governance_state=GovernanceState.EFFECTIVE,
                source_key__iexact=query.q,
            ).filter(Q(retired_at__isnull=True) | Q(retired_at__gt=now))
            if query.dimension is not None:
                crosswalks = crosswalks.filter(kind=query.dimension)
            rows = [
                row
                for row in crosswalks.order_by("kind", "issuer_key", "id")
                if in_brand_scope(query.brands, row)
            ]
            labels = target_labels(access.tenant_id, rows)
            items.extend(
                crosswalk_choice(row, labels, match="exact", reason="confirmed_rule")
                for row in rows
            )
            values = [value for value in vocabulary_items(access, query) if not value.retired]
            exact = [
                value
                for value in values
                if needle in (normalise_text(value.value_key), normalise_text(value.label))
            ]
            items.extend(
                value_choice(value, query.profile_version_id, match="exact", reason="exact_value")
                for value in exact
            )
            items.extend(_close_items(query, values, exclude={str(v.id) for v in exact}))
        window, cursor = paginate(items, query.params)
        return Response(page(window, cursor))


def _close_items(
    query: VocabularyQuery, values: list[VocabularyValue], *, exclude: set[str]
) -> list[dict[str, Any]]:
    """Close matches per dimension, each read with that dimension's own clean-up."""
    by_dimension: dict[str, list[VocabularyValue]] = {}
    for value in values:
        by_dimension.setdefault(value.dimension, []).append(value)
    out: list[dict[str, Any]] = []
    for dimension in sorted(by_dimension):
        rows = by_dimension[dimension]
        by_id = {str(value.id): value for value in rows}
        texts = [query.q, *clean_for_dimension(dimension, query.q)]
        candidates = close_matches(
            texts,
            [RuleValue(str(v.id), v.dimension, v.value_key, v.label) for v in rows],
            exclude=exclude,
        )
        out.extend(
            value_choice(
                by_id[candidate.value.id],
                query.profile_version_id,
                match="suggestion",
                reason=candidate.reason,
            )
            for candidate in candidates
        )
    return out


class CrosswalkDecision(GoodsAPIView):
    """E230/E231: propose a target (preparer) or confirm/reject it (product master owner)."""

    route = "resolve"

    @extend_schema(
        request={"application/json": {
            "type": "object",
            "required": ["command_id", "contract_version", "expected_revision", "action", "reason_code", "reviewed_hash"],
            "properties": {
                "command_id": {"type": "string", "format": "uuid"},
                "contract_version": {"type": "string", "enum": ["goods-v1"]},
                "expected_revision": {"type": "integer", "minimum": 1},
                "action": {"type": "string", "enum": ["propose", "confirm", "reject"]},
                "chosen_value_id": {"type": "string"},
                "reason_code": {"type": "string", "minLength": 1, "maxLength": 60},
                "reviewed_hash": {"type": "string"},
            },
            "additionalProperties": False,
        }},
        responses=_responses(200, VOCAB_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:  # noqa: C901 - the contract's ordered refusal steps
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"action", "chosen_value_id", "reason_code", "reviewed_hash"},
            required=["action", "reason_code", "reviewed_hash"],
        )
        action = body["action"]
        if action not in ("propose", "confirm", "reject"):
            raise invalid("action must be propose, confirm or reject.", field="action")
        reason = bounded_text(body["reason_code"], "reason_code", 60)
        reviewed = str(body["reviewed_hash"])
        raw_choice = body.get("chosen_value_id")
        chosen = (
            bounded_text(str(raw_choice), "chosen_value_id", 100)
            if raw_choice not in (None, "")
            else None
        )
        require_any(access, VOCAB_ACTIONS)
        brands = brand_scope(access, VOCAB_ACTIONS)
        current = SourceCrosswalk.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
        if current is None or not in_brand_scope(brands, current):
            raise Refusal("NOT_FOUND", "That mapping was not found.")
        target_brand = crosswalk_brand(current)
        if target_brand is not None:
            # Recorded, so the commit-time re-check replays the brand this relied on.
            access.can_reach_brand(VOCAB_ACTIONS, target_brand)
        if action == "propose":
            if not holds_any(access, PROPOSER_ACTIONS):
                raise Refusal("ACTION_DENIED", "You cannot propose crosswalk targets.")
            if chosen is None:
                raise Refusal(
                    "INVALID_REQUEST",
                    "chosen_value_id is required to propose.",
                    issues=[
                        issue("REQUIRED", "chosen_value_id is required", field="chosen_value_id")
                    ],
                )
        elif not holds_any(access, {CROSSWALK_MANAGE}):
            raise Refusal(
                "ACTION_DENIED", "Only the product master owner confirms or rejects a crosswalk."
            )
        clean = {
            "action": action,
            "chosen_value_id": chosen,
            "reason_code": reason,
            "reviewed_hash": reviewed,
        }

        def handler(run: CommandRun) -> CommandResult:
            row = run.lock(
                LockRank.DOCUMENT, SourceCrosswalk.objects.filter(tenant_id=run.tenant_id, pk=pk)
            )[0]
            check_revision(meta.expected_revision, row.revision)
            if (
                row.governance_state != GovernanceState.PENDING
                or content_hash(crosswalk_choice(row, target_labels(run.tenant_id, [row])))
                != reviewed
            ):
                raise Refusal(
                    "REVISION_SUPERSEDED",
                    "This mapping changed or was already decided; reload the queue.",
                )
            run.audit_before = crosswalk_data(row)
            if action == "reject":
                row.governance_state = GovernanceState.RETIRED
                row.retired_at = run.now
            else:
                target = chosen if chosen is not None else row.target_key
                if not target:
                    raise invalid("chosen_value_id is required.", field="chosen_value_id")
                target_brand = rule_brand(row.kind, row.issuer_key, target)
                if not crosswalk_target_valid(row.kind, target, run.tenant_id) or (
                    brands is not None and target_brand is not None and target_brand not in brands
                ):
                    raise Refusal(
                        "IDENTITY_PICK_STALE",
                        "That target is retired or outside your scope; choose another.",
                        status=409,
                    )
                row.target_key = target
                if action == "confirm":
                    row.governance_state = GovernanceState.EFFECTIVE
            row.revision += 1
            row.save(update_fields=["governance_state", "retired_at", "target_key", "revision"])
            record_master_version(
                run, "crosswalk", row, retired=action == "reject", reason_code=reason
            )
            run.audit_after = crosswalk_data(row)
            return CommandResult(resource_type="crosswalk", resource_id=str(row.pk))

        result = self.run_command(
            request,
            access=access,
            action=f"ptmapper.crosswalk.{self.route}.{action}",
            meta=meta,
            business_input=clean,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"crosswalk:{pk}",
            reviewed_hash=reviewed,
        )
        row = SourceCrosswalk.objects.get(pk=pk)
        choice = crosswalk_choice(row, target_labels(access.tenant_id, [row]))
        return Response(
            resource_dto(
                id=row.pk,
                data=choice,
                revision=row.revision,
                state=choice["state"],
                context={"brand_id": crosswalk_brand(row)},
            ),
            status=result.status_code,
        )


class GoodsReviewResolveView(CrosswalkDecision):
    """E230."""

    route = "resolve"


class GoodsProposalDecideView(CrosswalkDecision):
    """E231."""

    route = "decide"
