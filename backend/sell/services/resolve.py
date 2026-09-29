"""Turning what the till scanned into what the books know about it.

Three lookups the accept pipeline needs and nothing else: which cohort a barcode
means, which GST slab a bill's date falls in, and whether the person whose id
came back on an override is really a manager here.

The cohort one is the interesting one. A barcode is the SKU (a piece, at a size
and a colour); the *cohort* is that barcode in a season, and it is the cohort
that carries the cost of record. Bill a re-ordered style whose seasons the store
holds side by side and something has to choose which one left the shelf. The till
sends the season when it knows it; when it does not, the answer is the oldest
live season with stock here (A2) - oldest, because that is what a store actually
sells first and what keeps the aging honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from accounts.models import User
from accounts.permissions import user_can
from accounts.sections import CAP_APPROVE
from accounts.till_pin import may_hold_till_pin
from masters.models import Cohort, GstSlab, Season, Store
from masters.scoping import actionable_store_ids
from sell.pricing import Slab
from stockledger.models import MERCH_DIM_FIELDS, StockOnHand

#: Where a bill's tax lands when nobody has told the system what apparel GST is.
#: Every path that uses it says so on the bill's flag rather than pretending, and
#: this only ever *compares* - it never re-prices what the customer already paid.
_FALLBACK_SLAB = Slab(
    threshold_paise=250000,
    rate_below=Decimal("5"),
    rate_above=Decimal("18"),
    effective_from=date(2025, 9, 22),
)


@dataclass(frozen=True)
class ResolvedPiece:
    """A barcode, placed in a season and priced from the books."""

    cohort: Cohort | None
    season: str
    unit_cost_paise: int
    dims: dict[str, str]

    @property
    def is_known(self) -> bool:
        return self.cohort is not None


#: The merchandising dims a bill line snapshots, in the ledger's own vocabulary
#: minus `season`, which a sale carries separately because resolving *which*
#: season is the whole question this module answers.
LINE_DIM_FIELDS = tuple(f for f in MERCH_DIM_FIELDS if f != "season")


def line_dims(source: Any) -> dict[str, str]:
    """The dims a bill line snapshots off anything that carries them (Rule 3).

    One bundle, taken from `stockledger`'s own list rather than typed out, so a
    dim added there arrives on the bill instead of being silently dropped - the
    rule its docstring states as "copy it with `merch_dims`, never by hand-listing
    the fields".
    """
    return {f: getattr(source, f, "") or "" for f in LINE_DIM_FIELDS}


def _dims_from_cohort(cohort: Cohort) -> dict[str, str]:
    return {**line_dims(cohort.sku), "season": cohort.season or ""}


def _season_rank(candidates: list[Cohort]) -> dict[str, tuple[int, int]]:
    """`(is_closed, sort_order)` per season name - lower sorts older and liver.

    Seasons are named, not dated (`masters.Season` is explicit about that), so
    "oldest" means the master's own ordering. A season the cohort names but the
    master has never heard of sorts last rather than first: it is more likely a
    typo on a PT than the oldest thing in the shop.
    """
    names = {c.season for c in candidates if c.season}
    rows = Season.objects.filter(code__in=names) | Season.objects.filter(name__in=names)
    ranking: dict[str, tuple[int, int]] = {}
    for season in rows.distinct():
        rank = (1 if season.status == Season.Status.CLOSED else 0, season.sort_order)
        for key in (season.code, season.name):
            if key in names:
                ranking[key] = rank
    return ranking


def resolve_piece(store: Store, barcode: str, season: str) -> ResolvedPiece:
    """The cohort a scan means, and what it cost.

    An exact `(barcode, season)` wins outright - the till knew, and second-guessing
    it would be the system overruling the scan. Otherwise the candidates are
    ranked by what a store actually sells first: stock on this shelf, then a live
    season over a closed one, then the master's own oldest-first order.

    No cohort at all is not an error here. It means the piece is being sold before
    its paperwork arrived, and the caller turns that into a deferred line rather
    than a refused customer (grill Q5).
    """
    if not barcode:
        return ResolvedPiece(cohort=None, season=season, unit_cost_paise=0, dims={})
    if season:
        exact = Cohort.objects.select_related("sku").filter(barcode=barcode, season=season).first()
        if exact is not None:
            return ResolvedPiece(
                cohort=exact,
                season=exact.season,
                unit_cost_paise=int(exact.unit_cost_paise or 0),
                dims=_dims_from_cohort(exact),
            )
    candidates = list(Cohort.objects.select_related("sku").filter(barcode=barcode))
    if not candidates:
        return ResolvedPiece(cohort=None, season=season, unit_cost_paise=0, dims={})
    on_shelf = (
        StockOnHand.objects.filter(store=store, sku_code=barcode, net_qty__gt=0)
        .values_list("season", flat=True)
        .first()
    )
    ranking = _season_rank(candidates)
    chosen = min(
        candidates,
        key=lambda c: (
            0 if (on_shelf and c.season == on_shelf) else 1,
            ranking.get(c.season, (1, 10**6)),
            c.id,
        ),
    )
    return ResolvedPiece(
        cohort=chosen,
        season=chosen.season,
        unit_cost_paise=int(chosen.unit_cost_paise or 0),
        dims=_dims_from_cohort(chosen),
    )


def slab_for(hsn: str, when: date) -> Slab:
    """The apparel GST slab in force on `when`, preferring the row for this HSN.

    Date-effective by construction (Rule 12): the bill is compared against the
    slab that was live on the day it was billed, never against today's.
    """
    rows = GstSlab.objects.filter(effective_from__lte=when).order_by("-effective_from")
    prefix_match = next(
        (r for r in rows if r.hsn_prefix and hsn and hsn.startswith(r.hsn_prefix)), None
    )
    row = prefix_match or next((r for r in rows if not r.hsn_prefix), None) or rows.first()
    if row is None:
        return _FALLBACK_SLAB
    return Slab(
        threshold_paise=int(row.threshold_paise or 0),
        rate_below=row.rate_below,
        rate_above=row.rate_above,
        effective_from=row.effective_from,
    )


def manager_for_override(user_id: Any, store: Store, *, own_pin_rules: bool = False) -> User | None:
    """The manager behind an override, or `None` if that id does not name one.

    Two questions, and both have to answer yes: does this person hold the second
    eye on selling (`sell >= approve`), and do they hold it *here*. A manager from
    another store is not a manager at this counter - the override is evidence
    recorded on the bill and it has to name somebody who could actually have been
    standing at it.

    With `own_pin_rules` (store operations ticket 06, switched on per store) the
    answer is narrowed to exactly the people the till could have checked a PIN
    for: somebody who may hold a counter PIN at all (`may_hold_till_pin`: stores
    are their boundary, not the break-glass account) and who is assigned to this
    store by name - the same sentence the dataset's manager list is built from
    (`sell.services.dataset._managers`). Whether they still *have* a PIN is not
    asked: a PIN reset after an offline approval must not refuse a printed bill.
    """
    if not user_id:
        return None
    user: User | None = (
        User.objects.filter(pk=user_id, is_active=True).select_related("role").first()
    )
    if user is None or not user_can(user, "sell", CAP_APPROVE):
        return None
    allowed = actionable_store_ids(user)
    if allowed is not None and store.id not in allowed:
        return None
    if own_pin_rules and not (may_hold_till_pin(user) and user.stores.filter(pk=store.pk).exists()):
        return None
    return user
