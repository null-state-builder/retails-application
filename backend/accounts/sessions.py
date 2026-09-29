"""Server-side sessions, lockout and password step-up (design §4.2, E001-E005).

The browser holds two cookies: an HttpOnly random session token (only its hash
is stored) and a readable CSRF token that every cookie-authenticated write must
echo in ``X-CSRF-Token``. Sessions last at most 12 hours and end after 30 idle
minutes. Re-entering the password grants a five-minute step-up bound to this
session; logout, password change, revocation or an access change ends it.

Every expiry, idle limit and step-up window is judged by the database clock, the
same clock commands stamp and re-check authority with.

Ten failed attempts for one identifier inside 15 minutes lock that identifier
for 15 minutes, and an unknown email gets exactly the same answer as a wrong
password.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction

from core.canonical import sha256_hex
from core.commands import database_now
from core.refusals import Refusal

SESSION_COOKIE = "kdps_session"
CSRF_COOKIE = "kdps_csrf"
CSRF_HEADER = "HTTP_X_CSRF_TOKEN"

ABSOLUTE_LIFE = timedelta(hours=12)
IDLE_LIFE = timedelta(minutes=30)
STEP_UP_LIFE = timedelta(minutes=5)
LOCK_WINDOW = timedelta(minutes=15)
LOCK_DURATION = timedelta(minutes=15)
MAX_FAILURES = 10
#: How stale ``last_seen_at`` may be before a request refreshes it.
TOUCH_INTERVAL = timedelta(seconds=60)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return sha256_hex(token)


def identifier_hash(tenant_id: uuid.UUID | None, identifier: str) -> str:
    return sha256_hex(f"{tenant_id}|{identifier.strip().lower()}")


@dataclass
class IssuedSession:
    session: Any
    token: str
    csrf_token: str


def _guard_epoch(human: Any) -> int:
    from accounts.goods_models import SecurityGuard

    guard, _ = SecurityGuard.objects.get_or_create(tenant_id=human.tenant_id, human=human)
    return int(guard.epoch)


def check_lock(tenant_id: uuid.UUID, identifier: str, now: datetime) -> None:
    from accounts.goods_models import AuthenticationFailure

    row = AuthenticationFailure.objects.filter(
        tenant_id=tenant_id, identifier_hash=identifier_hash(tenant_id, identifier)
    ).first()
    if row is not None and row.locked_until is not None and row.locked_until > now:
        raise Refusal(
            "LOGIN_LOCKED", "Too many failed attempts. Try again in 15 minutes.", status=429
        )


def record_failure(tenant_id: uuid.UUID, identifier: str, now: datetime) -> None:
    from accounts.goods_models import AuthenticationFailure

    digest = identifier_hash(tenant_id, identifier)
    with transaction.atomic():
        row, _ = AuthenticationFailure.objects.select_for_update().get_or_create(
            tenant_id=tenant_id,
            identifier_hash=digest,
            defaults={"window_start": now, "failure_count": 0},
        )
        if row.window_start < now - LOCK_WINDOW:
            row.window_start = now
            row.failure_count = 0
            row.locked_until = None
        row.failure_count += 1
        if row.failure_count >= MAX_FAILURES:
            row.locked_until = now + LOCK_DURATION
        row.save()


def clear_failures(tenant_id: uuid.UUID, identifier: str) -> None:
    from accounts.goods_models import AuthenticationFailure

    AuthenticationFailure.objects.filter(
        tenant_id=tenant_id, identifier_hash=identifier_hash(tenant_id, identifier)
    ).delete()


def authenticate_email(tenant_id: uuid.UUID, email: str, password: str) -> Any:
    """The active user for ``email``+``password``, or a uniform refusal."""
    from accounts.models import User

    now = database_now()
    check_lock(tenant_id, email, now)
    user = (
        User.objects.select_related("human")
        .filter(email__iexact=email.strip(), tenant_id=tenant_id)
        .first()
    )
    valid = (
        user is not None
        and user.is_active
        and user.human is not None
        and user.human.active
        and not person_retired(user.human_id, now)
        and user.check_password(password)
    )
    if not valid:
        if user is None:
            # Spend the same hashing time an existing account would.
            User().set_password(password)
        record_failure(tenant_id, email, now)
        raise Refusal(
            "INVALID_CREDENTIALS", "That email and password do not match an active account."
        )
    clear_failures(tenant_id, email)
    return user


def issue_session(user: Any) -> IssuedSession:
    from accounts.goods_models import ServerSession

    now = database_now()
    token = new_token()
    csrf = new_token()
    session = ServerSession.objects.create(
        tenant_id=user.tenant_id,
        user=user,
        token_hash=token_hash(token),
        csrf_hash=token_hash(csrf),
        issued_at=now,
        last_seen_at=now,
        expires_at=now + ABSOLUTE_LIFE,
        security_epoch=_guard_epoch(user.human),
    )
    return IssuedSession(session=session, token=token, csrf_token=csrf)


def resolve_session(token: str | None, *, raise_expired: bool = False) -> Any:
    """The live session for a cookie token, refreshed for idle time; ``None`` if absent.

    An expired session reads as no session (so a stale cookie never breaks a public
    page) unless the caller asks, as refresh does, to hear ``SESSION_EXPIRED``.
    """
    from accounts.goods_models import SecurityGuard, ServerSession

    if not token:
        return None
    now = database_now()
    session = (
        ServerSession.objects.select_related("user", "user__human")
        .filter(token_hash=token_hash(token))
        .first()
    )
    if session is None or session.revoked_at is not None:
        return None
    if session.expires_at <= now or session.last_seen_at <= now - IDLE_LIFE:
        if raise_expired:
            raise Refusal("SESSION_EXPIRED", "Your session has ended. Sign in again.")
        return None
    user = session.user
    if not user.is_active or user.human is None or not user.human.active:
        return None
    if person_retired(user.human_id, now):
        return None
    epoch = (
        SecurityGuard.objects.filter(human_id=user.human_id).values_list("epoch", flat=True).first()
    )
    # No guard row is no verified epoch: fail closed, as the command re-check does.
    if epoch is None or int(epoch) != int(session.security_epoch):
        return None
    if session.last_seen_at <= now - TOUCH_INTERVAL:
        ServerSession.objects.filter(pk=session.pk).update(last_seen_at=now)
        session.last_seen_at = now
    return session


def rotate_session(session: Any) -> IssuedSession:
    """New token and CSRF secret; the absolute expiry never moves."""
    from accounts.goods_models import ServerSession

    now = database_now()
    if session.expires_at <= now or session.last_seen_at <= now - IDLE_LIFE:
        raise Refusal("SESSION_EXPIRED", "Your session has ended. Sign in again.")
    token = new_token()
    csrf = new_token()
    ServerSession.objects.filter(pk=session.pk).update(
        token_hash=token_hash(token), csrf_hash=token_hash(csrf), last_seen_at=now
    )
    session.token_hash = token_hash(token)
    session.csrf_hash = token_hash(csrf)
    session.last_seen_at = now
    return IssuedSession(session=session, token=token, csrf_token=csrf)


def revoke_session(session: Any) -> None:
    """Sign out. Like an access change, it writes the person's security guard (fix ticket 04):
    the write waits for any command holding that guard's share lock until it commits, and a
    command whose snapshot is older than the sign-out fails to lock the rewritten guard and
    retries, so a sign-out can never land between a command's checks and its commit."""
    from django.db import transaction
    from django.db.models import F

    from accounts.goods_models import SecurityGuard, ServerSession

    human_id = getattr(session.user, "human_id", None)
    with transaction.atomic():
        if human_id is not None:
            SecurityGuard.objects.get_or_create(tenant_id=session.tenant_id, human_id=human_id)
            SecurityGuard.objects.filter(human_id=human_id).update(epoch=F("epoch"))
        ServerSession.objects.filter(pk=session.pk, revoked_at__isnull=True).update(
            revoked_at=database_now(), step_up_at=None
        )


def grant_step_up(session: Any, password: str) -> datetime:
    from accounts.goods_models import ServerSession

    now = database_now()
    user = session.user
    email = user.email or ""
    check_lock(session.tenant_id, email, now)
    if not user.check_password(password):
        record_failure(session.tenant_id, email, now)
        raise Refusal("INVALID_CREDENTIALS", "That password is not correct.")
    clear_failures(session.tenant_id, email)
    ServerSession.objects.filter(pk=session.pk).update(step_up_at=now)
    session.step_up_at = now
    return now + STEP_UP_LIFE


def person_retired(human_id: uuid.UUID | None, now: datetime) -> bool:
    """A retired staff member cannot sign in or keep a session from their retirement time."""
    from accounts.goods_models import Staff

    if human_id is None:
        return False
    retired_at = (
        Staff.objects.filter(human_id=human_id).values_list("retired_at", flat=True).first()
    )
    return retired_at is not None and retired_at <= now


def step_up_valid_until(session: Any, now: datetime | None = None) -> datetime | None:
    if session is None or session.step_up_at is None:
        return None
    until: datetime = session.step_up_at + STEP_UP_LIFE
    return until if until > (now or database_now()) else None


def bump_security_epoch(human_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Invalidate every session and step-up of one person (access change, revocation)."""
    from django.db.models import F

    from accounts.goods_models import SecurityGuard, ServerSession

    SecurityGuard.objects.get_or_create(tenant_id=tenant_id, human_id=human_id)
    SecurityGuard.objects.filter(human_id=human_id).update(epoch=F("epoch") + 1)
    ServerSession.objects.filter(user__human_id=human_id, revoked_at__isnull=True).update(
        step_up_at=None
    )


def csrf_matches(request_token: str | None, cookie_token: str | None, session: Any = None) -> bool:
    if not request_token or not cookie_token:
        return False
    if not hmac.compare_digest(request_token, cookie_token):
        return False
    if session is not None:
        return hmac.compare_digest(token_hash(request_token), session.csrf_hash)
    return True


def cookie_kwargs(*, httponly: bool) -> dict[str, Any]:
    return {
        "httponly": httponly,
        "secure": bool(getattr(settings, "KDPS_COOKIE_SECURE", True)),
        "samesite": "Lax",
        "path": "/",
    }
