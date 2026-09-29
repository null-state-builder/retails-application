"""E247's HTTP boundary; the history service itself remains framework-neutral."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, check_query, page_limit
from core.goods_administrative_history import administrative_history, resolve_subject
from masters.goods_views import REFUSAL_RESPONSE

_AUDIT_VALUE = {
    "type": "object",
    "required": ["field", "redacted", "value"],
    "properties": {
        "field": {"type": "string"},
        "redacted": {"type": "boolean"},
        "value": {},
    },
}

_EVENT = {
    "type": "object",
    "required": [
        "id",
        "subject_kind",
        "subject_id",
        "actor_id",
        "actor_name",
        "recorded_at",
        "revision",
        "event_kind",
        "outcome",
        "reason_code",
        "before",
        "after",
        "evidence_ids",
    ],
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "subject_kind": {"type": "string", "enum": ["site", "master", "staff", "user", "role"]},
        "subject_id": {"type": "string"},
        "actor_id": {"type": "string", "format": "uuid", "nullable": True},
        # Null whenever `actor_id` is: the actor's name is shown only where the
        # reader's grants already reveal that person (ticket 02C).
        "actor_name": {"type": "string", "nullable": True},
        "recorded_at": {"type": "string", "format": "date-time"},
        "revision": {"type": "integer", "nullable": True},
        "event_kind": {"type": "string"},
        "outcome": {"type": "string"},
        "reason_code": {"type": "string", "nullable": True},
        "before": {"type": "array", "nullable": True, "items": _AUDIT_VALUE},
        "after": {"type": "array", "nullable": True, "items": _AUDIT_VALUE},
        "evidence_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
    },
}


class GoodsAdministrativeHistoryView(GoodsAPIView):
    """Read-only history for sites and organisation masters (02C) and people, logins and
    roles (03D). A person is ``staff/<staff id>``, a login ``user/<login id>`` and a role
    ``role/<role id>``; a hidden, unknown or malformed subject is ``NOT_FOUND`` alike."""

    @extend_schema(
        operation_id="goods_v1_administrative_history",
        parameters=[
            OpenApiParameter(
                "subject_kind",
                str,
                location=OpenApiParameter.PATH,
                enum=["site", "master", "staff", "user", "role"],
                description="What the history is of. `configuration` is not yet supported.",
            ),
            OpenApiParameter(
                "subject_id",
                str,
                location=OpenApiParameter.PATH,
                description=(
                    "site: store id; master: `entity:<id>`, `registration:<id>` or "
                    "`tenant:<uuid>`; staff: staff UUID; user: login id; role: role id. "
                    "A hidden, unknown or malformed id is NOT_FOUND alike."
                ),
            ),
            OpenApiParameter(
                "cursor",
                str,
                description="Opaque next-page cursor, at most 200 characters.",
            ),
            OpenApiParameter("limit", int, description="Events per page, from 1 through 50."),
        ],
        responses={
            200: {
                "type": "object",
                "required": ["items", "next_cursor", "as_of"],
                "properties": {
                    "items": {"type": "array", "items": _EVENT},
                    "next_cursor": {"type": "string", "nullable": True},
                    "as_of": {"type": "string", "format": "date-time"},
                },
            },
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, subject_kind: str, subject_id: str) -> Response:
        access = self.access(request)
        params: dict[str, Any] = check_query(request, {"cursor", "limit"})
        subject = resolve_subject(access, subject_kind, subject_id)
        return Response(
            administrative_history(
                access,
                subject,
                params.get("cursor") or None,
                page_limit(params, default=50, maximum=50),
            )
        )
