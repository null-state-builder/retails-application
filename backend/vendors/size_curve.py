"""Size curve: a style total booked is split into sizes (store operations ticket 40, ST-BUY-2).

Each brand and category at each store has a size split, taken from the **same
season last year**: SS26 learns from SS25, AW25 from AW24. The split is the
store's own sales of that brand and category in that season, by size, net of
pieces given back inside a bill (exchange legs). A booking line entered as a
style total gets its sizes filled from it; the buyer can change them.

* **Season** is the season the sold piece belongs to, as its bill line names it
  (the season's code, or its name), never the date it sold (B160).
* **Category** is the item's ITEM value, the word the sales report and ticket
  32's broken-size alerts already group by. A booking line carries no category,
  so the buyer picks it when filling (B161).
* Where there is no history, there is no curve and nothing is guessed: the
  answer says why (B162).

The rule (``reference_season_code``, ``size_sort_key``, ``allocate``) is pure;
``store_curves`` reads the history. Filling is ``fill``, the one write, run by
``vendors.goods_size_curve_views`` as an audited command.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q, Sum

from core.documents import DocStatus
from masters.models import Brand, Season, Store

#: A season code that says its kind and year: SS26, AW25.
SEASON_CODE = re.compile(r"^(SS|AW)(\d{2})$", re.IGNORECASE)
#: Letter sizes, smallest first. Two spellings of one size share a place.
LETTER_SIZES: dict[str, int] = {
    "XXXS": 0,
    "3XS": 0,
    "XXS": 1,
    "2XS": 1,
    "XS": 2,
    "S": 3,
    "M": 4,
    "L": 5,
    "XL": 6,
    "XXL": 7,
    "2XL": 7,
    "XXXL": 8,
    "3XL": 8,
    "XXXXL": 9,
    "4XL": 9,
    "5XL": 10,
}
#: Why a store, brand and season have no curve at all.
NO_LAST_YEAR = "no_last_year"
NO_HISTORY = "no_history"


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def reference_season_code(code: str) -> str | None:
    """The same season last year: SS26 -> SS25, AW25 -> AW24.

    None when the code does not say its kind and year (a season named some other
    way, or the unknown historical season): nothing is guessed from its name."""
    found = SEASON_CODE.match(str(code).strip())
    if found is None:
        return None
    kind, year = found.group(1).upper(), int(found.group(2))
    if year == 0:
        return None
    return f"{kind}{year - 1:02d}"


def size_key(size: str) -> str:
    """A size as compared: its words, whatever their case or spacing."""
    return " ".join(str(size).split()).upper()


def size_sort_key(size: str) -> tuple[int, float, str]:
    """Numbers first (smallest first), then letter sizes (XS to 5XL), then the rest."""
    key = size_key(size)
    try:
        return (0, float(key), key)
    except ValueError:
        pass
    if key in LETTER_SIZES:
        return (1, float(LETTER_SIZES[key]), key)
    return (2, 0.0, key)


def allocate(total: int, history: Mapping[str, int]) -> list[tuple[str, int]]:
    """Split ``total`` pieces over the sizes in ``history`` (pieces sold per size).

    Each size gets its share rounded down; the pieces left over go one each to
    the sizes with the largest remainder, a tie to the size that sold more, then
    to the earlier size. The split always adds up to ``total``. Sizes that did
    not sell are left out. Exact integer arithmetic, no floats."""
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise ValueError("the total must be a positive whole number")
    sold = sorted(
        ((size, pieces) for size, pieces in history.items() if pieces > 0),
        key=lambda item: size_sort_key(item[0]),
    )
    whole = sum(pieces for _, pieces in sold)
    if whole <= 0:
        raise ValueError("no history to split by")
    split = [total * pieces // whole for _, pieces in sold]
    spare = total - sum(split)
    order = sorted(
        range(len(sold)),
        key=lambda i: (-(total * sold[i][1] % whole), -sold[i][1], i),
    )
    for i in order[:spare]:
        split[i] += 1
    return [(size, qty) for (size, _), qty in zip(sold, split, strict=True)]


# ---------------------------------------------------------------------------
# The history
# ---------------------------------------------------------------------------


@dataclass
class Curve:
    """One category's size split at one store, from one season's sales."""

    category: str
    #: Pieces sold per size, net of exchange legs, sizes in order.
    sizes: list[tuple[str, int]] = field(default_factory=list)

    @property
    def pieces(self) -> int:
        return sum(pieces for _, pieces in self.sizes)

    def as_json(self) -> dict[str, Any]:
        whole = self.pieces
        return {
            "category": self.category,
            "pieces": whole,
            "sizes": [
                {
                    "size": size,
                    "pieces": pieces,
                    # Tenths of a percent, for showing only: the split itself is
                    # worked out from the pieces (``allocate``).
                    "share_permille": pieces * 1000 // whole if whole else 0,
                }
                for size, pieces in self.sizes
            ],
        }


@dataclass
class StoreCurves:
    """Every category's curve for one store, brand and booking season."""

    store: Store
    brand: Brand
    season: Season
    #: The season learnt from (SS25 for SS26); None when the code says no year.
    reference_code: str | None
    curves: list[Curve]
    #: Pieces sold with no category or no size: in no curve.
    uncategorised_pieces: int = 0
    unsized_pieces: int = 0

    @property
    def reason(self) -> str | None:
        """Why there is no curve at all, or None when there is one."""
        if self.reference_code is None:
            return NO_LAST_YEAR
        if not self.curves:
            return NO_HISTORY
        return None

    def curve(self, category: str) -> Curve | None:
        wanted = _category_key(category)
        return next((c for c in self.curves if _category_key(c.category) == wanted), None)


def _category_key(category: str) -> str:
    return " ".join(str(category).split()).casefold()


def _season_texts(code: str) -> list[str]:
    """What a bill line may call the reference season: its code, or its name."""
    texts = [code]
    named = Season.objects.filter(code__iexact=code).values_list("name", flat=True).first()
    if named:
        texts.append(named)
    return texts


def store_curves(store: Store, brand: Brand, season: Season) -> StoreCurves:
    """The size curves of ``brand`` at ``store`` for booking ``season``.

    Read from the store's accepted bills that were not cancelled: the lines of
    this brand whose piece belongs to the same season last year. A sold line adds
    its pieces, an exchange leg takes them off. Sizes that come to nothing are
    left out, and so is a category with no size left."""
    reference = reference_season_code(season.code)
    found = StoreCurves(
        store=store, brand=brand, season=season, reference_code=reference, curves=[]
    )
    if reference is None:
        return found
    from sell.models import SaleLine

    season_match = Q()
    for text in _season_texts(reference):
        season_match |= Q(season__iexact=text.strip())
    rows = (
        SaleLine.objects.filter(
            season_match,
            sale__store=store,
            sale__docstatus=DocStatus.SUBMITTED,
            sale__doc_number__isnull=False,
            brand__iexact=brand.name.strip(),
        )
        .values("item", "size", "direction")
        .annotate(pieces=Sum("qty"))
        .order_by()
    )
    net: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    spelled: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    size_spelled: dict[str, str] = {}
    for row in rows:
        sign = -1 if row["direction"] == SaleLine.Direction.RETURN else 1
        pieces = sign * int(row["pieces"] or 0)
        item = " ".join(str(row["item"] or "").split())
        size = " ".join(str(row["size"] or "").split())
        if not item:
            found.uncategorised_pieces += pieces
            continue
        if not size:
            found.unsized_pieces += pieces
            continue
        key = _category_key(item)
        net[key][size_key(size)] += pieces
        spelled[key][item] += max(pieces, 0)
        size_spelled.setdefault(size_key(size), size)
    for key, by_size in net.items():
        sizes = sorted(
            ((size_spelled[k], pieces) for k, pieces in by_size.items() if pieces > 0),
            key=lambda item: size_sort_key(item[0]),
        )
        if not sizes:
            continue
        # The category as it is most often spelled on the bills.
        label = max(sorted(spelled[key]), key=lambda text: spelled[key][text])
        found.curves.append(Curve(category=label, sizes=sizes))
    found.curves.sort(key=lambda curve: _category_key(curve.category))
    found.uncategorised_pieces = max(found.uncategorised_pieces, 0)
    found.unsized_pieces = max(found.unsized_pieces, 0)
    return found
