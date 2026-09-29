"""The PT a transfer carries with it (#72).

Every transfer generates its own PT file: one transfer = one carton = one
document, in the same KDPS column format the brand PTs arrive in, so the store
at the other end reads a file it already knows how to read.

It is never typed and never edited. ``build_transfer_pt_rows`` is a pure
function of the transfer's *scanned* lines, so the document and the carton
cannot disagree — regenerating it is the only way it ever changes, and it
regenerates to the same rows.

**A transfer PT is not an inbound PT.** It is generated from a scan, it raises
no vendor liability, and it books no money: the stock it lists is already on
KDPS's books and is merely moving. The rulings on who may *make* a PT (#119) and
who may *inward* one (#124) both govern the inbound PT — the one that turns a
brand's arriving goods into stock — and are deliberately not consulted here.

The tax columns stay blank for the same reason. A cross-state transfer is a
taxable supply, but its IGST invoice is a separate manual document by decision
(see ``outbound.posting.post_transfer_dispatch``); inventing a tax breakup on a
packing list would be the system quietly claiming to have raised one.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.money import paise_to_rupees_str

# The KDPS PT format itself, borrowed from the mapper rather than restated: if
# the format gains a column, a transfer's PT gains it on the same day.
from ptmapper.profiles import KDPS_COLUMNS

__all__ = ["KDPS_COLUMNS", "build_transfer_pt_rows"]


def _money(paise: int | None) -> str:
    """Rupees as the PT writes them — or blank where the books hold no number.

    Blank, never ``0.00``: a zero on a PT reads as "this piece is free", and the
    one thing a money column may not do is invent a value (#103).
    """
    return paise_to_rupees_str(paise) if paise else ""


def _margin(mrp_paise: int | None, cost_paise: int | None) -> str:
    """Margin percentage the way the mapper computes it, to two places."""
    if not mrp_paise or not cost_paise or mrp_paise <= 0:
        return ""
    pct = (Decimal(mrp_paise - cost_paise) / Decimal(mrp_paise)) * 100
    return str(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _row(line: Any) -> dict[str, Any]:
    """One dispatched line as one KDPS PT row.

    Everything comes off the line, which took its identity from the source
    location's own books at scan time — so the PT describes the pieces the
    ledger says left, not what somebody remembered about them.
    """
    cost_paise = line.unit_cost_paise
    mrp_paise = line.mrp_paise
    return {
        "SEASON": line.season,
        "BRAND": line.brand,
        "COLOR": line.color,
        # The taxonomy columns are a brand PT's guesses about goods arriving for
        # the first time. This stock has been in the books for a while; the
        # transfer knows nothing new about it, so it says nothing.
        "GENDER": "",
        "SUB CATEGORY": "",
        "TYPE": "",
        "ITEM": line.item,
        "FIT": "",
        "SIZE": line.size,
        "BARCODE": line.sku_code,
        "DESIGN": line.design,
        "HSN": line.hsn,
        "QTY": line.qty_dispatched,
        "MRP": _money(mrp_paise),
        "BASIC": "",
        "P RATE": _money(cost_paise),
        "INPUT TAX": "",
        "OUTPUT TAX": "",
        "NAG": line.qty_dispatched,
        "MARGIN": _margin(mrp_paise, cost_paise),
        "SUGGESTED SUB CATEGORY": "",
        "SUGGESTED TYPE": "",
    }


def build_transfer_pt_rows(transfer: Any) -> list[dict[str, Any]]:
    """The transfer's PT rows — a function of its scanned lines and nothing else.

    Nothing outside the transfer is consulted, master data included: cost and MRP
    were both snapshotted onto the line at scan time, so regenerating this a year
    later reproduces the paper that went in the carton even if the SKU has since
    been re-ticketed.

    Lines that were planned but never scanned are dropped — the PT lists what is
    in the carton, and a piece nobody scanned is not.
    """
    from outbound.models import StoreTransferLine

    # Queried, never read off ``transfer.lines`` — dispatch runs against a
    # transfer whose lines were prefetched *before* the scan created them, and a
    # stale cache would print a carton that was never packed.
    lines = StoreTransferLine.objects.filter(transfer=transfer)
    return [_row(line) for line in lines if line.qty_dispatched]
