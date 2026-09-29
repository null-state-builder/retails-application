"""Till against server, for a bill priced with GST after discount (ticket 11).

Store operations PRD §6 ST-CMP-1: "The till and the server must reach the same
result. A difference raises a bill flag and never blocks the bill." Success
measure (§26): nought till/server tax mismatches a month.

A bill that says it was priced after discount (``Sale.gst_after_discount``) is
worked out again here with ``sell.after_discount.price_bill`` - the function the
golden bills hold to the paisa on both sides - from the same inputs the till
had: the lines, the rulebook as it ran on the bill's day, and the tax version
the bill recorded. Every line, the round-off line and the bank offer payments
are compared exactly; any difference is ``till_server_mismatch``.

Separately, a bill whose pricing does not match the store's switch as it stands
(an old till, or a bill queued before the switch changed) is
``after_discount_mismatch``. It is still judged by what it says it did.

Neither is ever a refusal: the bill is printed and the customer has gone.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from masters.models import Store
from masters.store_feature_registry import GST_AFTER_DISCOUNT
from masters.store_features import is_feature_on
from offers.resolution import Resolution
from sell.after_discount import BillLineIn, PricedBill, price_bill
from sell.alteration_models import ALTERATION_GST_RATE
from sell.models import ContinuityFlag, Sale, SaleLine
from sell.services.tax_rulebook import StoreTaxBooks

Finding = tuple[str, dict[str, Any]]


def after_discount_on(store: Store) -> bool:
    """Is the store's switch on (and not held by its gate)?"""
    return is_feature_on(store, GST_AFTER_DISCOUNT)


def check(
    sale: Sale,
    store: Store,
    rows: Sequence[SaleLine],
    resolution: Resolution,
    tenders: Sequence[dict[str, Any]],
) -> list[Finding]:
    """The findings on one accepted bill; empty for a clean one."""
    findings: list[Finding] = []
    switch = after_discount_on(store)
    if switch != sale.gst_after_discount:
        findings.append(
            (
                ContinuityFlag.Kind.AFTER_DISCOUNT_MISMATCH,
                {"bill": sale.gst_after_discount, "store_switch": switch},
            )
        )
    if not sale.gst_after_discount:
        return findings
    books = StoreTaxBooks(store)
    rulebook = books.recorded(sale.tax_setting_version, sale.billed_at) or books.at(sale.billed_at)
    sold = [row for row in rows if row.direction != SaleLine.Direction.RETURN]
    returned = [row for row in rows if row.direction == SaleLine.Direction.RETURN]
    server = price_bill(
        [_line_in(row) for row in sold],
        resolution,
        lambda hsn, net, qty: rulebook.line_tax(hsn, net, qty).split,
        round_total_paise=rulebook.round_total_paise,
        returned_paise=sum(int(row.net_paise) for row in returned),
        returned_gst_paise=sum(int(row.gst_paise) for row in returned),
    )
    differences = _differences(sale, sold, server, tenders)
    if differences:
        findings.append(
            (
                ContinuityFlag.Kind.TILL_SERVER_MISMATCH,
                {"tax_setting_version": rulebook.version, **differences},
            )
        )
    return findings


def _line_in(row: SaleLine) -> BillLineIn:
    """A written line as the till had it: the cashier's own discount is what the
    line gave beyond the offer it claims (never below nought)."""
    claimed = int((row.offer_evidence or {}).get("saved_paise") or 0)
    return BillLineIn(
        line_no=row.line_no,
        hsn=row.hsn,
        qty=int(row.qty),
        mrp_paise=int(row.mrp_paise),
        manual_disc_paise=max(int(row.disc_paise) - claimed, 0),
        # Ticket 22: an alteration charge keeps its own fixed rate - the server's.
        fixed_rate=ALTERATION_GST_RATE if row.is_alteration else None,
    )


def _differences(
    sale: Sale,
    sold: Sequence[SaleLine],
    server: PricedBill,
    tenders: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    expected = {line.line_no: line for line in server.lines}
    lines = []
    for row in sold:
        want = expected[row.line_no]
        till = {
            "disc_paise": int(row.disc_paise),
            "net_paise": int(row.net_paise),
            "gst_rate": _rate(row.gst_rate),
            "gst_paise": int(row.gst_paise),
        }
        ours = {
            "disc_paise": want.disc_paise,
            "net_paise": want.net_paise,
            "gst_rate": _rate(want.gst_rate),
            "gst_paise": want.gst_paise,
        }
        if till != ours:
            lines.append({"line_no": row.line_no, "till": till, "server": ours})
    if lines:
        out["lines"] = lines
    if int(sale.round_paise) != server.round_paise:
        out["round_paise"] = {"till": int(sale.round_paise), "server": server.round_paise}
    till_bank = sorted(
        (int(t.get("offer_id") or 0), int(t["amount_paise"]))
        for t in tenders
        if t["mode"] == "bank_offer"
    )
    server_bank = sorted((offer.offer_id, offer.amount_paise) for offer in server.bank_offers)
    if till_bank != server_bank:
        out["bank_offers"] = {
            "till": [{"offer_id": i or None, "amount_paise": a} for i, a in till_bank],
            "server": [{"offer_id": i, "amount_paise": a} for i, a in server_bank],
        }
    return out


def _rate(value: Any) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"
