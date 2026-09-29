"""Goods-v1 command status and operations health (E191, E192).

These live in the project layer because they read across the kernel's own
records rather than any one domain: whether a command committed, and whether the
outbox, anchors and numbering ceilings are keeping up.
"""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, check_query
from core.commands import outcome_status_for
from core.refusals import Refusal

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}

#: `core.commands.outcome_status`. A command identity the caller never ran reads
#: `not_recorded` rather than 404: "I have no record of that" is the answer, and
#: it is the same answer whether the command was never sent or never reached us.
COMMAND_STATUS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "CommandStatusDTO (E191).",
    "properties": {
        "status": {
            # `CommandOutcome.Outcome` has exactly these two, plus the
            # "I have no record" answer. It never says `failed` or `unknown`:
            # a refusal is recorded as `refused`, and an uncertain commit has
            # no outcome row at all, which reads `not_recorded` (GSA-T18 fixed
            # the two invented values this schema used to document).
            "type": "string",
            "enum": ["not_recorded", "succeeded", "refused"],
        },
        "http_status": {"type": "integer", "nullable": True},
        "result": {"type": "object", "nullable": True, "additionalProperties": True},
    },
}

#: E192's checks. Each names what it observed, the threshold it was measured
#: against, when it was actually checked and what the check covered, so
#: "attention" is always readable without a second query.
OPERATIONS_HEALTH_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": (
        "OperationsHealthDTO (E192). A read of the results the worker's scheduled "
        "pass recorded (alerts.goods_health); loading it never runs a check."
    ),
    "required": ["checked_at", "items"],
    "properties": {
        "checked_at": {
            "type": "string",
            "format": "date-time",
            "nullable": True,
            "description": "When the newest recorded pass ran; null if none has.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "code",
                    "state",
                    "observed",
                    "threshold",
                    "owner_role",
                    "checked_at",
                    "scope",
                    "last_ok_at",
                    "last_attention_at",
                    "issue_id",
                ],
                "properties": {
                    "code": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["ok", "attention", "unavailable"],
                        "description": (
                            "`unavailable`: not measured - no scheduled result has been "
                            "recorded yet, or no provider is configured to measure."
                        ),
                    },
                    "observed": {"type": "string"},
                    "threshold": {"type": "string"},
                    "owner_role": {"type": "string"},
                    "checked_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                        "description": "When this result was measured; null if never.",
                    },
                    "scope": {
                        "type": "string",
                        "nullable": True,
                        "description": "What the recorded check covered.",
                    },
                    "last_ok_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                        "description": "The newest recorded passing result, kept through failures.",
                    },
                    "last_attention_at": {
                        "type": "string",
                        "format": "date-time",
                        "nullable": True,
                        "description": "The newest recorded failing result.",
                    },
                    "issue_id": {
                        "type": "string",
                        "format": "uuid",
                        "nullable": True,
                        "description": "The owned exception to open for this result.",
                    },
                },
            },
        },
    },
}

#: Provider health nobody measures yet. Named so the screen says "not measured"
#: rather than leaving the reader to assume a provider is fine because it is
#: absent. No provider is chosen, and the local adapters prove nothing about one.
UNMEASURED_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("email_provider", "email"),
    ("notification_provider", "notification"),
)


class CommandStatusView(GoodsAPIView):
    """E191: did my command identity commit, and with what outcome?"""

    @extend_schema(
        responses={
            200: COMMAND_STATUS_RESPONSE,
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            503: REFUSAL_RESPONSE,
        },
        parameters=[
            OpenApiParameter(
                name="principal_key",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Whose command identity to resolve (E191). Omitted, it is the "
                    "caller's own. Another principal's needs the audit grant."
                ),
            )
        ],
    )
    def get(self, request: Request, command_id: uuid.UUID) -> Response:
        access = self.access(request)
        principal = access.principal()
        # E191's input carries an optional `principal_key`: an authorised audit
        # reader resolves someone else's command identity, which is what makes
        # "did their command commit?" answerable at all during an incident. Only
        # `audit.view` may ask, and only for this tenant's keys.
        wanted = check_query(request, {"principal_key"}).get("principal_key") or principal.key
        if len(wanted) > 100:
            raise Refusal("INVALID_REQUEST", "principal_key must be at most 100 characters.")
        # `can`, not `all_actions`: a `CommandKey` carries no site, so it is a
        # site-less record, and only a grant unlimited in site and brand covers
        # one. A single-site audit reader must not read every principal's
        # commands tenant-wide.
        if wanted != principal.key and not access.can("audit.view"):
            raise Refusal("ACTION_DENIED", "You may not read another principal's command.")
        return Response(outcome_status_for(access.tenant_id, wanted, command_id))


class OperationsHealthView(GoodsAPIView):
    """E192: each check's last scheduled result and history; reading never runs a check.

    Refusals: AUTH_REQUIRED (no session), INVALID_REQUEST (any query parameter),
    ACTION_DENIED (no `ops.health.view`).
    """

    @extend_schema(
        responses={
            200: OPERATIONS_HEALTH_RESPONSE,
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            503: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        if "ops.health.view" not in access.all_actions():
            raise Refusal("ACTION_DENIED", "You may not view operations health.")
        check_query(request, set())
        from alerts.goods_health import HEALTH_ACTION, SCHEDULED_CODES, subject_for
        from core.kernel_models import AuditEvent

        # **Read, never run.** Every check - the full evidence verification above
        # all - runs on the worker's clock (alerts.goods_health). This is three
        # bounded reads of what those passes recorded: the newest result per
        # check, the newest pass and the newest failure. A page load therefore
        # cannot claim a check ran, and cannot make up a last success.
        subjects = [subject_for(code) for code in SCHEDULED_CODES]
        recorded = AuditEvent.objects.filter(
            tenant_id=access.tenant_id, action=HEALTH_ACTION, subject_key__in=subjects
        )

        def newest(rows: Any) -> dict[str, Any]:
            ordered = rows.order_by("subject_key", "-recorded_at", "-id").distinct("subject_key")
            return {row.subject_key: row for row in ordered}

        latest = newest(recorded)
        last_ok = newest(recorded.filter(outcome="ok"))
        last_attention = newest(recorded.filter(outcome="attention"))

        def stamp(row: Any) -> str | None:
            return row.event_at.isoformat() if row is not None else None

        items: list[dict[str, Any]] = []
        for code in SCHEDULED_CODES:
            subject = subject_for(code)
            row = latest.get(subject)
            after = (row.after or {}) if row is not None else {}
            items.append(
                {
                    "code": code,
                    "state": row.outcome if row is not None else "unavailable",
                    "observed": after.get("observed")
                    or "No scheduled check has recorded a result yet.",
                    "threshold": after.get("threshold") or "",
                    "owner_role": after.get("owner_role") or "X-PLT",
                    "checked_at": stamp(row),
                    "scope": after.get("scope"),
                    "last_ok_at": stamp(last_ok.get(subject)),
                    "last_attention_at": stamp(last_attention.get(subject)),
                    "issue_id": after.get("issue_id"),
                }
            )
        for code, what in UNMEASURED_PROVIDERS:
            items.append(
                {
                    "code": code,
                    "state": "unavailable",
                    "observed": (
                        f"Not measured: no {what} provider is configured or measured here. "
                        "A local adapter is not production proof."
                    ),
                    "threshold": "pending a chosen provider",
                    "owner_role": "X-PLT",
                    "checked_at": None,
                    "scope": None,
                    "last_ok_at": None,
                    "last_attention_at": None,
                    "issue_id": None,
                }
            )
        stamps = [row.event_at for row in latest.values()]
        return Response({"checked_at": max(stamps).isoformat() if stamps else None, "items": items})
