"""First-party review of legacy brand identity, without business-data disclosure."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, parse_meta
from accounts.sessions import grant_step_up
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from masters.brand_identity import RESOURCES, bind_reviewed_rows, reconciliation_page
from masters.models import Brand


REQUEST: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["command_id", "contract_version", "resource", "rows", "current_password"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "resource": {"type": "string", "enum": sorted(RESOURCES)},
        "current_password": {"type": "string", "writeOnly": True},
        "rows": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "brand_id", "fingerprint"],
            "properties": {"id": {"type": "integer"}, "brand_id": {"type": "integer"},
                           "fingerprint": {"type": "string", "minLength": 64, "maxLength": 64}},
        }},
    },
}


class BrandReconciliationView(GoodsAPIView):
    @extend_schema(responses={200: {"type": "object", "additionalProperties": True}})
    def get(self, request: Request) -> Response:
        access = self.access(request)
        # Establishing ownership must never be delegated to a brand-local role
        # that would gain access by selecting itself as an ambiguous row's owner.
        access.require("access.manage")
        label = str(request.query_params.get("resource", "stockledger.stockonhand"))
        try:
            after = int(request.query_params.get("cursor", "0"))
            limit = int(request.query_params.get("limit", "50"))
        except (ValueError, TypeError):
            raise Refusal("INVALID_REQUEST", "Cursor and limit must be integers.") from None
        if after < 0 or not 1 <= limit <= 100:
            raise Refusal("INVALID_REQUEST", "Use a nonnegative cursor and a limit from 1 to 100.")
        report = reconciliation_page(label, after=after, limit=limit)
        report["resources"] = sorted(RESOURCES)
        report["brands"] = list(Brand.objects.filter(tenant_id=access.tenant_id).order_by("code").values("id", "code", "name"))
        return Response(report)

    @extend_schema(request={"application/json": REQUEST}, responses={200: {"type": "object", "additionalProperties": True}})
    def post(self, request: Request) -> Response:
        access = self.access(request)
        access.require("access.manage")
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"resource", "rows", "current_password"}, required=["resource", "rows", "current_password"])
        label, rows = body["resource"], body["rows"]
        if not isinstance(label, str) or label not in RESOURCES:
            raise Refusal("INVALID_REQUEST", "Choose a registered reconciliation resource.")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100 or any(not isinstance(row, dict) for row in rows):
            raise Refusal("INVALID_REQUEST", "Review between 1 and 100 rows per batch.")
        password = body["current_password"]
        if not isinstance(password, str) or not password:
            raise Refusal("STEP_UP_REQUIRED", "Confirm your password to apply these identity mappings.")
        grant_step_up(request.auth, password)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            bind_reviewed_rows(run, label, rows)
            return CommandResult(resource_type="brand_identity", resource_id=label)

        self.run_command(
            request, access=access, action="access.brand_identity.bind", meta=meta,
            business_input={"resource": label, "rows": rows}, handler=handler,
            subject_key=f"brand-identity:{label}",
        )
        from core.models import AuditEvent

        event = AuditEvent.objects.filter(
            tenant_id=access.tenant_id, command_key__command_id=meta.command_id,
            action="access.brand_identity.bind",
        ).first()
        outcomes = (event.after or {}).get("outcomes", []) if event is not None else []
        return Response({"resource": label, "outcomes": outcomes,
                         "applied": sum(row["outcome"] == "migrated" for row in outcomes)})
