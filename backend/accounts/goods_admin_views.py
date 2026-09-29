"""Goods-v1 staff, logins, roles, grants and privileged-change review endpoints.

E061-E065, E070-E084, E214 and E237. Each write resolves scope and grants first,
then runs exactly one command; the resource is always re-read and rendered after
the command so a replay answers with what the caller may see now.

Several of these routes used to share a path with the legacy two-administrator
admin screens, so drf-spectacular described the legacy operation and never these
(#303). They answer under ``/api/goods-v1/auth/admin/...`` alone now. Every view
here hand-builds its response through ``accounts.goods_admin_services``, so the
schemas below are what makes the emitted contract the goods one.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from django.utils.crypto import salted_hmac
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts import goods_admin_services as svc
from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    page,
    paginate,
    parse_int_id,
    parse_meta,
)
from accounts.models import Role, User
from accounts.principal import AccessContext
from accounts.role_assignments import INITIAL_ROLE_CODES
from accounts.till_pin import (
    ADMIN_SET_ACTION,
    RESET_ACTION,
    hash_till_pin,
    may_hold_till_pin,
    may_reset_till_pin,
    pin_problem,
    write_pin,
)
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal

DETAIL_QUERY: frozenset[str] = frozenset()


def _nothing_to_change() -> Refusal:
    return Refusal("INVALID_REQUEST", "Send at least one field to change.")


# -- staff (E061-E065, E070) ---------------------------------------------------------------


REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


def _resource(data_schema: dict[str, Any], description: str) -> dict[str, Any]:
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
            "context": {"type": "object", "additionalProperties": True},
            "allowed_actions": {"type": "array", "items": {"type": "string"}},
            "data": data_schema,
        },
    }


def _page(item_schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "items": {"type": "array", "items": item_schema},
            "next_cursor": {"type": "string", "nullable": True},
            "as_of": {"type": "string", "format": "date-time"},
        },
    }


#: ``staff_dto``. ``mobile`` is personal data and appears only for a caller whose
#: field grant covers it at that site — its absence is a denial, not an empty row.
#: ``site_id`` is null for head-office staff, who have no primary site (GSA-T03).
STAFF_DATA: dict[str, Any] = {
    "type": "object",
    "description": "StaffDTO (E061-E065).",
    "properties": {
        "human_id": {"type": "string", "format": "uuid"},
        "staff_code": {"type": "string"},
        "display_name": {"type": "string"},
        "salesperson": {"type": "boolean"},
        "site_id": {"type": "string", "nullable": True},
        "effective_from": {"type": "string", "format": "date-time", "nullable": True},
        "mobile": {"type": "string", "nullable": True},
    },
}

#: E062's request body. ``site_id`` and ``effective_from`` are sent together or
#: not at all (GSA-T03): omitting both creates a head-office person with no
#: assignment, and half a pair is refused ``INVALID_REQUEST``. Only tenant-wide
#: staff authority may omit them - a site-scoped caller is refused
#: ``STAFF_INVALID`` and must name the site the person works at.
STAFF_CREATE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "StaffDTO create request (E062).",
    "required": ["command_id", "contract_version", "staff_code", "display_name"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "human_id": {"type": "string", "format": "uuid"},
        "staff_code": {"type": "string", "maxLength": 40},
        "display_name": {"type": "string", "maxLength": 160},
        "mobile": {"type": "string", "maxLength": 30, "nullable": True},
        "salesperson": {"type": "boolean"},
        "site_id": {"type": "string", "nullable": True},
        "effective_from": {"type": "string", "format": "date-time", "nullable": True},
    },
}

STAFF_UPDATE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "Correct at least one staff field with the current resource revision (E064).",
    "required": ["command_id", "contract_version", "expected_revision"],
    "additionalProperties": False,
    "properties": {
        **STAFF_CREATE_REQUEST["properties"],
        "expected_revision": {"type": "integer", "minimum": 1},
    },
}
STAFF_RETIRE_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": [
        "command_id",
        "contract_version",
        "expected_revision",
        "reason_code",
        "effective_at",
    ],
    "additionalProperties": False,
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer", "minimum": 1},
        "reason_code": {"type": "string", "maxLength": 60},
        "effective_at": {"type": "string", "format": "date-time"},
    },
}
STAFF_ASSIGN_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": [
        "command_id",
        "contract_version",
        "expected_revision",
        "site_id",
        "effective_from",
        "reason_code",
    ],
    "additionalProperties": False,
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer", "minimum": 1},
        "site_id": {"type": "integer", "minimum": 1},
        "effective_from": {"type": "string", "format": "date-time"},
        "reason_code": {"type": "string", "maxLength": 60},
    },
}

#: ``assignment_dto``: one effective-dated placement, not the staff record.
ASSIGNMENT_DATA: dict[str, Any] = {
    "type": "object",
    "description": "StaffAssignmentDTO (E070).",
    "properties": {
        "staff_id": {"type": "string", "format": "uuid"},
        "site_id": {"type": "string"},
        "effective_from": {"type": "string", "format": "date-time"},
        "effective_to": {"type": "string", "format": "date-time", "nullable": True},
        "primary": {"type": "boolean"},
    },
}

#: ``grant_dto``: one live role grant, its scope and what it may reach.
GRANT_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "RoleGrantDTO (E074, E081).",
    "properties": {
        "id": {"type": "string"},
        "human_id": {"type": "string", "format": "uuid"},
        "role_id": {"type": "string"},
        "role_code": {"type": "string"},
        "scope": {"type": "object", "additionalProperties": True},
        "actions": {
            "type": "object",
            "properties": {
                "actions": {"type": "array", "items": {"type": "string"}},
                "scope_kind": {"type": "string"},
            },
        },
        "fields": {"type": "array", "items": {"type": "string"}},
        "effective_from": {"type": "string", "format": "date-time", "nullable": True},
        "effective_to": {"type": "string", "format": "date-time", "nullable": True},
    },
}

#: ``user_dto``. Assignment scope is read and written through the canonical
#: ``/api/auth/admin/users/{id}/assignments`` endpoint.
USER_DATA: dict[str, Any] = {
    "type": "object",
    "description": "LoginDTO (E071-E074).",
    "properties": {
        "human_id": {"type": "string", "nullable": True},
        "email": {"type": "string"},
        "display_name": {"type": "string"},
        "active": {"type": "boolean"},
        # GSA-T03/ticket 03A: true from a create or assisted reset that issued a
        # temporary password, until this login's own E239 change-password succeeds.
        "must_change_password": {"type": "boolean"},
        # Store operations ticket 06: whether this login has a counter PIN, and
        # whether it may hold one at all - never the PIN or its hash.
        "has_till_pin": {"type": "boolean"},
        "may_hold_till_pin": {"type": "boolean"},
        "may_reset_till_pin": {"type": "boolean"},
    },
}

#: E073's request body. ``identity_email_confirmed`` (GSA-T03/ticket 03A) is
#: required, and must be ``true``, whenever ``password`` is sent - refused
#: ``IDENTITY_NOT_CONFIRMED`` otherwise (``accounts.goods_admin_services.parse_login``).
USER_CREATE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "LoginDTO create request (E073).",
    "required": ["command_id", "contract_version", "human_id", "email", "display_name"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "human_id": {"type": "string"},
        "email": {"type": "string"},
        "display_name": {"type": "string"},
        "active": {"type": "boolean"},
        "password": {"type": "string", "maxLength": 1024},
        "identity_email_confirmed": {"type": "boolean"},
    },
}

#: E074's request body - an assisted reset is this same PATCH with ``password``
#: set. Send at least one field. ``identity_email_confirmed`` carries the same
#: requirement as the create request above, and only means something alongside
#: ``password`` - sent alone, it is refused ``INVALID_REQUEST``.
USER_UPDATE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "LoginDTO update request (E074). Send at least one field.",
    "required": ["command_id", "contract_version"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "email": {"type": "string"},
        "display_name": {"type": "string"},
        "active": {"type": "boolean"},
        "password": {"type": "string", "maxLength": 1024},
        "identity_email_confirmed": {"type": "boolean"},
    },
}

#: ``role_data``.
ROLE_DATA: dict[str, Any] = {
    "type": "object",
    "description": "RoleDTO (E075-E080).",
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string", "nullable": True},
        "active": {"type": "boolean"},
    },
}

#: E077's request body (``RoleInput`` + ``MutationMeta``). A code that is not one
#: of ``accounts.actions.ROLE_TEMPLATES`` creates a role with no registered
#: maximum: it can be named and edited, but E080 refuses it
#: ``ROLE_WITHOUT_TEMPLATE`` and E081 refuses granting it ``ROLE_INVALID``
#: (GSA-T03, ticket 03C).
ROLE_CREATE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "RoleDTO create request (E077).",
    "required": ["command_id", "contract_version", "code", "name"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "code": {"type": "string", "minLength": 1, "maxLength": 40, "pattern": "^[A-Za-z0-9_-]+$"},
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "description": {"type": "string", "maxLength": 240, "nullable": True},
        "active": {"type": "boolean"},
    },
}

#: E078's request body. Send at least one field; ``code`` never changes
#: (``ROLE_CODE_FIXED``) and is accepted only when it equals the current code.
ROLE_UPDATE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "RoleDTO update request (E078). Send at least one field.",
    "required": ["command_id", "contract_version", "expected_revision"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer"},
        "code": {"type": "string", "minLength": 1, "maxLength": 40, "pattern": "^[A-Za-z0-9_-]+$"},
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "description": {"type": "string", "maxLength": 240, "nullable": True},
        "active": {"type": "boolean"},
    },
}

#: One E080 entry: a scope kind (no scope object) with the actions and fields the
#: role may carry there, each within the role's template.
ROLE_ACCESS_ITEM: dict[str, Any] = {
    "type": "object",
    "required": ["scope", "actions", "fields"],
    "properties": {
        "scope": {
            "type": "object",
            "required": ["scope_kind"],
            "properties": {
                "scope_kind": {
                    "type": "string",
                    "enum": ["tenant", "entity", "site", "sbu", "brand"],
                }
            },
        },
        "actions": {
            "type": "object",
            "required": ["actions"],
            "properties": {
                "actions": {"type": "array", "items": {"type": "string"}, "maxItems": 200},
                # Optional; when sent it must equal ``scope.scope_kind``
                # (``SCOPE_INCONSISTENT`` otherwise).
                "scope_kind": {
                    "type": "string",
                    "enum": ["tenant", "entity", "site", "sbu", "brand"],
                },
            },
        },
        "fields": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
}

#: E080's request body. The union of every entry becomes the role's maximum,
#: replacing the last one; an empty list leaves the role no actions or fields.
ROLE_ACCESS_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "Role access (maximum) request (E080).",
    "required": ["command_id", "contract_version", "expected_revision", "grants"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer"},
        "grants": {"type": "array", "items": ROLE_ACCESS_ITEM, "maxItems": 100},
    },
}

#: E082/E083: an effective approved approval policy version, or an open draft.
POLICY_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<ApprovalPolicyPayload> (E082, E083).",
    "additionalProperties": True,
    "properties": {
        "id": {"type": "string"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "state": {"type": "string"},
        "version": {"type": "integer", "nullable": True},
        "data": {"type": "object", "additionalProperties": True},
    },
}

#: ``privileged_dtos``: one privileged audit event and every review recorded on
#: it. ``before``/``after`` are redacted values — never a password or a token.
PRIVILEGED_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "PrivilegedChangeDTO (E084, E237).",
    "additionalProperties": True,
    "properties": {
        "action": {"type": "string"},
        "subject_key": {"type": "string", "nullable": True},
        "actor_id": {"type": "string", "nullable": True},
        "actor_name": {"type": "string", "nullable": True},
        "service_code": {"type": "string", "nullable": True},
        "outcome": {"type": "string"},
        "reason_code": {"type": "string", "nullable": True},
        "site_id": {"type": "string", "nullable": True},
        "before": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "after": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "event_at": {"type": "string", "format": "date-time"},
        "recorded_at": {"type": "string", "format": "date-time"},
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reviewer_id": {"type": "string"},
                    "reviewer_name": {"type": "string", "nullable": True},
                    "note": {"type": "string", "nullable": True},
                    "reviewed_at": {"type": "string", "format": "date-time"},
                },
            },
        },
    },
}

#: E214: site labels for People & Access. Roles and access policy have their
#: own canonical endpoints; the old grant/action/template catalog is retired.
ADMIN_META_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "AdminMetaDTO (E214): sites visible to this administrator.",
    "additionalProperties": False,
    "required": ["sites"],
    "properties": {
        "sites": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    },
}

#: #173: who holds what, and the maximum each role may ever hold.
ACCESS_MATRIX_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "AccessMatrixDTO (#173).",
    "additionalProperties": True,
    "properties": {
        "roles": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "grants": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"code": {"type": "string"}, "label": {"type": "string"}},
            },
        },
        "restrictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"action": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        # A role's effective maximum: its template narrowed by E080. A role with
        # no registered template has no row here.
        "role_maxima": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["role_code", "actions", "fields", "scope_kinds"],
                "properties": {
                    "role_code": {"type": "string"},
                    "actions": {"type": "array", "items": {"type": "string"}},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "scope_kinds": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

STAFF_RESOURCE = _resource(STAFF_DATA, "ResourceDTO<StaffDTO>.")
ASSIGNMENT_RESOURCE = _resource(ASSIGNMENT_DATA, "ResourceDTO<StaffAssignmentDTO>.")
USER_RESOURCE = _resource(USER_DATA, "ResourceDTO<LoginDTO>.")
ROLE_RESOURCE = _resource(ROLE_DATA, "ResourceDTO<RoleDTO>.")
STAFF_PAGE = _page(STAFF_RESOURCE, "Page<ResourceDTO<StaffDTO>>.")
# E062 step 8: only tenant-wide staff.manage may create a head-office person
# with no site. The People screen (E061, ticket 03B) reads this off the same
# list it already fetches rather than guessing from `session.sites`, so it
# never offers a choice the server is certain to refuse.
STAFF_PAGE["properties"]["can_create_unplaced"] = {
    "type": "boolean",
    "description": "Whether the caller's staff.manage reaches tenant-wide, so they may "
    "create a head-office person with no site (E062).",
}
USER_PAGE = _page(USER_RESOURCE, "Page<ResourceDTO<LoginDTO>>.")
ROLE_PAGE = _page(ROLE_RESOURCE, "Page<ResourceDTO<RoleDTO>>.")
POLICY_PAGE = _page(POLICY_ITEM, "Page<ResourceDTO<ApprovalPolicyPayload>>.")
PRIVILEGED_PAGE = _page(PRIVILEGED_ITEM, "Page<PrivilegedChangeDTO>, newest first.")


class GoodsStaffListCreateView(GoodsAPIView):
    """E061 lists staff in scope; E062 creates one, with or without a first assignment."""

    @extend_schema(responses=_responses(200, STAFF_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        window, cursor = paginate(svc.list_staff(access, params), params)
        body = page([svc.staff_dto(access, s, p) for s, p in window], cursor)
        # Same check E062 (POST, below) makes of the site-less branch - so the
        # screen that lists staff also knows, without guessing from
        # `session.sites`, whether "Add person" may offer a head-office choice.
        body["can_create_unplaced"] = access.can_at_store("staff.manage", None)
        return Response(body)

    @extend_schema(
        request={"application/json": STAFF_CREATE_REQUEST},
        responses=_responses(201, STAFF_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, svc.STAFF_KEYS, required=["staff_code", "display_name"])
        fields = svc.parse_staff_fields(body)
        # E062: the pair is all or nothing. Half a placement is a malformed body,
        # not a business refusal - there is no assignment to conflict with yet.
        if (fields.site_id is None) != (fields.effective_from is None):
            raise svc.invalid(
                "site_id and effective_from are given together, or neither is given.",
                "site_id" if fields.site_id is None else "effective_from",
            )
        # A head-office joiner has no site to be authorised at, so the actor needs
        # staff authority that is not tied to one - C-OWN's tenant grant (GSA-T03).
        # A site manager's site-scoped grant cannot reach an unplaced person, and
        # saying so as NOT_FOUND would name a record that does not exist.
        site = svc.resolve_site(fields.site_id) if fields.site_id is not None else None
        if site is not None:
            access.require_at_store("staff.manage", site.pk)
        else:
            access.require_action("staff.manage")
            if not access.can_at_store("staff.manage", None):
                raise svc.staff_invalid(
                    "Creating a person with no site needs tenant-wide staff authority; "
                    "name the site they work at instead.",
                    "site_id",
                )
        if fields.mobile is not None:
            svc.require_personal(access, site.pk if site is not None else None)

        def handler(run: CommandRun) -> CommandResult:
            staff = svc.create_staff(run, fields=fields)
            return CommandResult(
                resource_type="staff", resource_id=str(staff.pk), status_code=201, revision=1
            )

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_STAFF_CREATE,
            meta=meta,
            business_input=fields.as_json(),
            handler=handler,
            site_id=site.pk if site is not None else None,
            subject_key=f"staff-code:{fields.staff_code}",
        )
        staff, placement = svc.get_staff(access, uuid.UUID(str(result.resource_id)))
        return Response(svc.staff_dto(access, staff, placement), status=result.status_code)


class GoodsStaffDetailView(GoodsAPIView):
    """E063 reads one staff member; E064 corrects their details or moves them."""

    @extend_schema(
        operation_id="goods_v1_auth_admin_staff_detail",
        responses=_responses(200, STAFF_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, DETAIL_QUERY)
        staff, placement = svc.get_staff(access, pk)
        return Response(svc.staff_dto(access, staff, placement))

    @extend_schema(
        request={"application/json": STAFF_UPDATE_REQUEST},
        responses=_responses(200, STAFF_RESOURCE, _WRITE_REFUSALS),
    )
    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, svc.STAFF_KEYS)
        if not body:
            raise _nothing_to_change()
        fields = svc.parse_staff_fields(body)
        _staff, placement = svc.get_staff(access, pk)
        access.require_at_store("staff.manage", placement.site_id)
        if fields.site_id is not None and fields.site_id != placement.site_id:
            access.require_at_store("staff.manage", svc.resolve_site(fields.site_id).pk)
        if "mobile" in fields.present:
            svc.require_personal(access, placement.site_id)

        def handler(run: CommandRun) -> CommandResult:
            updated = svc.update_staff(
                run, staff_id=pk, expected_revision=meta.expected_revision, fields=fields
            )
            return CommandResult(
                resource_type="staff", resource_id=str(pk), revision=updated.revision
            )

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_STAFF_UPDATE,
            meta=meta,
            business_input=fields.as_json(),
            handler=handler,
            resource_ids=[str(pk)],
            site_id=placement.site_id,
            subject_key=f"staff:{pk}",
        )
        staff, placement = svc.get_staff(access, pk)
        return Response(svc.staff_dto(access, staff, placement), status=result.status_code)


class GoodsStaffRetireView(GoodsAPIView):
    """E065: retire a staff member once their open responsibilities are handed over."""

    @extend_schema(
        request={"application/json": STAFF_RETIRE_REQUEST},
        responses=_responses(200, STAFF_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, {"reason_code", "effective_at"}, required=["reason_code", "effective_at"]
        )
        reason = svc.text_field(body, "reason_code", 60, required=True)
        effective_at = svc.timestamp_field(body, "effective_at", required=True)
        assert reason is not None and effective_at is not None
        _staff, placement = svc.get_staff(access, pk)
        access.require_at_store("staff.retire", placement.site_id)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            retired = svc.retire_staff(
                run,
                access=access,
                staff_id=pk,
                expected_revision=meta.expected_revision,
                effective_at=effective_at,
                reason_code=reason,
            )
            return CommandResult(
                resource_type="staff", resource_id=str(pk), revision=retired.revision
            )

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_STAFF_RETIRE,
            meta=meta,
            business_input={"reason_code": reason, "effective_at": svc.iso(effective_at)},
            handler=handler,
            resource_ids=[str(pk)],
            site_id=placement.site_id,
            subject_key=f"staff:{pk}",
        )
        staff, placement = svc.get_staff(access, pk)
        return Response(svc.staff_dto(access, staff, placement), status=result.status_code)


class GoodsStaffAssignView(GoodsAPIView):
    """E070: move a staff member to a new primary site from a later start."""

    @extend_schema(
        request={"application/json": STAFF_ASSIGN_REQUEST},
        responses=_responses(200, ASSIGNMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"site_id", "effective_from", "reason_code"},
            required=["site_id", "effective_from", "reason_code"],
        )
        site_id = parse_int_id(body["site_id"], "site_id")
        effective_from = svc.timestamp_field(body, "effective_from", required=True)
        reason = svc.text_field(body, "reason_code", 60, required=True)
        assert effective_from is not None and reason is not None
        _staff, placement = svc.get_staff(access, pk)
        access.require_at_store("staff.manage", placement.site_id)
        site = svc.resolve_site(site_id)
        access.require_at_store("staff.manage", site.pk)

        def handler(run: CommandRun) -> CommandResult:
            updated, assignment = svc.assign_staff(
                run,
                staff_id=pk,
                expected_revision=meta.expected_revision,
                site_id=site.pk,
                effective_from=effective_from,
                reason_code=reason,
            )
            return CommandResult(
                resource_type="staff_assignment",
                resource_id=str(assignment.pk),
                revision=updated.revision,
            )

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_STAFF_ASSIGN,
            meta=meta,
            business_input={
                "site_id": str(site.pk),
                "effective_from": svc.iso(effective_from),
                "reason_code": reason,
            },
            handler=handler,
            resource_ids=[str(pk)],
            site_id=site.pk,
            subject_key=f"staff:{pk}",
        )
        staff, _placement = svc.get_staff(access, pk)
        return Response(
            svc.assignment_dto(staff, uuid.UUID(str(result.resource_id))),
            status=result.status_code,
        )


# -- logins and grants (E071-E074, E081) ---------------------------------------------------


class GoodsUserListCreateView(GoodsAPIView):
    """E071 lists goods logins; E073 creates a login for an existing person (step-up)."""

    @extend_schema(responses=_responses(200, USER_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        window, cursor = paginate(svc.list_users(access, params), params)
        return Response(page([svc.user_dto(access, user) for user in window], cursor))

    @extend_schema(
        request={"application/json": USER_CREATE_REQUEST},
        responses=_responses(201, USER_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, svc.USER_KEYS, required=["human_id", "email", "display_name"]
        )
        login = svc.parse_login(body)
        access.require("access.manage")
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            user = svc.create_user(run, login=login)
            return CommandResult(
                resource_type="user", resource_id=str(user.pk), status_code=201, revision=1
            )

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_USER_CREATE,
            meta=meta,
            business_input=login.fingerprint_input(),
            handler=handler,
            subject_key=f"human:{login.human_id}",
        )
        user = svc.goods_user(access.tenant_id, int(str(result.resource_id)))
        return Response(svc.user_dto(access, user), status=result.status_code)


class GoodsUserDetailView(GoodsAPIView):
    """E072 reads one login; E074 changes it after tenant-wide step-up."""

    @extend_schema(
        operation_id="goods_v1_auth_admin_users_detail",
        responses=_responses(200, USER_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        check_query(request, DETAIL_QUERY)
        access.require("access.manage")
        user = svc.goods_user(access.tenant_id, pk)
        return Response(svc.user_dto(access, user, with_pin_state=True))

    @extend_schema(
        request={"application/json": USER_UPDATE_REQUEST},
        responses=_responses(200, USER_RESOURCE, _WRITE_REFUSALS),
    )
    def patch(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, svc.USER_KEYS)
        if not body:
            raise _nothing_to_change()
        login = svc.parse_login(body)
        access.require("access.manage")
        svc.goods_user(access.tenant_id, pk)
        svc.require_login_in_scope(access, pk)
        access.require_step_up()
        actor_pk = int(cast(User, request.user).pk)

        def handler(run: CommandRun) -> CommandResult:
            svc.update_user(
                run,
                user_pk=pk,
                expected_revision=meta.expected_revision,
                login=login,
                actor_user_pk=actor_pk,
            )
            return CommandResult(resource_type="user", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_USER_UPDATE,
            meta=meta,
            business_input=login.fingerprint_input(),
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"user:{pk}",
        )
        user = svc.goods_user(access.tenant_id, pk)
        return Response(svc.user_dto(access, user, with_pin_state=True), status=result.status_code)


TILL_PIN_RESET_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "Counter PIN reset (store operations ticket 06): command meta only.",
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string"},
    },
    "required": ["command_id", "contract_version"],
}


def _require_pin_admin(access: AccessContext, request: Request, pk: int, *, verb: str) -> User:
    """The gate Admin's counter-PIN set and reset share, in order: ``access.manage``,
    Admin only (``may_reset_till_pin``), the login exists, and it is in the
    caller's scope. Returns the target login. Each view asks for the fresh
    password (``access.require_step_up()``) itself, after its own checks."""
    access.require("access.manage")
    if not may_reset_till_pin(request.user):
        raise Refusal("ACTION_DENIED", f"Only Admin can {verb} a counter PIN.")
    target = svc.goods_user(access.tenant_id, pk)
    svc.require_login_in_scope(access, pk)
    return target


class GoodsUserTillPinResetView(GoodsAPIView):
    """Admin clears a manager's counter PIN (store operations ticket 06).

    The same checks as changing a login (E074: ``access.manage``, the login in the
    caller's scope, a fresh password confirmation), narrowed to Admin
    (``accounts.role_lists.TILL_PIN_RESETTERS``, baseline B6). Every till drops
    the old PIN on its next sync. The command's audit record says "set" /
    "not set", never the PIN.
    """

    @extend_schema(
        operation_id="goods_v1_auth_admin_users_till_pin_reset",
        request={"application/json": TILL_PIN_RESET_REQUEST},
        responses=_responses(200, USER_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        business_body(request.data, set())
        _require_pin_admin(access, request, pk, verb="reset")
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            write_pin(run, pk, "", by="admin")
            return CommandResult(resource_type="user", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action=RESET_ACTION,
            meta=meta,
            business_input={"user": pk},
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"user:{pk}",
        )
        user = svc.goods_user(access.tenant_id, pk)
        return Response(svc.user_dto(access, user, with_pin_state=True), status=result.status_code)


TILL_PIN_SET_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "Admin sets a manager's counter PIN (store operations ticket 06, B76).",
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string"},
        "pin": {"type": "string", "description": "4 to 6 digits. Stored only as a hash."},
    },
    "required": ["command_id", "contract_version", "pin"],
}


class GoodsUserTillPinSetView(GoodsAPIView):
    """Admin sets a manager's counter PIN, which works at once (ticket 06, B76).

    The reset's checks (``access.manage``, Admin only, the login in scope, a
    fresh password), plus: the login must be somebody a till could hold a PIN
    for (``may_hold_till_pin``), and the PIN must pass the counter's rules. Only
    its hash is stored; the tills learn it on their next sync. Neither the PIN
    nor its hash is echoed, logged or put in the command's input or audit
    record ("set" / "not set" only).
    """

    @extend_schema(
        operation_id="goods_v1_auth_admin_users_till_pin_set",
        request={"application/json": TILL_PIN_SET_REQUEST},
        responses=_responses(200, USER_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"pin"}, required=["pin"])
        target = _require_pin_admin(access, request, pk, verb="set")
        pin = body["pin"]
        problem = pin_problem(pin) if isinstance(pin, str) else "A counter PIN is digits only."
        if problem:
            raise svc.invalid(problem, "pin")
        if not may_hold_till_pin(target):
            raise svc.invalid(
                "This login cannot hold a counter PIN: only a store manager who may "
                "approve counter exceptions can.",
                "pin",
            )
        access.require_step_up()
        new_hash = hash_till_pin(pin)
        # A replay under the same command_id must carry the same PIN, or it is a
        # conflict rather than a stale "set". The fingerprint gets a keyed digest
        # bound to this command, never the PIN or a plain hash of it (a 4-6 digit
        # PIN falls to a plain hash at once).
        pin_digest = salted_hmac(
            f"accounts.till_pin.admin_set:{meta.command_id}", pin, algorithm="sha256"
        ).hexdigest()

        def handler(run: CommandRun) -> CommandResult:
            write_pin(run, pk, new_hash, by="admin")
            return CommandResult(resource_type="user", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action=ADMIN_SET_ACTION,
            meta=meta,
            business_input={"user": pk, "pin_digest": pin_digest},
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"user:{pk}",
        )
        user = svc.goods_user(access.tenant_id, pk)
        return Response(svc.user_dto(access, user, with_pin_state=True), status=result.status_code)


class GoodsUserGrantsView(GoodsAPIView):
    """E081: add or revoke one person's role grants, one row per scope (step-up)."""

    @extend_schema(
        request={
            "application/json": {
            "type": "object",
            "required": [
                "command_id",
                "contract_version",
                "expected_revision",
                "grants",
                "reason_code",
            ],
            "additionalProperties": False,
            "properties": {
                "command_id": {"type": "string", "format": "uuid"},
                "contract_version": {"type": "string", "enum": ["goods-v1"]},
                "expected_revision": {"type": "integer", "minimum": 1},
                "reason_code": {"type": "string", "maxLength": 60},
                "grants": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "required": ["revokes"],
                                "additionalProperties": False,
                                "properties": {
                                    "revokes": {"type": "string", "format": "uuid"},
                                    "effective_from": {"type": "string", "format": "date-time"},
                                    "human_id": {"type": "string", "format": "uuid"},
                                },
                            },
                            {
                                "type": "object",
                                "required": ["scope", "actions", "fields", "effective_from"],
                                "additionalProperties": False,
                                "properties": {
                                    "human_id": {"type": "string", "format": "uuid"},
                                    "role_id": {"type": "integer", "minimum": 1},
                                    "scope": {
                                        "type": "object",
                                        "required": ["scope_kind"],
                                        "properties": {
                                            "scope_kind": {"type": "string"},
                                            "entity_id": {"type": "integer"},
                                            "site_id": {"type": "integer"},
                                            "sbu_id": {"type": "integer"},
                                            "brand_id": {"type": "integer"},
                                        },
                                    },
                                    "actions": {
                                        "type": "object",
                                        "required": ["actions"],
                                        "properties": {
                                            "actions": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "scope_kind": {"type": "string"},
                                        },
                                    },
                                    "fields": {"type": "array", "items": {"type": "string"}},
                                    "effective_from": {
                                        "type": "string",
                                        "format": "date-time",
                                    },
                                    "effective_to": {"type": "string", "format": "date-time"},
                                },
                            },
                        ]
                    },
                },
            },
            },
        },
        responses=_responses(200, USER_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, {"grants", "reason_code"}, required=["grants", "reason_code"]
        )
        reason = svc.text_field(body, "reason_code", 60, required=True)
        assert reason is not None
        items = svc.parse_grant_items(body["grants"])
        access.require_action("access.manage")
        user = svc.goods_user(access.tenant_id, pk)
        assert user.human_id is not None
        for item in items:
            svc.check_item_scope(access, item, user.human_id)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            svc.change_grants(
                run,
                user_pk=pk,
                expected_revision=meta.expected_revision,
                items=items,
                reason_code=reason,
            )
            return CommandResult(resource_type="user", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_GRANT_CHANGE,
            meta=meta,
            business_input={"grants": [item.as_json() for item in items], "reason_code": reason},
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"user:{pk}",
        )
        user = svc.goods_user(access.tenant_id, pk)
        return Response(svc.user_dto(access, user, with_pin_state=True), status=result.status_code)


# -- roles and the access matrix (E075-E080) -----------------------------------------------


def _visible_role(access: AccessContext, pk: int) -> Role:
    role = svc.get_role(pk)
    if role.tenant_id != access.tenant_id or (
        role.is_system and role.code not in INITIAL_ROLE_CODES
    ):
        raise Refusal("NOT_FOUND", "That role was not found.")
    return role


class GoodsRoleListCreateView(GoodsAPIView):
    """E075 lists roles; E077 creates one (step-up)."""

    @extend_schema(responses=_responses(200, ROLE_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        window, cursor = paginate(svc.list_roles(access, params), params)
        return Response(page([svc.role_dto(access, role) for role in window], cursor))

    @extend_schema(
        request={"application/json": ROLE_CREATE_REQUEST},
        responses=_responses(201, ROLE_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, svc.ROLE_KEYS, required=["code", "name"])
        fields = svc.parse_role(body)
        access.require_action("access.manage")
        svc.require_tenant_manage(access)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            role = svc.create_role(run, fields=fields)
            return CommandResult(resource_type="role", resource_id=str(role.pk), status_code=201)

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_ROLE_CREATE,
            meta=meta,
            business_input=fields,
            handler=handler,
            subject_key=f"role:{fields['code']}",
        )
        role = svc.get_role(int(str(result.resource_id)))
        return Response(svc.role_dto(access, role), status=result.status_code)


class GoodsRoleDetailView(GoodsAPIView):
    """E076 reads a role; E078 changes its name, description or active flag (step-up)."""

    @extend_schema(
        operation_id="goods_v1_auth_admin_roles_detail",
        responses=_responses(200, ROLE_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        check_query(request, DETAIL_QUERY)
        access.require("access.manage")
        return Response(svc.role_dto(access, _visible_role(access, pk)))

    @extend_schema(
        request={"application/json": ROLE_UPDATE_REQUEST},
        responses=_responses(200, ROLE_RESOURCE, _WRITE_REFUSALS),
    )
    def patch(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, svc.ROLE_KEYS)
        if not body:
            raise _nothing_to_change()
        fields = svc.parse_role(body)
        access.require_action("access.manage")
        svc.require_tenant_manage(access)
        _visible_role(access, pk)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            svc.update_role(
                run, role_pk=pk, expected_revision=meta.expected_revision, fields=fields
            )
            return CommandResult(resource_type="role", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_ROLE_UPDATE,
            meta=meta,
            business_input=fields,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"role:{pk}",
        )
        return Response(svc.role_dto(access, _visible_role(access, pk)), status=result.status_code)


class GoodsRoleAccessView(GoodsAPIView):
    """E080: set a role's goods action and field maximum, never beyond its template (step-up)."""

    @extend_schema(
        request={"application/json": ROLE_ACCESS_REQUEST},
        responses=_responses(200, ROLE_RESOURCE, _WRITE_REFUSALS),
    )
    def put(self, request: Request, code: str) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"grants"}, required=["grants"])
        if len(code) > 40:
            raise Refusal("INVALID_REQUEST", "A role code is at most 40 characters.")
        items = svc.parse_grant_items(body["grants"], role_level=True)
        access.require_action("access.manage")
        svc.require_tenant_manage(access)
        role = Role.objects.filter(code=code).first()
        if role is None:
            raise svc.not_found("role")
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            changed = svc.set_role_access(
                run, code=code, expected_revision=meta.expected_revision, items=items
            )
            return CommandResult(resource_type="role", resource_id=str(changed.pk))

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_ROLE_ACCESS,
            meta=meta,
            business_input={"grants": [item.as_json() for item in items]},
            handler=handler,
            resource_ids=[code],
            subject_key=f"role:{code}",
        )
        return Response(svc.role_dto(access, svc.get_role(role.pk)), status=result.status_code)


class GoodsAccessMatrixView(GoodsAPIView):
    """E079: effective roles, grants, action descriptions and fixed restrictions."""

    @extend_schema(responses=_responses(200, ACCESS_MATRIX_RESPONSE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"site_id", "role_id"})
        return Response(svc.access_matrix(access, params))


# -- policy lists (E082, E083) -------------------------------------------------------------


class GoodsActorPolicyListView(GoodsAPIView):
    """E082: effective approved actor policies and open policy drafts."""

    @extend_schema(responses=_responses(200, POLICY_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        items = svc.approval_policies(access, params, sort_key="action")
        window, cursor = paginate(items, params)
        return Response(page(window, cursor))


class GoodsApprovalPolicyListView(GoodsAPIView):
    """E083: effective approved approval policies and open policy drafts."""

    @extend_schema(responses=_responses(200, POLICY_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        items = svc.approval_policies(access, params, sort_key="purpose")
        window, cursor = paginate(items, params)
        return Response(page(window, cursor))


# -- privileged changes (E084, E237) and setup choices (E214) ------------------------------


class GoodsPrivilegedChangeListView(GoodsAPIView):
    """E084: login, grant, role, export and restore changes with their acknowledgements."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "q", str, description="Matches the action or subject, 100 characters at most."
            ),
            OpenApiParameter("site_id", int, description="Only changes recorded at this site."),
            OpenApiParameter(
                "id",
                str,
                description="Only this change, by id; the owned follow-up links here (03D).",
            ),
            OpenApiParameter(
                "cursor",
                str,
                description="Opaque next-page cursor, newest first by a stable keyset.",
            ),
            OpenApiParameter("limit", int, description="Changes per page, from 1 through 100."),
        ],
        responses=_responses(200, PRIVILEGED_PAGE, _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, svc.PRIVILEGED_QUERY_KEYS)
        events, cursor = svc.privileged_page(access, params)
        return Response(page(svc.privileged_dtos(access, events), cursor))


class GoodsPrivilegedChangeReviewView(GoodsAPIView):
    """E237: acknowledge someone else's privileged change; never an approval or a reversal."""

    @extend_schema(
        request={
            "application/json": {
            "type": "object",
            "required": ["command_id", "contract_version", "note"],
            "additionalProperties": False,
            "properties": {
                "command_id": {"type": "string", "format": "uuid"},
                "contract_version": {"type": "string", "enum": ["goods-v1"]},
                "expected_revision": {"type": "integer", "minimum": 1},
                "note": {"type": "string", "maxLength": 1000},
            },
            },
        },
        responses=_responses(200, PRIVILEGED_ITEM, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"note"}, required=["note"])
        note = svc.text_field(body, "note", 1000, required=True)
        assert note is not None
        access.require_action("access.review")
        svc.get_privileged_event(access, pk)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            svc.review_change(run, event_id=pk, note=note)
            return CommandResult(resource_type="privileged_change", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action=svc.ACTION_REVIEW,
            meta=meta,
            business_input={"note": note},
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"audit:{pk}",
        )
        event = svc.get_privileged_event(access, pk)
        return Response(svc.privileged_dtos(access, [event])[0], status=result.status_code)


class GoodsAdminMetaView(GoodsAPIView):
    """E214: the setup choices the goods admin screens offer, inside the caller's scope."""

    @extend_schema(responses=_responses(200, ADMIN_META_RESPONSE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, DETAIL_QUERY)
        return Response(svc.admin_meta(access))
