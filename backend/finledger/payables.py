"""Brand payables for outright brands (store operations PRD ST-MNY-4, ticket 28).

Per outright brand or vendor: what is owed, what is paid, what is due and how old
it is (overall PRD R-FIN-010).

* **Owed** is the vendor's tax invoices, amount including tax, that Accounts
  records on Money > Payables: which vendor, brand, store and season, the
  invoice's number, date and total. Nothing is worked out from receiving: the
  vendor's claim at the dock holds no tax, and no liability is assumed (R-FIN-010).
* **The model** is read once, when the invoice is recorded, from the brand's terms
  in force on the invoice date for that season (ticket 23), and kept on the
  invoice. Outright counts here. SOR, consignment and concession are refused: what
  is owed for them arises on sale and comes later (P4, after OQ-26). A brand with
  no recorded model is taken, kept apart as **unknown** and never counted with
  the outright figures (D9).
* **Due** is the invoice date plus the payment days in those terms. Terms that
  leave payment days blank leave the due date unknown; it is never guessed.
* **Paid** is payments Accounts records, made outside the system, to one vendor
  at one store and spread over that vendor's open invoices there, any brand:
  payables accrue per brand, payments anchor to the vendor (``system-dumb.md``
  §21.2). No bank file, payment run or posting is made here.
* **Ageing** is what is still unpaid, in bands of days past its due date.

A mistake is put right by cancelling, with a reason: an invoice with nothing paid
against it, or a payment. Nothing is edited or deleted.

The rules (due date, bands, the summary) are pure; the steps take a running
command, so a record and its audit record are written together or not at all.
Each step is switched per store with ticket 01's switch.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.db.models import Sum
from django.db.models.functions import Lower, Trim

from accounts.permissions import user_can
from accounts.role_lists import PAYABLE_EDITOR_ROLES
from accounts.sections import CAP_MANAGE
from core.commands import CommandRun, LockRank
from core.goods_money import MoneyInvalid, paise_from_json
from core.refusals import Refusal, issue
from finledger.payable_models import PayableAllocation, PayableInvoice, PayablePayment
from masters.brand_terms import terms_for
from masters.models import Brand, Season, Store
from masters.scoping import actionable_store_ids
from masters.store_feature_registry import BRAND_PAYABLES
from masters.store_features import require_feature
from sell.services.petty_cash import rupees as petty_cash_rupees
from vendors.models import Vendor, VendorBrand

FEATURE_KEY = BRAND_PAYABLES

#: Audit actions, one per step. Subjects are ``payable_invoice:<id>`` and
#: ``payable_payment:<id>``.
RECORD_INVOICE_ACTION = "finledger.payable_invoice.record"
CANCEL_INVOICE_ACTION = "finledger.payable_invoice.cancel"
RECORD_PAYMENT_ACTION = "finledger.payable_payment.record"
CANCEL_PAYMENT_ACTION = "finledger.payable_payment.cancel"

NOTE_LENGTH = 240
NUMBER_LENGTH = 60
MAX_PAISE = 100_000_000_000  # Rs 100 crore
MAX_ALLOCATIONS = 200

#: The models a payable invoice is kept under.
OUTRIGHT = "outright"
UNKNOWN = "unknown"

#: Ageing bands: days past the due date, as of today (India).
NOT_DUE = "not_due"
DAYS_0_30 = "days_0_30"
DAYS_31_60 = "days_31_60"
DAYS_61_90 = "days_61_90"
OVER_90 = "over_90"
DUE_UNKNOWN = "due_unknown"
BANDS = (NOT_DUE, DAYS_0_30, DAYS_31_60, DAYS_61_90, OVER_90, DUE_UNKNOWN)
#: The bands that are due: the due date is today or has passed.
DUE_BANDS = (DAYS_0_30, DAYS_31_60, DAYS_61_90, OVER_90)

#: How the summary is grouped.
BY_BRAND = "brand"
BY_VENDOR = "vendor"
GROUPS = (BY_BRAND, BY_VENDOR)


# ---------------------------------------------------------------------------
# The rules, without a database
# ---------------------------------------------------------------------------


def due_date(invoice_date: date, payment_days: int | None) -> date | None:
    """The day an invoice falls due; unknown where the terms leave payment days blank."""
    if payment_days is None:
        return None
    return invoice_date + timedelta(days=payment_days)


def days_late(due: date | None, today: date) -> int | None:
    """Days past the due date: 0 on the day it falls due, negative before it."""
    if due is None:
        return None
    return (today - due).days


def band(due: date | None, today: date) -> str:
    """The ageing band of an unpaid amount. Due today counts as due."""
    late = days_late(due, today)
    if late is None:
        return DUE_UNKNOWN
    if late < 0:
        return NOT_DUE
    if late <= 30:
        return DAYS_0_30
    if late <= 60:
        return DAYS_31_60
    if late <= 90:
        return DAYS_61_90
    return OVER_90


def rupees(paise: int) -> str:
    """Rupees in Indian grouping (Rs 12,34,567.89), for refusals."""
    return petty_cash_rupees(paise)


@dataclass(frozen=True)
class Figures:
    """One invoice as the summary reads it."""

    invoice_id: int
    brand_id: int
    brand_name: str
    vendor_id: int
    vendor_name: str
    model: str
    invoice_date: date
    due_date: date | None
    owed_paise: int
    paid_paise: int

    @property
    def outstanding_paise(self) -> int:
        return self.owed_paise - self.paid_paise


def _empty_bands() -> dict[str, int]:
    return dict.fromkeys(BANDS, 0)


@dataclass
class Position:
    """What one brand or vendor is owed, paid, due and how old it is."""

    key: int
    label: str
    owed_paise: int = 0
    paid_paise: int = 0
    bands: dict[str, int] = field(default_factory=_empty_bands)
    open_invoices: int = 0
    #: The most days past due of any unpaid invoice; None where nothing is due.
    oldest_days_late: int | None = None

    @property
    def outstanding_paise(self) -> int:
        return self.owed_paise - self.paid_paise

    @property
    def due_paise(self) -> int:
        return sum(self.bands[name] for name in DUE_BANDS)

    def add(self, figures: Figures, today: date) -> None:
        self.owed_paise += figures.owed_paise
        self.paid_paise += figures.paid_paise
        left = figures.outstanding_paise
        if left <= 0:
            return
        self.open_invoices += 1
        self.bands[band(figures.due_date, today)] += left
        late = days_late(figures.due_date, today)
        if late is not None and late >= 0:
            self.oldest_days_late = max(self.oldest_days_late or 0, late)


def summarise(
    invoices: Iterable[Figures], today: date, *, group: str
) -> tuple[list[Position], Position, list[Position]]:
    """Outright invoices per brand or vendor, their totals, and the invoices of
    brands with no recorded model, per brand and kept out of the totals."""
    rows: dict[int, Position] = {}
    unknown: dict[int, Position] = {}
    totals = Position(key=0, label="Total")
    for figures in invoices:
        if figures.model == UNKNOWN:
            row = unknown.get(figures.brand_id)
            if row is None:
                row = unknown[figures.brand_id] = Position(figures.brand_id, figures.brand_name)
            row.add(figures, today)
            continue
        key, label = (
            (figures.vendor_id, figures.vendor_name)
            if group == BY_VENDOR
            else (figures.brand_id, figures.brand_name)
        )
        row = rows.get(key)
        if row is None:
            row = rows[key] = Position(key, label)
        row.add(figures, today)
        totals.add(figures, today)
    ordered = sorted(
        rows.values(),
        key=lambda r: (-(r.oldest_days_late or 0), -r.due_paise, -r.outstanding_paise, r.label),
    )
    return ordered, totals, sorted(unknown.values(), key=lambda r: r.label)


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


def _role(user: Any) -> str:
    return str(getattr(getattr(user, "role", None), "code", "") or "")


def may_read(user: Any) -> bool:
    """Owner and Accounts: ``money: manage``. Store roles never see payables."""
    if getattr(user, "is_superuser", False):
        return True
    return user_can(user, "money", CAP_MANAGE)


def may_edit(user: Any) -> bool:
    """Accounts (``money: manage`` narrowed to the declared editors)."""
    if getattr(user, "is_superuser", False):
        return True
    return may_read(user) and _role(user) in PAYABLE_EDITOR_ROLES


def readable_stores(user: Any, tenant_id: Any) -> list[Store]:
    """The stores whose payables this person reads, in their own company."""
    if not may_read(user):
        return []
    ids = actionable_store_ids(user)
    rows = Store.objects.filter(tenant_id=tenant_id).order_by("code")
    return list(rows if ids is None else rows.filter(pk__in=ids))


def readable_invoices(user: Any, tenant_id: Any) -> Any:
    ids = [store.pk for store in readable_stores(user, tenant_id)]
    return (
        PayableInvoice.objects.filter(tenant_id=tenant_id, store_id__in=ids)
        .select_related("store", "vendor", "brand", "season", "recorded_by", "cancelled_by")
        .order_by("invoice_date", "id")
    )


def readable_payments(user: Any, tenant_id: Any) -> Any:
    ids = [store.pk for store in readable_stores(user, tenant_id)]
    return (
        PayablePayment.objects.filter(tenant_id=tenant_id, store_id__in=ids)
        .select_related("store", "vendor", "recorded_by", "cancelled_by")
        .prefetch_related("allocations")
        .order_by("-paid_on", "-id")
    )


# ---------------------------------------------------------------------------
# Reading what is owed and paid
# ---------------------------------------------------------------------------


def paid_by_invoice(invoice_ids: Iterable[int]) -> dict[int, int]:
    """What recorded (not cancelled) payments put against each invoice."""
    ids = list(invoice_ids)
    if not ids:
        return {}
    rows = (
        PayableAllocation.objects.filter(
            invoice_id__in=ids, payment__status=PayablePayment.Status.RECORDED
        )
        .values("invoice_id")
        .annotate(paid=Sum("amount_paise"))
    )
    return {row["invoice_id"]: int(row["paid"] or 0) for row in rows}


def figures_of(invoices: Iterable[PayableInvoice], paid: dict[int, int]) -> list[Figures]:
    """Open invoices as the summary reads them, with what ``paid_by_invoice`` says
    was paid on each; a cancelled one owes nothing."""
    rows = [inv for inv in invoices if inv.status == PayableInvoice.Status.OPEN]
    return [
        Figures(
            invoice_id=inv.pk,
            brand_id=inv.brand_id,
            brand_name=inv.brand.name,
            vendor_id=inv.vendor_id,
            vendor_name=inv.vendor.name,
            model=inv.model,
            invoice_date=inv.invoice_date,
            due_date=inv.due_date,
            owed_paise=int(inv.amount_paise),
            paid_paise=paid.get(inv.pk, 0),
        )
        for inv in rows
    ]


def invoice_snapshot(invoice: PayableInvoice) -> dict[str, Any]:
    """The invoice as its audit records hold it. Money is whole paise, as text."""
    return {
        "store_id": invoice.store_id,
        "vendor_id": invoice.vendor_id,
        "brand_id": invoice.brand_id,
        "season_id": invoice.season_id,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.isoformat(),
        "amount_paise": str(invoice.amount_paise),
        "model": invoice.model,
        "terms_version_id": str(invoice.terms_version_id) if invoice.terms_version_id else None,
        "payment_days": invoice.payment_days,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "note": invoice.note,
        "status": invoice.status,
        "cancel_reason": invoice.cancel_reason,
    }


def payment_snapshot(payment: PayablePayment) -> dict[str, Any]:
    return {
        "store_id": payment.store_id,
        "vendor_id": payment.vendor_id,
        "paid_on": payment.paid_on.isoformat(),
        "amount_paise": str(payment.amount_paise),
        "mode": payment.mode,
        "reference": payment.reference,
        "note": payment.note,
        "status": payment.status,
        "cancel_reason": payment.cancel_reason,
        "allocations": [
            {"invoice_id": row.invoice_id, "amount_paise": str(row.amount_paise)}
            for row in payment.allocations.order_by("invoice_id")
        ],
    }


# ---------------------------------------------------------------------------
# Checking what was typed
# ---------------------------------------------------------------------------


def _refuse(problems: list[dict[str, Any]]) -> None:
    if problems:
        raise Refusal(
            "INVALID_REQUEST", " ".join(str(p["message"]) for p in problems), issues=problems
        )


def _text(
    value: Any,
    field_name: str,
    label: str,
    problems: list[dict[str, Any]],
    *,
    required: bool,
    length: int = NOTE_LENGTH,
) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        problems.append(issue("REQUIRED", f"Type {label}.", field=field_name))
    elif len(text) > length:
        problems.append(issue("TOO_LONG", f"At most {length} characters.", field=field_name))
    return text


def _money(value: Any, field_name: str, problems: list[dict[str, Any]]) -> int:
    try:
        paise = int(paise_from_json(value, maximum=MAX_PAISE) or 0)
    except MoneyInvalid:
        paise = 0
    if paise <= 0:
        problems.append(
            issue(
                "INVALID",
                "An amount is whole paise written as text, more than 0.",
                field=field_name,
            )
        )
    return paise


def _day(
    value: Any, field_name: str, label: str, today: date, problems: list[dict[str, Any]]
) -> date | None:
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        problems.append(issue("INVALID", f"{label} must be a date.", field=field_name))
        return None
    if day > today:
        problems.append(issue("FUTURE", f"{label} cannot be after today.", field=field_name))
    return day


def _int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refusal(
            "INVALID_REQUEST",
            f"{field_name} is an id.",
            issues=[issue("INVALID", f"{field_name} is an integer id", field=field_name)],
        )
    return int(value)


# ---------------------------------------------------------------------------
# Locks and audit
# ---------------------------------------------------------------------------


def _guard(run: CommandRun, vendor_id: int) -> None:
    """Everything owed to or paid to one vendor is changed in turn."""
    run.advisory_lock(LockRank.DOCUMENT, [f"payables:vendor:{vendor_id}"])


def _check_revision(row: Any, expected_revision: int | None) -> None:
    if expected_revision is not None and expected_revision != row.revision:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Someone changed this after you loaded it. Reload it and try again.",
        )


def _audit(
    run: CommandRun,
    subject: str,
    site_id: int,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    run.audit_subject_key = subject
    run.audit_site_id = site_id
    run.audit_before = before
    run.audit_after = after


def _store(user: Any, tenant_id: Any, store_id: Any) -> Store:
    sid = _int(store_id, "store_id")
    store = next((s for s in readable_stores(user, tenant_id) if s.pk == sid), None)
    if store is None:
        raise Refusal("NOT_FOUND", "That store was not found.")
    return store


def _vendor(tenant_id: Any, vendor_id: Any, *, active: bool) -> Vendor:
    """A vendor of this company. New invoices need an active one; what is already
    owed to a vendor since retired can still be paid."""
    rows = Vendor.objects.filter(tenant_id=tenant_id, pk=_int(vendor_id, "vendor_id"))
    vendor: Vendor | None = (rows.filter(is_active=True) if active else rows).first()
    if vendor is None:
        raise Refusal("NOT_FOUND", "That vendor was not found.")
    return vendor


# ---------------------------------------------------------------------------
# Recording an invoice
# ---------------------------------------------------------------------------


def record_invoice(
    run: CommandRun,
    *,
    user: Any,
    tenant_id: Any,
    body: dict[str, Any],
    today: date,
) -> PayableInvoice:
    """Record one vendor tax invoice of one brand, received at one store.

    The model and payment days come from the brand's terms in force on the
    invoice date for its season, as approved now, and are kept on the invoice.
    SOR, consignment and concession are refused; no approved model is kept as
    unknown and listed apart.
    """
    store = _store(user, tenant_id, body.get("store_id"))
    require_feature(store, FEATURE_KEY)
    vendor = _vendor(tenant_id, body.get("vendor_id"), active=True)
    brand: Brand | None = Brand.objects.filter(
        tenant_id=tenant_id, pk=_int(body.get("brand_id"), "brand_id"), is_active=True
    ).first()
    if brand is None:
        raise Refusal("NOT_FOUND", "That brand was not found.")
    season: Season | None = Season.objects.filter(
        pk=_int(body.get("season_id"), "season_id"), historical_unknown=False
    ).first()
    if season is None:
        raise Refusal("NOT_FOUND", "That season was not found.")
    if not VendorBrand.objects.filter(vendor=vendor, brand=brand).exists():
        raise Refusal(
            "STATE_CONFLICT",
            f"{vendor.name} does not supply {brand.name}. Link the vendor to the brand in "
            "Setup, Vendors, first.",
            domain_code="VENDOR_NOT_BRAND",
        )
    problems: list[dict[str, Any]] = []
    number = _text(
        body.get("invoice_number"),
        "invoice_number",
        "the vendor's invoice number",
        problems,
        required=True,
        length=NUMBER_LENGTH,
    )
    day = _day(body.get("invoice_date"), "invoice_date", "The invoice date", today, problems)
    amount = _money(body.get("amount_paise"), "amount_paise", problems)
    note = _text(body.get("note"), "note", "a note", problems, required=False)
    _refuse(problems)
    assert day is not None

    terms = terms_for(tenant_id, brand.pk, season.pk, day, run.now)
    if terms is not None and terms.model != OUTRIGHT:
        raise Refusal(
            "STATE_CONFLICT",
            f"{brand.name} is {terms.model.upper()} for {season.code} on that date. Only "
            "outright brands' invoices are recorded here; SOR and consignment payables "
            "come later (P4).",
            domain_code="NOT_OUTRIGHT",
        )
    _guard(run, vendor.pk)
    taken = (
        PayableInvoice.objects.filter(
            tenant_id=tenant_id, vendor=vendor, status=PayableInvoice.Status.OPEN
        )
        .annotate(key=Lower(Trim("invoice_number")))
        .filter(key=number.lower())
        .exists()
    )
    if taken:
        raise Refusal(
            "STATE_CONFLICT",
            f"Invoice {number} from {vendor.name} is recorded already.",
            domain_code="DUPLICATE_INVOICE",
        )
    days = None if terms is None else terms.payment_days
    invoice = PayableInvoice.objects.create(
        store=store,
        vendor=vendor,
        brand=brand,
        season=season,
        invoice_number=number,
        invoice_date=day,
        amount_paise=amount,
        model=OUTRIGHT if terms is not None else UNKNOWN,
        terms_version_id=None if terms is None else terms.id,
        payment_days=days,
        due_date=due_date(day, days),
        note=note,
        recorded_by=user,
    )
    _audit(run, f"payable_invoice:{invoice.pk}", store.pk, None, invoice_snapshot(invoice))
    return invoice


def _lock_invoice(run: CommandRun, invoice_id: int) -> PayableInvoice:
    vendor_id = (
        PayableInvoice.objects.filter(pk=invoice_id).values_list("vendor_id", flat=True).first()
    )
    if vendor_id is None:
        raise Refusal("NOT_FOUND", "That invoice was not found.")
    _guard(run, vendor_id)
    invoice: PayableInvoice = run.lock(
        LockRank.DOCUMENT, PayableInvoice.objects.filter(pk=invoice_id)
    )[0]
    return invoice


def cancel_invoice(
    run: CommandRun,
    invoice_id: int,
    *,
    user: Any,
    reason: Any,
    expected_revision: int | None,
) -> PayableInvoice:
    """Cancel an invoice recorded by mistake. One with payments against it is
    refused: cancel those payments first."""
    invoice = _lock_invoice(run, invoice_id)
    _check_revision(invoice, expected_revision)
    require_feature(invoice.store_id, FEATURE_KEY)
    if invoice.status != PayableInvoice.Status.OPEN:
        raise Refusal("STATE_CONFLICT", "This invoice is cancelled already.")
    if paid_by_invoice([invoice.pk]).get(invoice.pk, 0):
        raise Refusal(
            "STATE_CONFLICT",
            "Payments are recorded against this invoice. Cancel those payments first.",
            domain_code="INVOICE_PAID",
        )
    problems: list[dict[str, Any]] = []
    text = _text(reason, "reason", "why it is cancelled", problems, required=True)
    _refuse(problems)
    before = invoice_snapshot(invoice)
    invoice.status = PayableInvoice.Status.CANCELLED
    invoice.cancel_reason = text
    invoice.cancelled_by = user
    invoice.cancelled_at = run.now
    invoice.revision += 1
    invoice.save()
    _audit(
        run, f"payable_invoice:{invoice.pk}", invoice.store_id, before, invoice_snapshot(invoice)
    )
    return invoice


# ---------------------------------------------------------------------------
# Recording a payment
# ---------------------------------------------------------------------------


def _allocations(value: Any) -> list[tuple[int, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ALLOCATIONS:
        raise Refusal(
            "INVALID_REQUEST",
            "Say which invoices the payment pays, and how much of each.",
            issues=[
                issue(
                    "REQUIRED",
                    f"allocations is a list of 1 to {MAX_ALLOCATIONS} invoices",
                    field="allocations",
                )
            ],
        )
    out: list[tuple[int, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict) or set(row) != {"invoice_id", "amount_paise"}:
            raise Refusal(
                "INVALID_REQUEST",
                "Each line names an invoice_id and an amount_paise.",
                issues=[
                    issue("INVALID", "invoice_id and amount_paise", field=f"allocations[{index}]")
                ],
            )
        out.append(
            (_int(row["invoice_id"], f"allocations[{index}].invoice_id"), row["amount_paise"])
        )
    if len({invoice_id for invoice_id, _ in out}) != len(out):
        raise Refusal(
            "INVALID_REQUEST",
            "Each invoice appears once in a payment.",
            issues=[issue("DUPLICATE", "one line per invoice", field="allocations")],
        )
    return out


def record_payment(
    run: CommandRun,
    *,
    user: Any,
    tenant_id: Any,
    body: dict[str, Any],
    today: date,
) -> PayablePayment:
    """Record a payment made to one vendor for one store, spread over that
    vendor's open invoices there. The lines add up to the payment, and none pays
    an invoice more than is left on it."""
    store = _store(user, tenant_id, body.get("store_id"))
    require_feature(store, FEATURE_KEY)
    vendor = _vendor(tenant_id, body.get("vendor_id"), active=False)
    wanted = _allocations(body.get("allocations"))
    problems: list[dict[str, Any]] = []
    paid_on = _day(body.get("paid_on"), "paid_on", "The payment date", today, problems)
    amount = _money(body.get("amount_paise"), "amount_paise", problems)
    mode = str(body.get("mode") or "")
    if mode not in PayablePayment.Mode.values:
        problems.append(
            issue("INVALID", "Say how it was paid: bank, cheque, UPI or cash.", field="mode")
        )
    reference = _text(
        body.get("reference"),
        "reference",
        "the bank reference, cheque number or UPI reference",
        problems,
        required=True,
        length=NUMBER_LENGTH,
    )
    note = _text(body.get("note"), "note", "a note", problems, required=False)
    lines = [
        (invoice_id, _money(raw, f"allocations[{index}].amount_paise", problems))
        for index, (invoice_id, raw) in enumerate(wanted)
    ]
    _refuse(problems)
    assert paid_on is not None

    _guard(run, vendor.pk)
    invoices = {
        inv.pk: inv
        for inv in run.lock(
            LockRank.DOCUMENT,
            PayableInvoice.objects.filter(
                tenant_id=tenant_id, pk__in=[invoice_id for invoice_id, _ in lines]
            ),
        )
    }
    paid = paid_by_invoice(invoices)
    for invoice_id, share in lines:
        invoice = invoices.get(invoice_id)
        if invoice is None or invoice.store_id != store.pk or invoice.vendor_id != vendor.pk:
            raise Refusal(
                "NOT_FOUND",
                f"Invoice {invoice_id} is not one of {vendor.name}'s invoices at {store.name}.",
            )
        if invoice.status != PayableInvoice.Status.OPEN:
            raise Refusal("STATE_CONFLICT", f"Invoice {invoice.invoice_number} is cancelled.")
        if paid_on < invoice.invoice_date:
            problems.append(
                issue(
                    "BEFORE_INVOICE",
                    f"Invoice {invoice.invoice_number} is dated {invoice.invoice_date:%d %b %Y}, "
                    "after this payment. A payment pays invoices already issued.",
                    field="paid_on",
                )
            )
        left = int(invoice.amount_paise) - paid.get(invoice_id, 0)
        if share > left:
            problems.append(
                issue(
                    "MORE_THAN_OWED",
                    f"Invoice {invoice.invoice_number} has {rupees(left)} left to pay.",
                    field="allocations",
                )
            )
    if sum(share for _, share in lines) != amount:
        problems.append(
            issue(
                "NOT_EQUAL",
                f"The invoice lines add up to {rupees(sum(s for _, s in lines))}, not the "
                f"payment's {rupees(amount)}.",
                field="allocations",
            )
        )
    _refuse(problems)
    payment = PayablePayment.objects.create(
        store=store,
        vendor=vendor,
        paid_on=paid_on,
        amount_paise=amount,
        mode=mode,
        reference=reference,
        note=note,
        recorded_by=user,
    )
    PayableAllocation.objects.bulk_create(
        [
            PayableAllocation(payment=payment, invoice_id=invoice_id, amount_paise=share)
            for invoice_id, share in lines
        ]
    )
    _audit(run, f"payable_payment:{payment.pk}", store.pk, None, payment_snapshot(payment))
    return payment


def cancel_payment(
    run: CommandRun,
    payment_id: int,
    *,
    user: Any,
    reason: Any,
    expected_revision: int | None,
) -> PayablePayment:
    """Cancel a payment recorded by mistake: what it paid is owed again."""
    vendor_id = (
        PayablePayment.objects.filter(pk=payment_id).values_list("vendor_id", flat=True).first()
    )
    if vendor_id is None:
        raise Refusal("NOT_FOUND", "That payment was not found.")
    _guard(run, vendor_id)
    payment: PayablePayment = run.lock(
        LockRank.DOCUMENT, PayablePayment.objects.filter(pk=payment_id)
    )[0]
    _check_revision(payment, expected_revision)
    require_feature(payment.store_id, FEATURE_KEY)
    if payment.status != PayablePayment.Status.RECORDED:
        raise Refusal("STATE_CONFLICT", "This payment is cancelled already.")
    problems: list[dict[str, Any]] = []
    text = _text(reason, "reason", "why it is cancelled", problems, required=True)
    _refuse(problems)
    before = payment_snapshot(payment)
    payment.status = PayablePayment.Status.CANCELLED
    payment.cancel_reason = text
    payment.cancelled_by = user
    payment.cancelled_at = run.now
    payment.revision += 1
    payment.save()
    _audit(
        run, f"payable_payment:{payment.pk}", payment.store_id, before, payment_snapshot(payment)
    )
    return payment
