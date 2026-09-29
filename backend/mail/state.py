"""Carrying identity across the Google round trip, safely.

The OAuth callback arrives as a plain browser redirect from accounts.google.com.
It has no Authorization header, so at the moment Google hands us an
authorization code the server has no idea who is holding the browser.

**The attack this file exists to stop.** Call it account fixation. An attacker
presses Connect on their own KDPS login, keeps the resulting Google URL, and
sends it to a colleague. The colleague signs in at Google - their own mailbox,
their own consent - and the code comes back carrying the attacker's ``state``.
If ``state`` is all the server has, the colleague's mailbox is now attached to
the attacker's login, and the attacker reads their mail from inside KDPS.

Signing ``state`` does not stop this. A signed user id is still a value the
attacker legitimately owns and can hand to anybody, as many times as they like.
It proves *who started* the flow, which is exactly the wrong half.

**So the flow is finished by an authenticated request, not by the redirect.**
Three steps, and the mailbox attaches only at the third:

1. *Connect* mints a random ``state`` in a row naming the person who pressed it.
2. *Callback* trades that ``state`` for a one-time ``handoff`` and parks the
   Google code, encrypted. It attaches nothing and tells the browser nothing
   except the handoff.
3. *Complete* is an ordinary authenticated POST from the PWA, which carries the
   caller's own JWT. The row must belong to *that* caller or the code is
   discarded.

Now the colleague in the story above finishes step 3 as themselves, the row says
the attacker, the two disagree, and nothing attaches. The mailbox can only ever
land on the person who both started the flow and was signed in when it finished.

Everything here is single-use and short-lived: a state that has been back once
cannot come back again, a handoff is consumed under a row lock, and anything
older than :data:`MAX_AGE` is refused and swept.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from . import crypto
from .models import MailOAuthFlow

#: A consent flow is a person clicking through two Google screens. Ten minutes is
#: generous for that and short enough that a code or a handoff left in a browser
#: history is worthless by the time anybody finds it.
MAX_AGE = timedelta(minutes=10)


def _cutoff() -> datetime:
    return timezone.now() - MAX_AGE


def _secret() -> str:
    """256 bits from the OS. These are guess-proof by size, not by signature -
    which is why none of them needs to be, or is, a signed user id."""
    return secrets.token_urlsafe(32)


def start(user: Any) -> str:
    """Begin a flow for this person and return the ``state`` to send to Google."""
    prune()
    return MailOAuthFlow.objects.create(user=user, state=_secret()).state


def claim(state: str, code: str) -> str | None:
    """Google came back. Park the code and return the one-time handoff, or
    ``None`` if this state is unknown, stale, or has already been claimed.

    One ``UPDATE`` with the whole test in its ``WHERE``, so two callbacks racing
    the same state cannot both succeed - the second matches nothing because the
    first has already filled ``handoff``.
    """
    if not state or not code:
        return None
    handoff = _secret()
    claimed = MailOAuthFlow.objects.filter(
        state=state, handoff="", created_at__gte=_cutoff()
    ).update(handoff=handoff, code_enc=crypto.encrypt(code))
    return handoff if claimed == 1 else None


def finish(handoff: str, user: Any) -> str | None:
    """The authorization code for this handoff, if it belongs to ``user``.

    ``user`` is the authenticated caller, and matching it against the row is the
    whole defence described at the top of this file - not a tidiness check.

    Consumed either way: the row is deleted on the way out, under a lock, so a
    handoff replayed a second later finds nothing.
    """
    if not handoff:
        return None
    with transaction.atomic():
        flow = (
            MailOAuthFlow.objects.select_for_update()
            .filter(handoff=handoff, user=user, created_at__gte=_cutoff())
            .first()
        )
        if flow is None:
            return None
        code = crypto.decrypt(flow.code_enc)
        flow.delete()
    return code or None


def abandon(state: str) -> None:
    """Drop a flow that will never be completed - a refused consent, a callback
    with no code. Leaving it would keep a usable state alive for ten minutes."""
    if state:
        MailOAuthFlow.objects.filter(state=state).delete()


def prune() -> None:
    """Sweep expired flows. Called on the way in rather than by a cron: the only
    thing that creates these rows is somebody pressing Connect, so that is also
    the only moment there is anything new to sweep."""
    MailOAuthFlow.objects.filter(created_at__lt=_cutoff()).delete()
