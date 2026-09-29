"""Pasting canonical values into a receipt PT's bound rows (ticket 06A, GSA-T06).

A receipt PT is bound to its GRN: E123 gives one row per eligible counted lot,
and a paste fills those rows - it never adds one. A paste reaches the server as
an ordinary E124 row edit, ``{line_key, canonical: {"MRP": "1,299.50", ...}}``,
so it is saved, re-priced and review-cleared exactly as a typed cell is, and it
is saved only when the preparer presses Save.

Each pasted cell is read by the canonical upload's own reader
(``goods_canonical.read_cells``): exact money or a refusal, never a rounded
amount; a barcode through one effective alias; a season by its code or name; a
whole-number quantity. What a paste adds is the binding to the counted row:

* a barcode must name the row's own item - the item its counted goods resolved
  to (overall PRD §15.2.1 rule 2); a paste never chooses or changes an item;
* a quantity stays on the row's one counted lot and may not ask for more pieces
  than that lot still has free for this PT, so a paste cannot reach held goods,
  goods another PT already covers, or pieces the count never found.

Any refused cell refuses the whole save (``ROW_INVALID``) with one issue per
cell, and nothing is written.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.goods_fields import MAX_LINE_QTY
from core.refusals import issue
from ptmapper.goods_canonical import read_cells
from stockledger import ranges

#: The canonical columns a paste may fill, by their KDPS names, and the key the
#: canonical reader knows each by. The same seven a canonical workbook is read from.
PASTE_COLUMNS = {
    "SEASON": "season",
    "BARCODE": "barcode",
    "HSN": "hsn",
    "QTY": "qty",
    "MRP": "mrp",
    "BASIC": "basic",
    "P RATE": "p_rate",
}
CELL_TEXT = 240


def shape_problem(canonical: Any) -> str | None:
    """Why ``canonical`` is not ``{KDPS column: text or null}``; ``None`` when it is."""
    if not isinstance(canonical, dict) or not canonical:
        return "canonical holds at least one pasted cell, keyed by its KDPS column."
    unknown = sorted(set(canonical) - set(PASTE_COLUMNS))
    if unknown:
        return (
            f"{', '.join(unknown)} cannot be pasted; a paste fills {', '.join(PASTE_COLUMNS)} only."
        )
    for value in canonical.values():
        if value is not None and (not isinstance(value, str) or len(value) > CELL_TEXT):
            return f"A pasted cell is text of at most {CELL_TEXT} characters, or null."
    return None


def paste_row(
    tenant_id: Any,
    line: dict[str, Any],
    canonical: dict[str, str | None],
    row: int,
    problems: list[dict[str, Any]],
) -> set[str]:
    """Apply one row's pasted cells to ``line``; the E124 column keys they set.

    A cell that cannot be used is added to ``problems`` (and leaves its value
    unset); the caller refuses the save when there are any.
    """
    key = line.get("line_key")
    values = {PASTE_COLUMNS[label]: text for label, text in canonical.items()}
    found: list[dict[str, Any]] = []
    cells = read_cells(tenant_id, values, row, found)

    def refuse(code: str, column: str, message: str) -> None:
        found.append(issue(code, f"Row {row}, {column}: {message}", field=column, line_key=key))

    edited: set[str] = set()
    if "barcode" in values:
        edited.add("alias_as_used")
        _barcode(line, cells, refuse)
    if "season" in values:
        edited.add("season_id")
        text = cells.text["season"]
        if text and cells.fields["season_id"] is None:
            refuse("SEASON_REQUIRED", "SEASON", f"{text} names no single season.")
        else:
            line["season_id"] = cells.fields["season_id"]
    if "hsn" in values:
        edited.add("hsn")
        line["hsn"] = cells.fields["hsn"]
    if "qty" in values:
        edited.add("qty")
        _quantity(line, cells.fields["qty"], cells.text["qty"], refuse)
    if cells.supplied:
        edited |= set(cells.supplied)
        line["supplied"] = {**(line.get("supplied") or {}), **cells.supplied}
    for problem in found:
        problem.setdefault("line_key", key)
    problems.extend(found)
    return edited


def _barcode(line: dict[str, Any], cells: Any, refuse: Any) -> None:
    text = cells.text["barcode"]
    if not text:
        # A blank barcode clears only the alias as used; the row keeps the item its
        # counted goods resolved to, since a paste never chooses or changes an item.
        line["alias_as_used"] = None
        line["alias_id"] = None
        return
    own = str(line.get("sku_id") or "")
    if not own:
        refuse(
            "IDENTITY_UNRESOLVED",
            "BARCODE",
            "this row names no item yet; a paste never chooses one.",
        )
    elif not cells.barcode_skus:
        refuse("IDENTITY_UNRESOLVED", "BARCODE", f"{text} is not a known barcode.")
    elif len(cells.barcode_skus) > 1:
        refuse(
            "IDENTITY_UNRESOLVED",
            "BARCODE",
            f"{text} belongs to {len(cells.barcode_skus)} items.",
        )
    elif cells.fields["sku_id"] != own:
        refuse(
            "COVERAGE_SKU_MISMATCH",
            "BARCODE",
            f"{text} is another item; a row keeps the item its counted goods resolved to.",
        )
    else:
        line["alias_as_used"] = cells.fields["alias_as_used"]
        line["alias_id"] = cells.fields["alias_id"]


def _quantity(line: dict[str, Any], qty: int | None, text: str, refuse: Any) -> None:
    if qty is None or not 1 <= qty <= MAX_LINE_QTY:
        refuse(
            "QTY_INVALID", "QTY", f"{text or 'a blank'} is not a whole number from 1 to 999,999."
        )
        return
    if qty == line.get("qty"):
        return
    requests = line.get("coverage_requests") or []
    if len(requests) != 1 or not requests[0].get("lot_id"):
        refuse(
            "COVERAGE_SOURCE_INVALID",
            "QTY",
            "this row is not bound to one counted lot, so a paste cannot change its quantity.",
        )
        return
    line["qty"] = qty
    line["coverage_requests"] = [{"lot_id": str(requests[0]["lot_id"]), "qty": qty}]


def room_problems(
    pool: Any, lines: dict[str, dict[str, Any]], raised: dict[str, int]
) -> list[dict[str, Any]]:
    """Rows whose pasted quantity went up past what their counted lot has free.

    ``pool`` is the GRN's receipt pool for this PT's kind: its free pieces already
    leave out held goods and goods another PT covers. Every line's claim is counted,
    so the answer does not depend on the order the rows were pasted in.
    """
    by_sku: dict[tuple[str, str], int] = defaultdict(int)
    by_grn_line: dict[str, int] = defaultdict(int)
    for line in lines.values():
        for request in line.get("coverage_requests") or []:
            lot = str(request.get("lot_id"))
            qty = int(request.get("qty") or 0)
            by_sku[(lot, str(line.get("sku_id")))] += qty
            by_grn_line[pool.line_of.get(lot, "")] += qty
    problems: list[dict[str, Any]] = []
    for key, row in raised.items():
        line = lines[key]
        (request,) = line["coverage_requests"]
        lot = str(request["lot_id"])
        if lot in pool.line_of:
            grn_line = pool.line_of[lot]
            free = ranges.total(pool.portions(lot, line.get("sku_id")))
            over = max(
                by_sku[(lot, str(line.get("sku_id")))] - free,
                by_grn_line[grn_line] - pool.line_room(grn_line),
            )
        else:
            over = int(line["qty"])  # not a lot of this GRN: nothing of it is coverable
        if over <= 0:
            continue
        can = max(line["qty"] - over, 0)
        problems.append(
            issue(
                "COVERAGE_EXCEEDS_COUNT",
                f"Row {row}, QTY: only {can} counted piece(s) can go on this row; a PT never "
                "adds pieces the count did not find, nor held or already covered ones.",
                field="QTY",
                line_key=key,
                quantity=can,
            )
        )
    return problems
