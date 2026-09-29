"""Opening manifest and variance routes (E106, E107, E137, E138, E235, GSA-T10)."""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    decode_cursor,
    encode_cursor,
    page,
    paginate,
    parse_int_id,
    parse_meta,
    resource_dto,
)
from accounts.principal import AccessContext
from approvals.goods_models import ActionDraft, ApprovalRequest
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from ptmapper import goods_manifest_services as services
from ptmapper.goods_models import (
    OpeningManifest,
    OpeningManifestRow,
    OpeningSeasonCorrection,
    OpeningVariance,
)
from ptmapper.goods_pt_services import _uuid_or_none

#: Any of these lets a person see an opening manifest: preparing it, or deciding
#: its manifest/variance/PT approvals.
READ_ACTIONS = (
    "pt.prepare.opening",
    services.MANIFEST_ACTION,
    services.VARIANCE_ACTION,
    services.APPROVE_OPENING,
)

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}

MANIFEST_ROW_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "One opening manifest row: its OpeningRow, its physical verification, "
    "and, when it differs, its variance decision.",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "source_row_key": {"type": "string"},
        "row": {"type": "object", "additionalProperties": True},
        "verification": {"type": "object", "additionalProperties": True},
        "matches_verification": {"type": "boolean"},
        "variance": {"type": "object", "nullable": True, "additionalProperties": True},
        "variance_request_id": {"type": "string", "nullable": True},
        "season_correction": {
            "type": "object",
            "nullable": True,
            "description": "The latest governed season correction on this row (OPS-03).",
            "additionalProperties": True,
        },
    },
}

MANIFEST_DATA: dict[str, Any] = {
    "type": "object",
    "description": "OpeningManifestDetailDTO (E106, E107).",
    "properties": {
        "site_id": {"type": "string"},
        "dataset_key": {"type": "string"},
        "batch_key": {"type": "string"},
        "revision": {"type": "integer"},
        "cutoff_at": {"type": "string", "format": "date-time", "nullable": True},
        "approved": {"type": "boolean"},
        "approval_request_id": {"type": "string", "nullable": True},
        "profile_version_id": {"type": "string", "nullable": True},
        "rows": {
            "type": "object",
            "description": "Row page (500 per page), ordered by `source_row_key` and never "
            "silently truncated. `total` counts the whole manifest; follow `next_cursor` "
            "through `row_cursor` to read the rest.",
            "properties": {
                "items": {"type": "array", "items": MANIFEST_ROW_ITEM},
                "next_cursor": {"type": "string", "nullable": True},
                "total": {"type": "integer"},
            },
        },
    },
}

MANIFEST_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<OpeningManifestDetailDTO>.",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "state": {"type": "string"},
        "context": {"type": "object", "additionalProperties": True},
        "data": MANIFEST_DATA,
    },
}

SEASON_CORRECTION_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": (
        "OpeningSeasonCorrectionDTO (OPS-03). The appended fact that an opening "
        "row's real season is now known. The manifest row, its verification and "
        "the opening PT's official version are never edited."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "manifest_row_id": {"type": "string", "format": "uuid"},
        "from_season_id": {"type": "string"},
        "to_season_id": {"type": "string"},
        "reason": {"type": "string"},
        "recorded_at": {"type": "string", "format": "date-time"},
    },
}

VARIANCE_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "The created variance request's id (E138); its approved "
    "OpeningVariancePayload is read back through E107's row.",
    "properties": {"variance_request_id": {"type": "string", "format": "uuid"}},
}

MANIFEST_LIST_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "OpeningManifestSummaryDTO (E106).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "site_id": {"type": "string"},
        "dataset_key": {"type": "string"},
        "batch_key": {"type": "string"},
        "revision": {"type": "integer"},
        "state": {"type": "string", "enum": ["draft", "approved"]},
        "row_count": {"type": "integer"},
        "created_at": {"type": "string", "format": "date-time"},
    },
}

MANIFEST_LIST_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "Page<OpeningManifestSummaryDTO>, newest first.",
    "properties": {
        "items": {"type": "array", "items": MANIFEST_LIST_ITEM},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}

ROW_PAGE = 500

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


_TEXT = {"type": "string"}
_UUID = {"type": "string", "format": "uuid"}
_LEGACY_ID = {"oneOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": "^[0-9]+$"}]}
_ATTRIBUTE_ID = {"oneOf": [_UUID, *_LEGACY_ID["oneOf"]]}
_FIELD_ID = {"oneOf": [*_ATTRIBUTE_ID["oneOf"], {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,59}$"}]}
_PAISE = {"type": "string", "pattern": "^[0-9]+$"}
_CONDITION = {"type": "string", "enum": ["good", "damaged", "wrong", "unidentified"]}


def _mutation_request(
    fields: dict[str, Any], *, required: tuple[str, ...], revision_bound: bool
) -> dict[str, Any]:
    required_fields = ["command_id", "contract_version", *required]
    if revision_bound:
        required_fields.append("expected_revision")
    return {
        "type": "object",
        "required": required_fields,
        "properties": {
            "command_id": _UUID,
            "contract_version": {"type": "string", "enum": ["goods-v1"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            **fields,
        },
        "additionalProperties": False,
    }


_IDENTITY_REQUEST = {
    "type": "object",
    "properties": {
        "sku_id": _UUID,
        "attributes": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "required": ["field_id"],
                "properties": {
                    "field_id": _FIELD_ID,
                    "vocabulary_value_id": _ATTRIBUTE_ID,
                    "supplied_text": {"type": "string", "maxLength": 240},
                    "unknown": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "raw_alias": _TEXT,
        "description": _TEXT,
    },
    "additionalProperties": False,
}
_OPENING_ROW_REQUEST = {
    "type": "object",
    "required": ["source_row_key", "site_id", "condition", "identity", "qty", "basic_paise", "mrp_paise", "hsn", "season_id"],
    "properties": {
        "source_row_key": {"type": "string", "minLength": 1, "maxLength": 100},
        "site_id": _LEGACY_ID,
        "location_id": _UUID,
        "condition": _CONDITION,
        "identity": _IDENTITY_REQUEST,
        "qty": {"type": "integer", "minimum": 1, "maximum": 999999},
        "basic_paise": _PAISE,
        "mrp_paise": _PAISE,
        "hsn": _TEXT,
        "tax_version_id": _UUID,
        "season_id": _LEGACY_ID,
        "season_unknown_historical": {"type": "boolean"},
        "older_origin_at": {"type": "string", "format": "date-time"},
        "older_origin_ref": _TEXT,
        "commercial_label": _TEXT,
    },
    "additionalProperties": False,
}
_MANIFEST_ROW_REQUEST = {
    "type": "object",
    "required": ["row", "verification"],
    "properties": {
        "row": _OPENING_ROW_REQUEST,
        "verification": {
            "type": "object",
            "required": ["observed_qty", "observed_condition"],
            "properties": {
                "observed_qty": {"type": "integer", "minimum": 0},
                "observed_condition": _CONDITION,
                "notes": _TEXT,
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}
_MANIFEST_FIELDS = {
    "cutoff_at": {"type": "string", "format": "date-time"},
    "source_evidence_id": _UUID,
    "profile_version_id": _UUID,
    "rows": {"type": "array", "items": _MANIFEST_ROW_REQUEST},
}
MANIFEST_CREATE_REQUEST = _mutation_request(
    {"site_id": _LEGACY_ID, "batch_key": _TEXT, "dataset_key": _TEXT, **_MANIFEST_FIELDS},
    required=("site_id", "batch_key", "dataset_key", "cutoff_at", "source_evidence_id", "rows"),
    revision_bound=False,
)
MANIFEST_REVISE_REQUEST = _mutation_request(
    _MANIFEST_FIELDS, required=("cutoff_at", "source_evidence_id", "rows"), revision_bound=True
)
VARIANCE_REQUEST = _mutation_request(
    {"manifest_row_id": _UUID, "accepted_qty": {"type": "integer", "minimum": 0},
     "accepted_condition": _CONDITION, "reason_code": _TEXT,
     "evidence_ids": {"type": "array", "items": _UUID, "maxItems": 20}},
    required=("manifest_row_id", "accepted_qty", "reason_code"), revision_bound=True,
)
SEASON_CORRECTION_REQUEST = _mutation_request(
    {"season_id": _LEGACY_ID, "reason": _TEXT},
    required=("season_id", "reason"), revision_bound=False,
)


def _has_read_action(access: AccessContext) -> bool:
    return any(action in access.all_actions() for action in READ_ACTIONS)


def _can_read(access: AccessContext, site_id: int) -> bool:
    return any(access.can(a, site_id=site_id) for a in READ_ACTIONS)


def _load(access: AccessContext, pk: uuid.UUID) -> OpeningManifest:
    if not _has_read_action(access):
        raise Refusal("ACTION_DENIED", "You do not have permission to read opening manifests.")
    manifest = (
        OpeningManifest.objects.select_related("current_version", "approved_version")
        .filter(tenant_id=access.tenant_id, pk=pk)
        .first()
    )
    if manifest is None or not _can_read(access, manifest.site_id):
        raise Refusal("NOT_FOUND", "That opening manifest was not found.")
    return manifest


def _manifest_summary(manifest: OpeningManifest) -> dict[str, Any]:
    version = manifest.current_version
    return {
        "id": str(manifest.pk),
        "record_contract": "goods-v1",
        "site_id": str(manifest.site_id),
        "dataset_key": manifest.dataset_key,
        "batch_key": manifest.batch_key,
        "revision": manifest.revision,
        "state": "approved" if manifest.approved_version_id else "draft",
        "row_count": version.row_count if version else 0,
        "created_at": manifest.created_at.isoformat(),
    }


def _row_dto(
    row: OpeningManifestRow,
    variances: dict[uuid.UUID, OpeningVariance],
    pending_variances: dict[uuid.UUID, uuid.UUID],
    corrections: dict[uuid.UUID, OpeningSeasonCorrection],
) -> dict[str, Any]:
    verification = row.verification
    matches = services.matches_verification(row)
    variance = variances.get(row.pk)
    pending_id = pending_variances.get(row.pk)
    correction = corrections.get(row.pk)
    return {
        "id": str(row.pk),
        "source_row_key": row.source_row_key,
        # `row.payload` stays exactly as the loader wrote it, correction or not:
        # the original season is part of the evidence (OPS-03).
        "row": row.payload,
        "verification": verification,
        "matches_verification": matches,
        "variance": variance.decision if variance else None,
        "variance_request_id": str(pending_id) if pending_id else None,
        "season_correction": services.correction_dto(correction) if correction else None,
    }


def _pending_variance_requests(
    access: AccessContext, site_id: int, rows: list[OpeningManifestRow]
) -> dict[uuid.UUID, uuid.UUID]:
    """Row id -> pending opening_variance ApprovalRequest id, for rows that have one.

    An ``ActionDraft.subject_key`` for a variance is ``manifest_row:{row_id}:{command_id}``
    (``propose_variance``); its ``ApprovalRequest.subject_key`` is ``action_draft:{draft_id}``,
    and the draft's own payload names the row it is about.

    The lookup starts from the site's pending requests, not from the rows. A row
    cannot name its draft exactly - the command id that completes the key is not
    derivable from the row - so asking row by row costs one unindexable prefix
    match per row, up to a page of 500, and ticket 10A made the screen pay that
    on every page of the manifest rather than once. Pending opening variances at
    one site are the checker's inbox: few, and reached through indexed columns.
    Reading them, then the drafts they name by primary key, costs two queries of
    a fixed shape however many rows the page holds - and still never reads every
    opening-variance draft in the tenant.
    """
    row_ids = {str(row.pk) for row in rows}
    if not row_ids:
        return {}
    request_by_draft: dict[str, uuid.UUID] = {}
    for request in ApprovalRequest.objects.filter(
        tenant_id=access.tenant_id,
        subject_kind="opening_variance",
        state=ApprovalRequest.State.PENDING,
        site_id=site_id,
    ):
        draft_id = _uuid_or_none(request.subject_key.removeprefix("action_draft:"))
        if draft_id is not None:
            request_by_draft[str(draft_id)] = request.pk
    if not request_by_draft:
        return {}
    pending: dict[uuid.UUID, uuid.UUID] = {}
    for draft in ActionDraft.objects.filter(
        tenant_id=access.tenant_id,
        subject_kind="opening_variance",
        pk__in=list(request_by_draft),
    ):
        row_id = _uuid_or_none(draft.payload.get("manifest_row_id"))
        if row_id is not None and str(row_id) in row_ids:
            pending[row_id] = request_by_draft[str(draft.pk)]
    return pending


def _manifest_resource(
    access: AccessContext, manifest: OpeningManifest, *, row_cursor: str | None = None
) -> dict[str, Any]:
    version = manifest.current_version
    # E106/E107 step 6: row arrays are paged, with a total and a next cursor - a
    # manifest may carry up to MAX_ROWS rows and must never be silently truncated.
    #
    # The order is `source_row_key`, which `uq_manifestrow_key` makes unique
    # within the version: a reader walking the cursor to the end sees every row
    # exactly once. Without an explicit order the database may answer two
    # OFFSET windows in different orders, which silently repeats some rows and
    # drops others - and the primary key is a random UUID, so it orders nothing
    # a person would recognise.
    ordered = (
        OpeningManifestRow.objects.filter(manifest_version=version).order_by("source_row_key")
        if version
        else OpeningManifestRow.objects.none()
    )
    total = ordered.count()
    offset = decode_cursor(row_cursor)
    rows = list(ordered[offset : offset + ROW_PAGE])
    variances = {
        v.manifest_row_id: v
        for v in OpeningVariance.objects.filter(manifest_row__in=rows).order_by("recorded_at")
    }
    pending_variances = _pending_variance_requests(access, manifest.site_id, rows)
    corrections: dict[uuid.UUID, OpeningSeasonCorrection] = {}
    for correction in OpeningSeasonCorrection.objects.filter(manifest_row__in=rows).order_by(
        "recorded_at", "event_at"
    ):
        corrections[correction.manifest_row_id] = correction
    pending = ApprovalRequest.objects.filter(
        tenant_id=access.tenant_id,
        subject_kind=ApprovalRequest.SubjectKind.MANIFEST,
        subject_key=str(manifest.pk),
        state=ApprovalRequest.State.PENDING,
    ).first()
    approved_version = manifest.approved_version
    data = {
        "site_id": str(manifest.site_id),
        "dataset_key": manifest.dataset_key,
        "batch_key": manifest.batch_key,
        "revision": manifest.revision,
        "cutoff_at": version.cutoff_at.isoformat() if version else None,
        "approved": manifest.approved_version_id is not None,
        "approval_request_id": str(pending.pk) if pending else None,
        # What `POST files/from-manifest/{id}` (E123's opening analogue) needs
        # to price its prefilled lines - the profile pinned on the exact
        # version the owner approved, not merely the manifest's current one.
        "profile_version_id": (
            str(approved_version.profile_version_id)
            if approved_version and approved_version.profile_version_id
            else None
        ),
        "rows": {
            "items": [_row_dto(row, variances, pending_variances, corrections) for row in rows],
            "next_cursor": (
                encode_cursor(offset + ROW_PAGE) if offset + ROW_PAGE < total else None
            ),
            "total": total,
        },
    }
    return resource_dto(
        id=manifest.pk,
        data=data,
        revision=manifest.revision,
        state="approved" if manifest.approved_version_id else "draft",
        context={"site_id": manifest.site_id},
        content={"hash": version.content_hash if version else ""},
    )


class GoodsOpeningManifestListView(GoodsAPIView):
    """E106 list, E137 create."""

    @extend_schema(responses=_responses(200, MANIFEST_LIST_RESPONSE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        if not _has_read_action(access):
            raise Refusal("ACTION_DENIED", "You do not have permission to read opening manifests.")
        queryset = OpeningManifest.objects.select_related("current_version").filter(
            tenant_id=access.tenant_id
        )
        if params.get("site_id"):
            queryset = queryset.filter(site_id=parse_int_id(params["site_id"], "site_id"))
        rows = [m for m in queryset.order_by("-created_at", "pk") if _can_read(access, m.site_id)]
        window, cursor = paginate(rows, params)
        return Response(page([_manifest_summary(m) for m in window], cursor))

    @extend_schema(request={"application/json": MANIFEST_CREATE_REQUEST}, responses=_responses(201, MANIFEST_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {
                "site_id",
                "batch_key",
                "dataset_key",
                "cutoff_at",
                "source_evidence_id",
                "profile_version_id",
                "rows",
            },
            required=[
                "site_id",
                "batch_key",
                "dataset_key",
                "cutoff_at",
                "source_evidence_id",
                "rows",
            ],
        )
        site_id = _site_id(body["site_id"])
        access.require("pt.prepare.opening", site_id=site_id)
        body = {**body, "site_id": site_id}

        def handler(run: CommandRun) -> CommandResult:
            manifest = services.create_manifest(run, body=body)
            return CommandResult(
                resource_type="opening_manifest", resource_id=str(manifest.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="opening.manifest.create",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=site_id,
        )
        manifest = _load(access, uuid.UUID(str(result.resource_id)))
        return Response(_manifest_resource(access, manifest), status=result.status_code)


def _site_id(raw: Any) -> int:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        raise Refusal("INVALID_REQUEST", "site_id must be an ID.") from None


class GoodsOpeningManifestDetailView(GoodsAPIView):
    """E107 detail, E235 revise."""

    @extend_schema(
        operation_id="goods_v1_ptmapper_opening_manifests_detail",
        parameters=[
            OpenApiParameter(
                "row_cursor",
                str,
                description=(
                    f"Opaque cursor for the next page of rows ({ROW_PAGE} per page). "
                    "Absent or empty reads the first page; follow `data.rows.next_cursor` "
                    "until it is null to read every row of the manifest. "
                    "Rows are ordered by `source_row_key`, and a manifest revised "
                    "between two pages answers a new `revision` and `content_hash`: "
                    "a reader that sees either change is reading a superseded manifest "
                    "and must start again."
                ),
            ),
        ],
        responses=_responses(200, MANIFEST_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, {"row_cursor"})
        return Response(
            _manifest_resource(access, _load(access, pk), row_cursor=params.get("row_cursor"))
        )

    @extend_schema(request={"application/json": MANIFEST_REVISE_REQUEST}, responses=_responses(200, MANIFEST_RESOURCE, _WRITE_REFUSALS))
    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"cutoff_at", "source_evidence_id", "profile_version_id", "rows"},
            required=["cutoff_at", "source_evidence_id", "rows"],
        )
        manifest = _load(access, pk)
        access.require("pt.prepare.opening", site_id=manifest.site_id)

        def handler(run: CommandRun) -> CommandResult:
            services.patch_manifest(
                run, manifest, body=body, expected_revision=meta.expected_revision
            )
            return CommandResult(resource_type="opening_manifest", resource_id=str(pk))

        self.run_command(
            request,
            access=access,
            action="opening.manifest.revise",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=manifest.site_id,
        )
        return Response(_manifest_resource(access, _load(access, pk)))


class GoodsOpeningVarianceView(GoodsAPIView):
    """E138: propose a distinct-approval variance for one manifest row."""

    @extend_schema(request={"application/json": VARIANCE_REQUEST}, responses=_responses(201, VARIANCE_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {
                "manifest_row_id",
                "accepted_qty",
                "accepted_condition",
                "reason_code",
                "evidence_ids",
            },
            required=["manifest_row_id", "accepted_qty", "reason_code"],
        )
        manifest = _load(access, pk)
        # E138: "C-INV; distinct C-INV or C-OWN approves" - proposing takes the
        # same preparer authority as the manifest itself; only the later
        # decide (E234, `services.VARIANCE_ACTION`) is the distinct approval.
        access.require("pt.prepare.opening", site_id=manifest.site_id)

        def handler(run: CommandRun) -> CommandResult:
            draft = services.propose_variance(
                run, manifest, body=body, expected_revision=meta.expected_revision
            )
            return CommandResult(
                resource_type="opening_variance", resource_id=str(draft.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="opening.variance.propose",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=manifest.site_id,
        )
        return Response({"variance_request_id": str(result.resource_id)}, status=result.status_code)


class GoodsOpeningSeasonCorrectionView(GoodsAPIView):
    """OPS-03: establish an opening row's real season, long after the fact.

    The person who loaded the row may have been right to say the cohort was
    unknown; this is the separate, governed event that says it is known now. It
    takes the manifest-approval authority at that site and a password
    confirmation, and it appends evidence - it never edits the manifest row, the
    opening PT's official version or any document already issued against the
    stock.

    Deliberately not a second-human action: the Owner who approved the manifest
    may correct it themselves (the ticket's own ruling), because withholding the
    correction leaves the wrong season on the shelf.
    """

    @extend_schema(request={"application/json": SEASON_CORRECTION_REQUEST}, responses=_responses(201, SEASON_CORRECTION_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID, row_id: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, {"season_id", "reason"}, required=["season_id", "reason"]
        )
        manifest = _load(access, pk)
        access.require(services.MANIFEST_ACTION, site_id=manifest.site_id)
        season_id = parse_int_id(body["season_id"], "season_id")
        reason = str(body["reason"])

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up()
            correction = services.record_season_correction(
                run,
                manifest=manifest,
                manifest_row_id=row_id,
                season_id=season_id,
                reason=reason,
            )
            return CommandResult(
                resource_type="opening_season_correction",
                resource_id=str(correction.pk),
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action="opening.season.correct",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=manifest.site_id,
        )
        correction = OpeningSeasonCorrection.objects.get(pk=uuid.UUID(str(result.resource_id)))
        return Response(services.correction_dto(correction), status=result.status_code)
