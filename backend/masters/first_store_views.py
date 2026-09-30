"""Selected-store counter setup using the existing counter writer."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, parse_meta
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal
from masters.goods_models import SiteGuard
from masters.models import Store
from sell.services.till_authority import TillError, active_till, register_till


class CounterSetupRead(serializers.Serializer[Any]):
    site_id = serializers.IntegerField()
    revision = serializers.IntegerField()
    registered = serializers.BooleanField()
    device_id = serializers.CharField(allow_null=True)
    counter_id = serializers.CharField(allow_null=True)
    device_token = serializers.CharField(required=False)
    token_issued = serializers.BooleanField(required=False)


class CounterSetupWrite(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(min_value=1)
    replace = serializers.BooleanField(default=False)
    reason = serializers.CharField(max_length=240, allow_blank=True, default="")


def _summary(site: Store, guard: SiteGuard) -> dict[str, Any]:
    till = active_till(site)
    return {"site_id": site.pk, "revision": guard.revision, "registered": till is not None,
            "device_id": str(till.pk) if till else None,
            "counter_id": till.counter_id if till else None}


class FirstStoreCounterView(GoodsAPIView):
    @extend_schema(responses={200: CounterSetupRead})
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        access.require("org.site.manage", site_id=pk)
        site = Store.objects.filter(pk=pk, tenant_id=access.tenant_id).first()
        guard = SiteGuard.objects.filter(site_id=pk, tenant_id=access.tenant_id).first()
        if site is None or guard is None:
            raise Refusal("NOT_FOUND", "That store was not found.")
        body = _summary(site, guard)
        access.revalidate_delivery()
        response = Response(body)
        response["Cache-Control"] = "no-store, private"
        return response

    @extend_schema(request=CounterSetupWrite, responses={201: CounterSetupRead})
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        raw = business_body(request.data, {"replace", "reason"})
        form = CounterSetupWrite(data=request.data)
        form.is_valid(raise_exception=True)
        body = {"replace": form.validated_data["replace"], "reason": form.validated_data["reason"]}
        access.require("org.site.manage", site_id=pk)
        access.require_step_up()
        site = Store.objects.filter(pk=pk, tenant_id=access.tenant_id, store_type="store").first()
        if site is None:
            raise Refusal("NOT_FOUND", "That store was not found.")
        token: str | None = None

        def handler(run: CommandRun) -> CommandResult:
            nonlocal token
            rows = run.lock(LockRank.SITE, SiteGuard.objects.filter(site=site, tenant_id=run.tenant_id))
            if not rows:
                raise Refusal("NOT_FOUND", "That store was not found.")
            guard = rows[0]
            if guard.revision != meta.expected_revision:
                raise Refusal("REVISION_SUPERSEDED", "Reload this store before changing its counter.")
            if guard.lifecycle in {SiteGuard.Lifecycle.CLOSING, SiteGuard.Lifecycle.CLOSED}:
                raise Refusal("SITE_INACTIVE", "A closing or closed store cannot register a counter.")
            run.audit_before = _summary(site, guard)
            try:
                till, token = register_till(site, request.user, replace=body["replace"], reason=body["reason"])
            except TillError as exc:
                raise Refusal(exc.code, exc.message, status=exc.status) from None
            guard.sell_ready = False
            guard.revision += 1
            guard.save(update_fields=["sell_ready", "revision"])
            run.audit_after = _summary(site, guard)
            return CommandResult(resource_type="counter", resource_id=str(till.pk), status_code=201)

        result = self.run_command(request, access=access, action="org.site.manage", meta=meta,
                                  business_input=raw, handler=handler, site_id=pk,
                                  subject_key=f"counter:{pk}")
        guard = SiteGuard.objects.get(site=site)
        response_body = _summary(site, guard)
        response_body["token_issued"] = token is not None
        if token is not None:
            response_body["device_token"] = token
        access.revalidate_delivery()
        response = Response(response_body, status=result.status_code)
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        return response
