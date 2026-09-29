"""Booking > Open-to-Buy (store operations PRD ST-BUY-1; ticket 39).

Under ``/api/goods-v1/``:

``GET  open-to-buy``                                   budgets in reach, with their figures
``POST open-to-buy/budgets``                           the Owner sets or changes one budget
``GET  open-to-buy/asks/<id>``                         one ask, as the approvals inbox opens it
``GET  bookings/<id>/open-to-buy``                     what open-to-buy says about a draft
``POST bookings/<id>/open-to-buy/request-approval``    the buyer asks the Owner

The Owner approves or rejects in the approvals inbox (``/api/approvals/<id>/decide``);
``vendors.open_to_buy`` records each decision. The booking's own confirmation
(``bookings/<id>/request-approval``) refuses a draft over open-to-buy until then.

Everything here is at cost, so every read needs a goods grant carrying ``cost``
over the budget's site and brand (B160). Setting a budget is the Owner's (B161).
New work needs the ``open-to-buy`` switch; reading never does. Every write is one
command, so its audit record carries the record before and after.
"""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_meta
from accounts.principal import AccessContext
from approvals.models import Approval, ApprovalStatus
from core.commands import CommandResult, CommandRun
from core.goods_money import MoneyInvalid, paise_from_json
from core.refusals import Refusal
from inbound import goods_input as inp
from masters.models import Store
from vendors import open_to_buy as otb
from vendors.goods_services import BOOKING_ACTION, booking_by_document, booking_head, booking_lines
from vendors.open_to_buy_models import OpenToBuyAsk, OpenToBuyBudget

# -- the wire ------------------------------------------------------------------------

#: Written out rather than an enum: "stage" is already another family's enum name.
STAGE_HELP = (
    "Where asking the Owner stands for the booking's draft as it is now: not_asked, "
    "waiting, approved, rejected, or stale (decided for a draft that has since changed)."
)


class OtbPartySerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class OtbBudgetSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    revision = serializers.IntegerField()
    brand = OtbPartySerializer()
    season = OtbPartySerializer()
    site = OtbPartySerializer(allow_null=True, help_text="Null: the whole company.")
    budget_paise = serializers.CharField()
    open_paise = serializers.CharField(help_text="Pieces still to come on open bookings, at cost.")
    received_paise = serializers.CharField(help_text="Pieces received against bookings, at cost.")
    open_to_buy_paise = serializers.CharField(
        help_text="Budget less open bookings less goods received. Below zero: over."
    )
    cost_missing_pieces = serializers.IntegerField(
        help_text="Pieces open or received on a line with no cost: not counted, never zero."
    )
    switched_on = serializers.BooleanField()
    set_by = serializers.CharField(allow_blank=True)
    updated_at = serializers.DateTimeField()


class OtbListSerializer(serializers.Serializer[Any]):
    can_set = serializers.BooleanField(help_text="The Owner: sets and changes budgets.")
    sites = OtbPartySerializer(
        many=True, help_text="Sites a budget may be set for: in reach, with the switch on."
    )
    company_switched_on = serializers.BooleanField(
        help_text="A company budget may be set: the switch is on at a site you work at."
    )
    budgets = OtbBudgetSerializer(many=True)


class OtbSetRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(
        required=False, help_text="The budget's revision; left out for a new budget."
    )
    brand_id = serializers.IntegerField()
    season_id = serializers.IntegerField()
    site_id = serializers.IntegerField(allow_null=True, help_text="Null: the whole company.")
    budget_paise = serializers.CharField(help_text="Whole paise, as text.")


class OtbOverSerializer(serializers.Serializer[Any]):
    budget_id = serializers.IntegerField()
    site_id = serializers.IntegerField(allow_null=True)
    open_to_buy_paise = serializers.CharField()
    booking_paise = serializers.CharField()
    over_paise = serializers.CharField()
    cost_missing_pieces = serializers.IntegerField()


class OtbAskApprovalSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    status = serializers.CharField()
    decided_by = serializers.CharField(allow_blank=True)
    decided_at = serializers.DateTimeField(allow_null=True)
    reason = serializers.CharField(allow_blank=True)


class OtbAskSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    booking_id = serializers.UUIDField()
    booking_number = serializers.CharField(allow_null=True)
    brand = OtbPartySerializer()
    season = OtbPartySerializer()
    site = OtbPartySerializer(allow_null=True)
    overs = OtbOverSerializer(many=True)
    over_paise = serializers.CharField()
    asked_by = serializers.CharField()
    asked_at = serializers.DateTimeField()
    approval = OtbAskApprovalSerializer(allow_null=True)
    stage = serializers.CharField(help_text=STAGE_HELP)


class OtbBookingCheckSerializer(serializers.Serializer[Any]):
    applies = serializers.BooleanField(
        help_text="Switched on where the booking goes, and a budget covers it."
    )
    state = serializers.CharField(help_text="The booking's state.")
    over = serializers.BooleanField()
    budgets = OtbBudgetSerializer(many=True)
    overs = OtbOverSerializer(many=True)
    stage = serializers.CharField(help_text=STAGE_HELP)
    ask = OtbAskSerializer(allow_null=True)
    allowed_actions = serializers.ListField(child=serializers.CharField())


class OtbAskRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()
    reviewed_hash = serializers.CharField()


# -- building answers ------------------------------------------------------------------


def _name(user: Any) -> str:
    if user is None:
        return ""
    return str(getattr(user, "full_name", "") or getattr(user, "username", "") or "")


def _party(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row.pk, "code": row.code, "name": row.name}


def budget_json(budget: OpenToBuyBudget, pos: otb.Position, *, switched_on: bool) -> dict[str, Any]:
    return {
        "id": budget.pk,
        "revision": budget.revision,
        "brand": _party(budget.brand),
        "season": _party(budget.season),
        "site": _party(budget.site),
        "budget_paise": str(pos.budget_paise),
        "open_paise": str(pos.open_paise),
        "received_paise": str(pos.received_paise),
        "open_to_buy_paise": str(pos.open_to_buy_paise),
        "cost_missing_pieces": pos.cost_missing_pieces,
        "switched_on": switched_on,
        "set_by": _name(budget.set_by),
        "updated_at": budget.updated_at,
    }


def readable_overs(
    access: AccessContext, overs: list[dict[str, Any]], brand_id: int
) -> list[dict[str, Any]]:
    """Only the budgets this person may read: another site's or the company's figures
    stay hidden from a login whose cost grant does not reach them."""
    return [row for row in overs if otb.reaches(access, row.get("site_id"), brand_id)]


def ask_json(
    access: AccessContext, ask: OpenToBuyAsk, approval: Approval | None, stage: str
) -> dict[str, Any]:
    return {
        "id": ask.pk,
        "booking_id": ask.booking.document_id,
        "booking_number": ask.booking.document.official_number,
        "brand": _party(ask.brand),
        "season": _party(ask.season),
        "site": _party(ask.site),
        "overs": readable_overs(access, ask.figures, ask.brand_id),
        "over_paise": str(ask.over_paise),
        "asked_by": _name(ask.asked_by),
        "asked_at": ask.created_at,
        "approval": None
        if approval is None
        else {
            "id": approval.pk,
            "status": approval.status,
            "decided_by": _name(approval.decided_by),
            "decided_at": approval.decided_at,
            "reason": approval.reason or "",
        },
        "stage": stage,
    }


def _require_cost_reader(access: AccessContext) -> None:
    if not otb.sees_cost_somewhere(access):
        raise Refusal(
            "ACTION_DENIED",
            "Open-to-buy is money at cost. Your role does not see cost.",
        )


def _paise(value: Any, field: str) -> int:
    try:
        paise = paise_from_json(value)
    except MoneyInvalid:
        raise inp.bad(f"{field} must be whole paise written as text.", field) from None
    if paise is None:
        raise inp.bad(f"{field} is required.", field)
    return paise


def _id(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise inp.bad(f"{field} must be an ID.", field)
    try:
        number = int(value)
    except ValueError:
        raise inp.bad(f"{field} must be an ID.", field) from None
    if number < 1:
        raise inp.bad(f"{field} must be an ID.", field)
    return number


def _booking_for_cost_reader(access: AccessContext, pk: uuid.UUID) -> Any:
    """A booking this person manages, over whose site and brand they see cost."""
    booking = booking_by_document(pk)
    if booking is None:
        if BOOKING_ACTION not in access.all_actions():
            raise Refusal("ACTION_DENIED", "You do not have permission to manage bookings.")
        raise Refusal("NOT_FOUND", "That booking was not found.")
    access.require(
        BOOKING_ACTION,
        site_id=booking.document.site_id,
        brand_id=booking.brand_id,
        entity_id=booking.document.entity_id,
    )
    if "cost" not in access.field_grants(
        site_id=booking.document.site_id,
        brand_id=booking.brand_id,
        entity_id=booking.document.entity_id,
        actions=[BOOKING_ACTION],
    ):
        raise Refusal("ACTION_DENIED", "Open-to-buy is money at cost. Your role does not see cost.")
    return booking


def booking_check(access: AccessContext, booking: Any) -> dict[str, Any]:
    head = booking_head(booking)
    draft = head.live_version_id is None
    state = "draft" if draft else ("closed" if booking.closed_at else "confirmed")
    latest, approval = otb.newest_ask(booking)
    if not draft:
        return {
            "applies": False,
            "state": state,
            "over": False,
            "budgets": [],
            "overs": [],
            "stage": otb.NOT_ASKED,
            "ask": None if latest is None else ask_json(access, latest, approval, otb.NOT_ASKED),
            "allowed_actions": [],
        }
    header, lines, _root = booking_lines(booking, head)
    found = otb.verdict(booking, header, lines)
    draft_hash = head.draft_revision.content_hash if head.draft_revision else None
    stage = otb.stage_of(latest, approval, draft_hash)
    if otb.approved_ask(booking, draft_hash or "") is not None:
        stage = otb.APPROVED
    readable = [
        (row, pos)
        for row, pos in found.budgets.values()
        if otb.reaches(access, row.site_id, row.brand_id)
    ]
    pending = approval is not None and approval.status == ApprovalStatus.PENDING
    ask_ok = found.overs and not pending and stage in (otb.NOT_ASKED, otb.REJECTED, otb.STALE)
    return {
        "applies": found.applies,
        "state": state,
        "over": bool(found.overs),
        "budgets": [budget_json(row, pos, switched_on=True) for row, pos in readable],
        "overs": readable_overs(
            access, [otb.over_json(over) for over in found.overs], booking.brand_id
        ),
        "stage": stage,
        "ask": None if latest is None else ask_json(access, latest, approval, stage),
        "allowed_actions": ["ask_owner"] if ask_ok else [],
    }


# -- the views ---------------------------------------------------------------------------


class GoodsOpenToBuyListView(GoodsAPIView):
    @extend_schema(responses=OtbListSerializer)
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        _require_cost_reader(access)
        budgets = otb.readable_budgets(access)
        figures = otb.positions(budgets)
        switches = otb.sites_switched_on({b.site_id for b in budgets if b.site_id})
        anywhere = otb.on_anywhere(request.user)
        can_set = otb.may_set(request.user)
        sites: list[dict[str, Any]] = []
        if can_set:
            active = Store.objects.filter(is_active=True).order_by("code")
            on = otb.sites_switched_on(active.values_list("pk", flat=True))
            sites = [
                {"id": s.pk, "code": s.code, "name": s.name}
                for s in active
                if s.pk in on and otb.reaches_site(access, s.pk)
            ]
        body = {
            "can_set": can_set,
            "sites": sites,
            "company_switched_on": anywhere,
            "budgets": [
                budget_json(
                    b,
                    figures[b.pk],
                    switched_on=(b.site_id in switches) if b.site_id else anywhere,
                )
                for b in budgets
            ],
        }
        return Response(OtbListSerializer(body).data)


class GoodsOpenToBuySetView(GoodsAPIView):
    @extend_schema(request=OtbSetRequestSerializer, responses=OtbBudgetSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        _require_cost_reader(access)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"brand_id", "season_id", "site_id", "budget_paise"},
            required=["brand_id", "season_id", "budget_paise"],
        )
        brand_id = _id(body["brand_id"], "brand_id")
        season_id = _id(body["season_id"], "season_id")
        site_id = _id(body.get("site_id"), "site_id", optional=True)
        budget_paise = _paise(body["budget_paise"], "budget_paise")
        assert brand_id is not None and season_id is not None
        found: dict[str, OpenToBuyBudget] = {}

        def handler(run: CommandRun) -> CommandResult:
            found["budget"] = otb.set_budget(
                run,
                user=request.user,
                access=access,
                brand_id=brand_id,
                season_id=season_id,
                site_id=site_id,
                budget_paise=budget_paise,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="open_to_buy_budget", resource_id=str(found["budget"].pk)
            )

        self.run_command(
            request,
            access=access,
            action=otb.SET_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"open_to_buy:{brand_id}:{season_id}:{site_id or 'company'}",
            site_id=site_id,
        )
        budget = (
            OpenToBuyBudget.objects.select_related("brand", "season", "site", "set_by")
            .filter(tenant_id=access.tenant_id, brand_id=brand_id, season_id=season_id)
            .get(site_id=site_id)
        )
        pos = otb.positions([budget])[budget.pk]
        return Response(
            OtbBudgetSerializer(
                budget_json(budget, pos, switched_on=otb.switched_on_for(budget.site_id))
            ).data
        )


class GoodsOpenToBuyAskDetailView(GoodsAPIView):
    @extend_schema(responses=OtbAskSerializer)
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        _require_cost_reader(access)
        ask = (
            OpenToBuyAsk.objects.select_related(
                "booking__document", "brand", "season", "site", "asked_by"
            )
            .filter(tenant_id=access.tenant_id, pk=pk)
            .first()
        )
        if ask is None or not otb.reaches(access, ask.site_id, ask.brand_id):
            raise Refusal("NOT_FOUND", "That request was not found.")
        approval = otb.approvals_of([ask.pk]).get(ask.pk)
        head = booking_head(ask.booking)
        draft_hash = head.draft_revision.content_hash if head.draft_revision else None
        stage = (
            otb.stage_of(ask, approval, draft_hash)
            if head.live_version_id is None
            else otb.NOT_ASKED
        )
        return Response(OtbAskSerializer(ask_json(access, ask, approval, stage)).data)


class GoodsBookingOpenToBuyView(GoodsAPIView):
    @extend_schema(responses=OtbBookingCheckSerializer)
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        booking = _booking_for_cost_reader(access, pk)
        return Response(OtbBookingCheckSerializer(booking_check(access, booking)).data)


class GoodsBookingOpenToBuyAskView(GoodsAPIView):
    @extend_schema(request=OtbAskRequestSerializer, responses=OtbBookingCheckSerializer)
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reviewed_hash"}, required=["reviewed_hash"])
        booking = _booking_for_cost_reader(access, pk)

        def handler(run: CommandRun) -> CommandResult:
            ask = otb.ask_owner(
                run,
                booking,
                user=request.user,
                reviewed_hash=str(body["reviewed_hash"]),
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="open_to_buy_ask", resource_id=str(ask.pk))

        result = self.run_command(
            request,
            access=access,
            action=otb.ASK_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"document:{pk}",
            site_id=booking.document.site_id,
        )
        booking = booking_by_document(pk)
        return Response(
            OtbBookingCheckSerializer(booking_check(access, booking)).data,
            status=result.status_code,
        )
