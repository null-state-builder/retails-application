"""The shared HTTP shape of every goods-v1 endpoint (design §6.1).

A goods view does five things in the order the endpoint contracts list them:
authenticate the session (and CSRF on writes), validate the closed input, resolve
the resource inside trusted scope, check the action and field grants, then run
exactly one command through ``core.commands.execute_command``. This module gives
views those steps as small helpers so no endpoint re-implements them.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import ServerSessionAuthentication
from accounts.principal import AccessContext, resolve_access
from core.canonical import content_hash
from core.commands import (
    CONTRACT_VERSION,
    CommandResult,
    CommandRun,
    CommandSpec,
    execute_command,
    record_refusal_outside_command,
)
from core.refusals import Refusal, issue

logger = logging.getLogger("kdps.goods")

MUTATION_META_KEYS = frozenset({"command_id", "contract_version", "expected_revision"})
LIST_QUERY_KEYS = frozenset({"q", "cursor", "limit", "site_id", "brand_id", "sbu_id"})


class GoodsAPIView(APIView):
    """Base for goods-v1 endpoints: session auth, closed errors, one command."""

    authentication_classes = [ServerSessionAuthentication]
    permission_classes: list[Any] = []
    goods_contract = True
    goods_command_id: uuid.UUID | None = None
    goods_command_started = False

    def access(self, request: Request) -> AccessContext:
        return resolve_access(request)

    def handle_exception(self, exc: Exception) -> Response:
        # Design §4.1: a write refused before its command runs (scope, grant, closed
        # input) still leaves a safe attempt. A command records its own refusals.
        # A view that names its command_id is about to run (or ran) that command.
        started = self.goods_command_started or self.goods_command_id is not None
        if isinstance(exc, Refusal) and not started:
            request = getattr(self, "request", None)
            access = getattr(request, "_goods_access", None)
            method = str(getattr(request, "method", "GET")).lower()
            if access is not None and method not in ("get", "head", "options"):
                match = getattr(request, "resolver_match", None)
                route = getattr(match, "url_name", None) or type(self).__name__
                kwargs = sorted((getattr(match, "kwargs", None) or {}).items())
                try:
                    record_refusal_outside_command(
                        access.principal(),
                        action=f"{method} {route}",
                        refusal=exc,
                        subject_key=",".join(f"{k}={v}" for k, v in kwargs) or None,
                    )
                except Exception:
                    # Never let the evidence write replace the refusal the caller is owed.
                    logger.exception("goods-v1 refusal evidence not recorded: %s", exc.code)
        return super().handle_exception(exc)

    def run_command(
        self,
        request: Request,
        *,
        access: AccessContext,
        action: str,
        meta: MutationMeta,
        business_input: Any,
        handler: Callable[[CommandRun], CommandResult],
        resource_ids: Iterable[str] = (),
        evidence_hashes: Iterable[str] = (),
        subject_key: str | None = None,
        site_id: int | None = None,
        reviewed_hash: str | None = None,
    ) -> CommandResult:
        self.goods_command_id = meta.command_id
        self.goods_command_started = True
        spec = CommandSpec(
            action=action,
            command_id=meta.command_id,
            business_input={"input": business_input, "expected_revision": meta.expected_revision},
            resource_ids=[str(r) for r in resource_ids],
            evidence_hashes=list(evidence_hashes),
            subject_key=subject_key,
            site_id=site_id,
            reviewed_hash=reviewed_hash,
        )
        return execute_command(access.principal(), spec, handler)


class MutationMeta:
    def __init__(self, command_id: uuid.UUID, expected_revision: int | None) -> None:
        self.command_id = command_id
        self.expected_revision = expected_revision


def parse_meta(data: Any, *, revision_bound: bool) -> MutationMeta:
    if not isinstance(data, dict):
        raise Refusal("INVALID_REQUEST", "The request body must be a JSON object.")
    try:
        command_id = uuid.UUID(str(data.get("command_id")))
    except (TypeError, ValueError):
        raise Refusal(
            "INVALID_REQUEST",
            "A command_id UUID is required.",
            issues=[issue("REQUIRED", "command_id must be a UUID", field="command_id")],
        ) from None
    if data.get("contract_version") != CONTRACT_VERSION:
        raise Refusal(
            "INVALID_REQUEST",
            "contract_version must be goods-v1.",
            issues=[
                issue("INVALID", "contract_version must be goods-v1", field="contract_version")
            ],
        )
    expected = data.get("expected_revision")
    if revision_bound:
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise Refusal(
                "INVALID_REQUEST",
                "expected_revision is required.",
                issues=[
                    issue(
                        "REQUIRED",
                        "expected_revision must be a positive integer",
                        field="expected_revision",
                    )
                ],
            )
    elif expected is not None and (
        isinstance(expected, bool) or not isinstance(expected, int) or expected < 1
    ):
        raise Refusal("INVALID_REQUEST", "expected_revision must be a positive integer.")
    return MutationMeta(command_id, expected if isinstance(expected, int) else None)


def business_body(
    data: dict[str, Any], allowed: Iterable[str], *, required: Iterable[str] = ()
) -> dict[str, Any]:
    """The body without MutationMeta, refusing unknown or missing keys."""
    allowed_set = set(allowed)
    body = {k: v for k, v in data.items() if k not in MUTATION_META_KEYS}
    unknown = sorted(set(body) - allowed_set)
    if unknown:
        raise Refusal(
            "INVALID_REQUEST",
            f"Unknown field(s): {', '.join(unknown)}.",
            issues=[
                issue("UNKNOWN_FIELD", f"{name} is not accepted", field=name) for name in unknown
            ],
        )
    missing = [name for name in required if body.get(name) in (None, "")]
    if missing:
        raise Refusal(
            "INVALID_REQUEST",
            f"Missing field(s): {', '.join(missing)}.",
            issues=[issue("REQUIRED", f"{name} is required", field=name) for name in missing],
        )
    return body


def check_query(request: Request, allowed: Iterable[str] = LIST_QUERY_KEYS) -> dict[str, str]:
    allowed_set = set(allowed)
    params = {key: str(request.query_params.get(key, "")) for key in request.query_params}
    unknown = sorted(set(params) - allowed_set)
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown query parameter(s): {', '.join(unknown)}.")
    return params


def page_limit(params: dict[str, str], default: int = 50, maximum: int = 100) -> int:
    raw = params.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise Refusal("INVALID_REQUEST", "limit must be an integer.") from None
    if value < 1 or value > maximum:
        raise Refusal("INVALID_REQUEST", f"limit must be between 1 and {maximum}.")
    return value


def resource_dto(
    *,
    id: Any,
    data: Any,
    revision: int,
    state: str,
    context: dict[str, Any],
    record_contract: str = "goods-v1",
    number: str | None = None,
    version: int | None = None,
    allowed_actions: Iterable[str] = (),
    history_cursor: str | None = None,
    content: Any = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": str(id),
        "record_contract": record_contract,
        "revision": revision,
        "content_hash": content_hash(content if content is not None else data),
        "state": state,
        "number": number,
        "version": version,
        "context": {
            "site_id": _id(context.get("site_id")),
            "entity_id": _id(context.get("entity_id")),
            "brand_id": _id(context.get("brand_id")),
            "sbu_id": _id(context.get("sbu_id")),
            # GSA-T07: a PT's own live `OfficialVersion` id, when a caller's
            # context names one (`ptmapper.goods_pt_views.pt_resource`) — the
            # one thing E139 needs to open an acceptance session and the one
            # thing no other read carries. `None` for every other resource,
            # which never sets it.
            "official_version_id": _id(context.get("official_version_id")),
        },
        "data": data,
        "allowed_actions": sorted(set(allowed_actions)),
        "history_cursor": history_cursor,
        "as_of": (as_of or timezone.now()).isoformat(),
    }


def _id(value: Any) -> str | None:
    return None if value is None else str(value)


def respond(result: CommandResult, body: Any) -> Response:
    return Response(body, status=result.status_code)


def check_revision(expected: int | None, current: int) -> None:
    if expected is not None and expected != current:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Someone changed this record after you loaded it. "
            "Reload and review the current version.",
        )


def check_reviewed_hash(reviewed: str | None, current: str) -> None:
    if not reviewed or reviewed != current:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "What you reviewed is no longer the current version. Reload and review it again.",
        )


def parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise Refusal(
            "INVALID_REQUEST",
            f"{field} must be an ID.",
            issues=[issue("INVALID", "not an ID", field=field)],
        ) from None


def parse_int_id(value: Any, field: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise Refusal(
            "INVALID_REQUEST",
            f"{field} must be an ID.",
            issues=[issue("INVALID", "not an ID", field=field)],
        ) from None
    if parsed < 1:
        raise Refusal("INVALID_REQUEST", f"{field} must be an ID.")
    return parsed


def now() -> datetime:
    return timezone.now()


def encode_cursor(offset: int) -> str:
    raw = json.dumps({"o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    if len(cursor) > 200:
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
        offset = int(value["o"])
    except (ValueError, KeyError, TypeError):
        raise Refusal("INVALID_REQUEST", "cursor is not valid.") from None
    if offset < 0:
        raise Refusal("INVALID_REQUEST", "cursor is not valid.")
    return offset


#: The query half of ``Page<T>`` for a list read through ``paginate`` with its
#: defaults, documented once so every paged picker declares the same contract.
PAGE_PARAMETERS: list[OpenApiParameter] = [
    OpenApiParameter(
        "cursor",
        str,
        required=False,
        description="Opaque next-page cursor from the previous page's next_cursor.",
    ),
    OpenApiParameter(
        "limit", int, required=False, description="Items per page, 1 through 100 (default 50)."
    ),
]


def paginate(
    items: list[Any], params: dict[str, str], *, default: int = 50, maximum: int = 100
) -> tuple[list[Any], str | None]:
    offset = decode_cursor(params.get("cursor"))
    limit = page_limit(params, default=default, maximum=maximum)
    window = items[offset : offset + limit]
    next_cursor = encode_cursor(offset + limit) if offset + limit < len(items) else None
    return window, next_cursor


def page(items: list[Any], next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor, "as_of": timezone.now().isoformat()}
