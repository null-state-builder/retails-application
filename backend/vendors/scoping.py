"""Read scope for Booking (issue #234) — one predicate, not four hand-copies.

A booking's list, its detail, the pending-bookings queue (`inbound`), and global
search (`search`) all answer the same question — "does this booking touch a
store I may see?" — and until now each answered it with its own copy of the same
`Q` (or, on the list and detail endpoints, no answer at all: any store login saw
every store's bookings). `outbound/scoping.py`'s docstring already names the
failure mode this leaves behind: "the #101 rewrite found the booking predicate
hand-copied three times". Booking itself was never given the module that would
have stopped a fourth.

Only the *shape* of the match lives here; the rule (honour the top-bar switcher,
fail a store-scoped caller closed) is `masters.scoping`'s, same as every other
reading gate — with one deliberate exception. Booking carries a `brand`, unlike
the storeless documents `scope_by_store_predicate` was built for (a transfer, a
stock request), so the generic brand-scoped branch (fail closed to nothing) is
the wrong answer here: it would take a brand manager's booking list from "every
booking, any store" to "none at all" in the same change that fixes the store
login's leak. The ratified scope rule (issue #234 triage, 7 Aug 2026) is explicit
that head-office and brand scopes **keep their existing, unrestricted read** —
only a store-scoped login is newly narrowed. A future ticket can give brand its
own by-brand rule (`#110` did exactly this for transfers); until then this is
the deliberate interim, not an oversight.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model, Q, QuerySet

from masters.scoping import is_brand_scoped, scope_by_store_predicate


def booking_at_stores(store_ids: list[int]) -> Q:
    """A booking belongs to **wherever its lines land**.

    A line names its own destination store; one left blank inherits the
    booking's default `destination_store` (a line bound for the warehouse/HO
    names neither, and matches nothing here — correct, since no store scope
    could ever include it).
    """
    return Q(lines__store_id__in=store_ids) | Q(
        lines__store__isnull=True, destination_store_id__in=store_ids
    )


def scope_bookings[M: Model](qs: QuerySet[M], user: Any) -> QuerySet[M]:
    """Restrict a booking queryset to the caller's own stores.

    A brand-scoped caller is left unrestricted (see module docstring — Booking's
    own carve-out, not the general ADR-0003 default). Everyone else is narrowed
    to bookings with a line landing at a store in their scope, honouring the
    top-bar switcher, fail-closed. A join across `lines` can repeat a booking
    once per matching line, so this is the one place the predicate is applied
    *and* de-duplicated — every call site gets the same answer without having to
    remember the `.distinct()` too.
    """
    if is_brand_scoped(user):
        return qs
    scoped: QuerySet[M] = scope_by_store_predicate(qs, user, booking_at_stores)
    return scoped.distinct()
