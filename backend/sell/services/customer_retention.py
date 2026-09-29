"""Customer data retention (store operations ticket 17, §8 ST-CUS-1, §23).

A customer with no purchase for the retention period has their profile removed,
so they can no longer be identified from it. The period is the setting
``KDPS_CUSTOMER_RETENTION_MONTHS`` (36, the 3 years Anand proposed; §23 still
lists it as proposed, so the feature is gated and never runs at a real store).

**What goes.** Exactly what an erasure at the customer's request removes
(``customer_rights.remove_profile``): the record, any record merged into it,
their other numbers and this company's consent answers - and every till replaces
its copy of the customer list at its next sync.

**What stays.** Every bill. A tax invoice keeps the name, number and GSTIN
printed on it, and tax records are kept for 72 months from the due date of that
year's annual return, or longer while an appeal or investigation is open (CGST
Act s.36). Nothing here, or anywhere in the code, deletes or rewrites a bill, so
the tax records outlive the profile whatever the retention period is.

**Who.** Only where the switch is on at every store that holds a bill or a
consent answer of theirs: one store with it off (or gated, as at every real
store while the period is only proposed) keeps the customer. Their last purchase
is the newest bill on any number that was theirs at the time; a record's own
creation counts too, so a customer asked for consent but never billed is not
removed before the period has passed since they were first recorded.

Each removal is its own command by the ``customer-retention`` service, audited
at the store of their newest bill with masked values, as an erasure is.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils import timezone

from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.refusals import Refusal
from masters.models import Customer, Store
from masters.store_feature_registry import CUSTOMER_MERGE_RETENTION
from masters.store_features import feature, switch_states
from sell.models import ConsentAnswer, Sale
from sell.services.customer_numbers import answers_q, bills_q, numbers_of
from sell.services.customer_rights import audit_profile, remove_profile

REMOVE_ACTION = "sell.customer_retention.remove"
SERVICE = "customer-retention"


def months_before(moment: datetime, months: int) -> datetime:
    """The same day and time ``months`` calendar months earlier (the month's last
    day where it is shorter)."""
    index = moment.year * 12 + moment.month - 1 - months
    year, month = divmod(index, 12)
    day = min(moment.day, calendar.monthrange(year, month + 1)[1])
    return moment.replace(year=year, month=month + 1, day=day)


def _lapsed(customer: Customer, edge: datetime) -> tuple[Store, datetime | None] | None:
    """The store to record their removal at, and their last purchase - or None
    when they are kept: a purchase since ``edge``, nobody's store, or a store
    that knows them with the switch off."""
    if customer.created_at > edge:
        return None
    numbers = numbers_of(customer)
    bills = Sale.objects.filter(bills_q(numbers))
    if bills.filter(billed_at__gt=edge).exists():
        return None
    answers = ConsentAnswer.objects.filter(answers_q(numbers))
    store_ids = set(bills.values_list("store_id", flat=True)) | set(
        answers.values_list("store_id", flat=True)
    )
    stores = list(Store.objects.filter(pk__in=store_ids))
    # A store this company cannot see is somebody else's to decide: kept.
    if not stores or len(stores) != len(store_ids):
        return None
    states = switch_states(stores, [feature(CUSTOMER_MERGE_RETENTION)])
    if not all(state.enabled for state in states):
        return None
    last_bill = bills.order_by("-billed_at", "-id").first()
    if last_bill is not None:
        return last_bill.store, last_bill.billed_at
    last_answer = answers.order_by("-answered_at").select_related("store").first()
    assert last_answer is not None  # a store knew them
    return last_answer.store, None


def _handler(customer_id: int, store: Store, edge: datetime, months: int) -> Any:
    def handler(run: CommandRun) -> CommandResult:
        rows = run.lock(LockRank.DOCUMENT, Customer.objects.filter(pk=customer_id))
        if not rows or rows[0].merged_into_id is not None:
            raise Refusal("NOT_FOUND", "This customer is no longer held.", status=404)
        customer = rows[0]
        # Checked again inside the transaction: a bill may have arrived since.
        found = _lapsed(customer, edge)
        if found is None:
            raise Refusal("STATE_CONFLICT", "This customer bought again or is kept.")
        _, last_purchase = found
        profile = audit_profile(customer)
        counts = remove_profile(run, customer, store)
        run.audit_before = {
            **profile,
            "consent_answers": counts["consent_answers"],
            "saved_sizes": counts["saved_sizes"],
        }
        run.audit_after = {
            "mobile": profile["mobile"],
            "profile": "removed",
            "saved_sizes": 0,
            "retention_months": months,
            "last_purchase_on": last_purchase.date().isoformat() if last_purchase else None,
            "consent_answers_removed": counts["consent_answers_removed"],
            "bills_kept": counts["bills_kept"],
            "records_removed": counts["records_removed"],
        }
        return CommandResult(resource_type="customer", resource_id=str(customer_id))

    return handler


def remove_lapsed(now: datetime | None = None) -> int:
    """Remove every customer with no purchase for the retention period, where the
    switch allows it. Returns how many were removed. Safe to run again."""
    months = int(settings.KDPS_CUSTOMER_RETENTION_MONTHS)
    edge = months_before(now or timezone.now(), months)
    recent = Sale.objects.filter(customer_mobile=OuterRef("mobile"), billed_at__gt=edge)
    candidates = (
        Customer.objects.filter(merged_into__isnull=True, created_at__lte=edge)
        .exclude(Exists(recent))
        .order_by("pk")
    )
    removed = 0
    for customer in candidates.iterator(chunk_size=500):
        found = _lapsed(customer, edge)
        if found is None:
            continue
        store, _ = found
        try:
            execute_command(
                Principal(tenant_id=store.tenant_id, service_code=SERVICE),
                CommandSpec(
                    action=REMOVE_ACTION,
                    command_id=uuid.uuid4(),
                    business_input={
                        "customer": customer.pk,
                        "edge": edge.isoformat(),
                        "months": months,
                    },
                    resource_ids=[str(customer.pk)],
                    subject_key=f"customer:{customer.pk}",
                    site_id=store.pk,
                ),
                _handler(customer.pk, store, edge, months),
            )
        except Refusal:
            continue
        removed += 1
    return removed
