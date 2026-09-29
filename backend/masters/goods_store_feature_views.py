"""Setup > Feature Switches (store operations PRD ST-OPS-6, ticket 01).

Under ``/api/goods-v1/masters/`` (the switch is a goods-v1 command):

``GET  store-features``         every registered feature at every store in the
                                caller's scope (``setup: view``)
``POST store-features/switch``  change one switch (Admin only)
``GET  store-features/probe``   the demo probe's own gated request

Only Admin changes a switch: ``setup: manage`` *and* a role code listed in
``accounts.role_lists.STORE_FEATURE_EDITOR_ROLES`` (``it_admin``). ``setup: manage``
alone would also let Owner in, which the PRD does not (baseline B6: narrow, never
widen). Every change runs as one command, so its ``AuditEvent`` records who, when,
which store, and the value before and after.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    check_revision,
    parse_meta,
)
from accounts.goods_models import HumanIdentity
from accounts.permissions import user_can
from accounts.role_lists import STORE_FEATURE_EDITOR_ROLES
from accounts.sections import CAP_MANAGE, CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank
from core.kernel_models import AuditEvent
from core.refusals import Refusal, issue
from masters.document_series import FEATURE_KEY as DOCUMENT_SERIES_KEY
from masters.document_series import switch_change_problem
from masters.models import Store
from masters.scoping import actionable_store_ids, actionable_stores
from masters.store_feature_models import StoreFeatureSwitch
from masters.store_feature_registry import StoreFeature, open_gates, registered_features
from masters.store_features import (
    CONNECTED,
    MANUAL,
    SwitchState,
    feature,
    gate_lock,
    is_real_store,
    real_stores,
    require_feature,
    switch_states,
)

SWITCH_ACTION = "masters.store_feature.switch"
RECENT_CHANGES = 20
PROBE_KEY = "demo-probe"


def may_change_switches(user: Any) -> bool:
    """Admin only: ``setup: manage`` and an editor role code (or break-glass)."""
    if getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    code = getattr(role, "code", "")
    return user_can(user, "setup", CAP_MANAGE) and code in STORE_FEATURE_EDITOR_ROLES


def stores_in_scope(user: Any) -> list[Store]:
    """The stores this person may see switches for: their own scope, fail-closed.

    A brand-scoped person gets none - stores are not their boundary.
    """
    return list(actionable_stores(user))


def state_json(state: SwitchState) -> dict[str, Any]:
    return {
        "store_id": state.site_id,
        "feature_key": state.feature.key,
        "enabled": state.enabled,
        "chosen": state.chosen,
        "mode": state.mode,
        "revision": state.revision,
        "locked_reason": state.locked_reason,
    }


class FeatureSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    gate = serializers.CharField(allow_null=True)
    has_modes = serializers.BooleanField()
    default_on = serializers.BooleanField()


class SwitchStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    store_type = serializers.CharField()
    real = serializers.BooleanField()


class SwitchStateSerializer(serializers.Serializer[Any]):
    store_id = serializers.IntegerField()
    feature_key = serializers.CharField()
    enabled = serializers.BooleanField(help_text="What the server enforces.")
    chosen = serializers.BooleanField(help_text="What Admin chose, or the default.")
    mode = serializers.ChoiceField(choices=[MANUAL, CONNECTED])
    revision = serializers.IntegerField(help_text="0 while the default is in force.")
    locked_reason = serializers.CharField(allow_null=True)


class SwitchChangeSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    at = serializers.DateTimeField()
    by = serializers.CharField()
    store_id = serializers.IntegerField()
    feature_key = serializers.CharField()
    before = serializers.JSONField()
    after = serializers.JSONField()


class FeatureSwitchesSerializer(serializers.Serializer[Any]):
    features = FeatureSerializer(many=True)
    stores = SwitchStoreSerializer(many=True)
    switches = SwitchStateSerializer(many=True)
    open_gates = serializers.ListField(child=serializers.CharField())
    can_change = serializers.BooleanField()
    changes = SwitchChangeSerializer(many=True)


class SwitchRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(required=False)
    store_id = serializers.IntegerField()
    feature_key = serializers.CharField()
    enabled = serializers.BooleanField()
    mode = serializers.ChoiceField(choices=[MANUAL, CONNECTED], required=False)


class ProbeSerializer(serializers.Serializer[Any]):
    store_id = serializers.IntegerField()
    feature_key = serializers.CharField()
    mode = serializers.CharField()


def _changes(stores: list[Store]) -> list[dict[str, Any]]:
    events = list(
        AuditEvent.objects.filter(
            action=SWITCH_ACTION, outcome="succeeded", site_id__in=[s.pk for s in stores]
        ).order_by("-recorded_at")[:RECENT_CHANGES]
    )
    names = dict(
        HumanIdentity.objects.filter(pk__in={e.actor_id for e in events if e.actor_id}).values_list(
            "pk", "display_name"
        )
    )
    return [
        {
            "id": str(event.pk),
            "at": event.recorded_at,
            "by": names.get(event.actor_id, "") if event.actor_id else (event.service_code or ""),
            "store_id": event.site_id,
            "feature_key": (event.after or {}).get("feature_key", ""),
            "before": event.before,
            "after": event.after,
        }
        for event in events
    ]


class GoodsStoreFeatureListView(GoodsAPIView):
    @extend_schema(responses=FeatureSwitchesSerializer)
    def get(self, request: Request) -> Response:
        self.access(request)
        check_query(request, allowed=())
        if not user_can(request.user, "setup", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Setup.")
        stores = stores_in_scope(request.user)
        real = real_stores(stores)
        body = {
            "features": [
                {
                    "key": f.key,
                    "name": f.name,
                    "description": f.description,
                    "gate": f.gate,
                    "has_modes": f.has_modes,
                    "default_on": f.default_on,
                }
                for f in registered_features()
            ],
            "stores": [
                {
                    "id": s.pk,
                    "code": s.code,
                    "name": s.name,
                    "store_type": s.store_type,
                    "real": real[s.pk],
                }
                for s in stores
            ],
            "switches": [state_json(state) for state in switch_states(stores)],
            "open_gates": open_gates(),
            "can_change": may_change_switches(request.user),
            "changes": _changes(stores),
        }
        return Response(FeatureSwitchesSerializer(body).data)


def _enabled(body: dict[str, Any]) -> bool:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise Refusal(
            "INVALID_REQUEST",
            "enabled must be true or false.",
            issues=[issue("INVALID", "enabled must be a boolean", field="enabled")],
        )
    return enabled


def _feature(body: dict[str, Any]) -> StoreFeature:
    try:
        return feature(str(body["feature_key"]))
    except LookupError:
        raise Refusal("NOT_FOUND", "That feature is not registered.") from None


def _mode(body: dict[str, Any], target: StoreFeature) -> str | None:
    mode = body.get("mode")
    if mode is not None and mode not in (MANUAL, CONNECTED):
        raise Refusal(
            "INVALID_REQUEST",
            "mode must be manual or connected.",
            issues=[issue("INVALID", "mode must be manual or connected", field="mode")],
        )
    if mode == CONNECTED and not target.has_modes:
        raise Refusal(
            "INVALID_REQUEST",
            f"{target.name} has no connected provider.",
            issues=[issue("INVALID", "this feature has no modes", field="mode")],
        )
    return mode


def store_in_scope(user: Any, store_id: Any) -> Store:
    """An active store the caller may act at, or ``NOT_FOUND`` - never a hint."""
    if isinstance(store_id, bool) or not isinstance(store_id, int):
        raise Refusal("INVALID_REQUEST", "store_id must be a store id.")
    allowed = actionable_store_ids(user)
    store = Store.objects.filter(pk=store_id, is_active=True).first()
    if store is None or (allowed is not None and store.pk not in allowed):
        raise Refusal("NOT_FOUND", "That store was not found.")
    return store


class GoodsStoreFeatureSwitchView(GoodsAPIView):
    @extend_schema(request=SwitchRequestSerializer, responses=SwitchStateSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        if not may_change_switches(request.user):
            raise Refusal("ACTION_DENIED", "Only Admin can change a feature switch.")
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"store_id", "feature_key", "enabled", "mode"},
            required=["store_id", "feature_key"],
        )
        enabled = _enabled(body)
        target = _feature(body)
        mode = _mode(body, target)
        store = store_in_scope(request.user, body["store_id"])
        lock = gate_lock(target, real=is_real_store(store))
        if enabled and lock is not None:
            raise Refusal("FEATURE_GATED", lock, status=409)

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, [f"store-feature:{store.pk}:{target.key}"])
            row = StoreFeatureSwitch.objects.filter(
                tenant_id=run.tenant_id, site=store, feature_key=target.key
            ).first()
            check_revision(meta.expected_revision, row.revision if row else 0)
            before_enabled = row.enabled if row else target.default_on
            if target.key == DOCUMENT_SERIES_KEY and enabled != before_enabled:
                # Ticket 04: switching changes the store's number format, which
                # may change only on 1 April (§6 principle 5).
                problem = switch_change_problem(run.tenant_id, timezone.localdate(run.now))
                if problem is not None:
                    raise Refusal("FORMAT_STARTED", problem, status=409)
            before_mode = row.mode if row else MANUAL
            after_mode = mode or before_mode
            run.audit_before = {
                "feature_key": target.key,
                "store": store.code,
                "enabled": before_enabled,
                "mode": before_mode,
            }
            if row is None:
                row = StoreFeatureSwitch.objects.create(
                    tenant_id=run.tenant_id,
                    site=store,
                    feature_key=target.key,
                    enabled=enabled,
                    mode=after_mode,
                    updated_at=run.now,
                )
            else:
                row.enabled = enabled
                row.mode = after_mode
                row.revision += 1
                row.updated_at = run.now
                row.save(update_fields=["enabled", "mode", "revision", "updated_at"])
            run.audit_after = {
                "feature_key": target.key,
                "store": store.code,
                "enabled": enabled,
                "mode": after_mode,
            }
            return CommandResult(
                resource_type="store_feature_switch", resource_id=str(row.pk), revision=row.revision
            )

        result = self.run_command(
            request,
            access=access,
            action=SWITCH_ACTION,
            meta=meta,
            business_input={
                "store_id": store.pk,
                "feature_key": target.key,
                "enabled": enabled,
                "mode": mode,
            },
            handler=handler,
            resource_ids=[f"{store.pk}:{target.key}"],
            subject_key=f"store_feature:{store.pk}:{target.key}",
            site_id=store.pk,
        )
        state = switch_states([store], [target])[0]
        return Response(SwitchStateSerializer(state_json(state)).data, status=result.status_code)


class GoodsStoreFeatureProbeView(GoodsAPIView):
    """The demo probe's gated request: answers only while the probe is on."""

    @extend_schema(
        parameters=[OpenApiParameter("store_id", int, required=True)],
        responses=ProbeSerializer,
    )
    def get(self, request: Request) -> Response:
        self.access(request)
        check_query(request, allowed={"store_id"})
        if not settings.KDPS_STORE_FEATURE_DEMO_PROBES:
            raise Refusal("NOT_FOUND", "There is no demo probe here.")
        try:
            store_id = int(request.query_params.get("store_id", ""))
        except ValueError:
            raise Refusal("INVALID_REQUEST", "store_id must be a store id.") from None
        store = store_in_scope(request.user, store_id)
        require_feature(store, PROBE_KEY)
        state = switch_states([store], [feature(PROBE_KEY)])[0]
        return Response({"store_id": store.pk, "feature_key": PROBE_KEY, "mode": state.mode})
