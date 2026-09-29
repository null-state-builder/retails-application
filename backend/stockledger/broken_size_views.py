"""Stock > Broken Sizes (store operations ticket 32, ST-INV-1).

``GET  /api/goods-v1/stock/broken-sizes[?site_id=]``  the store's broken-size alerts,
                                                      the 7-day measure and the rules
``POST /api/goods-v1/stock/broken-sizes/act``         record the action taken on one
``POST /api/goods-v1/stock/broken-sizes/rules``       set a category's core sizes and share

Readers hold ``stock: view`` or higher, for the selling stores in their own scope
where the ``broken-size`` switch is on. A store outside that is not found. The
read changes nothing and carries no cost.

Acting on an alert is the store's work: it needs ``transfer: operate`` or higher
(asking for a transfer is what fixes a broken size) as well as ``stock: view``,
at a store the person may read. An alert is acted on once; the first action
recorded is the one the 7-day measure counts. A closed alert is not acted on.

A category's rule is master data for the whole company, so it is written by the
master-data stewards (``masters.writes``: Owner, IT Admin, data steward), and
only while the switch is on at a store they can see. Every write runs as one
command, so its ``AuditEvent`` records who, when, and the values before and after.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.actor_policies import user_may_act
from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    parse_int_id,
    parse_meta,
)
from accounts.permissions import user_can
from accounts.sections import CAP_OPERATE, CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal, issue
from masters.models import Store
from masters.permissions import MASTER_WRITES
from masters.scoping import actionable_stores
from masters.store_feature_registry import BROKEN_SIZE
from masters.store_features import feature, switch_states
from stockledger.broken_size import NO_CATEGORY, measure, outcome, size_key, store_sizes
from stockledger.broken_size_models import BrokenSizeAlert, SizeRule

ACT_ACTION = "stock.broken_size.act"
RULE_ACTION = "masters.size_rule.save"
#: Closed alerts the page lists, newest first.
CLOSED_SHOWN = 50
MAX_CORE_SIZES = 30
SIZE_LENGTH = 24
NOTE_LENGTH = 240


class BrokenSizeStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class HeldSizeSerializer(serializers.Serializer[Any]):
    size = serializers.CharField()
    qty = serializers.IntegerField()


class BrokenSizeAlertSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    style_code = serializers.CharField()
    colour = serializers.CharField(allow_blank=True)
    category = serializers.CharField(allow_blank=True)
    core_sizes = serializers.ListField(child=serializers.CharField())
    missing_sizes = serializers.ListField(child=serializers.CharField())
    held = HeldSizeSerializer(many=True)
    pieces = serializers.IntegerField()
    rule_percent = serializers.IntegerField()
    missing_percent = serializers.IntegerField()
    opened_at = serializers.DateTimeField()
    checked_at = serializers.DateTimeField()
    acted_at = serializers.DateTimeField(allow_null=True)
    acted_by = serializers.CharField(allow_blank=True)
    action = serializers.ChoiceField(choices=[""] + list(BrokenSizeAlert.Action.values))
    action_note = serializers.CharField(allow_blank=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    closed_reason = serializers.ChoiceField(choices=[""] + list(BrokenSizeAlert.Closed.values))
    outcome = serializers.ChoiceField(
        choices=["on_time", "missed", "waiting", "closed_first"],
        help_text="Where this alert stands in the 7-day measure.",
    )


class BrokenSizeMeasureSerializer(serializers.Serializer[Any]):
    counted = serializers.IntegerField(
        help_text="Alerts acted on within 7 days, plus those whose 7 days ran out."
    )
    on_time = serializers.IntegerField()
    waiting = serializers.IntegerField(help_text="Still inside their 7 days, not acted on.")
    closed_first = serializers.IntegerField(
        help_text="Closed by themselves inside their 7 days, before anybody acted."
    )
    percent = serializers.IntegerField(allow_null=True)


class SizeRuleSerializer(serializers.Serializer[Any]):
    category = serializers.CharField(allow_blank=True, help_text="Blank is 'No category'.")
    core_sizes = serializers.ListField(child=serializers.CharField())
    missing_percent = serializers.IntegerField()
    active = serializers.BooleanField()
    revision = serializers.IntegerField()


class BrokenSizesSerializer(serializers.Serializer[Any]):
    stores = BrokenSizeStoreSerializer(many=True)
    site_id = serializers.IntegerField(allow_null=True)
    now = serializers.DateTimeField()
    goods_records = serializers.BooleanField(
        help_text="False: this store's stock is not on the goods records, so it cannot be checked."
    )
    checked_at = serializers.DateTimeField(
        allow_null=True, help_text="When the daily check last looked at this store's alerts."
    )
    open = BrokenSizeAlertSerializer(many=True)
    closed = BrokenSizeAlertSerializer(many=True)
    measure = BrokenSizeMeasureSerializer()
    rules = SizeRuleSerializer(many=True)
    unruled_categories = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        help_text="Categories held here that no rule covers; their stock is not checked.",
    )
    mixed = serializers.ListField(
        child=serializers.CharField(),
        help_text="Style-colours whose items name two categories; not checked.",
    )
    can_act = serializers.BooleanField()
    can_set_rules = serializers.BooleanField()


class BrokenSizeActRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    alert_id = serializers.UUIDField()
    action = serializers.ChoiceField(choices=list(BrokenSizeAlert.Action.values))
    note = serializers.CharField(
        required=False, allow_blank=True, help_text="Required for 'other'."
    )


class SizeRuleRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    category = serializers.CharField(allow_blank=True, help_text="Blank is 'No category'.")
    core_sizes = serializers.ListField(child=serializers.CharField())
    missing_percent = serializers.IntegerField()
    active = serializers.BooleanField(
        required=False, help_text="False removes the rule; its category is then not checked."
    )


def broken_size_stores(user: Any) -> list[Store]:
    """The selling stores in this person's scope where the switch is on."""
    stores = list(actionable_stores(user).filter(store_type=Store.StoreType.STORE))
    states = switch_states(stores, [feature(BROKEN_SIZE)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def may_act(user: Any) -> bool:
    return user_can(user, "stock", CAP_VIEW) and user_can(user, "transfer", CAP_OPERATE)


def alert_json(alert: BrokenSizeAlert, names: dict[Any, str], now: Any) -> dict[str, Any]:
    return {
        "id": str(alert.pk),
        "brand": alert.brand,
        "style_code": alert.style_code,
        "colour": alert.colour,
        "category": alert.category,
        "core_sizes": list(alert.core_sizes),
        "missing_sizes": list(alert.missing_sizes),
        "held": [{"size": size, "qty": qty} for size, qty in (alert.held or {}).items()],
        "pieces": alert.pieces,
        "rule_percent": alert.rule_percent,
        "missing_percent": alert.missing_percent,
        "opened_at": alert.opened_at,
        "checked_at": alert.checked_at,
        "acted_at": alert.acted_at,
        "acted_by": names.get(alert.acted_by_id, "") if alert.acted_by_id else "",
        "action": alert.action,
        "action_note": alert.action_note,
        "closed_at": alert.closed_at,
        "closed_reason": alert.closed_reason,
        "outcome": outcome(alert.opened_at, alert.acted_at, alert.closed_at, now),
    }


def rule_json(rule: SizeRule) -> dict[str, Any]:
    return {
        "category": rule.category,
        "core_sizes": list(rule.core_sizes),
        "missing_percent": rule.missing_percent,
        "active": rule.active,
        "revision": rule.revision,
    }


def _names(alerts: list[BrokenSizeAlert]) -> dict[Any, str]:
    from accounts.goods_models import HumanIdentity

    ids = {alert.acted_by_id for alert in alerts if alert.acted_by_id}
    return dict(HumanIdentity.objects.filter(pk__in=ids).values_list("pk", "display_name"))


class GoodsBrokenSizesView(GoodsAPIView):
    """The store's broken-size alerts (``stock: view``, in scope, switch on)."""

    @extend_schema(
        parameters=[OpenApiParameter("site_id", int, required=False)],
        responses=BrokenSizesSerializer,
    )
    def get(self, request: Request) -> Response:
        self.access(request)
        params = check_query(request, allowed=("site_id",))
        if not user_can(request.user, "stock", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Stock.")
        stores = broken_size_stores(request.user)
        chosen: Store | None = stores[0] if stores else None
        if params.get("site_id"):
            wanted = parse_int_id(params["site_id"], "site_id")
            chosen = next((store for store in stores if store.pk == wanted), None)
            if chosen is None:
                raise Refusal(
                    "NOT_FOUND", "That store's broken sizes are not available.", status=404
                )
        now = timezone.now()
        body: dict[str, Any] = {
            "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in stores],
            "site_id": chosen.pk if chosen is not None else None,
            "now": now,
            "goods_records": True,
            "checked_at": None,
            "open": [],
            "closed": [],
            "measure": measure([], now).as_json(),
            "rules": [],
            "unruled_categories": [],
            "mixed": [],
            "can_act": False,
            "can_set_rules": False,
        }
        if chosen is not None:
            alerts = list(BrokenSizeAlert.objects.filter(site=chosen).order_by("opened_at", "id"))
            names = _names(alerts)
            opened = [a for a in alerts if a.closed_at is None]
            closed = sorted(
                (a for a in alerts if a.closed_at is not None),
                key=lambda a: (a.closed_at, str(a.pk)),
                reverse=True,
            )[:CLOSED_SHOWN]
            sizes = store_sizes(chosen)
            body.update(
                goods_records=sizes.goods_records,
                checked_at=max((a.checked_at for a in opened), default=None),
                open=[alert_json(a, names, now) for a in opened],
                closed=[alert_json(a, names, now) for a in closed],
                measure=measure(alerts, now).as_json(),
                rules=[rule_json(rule) for rule in SizeRule.objects.order_by("category")],
                unruled_categories=sizes.unruled_categories,
                mixed=[
                    " ".join(filter(None, [row.brand, row.style_code, row.colour]))
                    + f" ({row.category})"
                    for row in sizes.mixed
                ],
                can_act=may_act(request.user),
                can_set_rules=user_may_act(request.user, MASTER_WRITES),
            )
        return Response(BrokenSizesSerializer(body).data)


def _note(body: dict[str, Any]) -> str:
    raw = body.get("note", "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise Refusal(
            "INVALID_REQUEST",
            "note must be text.",
            issues=[issue("INVALID", "not text", field="note")],
        )
    note = " ".join(raw.split())
    if len(note) > NOTE_LENGTH:
        raise Refusal(
            "INVALID_REQUEST",
            f"The note can be at most {NOTE_LENGTH} characters.",
            issues=[issue("INVALID", "too long", field="note")],
        )
    return note


class GoodsBrokenSizeActView(GoodsAPIView):
    """Record the action taken on one broken-size alert (store's work; audited)."""

    @extend_schema(request=BrokenSizeActRequestSerializer, responses=BrokenSizeAlertSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        if not may_act(request.user):
            raise Refusal(
                "ACTION_DENIED", "Only store staff who can ask for transfers act on these."
            )
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, {"alert_id", "action", "note"}, required=["alert_id", "action"]
        )
        action = body["action"]
        if action not in BrokenSizeAlert.Action.values:
            raise Refusal(
                "INVALID_REQUEST",
                "action must be transfer, markdown or other.",
                issues=[issue("INVALID", "unknown action", field="action")],
            )
        note = _note(body)
        if action == BrokenSizeAlert.Action.OTHER and not note:
            raise Refusal(
                "INVALID_REQUEST",
                "Say what was done in the note.",
                issues=[issue("REQUIRED", "a note is required for 'other'", field="note")],
            )
        alert = _alert_in_scope(request.user, body["alert_id"])

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, [f"broken-size:{alert.pk}"])
            row = BrokenSizeAlert.objects.select_for_update().get(pk=alert.pk)
            if row.closed_at is not None:
                raise Refusal(
                    "ALERT_CLOSED", "This alert has closed; there is nothing to act on.", status=409
                )
            if row.acted_at is not None:
                raise Refusal("ALREADY_ACTED", "This alert has already been acted on.", status=409)
            run.audit_before = _act_audit(row)
            row.acted_at = run.now
            row.acted_by_id = run.principal.human_id
            row.action = action
            row.action_note = note
            row.save(update_fields=["acted_at", "acted_by", "action", "action_note"])
            run.audit_after = _act_audit(row)
            return CommandResult(
                resource_type="broken_size_alert", resource_id=str(row.pk), revision=1
            )

        result = self.run_command(
            request,
            access=access,
            action=ACT_ACTION,
            meta=meta,
            business_input={"alert_id": str(alert.pk), "action": action, "note": note},
            handler=handler,
            resource_ids=[f"broken_size_alert:{alert.pk}"],
            subject_key=f"broken_size_alert:{alert.pk}",
            site_id=alert.site_id,
        )
        alert.refresh_from_db()
        payload = alert_json(alert, _names([alert]), timezone.now())
        return Response(BrokenSizeAlertSerializer(payload).data, status=result.status_code)


def _alert_in_scope(user: Any, raw_id: Any) -> BrokenSizeAlert:
    import uuid

    try:
        alert_id = uuid.UUID(str(raw_id))
    except ValueError as exc:
        raise Refusal(
            "INVALID_REQUEST",
            "alert_id must be an alert's id.",
            issues=[issue("INVALID", "not an id", field="alert_id")],
        ) from exc
    stores = {store.pk for store in broken_size_stores(user)}
    alert = BrokenSizeAlert.objects.filter(pk=alert_id, site_id__in=sorted(stores)).first()
    if alert is None:
        raise Refusal("NOT_FOUND", "That broken-size alert was not found.", status=404)
    return alert


def _act_audit(row: BrokenSizeAlert) -> dict[str, Any]:
    return {
        "style": row.style_code,
        "colour": row.colour,
        "acted_at": row.acted_at.isoformat() if row.acted_at else None,
        "action": row.action,
        "note": row.action_note,
    }


def _category(body: dict[str, Any]) -> str:
    raw = body.get("category")
    if not isinstance(raw, str):
        raise Refusal(
            "INVALID_REQUEST",
            "category must be text (blank for 'No category').",
            issues=[issue("INVALID", "not text", field="category")],
        )
    category = " ".join(raw.split())
    if len(category) > 120:
        raise Refusal(
            "INVALID_REQUEST",
            "A category name can be at most 120 characters.",
            issues=[issue("INVALID", "too long", field="category")],
        )
    return category


def _core_sizes(body: dict[str, Any]) -> list[str]:
    raw = body.get("core_sizes")
    if not isinstance(raw, list) or not all(isinstance(size, str) for size in raw):
        raise Refusal(
            "INVALID_REQUEST",
            "core_sizes must be a list of sizes.",
            issues=[issue("INVALID", "not a list of sizes", field="core_sizes")],
        )
    sizes = [" ".join(size.split()) for size in raw]
    if not sizes or any(not size for size in sizes):
        raise Refusal(
            "INVALID_REQUEST",
            "List at least one core size, and no blank ones.",
            issues=[issue("INVALID", "a blank or empty list", field="core_sizes")],
        )
    if len(sizes) > MAX_CORE_SIZES or any(len(size) > SIZE_LENGTH for size in sizes):
        raise Refusal(
            "INVALID_REQUEST",
            f"At most {MAX_CORE_SIZES} core sizes, each at most {SIZE_LENGTH} characters.",
            issues=[issue("INVALID", "too many or too long", field="core_sizes")],
        )
    if len({size_key(size) for size in sizes}) != len(sizes):
        raise Refusal(
            "INVALID_REQUEST",
            "A size is listed twice.",
            issues=[issue("INVALID", "a size is listed twice", field="core_sizes")],
        )
    return sizes


def _percent(body: dict[str, Any]) -> int:
    raw = body.get("missing_percent")
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 100:
        raise Refusal(
            "INVALID_REQUEST",
            "missing_percent must be a whole number from 1 to 100.",
            issues=[issue("INVALID", "not 1 to 100", field="missing_percent")],
        )
    return raw


def _rule_audit(rule: SizeRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "category": rule.category,
        "core_sizes": list(rule.core_sizes),
        "missing_percent": rule.missing_percent,
        "active": rule.active,
    }


class GoodsSizeRuleView(GoodsAPIView):
    """Set one category's core sizes and share (master-data stewards; audited)."""

    @extend_schema(request=SizeRuleRequestSerializer, responses=SizeRuleSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        if not user_may_act(request.user, MASTER_WRITES):
            raise Refusal("ACTION_DENIED", "Only a master-data steward sets core sizes.")
        if not broken_size_stores(request.user):
            raise Refusal(
                "FEATURE_OFF",
                "Broken-size alerts are not switched on at any store you work at.",
                status=403,
            )
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"category", "core_sizes", "missing_percent", "active"},
            required=["core_sizes", "missing_percent"],
        )
        # Blank is a real answer here ("No category"), so only a missing key is refused.
        if "category" not in body:
            raise Refusal(
                "INVALID_REQUEST",
                "category is required (blank for 'No category').",
                issues=[issue("REQUIRED", "category is required", field="category")],
            )
        category = _category(body)
        sizes = _core_sizes(body)
        percent = _percent(body)
        active = body.get("active", True)
        if not isinstance(active, bool):
            raise Refusal(
                "INVALID_REQUEST",
                "active must be true or false.",
                issues=[issue("INVALID", "not true or false", field="active")],
            )
        key = size_key(category)

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, [f"size-rule:{key}"])
            # "Shirt" and "SHIRT" are one category: the rule already kept for it,
            # whatever its spelling, is the one changed.
            row = SizeRule.objects.select_for_update().filter(category_key=key).first()
            run.audit_before = _rule_audit(row)
            if row is None:
                row = SizeRule.objects.create(
                    tenant_id=run.tenant_id,
                    category=category,
                    category_key=key,
                    core_sizes=sizes,
                    missing_percent=percent,
                    active=active,
                    updated_at=run.now,
                )
            else:
                row.category = category
                row.core_sizes = sizes
                row.missing_percent = percent
                row.active = active
                row.revision += 1
                row.updated_at = run.now
                row.save(
                    update_fields=[
                        "category",
                        "core_sizes",
                        "missing_percent",
                        "active",
                        "revision",
                        "updated_at",
                    ]
                )
            run.audit_after = _rule_audit(row)
            return CommandResult(
                resource_type="size_rule", resource_id=str(row.pk), revision=row.revision
            )

        result = self.run_command(
            request,
            access=access,
            action=RULE_ACTION,
            meta=meta,
            business_input={
                "category": category,
                "core_sizes": sizes,
                "missing_percent": percent,
                "active": active,
            },
            handler=handler,
            resource_ids=[f"size_rule:{key or NO_CATEGORY}"],
            subject_key=f"size_rule:{key}",
        )
        saved = SizeRule.objects.get(category_key=key)
        return Response(SizeRuleSerializer(rule_json(saved)).data, status=result.status_code)
