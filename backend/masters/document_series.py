"""The document numbering service (store operations ticket 04, ST-CMP-5).

One place that knows how every tax document is numbered once the new format
starts, so later tickets (gift vouchers 19, receipt vouchers 20, delivery
challans 36, debit notes 38) call it instead of writing their own:

    from masters.document_series import DocumentSeries, alert_on_refusal, issue_number

    with alert_on_refusal():             # outside the caller's transaction
        with transaction.atomic():
            number = issue_number(
                series=DocumentSeries.GIFT_VOUCHER, site=store, on=today,
                document_type="gift_voucher", document_ref=str(voucher.pk),
            )

The series (Anand, Q8, Q10 and 27 September 2026), ``XXX`` the owner's prefix:

==================  ==================  =========================
Tax invoice         ``XXX/26-27/n``      up to 999,999 a year
Credit note         ``XXX/CN/2627/n``    up to 9,999 a year
Receipt voucher     ``XXX/RV/2627/n``    up to 9,999 a year
Gift voucher        ``XXX/GV/2627/n``    up to 9,999 a year
Delivery challan    ``XXX/DC/2627/n``    the sending site's prefix
Debit note          ``XXX/DN/2627/n``    head office's prefix for the GSTIN
==================  ==================  =========================

Every number is checked **before** it is issued: at most 16 characters, only
letters, digits, ``-`` and ``/`` (CGST Rules 46, 50, 53 and 55), and unique in
its series and year. One that would break a rule is refused with
``NUMBER_REFUSED`` and nothing is written, and an alert is raised for head
office. The alert is written by ``alert_on_refusal``, which must wrap the
caller's transaction from outside: a refusal rolls that transaction back, and
an alert written inside it would go with it.

Numbers are never reused: every used or cancelled number has a row in
``IssuedDocumentNumber`` under a unique constraint, and the counters only move
forward. Gaps stay visible as cancelled rows.

**When the new format applies.** At a store whose ``document-series`` switch is
on, for a document dated on or after the start date (a 1 April, B8). Until then
every bill keeps today's format.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from core.fiscal import financial_year
from core.refusals import Refusal
from masters.document_series_models import (
    DocumentPrefix,
    DocumentSeries,
    DocumentSeriesCounter,
    IssuedDocumentNumber,
    NumberingSetting,
)
from masters.models import Gstin, Store
from masters.store_features import is_feature_on

__all__ = ["DocumentSeries"]

FEATURE_KEY = "document-series"

#: B8: the first 1 April that is not in the past when this was built.
DEFAULT_NEW_FORMAT_FROM = date(2027, 4, 1)
#: How many invoice numbers one offline block holds, until Admin changes it.
DEFAULT_TILL_BLOCK_SIZE = 300

MAX_LENGTH = 16
ALLOWED = re.compile(r"^[A-Za-z0-9/-]+$")
PREFIX_CODE = re.compile(r"^[A-Z]{3}$")

#: The most numbers each series has room for in one year (§6 ST-CMP-5 "Room").
ROOM: dict[str, int] = {
    DocumentSeries.TAX_INVOICE: 999_999,
    DocumentSeries.CREDIT_NOTE: 9_999,
    DocumentSeries.RECEIPT_VOUCHER: 9_999,
    DocumentSeries.GIFT_VOUCHER: 9_999,
    DocumentSeries.DELIVERY_CHALLAN: 9_999,
    DocumentSeries.DEBIT_NOTE: 9_999,
}

#: Whose prefix each series carries.
SITE_SERIES = frozenset(
    {
        DocumentSeries.TAX_INVOICE,
        DocumentSeries.CREDIT_NOTE,
        DocumentSeries.RECEIPT_VOUCHER,
        DocumentSeries.GIFT_VOUCHER,
        DocumentSeries.DELIVERY_CHALLAN,
    }
)
GSTIN_SERIES = frozenset({DocumentSeries.DEBIT_NOTE})


class NumberRefused(Refusal):
    """A number that would break a rule, refused before any document carries it."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        series: str,
        number: str = "",
        store_id: int | None = None,
        owner: str = "",
    ) -> None:
        super().__init__("NUMBER_REFUSED", message, status=409)
        self.reason = reason
        self.series = series
        self.number = number
        self.store_id = store_id
        self.owner = owner


# --- the format --------------------------------------------------------------


def compact_fy(fy: str) -> str:
    """``26-27`` -> ``2627``, the spelling the non-invoice series carry."""
    return fy.replace("-", "")


def render(series: str, prefix: str, fy: str, n: int) -> str:
    """How number ``n`` of ``series`` reads, e.g. ``DEA/26-27/74`` or ``DEA/CN/2627/5``."""
    if series == DocumentSeries.TAX_INVOICE:
        return f"{prefix}/{fy}/{n}"
    return f"{prefix}/{series}/{compact_fy(fy)}/{n}"


def number_problem(number: str) -> str | None:
    """Why ``number`` may not be issued, in words, or None if it may."""
    if len(number) > MAX_LENGTH:
        return f"{number} is {len(number)} characters; a tax document number may have 16 at most."
    if not ALLOWED.match(number):
        return f"{number} uses a character other than letters, digits, - and /."
    return None


_INVOICE = re.compile(r"^([A-Z]{3})/(\d{2}-\d{2})/([1-9]\d*)$")


def parse_invoice_number(number: str) -> tuple[str, str, int] | None:
    """``(prefix, fy, n)`` for a tax invoice number in the new format, or None."""
    matched = _INVOICE.match(number.strip())
    if not matched:
        return None
    return matched.group(1), matched.group(2), int(matched.group(3))


# --- the setting -------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    new_format_from: date
    till_block_size: int
    revision: int

    @property
    def started(self) -> bool:
        return self.new_format_from <= timezone.localdate()


def numbering_setting(tenant_id: Any) -> Setting:
    row = NumberingSetting.objects.filter(tenant_id=tenant_id).first()
    if row is None:
        return Setting(DEFAULT_NEW_FORMAT_FROM, DEFAULT_TILL_BLOCK_SIZE, 0)
    return Setting(row.new_format_from, row.till_block_size, row.revision)


def start_date_problem(value: date, today: date) -> str | None:
    """B8: the new format may start only on a 1 April that is not in the past."""
    if (value.month, value.day) != (4, 1):
        return (
            f"The number format can change only on 1 April. {value.isoformat()} is not a 1 April."
        )
    if value < today:
        return f"{value.isoformat()} is in the past. Pick a 1 April that is still to come."
    return None


def switch_change_problem(tenant_id: Any, today: date) -> str | None:
    """Why the ``document-series`` switch may not change today, or None.

    A store's number format may change only on 1 April (§6 principle 5), so once
    the new format has started, switching a store on or off would change its
    format mid-year. Refused until a rule for joining on a later 1 April exists.
    """
    starts = numbering_setting(tenant_id).new_format_from
    if starts <= today:
        return (
            f"The new number format started on {starts.isoformat()}, and a store's number "
            "format can change only on 1 April. This switch is fixed until a way to change "
            "it on a later 1 April is decided."
        )
    return None


def new_series_issued(tenant_id: Any, from_fy: str) -> bool:
    """Has any invoice number of year ``from_fy`` or later been handed out?"""
    return DocumentSeriesCounter.objects.filter(
        tenant_id=tenant_id,
        series=DocumentSeries.TAX_INVOICE,
        fy__gte=from_fy,
        next_n__gt=1,
    ).exists()


def new_format_applies(store: Store, on: date) -> bool:
    """Does a document of ``store`` dated ``on`` take the new format?"""
    if not is_feature_on(store, FEATURE_KEY):
        return False
    return on >= numbering_setting(store.tenant_id).new_format_from


# --- prefixes ----------------------------------------------------------------


def prefix_for_site(site: Store) -> DocumentPrefix | None:
    return DocumentPrefix.objects.filter(tenant_id=site.tenant_id, site=site).first()


def prefix_for_gstin(gstin: Gstin) -> DocumentPrefix | None:
    return DocumentPrefix.objects.filter(tenant_id=gstin.tenant_id, gstin=gstin).first()


def prefix_in_use(prefix: DocumentPrefix) -> bool:
    """Has any number been taken from this prefix? Then its code is fixed."""
    return (
        DocumentSeriesCounter.objects.filter(prefix=prefix, next_n__gt=1).exists()
        or IssuedDocumentNumber.objects.filter(prefix=prefix).exists()
    )


def _owner_prefix(series: str, site: Store | None, gstin: Gstin | None) -> DocumentPrefix:
    if series in GSTIN_SERIES:
        if gstin is None:
            raise ValueError(f"{series} numbers carry head office's prefix for a GSTIN")
        found = prefix_for_gstin(gstin)
        owner = f"head office ({gstin.gstin})"
        store_id = None
    else:
        if site is None:
            raise ValueError(f"{series} numbers carry a site's prefix")
        found = prefix_for_site(site)
        owner = f"{site.code} · {site.name}"
        store_id = site.pk
    if found is None:
        raise NumberRefused(
            f"{owner} has no document prefix yet, so no {DocumentSeries(series).label.lower()} "
            "number can be issued. Admin sets one in Setup, Document Numbering.",
            reason="no_prefix",
            series=series,
            store_id=store_id,
            owner=owner,
        )
    return found


# --- issuing -----------------------------------------------------------------


def _require_transaction() -> None:
    if not connection.in_atomic_block:
        raise RuntimeError("document numbers are issued inside the caller's transaction")


def _counter(prefix: DocumentPrefix, series: str, fy: str) -> DocumentSeriesCounter:
    """The series row, created at 1 the first time, locked for this transaction."""
    DocumentSeriesCounter.objects.get_or_create(
        tenant_id=prefix.tenant_id, prefix=prefix, series=series, fy=fy
    )
    return DocumentSeriesCounter.objects.select_for_update().get(
        tenant_id=prefix.tenant_id, prefix=prefix, series=series, fy=fy
    )


def allocate(prefix: DocumentPrefix, series: str, fy: str, count: int = 1) -> tuple[int, int]:
    """Take the next ``count`` numbers of a series: ``(first, last)``.

    Gap-free: the counter is locked for the caller's transaction and moves only
    here, so a rolled-back document gives its number back. Every number in the
    range is checked before the counter moves; a range that runs past the
    series' room is cut to what is left, and none left at all is refused.
    """
    _require_transaction()
    if count < 1:
        raise ValueError("count must be at least 1")
    counter = _counter(prefix, series, fy)
    first = counter.next_n
    last = min(first + count - 1, ROOM[series])
    label = DocumentSeries(series).label.lower()
    if first > ROOM[series]:
        raise NumberRefused(
            f"The {label} series {render(series, prefix.code, fy, 1)} onwards is full for "
            f"{fy}: it has room for {ROOM[series]:,} numbers.",
            reason="series_full",
            series=series,
            number=render(series, prefix.code, fy, first),
            store_id=prefix.site_id,
            owner=prefix.code,
        )
    # The longest number in the range is its last one; if it passes, all do.
    for n in (first, last):
        number = render(series, prefix.code, fy, n)
        problem = number_problem(number)
        if problem is not None:
            raise NumberRefused(
                problem,
                reason="format",
                series=series,
                number=number,
                store_id=prefix.site_id,
                owner=prefix.code,
            )
    taken = IssuedDocumentNumber.objects.filter(
        tenant_id=prefix.tenant_id, prefix=prefix, series=series, fy=fy, n__gte=first, n__lte=last
    ).first()
    if taken is not None:
        raise NumberRefused(
            f"{taken.number} has already been used, so it cannot be issued again.",
            reason="duplicate",
            series=series,
            number=taken.number,
            store_id=prefix.site_id,
            owner=prefix.code,
        )
    counter.next_n = last + 1
    counter.save(update_fields=["next_n"])
    return first, last


def issue_number(
    *,
    series: str,
    on: date,
    document_type: str,
    document_ref: str,
    site: Store | None = None,
    gstin: Gstin | None = None,
) -> IssuedDocumentNumber:
    """Issue the next number of ``series`` for a document dated ``on``.

    ``site`` owns every series but the debit note, which takes head office's
    prefix for ``gstin``. Must run inside the caller's transaction, so the
    number and the document are written (or rolled back) together.
    """
    _require_transaction()
    prefix = _owner_prefix(series, site, gstin)
    fy = financial_year(on)
    n, _ = allocate(prefix, series, fy)
    return record_number(
        prefix,
        series,
        fy,
        n,
        on=on,
        document_type=document_type,
        document_ref=document_ref,
    ).row


@dataclass(frozen=True)
class Recorded:
    row: IssuedDocumentNumber
    #: The number had been cancelled at month end and a late document used it.
    was_cancelled: bool = False


def record_number(
    prefix: DocumentPrefix,
    series: str,
    fy: str,
    n: int,
    *,
    on: date,
    document_type: str,
    document_ref: str,
) -> Recorded:
    """Write down that number ``n`` is on a document. Refuses one already used.

    A number cancelled unused at month end and then found on a document (a till
    that billed offline on the last day and synced late) is taken, because the
    bill is already in the customer's hand; the row says so and the caller flags
    it for Accounts, since the cancellation may already be in a return.
    """
    _require_transaction()
    number = render(series, prefix.code, fy, n)
    problem = number_problem(number)
    if problem is not None:
        raise NumberRefused(
            problem,
            reason="format",
            series=series,
            number=number,
            store_id=prefix.site_id,
            owner=prefix.code,
        )
    now = timezone.now()
    existing = (
        IssuedDocumentNumber.objects.select_for_update()
        .filter(tenant_id=prefix.tenant_id, number=number)
        .first()
    )
    if existing is not None:
        if existing.status == IssuedDocumentNumber.Status.CANCELLED:
            existing.status = IssuedDocumentNumber.Status.ISSUED
            existing.document_type = document_type
            existing.document_ref = document_ref
            existing.reason = (
                f"Cancelled unused on {existing.issued_on.isoformat()}, then used by a "
                "document that arrived late."
            )
            existing.issued_on = on
            existing.updated_at = now
            existing.save()
            return Recorded(existing, was_cancelled=True)
        raise NumberRefused(
            f"{number} has already been used, so it cannot be issued again.",
            reason="duplicate",
            series=series,
            number=number,
            store_id=prefix.site_id,
            owner=prefix.code,
        )
    try:
        with transaction.atomic():
            row = IssuedDocumentNumber.objects.create(
                tenant_id=prefix.tenant_id,
                number=number,
                prefix=prefix,
                series=series,
                fy=fy,
                n=n,
                status=IssuedDocumentNumber.Status.ISSUED,
                document_type=document_type,
                document_ref=document_ref,
                issued_on=on,
                period=on.replace(day=1),
                updated_at=now,
            )
    except IntegrityError:
        raise NumberRefused(
            f"{number} has already been used, so it cannot be issued again.",
            reason="duplicate",
            series=series,
            number=number,
            store_id=prefix.site_id,
            owner=prefix.code,
        ) from None
    return Recorded(row)


def cancel_unused(
    prefix: DocumentPrefix,
    series: str,
    fy: str,
    first: int,
    last: int,
    *,
    period: date,
    reason: str,
) -> int:
    """Record every number from ``first`` to ``last`` not yet used as cancelled.

    For GSTR-1 Table 13 (baseline, CA to confirm). A number already used or
    already cancelled is left as it is, so running this twice changes nothing.
    """
    _require_transaction()
    used = set(
        IssuedDocumentNumber.objects.filter(
            tenant_id=prefix.tenant_id,
            prefix=prefix,
            series=series,
            fy=fy,
            n__gte=first,
            n__lte=last,
        ).values_list("n", flat=True)
    )
    today = timezone.localdate()
    now = timezone.now()
    rows = [
        IssuedDocumentNumber(
            tenant_id=prefix.tenant_id,
            number=render(series, prefix.code, fy, n),
            prefix=prefix,
            series=series,
            fy=fy,
            n=n,
            status=IssuedDocumentNumber.Status.CANCELLED,
            issued_on=today,
            period=period,
            reason=reason,
            updated_at=now,
        )
        for n in range(first, last + 1)
        if n not in used
    ]
    IssuedDocumentNumber.objects.bulk_create(rows)
    return len(rows)


def cancelled_numbers(tenant_id: Any) -> list[dict[str, Any]]:
    """Cancelled numbers per prefix, series and month, ready for GSTR-1 Table 13."""
    rows = IssuedDocumentNumber.objects.filter(
        tenant_id=tenant_id, status=IssuedDocumentNumber.Status.CANCELLED
    ).select_related("prefix")
    grouped: dict[tuple[date, str, str, str], list[int]] = {}
    for row in rows:
        grouped.setdefault((row.period, row.prefix.code, row.series, row.fy), []).append(row.n)
    return [
        {
            "prefix": code,
            "series": series,
            "month": period.isoformat()[:7],
            "count": len(ns),
            "first": render(series, code, fy, min(ns)),
            "last": render(series, code, fy, max(ns)),
        }
        for (period, code, series, fy), ns in sorted(grouped.items())
    ]


# --- alerts ------------------------------------------------------------------


def raise_number_alert(refusal: NumberRefused) -> None:
    """Put a refused number in front of head office (the shared alert inbox).

    One open alert per (series, owner, reason): the same still-true problem is
    not listed twice.
    """
    from alerts.models import Alert, AlertKind, AlertStatus

    label = DocumentSeries(refusal.series).label if refusal.series else "Document"
    key = f"{refusal.series}:{refusal.owner}:{refusal.reason}"[:160]
    exists = Alert.objects.filter(
        kind=AlertKind.DOCUMENT_NUMBER, dedupe_key=key, status=AlertStatus.OPEN
    ).exists()
    if exists:
        return
    Alert.objects.create(
        kind=AlertKind.DOCUMENT_NUMBER,
        kind_label=AlertKind.DOCUMENT_NUMBER.label,
        title=f"{label} number refused: {refusal.message}"[:240],
        dedupe_key=key,
        store_id=refusal.store_id,
        due_date=timezone.localdate(),
    )


@contextmanager
def alert_on_refusal() -> Iterator[None]:
    """Raise an alert for any ``NumberRefused`` that leaves this block, then re-raise.

    Wrap the caller's transaction from **outside**: the refusal rolls that back,
    and the alert has to outlive it.
    """
    try:
        yield
    except NumberRefused as refused:
        raise_number_alert(refused)
        raise
