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
device, so it belongs only to a Store Person with a selected-site assignment
covering every brand at that site and ``sell >= approve`` in that role policy.
The dataset and server-side approval checks use this same rule at the exact site.

**Who sets one.** A selected-store Store Person with operating authority may
set or change their own personal credential from Till & Sync after password
confirmation. This does not grant exception approval: those consumers retain
the separate selected-store approval eligibility above. A tenant Admin assignment may also *set* an eligible approval manager's PIN, and it works at once
(store operations baseline B76, which departs from overall PRD §10.2 by Anand's
ruling), or *clear* one (`GoodsUserTillPinSetView`, `GoodsUserTillPinResetView`).
Either way every till picks the change up on its next sync. Nobody else may do
either, and nobody ever sees a PIN: only its hash is stored, and the audit
record says "set" / "not set".
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from django.contrib.auth.hashers import PBKDF2PasswordHasher

from accounts.sections import CAP_APPROVE, CAP_OPERATE
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command

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
    """Admin's current assignment must itself confer access management."""
    from accounts.principal import access_for_user

    return _active_person(user) and access_for_user(user).can("till.pin.admin")


def _active_person(user: Any) -> bool:
    from core.tenancy import require_tenant_id

    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "human_id", None)
        and getattr(user, "tenant_id", None) == require_tenant_id()
    )


def may_hold_till_pin(user: Any, *, site_id: int | None = None) -> bool:
    """A store manager may approve only at a site in one qualifying assignment.

    A PIN is placed on a till holding *all* brands at that store. A brand-limited
    assignment or a network-wide assignment cannot supply that credential.
    With no ``site_id`` this answers whether the person may set a PIN at any
    selected site; the dataset and approval checks pass their exact store.
    """
    if not _active_person(user):
        return False
    from accounts.principal import access_for_user

    access = access_for_user(user)
    return any(
        row.role_code == "store_person"
        and not row.all_sites
        and row.all_brands
        and (site_id is None or site_id in row.site_ids)
        for row in access.section_grants("sell", CAP_APPROVE)
    )


def _personal_pin_sites(access: Any) -> set[int]:
    """Credential setup needs one selected-store operating assignment.

    Holding a credential is separate from authority to decide an exception.
    This helper never contributes to the approval manager list or a business
    action. A network or brand-limited assignment cannot supply its scope.
    """
    return {
        site_id
        for row in access.section_grants("sell", CAP_OPERATE)
        if row.role_code == "store_person" and not row.all_sites and row.all_brands
        for site_id in row.site_ids
        if access.grant_covers(row, site_id, None)
    }


def may_set_personal_till_pin(user: Any, *, site_id: int | None = None) -> bool:
    if not _active_person(user):
        return False
    from accounts.principal import access_for_user

    sites = _personal_pin_sites(access_for_user(user))
    return bool(sites) if site_id is None else site_id in sites


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
    """Set only this authenticated person's credential, never their authority.

    The live session, selected-store operating assignment and password step-up
    are rechecked by the command boundary. Every change leaves an audit record.
    """
    from accounts.principal import AccessContext, effective_grants
    from core.refusals import Refusal

    new_hash = hash_till_pin(pin)
    human_id = getattr(user, "human_id", None)
    tenant_id = getattr(user, "tenant_id", None)
    if human_id is None or tenant_id is None or session is None:
        raise Refusal("AUTH_REQUIRED", "A live session is required to change a PIN.")
    access = AccessContext(user=user, human_id=human_id, tenant_id=tenant_id,
                           session=session, grants=effective_grants(human_id))
    eligible_sites = _personal_pin_sites(access)
    if not eligible_sites:
        raise Refusal("ACTION_DENIED", "A personal counter PIN requires a selected-store operating assignment.")
    for site_id in eligible_sites:
        if not access.grants_with_roles("section.sell.operate", [(site_id, None)], ["store_person"]):
            raise Refusal("ACTION_DENIED", "Your store operating authority changed.")
    access.require_step_up()

    def guard(run: CommandRun, final: bool) -> None:
        access.revalidate(run, final)
        if not _personal_pin_sites(access):
            raise Refusal("ACTION_DENIED", "Your selected-store PIN eligibility changed.")

    def handler(run: CommandRun) -> CommandResult:
        write_pin(run, user.pk, new_hash, by="self")
        return CommandResult(resource_type="user", resource_id=str(user.pk))

    execute_command(
        replace(access.principal(), guard=guard),
        CommandSpec(action=SET_ACTION, command_id=uuid.uuid4(), business_input={"user": user.pk}),
        handler,
    )
