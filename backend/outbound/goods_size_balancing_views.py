"""Stock > Size Balancing (store operations ticket 34, ST-TRF-1).

``GET  /api/goods-v1/outbound/size-balancing[?site_id=]``  the transfers suggested to
                                                          fill the store's broken sizes
``POST /api/goods-v1/outbound/size-balancing/approve``    approve one: it becomes an
                                                          ordinary transfer request
``POST /api/goods-v1/outbound/size-balancing/reject``     reject one, saying why

Readers hold ``stock: view`` or higher, for the selling stores in their own scope
where the ``size-balancing`` switch is on; they see the suggestions made *to*
that store. A store outside that is not found. No cost is in any answer: each
line carries its MRP only.

Deciding is the receiving store's work, exactly as asking for stock is: it needs
``transfer.allocate`` at the store that would receive (the grant that raises a
transfer request there), and the switch on there. Approving also needs it on at
the sending store. Each decision runs as one command, so its ``AuditEvent``
records who, when, and the suggestion before and after; an approval also names
the transfer request it raised and the broken-size alerts it recorded as acted on.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_int_id, parse_meta
from accounts.permissions import user_can
from accounts.sections import CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal, issue
from masters.models import Store
from masters.scoping import actionable_stores
from masters.store_feature_registry import BROKEN_SIZE, SIZE_BALANCING
from masters.store_features import feature, require_feature, switch_states
from outbound import size_balancing, transfers
from outbound.size_balancing_models import SizeBalanceSuggestion
from stockledger.broken_size_models import BrokenSizeAlert

APPROVE_ACTION = "stock.size_balance.approve"
REJECT_ACTION = "stock.size_balance.reject"
#: Decided or withdrawn suggestions the page lists, newest first.
DECIDED_SHOWN = 50
REASON_LENGTH = 240


class SizeBalanceStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class SizeBalanceLineSerializer(serializers.Serializer[Any]):
    alert_id = serializers.CharField()
    sku_id = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    style_code = serializers.CharField(allow_blank=True)
    colour = serializers.CharField(allow_blank=True)
    category = serializers.CharField(allow_blank=True)
    size = serializers.CharField()
    qty = serializers.IntegerField()
    mrp_paise = serializers.IntegerField(
        allow_null=True, help_text="MRP per piece; null if not recorded."
    )
    destination_sold = serializers.IntegerField(
        help_text="This store's sales of it over the rule's weeks (`settings.weeks`)."
    )


class SizeBalanceSuggestionSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    receiving = SizeBalanceStoreSerializer(help_text="The store that lacks the sizes.")
    sending = SizeBalanceStoreSerializer(help_text="The store that would send them.")
    lines = SizeBalanceLineSerializer(many=True)
    pieces = serializers.IntegerField()
    mrp_paise = serializers.IntegerField()
    mrp_unknown_pieces = serializers.IntegerField()
    state = serializers.ChoiceField(choices=SizeBalanceSuggestion.State.choices)
    made_at = serializers.DateTimeField()
    checked_at = serializers.DateTimeField()
    decided_at = serializers.DateTimeField(allow_null=True)
    decided_by = serializers.CharField(allow_blank=True)
    reason = serializers.CharField(allow_blank=True)
    withdrawn_reason = serializers.CharField(allow_blank=True)
    transfer_request_id = serializers.CharField(allow_null=True)


class SizeBalanceSettingsSerializer(serializers.Serializer[Any]):
    weeks = serializers.IntegerField()
    min_pieces = serializers.IntegerField()
    min_mrp_paise = serializers.IntegerField()


class SizeBalancingSerializer(serializers.Serializer[Any]):
    stores = SizeBalanceStoreSerializer(many=True)
    site_id = serializers.IntegerField(allow_null=True)
    now = serializers.DateTimeField()
    checked_at = serializers.DateTimeField(allow_null=True)
    pending = SizeBalanceSuggestionSerializer(many=True)
    decided = SizeBalanceSuggestionSerializer(many=True)
    can_decide = serializers.BooleanField()
    settings = SizeBalanceSettingsSerializer()


class SizeBalanceApproveRequestSerializer(serializers.Serializer[Any]):
    suggestion_id = serializers.UUIDField()


class SizeBalanceRejectRequestSerializer(serializers.Serializer[Any]):
    suggestion_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=REASON_LENGTH)


def _stores_in_scope(user: Any) -> list[Store]:
    return list(actionable_stores(user, section="stock").filter(store_type=Store.StoreType.STORE))


def balancing_stores(user: Any) -> list[Store]:
    """The selling stores in this person's scope where the switch is on."""
    stores = _stores_in_scope(user)
    states = switch_states(stores, [feature(SIZE_BALANCING)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def _store_json(store: Store) -> dict[str, Any]:
    return {"id": store.pk, "code": store.code, "name": store.name}


def _names(rows: list[SizeBalanceSuggestion]) -> dict[Any, str]:
    return dict(transfers.people_names([r.decided_by_id for r in rows if r.decided_by_id]))


def suggestion_json(row: SizeBalanceSuggestion, names: dict[Any, str]) -> dict[str, Any]:
    return {
        "id": str(row.pk),
        "receiving": _store_json(row.destination_site),
        "sending": _store_json(row.source_site),
        "lines": [
            {
                "alert_id": str(line.get("alert_id", "")),
                "sku_id": str(line.get("sku_id", "")),
                "brand": line.get("brand", ""),
                "style_code": line.get("style_code", ""),
                "colour": line.get("colour", ""),
                "category": line.get("category", ""),
                "size": line.get("size", ""),
                "qty": int(line.get("qty", 0)),
                "mrp_paise": line.get("mrp_paise"),
                "destination_sold": int(line.get("destination_sold", 0)),
            }
            for line in row.lines or []
        ],
        "pieces": row.pieces,
        "mrp_paise": row.mrp_paise,
        "mrp_unknown_pieces": row.mrp_unknown_pieces,
        "state": row.state,
        "made_at": row.made_at,
        "checked_at": row.checked_at,
        "decided_at": row.decided_at,
        "decided_by": names.get(row.decided_by_id, "") if row.decided_by_id else "",
        "reason": row.reason,
        "withdrawn_reason": row.withdrawn_reason,
        "transfer_request_id": str(row.transfer_request_id) if row.transfer_request_id else None,
    }


class GoodsSizeBalancingView(GoodsAPIView):
    """The transfers suggested to fill a store's broken sizes (``stock: view``, in scope)."""

    @extend_schema(
        operation_id="goods_v1_outbound_size_balancing_list",
        parameters=[OpenApiParameter("site_id", int, required=False)],
        responses=SizeBalancingSerializer,
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, allowed=("site_id",))
        if not user_can(request.user, "stock", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Stock.")
        stores = balancing_stores(request.user)
        chosen: Store | None = stores[0] if stores else None
        if params.get("site_id"):
            wanted = parse_int_id(params["site_id"], "site_id")
            chosen = next((store for store in stores if store.pk == wanted), None)
            if chosen is None:
                raise Refusal(
                    "NOT_FOUND",
                    "That store's size-balancing suggestions are not available.",
                    status=404,
                )
        body: dict[str, Any] = {
            "stores": [_store_json(store) for store in stores],
            "site_id": chosen.pk if chosen is not None else None,
            "now": timezone.now(),
            "checked_at": None,
            "pending": [],
            "decided": [],
            "can_decide": False,
            "settings": size_balancing.current_settings().as_json(),
        }
        if chosen is not None:
            rows = SizeBalanceSuggestion.objects.select_related(
                "destination_site", "source_site"
            ).filter(destination_site=chosen)
            pending = list(
                rows.filter(state=SizeBalanceSuggestion.State.PENDING).order_by("made_at", "id")
            )
            decided = list(
                rows.exclude(state=SizeBalanceSuggestion.State.PENDING).order_by(
                    "-decided_at", "-id"
                )[:DECIDED_SHOWN]
            )
            names = _names(decided)
            body.update(
                checked_at=max((row.checked_at for row in pending), default=None),
                pending=[suggestion_json(row, names) for row in pending],
                decided=[suggestion_json(row, names) for row in decided],
                can_decide=access.holds(transfers.ALLOCATE_ACTION)
                and access.can(transfers.ALLOCATE_ACTION, site_id=chosen.pk),
            )
        return Response(SizeBalancingSerializer(body).data)


def _suggestion_in_scope(user: Any, raw_id: Any) -> SizeBalanceSuggestion:
    try:
        suggestion_id = uuid.UUID(str(raw_id))
    except ValueError as exc:
        raise Refusal(
            "INVALID_REQUEST",
            "suggestion_id must be a suggestion's id.",
            issues=[issue("INVALID", "not an id", field="suggestion_id")],
        ) from exc
    stores = sorted(store.pk for store in _stores_in_scope(user))
    row = (
        SizeBalanceSuggestion.objects.select_related("destination_site", "source_site")
        .filter(pk=suggestion_id, destination_site_id__in=stores)
        .first()
    )
    if row is None:
        raise Refusal("NOT_FOUND", "That size-balancing suggestion was not found.", status=404)
    return row


def _reason(body: dict[str, Any]) -> str:
    raw = body.get("reason")
    if not isinstance(raw, str):
        raise Refusal(
            "INVALID_REQUEST",
            "Say why it is rejected.",
            issues=[issue("REQUIRED", "a reason is required", field="reason")],
        )
    reason = " ".join(raw.split())
    if not reason:
        raise Refusal(
            "INVALID_REQUEST",
            "Say why it is rejected.",
            issues=[issue("REQUIRED", "a reason is required", field="reason")],
        )
    if len(reason) > REASON_LENGTH:
        raise Refusal(
            "INVALID_REQUEST",
            f"The reason can be at most {REASON_LENGTH} characters.",
            issues=[issue("INVALID", "too long", field="reason")],
        )
    return reason


def _audit(row: SizeBalanceSuggestion) -> dict[str, Any]:
    return {
        "destination": row.destination_site_id,
        "source": row.source_site_id,
        "state": row.state,
        "pieces": row.pieces,
        "mrp_paise": row.mrp_paise,
        "lines": [
            {k: line.get(k) for k in ("style_code", "colour", "size", "sku_id", "qty", "mrp_paise")}
            for line in row.lines or []
        ],
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "reason": row.reason,
        "transfer_request_id": str(row.transfer_request_id) if row.transfer_request_id else None,
    }


def _locked_pending(run: CommandRun, suggestion_id: uuid.UUID) -> SizeBalanceSuggestion:
    """The suggestion, locked, still waiting; a check running now is waited for first."""
    run.advisory_lock(LockRank.DOCUMENT, [size_balancing.LOCK_KEY])
    row = SizeBalanceSuggestion.objects.select_for_update().get(pk=suggestion_id)
    if row.state != SizeBalanceSuggestion.State.PENDING:
        words: dict[str, str] = {
            SizeBalanceSuggestion.State.APPROVED: "It has already been approved.",
            SizeBalanceSuggestion.State.REJECTED: "It has already been rejected.",
            SizeBalanceSuggestion.State.WITHDRAWN: (
                "The daily check withdrew it: the stock or the sizes changed."
            ),
        }
        raise Refusal("STATE_CONFLICT", words.get(row.state, "It is not waiting."), status=409)
    return row


def _refuse_off(row: SizeBalanceSuggestion, *, approving: bool) -> None:
    """Deciding needs the switch on at the receiving store. Approving also needs it on
    at the sending store, and broken-size alerts on at the receiving store, since the
    approval records the action on them."""
    require_feature(row.destination_site, SIZE_BALANCING)
    if approving:
        require_feature(row.source_site, SIZE_BALANCING)
        require_feature(row.destination_site, BROKEN_SIZE)


def _alert_audit(alert: BrokenSizeAlert) -> dict[str, Any]:
    return {
        "id": str(alert.pk),
        "style": alert.style_code,
        "colour": alert.colour,
        "acted_at": alert.acted_at.isoformat() if alert.acted_at else None,
        "action": alert.action,
        "note": alert.action_note,
    }


def _act_on_alerts(run: CommandRun, row: SizeBalanceSuggestion) -> list[dict[str, Any]]:
    """Record "asked for a transfer" on each open alert it fills that nobody acted on."""
    alert_ids = sorted(
        {str(line.get("alert_id")) for line in row.lines or [] if line.get("alert_id")}
    )
    if not alert_ids:
        return []
    run.advisory_lock(LockRank.DOCUMENT, [f"broken-size:{pk}" for pk in alert_ids])
    acted: list[dict[str, Any]] = []
    for alert in (
        BrokenSizeAlert.objects.select_for_update()
        .filter(
            pk__in=alert_ids,
            site_id=row.destination_site_id,
            closed_at__isnull=True,
            acted_at__isnull=True,
        )
        .order_by("id")
    ):
        before = _alert_audit(alert)
        alert.acted_at = run.now
        alert.acted_by_id = run.principal.human_id
        alert.action = BrokenSizeAlert.Action.TRANSFER
        alert.action_note = "Approved a size-balancing suggestion."
        alert.save(update_fields=["acted_at", "acted_by", "action", "action_note"])
        acted.append({"before": before, "after": _alert_audit(alert)})
    return acted


def _request_body(row: SizeBalanceSuggestion) -> dict[str, Any]:
    return {
        "source_site_id": row.source_site_id,
        "destination_site_id": row.destination_site_id,
        "note": f"Size balancing: fills missing sizes at {row.destination_site.code}.",
        "lines": [
            {
                "line_key": str(uuid.uuid5(row.pk, f"{line.get('alert_id')}:{line.get('sku_id')}")),
                "sku_id": str(line.get("sku_id")),
                "qty": int(line.get("qty", 0)),
                "note": " ".join(
                    filter(None, [line.get("style_code"), line.get("colour"), line.get("size")])
                )[:240]
                or None,
            }
            for line in row.lines or []
        ],
    }


class GoodsSizeBalanceApproveView(GoodsAPIView):
    """Approve one suggestion: it becomes an ordinary transfer request (audited)."""

    @extend_schema(
        operation_id="goods_v1_outbound_size_balancing_approve",
        request=SizeBalanceApproveRequestSerializer,
        responses=SizeBalanceSuggestionSerializer,
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"suggestion_id"}, required=["suggestion_id"])
        found = _suggestion_in_scope(request.user, body["suggestion_id"])
        access.require(transfers.ALLOCATE_ACTION, site_id=found.destination_site_id)

        def handler(run: CommandRun) -> CommandResult:
            # SITE first (the request locks both stores), then the suggestion. The
            # request names itself as the audit subject; this command's subject is
            # the suggestion, refused or not, and the request is in its after-values.
            record = transfers.create_request(run, _request_body(found))
            run.audit_subject_key = f"size_balance_suggestion:{found.pk}"
            run.audit_site_id = found.destination_site_id
            row = _locked_pending(run, found.pk)
            _refuse_off(row, approving=True)
            before = _audit(row)
            acted = _act_on_alerts(run, row)
            row.state = SizeBalanceSuggestion.State.APPROVED
            row.decided_at = run.now
            row.decided_by_id = run.principal.human_id
            row.transfer_request = record
            row.save(update_fields=["state", "decided_at", "decided_by", "transfer_request"])
            run.audit_before = before
            run.audit_after = {
                **_audit(row),
                "transfer_request": {
                    "id": str(record.pk),
                    "source_site_id": record.source_site_id,
                    "destination_site_id": record.destination_site_id,
                    "state": record.state,
                    "lines": record.lines,
                    "note": record.note,
                },
                "alerts_acted": acted,
            }
            return CommandResult(
                resource_type="size_balance_suggestion", resource_id=str(row.pk), revision=1
            )

        result = self.run_command(
            request,
            access=access,
            action=APPROVE_ACTION,
            meta=meta,
            business_input={"suggestion_id": str(found.pk)},
            handler=handler,
            resource_ids=[f"size_balance_suggestion:{found.pk}"],
            subject_key=f"size_balance_suggestion:{found.pk}",
            site_id=found.destination_site_id,
        )
        return _answer(found.pk, result.status_code)


class GoodsSizeBalanceRejectView(GoodsAPIView):
    """Reject one suggestion, saying why (audited). Nothing is requested."""

    @extend_schema(
        operation_id="goods_v1_outbound_size_balancing_reject",
        request=SizeBalanceRejectRequestSerializer,
        responses=SizeBalanceSuggestionSerializer,
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"suggestion_id", "reason"}, required=["suggestion_id"])
        reason = _reason(body)
        found = _suggestion_in_scope(request.user, body["suggestion_id"])
        access.require(transfers.ALLOCATE_ACTION, site_id=found.destination_site_id)

        def handler(run: CommandRun) -> CommandResult:
            row = _locked_pending(run, found.pk)
            _refuse_off(row, approving=False)
            before = _audit(row)
            row.state = SizeBalanceSuggestion.State.REJECTED
            row.decided_at = run.now
            row.decided_by_id = run.principal.human_id
            row.reason = reason
            row.save(update_fields=["state", "decided_at", "decided_by", "reason"])
            run.audit_before = before
            run.audit_after = _audit(row)
            return CommandResult(
                resource_type="size_balance_suggestion", resource_id=str(row.pk), revision=1
            )

        result = self.run_command(
            request,
            access=access,
            action=REJECT_ACTION,
            meta=meta,
            business_input={"suggestion_id": str(found.pk), "reason": reason},
            handler=handler,
            resource_ids=[f"size_balance_suggestion:{found.pk}"],
            subject_key=f"size_balance_suggestion:{found.pk}",
            site_id=found.destination_site_id,
        )
        return _answer(found.pk, result.status_code)


def _answer(pk: uuid.UUID, status: int) -> Response:
    row = SizeBalanceSuggestion.objects.select_related("destination_site", "source_site").get(pk=pk)
    payload = suggestion_json(row, _names([row]))
    return Response(SizeBalanceSuggestionSerializer(payload).data, status=status)
