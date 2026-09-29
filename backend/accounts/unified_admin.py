"""Canonical administration of scoped role assignments and role policy.

Each write is a step-up command, audited and followed by a different person's
review.  Old per-user action/field grants have no write path here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from django.db.models import Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.floors import floor_violations
from accounts.goods_api import GoodsAPIView, business_body, parse_meta
from accounts.goods_models import RoleAssignment
from accounts.models import Role, User
from accounts.role_assignments import INITIAL_ROLE_CODES
from accounts.sessions import bump_security_epoch, grant_step_up
from accounts.unified_policy import (
    ACTION_LEVELS, WORKFLOW_TARGET_KEY, allowed_fields_for_role,
    serialise_workflow_levels, workflow_levels,
    workflow_floor_violation,
    pending_workflow_actions,
    initial_step_actions,
)
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal
from masters.goods_models import MasterVersion
from masters.models import Brand, Store


def _revision(tenant_id: Any, kind: str, target_key: str) -> int:
    return int(MasterVersion.objects.filter(
        tenant_id=tenant_id, kind=kind, target_key=target_key
    ).aggregate(n=Max("revision"))["n"] or 1)


def _administrator(access: Any) -> None:
    # Access administration is a tenant-wide act; a store assignment cannot
    # alter another person's assignments or the policy shared by the tenant.
    access.require("access.manage")


def _confirm_password(request: Request, access: Any, password: Any) -> None:
    if not isinstance(password, str) or not password:
        raise Refusal("STEP_UP_REQUIRED", "Confirm your password to continue.")
    grant_step_up(request.auth, password)
    access.require_step_up()


def _target_user(access: Any, pk: int) -> User:
    user = User.objects.filter(pk=pk, tenant_id=access.tenant_id, human__isnull=False).first()
    if user is None:
        raise Refusal("NOT_FOUND", "That login was not found.")
    return cast(User, user)


def _assignment_data(row: RoleAssignment) -> dict[str, Any]:
    return {
        "id": str(row.pk), "role_code": row.role.code,
        "all_sites": row.all_sites, "site_ids": sorted(int(pk) for pk in row.site_ids),
        "all_brands": row.all_brands, "brand_ids": sorted(int(pk) for pk in row.brand_ids),
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
    }


ASSIGNMENT_REPLACE_REQUEST = {
    "type": "object",
    "description": "Replace current and scheduled assignments. Existing rows must include their id and unchanged effective_from; new rows omit both.",
    "required": ["command_id", "contract_version", "expected_revision", "current_password", "assignments"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer"},
        "current_password": {"type": "string", "writeOnly": True},
        "assignments": {
            "type": "array", "maxItems": 100,
            "items": {
                "type": "object",
                "required": ["role_code", "all_sites", "site_ids", "all_brands", "brand_ids"],
                "properties": {
                    "id": {"type": "string", "format": "uuid", "description": "Existing row ID; supply together with effective_from."},
                    "effective_from": {"type": "string", "format": "date-time", "description": "The original start date for an existing row; new rows omit this."},
                    "role_code": {"type": "string"},
                    "all_sites": {"type": "boolean"},
                    "site_ids": {"type": "array", "items": {"type": "integer"}},
                    "all_brands": {"type": "boolean"},
                    "brand_ids": {"type": "array", "items": {"type": "integer"}},
                    "effective_to": {"type": "string", "format": "date-time", "nullable": True},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _open_assignments(tenant_id: Any, human_id: Any, at: datetime) -> list[RoleAssignment]:
    """Show scheduled as well as current rows so a replacement can revoke both."""

    return list(
        RoleAssignment.objects.select_related("role")
        .filter(tenant_id=tenant_id, human_id=human_id)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))
        .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gt=at))
        .order_by("effective_from", "id")
    )


def _approved_custom_role_codes(tenant_id: Any) -> set[str]:
    return set(MasterVersion.objects.filter(
        tenant_id=tenant_id, kind="role", payload__has_key="role_policy"
    ).values_list("target_key", flat=True))


def _assignable_role(role: Role, tenant_id: Any) -> bool:
    return role.code in INITIAL_ROLE_CODES or (
        not role.is_system and role.code in _approved_custom_role_codes(tenant_id)
    )


def _choices(tenant_id: Any) -> dict[str, Any]:
    approved = INITIAL_ROLE_CODES | _approved_custom_role_codes(tenant_id)
    return {
        "roles": list(Role.objects.filter(
            tenant_id=tenant_id, is_active=True, code__in=approved
        ).filter(Q(code__in=INITIAL_ROLE_CODES) | Q(is_system=False)).order_by("code").values("code", "name")),
        "sites": list(Store.objects.filter(tenant_id=tenant_id, is_active=True).order_by("code").values("id", "code", "name")),
        "brands": list(Brand.objects.filter(tenant_id=tenant_id, is_active=True).order_by("code").values("id", "code", "name")),
    }


def _parse_assignment(raw: Any, *, tenant_id: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"id", "effective_from", "role_code", "all_sites", "site_ids", "all_brands", "brand_ids", "effective_to"}:
        raise Refusal("INVALID_REQUEST", "An assignment has unknown or missing fields.")
    source_id: uuid.UUID | None = None
    source_start: datetime | None = None
    if ("id" in raw) != ("effective_from" in raw):
        raise Refusal("INVALID_REQUEST", "Existing assignments require both id and effective_from.")
    if "id" in raw:
        try:
            source_id = uuid.UUID(str(raw["id"]))
        except (TypeError, ValueError):
            raise Refusal("INVALID_REQUEST", "id must be a UUID.") from None
        source_start = parse_datetime(str(raw["effective_from"]))
        if source_start is None or source_start.tzinfo is None:
            raise Refusal("INVALID_REQUEST", "effective_from must include a time zone.")
    role = Role.objects.filter(tenant_id=tenant_id, code=raw.get("role_code"), is_active=True).first()
    if role is None or not _assignable_role(role, tenant_id):
        raise Refusal("INVALID_REQUEST", "Choose an active approved role.")
    all_sites, all_brands = raw.get("all_sites"), raw.get("all_brands")
    if not isinstance(all_sites, bool) or not isinstance(all_brands, bool):
        raise Refusal("INVALID_REQUEST", "Both all-scope choices must be true or false.")
    sites, brands = raw.get("site_ids"), raw.get("brand_ids")
    if not isinstance(sites, list) or not isinstance(brands, list):
        raise Refusal("INVALID_REQUEST", "Site and brand IDs must be lists.")
    if any(isinstance(pk, bool) or not isinstance(pk, int) for pk in sites + brands):
        raise Refusal("INVALID_REQUEST", "Site and brand IDs must be integers.")
    sites, brands = sorted(set(sites)), sorted(set(brands))
    if (all_sites and sites) or (all_brands and brands) or not (all_sites or sites) or not (all_brands or brands):
        raise Refusal("INVALID_REQUEST", "Each scope must be explicit all or a nonempty selection.")
    if sites and Store.objects.filter(tenant_id=tenant_id, pk__in=sites, is_active=True).count() != len(sites):
        raise Refusal("NOT_FOUND", "A selected site was not found.")
    if brands and Brand.objects.filter(tenant_id=tenant_id, pk__in=brands, is_active=True).count() != len(brands):
        raise Refusal("NOT_FOUND", "A selected brand was not found.")
    end: datetime | None = None
    if raw.get("effective_to") is not None:
        end = parse_datetime(str(raw["effective_to"]))
        if end is None or end.tzinfo is None:
            raise Refusal("INVALID_REQUEST", "effective_to must include a time zone.")
    return {"source_id": source_id, "source_start": source_start,
            "role": role, "all_sites": all_sites, "site_ids": sites,
            "all_brands": all_brands, "brand_ids": brands, "effective_to": end}


def _same_assignment(row: RoleAssignment, item: dict[str, Any]) -> bool:
    return bool(
        row.role_id == item["role"].pk
        and row.all_sites == item["all_sites"]
        and sorted(row.site_ids) == item["site_ids"]
        and row.all_brands == item["all_brands"]
        and sorted(row.brand_ids) == item["brand_ids"]
        and row.effective_to == item["effective_to"]
    )


class UserAssignmentsView(GoodsAPIView):
    @extend_schema(responses={200: {"type": "object", "additionalProperties": True}})
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        _administrator(access)
        user = _target_user(access, pk)
        assert user.human_id is not None
        target_human_id = user.human_id
        key = f"assignments:{target_human_id}"
        return Response({"revision": _revision(access.tenant_id, "user", key),
                         "items": [_assignment_data(row) for row in _open_assignments(
                             access.tenant_id, target_human_id, timezone.now()
                         )],
                         "choices": _choices(access.tenant_id)})

    @extend_schema(request={"application/json": ASSIGNMENT_REPLACE_REQUEST},
                   responses={200: {"type": "object", "additionalProperties": True}})
    def put(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        _administrator(access)
        user = _target_user(access, pk)
        assert user.human_id is not None
        target_human_id = user.human_id
        if target_human_id == access.human_id:
            raise Refusal("SELF_ASSIGNMENT", "Another authorised person must change your assignments.", status=403)
        meta = parse_meta(request.data, revision_bound=True)
        assert meta.expected_revision is not None
        body = business_body(request.data, {"assignments", "current_password"}, required=["assignments", "current_password"])
        if not isinstance(body["assignments"], list) or len(body["assignments"]) > 100:
            raise Refusal("INVALID_REQUEST", "assignments must be a list of at most 100 rows.")
        desired = [_parse_assignment(item, tenant_id=access.tenant_id) for item in body["assignments"]]
        source_ids = [item["source_id"] for item in desired if item["source_id"] is not None]
        if len(set(source_ids)) != len(source_ids):
            raise Refusal("INVALID_REQUEST", "An existing assignment may appear only once.")
        new_keys = [(
            item["role"].pk, item["all_sites"], tuple(item["site_ids"]),
            item["all_brands"], tuple(item["brand_ids"]), item["effective_to"],
        ) for item in desired if item["source_id"] is None]
        if len(set(new_keys)) != len(new_keys):
            raise Refusal("INVALID_REQUEST", "Duplicate assignments are not allowed.")
        keys = [(
            str(item["source_id"]) if item["source_id"] else None,
            item["source_start"].isoformat() if item["source_start"] else None,
            item["role"].pk, item["all_sites"], tuple(item["site_ids"]),
            item["all_brands"], tuple(item["brand_ids"]),
            item["effective_to"].isoformat() if item["effective_to"] else None,
        ) for item in desired]
        _confirm_password(request, access, body["current_password"])
        key = f"assignments:{target_human_id}"

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.SECURITY, [key])
            revision = _revision(run.tenant_id, "user", key)
            if revision != meta.expected_revision:
                raise Refusal("STALE_REVISION", "Access changed. Reload before saving.", status=409)
            current = list(RoleAssignment.objects.select_for_update().select_related("role").filter(
                tenant_id=run.tenant_id, human_id=target_human_id,
            ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=run.now))
             .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gt=run.now)))
            before = [_assignment_data(row) for row in current]
            by_id = {row.pk: row for row in current}
            unchanged_ids: set[uuid.UUID] = set()
            edited_future_ids: set[uuid.UUID] = set()
            for item in desired:
                source_id = item["source_id"]
                if source_id is None:
                    continue
                source = by_id.get(source_id)
                if source is None or source.effective_from != item["source_start"]:
                    raise Refusal("STALE_REVISION", "An assignment changed. Reload before saving.", status=409)
                if _same_assignment(source, item):
                    unchanged_ids.add(source_id)
                elif source.effective_from > run.now:
                    edited_future_ids.add(source_id)
            for row in current:
                if row.pk in unchanged_ids or row.pk in edited_future_ids:
                    continue
                row.revoked_at = run.now
                row.save(update_fields=["revoked_at"])
            for item in desired:
                if item["source_id"] in unchanged_ids:
                    continue
                source_start = item["source_start"]
                start = source_start if source_start is not None and source_start > run.now else run.now
                if item["effective_to"] is not None and item["effective_to"] <= start:
                    raise Refusal("INVALID_REQUEST", "effective_to must be after the assignment starts.")
                if item["source_id"] in edited_future_ids:
                    row = by_id[item["source_id"]]
                    row.role = item["role"]
                    row.all_sites = item["all_sites"]
                    row.site_ids = item["site_ids"]
                    row.all_brands = item["all_brands"]
                    row.brand_ids = item["brand_ids"]
                    row.effective_to = item["effective_to"]
                    row.save(update_fields=["role", "all_sites", "site_ids", "all_brands", "brand_ids", "effective_to"])
                    continue
                RoleAssignment.objects.create(tenant_id=run.tenant_id, human_id=target_human_id,
                                              effective_from=start, role=item["role"],
                                              all_sites=item["all_sites"], site_ids=item["site_ids"],
                                              all_brands=item["all_brands"], brand_ids=item["brand_ids"],
                                              effective_to=item["effective_to"])
            after = [_assignment_data(row) for row in _open_assignments(
                run.tenant_id, target_human_id, run.now
            )]
            from accounts.goods_admin_services import audit_values, record_version
            record_version(run, kind="user", target_key=key, revision=revision + 1,
                           payload={"assignments": after})
            bump_security_epoch(target_human_id, run.tenant_id)
            run.audit_before = audit_values({"assignments": before})
            run.audit_after = audit_values({"assignments": after})
            run.audit_subject_key = key[:100]
            return CommandResult(resource_type="role_assignments", resource_id=str(target_human_id), revision=revision + 1)

        self.run_command(request, access=access, action="access.assignment.replace", meta=meta,
                         business_input={"target": pk, "assignments": keys}, handler=handler,
                         resource_ids=[str(target_human_id)], subject_key=key[:100])
        return Response({
            "revision": meta.expected_revision + 1,
            "items": [_assignment_data(row) for row in _open_assignments(
                access.tenant_id, target_human_id, timezone.now()
            )],
            "choices": _choices(access.tenant_id),
        })


class RolePolicyView(GoodsAPIView):
    @extend_schema(responses={200: {"type": "object", "additionalProperties": True}})
    def get(self, request: Request, code: str) -> Response:
        access = self.access(request)
        _administrator(access)
        role = Role.objects.filter(tenant_id=access.tenant_id, code=code, is_active=True).first()
        if role is None or (role.is_system and role.code not in INITIAL_ROLE_CODES):
            raise Refusal("NOT_FOUND", "That role was not found.")
        return Response({"revision": _revision(access.tenant_id, "role", code), "code": role.code,
                         "section_access": role.section_access,
                         "field_access": role.field_access,
                         "initial_step_defaults": initial_step_actions(code),
                         "step_actions": (role.permissions_map or {}).get("step_actions", [])})

    @extend_schema(request={"application/json": {"type": "object", "additionalProperties": True}},
                   responses={200: {"type": "object", "additionalProperties": True}})
    def put(self, request: Request, code: str) -> Response:
        access = self.access(request)
        _administrator(access)
        role = Role.objects.filter(tenant_id=access.tenant_id, code=code, is_active=True).first()
        if role is None or (role.is_system and role.code not in INITIAL_ROLE_CODES):
            raise Refusal("NOT_FOUND", "That role was not found.")
        meta = parse_meta(request.data, revision_bound=True)
        assert meta.expected_revision is not None
        body = business_body(request.data, {"section_access", "field_access", "step_actions", "current_password"},
                             required=["section_access", "field_access", "step_actions", "current_password"])
        from accounts.sections import CAPABILITY_RANK, SECTION_CODES
        sections, fields, steps = body["section_access"], body["field_access"], body["step_actions"]
        if not isinstance(sections, dict) or set(sections) != set(SECTION_CODES):
            raise Refusal("INVALID_REQUEST", "Supply all sections exactly once.")
        if any(not isinstance(value, dict) or value.get("capability") not in CAPABILITY_RANK
               for value in sections.values()):
            raise Refusal("INVALID_REQUEST", "A section has an invalid level.")
        if floor_violations(code, sections):
            raise Refusal("ACTION_DENIED", "This policy crosses a fixed business floor.")
        if not isinstance(fields, list) or not set(fields) <= allowed_fields_for_role(code):
            raise Refusal("INVALID_REQUEST", "Protected fields exceed this role's safeguards.")
        if not isinstance(steps, list) or not set(steps) <= ACTION_LEVELS.keys():
            raise Refusal("INVALID_REQUEST", "A step action is unknown.")
        if code not in {"owner", "it_admin"} and set(steps) & {"access.manage", "access.review"}:
            raise Refusal("ACTION_DENIED", "Access administration is reserved for Owner and Admin.")
        _confirm_password(request, access, body["current_password"])

        def handler(run: CommandRun) -> CommandResult:
            from accounts.goods_admin_services import audit_values, record_version
            run.advisory_lock(LockRank.SECURITY, [f"role:{code}"])
            revision = _revision(run.tenant_id, "role", code)
            if revision != meta.expected_revision:
                raise Refusal("STALE_REVISION", "Role policy changed. Reload before saving.", status=409)
            before = {"section_access": role.section_access, "field_access": role.field_access,
                      "step_actions": (role.permissions_map or {}).get("step_actions", [])}
            role.section_access = sections
            role.field_access = sorted(set(fields))
            role.permissions_map = {**(role.permissions_map or {}), "step_actions": sorted(set(steps))}
            role.save(update_fields=["section_access", "field_access", "permissions_map", "updated_at"])
            after = {"section_access": sections, "field_access": role.field_access,
                     "step_actions": role.permissions_map["step_actions"]}
            record_version(run, kind="role", target_key=code, revision=revision + 1,
                           payload={"role_policy": after})
            for human_id in RoleAssignment.objects.filter(tenant_id=run.tenant_id, role=role).values_list("human_id", flat=True).distinct():
                bump_security_epoch(human_id, run.tenant_id)
            run.audit_before = audit_values(before)
            run.audit_after = audit_values(after)
            run.audit_subject_key = f"role:{code}"
            return CommandResult(resource_type="role_policy", resource_id=str(role.pk), revision=revision + 1)

        self.run_command(request, access=access, action="access.role.policy", meta=meta,
                         business_input={"code": code, "policy": {"section_access": sections, "field_access": fields, "step_actions": steps}},
                         handler=handler, resource_ids=[str(role.pk)], subject_key=f"role:{code}")
        return Response({
            "revision": meta.expected_revision + 1, "code": role.code,
            "section_access": role.section_access, "field_access": role.field_access,
            "step_actions": (role.permissions_map or {}).get("step_actions", []),
        })


class WorkflowPolicyView(GoodsAPIView):
    """Versioned section thresholds shared by every scoped role assignment.

    Approval routes remain in the established approval-policy service; their
    role, separate-person, limit and document-state checks are applied after
    the action decision and cannot be replaced by a threshold edit.
    """

    @extend_schema(responses={200: {"type": "object", "additionalProperties": True}})
    def get(self, request: Request) -> Response:
        access = self.access(request)
        _administrator(access)
        return Response({
            "revision": _revision(access.tenant_id, "tenant", WORKFLOW_TARGET_KEY),
            "action_levels": serialise_workflow_levels(workflow_levels(access.tenant_id)),
            "upgrade_defaults": pending_workflow_actions(access.tenant_id),
        })

    @extend_schema(
        request={"application/json": {"type": "object", "additionalProperties": True}},
        responses={200: {"type": "object", "additionalProperties": True}},
    )
    def put(self, request: Request) -> Response:
        from accounts.sections import CAPABILITY_RANK, SECTION_CODES

        access = self.access(request)
        _administrator(access)
        meta = parse_meta(request.data, revision_bound=True)
        assert meta.expected_revision is not None
        body = business_body(
            request.data, {"action_levels", "current_password"},
            required=["action_levels", "current_password"],
        )
        raw = body["action_levels"]
        if not isinstance(raw, dict) or set(raw) != set(ACTION_LEVELS):
            raise Refusal("INVALID_REQUEST", "Supply every known workflow action exactly once.")
        parsed: dict[str, tuple[str, str]] = {}
        for action, entry in raw.items():
            if not isinstance(entry, dict) or set(entry) != {"section", "minimum"}:
                raise Refusal("INVALID_REQUEST", f"{action} needs one section and level.")
            section, minimum = entry["section"], entry["minimum"]
            if section not in SECTION_CODES or minimum not in CAPABILITY_RANK:
                raise Refusal("INVALID_REQUEST", f"{action} has an invalid section or level.")
            if workflow_floor_violation(action, section, minimum):
                raise Refusal("ACTION_DENIED", f"{action} crosses a fixed workflow safeguard.")
            parsed[action] = (section, minimum)
        _confirm_password(request, access, body["current_password"])

        def handler(run: CommandRun) -> CommandResult:
            from accounts.goods_admin_services import audit_values, record_version

            run.advisory_lock(LockRank.SECURITY, [WORKFLOW_TARGET_KEY])
            revision = _revision(run.tenant_id, "tenant", WORKFLOW_TARGET_KEY)
            if revision != meta.expected_revision:
                raise Refusal("STALE_REVISION", "Workflow policy changed. Reload before saving.", status=409)
            before = serialise_workflow_levels(workflow_levels(run.tenant_id))
            after = serialise_workflow_levels(parsed)
            record_version(
                run, kind="tenant", target_key=WORKFLOW_TARGET_KEY,
                revision=revision + 1, payload={"action_levels": after},
                reason_code="so03_workflow_policy",
            )
            for human_id in RoleAssignment.objects.filter(
                tenant_id=run.tenant_id
            ).values_list("human_id", flat=True).distinct():
                bump_security_epoch(human_id, run.tenant_id)
            run.audit_before = audit_values({"action_levels": before})
            run.audit_after = audit_values({"action_levels": after})
            run.audit_subject_key = WORKFLOW_TARGET_KEY
            return CommandResult(
                resource_type="workflow_policy", resource_id=str(run.tenant_id),
                revision=revision + 1,
            )

        self.run_command(
            request, access=access, action="access.workflow.policy", meta=meta,
            business_input={"action_levels": serialise_workflow_levels(parsed)},
            handler=handler, subject_key=WORKFLOW_TARGET_KEY,
        )
        return Response({
            "revision": meta.expected_revision + 1,
            "action_levels": serialise_workflow_levels(parsed),
        })
