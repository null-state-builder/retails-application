"""Gift vouchers (store operations ticket 19, ST-POS-4; overall PRD R-POS-003, R-POS-014).

**Sold at the till, as its own document.** A number from the store's GV series
(ST-CMP-5), a value, paid in cash, card or UPI, and a last day it can be used:
the day before the same date 12 months on (setting ``KDPS_GIFT_VOUCHER_MONTHS``,
frozen on the voucher). A bearer voucher: no customer is recorded on it.

**Neither goods nor services (Circular 243/37/2024).** Selling one charges no
GST: the books take the tender in and owe the same amount as
``GIFT_VOUCHER_LIABILITY``, with a cash-ledger receipt, under the GV number.
GST is charged on the goods bought with it, on the bill, exactly as on any bill:
the voucher is only a way of paying (a ``gift_voucher`` tender that gives up the
liability). What a voucher still holds after its last day is written off to
``EXPIRED_GIFT_VOUCHERS`` on the worker's clock, with no GST.

**Used in parts.** A bill spends up to what the voucher holds and the rest stays
on it. Every change - sold, used, expired - is its own ``GiftVoucherMovement``
row, never an edited total, so the balance is their sum.

**Where.** At the store that sold it or any other store under the same GSTIN
where the switch is on (baseline, like returns: the liability stays in the books
of the registration that raised it).

**Online only.** Selling one is a request to head office. The till looks a
voucher up with head office before it takes it and refuses offline; the bill
that spends it is checked again here when it arrives (``check_for_bill`` /
``record_use``, called by ``accept``) and refused whole if the voucher has
expired, is not usable here, or holds less than the bill used.

Every write is one command, audited with the voucher before and after.
"""

from __future__ import annotations

import calendar
import hmac
import logging
import secrets
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Case, F, IntegerField, QuerySet, Sum, Value, When
from django.utils import timezone

from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.gl import GLAccount
from core.posting import cr, dr, post_entries
from core.refusals import Refusal
from finledger.models import CashLedgerEntry
from finledger.posting import account_for_mode
from masters.document_series import DocumentSeries, alert_on_refusal, issue_number
from masters.models import Store
from masters.store_feature_registry import GIFT_VOUCHERS
from masters.store_features import is_feature_on, require_feature
from sell.gift_voucher_models import GiftVoucher, GiftVoucherMovement
from sell.models import Sale, SaleTender
from sell.services.advances import TENDER_ACCOUNT, TENDER_MODES, user_of
from sell.services.petty_cash import rupees

logger = logging.getLogger(__name__)

ISSUE_ACTION = "sell.gift_voucher.issue"
USE_ACTION = "sell.gift_voucher.use"
EXPIRE_ACTION = "sell.gift_voucher.expire"
#: The service that writes off expired vouchers on the worker's clock.
EXPIRY_SERVICE = "gift-voucher-expiry"

#: Said on every voucher, word for word.
NO_GST_NOTE = (
    "No GST is charged on a gift voucher. GST is charged on the goods bought with it "
    "(Circular 243/37/2024)."
)

#: The check code: 8 characters from an alphabet with no look-alikes (0/O, 1/I).
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

#: A voucher's state on a day, in one word the screen shows.
ACTIVE = "active"
USED_UP = "used_up"
EXPIRED = "expired"


def _refuse(code: str, message: str, status: int = 422) -> Refusal:
    return Refusal(code, message, status=status)


def date_text(day: date) -> str:
    """A date as the customer reads it: 27 Sep 2027."""
    return f"{day.day} {day:%b %Y}"


# ---------------------------------------------------------------------------
# The rule, pure (the golden cases in ``sell/vectors/gift_vouchers.json``)
# ---------------------------------------------------------------------------


def add_months(day: date, months: int) -> date:
    """The same day ``months`` calendar months on; the month's last day where it is shorter."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def valid_until_for(issued_on: date, months: int) -> date:
    """The last day a voucher sold on ``issued_on`` can be used: the day before the
    same date ``months`` months on (sold 28 Sep 2026, used up to 27 Sep 2027)."""
    return add_months(issued_on, months) - timedelta(days=1)


def usable_on(valid_until: date, day: date) -> bool:
    return day <= valid_until


def plan_payment(owed_paise: int, balances: Sequence[int]) -> list[int]:
    """What each voucher pays towards ``owed_paise``, in the order given: each up to
    what it holds, never more than is still owed. The rest stays on the voucher."""
    left = max(owed_paise, 0)
    pays: list[int] = []
    for balance in balances:
        part = max(0, min(balance, left))
        pays.append(part)
        left -= part
    return pays


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def balance_paise(voucher: GiftVoucher) -> int:
    """What the voucher still holds: sold, less every use and any write-off."""
    total = 0
    for row in voucher.movements.all():
        total += (
            row.amount_paise if row.kind == GiftVoucherMovement.Kind.ISSUED else -row.amount_paise
        )
    return total


def expired_paise(voucher: GiftVoucher) -> int:
    """What was written off when it expired unused (0 if nothing was)."""
    return sum(
        row.amount_paise
        for row in voucher.movements.all()
        if row.kind == GiftVoucherMovement.Kind.EXPIRED
    )


def state_on(voucher: GiftVoucher, day: date) -> str:
    """``used_up`` once nothing is left (spent in full, before or after its last
    day); ``expired`` after its last day with something unused on it, held or
    already written off; else ``active``."""
    if not usable_on(voucher.valid_until, day):
        return EXPIRED if balance_paise(voucher) + expired_paise(voucher) > 0 else USED_UP
    if balance_paise(voucher) <= 0:
        return USED_UP
    return ACTIVE


def snapshot(voucher: GiftVoucher) -> dict[str, Any]:
    """The voucher as the audit log keeps it."""
    return {
        "number": voucher.number,
        "store": voucher.store.code,
        "issued_on": voucher.issued_on.isoformat(),
        "valid_until": voucher.valid_until.isoformat(),
        "value_paise": voucher.value_paise,
        "mode": voucher.mode,
        "balance_paise": balance_paise(voucher),
        "expired_paise": expired_paise(voucher),
    }


def rows_for(store: Store) -> QuerySet[GiftVoucher]:
    """The vouchers ``store`` sold, with what is needed to read their balance."""
    return (
        GiftVoucher.objects.filter(store=store)
        .select_related("store")
        .prefetch_related("movements__sale", "movements__store")
    )


# ---------------------------------------------------------------------------
# Who does it
# ---------------------------------------------------------------------------


def _person(actor: Any) -> Principal:
    """The named person making the change."""
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    if human_id is None or tenant_id is None:
        raise _refuse(
            "SCOPE_DENIED",
            "This login is not a person in the records, so it cannot sell or take a gift voucher.",
            403,
        )
    return Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))


# ---------------------------------------------------------------------------
# Selling a voucher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issued:
    voucher: GiftVoucher
    created: bool


def issue(store: Store, actor: Any, data: dict[str, Any]) -> Issued:
    """Sell a gift voucher: the GV number, the money in, the liability - or refuse whole.

    ``data``: ``id`` (the till's own id for this sale), ``value_paise``, ``mode``
    (cash, card or UPI) and an optional ``reference``. A replay of the same id
    answers with the voucher it made.
    """
    sale_id: uuid.UUID = data["id"]
    existing = GiftVoucher.objects.filter(idempotency_uuid=sale_id).first()
    if existing is not None:
        if not (
            existing.store_id == store.pk
            and existing.value_paise == int(data.get("value_paise") or 0)
            and existing.mode == str(data.get("mode") or "")
        ):
            raise _refuse(
                "GIFT_VOUCHER_CONFLICT",
                "This voucher id is already used for a different gift voucher.",
                409,
            )
        return Issued(voucher=existing, created=False)
    require_feature(store, GIFT_VOUCHERS)
    value = int(data.get("value_paise") or 0)
    mode = str(data.get("mode") or "")
    reference = str(data.get("reference") or "").strip()[:64]
    if value <= 0:
        raise _refuse("VALIDATION", "Type the voucher's value.", 400)
    if mode not in TENDER_MODES:
        raise _refuse("VALIDATION", "A gift voucher is paid for in cash, card or UPI.", 400)
    principal = _person(actor)
    today = timezone.localdate()
    months = int(settings.KDPS_GIFT_VOUCHER_MONTHS)
    code = new_code()

    def handler(run: CommandRun) -> CommandResult:
        number = issue_number(
            series=DocumentSeries.GIFT_VOUCHER,
            site=store,
            on=today,
            document_type="gift_voucher",
            document_ref=str(sale_id),
        )
        voucher = GiftVoucher.objects.create(
            idempotency_uuid=sale_id,
            store=store,
            number=number.number,
            code=code,
            issued_on=today,
            valid_until=valid_until_for(today, months),
            value_paise=value,
            mode=mode,
            reference=reference,
            created_by=user_of(actor),
        )
        voucher.post()
        post_entries(
            voucher,
            [
                dr(TENDER_ACCOUNT[mode], value, memo=f"Gift voucher {voucher.number} sold"),
                cr(GLAccount.GIFT_VOUCHER_LIABILITY, value, memo=f"Owed on {voucher.number}"),
            ],
            posted_by=actor,
        )
        CashLedgerEntry.objects.create(
            account=account_for_mode(mode),
            amount=value,
            kind=CashLedgerEntry.Kind.RECEIPT,
            doc_number=voucher.number,
            description=f"Gift voucher {voucher.number} sold",
            mode=mode,
            posted_by=user_of(actor),
        )
        GiftVoucherMovement.objects.create(
            voucher=voucher,
            kind=GiftVoucherMovement.Kind.ISSUED,
            amount_paise=value,
            store=store,
            at=timezone.now(),
            by=user_of(actor),
        )
        run.audit_subject_key = f"gift_voucher:{voucher.pk}"
        run.audit_site_id = store.pk
        run.audit_before = None
        run.audit_after = snapshot(voucher)
        return CommandResult(
            resource_type="gift_voucher", resource_id=str(voucher.pk), status_code=201
        )

    with alert_on_refusal():
        execute_command(
            principal,
            CommandSpec(
                action=ISSUE_ACTION,
                command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"gift-voucher:{sale_id}"),
                business_input={
                    "id": str(sale_id),
                    "store": store.code,
                    "value_paise": value,
                    "mode": mode,
                },
                site_id=store.pk,
                subject_key=f"gift_voucher_sale:{sale_id}",
            ),
            handler,
        )
    return Issued(voucher=GiftVoucher.objects.get(idempotency_uuid=sale_id), created=True)


# ---------------------------------------------------------------------------
# Using a voucher
# ---------------------------------------------------------------------------


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalise_number(number: str) -> str:
    return str(number or "").strip().upper()


def normalise_code(code: str) -> str:
    """As typed off the slip: case, spaces and dashes do not matter."""
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _judge(
    voucher: GiftVoucher | None, store: Store, day: date, typed: str, code: str
) -> GiftVoucher:
    """The voucher, if it can pay at ``store`` on ``day``; otherwise the refusal.

    A wrong code reads exactly as an unknown number, so a guess learns nothing.
    """
    if voucher is None or not hmac.compare_digest(voucher.code, normalise_code(code)):
        raise _refuse(
            "GIFT_VOUCHER_NOT_FOUND",
            f"No gift voucher has the number {typed} with that code. Check both on the slip.",
            404,
        )
    if voucher.store.gstin_id != store.gstin_id:
        raise _refuse(
            "GIFT_VOUCHER_ELSEWHERE",
            f"{voucher.number} was sold by {voucher.store.name} under another GSTIN, so it "
            f"cannot be used at {store.name}. Take another payment.",
        )
    if not usable_on(voucher.valid_until, day):
        raise _refuse(
            "GIFT_VOUCHER_EXPIRED",
            f"{voucher.number} expired on {date_text(voucher.valid_until)}, so it cannot be "
            "used. Take another payment.",
        )
    if balance_paise(voucher) <= 0:
        raise _refuse(
            "GIFT_VOUCHER_USED_UP",
            f"{voucher.number} has nothing left on it. Take another payment.",
        )
    return voucher


def _of_company(store: Store) -> QuerySet[GiftVoucher]:
    """Vouchers of ``store``'s own company: another company's reads as unknown."""
    return GiftVoucher.objects.filter(store__tenant_id=store.tenant_id)


def look_up(store: Store, number: str, code: str, day: date | None = None) -> GiftVoucher:
    """The voucher the till is about to take, judged for this store today.

    Refused where the switch is off here, the number and code do not match a
    voucher of this company, or the voucher was sold under another GSTIN, has
    expired or has nothing left.
    """
    require_feature(store, GIFT_VOUCHERS)
    typed = normalise_number(number)
    voucher = (
        _of_company(store)
        .select_related("store")
        .prefetch_related("movements")
        .filter(number=typed)
        .first()
    )
    return _judge(voucher, store, day or timezone.localdate(), typed, code)


@dataclass(frozen=True)
class Use:
    voucher: GiftVoucher
    amount_paise: int


def check_for_bill(store: Store, data: dict[str, Any], actor: Any) -> list[Use]:
    """The vouchers this bill spends, checked and locked - or an empty list.

    Runs inside the bill's transaction, so two bills spending one voucher at once
    wait for each other and the second sees what the first used. Refused (the
    bill is not taken) where the switch is off at this store, the login is not a
    person, or a voucher is unknown, not usable here, expired on the bill's day,
    or holds less than the bill used.
    """
    from sell.services.accept import AcceptError

    wanted = [t for t in data["tenders"] if t["mode"] == SaleTender.Mode.GIFT_VOUCHER]
    if not wanted:
        return []
    if not is_feature_on(store, GIFT_VOUCHERS):
        raise AcceptError(
            "FEATURE_OFF",
            f"Gift vouchers are switched off at {store.code}, so this bill cannot spend one.",
            403,
        )
    try:
        _person(actor)
    except Refusal as refusal:
        raise AcceptError(refusal.code, refusal.message, refusal.status) from refusal
    # Judged on the bill's own day - but a voucher is taken only online, so a
    # bill dated back more than a day cannot spend one past its last day.
    day = max(
        timezone.localtime(data["billed_at"]).date(),
        timezone.localdate() - timedelta(days=1),
    )
    numbers = sorted({normalise_number(t["gift_voucher"]) for t in wanted})
    # What each holds is read only now, under the lock.
    locked = {
        v.number: v
        for v in _of_company(store)
        .select_for_update(of=("self",))
        .select_related("store")
        .filter(number__in=numbers)
        .order_by("number")
    }
    uses: list[Use] = []
    for tender in wanted:
        typed = normalise_number(tender["gift_voucher"])
        try:
            voucher = _judge(
                locked.get(typed), store, day, typed, tender.get("gift_voucher_code") or ""
            )
        except Refusal as refusal:
            raise AcceptError(
                refusal.code, refusal.message, 422 if refusal.status == 404 else refusal.status
            ) from refusal
        held = balance_paise(voucher)
        amount = int(tender["amount_paise"])
        if amount > held:
            raise AcceptError(
                "GIFT_VOUCHER_BALANCE",
                f"{voucher.number} holds {rupees(held)}; the bill used {rupees(amount)}.",
                422,
            )
        uses.append(Use(voucher=voucher, amount_paise=amount))
    return uses


def record_use(sale: Sale, uses: Iterable[Use], actor: Any) -> None:
    """What each voucher paid towards ``sale``, one audited movement per voucher."""
    for use in uses:
        voucher = use.voucher

        def handler(
            run: CommandRun, voucher: GiftVoucher = voucher, amount: int = use.amount_paise
        ) -> CommandResult:
            before = snapshot(voucher)
            GiftVoucherMovement.objects.create(
                voucher=voucher,
                kind=GiftVoucherMovement.Kind.USED,
                amount_paise=amount,
                sale=sale,
                store=sale.store,
                at=timezone.now(),
                by=user_of(actor),
            )
            fresh = GiftVoucher.objects.select_related("store").get(pk=voucher.pk)
            run.audit_subject_key = f"gift_voucher:{voucher.pk}"
            run.audit_site_id = sale.store_id
            run.audit_before = before
            run.audit_after = {**snapshot(fresh), "sale": sale.doc_number, "used_paise": amount}
            return CommandResult(
                resource_type="gift_voucher", resource_id=str(voucher.pk), status_code=200
            )

        execute_command(
            _person(actor),
            CommandSpec(
                action=USE_ACTION,
                command_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"gift-voucher:{voucher.pk}:bill:{sale.idempotency_uuid}"
                ),
                business_input={
                    "voucher": voucher.number,
                    "sale": sale.doc_number,
                    "amount_paise": use.amount_paise,
                },
                site_id=sale.store_id,
                subject_key=f"gift_voucher:{voucher.pk}",
            ),
            handler,
        )


# ---------------------------------------------------------------------------
# Expiry, on the worker's clock
# ---------------------------------------------------------------------------


def expire_due(today: date | None = None, tenant_id: Any = None) -> int:
    """Write off what every voucher past its last day still holds. Returns how many.

    No GST (Circular 243/37/2024). One voucher that cannot be written off is
    logged and left for the next run; it never stops the others.
    """
    day = today or timezone.localdate()
    # Only those still holding something: a voucher spent in full, or already
    # written off, is never looked at again.
    due = (
        GiftVoucher.objects.filter(valid_until__lt=day)
        .annotate(
            left=Sum(
                Case(
                    When(
                        movements__kind=GiftVoucherMovement.Kind.ISSUED,
                        then="movements__amount_paise",
                    ),
                    default=Value(0) - F("movements__amount_paise"),
                    output_field=IntegerField(),
                )
            )
        )
        .filter(left__gt=0)
        .select_related("store")
    )
    if tenant_id is not None:
        due = due.filter(store__tenant_id=tenant_id)
    done = 0
    for voucher in list(due):
        try:
            if _expire(voucher):
                done += 1
        except Exception:
            logger.exception("gift voucher %s could not be written off", voucher.number)
    return done


def _expire(voucher: GiftVoucher) -> bool:
    store = voucher.store
    wrote: list[bool] = []

    def handler(run: CommandRun) -> CommandResult:
        locked = (
            GiftVoucher.objects.select_for_update(of=("self",))
            .select_related("store")
            .get(pk=voucher.pk)
        )
        held = balance_paise(locked)
        before = snapshot(locked)
        if held > 0:
            post_entries(
                locked,
                [
                    dr(GLAccount.GIFT_VOUCHER_LIABILITY, held, memo=f"{locked.number} expired"),
                    cr(
                        GLAccount.EXPIRED_GIFT_VOUCHERS,
                        held,
                        memo=f"{locked.number} expired unused, no GST",
                    ),
                ],
                posted_by=None,
            )
            GiftVoucherMovement.objects.create(
                voucher=locked,
                kind=GiftVoucherMovement.Kind.EXPIRED,
                amount_paise=held,
                store=store,
                at=timezone.now(),
            )
            wrote.append(True)
        run.audit_subject_key = f"gift_voucher:{locked.pk}"
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = snapshot(GiftVoucher.objects.select_related("store").get(pk=locked.pk))
        return CommandResult(
            resource_type="gift_voucher", resource_id=str(locked.pk), status_code=200
        )

    if balance_paise(voucher) <= 0:
        return False
    execute_command(
        Principal(tenant_id=store.tenant_id, service_code=EXPIRY_SERVICE),
        CommandSpec(
            action=EXPIRE_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"gift-voucher:{voucher.pk}:expire"),
            business_input={
                "voucher": voucher.number,
                "valid_until": voucher.valid_until.isoformat(),
            },
            site_id=store.pk,
            subject_key=f"gift_voucher:{voucher.pk}",
        ),
        handler,
    )
    return bool(wrote)


def scheduled_expiry(tenant_id: uuid.UUID) -> None:
    """The worker's call: write off what expired vouchers still hold."""
    expire_due(tenant_id=tenant_id)


__all__ = [
    "ACTIVE",
    "EXPIRED",
    "NO_GST_NOTE",
    "USED_UP",
    "Issued",
    "Use",
    "add_months",
    "balance_paise",
    "check_for_bill",
    "expire_due",
    "expired_paise",
    "issue",
    "look_up",
    "plan_payment",
    "record_use",
    "rows_for",
    "snapshot",
    "state_on",
    "usable_on",
    "valid_until_for",
]
