"""Tenant-bound access migration review in the canonical People & Access API."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.assignment_migration import build_migration_plan
from accounts.goods_api import GoodsAPIView
from accounts.models import User
from core.refusals import Refusal


class AssignmentReconciliationView(GoodsAPIView):
    @extend_schema(responses={200: {"type": "object", "additionalProperties": True}})
    def get(self, request: Request) -> Response:
        access = self.access(request)
        access.require("access.manage")
        try:
            offset = int(request.query_params.get("cursor", "0"))
            limit = int(request.query_params.get("limit", "50"))
        except (TypeError, ValueError):
            raise Refusal("INVALID_REQUEST", "Cursor and limit must be integers.") from None
        if offset < 0 or not 1 <= limit <= 100:
            raise Refusal("INVALID_REQUEST", "Use a nonnegative cursor and a limit from 1 to 100.")
        plan = build_migration_plan(access.tenant_id)
        fingerprint = plan.source_fingerprint()
        if offset and request.query_params.get("fingerprint") != fingerprint:
            raise Refusal("STALE_MIGRATION_PLAN", "Access changed. Restart the review from the first page.")
        people = {row["id"]: row for row in User.objects.filter(
            tenant_id=access.tenant_id,
        ).values("id", "human__staff__id", "full_name")}
        items: list[dict[str, Any]] = []
        for effect in plan.before_after:
            user_id = effect["user_id"]
            person = people[user_id]
            items.append({
                **effect,
                "display_name": person["full_name"],
                "staff_id": str(person["human__staff__id"]) if person["human__staff__id"] else None,
                "exclusions": [row for row in plan.blocked if row.get("user_id") == user_id],
            })
        return Response({
            "tenant_id": str(access.tenant_id), "fingerprint": fingerprint,
            "items": items[offset:offset + limit],
            "next_cursor": offset + limit if offset + limit < len(items) else None,
            "totals": {"people": len(items), "candidates": len(plan.candidates),
                       "blocked_people": len({row.get("user_id") for row in plan.blocked if row.get("user_id")}),
                       "orphaned_identities": len({row.get("human_id") for row in plan.blocked if not row.get("user_id")})},
            "policy_changes": plan.policy_changes,
            "activation_blockers": sorted({row["code"] for row in plan.blocked}),
        })
