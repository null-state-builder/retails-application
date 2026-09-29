"""Caps on returns without a bill (store operations ticket 48, ST-RPT-6, Anand B10).

A **return without a bill** is a piece taken back against a bill head office
does not hold: the paper era, or a bill that is not this store's. The pipeline
already takes it and flags it (``return_orig_missing``); on the written bill it
is a return line with no ``original_line``. Whether such a return is allowed at
all is not decided here: it still follows the customer return policy PRD and
OQ-44 (a goods-v1 store refuses it with ``ORIGINAL_REQUIRED`` before this runs).

**The caps.** At most ``KDPS_NO_BILL_RETURN_CAP_PER_PHONE`` (2) such bills per
customer phone number, counted across every store, and
``KDPS_NO_BILL_RETURN_CAP_PER_STAFF`` (5) per staff member - the login that
billed the return (``Sale.created_by``) - in a calendar month, India time. A bill
counts in the month it was billed, not the month it reached head office, so an
offline queue flushed on the 1st cannot start a fresh count. A return with no
phone number counts only for the staff member. Cancelled bills do not count.

**Going over never blocks the bill** (acceptance item 6). The bill is flagged
(``no_bill_return_cap``), an alert is raised at its store (one open alert per
phone or person per month at each store, so every store where it happens is
told), and one audit record says what the counts were before and after this
bill. The phone number is never written whole: the flag, alert and audit carry
its last four digits, and the alert's key a keyed hash (HMAC with the server's
secret), which a list of every mobile number cannot turn back into the number.

**Two at once.** Before counting, the bill takes a transaction lock on its
phone's month and its cashier's month, so a second bill for the same phone or
person waits until this one is committed and then counts it.

Only where the store's ``exceptions-report`` switch is on (ticket 01); off,
nothing here runs, exactly as before (B3). A replay of a bill already accepted
never reaches this module, so nothing is counted twice.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from alerts.models import Alert, AlertKind, AlertStatus
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.documents import DocStatus
from masters.models import Store
from masters.store_feature_registry import EXCEPTIONS_REPORT
from masters.store_features import is_feature_on
from sell.models import ContinuityFlag, Sale, SaleLine

logger = logging.getLogger(__name__)

#: The audit action every cap flag is filed under.
CAP_ACTION = "sell.no_bill_return_cap.flag"


def caps_on(store: Store) -> bool:
    """Whether ticket 48's caps apply at this store."""
    return is_feature_on(store, EXCEPTIONS_REPORT)


@dataclass(frozen=True)
class Month:
    """A calendar month in India time, and the instants it runs between."""

    label: str
    starts: datetime
    ends: datetime

    @classmethod
    def of(cls, instant: datetime) -> Month:
        local = timezone.localtime(instant)
        starts = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year, month = (
            (starts.year + 1, 1) if starts.month == 12 else (starts.year, starts.month + 1)
        )
        ends = starts.replace(year=year, month=month)
        return cls(label=f"{starts:%Y-%m}", starts=starts, ends=ends)


def no_bill_bills(month: Month) -> Any:
    """Bills billed in ``month`` that took a piece back without a bill."""
    return Sale.objects.filter(
        billed_at__gte=month.starts,
        billed_at__lt=month.ends,
        doc_number__isnull=False,
        lines__direction=SaleLine.Direction.RETURN,
        lines__original_line__isnull=True,
    ).exclude(docstatus=DocStatus.CANCELLED)


def is_no_bill_return(sale: Sale) -> bool:
    return sale.lines.filter(
        direction=SaleLine.Direction.RETURN, original_line__isnull=True
    ).exists()


def record_caps(sale: Sale, store: Store, actor: Any) -> list[str]:
    """Flag, alert and audit ``sale`` if it took a phone or a person over a cap.

    Returns the flags raised, as the pipeline collects them. Never raises: a
    problem recording the flag is logged and the bill stands.
    """
    if not caps_on(store) or not is_no_bill_return(sale):
        return []
    try:
        with transaction.atomic():
            return _record(sale, store, actor)
    except Exception:
        logger.exception("no-bill return cap could not be recorded for %s", sale.doc_number)
        return []


def _record(sale: Sale, store: Store, actor: Any) -> list[str]:
    month = Month.of(sale.billed_at)
    phone = sale.customer_mobile
    _lock_counts(month, phone, sale.created_by_id)
    bills = no_bill_bills(month)
    phone_count = bills.filter(customer_mobile=phone).distinct().count() if phone else None
    staff_count = bills.filter(created_by_id=sale.created_by_id).distinct().count()
    person = sale.created_by
    over: list[dict[str, Any]] = []
    if phone_count is not None and phone_count > settings.KDPS_NO_BILL_RETURN_CAP_PER_PHONE:
        over.append(
            {
                "by": "phone",
                "count": phone_count,
                "cap": settings.KDPS_NO_BILL_RETURN_CAP_PER_PHONE,
                "phone_end": phone[-4:],
            }
        )
    if staff_count > settings.KDPS_NO_BILL_RETURN_CAP_PER_STAFF:
        over.append(
            {
                "by": "staff",
                "count": staff_count,
                "cap": settings.KDPS_NO_BILL_RETURN_CAP_PER_STAFF,
                "user_id": person.pk,
                "name": person.full_name or person.username,
            }
        )
    if not over:
        return []
    before = {
        "flagged": False,
        "month": month.label,
        "phone_count": None if phone_count is None else phone_count - 1,
        "staff_count": staff_count - 1,
    }
    after = {
        "flagged": True,
        "month": month.label,
        "phone_count": phone_count,
        "staff_count": staff_count,
        "over": over,
        "doc_number": sale.doc_number,
    }

    def handler(run: CommandRun) -> CommandResult:
        ContinuityFlag.objects.create(
            kind=ContinuityFlag.Kind.NO_BILL_RETURN_CAP,
            store=store,
            sale=sale,
            details={"month": month.label, "over": over},
        )
        for item in over:
            _alert(sale, store, month, item)
        run.audit_subject_key = f"no_bill_cap:{sale.pk}"
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = after
        return CommandResult(resource_type="sale", resource_id=str(sale.pk), status_code=201)

    execute_command(
        _principal(actor, store),
        CommandSpec(
            action=CAP_ACTION,
            # Derived from the bill's own key: a replay is the same record.
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"no-bill-cap:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=f"no_bill_cap:{sale.pk}",
        ),
        handler,
    )
    return [ContinuityFlag.Kind.NO_BILL_RETURN_CAP]


def phone_key(phone: str) -> str:
    """A phone number as a key nobody can turn back into it without the secret."""
    return hmac.new(settings.SECRET_KEY.encode(), phone.encode(), hashlib.sha256).hexdigest()[:32]


def _lock_counts(month: Month, phone: str, user_id: int) -> None:
    """Hold this month's count for the phone and the cashier until commit."""
    keys = [f"staff:{user_id}:{month.label}"]
    if phone:
        keys.append(f"phone:{phone_key(phone)}:{month.label}")
    with connection.cursor() as cursor:
        for key in sorted(keys):
            digest = hashlib.sha256(f"no-bill-cap:{key}".encode()).digest()
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(digest[:8], "big", signed=True)]
            )


def _alert(sale: Sale, store: Store, month: Month, item: dict[str, Any]) -> None:
    """One open alert per phone number or person per month at each store."""
    if item["by"] == "phone":
        who = phone_key(sale.customer_mobile)
        title = (
            f"Phone ending {item['phone_end']}: {item['count']} returns without a bill in "
            f"{month.starts:%b %Y} (cap {item['cap']}), latest {sale.doc_number} at {store.code}"
        )
    else:
        who = f"user{item['user_id']}"
        title = (
            f"{item['name']}: {item['count']} returns without a bill in {month.starts:%b %Y} "
            f"(cap {item['cap']}), latest {sale.doc_number} at {store.code}"
        )
    dedupe = f"no_bill_cap:{item['by']}:{who}:{month.label}:{store.pk}"
    if Alert.objects.filter(
        kind=AlertKind.NO_BILL_RETURN_CAP, dedupe_key=dedupe, status=AlertStatus.OPEN
    ).exists():
        return
    try:
        # Its own savepoint: another bill that opened the same alert a moment
        # ago is "already open", and must not undo this bill's flag and audit.
        with transaction.atomic():
            Alert.objects.create(
                kind=AlertKind.NO_BILL_RETURN_CAP,
                kind_label=AlertKind.NO_BILL_RETURN_CAP.label,
                title=title[:240],
                dedupe_key=dedupe,
                store=store,
                object_id=sale.pk,
                due_date=timezone.localdate(sale.billed_at),
                status=AlertStatus.OPEN,
            )
    except IntegrityError:
        return


def _principal(actor: Any, store: Store) -> Principal:
    """Who the audit record names: the cashier, or the sell service for a login
    with no person behind it (the bill is never refused for that)."""
    tenant_id = getattr(actor, "tenant_id", None) or store.tenant_id
    human_id = getattr(actor, "human_id", None)
    if human_id is not None:
        return Principal(tenant_id=tenant_id, human_id=human_id, user_id=actor.pk)
    return Principal(tenant_id=tenant_id, service_code="sell", user_id=getattr(actor, "pk", None))
