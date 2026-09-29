"""Stock Count > Count Schedule (store operations ticket 35, ST-INV-3).

``GET  /api/goods-v1/outbound/count-schedules``            the schedules, today's
                                                           due counts and the missed ones
``POST /api/goods-v1/outbound/count-schedules``            set a schedule
``POST /api/goods-v1/outbound/count-schedules/<id>``       change how often, or from when
``POST /api/goods-v1/outbound/count-schedules/<id>/stop``  stop it

Readers hold ``stock_count: view`` or higher, for the stores in their own scope
where the ``scheduled-counts`` switch is on; a store person sees their own store.
Only the Owner sets, changes and stops a schedule (``stock_count: approve``
narrowed to ``outbound.count_schedule_editors``), at a store in their scope. A
store outside it is not found. Setting and changing need the switch on at that
store; stopping never does, because it only corrects a record already there.

Every write runs as one command, so its ``AuditEvent`` records who, when, and the
schedule before and after. The first due day is today or later: a schedule
never makes a day that has passed late.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from django.db import IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_revision,
    parse_int_id,
    parse_meta,
)
from accounts.permissions import user_can
from accounts.role_lists import COUNT_SCHEDULE_EDITOR_ROLES
from accounts.sections import CAP_APPROVE, CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal, issue
from masters.models import Brand, Store
from masters.scoping import actionable_store_ids, actionable_stores
from masters.store_features import require_feature
from outbound import count_schedules as rules
from outbound.count_schedule_models import CountEvery, CountSchedule

SET_ACTION = "stock.count_schedule.set"
CHANGE_ACTION = "stock.count_schedule.change"
STOP_ACTION = "stock.count_schedule.stop"


def may_read(user: Any) -> bool:
    return bool(getattr(user, "is_superuser", False)) or user_can(user, "stock_count", CAP_VIEW)


def may_edit(user: Any) -> bool:
    """The Owner: ``stock_count: approve`` and a declared editor role (or break-glass)."""
    if getattr(user, "is_superuser", False):
        return True
    code = getattr(getattr(user, "role", None), "code", "")
    return user_can(user, "stock_count", CAP_APPROVE) and code in COUNT_SCHEDULE_EDITOR_ROLES


def schedule_stores(user: Any) -> list[Store]:
    """The active stores in this person's scope where the switch is on."""
    return rules.switched_on(list(actionable_stores(user)))


# -- serializers (the OpenAPI shape the PWA's client is generated from) -------------


class ScheduleSiteSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    goods_v1 = serializers.BooleanField(
        help_text="True: the site counts through goods-v1 counts; false: the older count."
    )


class ScheduleBrandSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()


class ScheduleRowSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    site_id = serializers.IntegerField()
    site_code = serializers.CharField()
    brand_id = serializers.IntegerField(allow_null=True, help_text="Null: the whole store.")
    brand_name = serializers.CharField(allow_null=True)
    every = serializers.ChoiceField(choices=CountEvery.choices)
    first_due_on = serializers.DateField()
    next_due_on = serializers.DateField()
    last_counted_on = serializers.DateField(allow_null=True)
    revision = serializers.IntegerField()


class DueCountSerializer(serializers.Serializer[Any]):
    schedule_id = serializers.UUIDField()
    site_id = serializers.IntegerField()
    site_code = serializers.CharField()
    brand_id = serializers.IntegerField(allow_null=True)
    brand_name = serializers.CharField(allow_null=True)
    due_on = serializers.DateField()


class CountSchedulePageSerializer(serializers.Serializer[Any]):
    today = serializers.DateField()
    can_edit = serializers.BooleanField()
    sites = ScheduleSiteSerializer(many=True)
    brands = ScheduleBrandSerializer(many=True)
    due_today = DueCountSerializer(many=True)
    missed = DueCountSerializer(many=True)
    schedules = ScheduleRowSerializer(many=True)


class ScheduleSetRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    site_id = serializers.IntegerField()
    brand_id = serializers.IntegerField(allow_null=True, help_text="Null: the whole store.")
    every = serializers.ChoiceField(choices=CountEvery.choices)
    first_due_on = serializers.DateField()


class ScheduleChangeRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()
    every = serializers.ChoiceField(choices=CountEvery.choices)
    first_due_on = serializers.DateField()


class ScheduleStopRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()


# -- JSON --------------------------------------------------------------------------


def row_json(
    schedule: CountSchedule, today: date, log: rules.CountLog | None = None
) -> dict[str, Any]:
    brand = schedule.brand
    log = log or rules.CountLog([schedule.site_id])
    return {
        "id": schedule.pk,
        "site_id": schedule.site_id,
        "site_code": schedule.site.code,
        "brand_id": brand.pk if brand else None,
        "brand_name": brand.name if brand else None,
        "every": schedule.every,
        "first_due_on": schedule.first_due_on,
        "next_due_on": rules.next_due(schedule.first_due_on, schedule.every, today),
        "last_counted_on": log.last_counted_on(schedule),
        "revision": schedule.revision,
    }


def due_json(schedule: CountSchedule, due_on: date) -> dict[str, Any]:
    brand = schedule.brand
    return {
        "schedule_id": schedule.pk,
        "site_id": schedule.site_id,
        "site_code": schedule.site.code,
        "brand_id": brand.pk if brand else None,
        "brand_name": brand.name if brand else None,
        "due_on": due_on,
    }


def audit_json(schedule: CountSchedule) -> dict[str, Any]:
    return {
        "store": schedule.site.code,
        "brand": schedule.brand.name if schedule.brand else None,
        "every": schedule.every,
        "first_due_on": schedule.first_due_on.isoformat(),
        "active": schedule.active,
    }


def _missed_rows(tenant_id: uuid.UUID, stores: list[Store]) -> list[dict[str, Any]]:
    """Missed counts at these stores still open - whatever the switch says now."""
    out: list[dict[str, Any]] = []
    rows = rules.open_missed(tenant_id, [store.pk for store in stores])
    found = {row.pk: rules.parse_subject(row.subject_key) for row in rows}
    schedules = {
        s.pk: s
        for s in CountSchedule.objects.filter(
            pk__in=[f[0] for f in found.values() if f is not None]
        ).select_related("site", "brand")
    }
    for row in rows:
        hit = found[row.pk]
        schedule = schedules.get(hit[0]) if hit else None
        if hit is not None and schedule is not None:
            out.append(due_json(schedule, hit[1]))
    return out


# -- the page ------------------------------------------------------------------------


class CountScheduleListView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_outbound_count_schedules_list",
        responses=CountSchedulePageSerializer,
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        if not may_read(request.user):
            raise Refusal("ACTION_DENIED", "You do not have access to Stock Count.")
        today = timezone.localdate()
        in_scope = list(actionable_stores(request.user))
        stores = rules.switched_on(in_scope)
        goods = rules.goods_v1_sites([store.pk for store in stores])
        editor = may_edit(request.user) and bool(stores)
        schedules = rules.live_schedules(stores)
        log = rules.CountLog(store.pk for store in stores)
        body = {
            "today": today,
            "can_edit": editor,
            "sites": [
                {"id": s.pk, "code": s.code, "name": s.name, "goods_v1": s.pk in goods}
                for s in stores
            ],
            "brands": [
                {"id": b.pk, "name": b.name}
                for b in Brand.objects.filter(is_active=True).order_by("name")
            ]
            if editor
            else [],
            "due_today": [
                due_json(d.schedule, d.due_on) for d in rules.due_today(stores, today, schedules)
            ],
            "missed": _missed_rows(access.tenant_id, in_scope),
            "schedules": [row_json(s, today, log) for s in schedules],
        }
        return Response(CountSchedulePageSerializer(body).data)

    @extend_schema(
        operation_id="goods_v1_outbound_count_schedules_set",
        request=ScheduleSetRequestSerializer,
        responses={201: ScheduleRowSerializer},
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        _require_editor(request.user)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"site_id", "brand_id", "every", "first_due_on"},
            required=["site_id", "every", "first_due_on"],
        )
        store = _store_in_scope(request.user, body["site_id"])
        require_feature(store, rules.FEATURE_KEY)
        brand = _brand(body.get("brand_id"))
        every = _every(body)
        today = timezone.localdate()
        first = _first_due_on(body, today)

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, [f"count-schedule:{store.pk}"])
            if CountSchedule.objects.filter(
                tenant_id=run.tenant_id, site=store, brand=brand, active=True
            ).exists():
                raise Refusal(
                    "SCHEDULE_EXISTS",
                    f"{store.code} already has a schedule for "
                    f"{brand.name if brand else 'the whole store'}. Change that one instead.",
                    status=409,
                )
            run.audit_before = {
                "store": store.code,
                "brand": brand.name if brand else None,
                "every": None,
                "first_due_on": None,
                "active": False,
            }
            try:
                row = CountSchedule.objects.create(
                    tenant_id=run.tenant_id,
                    site=store,
                    brand=brand,
                    every=every,
                    first_due_on=first,
                    judged_from=timezone.localdate(run.now),
                    updated_at=run.now,
                )
            except IntegrityError:  # pragma: no cover - the lock above keeps this out
                raise Refusal(
                    "SCHEDULE_EXISTS", "That schedule already exists.", status=409
                ) from None
            run.audit_after = audit_json(row)
            return CommandResult(
                resource_type="count_schedule",
                resource_id=str(row.pk),
                revision=row.revision,
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action=SET_ACTION,
            meta=meta,
            business_input={
                "site_id": store.pk,
                "brand_id": brand.pk if brand else None,
                "every": every,
                "first_due_on": first.isoformat(),
            },
            handler=handler,
            resource_ids=[f"{store.pk}:{brand.pk if brand else 'store'}"],
            subject_key=f"count_schedule:{store.pk}:{brand.pk if brand else 'store'}",
            site_id=store.pk,
        )
        row = CountSchedule.objects.select_related("site", "brand").get(pk=str(result.resource_id))
        return Response(ScheduleRowSerializer(row_json(row, today)).data, status=result.status_code)


class CountScheduleChangeView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_outbound_count_schedules_change",
        request=ScheduleChangeRequestSerializer,
        responses=ScheduleRowSerializer,
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        _require_editor(request.user)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, {"every", "first_due_on"}, required=["every", "first_due_on"]
        )
        schedule = _schedule_in_scope(request.user, pk)
        require_feature(schedule.site, rules.FEATURE_KEY)
        every = _every(body)
        today = timezone.localdate()
        first = _first_due_on(body, today)

        def handler(run: CommandRun) -> CommandResult:
            row = _locked(run, schedule.pk)
            check_revision(meta.expected_revision, row.revision)
            if not row.active:
                raise Refusal("SCHEDULE_STOPPED", "This schedule has been stopped.", status=409)
            run.audit_before = audit_json(row)
            row.every = every
            row.first_due_on = first
            row.judged_from = timezone.localdate(run.now)
            row.revision += 1
            row.updated_at = run.now
            row.save(
                update_fields=["every", "first_due_on", "judged_from", "revision", "updated_at"]
            )
            run.audit_after = audit_json(row)
            return CommandResult(
                resource_type="count_schedule", resource_id=str(row.pk), revision=row.revision
            )

        result = self.run_command(
            request,
            access=access,
            action=CHANGE_ACTION,
            meta=meta,
            business_input={
                "schedule_id": str(schedule.pk),
                "every": every,
                "first_due_on": first.isoformat(),
            },
            handler=handler,
            resource_ids=[str(schedule.pk)],
            subject_key=f"count_schedule:{schedule.pk}",
            site_id=schedule.site_id,
        )
        schedule.refresh_from_db()
        return Response(
            ScheduleRowSerializer(row_json(schedule, today)).data, status=result.status_code
        )


class CountScheduleStopView(GoodsAPIView):
    @extend_schema(request=ScheduleStopRequestSerializer, responses=ScheduleRowSerializer)
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        _require_editor(request.user)
        meta = parse_meta(request.data, revision_bound=True)
        business_body(request.data, set())
        schedule = _schedule_in_scope(request.user, pk)

        def handler(run: CommandRun) -> CommandResult:
            row = _locked(run, schedule.pk)
            check_revision(meta.expected_revision, row.revision)
            if not row.active:
                raise Refusal("SCHEDULE_STOPPED", "This schedule is already stopped.", status=409)
            run.audit_before = audit_json(row)
            row.active = False
            row.revision += 1
            row.updated_at = run.now
            row.save(update_fields=["active", "revision", "updated_at"])
            closed = rules.close_for_stopped(run, row)
            run.audit_after = {**audit_json(row), "missed_counts_closed": closed}
            return CommandResult(
                resource_type="count_schedule", resource_id=str(row.pk), revision=row.revision
            )

        result = self.run_command(
            request,
            access=access,
            action=STOP_ACTION,
            meta=meta,
            business_input={"schedule_id": str(schedule.pk)},
            handler=handler,
            resource_ids=[str(schedule.pk)],
            subject_key=f"count_schedule:{schedule.pk}",
            site_id=schedule.site_id,
        )
        schedule.refresh_from_db()
        return Response(
            ScheduleRowSerializer(row_json(schedule, timezone.localdate())).data,
            status=result.status_code,
        )


# -- checks shared by the writes -------------------------------------------------------


def _require_editor(user: Any) -> None:
    if not may_edit(user):
        raise Refusal("ACTION_DENIED", "Only the Owner sets a store's count schedule.")


def _store_in_scope(user: Any, raw: Any) -> Store:
    store_id = parse_int_id(raw, "site_id")
    allowed = actionable_store_ids(user)
    store = Store.objects.filter(pk=store_id, is_active=True).first()
    if store is None or (allowed is not None and store.pk not in allowed):
        raise Refusal("NOT_FOUND", "That store was not found.", status=404)
    return store


def _schedule_in_scope(user: Any, pk: uuid.UUID) -> CountSchedule:
    schedule = CountSchedule.objects.select_related("site", "brand").filter(pk=pk).first()
    allowed = actionable_store_ids(user)
    if schedule is None or (allowed is not None and schedule.site_id not in allowed):
        raise Refusal("NOT_FOUND", "That schedule was not found.", status=404)
    return schedule


def _locked(run: CommandRun, pk: uuid.UUID) -> CountSchedule:
    # The brand is nullable: lock this table's row alone (see ``CommandRun.lock``).
    rows = run.lock(
        LockRank.DOCUMENT,
        CountSchedule.objects.filter(pk=pk).select_related("site", "brand"),
        of=("self",),
    )
    row: CountSchedule = rows[0]
    return row


def _brand(raw: Any) -> Brand | None:
    if raw is None:
        return None
    brand = Brand.objects.filter(pk=parse_int_id(raw, "brand_id"), is_active=True).first()
    if brand is None:
        raise Refusal("NOT_FOUND", "That brand was not found.", status=404)
    return brand


def _every(body: dict[str, Any]) -> str:
    every = body.get("every")
    if every not in CountEvery.values:
        raise Refusal(
            "INVALID_REQUEST",
            "every must be week, month or quarter.",
            issues=[issue("INVALID", "not week, month or quarter", field="every")],
        )
    return str(every)


def _first_due_on(body: dict[str, Any], today: date) -> date:
    raw = body.get("first_due_on")
    parsed = parse_date(raw) if isinstance(raw, str) else None
    if parsed is None:
        raise Refusal(
            "INVALID_REQUEST",
            "first_due_on must be a date (YYYY-MM-DD).",
            issues=[issue("INVALID", "not a date", field="first_due_on")],
        )
    if parsed < today:
        raise Refusal(
            "INVALID_REQUEST",
            "The first count is due today or later: a schedule never makes a past day late.",
            issues=[issue("INVALID", "in the past", field="first_due_on")],
        )
    return parsed
