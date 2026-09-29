"""Fail-closed data scoping (ADR-0003), kept out of any view module so apps don't
peer-import each other. A user is scoped all / entity / region / store_group /
store / brand over LegalEntity → GSTIN → Store; an unrecognised or empty scope
resolves to *no rows*, never to everything.

Two layers, deliberately separate (issue #88):

  · **scope** — `visible_store_ids` / `visible_brand_names`: everything this
    person may ever see. Admin-set data; the permission boundary.
  · **context** — `active_store_ids` / `active_brand_names`: the one unit (or
    brand) they picked in the top-bar switcher for *this* request. Always the
    intersection with scope, so the header can only ever narrow.

Six gates sit on top, and which one you want depends on two questions — is the
caller reading or acting, and how do the rows say whose they are?

  · `scope_by_store` — reading rows with no brand (documents), and
    `scope_by_store_predicate` for rows whose store is not one plain field.
  · `scope_by_store_and_brand` — reading rows that carry one (stock): everyone
    is narrowed by both.
  · `scope_by_store_or_brand` — reading rows where either axis alone is the whole
    boundary (approvals): a brand-scoped caller is narrowed by brand, everyone
    else by store.
  · `scope_by_entitlement` / `scope_by_entitlement_or_brand` — fetching a row in
    order to *act* on it.
  · `actionable_store_ids` — the "may I operate at this store?" check.

The two acting gates ignore the switcher on purpose: it narrows what you read,
never what you may do, so you may still post to any store you are entitled to
whichever one happens to be on screen.

A brand-scoped user (a brand manager) is the case that makes the split matter.
Stores are the wrong question for them, so `visible_store_ids` answers None —
and None must never be read as "show everything". Every gate above turns that
into *no rows* unless a brand is present to do the narrowing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from masters.models import Brand, Store
from masters.unit_context import active_brand_name, active_unit_code

# Brand-scoped users (a brand manager) are not tied to stores — their scope is a
# set of brands, network-wide. Kept as a literal so `masters` never imports
# `accounts` (the FK already points the other way).
BRAND_SCOPE = "brand"


def is_brand_scoped(user: Any) -> bool:
    """Is this person bounded by brands rather than by stores?"""
    if getattr(user, "is_superuser", False):
        return False
    return bool(getattr(user, "scope_type", None) == BRAND_SCOPE)


def visible_store_ids(user: Any) -> list[int] | None:
    """Store ids the user may see, or None meaning 'all stores' (no restriction).

    Callers must not read None as "safe to show everything" for a brand-scoped
    user — their boundary simply isn't a store list. Go through the gates below,
    which fail such a user closed unless the rows carry a brand to narrow by.
    """
    if getattr(user, "is_superuser", False):
        return None
    scope = getattr(user, "scope_type", None)
    if scope == "all":
        return None
    if scope == BRAND_SCOPE:
        # Not "everything" — "stores are the wrong question". The gates below
        # turn this into no-rows wherever a brand cannot do the narrowing.
        return None
    if scope == "entity" and getattr(user, "entity_id", None):
        return list(
            Store.objects.filter(gstin__legal_entity_id=user.entity_id).values_list("id", flat=True)
        )
    # store / store_group / region → explicit membership; empty ⇒ sees nothing
    return list(user.stores.values_list("id", flat=True))


def actionable_store_ids(user: Any) -> list[int] | None:
    """Stores the user may *act* on — the gate for "may I receive/dispatch here?".

    Same as `visible_store_ids`, except a brand-scoped user resolves to *no*
    stores rather than to all of them. Acting on a store is a claim about a
    place; a boundary made of brands cannot support one.
    """
    if is_brand_scoped(user):
        return []
    return visible_store_ids(user)


def actionable_stores(user: Any) -> Any:
    """The active stores the user may act at, by code - ``actionable_store_ids``
    as rows. A brand-scoped person gets none."""
    ids = actionable_store_ids(user)
    rows = Store.objects.filter(is_active=True).order_by("code")
    return rows if ids is None else rows.filter(pk__in=ids)


def visible_brand_names(user: Any) -> list[str] | None:
    """Brand names the user is limited to, or None meaning 'every brand'."""
    if getattr(user, "is_superuser", False):
        return None
    if getattr(user, "scope_type", None) != BRAND_SCOPE:
        return None
    return list(user.brands.filter(is_active=True).values_list("name", flat=True))


def scoped_stores(user: Any) -> Any:
    """Every store the user may see — the switcher's list of units, and the
    store pickers'. Deliberately *not* narrowed by the active unit: you pick a
    unit out of this list, so narrowing it by the pick would collapse it."""
    qs = Store.objects.filter(is_active=True).select_related("gstin")
    ids = visible_store_ids(user)
    return qs if ids is None else qs.filter(id__in=ids)


def scoped_brands(user: Any) -> Any:
    """Every brand the user is assigned — the brand manager's filter list."""
    qs = Brand.objects.filter(is_active=True)
    names = visible_brand_names(user)
    return qs if names is None else qs.filter(name__in=names)


def active_store_ids(user: Any) -> list[int] | None:
    """The store ids this request may read: the caller's scope, narrowed to the
    unit chosen in the switcher. None means 'no restriction' (network view).

    Raises 403 rather than silently widening or silently emptying: a unit that
    does not exist, or one outside the caller's scope, is an answer they must
    never get.
    """
    visible = visible_store_ids(user)
    code = active_unit_code()
    if not code:
        return visible
    store_id = Store.objects.filter(code=code, is_active=True).values_list("id", flat=True).first()
    if store_id is None:
        raise PermissionDenied(f"Unknown business unit '{code}'.")
    if visible is not None and store_id not in visible:
        raise PermissionDenied("You may not work in this business unit.")
    return [store_id]


def active_brand_names(user: Any) -> list[str] | None:
    """Brand names this request may read: assigned brands, narrowed to the one
    chosen in the switcher. None means 'no brand restriction'."""
    allowed = visible_brand_names(user)
    chosen = active_brand_name()
    if not chosen:
        return allowed
    if not Brand.objects.filter(name__iexact=chosen, is_active=True).exists():
        raise PermissionDenied(f"Unknown brand '{chosen}'.")
    if allowed is not None and not any(name.lower() == chosen.lower() for name in allowed):
        raise PermissionDenied("You may not work in this brand.")
    return [chosen]


def scope_by_store(qs: Any, user: Any, field: str = "store_id") -> Any:
    """Restrict a queryset to the unit the user is acting in (fail-closed).

    The *reading* gate for rows with no brand of their own. A brand-scoped user
    gets nothing here: these rows carry nothing that could prove they are theirs,
    and "their scope isn't stores" must never be read as "so show them all
    stores". Rows that do carry a brand go through `scope_by_store_and_brand`.

    Use `scope_by_entitlement` wherever the row is fetched in order to act on it.
    """
    return scope_by_store_many(user, (qs, field))[0]


def scope_by_store_predicate(qs: Any, user: Any, predicate: Callable[[list[int]], Q]) -> Any:
    """`scope_by_store` for a row whose store is not one plain field.

    A store transfer has two of them — it belongs to the sender and to the
    receiver alike — so the *rule* it needs is this module's, but the *shape* of
    the match is the owning module's. This takes that shape as a callable; the
    rule stays here, in `_scoped` with every other spelling of it.

    `predicate` receives the store ids this request may read and returns the `Q`
    matching rows at them.
    """
    return _scoped(user, [(qs, predicate)])[0]


def scope_by_store_many(user: Any, *targets: tuple[Any, str]) -> list[Any]:
    """`scope_by_store` over several querysets, resolving the scope once.

    A screen built from two lists gates both against the same person in the same
    request, so the two answers cannot differ — but `active_store_ids` is not
    free (it reads the user's store membership and resolves the switcher's unit
    to a row), and calling it per queryset pays that twice for one answer.

    Each target is `(queryset, field)`; the results come back in the same order.
    """
    return _scoped(user, [(qs, _at_store_field(field)) for qs, field in targets])


def _at_store_field(field: str) -> Callable[[list[int]], Q]:
    """The ordinary match: the row's own store column is one of these ids."""
    return lambda ids: Q(**{f"{field}__in": ids})


def _scoped(user: Any, targets: list[tuple[Any, Callable[[list[int]], Q]]]) -> list[Any]:
    """The reading rule itself, and the only copy of it.

    Every reading gate above is this function with a different match shape, so
    the fail-closed branch a brand-scoped user takes is written once and cannot
    be fixed in one gate and forgotten in another. Three answers, in order:
    a brand-scoped caller gets nothing, an unrestricted one gets everything, and
    everyone else gets the rows their predicate matches.
    """
    if is_brand_scoped(user):
        return [qs.none() for qs, _ in targets]
    ids = active_store_ids(user)
    if ids is None:
        return [qs for qs, _ in targets]
    return [qs.filter(predicate(ids)) for qs, predicate in targets]


def scope_by_store_and_brand(
    qs: Any, user: Any, store_field: str = "store_id", brand_field: str = "brand"
) -> Any:
    """The reading gate for rows that carry a brand — stock and its projections.

    A brand-scoped user is narrowed by brand alone: their work genuinely spans
    every store, so the brand is the whole boundary. Everyone else is narrowed
    by the unit in the top bar, then optionally by a brand they chose as a
    filter.
    """
    if is_brand_scoped(user):
        return scope_by_brand(qs, user, brand_field)
    ids = active_store_ids(user)
    if ids is not None:
        qs = qs.filter(**{f"{store_field}__in": ids})
    return scope_by_brand(qs, user, brand_field)


def scope_by_store_or_brand(
    qs: Any, user: Any, store_field: str = "store_id", brand_field: str = "brand"
) -> Any:
    """The reading gate for rows that belong to a store *or* to a brand.

    Deliberately not `scope_by_store_and_brand`, which narrows everyone by both:
    these are rows where one axis or the other is the whole boundary. An approval
    is the case that made it necessary — a return to brand is a store's document
    and a brand manager's decision, and `scope_by_store` fails a brand-scoped
    caller closed, so before this the person the policy names as the approver
    could not see the thing they were meant to approve (#75).

    A brand-scoped caller is narrowed by their *entitled* brands alone — not by
    the switcher, which is why this reaches for `visible_brand_names` rather than
    `scope_by_brand`. A row carrying no brand is not theirs, which keeps the
    fail-closed reading: "stores are the wrong question" never becomes "so show
    them everything". Everyone else is narrowed by store, exactly as before.

    Nobody is narrowed by the top-bar brand here, and that is the point: an
    inbox that hid work behind the header would leave a brand manager able to
    decide a return they cannot see, because the acting gate below ignores the
    header too.
    """
    if is_brand_scoped(user):
        return scope_by_entitled_brands(qs, user, brand_field)
    return scope_by_store(qs, user, store_field)


def scope_by_entitled_brands(qs: Any, user: Any, brand_field: str = "brand") -> Any:
    """Rows whose brand is one this person is entitled to, switcher ignored.

    Public because the cross-store availability search (#175) needs this axis on
    its own: it suspends the *store* boundary by registered exception and must
    keep the brand one, and a brand manager entitled to two brands is answering
    a question about those brands wherever the pieces happen to sit.

    Three answers, and the middle one is why the None branch is spelled out:
    a caller who is not brand-scoped has no brand boundary and keeps every row,
    a brand-scoped one with no brands assigned gets none, and everyone else gets
    theirs. The two gates above only ever reach here down their brand-scoped
    branch, so they cannot tell "no boundary" from "an empty one" — a general
    helper must.
    """
    names = visible_brand_names(user)
    if names is None:
        return qs
    if not names:
        return qs.none()
    match = Q()
    for name in names:
        match |= Q(**{f"{brand_field}__iexact": name})
    return qs.exclude(**{brand_field: ""}).filter(match)


def scope_by_entitlement_or_brand(
    qs: Any, user: Any, store_field: str = "store_id", brand_field: str = "brand"
) -> Any:
    """`scope_by_store_or_brand` for a row fetched in order to *act* on it.

    Same split, and the same reason the acting gates ignore the switcher: what
    you are looking at must never decide what you may do. A brand manager
    entitled to two brands may decide either one's return whichever is on screen.
    """
    if is_brand_scoped(user):
        return scope_by_entitled_brands(qs, user, brand_field)
    return scope_by_entitlement(qs, user, store_field)


def scope_by_entitlement(qs: Any, user: Any, field: str = "store_id") -> Any:
    """Restrict a queryset to every store the user is entitled to, ignoring the
    switcher — the *permission* boundary (fail-closed).

    Deliberately not `scope_by_store`. The top bar chooses what you are looking
    at; it must never decide what you are allowed to do. A person entitled to
    Deoghar and Patna who happens to have Deoghar on screen may still approve
    Patna's document — narrowing that to the active unit would let the act of
    looking elsewhere silently strip rights the admin granted.

    A brand-scoped user acts on nothing here, for the same reason as above: an
    act needs a row provably theirs, and a store list cannot prove it.
    """
    if is_brand_scoped(user):
        return qs.none()
    ids = visible_store_ids(user)
    return qs if ids is None else qs.filter(**{f"{field}__in": ids})


def scope_by_brand(qs: Any, user: Any, field: str = "brand") -> Any:
    """Restrict a queryset to the brands the user is acting in (fail-closed).

    Stock rows carry the brand as the name printed on the PT (`PETER ENGLAND`),
    not a foreign key, so the match is case-insensitive against the master name.
    A brand-scoped user with no brands assigned sees nothing.
    """
    names = active_brand_names(user)
    if names is None:
        return qs
    if not names:
        return qs.none()
    match = Q()
    for name in names:
        match |= Q(**{f"{field}__iexact": name})
    return qs.filter(match)
