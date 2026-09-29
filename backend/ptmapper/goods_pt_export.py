"""The canonical KDPS workbook view of goods-v1 PT lines (E132/E133; OPS-15).

Store and warehouse operations PRD §5.4 and the canonical PT file: an export is the
twenty-two KDPS columns in their order - SEASON, BRAND, COLOR, GENDER, SUB CATEGORY,
TYPE, ITEM, FIT, SIZE, BARCODE, DESIGN, HSN, QTY, MRP, BASIC, P RATE, INPUT TAX,
OUTPUT TAX, NAG, MARGIN, SUGGESTED SUB CATEGORY, SUGGESTED TYPE - as a *view* of
each resolved line. Describing values come from the line, else from its item; the
calculated columns are the server's (NAG is QTY; the two SUGGESTED columns are the
master's ITEM hints). Cost columns (BASIC, P RATE, MARGIN) appear only for a caller
entitled to cost.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.utils import timezone

from masters.goods_identity_models import ProductSku
from masters.goods_identity_services import ITEM_ISSUER, value_labels
from masters.models import Brand, Season
from ptmapper.goods_item_match import by_field

#: (PRD column, key, logical type, mode), in the PRD's order.
KDPS_EXPORT: tuple[tuple[str, str, str, str], ...] = (
    ("SEASON", "season", "text", "supplied"),
    ("BRAND", "brand", "text", "supplied"),
    ("COLOR", "colour", "text", "supplied"),
    ("GENDER", "gender", "text", "supplied"),
    ("SUB CATEGORY", "sub_category", "text", "supplied"),
    ("TYPE", "type", "text", "supplied"),
    ("ITEM", "item", "text", "supplied"),
    ("FIT", "fit", "text", "supplied"),
    ("SIZE", "size", "text", "supplied"),
    ("BARCODE", "alias_as_used", "text", "supplied"),
    ("DESIGN", "design", "text", "supplied"),
    ("HSN", "hsn", "text", "supplied"),
    ("QTY", "qty", "text", "supplied"),
    ("MRP", "mrp_paise", "money", "supplied"),
    ("BASIC", "basic_paise", "money", "supplied"),
    ("P RATE", "p_rate_paise", "money", "derived"),
    ("INPUT TAX", "input_tax_pct", "text", "derived"),
    ("OUTPUT TAX", "output_tax_pct", "text", "derived"),
    ("NAG", "nag", "text", "derived"),
    ("MARGIN", "margin_pct", "text", "derived"),
    ("SUGGESTED SUB CATEGORY", "suggested_sub_category", "text", "derived"),
    ("SUGGESTED TYPE", "suggested_type", "text", "derived"),
)
COST_KEYS = frozenset({"basic_paise", "p_rate_paise", "margin_pct"})
#: Export key -> the vocabulary dimension the line (or its item) holds it in.
DIMENSIONS = {
    "colour": "colour",
    "gender": "gender",
    "sub_category": "sub_category",
    "type": "type",
    "item": "item",
    "fit": "fit",
    "size": "size",
}


def export_columns(shows_cost: bool) -> list[dict[str, Any]]:
    return [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"pt-column:{key}")),
            "key": key,
            "label": label,
            "order": index,
            "logical_type": logical,
            "required": mode == "supplied",
            "mode": mode,
            "table_visible": True,
            "export_visible": True,
        }
        for index, (label, key, logical, mode) in enumerate(
            (column for column in KDPS_EXPORT if shows_cost or column[1] not in COST_KEYS),
            start=1,
        )
    ]


class CanonicalCells:
    """Resolves lines to their canonical cell values, reading each master once."""

    def __init__(self, tenant_id: uuid.UUID, lines: Iterable[dict[str, Any]]) -> None:
        lines = list(lines)
        sku_ids = {str(line["sku_id"]) for line in lines if line.get("sku_id")}
        self.skus = {
            str(sku.pk): sku
            for sku in ProductSku.objects.select_related("style", "style__brand").filter(
                tenant_id=tenant_id, pk__in=sorted(sku_ids)
            )
        }
        season_ids = {str(line["season_id"]) for line in lines if line.get("season_id")}
        self.seasons = {
            str(pk): name
            for pk, name in Season.objects.filter(pk__in=sorted(season_ids)).values_list(
                "pk", "name"
            )
        }
        brand_ids = {
            int((line.get("describing") or {})["brand_id"])
            for line in lines
            if (line.get("describing") or {}).get("brand_id")
        }
        self.brands = {
            pk: name
            for pk, name in Brand.objects.filter(pk__in=brand_ids).values_list("pk", "name")
        }
        self.labels = value_labels(tenant_id)
        self.tenant_id = tenant_id
        self._rulebook: Any = None

    def _book(self) -> Any:
        if self._rulebook is None:
            from ptmapper.goods_rulebook import Rulebook

            self._rulebook = Rulebook.load(self.tenant_id, timezone.now())
        return self._rulebook

    def _hint(self, dimension: str, item_key: str | None) -> str | None:
        """The master's ITEM hint for SUB CATEGORY or TYPE (a check, not an input)."""
        if not item_key:
            return None
        rule, _clash = self._book().lookup(dimension, item_key, [ITEM_ISSUER])
        return rule.target.label if rule is not None else None

    def _attribute(self, entry: dict[str, Any] | None) -> str | None:
        if not entry or entry.get("unknown"):
            return None
        if entry.get("vocabulary_value_id"):
            value_id = str(entry["vocabulary_value_id"])
            return self.labels.get(value_id, value_id)
        text = entry.get("supplied_text")
        return str(text) if text else None

    def cells(self, line: dict[str, Any]) -> dict[str, Any]:
        """``{export key: value}``; money is integer paise text, blanks are None."""
        sku = self.skus.get(str(line.get("sku_id")))
        stated = by_field(line.get("attributes") or [])
        own = by_field(sku.attrs or []) if sku is not None else {}
        describing = line.get("describing") or {}
        calculated = line.get("calculated") or {}
        supplied = line.get("supplied") or {}
        out: dict[str, Any] = {
            "season": self.seasons.get(str(line.get("season_id"))),
            "brand": (
                self.brands.get(describing["brand_id"])
                if describing.get("brand_id")
                else (sku.style.brand.name if sku is not None else None)
            ),
            "design": describing.get("design")
            or (sku.style.style_code if sku is not None else None),
            "alias_as_used": line.get("alias_as_used"),
            "hsn": line.get("hsn"),
            "qty": line.get("qty"),
            "nag": line.get("qty"),
        }
        for key, dimension in DIMENSIONS.items():
            out[key] = self._attribute(stated.get(dimension)) or self._attribute(own.get(dimension))
        item_entry = stated.get("item") or own.get("item") or {}
        item_key = self._value_key(item_entry)
        out["suggested_sub_category"] = self._hint("sub_category", item_key)
        out["suggested_type"] = self._hint("type", item_key)
        for key in ("mrp_paise", "basic_paise", "p_rate_paise"):
            out[key] = calculated.get(key) or supplied.get(key)
        for key in ("margin_pct", "output_tax_pct", "input_tax_pct"):
            out[key] = calculated.get(key)
        return {key: (None if value in (None, "") else value) for key, value in out.items()}

    def _value_key(self, entry: dict[str, Any]) -> str | None:
        """An ITEM value's key, as the rulebook's ITEM hints are keyed."""
        value_id = entry.get("vocabulary_value_id")
        if not value_id:
            return None
        for value in self._book().choices("item"):
            if value.id == str(value_id):
                return str(value.value_key)
        return None
