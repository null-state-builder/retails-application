"""The number a store's till checks its copy of the shelf against (OPS-07, PRD §8).

One counting number per store. Every goods stock posting made at that store
raises it, inside the posting's own transaction; the dataset hands the current
number to the till and sends the whole stock section whenever the till's number
is not it.

The whole point is that raising it is *not* a decision anybody makes. A service
that had to remember to call `bump` would work until the day somebody added a
new way for stock to move and forgot, and the failure would be a counter selling
a piece that is not there - silently, until a customer is standing at it. So the
raise hangs off `stockledger.goods_engine.post`, which is the one door every
goods stock posting goes through, and it reads the sites off the posting's own
quantity legs rather than being told which store to raise.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models import F

from sell.models import WorkingSetVersion

#: The number a store is on before anything has ever moved there. A till holding
#: nothing is on nothing, so any real version differs from it and forces a full send.
FIRST_VERSION = 1


def current_version(store_id: int) -> int:
    """This store's number now. A store nothing has ever moved at is on the first."""
    found = (
        WorkingSetVersion.objects.filter(store_id=store_id)
        .values_list("version", flat=True)
        .first()
    )
    return int(found) if found is not None else FIRST_VERSION


def bump(site_ids: Iterable[int]) -> None:
    """Raise the number for every named site, creating the row if it is the first move.

    `F("version") + 1` rather than a read and a write: two commands moving stock at
    one store at the same time must both be counted, and a read-modify-write would
    let the second overwrite the first with the same number. A till would then be
    told nothing had changed since a posting that had.
    """
    for site_id in sorted({int(s) for s in site_ids}):
        updated = WorkingSetVersion.objects.filter(store_id=site_id).update(
            version=F("version") + 1
        )
        if not updated:
            # First movement at this store. `get_or_create` rather than `create`,
            # because a concurrent first posting is a race this must not lose a
            # transaction over - the row's value is then raised by the update above
            # on whichever of the two runs second.
            _, created = WorkingSetVersion.objects.get_or_create(
                store_id=site_id, defaults={"version": FIRST_VERSION + 1}
            )
            if not created:
                WorkingSetVersion.objects.filter(store_id=site_id).update(version=F("version") + 1)


def bump_for_posting(quantity_pairs: Iterable[Any]) -> None:
    """Raise the number for every store a posting's quantity legs touch.

    Both ends of every pair are read. A piece leaving a store and a piece arriving
    at one are equally reasons for that store's till to stop trusting its copy, and
    a dispatch names the source on one side only.
    """
    sites: set[int] = set()
    for pair in quantity_pairs:
        for side in (pair.source, pair.destination):
            site_id = side.get("site_id") if isinstance(side, dict) else None
            if site_id is not None:
                sites.add(int(site_id))
    if sites:
        bump(sites)
