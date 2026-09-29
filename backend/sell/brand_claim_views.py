"""Brands > Claims (store operations PRD ST-BRD-3, ST-BRD-6; ticket 26).

Under ``/api/goods-v1/sell/``:

``GET  brand-claims?month=YYYY-MM``   the month's claims in reach, and what is left
                                      to raise per store and brand (last month
                                      when no month is named)
``GET  brand-claims/<id>``            one claim, with the bill lines it took in
``POST brand-claims/raise``           Accounts: raise an ended month's claims
``POST brand-claims/<id>/accept``     Accounts: the brand agreed the claim
``POST brand-claims/<id>/settle``     Accounts: the brand's commercial credit note

Reading needs ``money: manage`` (Owner and Accounts), at the claim's store; store
roles never see a claim. Writing is Accounts' alone. New work needs the
``brand-discount-claims`` switch on at the claim's store; reading never does.
Every write is one command, so its audit record carries the claim before and after.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_meta
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal, issue
from masters.store_features import is_feature_on
from sell.claim_models import BrandClaim
from sell.models import Sale
from sell.services import brand_claims as bc

STATUSES = [bc.RAISED, bc.ACCEPTED, bc.SETTLED, bc.SETTLED_SHORT]


# -- the wire ------------------------------------------------------------------------


class BrandClaimPartySerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class BrandClaimSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    revision = serializers.IntegerField()
    reference = serializers.CharField(help_text="For the brand to quote; not a tax number.")
    kind = serializers.CharField()
    status = serializers.ChoiceField(choices=STATUSES)
    month = serializers.CharField(help_text="YYYY-MM")
    sequence = serializers.IntegerField(
        help_text="1 for the month's claim; 2, 3 ... for bills that arrived after it."
    )
    brand = BrandClaimPartySerializer()
    store = BrandClaimPartySerializer()
    amount_paise = serializers.CharField(help_text="The brand-funded share claimed.")
    parts = serializers.IntegerField()
    pieces = serializers.IntegerField()
    unknown_paise = serializers.CharField(
        help_text="Brand-funded discount in the month whose share is unknown: not claimed."
    )
    promotion_flag = serializers.BooleanField(
        help_text="The brand's promotion-services agreement was Yes: for Accounts to check."
    )
    raised_by = serializers.CharField(allow_blank=True)
    raised_at = serializers.DateTimeField()
    accepted_on = serializers.DateField(allow_null=True)
    accepted_by = serializers.CharField(allow_blank=True)
    accepted_note = serializers.CharField(allow_blank=True)
    credit_note_number = serializers.CharField(allow_blank=True)
    credit_note_date = serializers.DateField(allow_null=True)
    settled_paise = serializers.CharField(allow_null=True)
    difference_paise = serializers.CharField(allow_null=True)
    difference_reason = serializers.CharField(allow_blank=True)
    settled_by = serializers.CharField(allow_blank=True)
    gst_effect = serializers.BooleanField(
        help_text="Always false: a commercial credit note changes no bill and carries no GST."
    )
    posted = serializers.BooleanField(
        help_text="Always false: posting through the accounting export is not built."
    )
    switched_on = serializers.BooleanField(help_text="The switch is on at the claim's store.")
    allowed_actions = serializers.ListField(child=serializers.CharField())


class BrandClaimLineSerializer(serializers.Serializer[Any]):
    bill_id = serializers.IntegerField()
    bill_number = serializers.CharField(allow_blank=True)
    billed_at = serializers.DateTimeField(allow_null=True)
    line_id = serializers.IntegerField()
    direction = serializers.CharField(help_text="sale, or return for pieces given back.")
    pieces = serializers.IntegerField(help_text="Negative for pieces given back.")
    brand_paise = serializers.CharField(help_text="Negative for pieces given back.")


class BrandClaimDetailSerializer(BrandClaimSerializer):
    lines = BrandClaimLineSerializer(many=True)


class BrandClaimToRaiseSerializer(serializers.Serializer[Any]):
    store = BrandClaimPartySerializer()
    brand_id = serializers.IntegerField(
        allow_null=True, help_text="Null: the bill's brand is not one brand in the list."
    )
    brand_name = serializers.CharField(allow_blank=True)
    amount_paise = serializers.CharField(help_text="Owed and not claimed yet.")
    parts = serializers.IntegerField()
    pieces = serializers.IntegerField()
    unknown_paise = serializers.CharField()
    unknown_parts = serializers.IntegerField()
    raised = serializers.IntegerField(help_text="Claims already raised for this month.")
    promotion_flag = serializers.BooleanField()
    switched_on = serializers.BooleanField()


class BrandClaimListSerializer(serializers.Serializer[Any]):
    month = serializers.CharField()
    month_ended = serializers.BooleanField(help_text="Claims are raised only once it has.")
    can_edit = serializers.BooleanField(help_text="Accounts: raises, accepts and settles.")
    switched_on = serializers.BooleanField(help_text="On at one or more stores in reach.")
    claims = BrandClaimSerializer(many=True)
    to_raise = BrandClaimToRaiseSerializer(many=True)


class BrandClaimRaiseRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    month = serializers.CharField(help_text="YYYY-MM, a month that has ended.")
    store_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    brand_ids = serializers.ListField(child=serializers.IntegerField(), required=False)


class BrandClaimRaisedSerializer(serializers.Serializer[Any]):
    claims = BrandClaimSerializer(many=True)


class BrandClaimAcceptRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_blank=True)


class BrandClaimSettleRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()
    credit_note_number = serializers.CharField()
    credit_note_date = serializers.DateField()
    settled_paise = serializers.CharField(help_text="Whole paise, as text.")
    difference_reason = serializers.CharField(required=False, allow_blank=True)


# -- building answers ------------------------------------------------------------------


def _name(user: Any) -> str:
    if user is None:
        return ""
    return str(getattr(user, "full_name", "") or getattr(user, "username", "") or "")


def _money(value: int | None) -> str | None:
    return None if value is None else str(int(value))


def _party(row: Any) -> dict[str, Any]:
    return {"id": row.pk, "code": row.code, "name": row.name}


def _allowed(claim: BrandClaim, *, editor: bool, switched_on: bool) -> list[str]:
    if not editor or not switched_on:
        return []
    if claim.status == bc.RAISED:
        return ["accept"]
    if claim.status == bc.ACCEPTED:
        return ["settle"]
    return []


def claim_json(claim: BrandClaim, *, editor: bool, switched_on: bool) -> dict[str, Any]:
    return {
        "id": claim.pk,
        "revision": claim.revision,
        "reference": claim.reference,
        "kind": claim.kind,
        "status": claim.status,
        "month": f"{claim.month:%Y-%m}",
        "sequence": claim.sequence,
        "brand": _party(claim.brand),
        "store": _party(claim.store),
        "amount_paise": str(claim.amount_paise),
        "parts": claim.parts,
        "pieces": claim.pieces,
        "unknown_paise": str(claim.unknown_paise),
        "promotion_flag": claim.promotion_flag,
        "raised_by": _name(claim.raised_by),
        "raised_at": claim.created_at,
        "accepted_on": claim.accepted_on,
        "accepted_by": _name(claim.accepted_by),
        "accepted_note": claim.accepted_note,
        "credit_note_number": claim.credit_note_number,
        "credit_note_date": claim.credit_note_date,
        "settled_paise": _money(claim.settled_paise),
        "difference_paise": _money(claim.difference_paise),
        "difference_reason": claim.difference_reason,
        "settled_by": _name(claim.settled_by),
        "gst_effect": False,
        "posted": False,
        "switched_on": switched_on,
        "allowed_actions": _allowed(claim, editor=editor, switched_on=switched_on),
    }


def _lines(claim: BrandClaim) -> list[dict[str, Any]]:
    """One row per bill line: a line with two funded parts shows its pieces once."""
    parts = list(claim.claimed_parts.order_by("sale_id", "line_id", "funding_id"))
    bills = Sale.objects.in_bulk({part.sale_id for part in parts})
    lines: dict[int, dict[str, Any]] = {}
    for part in parts:
        line = lines.get(part.line_id)
        if line is not None:
            line["brand_paise"] += part.brand_paise
            continue
        bill = bills.get(part.sale_id)
        lines[part.line_id] = {
            "bill_id": part.sale_id,
            "bill_number": (bill.doc_number or "") if bill is not None else "",
            "billed_at": bill.billed_at if bill is not None else None,
            "line_id": part.line_id,
            "direction": "return" if part.pieces < 0 else "sale",
            "pieces": part.pieces,
            "brand_paise": part.brand_paise,
        }
    return [{**line, "brand_paise": str(line["brand_paise"])} for line in lines.values()]


def _require_reader(user: Any) -> None:
    if not bc.may_read(user):
        raise Refusal("ACTION_DENIED", "Brand claims are Accounts' and the Owner's work.")


def _require_editor(user: Any) -> None:
    if not bc.may_edit(user):
        raise Refusal(
            "ACTION_DENIED", "Accounts raises brand claims and records how the brand settles them."
        )


def _readable(user: Any, tenant_id: Any, pk: int) -> BrandClaim:
    claim: BrandClaim | None = bc.readable_claims(user, tenant_id).filter(pk=pk).first()
    if claim is None:
        raise Refusal("NOT_FOUND", "That claim was not found.")
    return claim


def _answer(user: Any, tenant_id: Any, pk: int, *, with_lines: bool) -> dict[str, Any]:
    claim = _readable(user, tenant_id, pk)
    body = claim_json(
        claim,
        editor=bc.may_edit(user),
        switched_on=is_feature_on(claim.store_id, bc.FEATURE_KEY),
    )
    if with_lines:
        body["lines"] = _lines(claim)
    return body


def _ids(value: Any, field: str) -> list[int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        raise Refusal(
            "INVALID_REQUEST",
            f"{field} is a list of one or more ids; leave it out for every store or brand.",
            issues=[issue("INVALID", f"{field} is a non-empty list of ids", field=field)],
        )
    return list(value)


# -- the views ---------------------------------------------------------------------------


class GoodsBrandClaimListView(GoodsAPIView):
    @extend_schema(
        parameters=[
            OpenApiParameter("month", str, description="YYYY-MM (last month when left out).")
        ],
        responses=BrandClaimListSerializer,
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, allowed=("month",))
        user = request.user
        _require_reader(user)
        today = timezone.localdate()
        month = (
            bc.parse_month(params["month"])
            if params.get("month")
            else (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        )
        stores = bc.readable_stores(user, access.tenant_id)
        switches = {store.pk: is_feature_on(store, bc.FEATURE_KEY) for store in stores}
        editor = bc.may_edit(user)
        claims = bc.readable_claims(user, access.tenant_id).filter(month=month)
        rows = bc.to_raise(user, access.tenant_id, month, timezone.now())
        body = {
            "month": f"{month:%Y-%m}",
            "month_ended": bc.is_ended(month, today),
            "can_edit": editor,
            "switched_on": any(switches.values()),
            "claims": [
                claim_json(claim, editor=editor, switched_on=switches.get(claim.store_id, False))
                for claim in claims
            ],
            "to_raise": [
                {
                    "store": _party(row.store),
                    "brand_id": row.brand_id,
                    "brand_name": row.brand_name,
                    "amount_paise": str(row.amount_paise),
                    "parts": row.parts,
                    "pieces": row.pieces,
                    "unknown_paise": str(row.unknown_paise),
                    "unknown_parts": row.unknown_parts,
                    "raised": row.raised,
                    "promotion_flag": row.promotion_flag,
                    "switched_on": row.switched_on,
                }
                for row in rows
            ],
        }
        return Response(BrandClaimListSerializer(body).data)


class GoodsBrandClaimDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_sell_brand_claims_detail", responses=BrandClaimDetailSerializer
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        _require_reader(request.user)
        body = _answer(request.user, access.tenant_id, pk, with_lines=True)
        return Response(BrandClaimDetailSerializer(body).data)


class GoodsBrandClaimRaiseView(GoodsAPIView):
    @extend_schema(request=BrandClaimRaiseRequestSerializer, responses=BrandClaimRaisedSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        _require_editor(user)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"month", "store_ids", "brand_ids"}, required=("month",))
        month = bc.parse_month(body["month"])
        store_ids = _ids(body.get("store_ids"), "store_ids")
        brand_ids = _ids(body.get("brand_ids"), "brand_ids")
        raised: list[int] = []

        def handler(run: CommandRun) -> CommandResult:
            claims = bc.raise_claims(
                run,
                user=user,
                tenant_id=access.tenant_id,
                month=month,
                today=timezone.localdate(run.now),
                store_ids=store_ids,
                brand_ids=brand_ids,
            )
            raised.extend(claim.pk for claim in claims)
            return CommandResult(
                resource_type="brand_claim",
                resource_id=",".join(str(pk) for pk in raised),
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action=bc.RAISE_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"brand_claims:{month:%Y-%m}",
            site_id=store_ids[0] if store_ids and len(store_ids) == 1 else None,
        )
        # A replayed command answers with the claims it raised the first time.
        ids = raised or [int(pk) for pk in (result.resource_id or "").split(",") if pk]
        claims = [_answer(user, access.tenant_id, pk, with_lines=False) for pk in ids]
        return Response(BrandClaimRaisedSerializer({"claims": claims}).data, status=201)


class _StepView(GoodsAPIView):
    """One Accounts step on one claim: the shared checks, then one command."""

    action = ""
    fields: frozenset[str] = frozenset()
    required: tuple[str, ...] = ()

    def step(self, run: CommandRun, claim: BrandClaim, body: dict[str, Any], meta: Any) -> None:
        raise NotImplementedError

    def run_step(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        claim = _readable(user, access.tenant_id, pk)
        _require_editor(user)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, self.fields, required=self.required)

        def handler(run: CommandRun) -> CommandResult:
            self.step(run, claim, body, meta)
            return CommandResult(resource_type="brand_claim", resource_id=str(claim.pk))

        self.run_command(
            request,
            access=access,
            action=self.action,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(claim.pk)],
            subject_key=f"brand_claim:{claim.pk}",
            site_id=claim.store_id,
        )
        return Response(
            BrandClaimSerializer(_answer(user, access.tenant_id, pk, with_lines=False)).data
        )


class GoodsBrandClaimAcceptView(_StepView):
    action = bc.ACCEPT_ACTION
    fields = frozenset({"note"})

    def step(self, run: CommandRun, claim: BrandClaim, body: dict[str, Any], meta: Any) -> None:
        bc.accept(
            run,
            claim.pk,
            user=self.request.user,
            note=body.get("note"),
            today=timezone.localdate(run.now),
            expected_revision=meta.expected_revision,
        )

    @extend_schema(request=BrandClaimAcceptRequestSerializer, responses=BrandClaimSerializer)
    def post(self, request: Request, pk: int) -> Response:
        return self.run_step(request, pk)


class GoodsBrandClaimSettleView(_StepView):
    action = bc.SETTLE_ACTION
    fields = frozenset(
        {"credit_note_number", "credit_note_date", "settled_paise", "difference_reason"}
    )
    required = ("credit_note_number", "credit_note_date", "settled_paise")

    def step(self, run: CommandRun, claim: BrandClaim, body: dict[str, Any], meta: Any) -> None:
        bc.settle(
            run,
            claim.pk,
            user=self.request.user,
            credit_note_number=body["credit_note_number"],
            credit_note_date=body["credit_note_date"],
            settled_paise=body["settled_paise"],
            difference_reason=body.get("difference_reason"),
            today=timezone.localdate(run.now),
            expected_revision=meta.expected_revision,
        )

    @extend_schema(request=BrandClaimSettleRequestSerializer, responses=BrandClaimSerializer)
    def post(self, request: Request, pk: int) -> Response:
        return self.run_step(request, pk)
