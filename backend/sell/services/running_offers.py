"""Running Offers: which pieces on a site's shelf each live rule reaches (OPS-10, PRD §11).

A store manager's morning question is not "what rules exist" - the Promotions
list already answers that - it is **"which of my pieces does this offer take money
off, today?"**. Answering it needs two things at once: the site's working set, and
the rulebook's own applicability test.

Both are borrowed, neither is restated. The pieces are
`sell.services.dataset.working_set_items`, the same rows the counter prices with.
The test is `offers.resolution.covers`, the same function the engine asks before
it makes a proposal - so an item listed here is by construction an item a bill
would discount, and an item missing from the list is one a bill would not. PRD
§11's "one engine answers both" is that identity, not a promise to keep two
spellings in step.

It lives in `sell` rather than in `offers` for the reason
`sell/discount_views.py` gives: the rulebook is a leaf below the counter and may
not import it (the import contract's one-way street), and the pieces are the
counter's. The URL is the rulebook's, because that is the screen asking.

**Nothing here prices anything.** `covers` is a scope question - dates, brand,
item scope, the no-discount flag - and says nothing about whether a bill reaches
a ladder's bottom step. A card that showed a spend-threshold offer's pieces as
"discounted" would be lying to a counter; the card names the threshold separately
and this module lists the pieces the threshold would be measured over.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from masters.models import Store
from offers.models import Offer
from offers.resolution import CartLine, covers
from offers.serializers import reward_phrase, trigger_phrase
from sell.services.dataset import working_set_items

#: How many pieces one offer's card carries before it stops being a list a person
#: reads and becomes a file they download. A storewide rule at a real store covers
#: the whole shelf - twenty thousand rows - and sending them would make one card
#: heavier than the entire rest of the screen. The count is always the true one;
#: only the rows are cut, and `items_truncated` says so out loud rather than
#: letting a shortened list read as a complete one.
ITEM_CAP = 500


def _line(line_no: int, row: Mapping[str, Any]) -> CartLine:
    """One working-set row as the engine needs to see it.

    A quantity of one, because applicability is a question about the piece and
    not about how many of it somebody is buying. An unpriced piece keeps its
    honest nought: `covers` refuses it, exactly as a bill would.
    """
    return CartLine(
        line_no=line_no,
        brand=str(row.get("brand") or ""),
        item=str(row.get("item") or ""),
        design=str(row.get("design") or ""),
        size=str(row.get("size") or ""),
        color=str(row.get("color") or ""),
        barcode=str(row.get("barcode") or ""),
        season=str(row.get("season") or ""),
        qty=1,
        mrp_paise=int(row.get("mrp_paise") or 0),
        no_discount=bool(row.get("no_discount")),
    )


def _description(row: Mapping[str, Any]) -> str:
    """What a person standing at the rack would call this piece.

    Brand, style and kind, then the size and colour that tell two of them apart.
    A row that names none of those falls back to its barcode rather than to an
    empty cell: a blank line in a list of affected pieces is unreadable.
    """
    head = " ".join(
        part
        for part in (str(row.get(key) or "").strip() for key in ("brand", "design", "item"))
        if part
    )
    tail = [part for part in (str(row.get(key) or "").strip() for key in ("size", "color")) if part]
    return " · ".join([head or str(row.get("barcode") or "")] + tail)


def _source(offer: Offer) -> str:
    """Who this rule belongs to: `company` for a rule of ours, `brand:<code>` otherwise.

    An empty brand on the row is the model's own way of saying "not one brand's"
    - a storewide or KDPS rule - so the two answers are read off that one field
    rather than off the layer, which asks a different question (how it stacks).
    """
    if offer.brand is None:
        return "company"
    return f"brand:{offer.brand.code}"


def running_offers(store: Store, day: date) -> list[dict[str, Any]]:
    """Every rule running at this site on this day, with the pieces it reaches.

    Live on the day *and* scoped to this site: `store_scope` is a resolved list
    of store codes (never a wildcard), so a site added after the offer was
    written is simply not in it, and that is the answer.
    """
    rows = working_set_items(store, day)
    lines = [_line(index, row) for index, row in enumerate(rows)]
    offers = (
        Offer.objects.live_on(day)
        .for_store(store.code)
        .select_related("brand")
        .order_by("priority", "id")
    )
    return [_card(offer, rows, lines, day) for offer in offers]


def _card(
    offer: Offer,
    rows: list[dict[str, Any]],
    lines: list[CartLine],
    day: date,
) -> dict[str, Any]:
    rule = offer.as_rule()
    reached = [rows[line.line_no] for line in lines if covers(rule, line, day)]
    asks = trigger_phrase(offer)
    return {
        "id": offer.id,
        "name": offer.name,
        "source": _source(offer),
        "brand": offer.brand_name,
        "layer": offer.layer,
        "starts_on": offer.starts_on.isoformat(),
        "ends_on": offer.ends_on.isoformat() if offer.ends_on else None,
        "combinable": offer.combinable,
        "priority": offer.priority,
        "reward": reward_phrase(offer),
        # Said in words rather than left empty, because "nothing" and "we did not
        # work it out" read the same as a blank on a card.
        "trigger": asks or "No condition",
        "items_count": len(reached),
        "items_truncated": len(reached) > ITEM_CAP,
        "items": [
            {
                "barcode": str(row.get("barcode") or ""),
                "description": _description(row),
                "season": str(row.get("season") or ""),
                "mrp_paise": int(row.get("mrp_paise") or 0),
            }
            for row in reached[:ITEM_CAP]
        ],
    }
