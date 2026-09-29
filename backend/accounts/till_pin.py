"""The manager's counter PIN: who may hold one, and how it is hashed (#182).

A store manager authorises an over-cap discount, or an unrecognised credit note,
by typing a PIN at the till - and the till is offline while they do it. So the
PIN is verified **on the device**, against a hash that came down in the dataset
(`sell.services.dataset._managers`), and every property below follows from that
one sentence.

**It is hashed with PBKDF2-SHA256 explicitly, not with the project's default
hasher.** `PASSWORD_HASHERS` puts bcrypt first, which is the right choice for a
password checked on a server; it is the wrong choice for a secret verified in a
browser, because the Web Crypto API a browser gives us has PBKDF2 and does not
have bcrypt. Verifying a bcrypt hash offline would mean shipping a bcrypt
implementation to the shop floor, and a hash nothing can verify is not a
credential. `till/pin.ts` reads the iteration count out of the string, so raising
it here needs nothing on the device.

**Not everybody may hold one.** The hash leaves the building on a shop-floor
device, so the only people whose hashes are ever written are the people a counter
could actually be asked to trust: somebody whose boundary is stores at all, who
holds `sell >= approve` on the stored matrix, and who is not the break-glass
superuser. That is the same sentence the dataset's manager list is built from,
and it is written here once so the two cannot drift.

**Who sets one.** A manager sets or changes their own from Till & Sync, with
their own password. Admin may also *set* a manager's PIN, and it works at once
(store operations baseline B76, which departs from overall PRD §10.2 by Anand's
ruling), or *clear* one (`GoodsUserTillPinSetView`, `GoodsUserTillPinResetView`).
Either way every till picks the change up on its next sync. Nobody else may do
either, and nobody ever sees a PIN: only its hash is stored, and the audit
record says "set" / "not set".
"""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib.auth.hashers import PBKDF2PasswordHasher

from accounts.models import ScopeType
from accounts.permissions import user_can
from accounts.sections import CAP_APPROVE
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command

#: Scopes whose boundary genuinely *is* a set of stores. A network- or
#: entity-wide administrator whose matrix cell happens to say `sell: manage` is
#: not one of a counter's people, and shipping their hash to fifty tills would be
#: a worse answer than shipping nobody's.
STORE_BOUND_SCOPES = (ScopeType.STORE, ScopeType.STORE_GROUP, ScopeType.REGION)

#: Four to six digits, the length a person can type on a counter keypad with a
#: customer waiting. Short by design and therefore weak by design: the protection
#: is that a PIN only ever authorises an exception that is recorded, on a device
#: that already holds the store's whole price list.
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 6

_hasher = PBKDF2PasswordHasher()


def hash_till_pin(pin: str) -> str:
    """A PIN as the till will read it: `pbkdf2_sha256$<iterations>$<salt>$<b64>`."""
    return _hasher.encode(pin, _hasher.salt())


def verify_till_pin(pin: str, pin_hash: str) -> bool:
    """Does `pin` match `pin_hash`? For a check the server makes itself, where the
    request is online (the cash count, ticket 41). A blank hash matches nothing."""
    if not pin or not pin_hash:
        return False
    try:
        return bool(_hasher.verify(pin, pin_hash))
    except (ValueError, TypeError):
        return False


def pin_problem(pin: str) -> str:
    """Why this is not a PIN, in a sentence for the person typing it - or ""."""
    if not (pin.isascii() and pin.isdigit()):
        return "A counter PIN is digits only - it is typed at a till, often on a keypad."
    if not PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH:
        return f"A counter PIN is {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} digits."
    if len(set(pin)) == 1:
        return "That PIN is the same digit repeated. Anybody watching would have it."
    return ""


def may_reset_till_pin(user: Any) -> bool:
    """May this login set or clear somebody else's PIN? Admin only (ticket 06,
    B76): the it_admin role code, or break-glass. The views also ask for
    ``access.manage``, the login in scope and a fresh password."""
    from accounts.role_lists import TILL_PIN_RESETTERS

    if getattr(user, "is_superuser", False):
        return True
    return getattr(getattr(user, "role", None), "code", "") in TILL_PIN_RESETTERS


def may_hold_till_pin(user: Any) -> bool:
    """Is this somebody a counter could be asked to trust? See the module docstring."""
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_active
        and not user.is_superuser
        and user.scope_type in STORE_BOUND_SCOPES
        and user_can(user, "sell", CAP_APPROVE)
    )


# -- changing a PIN (store operations ticket 06) -----------------------------------
#
# Every change is one command, so its audit record says who changed whose PIN and
# when, with "set"/"not set" before and after - never the PIN or its hash.

SET_ACTION = "accounts.till_pin.set"
RESET_ACTION = "accounts.till_pin.reset"
ADMIN_SET_ACTION = "accounts.till_pin.admin_set"


def _state(pin_hash: str) -> str:
    return "set" if pin_hash else "not set"


def write_pin(run: CommandRun, user_pk: int, new_hash: str, *, by: str) -> None:
    """Inside a command: replace (or, with "", clear) a login's PIN hash, audited."""
    from accounts.models import User

    row = User.objects.select_for_update().get(pk=user_pk)
    run.audit_subject_key = f"user:{user_pk}"
    run.audit_before = {"counter_pin": _state(row.till_pin_hash)}
    row.till_pin_hash = new_hash
    row.save(update_fields=["till_pin_hash"])
    run.audit_after = {"counter_pin": _state(new_hash), "changed_by": by}


def set_own_pin(user: Any, pin: str, session: Any = None) -> None:
    """A manager sets or changes their own PIN. The caller has checked the rest.

    A login that is not a person cannot sign in (goods-v1 sessions refuse it),
    so the no-person branch is only ever a fixture login from before persons
    existed (`tests/_sell.build_manager`); it writes the hash with nothing to sign
    an audit record with.
    """
    new_hash = hash_till_pin(pin)
    human_id = getattr(user, "human_id", None)
    tenant_id = getattr(user, "tenant_id", None)
    if human_id is None or tenant_id is None:
        user.till_pin_hash = new_hash
        user.save(update_fields=["till_pin_hash"])
        return
    principal = Principal(
        tenant_id=tenant_id,
        human_id=human_id,
        user_id=user.pk,
        session_id=getattr(session, "pk", None),
        step_up_at=getattr(session, "step_up_at", None),
    )

    def handler(run: CommandRun) -> CommandResult:
        write_pin(run, user.pk, new_hash, by="self")
        return CommandResult(resource_type="user", resource_id=str(user.pk))

    execute_command(
        principal,
        CommandSpec(action=SET_ACTION, command_id=uuid.uuid4(), business_input={"user": user.pk}),
        handler,
    )
