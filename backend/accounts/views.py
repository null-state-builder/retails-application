"""Auth endpoints: server-side sessions for the whole application (goods-v1 design §4.2).

`/api/auth/login` takes an email and password, creates an opaque server session in
an HttpOnly cookie plus a session-bound CSRF token, and answers `SessionDTO` with
the legacy shell profile alongside so the PWA can resolve the shell before any
other call. Ten failures in 15 minutes lock an identifier for 15 minutes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.access_changes import is_access_administrator, propose_access_change
from accounts.authentication import enforce_write_protection
from accounts.change_password import change_own_password
from accounts.floors import RULES, cell_limits, describe_floors, floor_violations
from accounts.goods_admin_views import REFUSAL_RESPONSE
from accounts.matrix import diff, invalid_cells, replacement_row, stored_row
from accounts.models import (
    NAV_GROUPS,
    AccessChange,
    ActorPolicy,
    Role,
    ScopeType,
    User,
)
from accounts.permissions import require_section
from accounts.sections import (
    CAP_MANAGE,
    CAP_OPERATE,
    CAPABILITY_ORDER,
    CAPABILITY_WORDS,
    SECTIONS,
)
from accounts.serializers import (
    ActorPolicySerializer,
    AdminRoleSerializer,
    AdminUserSerializer,
    ApprovalPolicyAdminSerializer,
)
from accounts.session_payload import session_payload
from accounts.sessions import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    authenticate_email,
    cookie_kwargs,
    csrf_matches,
    grant_step_up,
    issue_session,
    new_token,
    resolve_session,
    revoke_session,
    rotate_session,
)
from accounts.till_pin import may_set_personal_till_pin, pin_problem, set_own_pin
from approvals.models import ApprovalPolicy
from core.refusals import Refusal
from core.tenancy import current_tenant_id
from core.textsearch import search_term, text_filter
from masters.models import Brand, Store

# Managing users and roles *is* the Setup section. Gate the admin APIs on the
# section capability (config-driven) rather than a hardcoded role list: only
# Owner and Admin hold `setup: manage` in the RBAC matrix, so this is
# behaviour-identical to the old role check but now retunable as data (#85).
IsRbacAdmin = require_section("setup", CAP_MANAGE)


class IsAccessAdministrator(BasePermission):
    """Floor rule: editable Setup grants cannot grant permission to edit Setup."""

    message = "Only Owner or IT Admin may propose user, role, or permission changes."

    def has_permission(self, request: Request, view: Any) -> bool:
        return is_access_administrator(request.user)


def _set_session_cookies(response: Response, token: str, csrf_token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=12 * 3600, **cookie_kwargs(httponly=True))
    response.set_cookie(CSRF_COOKIE, csrf_token, max_age=12 * 3600, **cookie_kwargs(httponly=False))


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="Lax")
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="Lax")


def _bound_tenant() -> Any:
    tenant_id = current_tenant_id()
    if tenant_id is None:
        raise Refusal("SERVICE_UNAVAILABLE", "This deployment has not been set up yet.", status=503)
    return tenant_id


class CsrfView(APIView):
    """Mint the double-submit CSRF token a signed-out page needs before login."""

    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: {
            "type": "object",
            "required": ["csrf_token"],
            "properties": {"csrf_token": {"type": "string"}},
        }}
    )
    def get(self, request: Request) -> Response:
        token = request.COOKIES.get(CSRF_COOKIE) or new_token()
        response = Response({"csrf_token": token})
        response.set_cookie(CSRF_COOKIE, token, max_age=12 * 3600, **cookie_kwargs(httponly=False))
        return response


#: ``SessionDTO`` (design §6.1) as E001/E002/E003 answer it (``accounts.session_payload``).
SESSION_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "One server-derived access session. Display hints are never authority.",
    "required": [
        "contract_version",
        "policy_version",
        "user",
        "assignments",
        "navigation",
        "sections",
        "capabilities",
        "display_actions",
        "context_choices",
        "sites",
        "expires_at",
        "step_up_valid_until",
        "store_features",
    ],
    "properties": {
        "contract_version": {"type": "string", "enum": ["access-v2"]},
        "policy_version": {"type": "string"},
        "user": {
            "type": "object",
            "required": ["id", "human_id", "display_name", "email", "must_change_password"],
            "properties": {
                "id": {"type": "string"},
                "human_id": {"type": "string", "nullable": True},
                "display_name": {"type": "string"},
                "email": {"type": "string", "nullable": True},
                "has_till_pin": {"type": "boolean"},
                "may_set_till_pin": {"type": "boolean", "description": "May set this person's own credential; does not grant approval authority."},
                "may_hold_till_pin": {"type": "boolean", "description": "Separately eligible to decide counter exceptions under current scoped approval policy."},
                "must_change_password": {
                    "type": "boolean",
                    "description": (
                        "GSA-T03/ticket 03A: true while this login holds an "
                        "administrator-issued temporary password. Until E239 "
                        "replaces it, every route but this session's own "
                        "lifecycle and E239 answers 403 PASSWORD_CHANGE_REQUIRED."
                    ),
                },
            },
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "role_code", "all_sites", "site_ids", "all_brands", "brand_ids", "effective_from"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "role_code": {"type": "string"},
                    "all_sites": {"type": "boolean"},
                    "site_ids": {"type": "array", "items": {"type": "integer"}},
                    "all_brands": {"type": "boolean"},
                    "brand_ids": {"type": "array", "items": {"type": "integer"}},
                    "effective_from": {"type": "string", "format": "date-time"},
                    "effective_to": {"type": "string", "format": "date-time", "nullable": True},
                },
            },
        },
        "navigation": {"type": "array", "items": {"type": "string"}},
        "sections": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "capabilities": {"type": "object", "additionalProperties": {"type": "string"}},
        "display_actions": {"type": "array", "items": {"type": "string"}},
        "context_choices": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["units", "brands"]},
                "all_units": {"type": "boolean"},
                "sites": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "brands": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            },
        },
        "sites": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "code", "name", "type", "stock_contract"],
                "properties": {
                    "id": {"type": "string"},
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "stock_contract": {
                        "type": "string",
                        "enum": ["legacy", "goods_v1"],
                        "description": "The stock system this site runs on.",
                    },
                },
            },
        },
        "expires_at": {"type": "string", "format": "date-time"},
        "step_up_valid_until": {"type": "string", "format": "date-time", "nullable": True},
        "store_features": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
            "description": (
                "Store operations feature switches (ST-OPS-6): each feature that is "
                "on at one or more of the stores this person may act at, with the "
                "ids of those stores. A feature absent here is off everywhere this "
                "person works, and its menus hide."
            ),
        },
        "csrf_token": {
            "type": "string",
            "description": "Sent by E001 and E003 only, which issue or rotate the session.",
        },
    },
}


class LoginView(APIView):
    """E001: email + password → server session."""

    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]

    @extend_schema(
        request={"application/json": {
            "type": "object",
            "required": ["email", "password"],
            "properties": {
                "email": {"type": "string", "maxLength": 254},
                "password": {"type": "string", "maxLength": 1024},
                "csrf_token": {"type": "string"},
            },
        }},
        responses={
            200: SESSION_RESPONSE,
            **{code: REFUSAL_RESPONSE for code in (400, 401, 403, 429)},
        }
    )
    def post(self, request: Request) -> Response:
        enforce_origin_only(request)
        data = request.data if isinstance(request.data, dict) else {}
        unknown = set(data) - {"email", "password", "csrf_token"}
        email = data.get("email")
        password = data.get("password")
        if unknown or not isinstance(email, str) or not isinstance(password, str):
            raise Refusal("INVALID_REQUEST", "Send an email and a password.")
        if len(email) > 254 or len(password) > 1024 or not email.strip():
            raise Refusal("INVALID_REQUEST", "Send an email and a password.")
        submitted = data.get("csrf_token") or request.META.get("HTTP_X_CSRF_TOKEN")
        if not csrf_matches(submitted, request.COOKIES.get(CSRF_COOKIE)):
            raise Refusal(
                "CSRF_FAILED",
                "The page's security token is missing or stale. Reload and try again.",
            )
        tenant_id = _bound_tenant()
        user = authenticate_email(tenant_id, email, password)
        issued = issue_session(user)
        response = Response(session_payload(user, issued.session, csrf_token=issued.csrf_token))
        _set_session_cookies(response, issued.token, issued.csrf_token)
        return response


def enforce_origin_only(request: Request) -> None:
    from accounts.authentication import origin_allowed

    if not origin_allowed(request._request):
        raise Refusal("CSRF_FAILED", "This request did not come from an allowed page.")


class MeView(APIView):
    """E002: the current person's session, roles, sites, actions and field grants."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: SESSION_RESPONSE, 401: REFUSAL_RESPONSE})
    def get(self, request: Request) -> Response:
        # `IsAuthenticated` above guarantees a real `User` with a live session.
        return Response(session_payload(cast(User, request.user), request.auth))


class TillPinView(APIView):
    """Set only your own personal counter credential after password confirmation.

    One selected-store/all-brand Store Person operating assignment is required.
    Setting a PIN grants no exception authority: decision consumers retain the
    separate ``may_hold_till_pin`` approval eligibility and live decision gates.
    Administrator-assisted PIN changes retain their separately governed route.
    """

    permission_classes = [IsAuthenticated, require_section("sell", CAP_OPERATE)]

    @extend_schema(
        request={"application/json": {
            "type": "object",
            "required": ["pin", "current_password"],
            "properties": {
                "pin": {"type": "string"},
                "current_password": {"type": "string"},
            },
        }},
        responses={
            200: {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string", "enum": ["set"]}},
            },
            400: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
        },
    )
    def put(self, request: Request) -> Response:
        # `IsAuthenticated` above guarantees a real `User`, not the
        # `AnonymousUser` half of DRF's `request.user` union (same pattern as
        # `alerts/views.py`).
        user = cast(User, request.user)
        if not may_set_personal_till_pin(user):
            return _refuse(
                "A personal counter PIN requires a selected-store Store Person "
                "operating assignment covering that store's brands.",
                "NOT_A_TILL_MANAGER",
                status.HTTP_403_FORBIDDEN,
            )
        if not isinstance(request.data, dict) or set(request.data) - {"pin", "current_password"}:
            raise Refusal("INVALID_REQUEST", "Send only your personal PIN and current password.")
        pin = str(request.data.get("pin") or "")
        problem = pin_problem(pin)
        if problem:
            return _refuse(problem, "VALIDATION", status.HTTP_400_BAD_REQUEST)
        # The password check comes second so a bad PIN is answered as a bad PIN.
        # It comes at all because a counter is a shared machine: a screen left
        # signed in is otherwise a way to give yourself somebody else's override.
        password = request.data.get("current_password")
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise Refusal("INVALID_REQUEST", "Send your current password.")
        try:
            grant_step_up(request.auth, password)
        except Refusal as refusal:
            if refusal.code != "INVALID_CREDENTIALS":
                raise
            return _refuse(
                "That is not your password, so the PIN was not changed.",
                "PASSWORD_WRONG",
                status.HTTP_403_FORBIDDEN,
            )
        # One command, so the change is in the audit log - without the PIN.
        set_own_pin(user, pin, request.auth)
        # The till learns about it on its next sync, like every other fact about
        # this store - there is no push, and there does not need to be.
        return Response({"status": "set"})


def _refuse(message: str, code: str, http_status: int) -> Response:
    """The `{"error", "code"}` body every endpoint written for the till uses."""
    return Response({"error": message, "code": code}, status=http_status)


class LogoutView(APIView):
    """E004: revoke whatever session this browser holds; always answers logged_out."""

    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]

    @extend_schema(
        request=None,
        responses={200: {
            "type": "object",
            "required": ["logged_out"],
            "properties": {"logged_out": {"type": "boolean", "enum": [True]}},
        }},
    )
    def post(self, request: Request) -> Response:
        session = resolve_session(request.COOKIES.get(SESSION_COOKIE))
        enforce_write_protection(request._request, session)
        if session is not None:
            revoke_session(session)
        response = Response({"logged_out": True})
        _clear_session_cookies(response)
        return response


class CookieRefreshView(APIView):
    """E003: rotate the session token; the 12-hour absolute life never moves."""

    authentication_classes: list[Any] = []
    permission_classes = [AllowAny]

    @extend_schema(
        request=None,
        responses={200: SESSION_RESPONSE, 401: REFUSAL_RESPONSE, 403: REFUSAL_RESPONSE},
    )
    def post(self, request: Request) -> Response:
        session = resolve_session(request.COOKIES.get(SESSION_COOKIE), raise_expired=True)
        if session is None:
            raise Refusal("AUTH_REQUIRED", "Sign in to continue.")
        enforce_write_protection(request._request, session)
        issued = rotate_session(session)
        response = Response(
            session_payload(session.user, issued.session, csrf_token=issued.csrf_token)
        )
        _set_session_cookies(response, issued.token, issued.csrf_token)
        return response


class StepUpView(APIView):
    """E005: re-enter the password for five minutes of privileged actions on this session."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request={"application/json": {
            "type": "object",
            "required": ["password"],
            "properties": {"password": {"type": "string", "maxLength": 1024}},
        }},
        responses={
            200: {
                "type": "object",
                "required": ["valid_until"],
                "properties": {"valid_until": {"type": "string", "format": "date-time"}},
            },
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            429: REFUSAL_RESPONSE,
        },
    )
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        password = data.get("password")
        if set(data) - {"password"} or not isinstance(password, str) or len(password) > 1024:
            raise Refusal("INVALID_REQUEST", "Send your password.")
        valid_until = grant_step_up(request.auth, password)
        return Response({"valid_until": valid_until.isoformat()})


CHANGE_PASSWORD_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": "ChangePasswordRequest (E239).",
    "required": ["current_password", "new_password"],
    "properties": {
        "current_password": {"type": "string", "maxLength": 1024},
        "new_password": {"type": "string", "maxLength": 1024},
    },
}

CHANGE_PASSWORD_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": (
        "ChangePasswordResponse (E239, design §6.2). Succeeding ends every "
        "session this login holds, this one included; sign in again to continue."
    ),
    "properties": {
        "changed": {"type": "boolean", "enum": [True]},
        "reauthentication_required": {"type": "boolean", "enum": [True]},
    },
}


class ChangePasswordView(APIView):
    """E239: replace your own password. Ends every session, including this one.

    Verifies the current password (locked out exactly like a wrong login or
    step-up attempt), validates the new one against the same password policy
    the admin surface enforces, then bumps the person's security epoch so every
    session - this one included - fails its next check and reauthentication is
    required (GSA-T03, `accounts.sessions.bump_security_epoch`). A wrong current
    password answers AUTH_REQUIRED; a policy-rejected or malformed new one
    answers INVALID_REQUEST; lockout answers LOGIN_LOCKED (design §6.2's own,
    simpler shape for this endpoint - not the admin surface's ACCESS_INVALID).
    Every refusal leaves every session exactly as it was.

    The one write a temporary-password session may reach besides its own session
    lifecycle (`accounts.authentication.enforce_password_change_restriction`) -
    reused for an ordinary forgotten-password change too, since GSA-T03 gives
    both the same contract: replace it, then sign in again.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request={"application/json": CHANGE_PASSWORD_REQUEST},
        responses={
            200: CHANGE_PASSWORD_RESPONSE,
            **{code: REFUSAL_RESPONSE for code in (400, 401, 403, 429)},
        },
    )
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        current_password = data.get("current_password")
        new_password = data.get("new_password")
        if (
            set(data) - {"current_password", "new_password"}
            or not isinstance(current_password, str)
            or not isinstance(new_password, str)
            or not current_password
            or len(current_password) > 1024
            or not 1 <= len(new_password) <= 1024
        ):
            raise Refusal("INVALID_REQUEST", "Send your current password and a new password.")
        user = cast(User, request.user)
        change_own_password(
            user=user,
            session=request.auth,
            current_password=current_password,
            new_password=new_password,
        )
        response = Response({"changed": True, "reauthentication_required": True})
        _clear_session_cookies(response)
        return response


_SECTION_CHOICE = {
    "type": "object",
    "required": ["code", "label"],
    "properties": {"code": {"type": "string"}, "label": {"type": "string"}},
}
_SECTION_ACCESS = {
    "type": "object",
    "description": "Section code to its stored or proposed access cell.",
    "additionalProperties": {
        "type": "object",
        "required": ["capability"],
        "properties": {
            "capability": {"type": "string", "enum": list(CAPABILITY_ORDER)},
            "label": {"type": "string", "maxLength": 120},
        },
    },
}
_ADMIN_META_RESPONSE = {
    "type": "object",
    "required": ["nav_groups", "sections", "capabilities", "scope_types", "stores", "brands"],
    "properties": {
        "nav_groups": {"type": "array", "items": {"type": "string"}},
        "sections": {"type": "array", "items": _SECTION_CHOICE},
        "capabilities": {"type": "array", "items": {"type": "string"}},
        "scope_types": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"value": {"type": "string"}, "label": {"type": "string"}},
            },
        },
        "stores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "store_type": {"type": "string"},
                },
            },
        },
        "brands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
        },
    },
}
_PENDING_ACCESS_RESPONSE = {
    "type": "object",
    "required": ["change_id", "approval_id", "status", "detail"],
    "properties": {
        "change_id": {"type": "integer"},
        "approval_id": {"type": "integer"},
        "status": {"type": "string", "enum": ["pending_approval"]},
        "detail": {"type": "string"},
        "cells": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    },
}


class AdminMetaView(APIView):
    permission_classes = [IsAuthenticated, IsRbacAdmin]

    @extend_schema(responses={200: _ADMIN_META_RESPONSE})
    def get(self, request: Request) -> Response:
        return Response(
            {
                "nav_groups": list(NAV_GROUPS),  # legacy; nothing navigates by it
                # What the role editor actually edits: the sections in sidebar
                # order and the ladder each may be set to. Sent as data so adding
                # a section needs no front-end release (Rule 12).
                "sections": [{"code": code, "label": label} for code, label in SECTIONS],
                "capabilities": list(CAPABILITY_ORDER),
                "scope_types": [
                    {"value": value, "label": label} for value, label in ScopeType.choices
                ],
                "stores": [
                    {"id": s.id, "code": s.code, "name": s.name, "store_type": s.store_type}
                    for s in Store.objects.filter(is_active=True).order_by("name")
                ],
                # Brand-scoped users (a brand manager) are assigned brands, not
                # stores — the user editor needs the list to pick from (#88).
                "brands": [
                    {"id": b.id, "code": b.code, "name": b.name}
                    for b in Brand.objects.filter(is_active=True).order_by("name")
                ],
            }
        )


def _pending_response(change: AccessChange, approval: Any) -> Response:
    return Response(
        {
            "change_id": change.pk,
            "approval_id": approval.pk,
            "status": "pending_approval",
            "detail": "A different Owner or IT Admin must approve this change.",
        },
        status=status.HTTP_202_ACCEPTED,
    )


class PendingAccessChangeMixin:
    """Every Setup write becomes a proposal a second administrator applies.

    Always combined with a `generics.GenericAPIView` subclass (see the view
    classes below), which is where `get_serializer`/`get_object` actually come
    from - this mixin alone is never instantiated on its own.
    """

    access_resource: str

    if TYPE_CHECKING:
        # Declared only so mypy can check this mixin in isolation; the real
        # implementations are supplied by whichever `generics.GenericAPIView`
        # subclass a concrete view also inherits from.
        def get_serializer(self, *args: Any, **kwargs: Any) -> Any: ...
        def get_object(self) -> Any: ...

    def _propose(self, request: Request, *, target: Any = None, partial: bool = False) -> Response:
        # A field this serializer does not know is refused rather than dropped.
        # DRF's default is to ignore it, which is the one answer this endpoint
        # must never give: somebody sending `allow_self_approval: true` to
        # switch off a floor rule would get 202 and believe it worked. There is
        # no such flag and there never will be — so say so, out loud.
        writable = {
            name for name, field in self.get_serializer().fields.items() if not field.read_only
        }
        unknown = sorted(set(request.data) - writable)
        if unknown:
            return Response(
                {
                    "detail": (
                        "These are not settings on this row, and the four floor rules "
                        "cannot be configured away by adding one: "
                        f"{', '.join(unknown)}."
                    ),
                    "fields": unknown,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        change, approval = propose_access_change(
            resource=self.access_resource,
            # `IsAuthenticated` (every concrete view below) guarantees a real
            # `User`, not the `AnonymousUser` half of DRF's `request.user`.
            actor=cast(User, request.user),
            data=request.data,
            target=target,
            partial=partial,
        )
        return _pending_response(change, approval)

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self._propose(request)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self._propose(
            request,
            target=self.get_object(),
            partial=bool(kwargs.get("partial", False)),
        )


class AccessMatrixView(APIView):
    """The roles x sections grid an administrator edits (#173).

    The answer is the **stored** matrix - ``Role.section_access`` as it is
    today, not the seed table it started from - plus the cells the money floor
    has locked and the sentence to show over each. The grid is data all the way
    down: sections, rungs, roles and locks all arrive from here, so adding a
    section or ratifying a floor needs no front-end release (Rule 12).
    """

    # Both gates, like every sibling admin endpoint. The Setup rung and the
    # access-administrator floor are separate questions (a role can be granted
    # `setup: manage` and still not be Owner or IT Admin), and reading who may
    # do what across the whole business, with head counts, is the same secret
    # the roles list keeps.
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "required": ["sections", "capabilities", "rules", "roles"],
                "properties": {
                    "sections": {"type": "array", "items": _SECTION_CHOICE},
                    "capabilities": {"type": "array", "items": _SECTION_CHOICE},
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rule": {"type": "integer"},
                                "text": {"type": "string"},
                            },
                        },
                    },
                    "roles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "name": {"type": "string"},
                                "is_system": {"type": "boolean"},
                                "is_active": {"type": "boolean"},
                                "user_count": {"type": "integer"},
                                "section_access": _SECTION_ACCESS,
                                "locked": {
                                    "type": "object",
                                    "additionalProperties": {
                                        "type": "object",
                                        "properties": {
                                            "max_capability": {"type": "string"},
                                            "rule": {"type": "string"},
                                            "reason": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    )
    def get(self, request: Request) -> Response:
        roles = Role.objects.annotate(head_count=Count("users")).order_by("name")
        return Response(
            {
                "sections": [{"code": code, "label": label} for code, label in SECTIONS],
                # The ladder with its words, so the grid never has to keep its
                # own copy of what "operate" means (Rule 12: the screen renders
                # the payload, it does not restate it).
                "capabilities": [
                    {"code": cap, "label": CAPABILITY_WORDS[cap]} for cap in CAPABILITY_ORDER
                ],
                # All four ratified rules, including the two no cell can express,
                # so the screen states the whole floor rather than the part it
                # happens to grey out.
                "rules": [{"rule": number, "text": text} for number, text in sorted(RULES.items())],
                "roles": [
                    {
                        "code": role.code,
                        "name": role.name,
                        "is_system": role.is_system,
                        "is_active": role.is_active,
                        "user_count": role.head_count,
                        "section_access": stored_row(role),
                        "locked": cell_limits(role.code),
                    }
                    for role in roles
                ],
            }
        )


class RoleAccessView(APIView):
    """Replace one role's row of the matrix - as a proposal, never as a save.

    Two things stand between an administrator and the stored row, and both are
    floor rules rather than policy:

    · the **money floor** (``accounts.floors``) refuses a cell that would put a
      store seat on the books or hand full Money or full Setup to a role the
      ruling does not trust - cell by cell, naming each one;
    · **"never by one person alone"** (rule 4) makes the write a proposal a
      second Owner or IT Admin applies through the existing approvals
      machinery. The api-contract sketched an immediate 200 here; a direct write
      would have been the one door in the system where one person could change
      a role, which is exactly what the rule this ticket is enforcing forbids.
      So the endpoint answers 202 with the approval to clear.
    """

    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "required": ["section_access"],
                "additionalProperties": False,
                "properties": {"section_access": _SECTION_ACCESS},
            }
        },
        responses={
            200: {
                "type": "object",
                "description": "No changed cells; no approval request was created.",
                "properties": {
                    "status": {"type": "string", "enum": ["unchanged"]},
                    "detail": {"type": "string"},
                    "code": {"type": "string"},
                    "section_access": _SECTION_ACCESS,
                },
            },
            202: _PENDING_ACCESS_RESPONSE,
            400: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        },
    )
    def put(self, request: Request, code: str) -> Response:
        try:
            role = Role.objects.get(code=code)
        except Role.DoesNotExist:
            return Response(
                {"error": f"No role with code {code!r}.", "code": "NOT_FOUND"},
                status=status.HTTP_404_NOT_FOUND,
            )

        unknown = sorted(set(request.data) - {"section_access"})
        if unknown:
            return Response(
                {
                    "error": (
                        "This endpoint sets section access and nothing else, and the "
                        "four floor rules cannot be configured away by adding a "
                        f"setting: {', '.join(unknown)}."
                    ),
                    "code": "VALIDATION",
                    "fields": unknown,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested = request.data.get("section_access")
        if not isinstance(requested, dict):
            return Response(
                {"error": "section_access must be an object.", "code": "VALIDATION"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        invalid = invalid_cells(requested)
        if invalid:
            return Response(
                {"error": "; ".join(invalid), "code": "VALIDATION"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        crossed = floor_violations(role.code, requested)
        if crossed:
            return Response(
                {
                    "error": " ".join(describe_floors(crossed)),
                    "code": "FLOOR_LOCKED",
                    "cells": crossed,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        before = stored_row(role)
        after = replacement_row(requested, current=before)
        cells = diff(before, after)
        if not cells:
            return Response(
                {
                    "status": "unchanged",
                    "detail": "Nothing changed, so nobody was asked to approve anything.",
                    "code": role.code,
                    "section_access": before,
                }
            )

        change, approval = propose_access_change(
            resource=AccessChange.Resource.ROLE,
            # `IsAuthenticated` above guarantees a real `User`.
            actor=cast(User, request.user),
            data={"section_access": after},
            target=role,
            partial=True,
            # The row as it stands *now* rides along with the diff. The proposal
            # is a whole-row replacement built from a grid somebody looked at
            # minutes ago, so the applier compares this against the row it is
            # about to overwrite and refuses if a second administrator moved it
            # in between - otherwise a stale column silently reverts their work.
            annotations={"cells": cells, "before": before},
            summary=(
                f"Access change: {role.name} - "
                + ", ".join(f"{c['section']} {c['from']}->{c['to']}" for c in cells)
            ),
        )
        response = _pending_response(change, approval)
        response.data["cells"] = cells
        return response


#: Users & Roles (#106) — name / code, same as every other master list.
ROLE_SEARCH_FIELDS = ("code", "name")
USER_SEARCH_FIELDS = ("username", "full_name")


class RoleListCreateView(PendingAccessChangeMixin, generics.ListCreateAPIView[Role]):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = AdminRoleSerializer
    access_resource = AccessChange.Resource.ROLE

    def get_queryset(self) -> Any:
        qs = Role.objects.all().order_by("name")
        return text_filter(qs, search_term(self.request), ROLE_SEARCH_FIELDS)


class RoleDetailView(PendingAccessChangeMixin, generics.RetrieveUpdateAPIView[Role]):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = AdminRoleSerializer
    queryset = Role.objects.all()
    access_resource = AccessChange.Resource.ROLE


class UserListCreateView(PendingAccessChangeMixin, generics.ListCreateAPIView[User]):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = AdminUserSerializer
    access_resource = AccessChange.Resource.USER

    def get_queryset(self) -> Any:
        qs = (
            User.objects.select_related("role", "entity")
            .prefetch_related("stores", "brands")
            .order_by("username")
        )
        return text_filter(qs, search_term(self.request), USER_SEARCH_FIELDS)


class UserDetailView(PendingAccessChangeMixin, generics.RetrieveUpdateAPIView[User]):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = AdminUserSerializer
    access_resource = AccessChange.Resource.USER

    def get_queryset(self) -> Any:
        return User.objects.select_related("role", "entity").prefetch_related("stores", "brands")


class ActorPolicyListView(generics.ListAPIView[ActorPolicy]):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = ActorPolicySerializer
    queryset = ActorPolicy.objects.all()


class ActorPolicyDetailView(PendingAccessChangeMixin, generics.RetrieveUpdateAPIView[ActorPolicy]):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = ActorPolicySerializer
    queryset = ActorPolicy.objects.all()
    access_resource = AccessChange.Resource.ACTOR_POLICY
    lookup_field = "action"
    lookup_url_kwarg = "action"


class ApprovalPolicyListCreateView(
    PendingAccessChangeMixin, generics.ListCreateAPIView[ApprovalPolicy]
):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = ApprovalPolicyAdminSerializer
    queryset = ApprovalPolicy.objects.all()
    access_resource = AccessChange.Resource.APPROVAL_POLICY


class ApprovalPolicyDetailView(
    PendingAccessChangeMixin, generics.RetrieveUpdateAPIView[ApprovalPolicy]
):
    permission_classes = [IsAuthenticated, IsRbacAdmin, IsAccessAdministrator]
    serializer_class = ApprovalPolicyAdminSerializer
    queryset = ApprovalPolicy.objects.all()
    access_resource = AccessChange.Resource.APPROVAL_POLICY
    lookup_field = "kind"
    lookup_url_kwarg = "kind"
