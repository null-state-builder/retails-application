"""Day close read and write shapes (store operations, R-POS-011 / R-FIN-015)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from rest_framework import serializers

from approvals.names import display_name
from sell.services.day_close import DayBills, DayCloseState

#: The five steps, in the order the store works them.
STEPS: tuple[str, ...] = ("synced", "held", "cash", "summary", "close")


class DaySummaryCheckWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The screen's own id for this tick; a retry reuses it.")
    business_day = serializers.DateField(help_text="The business day the screen showed.")


class DayCloseWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The screen's own id for this close; a retry reuses it.")
    business_day = serializers.DateField(help_text="The business day the screen showed.")
    approved_by = serializers.IntegerField(
        required=False, allow_null=True, default=None, help_text="The manager typing their PIN."
    )
    manager_pin = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=12,
        write_only=True,
        help_text="That manager's own PIN, checked again here; never stored.",
    )
    counter_pending = serializers.IntegerField(
        min_value=0, default=0, help_text="Bills the counter says it still has to send."
    )
    till_number = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)


class DayBillsSerializer(serializers.Serializer[Any]):
    bills = serializers.IntegerField()
    net_paise = serializers.IntegerField()
    tenders = serializers.DictField(child=serializers.IntegerField())
    frontier = serializers.IntegerField()


class DayCloseStepSerializer(serializers.Serializer[Any]):
    key = serializers.CharField(help_text="synced, held, cash, summary or close.")
    done = serializers.BooleanField()
    code = serializers.CharField(allow_blank=True)
    message = serializers.CharField(allow_blank=True)


class DayCloseCountSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    counted_at = serializers.DateTimeField()
    counted_paise = serializers.IntegerField()
    expected_paise = serializers.IntegerField()
    variance_paise = serializers.IntegerField()
    counted_by_name = serializers.CharField(allow_blank=True)
    approved_by_name = serializers.CharField(allow_blank=True)


class DayCloseRecordSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    summary_checked_at = serializers.DateTimeField(allow_null=True)
    summary_checked_by_name = serializers.CharField(allow_blank=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    closed_by_name = serializers.CharField(allow_blank=True)
    confirmed_by_name = serializers.CharField(allow_blank=True)
    facts = serializers.DictField(help_text="The day's figures frozen at the close.")


class DayCloseStillOpenSerializer(serializers.Serializer[Any]):
    reservations_holding = serializers.IntegerField()
    reservations_past_date = serializers.IntegerField()
    alterations_ready = serializers.IntegerField()
    special_orders_arrived = serializers.IntegerField()


class DayCloseStateSerializer(serializers.Serializer[Any]):
    """One store's business day, step by step."""

    store = serializers.CharField()
    business_day = serializers.DateField()
    next_business_day = serializers.DateField()
    closed = serializers.BooleanField()
    can_close = serializers.BooleanField(
        help_text="Every step but the close is done (as the server sees it)."
    )
    steps = DayCloseStepSerializer(many=True)
    counter_id = serializers.CharField(allow_blank=True)
    hole_count = serializers.IntegerField()
    last_doc_number = serializers.CharField(allow_blank=True)
    held_open = serializers.IntegerField()
    held_kept = serializers.IntegerField()
    switched_on = serializers.BooleanField()
    count = DayCloseCountSerializer(allow_null=True)
    bills_after_count = serializers.IntegerField()
    summary = DayBillsSerializer()
    summary_changed = serializers.BooleanField()
    carried_in = DayBillsSerializer()
    after_close = DayBillsSerializer()
    record = DayCloseRecordSerializer(allow_null=True)
    last_closed = serializers.DateField(allow_null=True)
    still_open = DayCloseStillOpenSerializer()


def _bills(value: DayBills) -> dict[str, Any]:
    return {
        "bills": value.bills,
        "net_paise": value.net_paise,
        "tenders": dict(value.tenders),
        "frontier": value.frontier,
    }


def state_body(state: DayCloseState) -> dict[str, Any]:
    """The wire shape of `state`; names are projected by the view."""
    open_steps = {blocker.step: blocker for blocker in state.blockers()}
    steps = []
    for key in STEPS[:-1]:
        blocker = None if state.closed else open_steps.get(key)
        steps.append({
            "key": key,
            "done": blocker is None,
            "code": blocker.code if blocker else "",
            "message": blocker.message if blocker else "",
        })
    can_close = not state.closed and not open_steps
    steps.append({
        "key": "close",
        "done": state.closed,
        "code": "" if state.closed or can_close else "STEPS_OPEN",
        "message": "" if state.closed or can_close else "Finish the steps above first.",
    })
    count = state.count
    row = state.row
    return {
        "store": state.store.code,
        "business_day": state.day,
        "next_business_day": state.day + timedelta(days=1),
        "closed": state.closed,
        "can_close": can_close,
        "steps": steps,
        "counter_id": state.counter_id,
        "hole_count": state.hole_count,
        "last_doc_number": state.last_doc_number,
        "held_open": state.held_open,
        "held_kept": state.held_kept,
        "switched_on": state.switched_on,
        "count": None if count is None else {
            "id": count.pk,
            "counted_at": count.counted_at,
            "counted_paise": count.counted_paise,
            "expected_paise": count.expected_paise,
            "variance_paise": count.variance_paise,
            "counted_by_name": display_name(count.counted_by),
            "approved_by_name": display_name(count.approved_by) if count.approved_by_id else "",
        },
        "bills_after_count": state.bills_after_count,
        "summary": _bills(state.summary),
        "summary_changed": state.summary_changed,
        "carried_in": _bills(state.carried),
        "after_close": _bills(state.after),
        "record": None if row is None else {
            "id": row.pk,
            "summary_checked_at": row.summary_checked_at,
            "summary_checked_by_name": (
                display_name(row.summary_checked_by) if row.summary_checked_by_id else ""
            ),
            "closed_at": row.closed_at,
            "closed_by_name": display_name(row.closed_by) if row.closed_by_id else "",
            "confirmed_by_name": display_name(row.confirmed_by) if row.confirmed_by_id else "",
            "facts": dict(row.facts or {}),
        },
        "last_closed": state.last_closed,
        "still_open": dict(state.still_open),
    }
