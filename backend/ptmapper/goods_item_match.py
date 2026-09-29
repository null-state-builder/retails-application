"""Which item (SKU) a PT line is, from its barcode or its describing values (OPS-15).

Store and warehouse operations PRD §5.4: "a row is an item". A brand-file row, or a
row a person edits, is matched in a fixed order and never on a guess:

1. **Barcode first.** The barcode is an alias of exactly one item here and now:
   that item. An alias shared by several items is *ambiguous* - no item is chosen
   and a person picks (canonical PT file: "two identities sharing one vendor
   barcode is an exception, not a silent merge").
2. **Describing values.** Brand and design name the style; the style's items whose
   identity values (the identity profile's distinguishing values and size) equal
   the row's are candidates. One candidate is the item; several are ambiguous.
3. **Otherwise a new item** is drafted: no SKU, the mapped values kept, and an issue
   says so. It becomes an item only through the goods-v1 proposal and the
   product-master owner's confirmation.

A pending item proposed while preparing *this* PT draft (any of its revisions) is
a candidate like an effective one (goods change PRD decision I2); a pending item
of any other document never is.

Nothing here writes. Callers pass the lines' own attribute lists
(``[{field_id, vocabulary_value_id?, supplied_text?, unknown}]``) whose
``field_id`` is the vocabulary dimension name, as on ``ProductSku.attrs``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db.models import Q

from core.refusals import issue
from masters.goods_identity_models import GovernanceState, ProductSku, SkuAlias
from masters.goods_identity_services import (
    IdentityProfile,
    normalise_text,
    profile_from_version,
    usable_q,
)
from masters.models import Season
from ptmapper.goods_mapper import COLUMN_DIMENSIONS

#: How a line's item was found.
BY_BARCODE = "barcode"
BY_VALUES = "describing_values"
NEW_ITEM = "new_item"
AMBIGUOUS = "ambiguous"
BY_PERSON = "person"
MATCH_KINDS = frozenset({BY_BARCODE, BY_VALUES, NEW_ITEM, AMBIGUOUS, BY_PERSON})

#: Issue codes the matcher (and the brand-file intake) leave on a line.
NOTE_NEW_ITEM = "NEW_ITEM"
NOTE_AMBIGUOUS = "ITEM_AMBIGUOUS"
NOTE_UNCOVERED = "COVERAGE_UNMATCHED"
NOTE_SEASON = "SEASON_UNMATCHED"
NOTE_VALUE = "VALUE_UNREADABLE"
ITEM_NOTES = frozenset({NOTE_NEW_ITEM, NOTE_AMBIGUOUS})
MAX_CANDIDATES = 20

#: The KDPS columns a PT line holds a value (and so an origin) for, in PRD order.
LINE_COLUMNS = (
    "SEASON",
    "BRAND",
    "COLOR",
    "GENDER",
    "SUB CATEGORY",
    "TYPE",
    "ITEM",
    "FIT",
    "SIZE",
    "BARCODE",
    "DESIGN",
    "HSN",
    "QTY",
    "MRP",
    "BASIC",
    "P RATE",
)
#: Vocabulary dimension -> the KDPS column it fills (season lives on ``season_id``).
DIMENSION_COLUMNS = {
    dimension: column for column, dimension in COLUMN_DIMENSIONS.items() if column != "SEASON"
}
#: E124 column key -> the KDPS column it is (attributes are split per dimension).
EDIT_COLUMNS = {
    "season_id": "SEASON",
    "brand_id": "BRAND",
    "alias_as_used": "BARCODE",
    "design": "DESIGN",
    "hsn": "HSN",
    "qty": "QTY",
    "mrp_paise": "MRP",
    "basic_paise": "BASIC",
    "check_p_rate_paise": "P RATE",
}
#: Edits that change what the row describes, and so which item it is.
DESCRIBING_EDITS = frozenset({"attributes", "brand_id", "design", "alias_as_used"})


@dataclass
class ItemMatch:
    by: str
    sku_id: str | None
    candidates: list[str] = field(default_factory=list)
    note: dict[str, Any] | None = None


def attribute_value(entry: dict[str, Any] | None) -> tuple[str, str] | None:
    """An attribute's comparable value; None when it is absent or explicitly unknown."""
    if not entry:
        return None
    if entry.get("vocabulary_value_id"):
        return ("value", str(entry["vocabulary_value_id"]))
    if entry.get("supplied_text") is not None:
        return ("text", normalise_text(str(entry["supplied_text"])))
    return None


def by_field(attributes: Iterable[Any]) -> dict[str, dict[str, Any]]:
    return {str(e.get("field_id")): e for e in attributes or [] if isinstance(e, dict)}


def set_note(
    line: dict[str, Any], code: str, message: str | None, *, field: str | None = None
) -> None:
    """Replace the line's server note of ``code`` about ``field`` (``message`` None removes it)."""
    match = dict(line.get("match") or {})
    notes = [
        n
        for n in match.get("notes") or []
        if not (n.get("code") == code and n.get("field") == field)
    ]
    if message is not None:
        note = {"code": code, "message": message[:300]}
        if field is not None:
            note["field"] = field
        notes.append(note)
    match["notes"] = notes
    line["match"] = match


# ---------------------------------------------------------------------------
# Barcodes
# ---------------------------------------------------------------------------


def barcode_skus(
    tenant_id: uuid.UUID,
    values: Iterable[str],
    *,
    site_id: int | None,
    as_of: datetime,
    lineage: list[uuid.UUID] | None = None,
) -> dict[str, list[str]]:
    """Each barcode's items: aliases in their period, unscoped or for ``site_id``,
    usable with their item and style (``resolve_alias``'s rules, in one query)."""
    wanted = sorted({value for value in values if value})
    if not wanted:
        return {}
    lineage = lineage or []
    aliases = (
        SkuAlias.objects.filter(tenant_id=tenant_id, value__in=wanted, effective_from__lte=as_of)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
        .filter(Q(site__isnull=True) | Q(site_id=site_id))
        .filter(usable_q(as_of, lineage, retired_field=False))
        .filter(usable_q(as_of, lineage, prefix="sku__"))
        .filter(usable_q(as_of, lineage, prefix="sku__style__"))
    )
    found: dict[str, set[str]] = {}
    for value, sku_id in aliases.values_list("value", "sku_id"):
        found.setdefault(value, set()).add(str(sku_id))
    return {value: sorted(skus) for value, skus in found.items()}


# ---------------------------------------------------------------------------
# Describing values
# ---------------------------------------------------------------------------


@dataclass
class ItemIndex:
    """Candidate items per (brand, design) of one product family, read once per key."""

    tenant_id: uuid.UUID
    family: str
    as_of: datetime
    lineage: list[uuid.UUID] = field(default_factory=list)
    _styles: dict[tuple[int, str], list[ProductSku]] = field(default_factory=dict)
    _profiles: dict[uuid.UUID, IdentityProfile | None] = field(default_factory=dict)

    def skus(self, brand_id: int, design: str) -> list[ProductSku]:
        key = (brand_id, normalise_text(design))
        if key not in self._styles:
            usable = usable_q(self.as_of, self.lineage)
            rows = (
                ProductSku.objects.select_related("style", "identity_profile")
                .filter(
                    tenant_id=self.tenant_id,
                    style__brand_id=brand_id,
                    style__profile_family=self.family,
                    style__style_code__iexact=design.strip(),
                )
                .filter(usable)
                .filter(usable_q(self.as_of, self.lineage, prefix="style__"))
                .order_by("id")
            )
            self._styles[key] = [
                sku for sku in rows if normalise_text(sku.style.style_code) == key[1]
            ]
        return self._styles[key]

    def profile(self, sku: ProductSku) -> IdentityProfile | None:
        version_id = sku.identity_profile_id
        if version_id is None or sku.identity_profile is None:
            return None
        if version_id not in self._profiles:
            self._profiles[version_id] = profile_from_version(sku.identity_profile)
        return self._profiles[version_id]


def fits(sku: ProductSku, stated: dict[str, dict[str, Any]], index: ItemIndex) -> bool:
    """Every identity value of ``sku`` equals the row's (an unknown size only an unstated one).

    With an identity profile the identity values are its distinguishing values and
    size; without one, every value the item itself states.
    """
    own = by_field(sku.attrs or [])
    profile = index.profile(sku)
    fields = profile.identity_fields if profile is not None else tuple(own)
    return all(attribute_value(own.get(f)) == attribute_value(stated.get(f)) for f in fields)


def match_item(
    *,
    barcode: str | None,
    brand_id: int | None,
    design: str | None,
    attributes: list[dict[str, Any]],
    index: ItemIndex,
    barcodes: dict[str, list[str]],
) -> ItemMatch:
    """The row's item by barcode, then describing values, else a drafted new item."""
    if barcode and barcodes.get(barcode):
        skus = barcodes[barcode]
        if len(skus) == 1:
            return ItemMatch(BY_BARCODE, skus[0])
        return ItemMatch(
            AMBIGUOUS,
            None,
            skus[:MAX_CANDIDATES],
            issue(
                NOTE_AMBIGUOUS,
                f"Barcode {barcode} belongs to {len(skus)} items; choose which one this row is",
                field="sku_id",
            ),
        )
    missing = [name for name, value in (("brand", brand_id), ("design", design)) if not value]
    if missing:
        return ItemMatch(
            NEW_ITEM,
            None,
            note=issue(
                NOTE_NEW_ITEM,
                f"No existing item can be found without its {' and '.join(missing)}; "
                "this row is drafted as a new item",
                field="sku_id",
            ),
        )
    assert brand_id is not None and design is not None
    stated = by_field(attributes)
    hits = [str(sku.pk) for sku in index.skus(brand_id, design) if fits(sku, stated, index)]
    if len(hits) == 1:
        return ItemMatch(BY_VALUES, hits[0])
    if hits:
        return ItemMatch(
            AMBIGUOUS,
            None,
            hits[:MAX_CANDIDATES],
            issue(
                NOTE_AMBIGUOUS,
                f"{len(hits)} items have these values; choose which one this row is",
                field="sku_id",
            ),
        )
    return ItemMatch(
        NEW_ITEM,
        None,
        note=issue(
            NOTE_NEW_ITEM,
            "No existing item has this brand, design and these values; this row is drafted "
            "as a new item to propose",
            field="sku_id",
        ),
    )


def apply_match(line: dict[str, Any], found: ItemMatch) -> None:
    """Write a match onto the line: its SKU, how it was found and the item note."""
    line["sku_id"] = found.sku_id
    match = dict(line.get("match") or {})
    match["by"] = found.by
    match["candidates"] = list(found.candidates)
    line["match"] = match
    for code in ITEM_NOTES:
        set_note(line, code, None, field="sku_id")
    if found.note is not None:
        set_note(line, found.note["code"], found.note["message"], field=found.note.get("field"))


def describing(line: dict[str, Any], sku: ProductSku | None) -> tuple[int | None, str | None]:
    """(brand, design) the row states, falling back to its current item's style."""
    stated = line.get("describing") or {}
    brand = stated["brand_id"] if "brand_id" in stated else (sku.style.brand_id if sku else None)
    design = stated["design"] if "design" in stated else (sku.style.style_code if sku else None)
    return (int(brand) if brand else None), (str(design) if design else None)


def identity_attributes(line: dict[str, Any], sku: ProductSku | None) -> list[dict[str, Any]]:
    """The row's own attributes over its current item's: a row that restates only its
    new size still has the colour of the item it was."""
    merged = by_field(sku.attrs or []) if sku is not None else {}
    merged.update(by_field(line.get("attributes") or []))
    return list(merged.values())


def pending_elsewhere(sku_id: Any, lineage: list[uuid.UUID]) -> bool:
    """Whether ``sku_id`` is an unconfirmed proposal of some other document (decision I2)."""
    row = (
        ProductSku.objects.filter(pk=sku_id)
        .values("governance_state", "originating_revision_id")
        .first()
    )
    return bool(
        row
        and row["governance_state"] == GovernanceState.PENDING
        and row["originating_revision_id"] not in set(lineage)
    )


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------


def season_for(texts: Iterable[str | None]) -> tuple[int | None, bool]:
    """(the Season master row named by one of ``texts``, whether the name was ambiguous).

    A season on a PT line is a Season master row; a vocabulary season or a file's
    season text becomes one only when exactly one row has that code or that name
    (compared without case). The unknown historical season is never matched: a
    receipt always has a real cohort. Nothing is invented.
    """
    wanted = sorted({t.strip() for t in texts if t and t.strip()})
    if not wanted:
        return None, False
    query = Q()
    for text in wanted:
        query |= Q(code__iexact=text) | Q(name__iexact=text)
    found = list(
        Season.objects.filter(query, historical_unknown=False).values_list("pk", flat=True)[:2]
    )
    if len(found) == 1:
        return int(found[0]), False
    return None, len(found) > 1
