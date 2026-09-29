"""E239: self-service password change (GSA-T03, ticket 03A, design §6.2).

Ends every session this person holds on success, the calling one included: the
handler bumps the person's `SecurityGuard` epoch, the same mechanism an access
change already relies on (`accounts.sessions.bump_security_epoch`), so every
`ServerSession` whose stored `security_epoch` no longer matches fails its next
`resolve_session` and the caller must sign in again.

Not run through the goods-v1 command-replay contract: there is no `command_id`
or `expected_revision` on the wire (design §6.2, E239), so a fresh internal
command id is minted per call. Atomicity, the append-only evidence chain and the
secret-free audit fact still come from the same kernel every goods-v1 write
uses (`core.commands.execute_command`); only the wire shape is plain.

A wrong current password is judged and locked out exactly like a wrong login or
step-up password (`accounts.sessions.check_lock`/`record_failure`), against the
same per-identifier window - before the kernel ever sees the request, so a
wrong guess leaves no command evidence and no state changes. Design §6.2 gives
E239 its own, simpler refusal shape: AUTH_REQUIRED for a wrong current
password, INVALID_REQUEST for a policy-rejected new one - not the admin
surface's ACCESS_INVALID/PASSWORD_REJECTED issue shape (`apply_password`).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError

from accounts.goods_admin_services import audit_values
from accounts.models import User
from accounts.sessions import bump_security_epoch, check_lock, clear_failures, record_failure
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    Principal,
    database_now,
    execute_command,
)
from core.refusals import Refusal, issue


def change_own_password(
    *, user: User, session: Any, current_password: str, new_password: str
) -> None:
    """Verify, replace and end every session - or leave everything exactly as it was."""
    # A live session's user always has a person and a tenant (`accounts.sessions.
    # authenticate_email`/`resolve_session` refuse anything else before a request
    # ever reaches a view).
    tenant_id = user.tenant_id
    human_id = user.human_id
    email = user.email
    assert tenant_id is not None and human_id is not None and email is not None
    now = database_now()
    check_lock(tenant_id, email, now)
    if not user.check_password(current_password):
        record_failure(tenant_id, email, now)
        raise Refusal("AUTH_REQUIRED", "That is not your current password.")
    # Read after the check: a hasher upgrade inside `check_password` saves a new hash.
    verified_hash = user.password
    clear_failures(tenant_id, email)

    try:
        password_validation.validate_password(new_password, user=user)
    except ValidationError as exc:
        raise Refusal(
            "INVALID_REQUEST",
            "That password does not meet the password rules.",
            issues=[
                issue("PASSWORD_REJECTED", message, field="new_password")
                for message in exc.messages
            ],
        ) from None
    if user.check_password(new_password):
        raise Refusal(
            "INVALID_REQUEST",
            "That password does not meet the password rules.",
            issues=[
                issue(
                    "PASSWORD_REJECTED",
                    "The new password must be different from the current one.",
                    field="new_password",
                )
            ],
        )

    principal = Principal(
        tenant_id=tenant_id,
        human_id=human_id,
        user_id=user.pk,
        session_id=getattr(session, "pk", None),
        step_up_at=getattr(session, "step_up_at", None),
    )

    def handler(run: CommandRun) -> CommandResult:
        # Re-read the login under a row lock: an administrator's assisted reset
        # that committed after the check above must win, never be overwritten by
        # the session that reset was meant to end.
        locked = User.objects.select_for_update().get(pk=user.pk)
        if locked.password != verified_hash:
            raise Refusal("AUTH_REQUIRED", "Your password was just changed. Sign in again.")
        locked.set_password(new_password)
        locked.must_change_password = False
        locked.save(update_fields=["password", "must_change_password"])
        bump_security_epoch(human_id, run.tenant_id)
        run.audit_subject_key = f"user:{user.pk}"
        # Never the password itself - `credential` is always redacted (`SECRET_FIELDS`
        # also drops it outright), so this records only the secret-free fact that a
        # change happened, with the actor and time every `AuditEvent` already carries.
        run.audit_after = audit_values({"credential": None}, redacted={"credential"})
        return CommandResult(resource_type="user", resource_id=str(user.pk))

    execute_command(
        principal,
        CommandSpec(action="auth.password_change", command_id=uuid.uuid4(), business_input={}),
        handler,
    )
