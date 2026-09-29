"""Books-health (equation-of-state / trial balance) read for the owner dashboard.

Surfaces the kernel's balanced-posting guarantee: every voucher fans into legs
that sum to zero, so the whole value GL's `Σ(amount)` (trial balance) is exactly
0 when the books tie. We also present the equation of state — assets the PT slice
builds (inventory + SOR stock + input GST + cash + card/UPI awaiting settlement)
against the liabilities/contra it raises (vendor payable + GRNI + SOR contra +
output GST + credit notes outstanding), with income and expense shown alongside
but outside those two totals. Finance/owner-only (ADR-0003).

It also answers one question the trial balance cannot: whether any stock row
holds **zero pieces and non-zero rupees** (#103). A balanced GL says the legs
tie; it says nothing about value stranded against pieces that have gone.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Sum
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.gl import GLAccount, GLEntry, account_balance, trial_balance
from core.money import paise_to_rupees_str
from finledger.models import CashLedgerEntry, VendorLedgerEntry
from finledger.posting import CASH_CONTROL_ACCOUNTS, gl_control_for
from finledger.views import IsBooksKeeper
from stockledger.models import StockOnHand

#: How many stranded rows to name before the list is just a count.
STRANDED_SAMPLE = 20

#: Every account in the chart, with the side it sits on. Exhaustive by test
#: (`test_books_health_covers_every_gl_account`): a new code that never reaches
#: this list would not break the trial balance — that is Σ over all legs — but it
#: would quietly vanish from the equation of state and the account breakdown,
#: which is a wrong number rather than a missing one.
#:
#: `assets_paise` and `liabilities_paise` deliberately sum only the balance-sheet
#: sides. Income and expense are shown in the breakdown but excluded from those
#: two totals, so they keep meaning what they meant before selling existed.
ACCOUNTS = [
    (GLAccount.INVENTORY, "Inventory (owned stock)", "asset"),
    (GLAccount.SOR_STOCK, "SOR / consignment stock", "asset"),
    (GLAccount.INPUT_GST, "Input GST", "asset"),
    (GLAccount.CASH, "Cash & bank", "asset"),
    (GLAccount.CARD_CLEARING, "Card, awaiting settlement", "asset"),
    (GLAccount.UPI_CLEARING, "UPI, awaiting settlement", "asset"),
    (GLAccount.PARTNER_RECEIVABLE, "Partner store receivable", "asset"),
    (GLAccount.VENDOR_PAYABLE, "Vendor payable", "liability"),
    (GLAccount.GRNI, "Goods received, not invoiced", "liability"),
    (GLAccount.SOR_CONTRA, "SOR contra (off-book)", "liability"),
    (GLAccount.OUTPUT_GST, "Output GST payable", "liability"),
    (GLAccount.CREDIT_NOTE_LIABILITY, "Credit notes outstanding", "liability"),
    (GLAccount.SUSPENSE, "Suspense", "liability"),
    (GLAccount.SALES_REVENUE, "Sales revenue", "income"),
    (GLAccount.COGS, "Cost of goods sold", "expense"),
    (GLAccount.ROUND_OFF, "Rounding", "expense"),
]


#: Display label per account code, off the one chart above.
ACCOUNT_LABELS = {code: label for code, label, _ in ACCOUNTS}


class BooksHealthView(APIView):
    """`GET /api/finledger/health` — trial balance + equation-of-state snapshot."""

    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def get(self, request: Request) -> Response:
        balances = {code: account_balance(code) for code, _, _ in ACCOUNTS}
        tb = trial_balance()
        assets = sum(balances[c] for c, _, side in ACCOUNTS if side == "asset")
        # liabilities/contra are credit-side (negative paise); present magnitude.
        liabilities = -sum(balances[c] for c, _, side in ACCOUNTS if side == "liability")
        return Response(
            {
                "balanced": tb == 0,
                "trial_balance_paise": tb,
                "trial_balance_rupees": paise_to_rupees_str(tb),
                "reconciliation": _reconciliation(balances),
                "stranded_stock_value": _stranded_stock_value(),
                "assets_paise": assets,
                "assets_rupees": paise_to_rupees_str(assets),
                "liabilities_paise": liabilities,
                "liabilities_rupees": paise_to_rupees_str(liabilities),
                "leg_count": GLEntry.objects.count(),
                "voucher_count": GLEntry.objects.values("doc_number").distinct().count(),
                "accounts": _accounts(balances),
            }
        )


def _reconciliation(balances: dict[str, int]) -> dict[str, Any]:
    """F1 subledger-reconciliation proof.

    The single value GL is the book of record, so its control accounts MUST equal
    the subledger running sums. Vendor payable is credit-side (−paise) against a
    positive "we owe" subledger, so it ties when Σ(vendor rows) == −GL payable.
    Cash is an asset (debit +), the same sign as its subledger.

    Cash is now more than one control account, and it is checked **per account**.
    A counter sale writes one cash-ledger receipt row per tender - cash into CASH,
    card into CARD, UPI into UPI - and the value GL sends the same three to CASH,
    CARD_CLEARING and UPI_CLEARING, because a card sale is money the customer has
    paid and the bank has not yet settled. `gl_control_for` is the one mapping both
    sides use. Comparing only the family total would tie perfectly while a card
    receipt sat in CASH, which is a settlement reconciliation quietly reading the
    wrong number; the totals are still reported for the screen, but `reconciled`
    is every account agreeing.
    """
    vendor_sub = VendorLedgerEntry.objects.aggregate(b=Sum("amount"))["b"] or 0
    gl_payable = balances[GLAccount.VENDOR_PAYABLE]
    vendor_reconciled = vendor_sub == -gl_payable
    cash = _cash_reconciliation(balances)
    return {
        "reconciled": vendor_reconciled and cash["reconciled"],
        "vendor": {
            "reconciled": vendor_reconciled,
            "subledger_paise": vendor_sub,
            "subledger_rupees": paise_to_rupees_str(vendor_sub),
            "gl_control_paise": -gl_payable,
            "gl_control_rupees": paise_to_rupees_str(-gl_payable),
            "drift_paise": vendor_sub + gl_payable,
        },
        "cash": cash,
    }


def _cash_reconciliation(balances: dict[str, int]) -> dict[str, Any]:
    """The cash subledger against each of its value control accounts."""
    subledger: dict[str, int] = dict.fromkeys(CASH_CONTROL_ACCOUNTS.values(), 0)
    for row in CashLedgerEntry.objects.values("account").annotate(total=Sum("amount")):
        subledger[gl_control_for(row["account"])] += row["total"] or 0
    pairs = sorted(subledger.items())
    by_account = [
        {
            "code": control,
            "label": ACCOUNT_LABELS.get(control, control),
            "subledger_paise": total,
            "subledger_rupees": paise_to_rupees_str(total),
            "gl_control_paise": balances[control],
            "gl_control_rupees": paise_to_rupees_str(balances[control]),
            "drift_paise": total - balances[control],
            "reconciled": total == balances[control],
        }
        for control, total in pairs
    ]
    total_sub = sum(total for _, total in pairs)
    total_gl = sum(balances[control] for control, _ in pairs)
    return {
        "reconciled": all(total == balances[control] for control, total in pairs),
        "subledger_paise": total_sub,
        "subledger_rupees": paise_to_rupees_str(total_sub),
        "gl_control_paise": total_gl,
        "gl_control_rupees": paise_to_rupees_str(total_gl),
        "drift_paise": total_sub - total_gl,
        "by_account": by_account,
    }


def _stranded_stock_value() -> dict[str, Any]:
    """Zero pieces still carrying rupees (#103).

    A movement that relieved quantity but not value. Every quantity-based
    reconciliation passes over it, including a physical count, because the piece
    count is right; only a value-per-piece read like this one ever sees it.
    """
    stranded_qs = StockOnHand.objects.filter(net_qty=0).exclude(net_value_paise=0)
    totals = stranded_qs.aggregate(rows=Count("id"), value=Sum("net_value_paise"))
    count = totals["rows"] or 0
    total = totals["value"] or 0
    rows = list(stranded_qs.select_related("store").order_by("-net_value_paise")[:STRANDED_SAMPLE])
    return {
        "clean": count == 0,
        "row_count": count,
        "value_paise": total,
        "value_rupees": paise_to_rupees_str(total),
        "rows": [
            {
                "store_code": row.store.code,
                "sku_code": row.sku_code,
                "value_paise": row.net_value_paise,
                "value_rupees": paise_to_rupees_str(row.net_value_paise),
            }
            for row in rows
        ],
    }


def _accounts(balances: dict[str, int]) -> list[dict[str, Any]]:
    """The equation of state, account by account, in the ratified display order."""
    return [
        {
            "code": code,
            "label": label,
            "side": side,
            "balance_paise": balances[code],
            "balance_rupees": paise_to_rupees_str(balances[code]),
        }
        for code, label, side in ACCOUNTS
    ]
