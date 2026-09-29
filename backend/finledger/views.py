"""Vendor & cash ledger API — paginated reads + posting actions (finance/owner only)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section, user_can
from accounts.sections import CAP_MANAGE
from core.money import paise_to_rupees_str
from core.textsearch import search_term, text_filter
from files.models import StoredFile, UploadTooLarge
from finledger.models import (
    BankStatementImport,
    BankStatementLine,
    CashLedgerEntry,
    VendorLedgerEntry,
)
from finledger.posting import (
    AlreadyReversedError,
    account_for_mode,
    post_cash_movement,
    post_vendor_bill,
    post_vendor_payment,
    reverse_cash_entry,
    reverse_vendor_entry,
    rupees_to_paise,
)
from finledger.reconciliation import UnsupportedFormat, parse_statement, process_import
from finledger.serializers import CashLedgerEntrySerializer, VendorLedgerEntrySerializer
from vendors.models import Vendor

if TYPE_CHECKING:
    from accounts.models import User

# Vendor/cash payables, balances and ageing are the books (ADR-0003) — gated by
# the SIDEBAR RBAC contract, not by a hand-kept role list. `money: manage` is the
# rung only Owner and Accounts hold: a store person or warehouse operator holds
# `money: operate` ("Expenses only"), which creates an expense but never opens
# the books, and Admin holds `none` (Sheet-1 note 2 — deliberately no Money).
#
# It was a role list until #87 browser-testing found `it_admin` sitting in it, so
# the sidebar hid Money from Admin while this API still served vendor payables.
# Reading the same `Role.section_access` the sidebar reads is what keeps the two
# from drifting again — and retuning it stays data, never a release (Rule 12).
IsBooksKeeper = require_section("money", CAP_MANAGE)


def _keeps_books(user: Any) -> bool:
    return user_can(user, "money", CAP_MANAGE)


class LedgerPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500


# --- Vendor ledger ---------------------------------------------------------


#: Vendor Ledger (#106) — the document number and the party (the vendor).
VENDOR_ENTRY_SEARCH_FIELDS = ("doc_number", "vendor__name", "vendor__code")


class VendorEntriesView(generics.ListAPIView[VendorLedgerEntry]):
    permission_classes = [IsAuthenticated, IsBooksKeeper]
    serializer_class = VendorLedgerEntrySerializer
    pagination_class = LedgerPagination

    def get_queryset(self) -> Any:
        qs = VendorLedgerEntry.objects.select_related("vendor")
        vendor = self.request.query_params.get("vendor")
        if vendor:
            qs = qs.filter(vendor_id=vendor)
        # The screen's own search box (#102), applied last.
        return text_filter(qs, search_term(self.request), VENDOR_ENTRY_SEARCH_FIELDS)


class VendorBalancesView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def get(self, request: Request) -> Response:
        rows = (
            VendorLedgerEntry.objects.values("vendor_id", "vendor__code", "vendor__name")
            .annotate(outstanding=Sum("amount"), entries=Count("id"))
            .order_by("-outstanding")
        )
        total = 0
        out = []
        for r in rows:
            bal = r["outstanding"] or 0
            total += bal
            if bal != 0:
                out.append(
                    {
                        "vendor_id": r["vendor_id"],
                        "vendor_code": r["vendor__code"],
                        "vendor_name": r["vendor__name"],
                        "outstanding_paise": bal,
                        "outstanding_rupees": paise_to_rupees_str(bal),
                        "entries": r["entries"],
                    }
                )
        return Response(
            {
                "total_payable_paise": total,
                "total_payable_rupees": paise_to_rupees_str(total),
                "vendors_with_dues": len(out),
                "rows": out,
            }
        )


def _bucket_for_days(days: int) -> str:
    if days <= 30:
        return "bucket_0_30"
    if days <= 60:
        return "bucket_31_60"
    return "bucket_60_plus"


def _open_vendor_lots(entries: list[VendorLedgerEntry]) -> list[dict[str, Any]]:
    lots: list[dict[str, Any]] = []
    for entry in entries:
        amount = int(entry.amount or 0)
        if amount > 0:
            lots.append(
                {
                    "amount": amount,
                    "created_at": entry.created_at,
                    "doc_number": entry.doc_number,
                    "reference": entry.reference,
                }
            )
            continue
        remaining = abs(amount)
        while remaining > 0 and lots:
            if lots[0]["amount"] <= remaining:
                remaining -= lots[0]["amount"]
                lots.pop(0)
            else:
                lots[0]["amount"] -= remaining
                remaining = 0
    return lots


class VendorAgeingView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def get(self, request: Request) -> Response:
        grouped: dict[int, list[VendorLedgerEntry]] = defaultdict(list)
        entries = VendorLedgerEntry.objects.select_related("vendor").order_by(
            "vendor_id", "created_at", "id"
        )
        for entry in entries:
            grouped[entry.vendor_id].append(entry)

        today = timezone.localdate()
        totals = {"bucket_0_30": 0, "bucket_31_60": 0, "bucket_60_plus": 0}
        rows = []
        for vendor_id, vendor_entries in grouped.items():
            first = vendor_entries[0]
            buckets = {"bucket_0_30": 0, "bucket_31_60": 0, "bucket_60_plus": 0}
            oldest_days = 0
            open_lots = _open_vendor_lots(vendor_entries)
            for lot in open_lots:
                local_date = timezone.localtime(lot["created_at"]).date()
                age_days = max((today - local_date).days, 0)
                oldest_days = max(oldest_days, age_days)
                bucket = _bucket_for_days(age_days)
                buckets[bucket] += lot["amount"]
                totals[bucket] += lot["amount"]
            total_due = sum(buckets.values())
            if total_due <= 0:
                continue
            rows.append(
                {
                    "vendor_id": vendor_id,
                    "vendor_code": first.vendor.code,
                    "vendor_name": first.vendor.name,
                    "payment_terms": first.vendor.payment_terms,
                    "oldest_days": oldest_days,
                    "bucket_0_30_paise": buckets["bucket_0_30"],
                    "bucket_0_30_rupees": paise_to_rupees_str(buckets["bucket_0_30"]),
                    "bucket_31_60_paise": buckets["bucket_31_60"],
                    "bucket_31_60_rupees": paise_to_rupees_str(buckets["bucket_31_60"]),
                    "bucket_60_plus_paise": buckets["bucket_60_plus"],
                    "bucket_60_plus_rupees": paise_to_rupees_str(buckets["bucket_60_plus"]),
                    "total_due_paise": total_due,
                    "total_due_rupees": paise_to_rupees_str(total_due),
                    "open_bill_count": len(open_lots),
                }
            )

        rows.sort(key=lambda r: (r["bucket_60_plus_paise"], r["total_due_paise"]), reverse=True)
        total_due = sum(totals.values())
        return Response(
            {
                "as_of": today.isoformat(),
                "total_due_paise": total_due,
                "total_due_rupees": paise_to_rupees_str(total_due),
                "bucket_0_30_paise": totals["bucket_0_30"],
                "bucket_0_30_rupees": paise_to_rupees_str(totals["bucket_0_30"]),
                "bucket_31_60_paise": totals["bucket_31_60"],
                "bucket_31_60_rupees": paise_to_rupees_str(totals["bucket_31_60"]),
                "bucket_60_plus_paise": totals["bucket_60_plus"],
                "bucket_60_plus_rupees": paise_to_rupees_str(totals["bucket_60_plus"]),
                "rows": rows,
            }
        )


class VendorBillView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def post(self, request: Request) -> Response:
        if not _keeps_books(request.user):
            return Response({"detail": "Not permitted."}, status=403)
        vendor_id: Any = request.data.get("vendor_id")
        vendor = Vendor.objects.filter(pk=vendor_id).first()
        if not vendor:
            return Response({"detail": "vendor_id is required / invalid."}, status=400)
        amount = rupees_to_paise(request.data.get("amount"))
        if amount <= 0:
            return Response({"detail": "A positive amount is required."}, status=400)
        entry = post_vendor_bill(
            vendor,
            amount,
            request.data.get("description", ""),
            request.user,
            reference=request.data.get("reference", ""),
        )
        return Response(VendorLedgerEntrySerializer(entry).data, status=201)


class VendorPaymentView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def post(self, request: Request) -> Response:
        if not _keeps_books(request.user):
            return Response({"detail": "Not permitted."}, status=403)
        vendor_id: Any = request.data.get("vendor_id")
        vendor = Vendor.objects.filter(pk=vendor_id).first()
        if not vendor:
            return Response({"detail": "vendor_id is required / invalid."}, status=400)
        amount = rupees_to_paise(request.data.get("amount"))
        if amount <= 0:
            return Response({"detail": "A positive amount is required."}, status=400)
        mode = request.data.get("mode", "cash")
        entry = post_vendor_payment(
            vendor,
            amount,
            request.data.get("description", ""),
            request.user,
            mode=mode,
            # See `account_for_mode` — buckets by *how* the vendor was paid
            # instead of trusting a request-supplied `account` that no caller
            # actually sends.
            account=account_for_mode(mode),
            also_cash=request.data.get("also_cash", True),
        )
        return Response(VendorLedgerEntrySerializer(entry).data, status=201)


class VendorReverseView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def post(self, request: Request, pk: int) -> Response:
        if not _keeps_books(request.user):
            return Response({"detail": "Not permitted."}, status=403)
        entry = VendorLedgerEntry.objects.filter(pk=pk).first()
        if not entry:
            return Response({"detail": "Not found."}, status=404)
        try:
            rev = reverse_vendor_entry(entry, request.user)
        except AlreadyReversedError as exc:
            return Response({"detail": str(exc)}, status=409)
        return Response(VendorLedgerEntrySerializer(rev).data, status=201)


# --- Cash ledger -----------------------------------------------------------


#: Cash Ledger (#106) — the document number and the party (a cash-out that pays
#: a vendor carries one; a plain expense doesn't, and the description covers it).
CASH_ENTRY_SEARCH_FIELDS = ("doc_number", "description", "vendor__name")


class CashEntriesView(generics.ListAPIView[CashLedgerEntry]):
    permission_classes = [IsAuthenticated, IsBooksKeeper]
    serializer_class = CashLedgerEntrySerializer
    pagination_class = LedgerPagination

    def get_queryset(self) -> Any:
        qs = CashLedgerEntry.objects.select_related("vendor")
        account = self.request.query_params.get("account")
        if account:
            qs = qs.filter(account=account)
        # The screen's own search box (#102), applied last.
        return text_filter(qs, search_term(self.request), CASH_ENTRY_SEARCH_FIELDS)


class CashDailyView(APIView):
    """A day's money in vs out, split by account (Cash/Bank/Card/UPI) — the
    dashboard Accounts asked for directly (`KDPS Daily Work Survey - Accounts
    Team.pdf`): a same-day read of what moved, rather than only finding out at
    month-end DSR reconciliation. Reads the same `CashLedgerEntry` rows the
    Cash Ledger (#106) already lists; this just slices them to one day and
    splits the signed `amount` into in/out instead of netting it into one
    running balance."""

    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def get(self, request: Request) -> Response:
        day_str = request.query_params.get("date")
        try:
            day = date.fromisoformat(day_str) if day_str else timezone.localdate()
        except ValueError:
            return Response({"detail": "date must be YYYY-MM-DD."}, status=400)

        qs = (
            CashLedgerEntry.objects.select_related("vendor")
            .filter(created_at__date=day)
            .order_by("-created_at")
        )

        by_account: dict[str, dict[str, int]] = {
            choice: {"in_paise": 0, "out_paise": 0} for choice in CashLedgerEntry.Account.values
        }
        for e in qs:
            bucket = by_account.setdefault(e.account, {"in_paise": 0, "out_paise": 0})
            if e.amount >= 0:
                bucket["in_paise"] += e.amount
            else:
                bucket["out_paise"] += -e.amount

        money_in = sum(a["in_paise"] for a in by_account.values())
        money_out = sum(a["out_paise"] for a in by_account.values())
        return Response(
            {
                "date": day.isoformat(),
                "money_in_paise": money_in,
                "money_out_paise": money_out,
                "net_paise": money_in - money_out,
                "accounts": [
                    {
                        "account": account,
                        "in_paise": a["in_paise"],
                        "out_paise": a["out_paise"],
                        "net_paise": a["in_paise"] - a["out_paise"],
                    }
                    for account, a in by_account.items()
                ],
                "entries": CashLedgerEntrySerializer(qs, many=True).data,
            }
        )


class CashSummaryView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def get(self, request: Request) -> Response:
        rows = (
            CashLedgerEntry.objects.values("account")
            .annotate(balance=Sum("amount"), entries=Count("id"))
            .order_by("account")
        )
        total = 0
        out = []
        for r in rows:
            bal = r["balance"] or 0
            total += bal
            out.append(
                {
                    "account": r["account"],
                    "balance_paise": bal,
                    "balance_rupees": paise_to_rupees_str(bal),
                    "entries": r["entries"],
                }
            )
        return Response(
            {
                "total_paise": total,
                "total_rupees": paise_to_rupees_str(total),
                "accounts": out,
            }
        )


class CashMovementView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def post(self, request: Request) -> Response:
        if not _keeps_books(request.user):
            return Response({"detail": "Not permitted."}, status=403)
        direction = request.data.get("direction")
        if direction not in ("in", "out"):
            return Response({"detail": "direction must be 'in' or 'out'."}, status=400)
        amount = rupees_to_paise(request.data.get("amount"))
        if amount <= 0:
            return Response({"detail": "A positive amount is required."}, status=400)
        entry = post_cash_movement(
            direction,
            amount,
            request.data.get("description", ""),
            request.user,
            account=request.data.get("account", "CASH"),
            mode=request.data.get("mode", ""),
        )
        return Response(CashLedgerEntrySerializer(entry).data, status=201)


class CashReverseView(APIView):
    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def post(self, request: Request, pk: int) -> Response:
        if not _keeps_books(request.user):
            return Response({"detail": "Not permitted."}, status=403)
        entry = CashLedgerEntry.objects.filter(pk=pk).first()
        if not entry:
            return Response({"detail": "Not found."}, status=404)
        try:
            rev = reverse_cash_entry(entry, request.user)
        except AlreadyReversedError as exc:
            return Response({"detail": str(exc)}, status=409)
        return Response(CashLedgerEntrySerializer(rev).data, status=201)


def _line_dict(line: BankStatementLine) -> dict[str, Any]:
    return {
        "id": line.id,
        "import_id": line.import_batch_id,
        "txn_date": line.txn_date,
        "narration": line.narration,
        "debit_paise": line.debit_paise,
        "credit_paise": line.credit_paise,
        "balance_paise": line.balance_paise,
        "status": line.status,
        "match_confidence": line.match_confidence,
        "candidates": line.candidates,
        "matched_entry_id": line.matched_entry_id,
        "matched_entry_doc_number": line.matched_entry.doc_number if line.matched_entry else None,
        "matched_by_name": (
            (line.matched_by.full_name or line.matched_by.username) if line.matched_by else ""
        ),
    }


class BankStatementImportsView(APIView):
    """Upload a bank statement (`money: manage`) — parsed and auto-matched in
    one shot (`finledger.reconciliation`) — or list past uploads
    (`money: view`, same rung as reading the cash ledger these lines offset)."""

    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def get(self, request: Request) -> Response:
        batches = BankStatementImport.objects.select_related("file", "uploaded_by").order_by(
            "-created_at"
        )[:100]
        return Response(
            {
                "rows": [
                    {
                        "id": b.id,
                        "created_at": b.created_at,
                        "filename": b.file.filename,
                        "bank_label": b.bank_label,
                        "row_count": b.row_count,
                        "matched_count": b.matched_count,
                        "uploaded_by_name": (
                            (b.uploaded_by.full_name or b.uploaded_by.username)
                            if b.uploaded_by
                            else ""
                        ),
                    }
                    for b in batches
                ]
            }
        )

    def post(self, request: Request) -> Response:
        if not _keeps_books(request.user):
            return Response({"detail": "Not permitted."}, status=403)
        upload = request.FILES.get("file")
        if not upload:
            return Response({"detail": "A file is required."}, status=400)
        try:
            stored = StoredFile.from_upload(upload, StoredFile.Kind.BANK_STATEMENT, request.user)
        except UploadTooLarge as exc:
            return Response({"detail": str(exc)}, status=413)
        try:
            rows = parse_statement(bytes(stored.content), stored.filename, stored.content_type)
        except UnsupportedFormat as exc:
            stored.delete()
            return Response({"detail": str(exc)}, status=400)
        batch = BankStatementImport.objects.create(
            file=stored,
            bank_label=request.data.get("bank_label", ""),
            # Every writer on this view is `IsAuthenticated` + `money: manage`
            # gated, so the actor is a real person, never `AnonymousUser`.
            uploaded_by=cast("User", request.user),
        )
        process_import(batch, rows)
        return Response(
            {
                "id": batch.id,
                "filename": stored.filename,
                "row_count": batch.row_count,
                "matched_count": batch.matched_count,
            },
            status=201,
        )


class BankStatementLinesView(APIView):
    """A batch's rows, optionally narrowed to one status — the review queue
    Accounts works through after an upload."""

    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def get(self, request: Request, import_id: int) -> Response:
        qs = BankStatementLine.objects.select_related("matched_entry", "matched_by").filter(
            import_batch_id=import_id
        )
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response({"rows": [_line_dict(line) for line in qs]})


class BankStatementLineMatchView(APIView):
    """Accounts' say on one line once the matcher's own guess isn't good
    enough on its own: link it to a specific `CashLedgerEntry` (from the
    cached `candidates` or picked by id another way), unlink a wrong
    auto-match, or mark it `ignored` (a bank charge/interest row with no
    ledger counterpart at all)."""

    permission_classes = [IsAuthenticated, IsBooksKeeper]

    def post(self, request: Request, pk: int) -> Response:
        if not _keeps_books(request.user):
            return Response({"detail": "Not permitted."}, status=403)
        line = BankStatementLine.objects.filter(pk=pk).first()
        if not line:
            return Response({"detail": "Not found."}, status=404)

        action = request.data.get("action", "link")
        if action == "ignore":
            line.status = BankStatementLine.Status.IGNORED
            line.matched_entry = None
            line.matched_by = cast("User", request.user)
        elif action == "unlink":
            line.status = (
                BankStatementLine.Status.REVIEW
                if line.candidates
                else BankStatementLine.Status.UNMATCHED
            )
            line.matched_entry = None
            line.matched_by = None
        else:
            entry_id: Any = request.data.get("entry_id")
            entry = CashLedgerEntry.objects.filter(pk=entry_id).first()
            if not entry:
                return Response(
                    {"detail": "entry_id is required and must be a cash ledger entry."}, status=400
                )
            if BankStatementLine.objects.filter(matched_entry=entry).exclude(pk=line.pk).exists():
                return Response(
                    {"detail": "That cash ledger entry is already matched to another line."},
                    status=409,
                )
            line.status = BankStatementLine.Status.MATCHED
            line.matched_entry = entry
            line.matched_by = cast("User", request.user)
        line.save(update_fields=["status", "matched_entry", "matched_by"])
        return Response(_line_dict(line))
