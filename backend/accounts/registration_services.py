"""One installation, one jointly confirmed genesis. Never a reusable grant path."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import DatabaseError, connection, transaction

from accounts.goods_setup import (
    create_person,
    ensure_goods_roles,
    seed_role_assignment,
    service_principal,
)
from accounts.models import LoginAttempt
from accounts.rbac_matrix import section_access_for
from accounts.registration_models import InstallationRegistration
from accounts.role_assignments import INITIAL_FIELD_ACCESS, INITIAL_ROLE_CODES
from accounts.unified_policy import (
    ACTION_LEVELS,
    WORKFLOW_TARGET_KEY,
    initial_step_actions,
    serialise_workflow_levels,
)
from core.canonical import content_hash
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    database_now,
    execute_command,
)
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.goods_models import SiteGuard, Tenant
from masters.goods_services import (
    append_master_version,
    ensure_site_sbus,
    ensure_system_locations,
    start_revision,
)
from masters.models import Gstin, LegalEntity, Store

T = TypeVar("T")
CLOSED_MESSAGE = (
    "This installation is already registered. New company registrations are closed."
)


def deployment_key() -> uuid.UUID:
    try:
        return uuid.UUID(str(settings.KDPS_DEPLOYMENT_KEY))
    except (ValueError, AttributeError):
        raise Refusal(
            "SERVICE_UNAVAILABLE", "The installation binding is not configured."
        ) from None


def public_state() -> dict[str, Any]:
    closed = Tenant.objects.exists()
    pending = (
        not closed
        and InstallationRegistration.objects.filter(
            deployment_key=deployment_key()
        ).exists()
    )
    return {
        "available": not closed,
        "pending_confirmation": pending,
        "message": CLOSED_MESSAGE
        if closed
        else "Register this company and its first store.",
        "synthetic": bool(
            Tenant.objects.filter(
                deployment_key=deployment_key(), synthetic=True
            ).exists()
        ),
    }


def _private_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hmac.new(
        str(settings.SECRET_KEY).encode(), encoded, hashlib.sha256
    ).hexdigest()


def registration_baseline() -> dict[str, Any]:
    """Pin the complete initial role and action registry, not just role labels."""
    return {
        "roles": {
            code: {
                "section_access": section_access_for(code),
                "field_access": list(INITIAL_FIELD_ACCESS[code]),
                "step_actions": initial_step_actions(code),
            }
            for code in sorted(INITIAL_ROLE_CODES)
        },
        "action_levels": serialise_workflow_levels(ACTION_LEVELS),
    }


def registration_summary(data: dict[str, Any]) -> dict[str, Any]:
    people = {
        key: {k: v for k, v in data[key].items() if k != "temporary_password"}
        for key in ("owner", "admin")
    }
    return {
        "company": data["company"],
        "store": data["store"],
        **people,
        "proposed_team": data.get("proposed_team", []),
        "initial_access": {
            "policy_baseline": registration_baseline(),
            "owner": {"role": "owner", "all_sites": True, "all_brands": True},
            "admin": {
                "role": "it_admin",
                "all_sites": True,
                "all_brands": True,
                "protected_business_fields": [],
            },
            "other_people": "Proposed only; normal company administration and independent review are required.",
            "trading": "Inactive until approved setup, stock and selling readiness.",
            "confirmation": "One-time joint Owner and separate Admin genesis exception.",
        },
    }


def _safe_result(row: InstallationRegistration) -> dict[str, Any]:
    return {
        "state": "registered" if row.completed_at else "awaiting_confirmation",
        "summary": row.summary,
        "summary_hash": row.summary_hash,
        "revision": row.revision,
        "confirmed": {
            "owner": row.owner_confirmed_at is not None,
            "admin": row.admin_confirmed_at is not None,
        },
        "first_store_id": row.first_store_id,
        "login_url": "/login",
    }


def _locked_row(key: uuid.UUID) -> InstallationRegistration | None:
    lock_id = int.from_bytes(
        hashlib.sha256(f"installation-registration:{key}".encode()).digest()[:8], "big"
    ) % (2**63)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])
    return (
        InstallationRegistration.objects.select_for_update()
        .filter(deployment_key=key)
        .first()
    )


def _atomic_claim(operation: Callable[[], T]) -> T:
    """Retry the complete outer claim, including tenant creation and its kernel.

    The kernel cannot retry a nested transaction. Establish isolation before
    any business query here; middleware's earlier lookup is outside this block.
    """
    nested = connection.in_atomic_block
    for attempt in range(5):
        committing = False
        try:
            with transaction.atomic():
                if not nested:
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                result = operation()
                committing = True
            return result
        except DatabaseError as exc:
            cause = exc.__cause__
            state = getattr(cause, "sqlstate", None) or getattr(exc, "sqlstate", None)
            constraint = getattr(getattr(cause, "diag", None), "constraint_name", "")
            # A transaction waiting for the installation advisory lock can
            # retain its pre-wait SERIALIZABLE snapshot. Concurrent first
            # staging may therefore meet the newly committed claim's primary
            # key; retry this exact claim conflict, never unrelated data errors.
            registration_race = (
                state == "23505"
                and constraint == "accounts_installationregistration_pkey"
            )
            if not nested and (state in {"40001", "40P01"} or registration_race):
                if attempt < 4:
                    time.sleep(secrets.randbelow(20 * (attempt + 1) + 1) / 1000)
                    continue
                raise Refusal(
                    "RETRY_EXHAUSTED", "Registration was busy. Retry the same request."
                ) from None
            if not nested and (state is None or str(state).startswith("08")):
                raise Refusal(
                    "OUTCOME_UNKNOWN" if committing else "SERVICE_UNAVAILABLE",
                    "Check registration status and retry the same request. Do not start another registration.",
                ) from None
            raise
    raise Refusal("RETRY_EXHAUSTED", "Retry the same registration request.")


def stage_registration(data: dict[str, Any], *, edit: bool = False) -> dict[str, Any]:
    key = deployment_key()
    summary = registration_summary(data)
    fingerprint = _private_digest(
        {k: v for k, v in data.items() if k != "current_owner_password"}
    )

    def operation() -> dict[str, Any] | Refusal:
        row = _locked_row(key)
        if row and row.command_id == data["command_id"]:
            if not hmac.compare_digest(row.request_fingerprint, fingerprint):
                raise Refusal(
                    "COMMAND_CONFLICT",
                    "This registration command was used with different information.",
                )
            if row.completed_at:
                return {"state": "registered", "login_url": "/login"}
            return _safe_result(row)
        if Tenant.objects.exists():
            raise Refusal("REGISTRATION_CLOSED", CLOSED_MESSAGE)
        if row is not None:
            if not edit:
                raise Refusal(
                    "REGISTRATION_PENDING",
                    "Initial registration is waiting for Owner and Admin confirmation.",
                )
            authenticated = _authenticate(
                row, row.summary["owner"]["email"], data["current_owner_password"]
            )
            if authenticated is None:
                return Refusal(
                    "INVALID_CREDENTIALS",
                    "The initial credentials were not accepted.",
                    status=403,
                )
            if authenticated != "owner":
                raise Refusal(
                    "ACTION_DENIED",
                    "Only the proposed Owner may revise initial registration.",
                )
            history = list(row.confirmation_history)
            history.append(
                {
                    "event": "revised",
                    "summary_hash": row.summary_hash,
                    "revision": row.revision,
                    "at": database_now().isoformat(),
                }
            )
            row.confirmation_history = history
            row.revision += 1
            row.owner_confirmed_at = None
            row.admin_confirmed_at = None
        else:
            if edit:
                raise Refusal("NOT_FOUND", "There is no pending registration.")
            row = InstallationRegistration(deployment_key=key)
        row.command_id = data["command_id"]
        row.request_fingerprint = fingerprint
        row.summary = summary
        row.summary_hash = content_hash(summary)
        row.owner_password_hash = make_password(data["owner"]["temporary_password"])
        row.admin_password_hash = make_password(data["admin"]["temporary_password"])
        row.save()
        return _safe_result(row)

    result = _atomic_claim(operation)
    if isinstance(result, Refusal):
        raise result
    return result


def _authenticate(
    row: InstallationRegistration, email: str, password: str
) -> str | None:
    identifier = (
        f"registration:{_private_digest([str(row.deployment_key), email.lower()])}"
    )
    guard, _ = LoginAttempt.objects.select_for_update().get_or_create(
        identifier=identifier
    )
    now = database_now()
    if guard.locked_until and guard.locked_until > now:
        raise Refusal("LOGIN_LOCKED", "Too many attempts. Try again later.")
    if guard.locked_until is not None:
        guard.failures = 0
        guard.locked_until = None
    role = next(
        (
            key
            for key in ("owner", "admin")
            if row.summary[key]["email"] == email.lower()
        ),
        None,
    )
    encoded = getattr(row, f"{role}_password_hash", "") if role else ""
    accepted = bool(encoded) and check_password(password, encoded)
    if not accepted:
        # Hashing even an unknown identifier keeps it from becoming an email oracle.
        if not encoded:
            make_password(password)
        guard.failures += 1
        if guard.failures >= 10:
            guard.locked_until = now + timedelta(minutes=15)
        guard.save()
        return None
    guard.failures = 0
    guard.locked_until = None
    guard.save()
    return role


def confirm_registration(data: dict[str, Any]) -> dict[str, Any]:
    key = deployment_key()

    def operation() -> dict[str, Any] | Refusal:
        row = _locked_row(key)
        if (
            row is not None
            and row.completed_at
            and data.get("summary_hash") == row.summary_hash
        ):
            # A lost success reply may be retried after closure. This receipt
            # contains no identity/profile data and never reinstates authority.
            return {"state": "registered", "login_url": "/login"}
        if row is None or row.completed_at or Tenant.objects.exists():
            raise Refusal("REGISTRATION_CLOSED", CLOSED_MESSAGE)
        role = _authenticate(row, data["email"], data["temporary_password"])
        if role is None:
            return Refusal(
                "INVALID_CREDENTIALS",
                "The initial credentials were not accepted.",
                status=403,
            )
        supplied_hash = data.get("summary_hash")
        if supplied_hash is None:
            return {**_safe_result(row), "confirming_role": role}
        if data.get("acknowledged") is not True:
            raise Refusal(
                "INVALID_REQUEST",
                "Personally acknowledge the initial summary before confirming.",
            )
        if not hmac.compare_digest(supplied_hash, row.summary_hash):
            raise Refusal(
                "REVISION_SUPERSEDED",
                "Initial details changed. Review the new summary before confirming.",
            )
        at = database_now()
        if getattr(row, f"{role}_confirmed_at") is None:
            setattr(row, f"{role}_confirmed_at", at)
            row.confirmation_history = [
                *row.confirmation_history,
                {
                    "event": "confirmed",
                    "role": role,
                    "email": row.summary[role]["email"],
                    "summary_hash": row.summary_hash,
                    "revision": row.revision,
                    "at": at.isoformat(),
                },
            ]
        if row.owner_confirmed_at and row.admin_confirmed_at:
            _complete_registration(row)
        row.save()
        return {**_safe_result(row), "confirming_role": role}

    result = _atomic_claim(operation)
    if isinstance(result, Refusal):
        raise result
    return result


def _complete_registration(row: InstallationRegistration) -> None:
    summary = row.summary
    baseline = summary.get("initial_access", {}).get("policy_baseline")
    if baseline != registration_baseline():
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The initial access baseline changed. The proposed Owner must revise it and both people must confirm again.",
        )
    company, first = summary["company"], summary["store"]
    tenant = Tenant.objects.create(
        id=uuid.uuid5(row.deployment_key, "registered-company"),
        deployment_key=row.deployment_key,
        code=company["code"],
        name=company["name"],
        timezone=company["timezone"],
        currency=company["currency"],
        locale=company["locale"],
        synthetic=False,
    )

    def handler(run: CommandRun) -> CommandResult:
        roles = ensure_goods_roles()
        for code, role in roles.items():
            append_master_version(
                run,
                kind="role",
                target_key=code,
                revision=1,
                payload={
                    "role_policy": {
                        "section_access": role.section_access,
                        "field_access": role.field_access,
                        "step_actions": role.permissions_map["step_actions"],
                    }
                },
            )
        append_master_version(
            run,
            kind="tenant",
            target_key=WORKFLOW_TARGET_KEY,
            revision=1,
            payload={"action_levels": baseline["action_levels"]},
        )
        people: dict[str, str] = {}
        for who, code in (("owner", "owner"), ("admin", "it_admin")):
            person = summary[who]
            human, user = create_person(
                run,
                email=person["email"],
                display_name=person["name"],
                staff_code=person["staff_code"],
                password=None,
                must_change_password=True,
                username=f"initial-{uuid.uuid5(row.deployment_key, who)}",
            )
            user.password = getattr(row, f"{who}_password_hash")
            user.save(update_fields=["password"])
            seed_role_assignment(
                tenant,
                human,
                code,
                source_key=f"joint-registration-{who}",
                all_sites=True,
                all_brands=True,
            )
            people[who] = str(human.pk)
            append_master_version(
                run,
                kind="user",
                target_key=str(user.pk),
                revision=1,
                payload={
                    "human_id": str(human.pk),
                    "email": person["email"],
                    "display_name": person["name"],
                    "active": True,
                    "must_change_password": True,
                },
            )
        entity = LegalEntity.objects.create(
            tenant_id=tenant.pk,
            code=company["code"],
            name=company["legal_name"],
            pan=company["pan"],
        )
        gstin = Gstin.objects.create(
            tenant_id=tenant.pk,
            legal_entity=entity,
            gstin=company["gstin"],
            state_code=company["state_code"],
            state_name=company["state_name"],
        )
        store = Store.objects.create(
            tenant_id=tenant.pk,
            code=first["code"],
            name=first["name"],
            store_type="store",
            gstin=gstin,
            city=first["city"],
        )
        entity_payload = {
            "code": entity.code,
            "name": entity.name,
            "pan": entity.pan,
            "address": company["billing_address"],
            "books_code": "",
        }
        registration_payload = {
            "entity_id": str(entity.pk),
            "gstin": gstin.gstin,
            "state_code": gstin.state_code,
            "state_name": gstin.state_name,
        }
        site_payload = {
            "code": store.code,
            "name": store.name,
            "aliases": [],
            "city": store.city,
            "state": company["state_name"],
            "country": company["country"],
            "address": first["address"],
            "type": "store",
            "entity_id": str(entity.pk),
            "registration_id": str(gstin.pk),
            "counter_count": 1,
            "permitted_operations": ["receive", "hold", "transfer", "sell"],
            "brand_ids": [],
            "setup_kind": first["setup_kind"],
            "source_system": first["source_system"],
        }
        for kind, model, payload in (
            ("entity", entity, entity_payload),
            ("registration", gstin, registration_payload),
            ("site", store, site_payload),
        ):
            start_revision(tenant.pk, f"master:{kind}", str(model.pk))
            append_master_version(
                run, kind=kind, target_key=str(model.pk), revision=1, payload=payload
            )
        SiteGuard.objects.create(
            tenant_id=tenant.pk,
            site=store,
            stock_contract="goods_v1",
            lifecycle="planned",
            selling_mode="online_alpha",
            opening_setup_ready=False,
            goods_ready=False,
            sell_ready=False,
        )
        ensure_system_locations(tenant.pk, store)
        ensure_site_sbus(tenant.pk, store, [])
        row.first_store_id = store.pk
        run.authority["joint_genesis"] = {
            "summary_hash": row.summary_hash,
            "revision": row.revision,
            "people": people,
            "confirmations": row.confirmation_history,
            "exception": "Initial Owner and separate Admin only; no subsequent platform staff/grant access.",
        }
        run.audit_after = [
            {
                "field": "initial_summary_hash",
                "redacted": False,
                "value": row.summary_hash,
            },
            {"field": "first_store", "redacted": False, "value": str(store.pk)},
        ]
        return CommandResult(
            resource_type="registration", resource_id=str(tenant.pk), status_code=201
        )

    with tenant_context(tenant.pk):
        execute_command(
            service_principal(tenant.pk, "initial_joint_registration"),
            CommandSpec(
                action="deployment.register",
                command_id=row.command_id,
                business_input={
                    "summary_hash": row.summary_hash,
                    "revision": row.revision,
                },
                subject_key=f"installation:{row.deployment_key}",
            ),
            handler,
        )
    row.tenant = tenant
    row.completed_at = database_now()
    row.owner_password_hash = ""
    row.admin_password_hash = ""
