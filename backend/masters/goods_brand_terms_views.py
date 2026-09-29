"""Brands > Terms (store operations PRD ST-BRD-1, ST-BRD-6; ticket 23).

Under ``/api/goods-v1/masters/``:

``GET  brand-terms``               every brand in reach: its model per current
                                   season (or unknown), its promotion-services
                                   agreement, the list of brands whose model is
                                   unknown, and every change waiting for the Owner
``GET  brand-terms/<brand id>``    one brand's whole history
``POST brand-terms/versions``      propose terms for a brand and season (Brand Manager)
``POST brand-terms/promotion``     propose the promotion-services agreement (Brand Manager)
``POST brand-terms/decisions``     approve or reject a proposal (Owner), or withdraw
                                   your own

Reading needs ``setup: view``, so a store person never sees terms. A brand
manager reads and proposes for their own brands only. The Owner approves, and
never their own proposal (``SELF_APPROVAL``). Terms apply company-wide, so a
person whose scope is only some stores changes none of them.

Proposing and approving are new work, so they need the ``brand-terms`` switch on
at one or more of the person's stores (ticket 01's rule); reading, rejecting and
withdrawing never do. Every write is one command, so its ``AuditEvent`` records
who, when, and the terms before and after.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, check_revision, parse_meta
from accounts.goods_models import HumanIdentity
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal
from masters.brand_terms import (
    APPROVED,
    PROMOTION,
    REJECTED,
    TERMS,
    WAITING,
    WITHDRAWN,
    Problems,
    Promotion,
    Terms,
    approved_promotions,
    approved_terms,
    as_terms,
    changeable_brands,
    check_applies_from,
    decisions,
    in_force,
    may_approve,
    may_propose,
    may_read,
    newest_approved,
    parse_day,
    parse_days,
    parse_model,
    parse_note,
    parse_percent,
    percent_text,
    readable_brands,
    require_switched_on,
    switched_on,
)
from masters.brand_terms_models import (
    BrandPromotionVersion,
    BrandTermsDecision,
    BrandTermsVersion,
    CommercialModel,
)
from masters.models import Brand, Season

PROPOSE_TERMS_ACTION = "masters.brand_terms.propose"
PROPOSE_PROMOTION_ACTION = "masters.brand_promotion.propose"
DECIDE_ACTION = "masters.brand_terms.decide"

#: The seasons terms are listed for on the summary: the ones still selling.
CURRENT_SEASONS = (Season.Status.OPEN, Season.Status.EOSS)


# -- the wire ------------------------------------------------------------------


class BrandTermsSeasonSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    status = serializers.CharField()
    current = serializers.BooleanField(help_text="Open or end of season: still selling.")


class BrandTermsSeasonModelSerializer(serializers.Serializer[Any]):
    season_id = serializers.IntegerField()
    season_code = serializers.CharField()
    model = serializers.CharField(allow_null=True, help_text="Null: unknown (D9).")
    version = serializers.IntegerField(allow_null=True)
    waiting = serializers.BooleanField(help_text="A change is waiting for the Owner.")


class BrandTermsBrandSummarySerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    is_active = serializers.BooleanField()
    setup_label = serializers.CharField(
        help_text="What Setup > Brands says today. Not the brand's terms: it has a default."
    )
    promotion_agreement = serializers.BooleanField()
    promotion_waiting = serializers.BooleanField()
    seasons = BrandTermsSeasonModelSerializer(many=True)


class BrandTermsUnknownBrandSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    seasons = serializers.ListField(
        child=serializers.CharField(), help_text="Current seasons with no approved model."
    )


class BrandTermsProposalSerializer(serializers.Serializer[Any]):
    kind = serializers.ChoiceField(choices=[TERMS, PROMOTION])
    id = serializers.UUIDField()
    brand_id = serializers.IntegerField()
    brand_code = serializers.CharField()
    brand_name = serializers.CharField()
    season_code = serializers.CharField(allow_null=True)
    version = serializers.IntegerField()
    applies_from = serializers.DateField()
    summary = serializers.CharField()
    note = serializers.CharField()
    proposed_by = serializers.CharField()
    proposed_at = serializers.DateTimeField()
    mine = serializers.BooleanField()


class BrandTermsSummarySerializer(serializers.Serializer[Any]):
    today = serializers.DateField()
    can_propose = serializers.BooleanField()
    can_approve = serializers.BooleanField()
    switched_on = serializers.BooleanField(
        help_text="Brand terms are on at one or more of your stores, so changes can be made."
    )
    models = serializers.ListField(child=serializers.CharField())
    seasons = BrandTermsSeasonSerializer(many=True)
    brands = BrandTermsBrandSummarySerializer(many=True)
    unknown = BrandTermsUnknownBrandSerializer(many=True)
    waiting = BrandTermsProposalSerializer(many=True)


class BrandTermsDecidedSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=[WAITING, APPROVED, REJECTED, WITHDRAWN])
    proposed_by = serializers.CharField()
    proposed_at = serializers.DateTimeField()
    mine = serializers.BooleanField(help_text="You proposed it.")
    decided_by = serializers.CharField(allow_blank=True)
    decided_at = serializers.DateTimeField(allow_null=True)
    decision_note = serializers.CharField(allow_blank=True)
    in_force_today = serializers.BooleanField()


class BrandTermsTermsRowSerializer(BrandTermsDecidedSerializer):
    id = serializers.UUIDField()
    season_id = serializers.IntegerField()
    season_code = serializers.CharField()
    version = serializers.IntegerField()
    applies_from = serializers.DateField()
    model = serializers.CharField()
    margin_percent = serializers.CharField(allow_null=True)
    return_allowance_percent = serializers.CharField(allow_null=True)
    discount_funding_percent = serializers.CharField(allow_null=True)
    payment_days = serializers.IntegerField(allow_null=True)
    note = serializers.CharField()


class BrandTermsPromotionRowSerializer(BrandTermsDecidedSerializer):
    id = serializers.UUIDField()
    version = serializers.IntegerField()
    applies_from = serializers.DateField()
    agreement = serializers.BooleanField()
    note = serializers.CharField()


class BrandTermsNextRevisionSerializer(serializers.Serializer[Any]):
    season_id = serializers.IntegerField()
    expected_revision = serializers.IntegerField()


class BrandTermsDetailSerializer(serializers.Serializer[Any]):
    brand = BrandTermsBrandSummarySerializer()
    today = serializers.DateField()
    can_propose = serializers.BooleanField()
    can_approve = serializers.BooleanField()
    switched_on = serializers.BooleanField()
    models = serializers.ListField(child=serializers.CharField())
    seasons = BrandTermsSeasonSerializer(many=True)
    terms = BrandTermsTermsRowSerializer(many=True)
    promotion = BrandTermsPromotionRowSerializer(many=True)
    terms_revisions = BrandTermsNextRevisionSerializer(many=True)
    promotion_revision = serializers.IntegerField()


class ProposeTermsRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(
        help_text="How many versions this brand and season had when the form was built, plus one."
    )
    brand_id = serializers.IntegerField()
    season_id = serializers.IntegerField()
    applies_from = serializers.DateField()
    model = serializers.ChoiceField(choices=CommercialModel.values)
    margin_percent = serializers.CharField(allow_null=True, required=False)
    return_allowance_percent = serializers.CharField(allow_null=True, required=False)
    discount_funding_percent = serializers.CharField(allow_null=True, required=False)
    payment_days = serializers.IntegerField(allow_null=True, required=False)
    note = serializers.CharField()


class ProposePromotionRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(
        help_text="How many versions this brand had when the form was built, plus one."
    )
    brand_id = serializers.IntegerField()
    applies_from = serializers.DateField()
    agreement = serializers.BooleanField()
    note = serializers.CharField()


class BrandTermsDecideRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    kind = serializers.ChoiceField(choices=[TERMS, PROMOTION])
    id = serializers.UUIDField()
    outcome = serializers.ChoiceField(choices=[APPROVED, REJECTED, WITHDRAWN])
    note = serializers.CharField(required=False, allow_blank=True)


class BrandTermsDecisionResultSerializer(serializers.Serializer[Any]):
    kind = serializers.CharField()
    id = serializers.UUIDField()
    status = serializers.CharField()


# -- building answers -------------------------------------------------------------


def _names(ids: set[Any]) -> dict[Any, str]:
    ids = {i for i in ids if i}
    return dict(HumanIdentity.objects.filter(pk__in=ids).values_list("pk", "display_name"))


def _season_json(season: Season) -> dict[str, Any]:
    return {
        "id": season.pk,
        "code": season.code,
        "name": season.name,
        "status": season.status,
        "current": season.status in CURRENT_SEASONS,
    }


def _seasons() -> list[Season]:
    """Every season terms can be recorded for. The one "unknown historical
    season" row is left out: it is a cohort nobody could agree terms for."""
    return list(Season.objects.filter(historical_unknown=False).order_by("-sort_order", "code"))


def _terms_summary(row: BrandTermsVersion) -> str:
    parts = [CommercialModel(row.model).label]
    if row.margin_percent is not None:
        parts.append(f"margin {percent_text(row.margin_percent)}%")
    if row.return_allowance_percent is not None:
        parts.append(f"returns {percent_text(row.return_allowance_percent)}%")
    if row.discount_funding_percent is not None:
        parts.append(f"brand funds {percent_text(row.discount_funding_percent)}% of discounts")
    if row.payment_days is not None:
        parts.append(f"pay in {row.payment_days} days")
    return ", ".join(parts)


def _promotion_summary(row: BrandPromotionVersion) -> str:
    return f"Promotion-services agreement: {'Yes' if row.agreement else 'No'}"


def _status(decision: BrandTermsDecision | None) -> str:
    return decision.outcome if decision is not None else WAITING


def _terms_snapshot(row: BrandTermsVersion | Terms | None) -> dict[str, Any]:
    """Terms as the audit record holds them."""
    if row is None:
        return {"model": "unknown"}
    return {
        "version": row.version,
        "applies_from": row.applies_from.isoformat(),
        "model": row.model,
        "margin_percent": percent_text(row.margin_percent),
        "return_allowance_percent": percent_text(row.return_allowance_percent),
        "discount_funding_percent": percent_text(row.discount_funding_percent),
        "payment_days": row.payment_days,
    }


def _promotion_snapshot(row: BrandPromotionVersion | Promotion | None) -> dict[str, Any]:
    if row is None:
        return {"agreement": False, "default": True}
    return {
        "version": row.version,
        "applies_from": row.applies_from.isoformat(),
        "agreement": row.agreement,
    }


def _waiting(tenant_id: Any, brands: list[Brand], human_id: Any) -> list[dict[str, Any]]:
    """Every proposal in reach that nobody has decided yet, oldest first."""
    by_id = {brand.pk: brand for brand in brands}
    terms = list(
        BrandTermsVersion.objects.filter(tenant_id=tenant_id, brand_id__in=list(by_id))
        .select_related("season")
        .order_by("created_at")
    )
    promos = list(
        BrandPromotionVersion.objects.filter(
            tenant_id=tenant_id, brand_id__in=list(by_id)
        ).order_by("created_at")
    )
    decided_terms = decisions(tenant_id, TERMS, [row.pk for row in terms])
    decided_promos = decisions(tenant_id, PROMOTION, [row.pk for row in promos])
    open_terms = [row for row in terms if row.pk not in decided_terms]
    open_promos = [row for row in promos if row.pk not in decided_promos]
    names = _names({t.actor_id for t in open_terms} | {p.actor_id for p in open_promos})
    out: list[dict[str, Any]] = []
    for row in open_terms:
        out.append(
            _proposal_json(TERMS, row, by_id[row.brand_id], row.season.code, names, human_id)
        )
    for promo in open_promos:
        out.append(_proposal_json(PROMOTION, promo, by_id[promo.brand_id], None, names, human_id))
    return sorted(out, key=lambda p: p["proposed_at"])


def _proposal_json(
    kind: str,
    row: BrandTermsVersion | BrandPromotionVersion,
    brand: Brand,
    season_code: str | None,
    names: dict[Any, str],
    human_id: Any,
) -> dict[str, Any]:
    summary = _terms_summary(row) if isinstance(row, BrandTermsVersion) else _promotion_summary(row)
    return {
        "kind": kind,
        "id": row.pk,
        "brand_id": brand.pk,
        "brand_code": brand.code,
        "brand_name": brand.name,
        "season_code": season_code,
        "version": row.version,
        "applies_from": row.applies_from,
        "summary": summary,
        "note": row.note,
        "proposed_by": names.get(row.actor_id, ""),
        "proposed_at": row.created_at,
        "mine": row.actor_id is not None and row.actor_id == human_id,
    }


def _brand_summaries(
    tenant_id: Any, brands: list[Brand], current: list[Season], today: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ids = [brand.pk for brand in brands]
    terms = approved_terms(tenant_id, brand_ids=ids, season_ids=[s.pk for s in current])
    promos = approved_promotions(tenant_id, brand_ids=ids)
    rows = list(BrandTermsVersion.objects.filter(tenant_id=tenant_id, brand_id__in=ids))
    promo_rows = list(BrandPromotionVersion.objects.filter(tenant_id=tenant_id, brand_id__in=ids))
    decided = decisions(tenant_id, TERMS, [row.pk for row in rows])
    decided_promos = decisions(tenant_id, PROMOTION, [row.pk for row in promo_rows])
    waiting_terms = {(row.brand_id, row.season_id) for row in rows if row.pk not in decided}
    waiting_promos = {row.brand_id for row in promo_rows if row.pk not in decided_promos}
    ever_approved = {t.brand_id for t in approved_terms(tenant_id, brand_ids=ids)}

    summaries: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for brand in brands:
        by_season = []
        missing = []
        for season in current:
            found = in_force(
                [t for t in terms if t.brand_id == brand.pk and t.season_id == season.pk], today
            )
            by_season.append(
                {
                    "season_id": season.pk,
                    "season_code": season.code,
                    "model": found.model if found else None,
                    "version": found.version if found else None,
                    "waiting": (brand.pk, season.pk) in waiting_terms,
                }
            )
            if found is None:
                missing.append(season.code)
        promotion = in_force([p for p in promos if p.brand_id == brand.pk], today)
        summaries.append(
            {
                "id": brand.pk,
                "code": brand.code,
                "name": brand.name,
                "is_active": brand.is_active,
                "setup_label": brand.commercial_label,
                "promotion_agreement": bool(promotion and promotion.agreement),
                "promotion_waiting": brand.pk in waiting_promos,
                "seasons": by_season,
            }
        )
        # Every active brand with no model for a season still selling, or - where
        # no season is open - with no approved terms at all, is Anand's to fill in.
        if brand.is_active and (missing or (not current and brand.pk not in ever_approved)):
            unknown.append(
                {"id": brand.pk, "code": brand.code, "name": brand.name, "seasons": missing}
            )
    return summaries, unknown


def _access_flags(user: Any) -> dict[str, Any]:
    return {
        "can_propose": may_propose(user),
        "can_approve": may_approve(user),
        "switched_on": switched_on(user),
        "models": list(CommercialModel.values),
    }


def _require_reader(user: Any) -> None:
    if not may_read(user):
        raise Refusal("ACTION_DENIED", "You do not have access to brand terms.")


def _readable_brand(user: Any, brand_id: Any) -> Brand:
    brand: Brand | None = readable_brands(user).filter(pk=brand_id).first()
    if brand is None:
        raise Refusal("NOT_FOUND", "No such brand in your reach.")
    return brand


def _changeable_brand(user: Any, brand_id: Any) -> Brand:
    brand = _readable_brand(user, brand_id)
    if not changeable_brands(user).filter(pk=brand.pk).exists():
        raise Refusal(
            "ACTION_DENIED",
            "Brand terms apply to every store, so only someone whose scope covers every "
            "store (or this brand) can change them.",
        )
    return brand


def _int_id(value: Any, field: str) -> int:
    if isinstance(value, bool):
        value = None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise Refusal("INVALID_REQUEST", f"{field} must be a number.") from None


# -- the views -----------------------------------------------------------------------


class GoodsBrandTermsView(GoodsAPIView):
    @extend_schema(responses=BrandTermsSummarySerializer)
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        _require_reader(request.user)
        brands = list(readable_brands(request.user))
        seasons = _seasons()
        current = [s for s in seasons if s.status in CURRENT_SEASONS]
        today = timezone.localdate()
        summaries, unknown = _brand_summaries(access.tenant_id, brands, current, today)
        body = {
            "today": today,
            **_access_flags(request.user),
            "seasons": [_season_json(s) for s in seasons],
            "brands": summaries,
            "unknown": unknown,
            "waiting": _waiting(access.tenant_id, brands, access.human_id),
        }
        return Response(BrandTermsSummarySerializer(body).data)


class GoodsBrandTermsDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_masters_brand_terms_detail",
        responses=BrandTermsDetailSerializer,
    )
    def get(self, request: Request, brand_id: int) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        _require_reader(request.user)
        brand = _readable_brand(request.user, brand_id)
        today = timezone.localdate()
        seasons = _seasons()
        season_codes = {s.pk: s.code for s in Season.objects.all()}
        rows = list(
            BrandTermsVersion.objects.filter(tenant_id=access.tenant_id, brand=brand).order_by(
                "-created_at"
            )
        )
        promo_rows = list(
            BrandPromotionVersion.objects.filter(tenant_id=access.tenant_id, brand=brand).order_by(
                "-created_at"
            )
        )
        decided = decisions(access.tenant_id, TERMS, [row.pk for row in rows])
        decided_promos = decisions(access.tenant_id, PROMOTION, [row.pk for row in promo_rows])
        names = _names(
            {row.actor_id for row in rows}
            | {row.actor_id for row in promo_rows}
            | {d.actor_id for d in [*decided.values(), *decided_promos.values()]}
        )
        terms = approved_terms(access.tenant_id, brand_ids=[brand.pk])
        in_force_ids = {
            found.id
            for found in (
                in_force([t for t in terms if t.season_id == sid], today)
                for sid in {t.season_id for t in terms}
            )
            if found is not None
        }
        promotion_now = in_force(approved_promotions(access.tenant_id, brand_ids=[brand.pk]), today)
        summaries, _ = _brand_summaries(
            access.tenant_id, [brand], [s for s in seasons if s.status in CURRENT_SEASONS], today
        )

        def decided_json(
            row: Any, decision: BrandTermsDecision | None, live: bool
        ) -> dict[str, Any]:
            return {
                "status": _status(decision),
                "proposed_by": names.get(row.actor_id, ""),
                "proposed_at": row.created_at,
                "mine": row.actor_id is not None and row.actor_id == access.human_id,
                "decided_by": names.get(decision.actor_id, "") if decision else "",
                "decided_at": decision.created_at if decision else None,
                "decision_note": decision.note if decision else "",
                "in_force_today": live,
            }

        counts: dict[int, int] = {}
        for row in rows:
            counts[row.season_id] = counts.get(row.season_id, 0) + 1
        body = {
            "brand": summaries[0],
            "today": today,
            **_access_flags(request.user),
            "seasons": [_season_json(s) for s in seasons],
            "terms": [
                {
                    "id": row.pk,
                    "season_id": row.season_id,
                    "season_code": season_codes.get(row.season_id, ""),
                    "version": row.version,
                    "applies_from": row.applies_from,
                    "model": row.model,
                    "margin_percent": percent_text(row.margin_percent),
                    "return_allowance_percent": percent_text(row.return_allowance_percent),
                    "discount_funding_percent": percent_text(row.discount_funding_percent),
                    "payment_days": row.payment_days,
                    "note": row.note,
                    **decided_json(row, decided.get(row.pk), row.pk in in_force_ids),
                }
                for row in rows
            ],
            "promotion": [
                {
                    "id": row.pk,
                    "version": row.version,
                    "applies_from": row.applies_from,
                    "agreement": row.agreement,
                    "note": row.note,
                    **decided_json(
                        row,
                        decided_promos.get(row.pk),
                        promotion_now is not None and promotion_now.id == row.pk,
                    ),
                }
                for row in promo_rows
            ],
            "terms_revisions": [
                {"season_id": s.pk, "expected_revision": counts.get(s.pk, 0) + 1} for s in seasons
            ],
            "promotion_revision": len(promo_rows) + 1,
        }
        return Response(BrandTermsDetailSerializer(body).data)


def _no_open_proposal(open_rows: list[Any], what: str) -> None:
    if open_rows:
        raise Refusal(
            "STATE_CONFLICT",
            f"A change to {what} is already waiting for the Owner. It must be approved, "
            "rejected or withdrawn before another is proposed.",
        )


class GoodsBrandTermsProposeView(GoodsAPIView):
    @extend_schema(request=ProposeTermsRequestSerializer, responses=BrandTermsTermsRowSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        user = request.user
        if not may_propose(user):
            raise Refusal("ACTION_DENIED", "Only the Brand Manager proposes brand terms.")
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {
                "brand_id",
                "season_id",
                "applies_from",
                "model",
                "margin_percent",
                "return_allowance_percent",
                "discount_funding_percent",
                "payment_days",
                "note",
            },
            required=["brand_id", "season_id"],
        )
        brand = _changeable_brand(user, _int_id(body["brand_id"], "brand_id"))
        require_switched_on(user)
        season = Season.objects.filter(
            pk=_int_id(body["season_id"], "season_id"), historical_unknown=False
        ).first()
        if season is None:
            raise Refusal("INVALID_REQUEST", "Pick a season for these terms.")
        problems = Problems()
        applies = parse_day(body.get("applies_from"), problems)
        model = parse_model(body.get("model"), problems)
        margin = parse_percent(body.get("margin_percent"), "margin_percent", "margin", problems)
        allowance = parse_percent(
            body.get("return_allowance_percent"),
            "return_allowance_percent",
            "return allowance",
            problems,
        )
        funding = parse_percent(
            body.get("discount_funding_percent"),
            "discount_funding_percent",
            "brand's share of discount funding",
            problems,
        )
        days = parse_days(body.get("payment_days"), problems)
        note = parse_note(body.get("note"), problems)
        problems.raise_any()
        assert applies is not None
        new_id = uuid.uuid4()
        what = f"{brand.name}'s terms for {season.code}"

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, ["brand-terms", str(brand.pk), str(season.pk)])
            rows = list(
                BrandTermsVersion.objects.filter(
                    tenant_id=run.tenant_id, brand=brand, season=season
                )
            )
            check_revision(meta.expected_revision, len(rows) + 1)
            decided = decisions(run.tenant_id, TERMS, [row.pk for row in rows])
            _no_open_proposal([row for row in rows if row.pk not in decided], what)
            approved = [
                as_terms(row, decided[row.pk].created_at)
                for row in rows
                if row.pk in decided and decided[row.pk].outcome == APPROVED
            ]
            today = timezone.localdate(run.now)
            newest = newest_approved(approved)
            check_applies_from(applies, today, newest.applies_from if newest else None)
            run.audit_before = {
                "brand": brand.code,
                "season": season.code,
                **_terms_snapshot(in_force(approved, today)),
            }
            row = BrandTermsVersion.objects.create(
                id=new_id,
                tenant_id=run.tenant_id,
                brand=brand,
                season=season,
                version=len(rows) + 1,
                applies_from=applies,
                model=model,
                margin_percent=margin,
                return_allowance_percent=allowance,
                discount_funding_percent=funding,
                payment_days=days,
                note=note,
                actor_id=run.principal.human_id,
            )
            run.audit_after = {
                "brand": brand.code,
                "season": season.code,
                **_terms_snapshot(row),
                "status": WAITING,
                "note": note,
            }
            return CommandResult(
                resource_type="brand_terms_version",
                resource_id=str(row.pk),
                revision=row.version,
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action=PROPOSE_TERMS_ACTION,
            meta=meta,
            business_input={
                "brand_id": brand.pk,
                "season_id": season.pk,
                "applies_from": applies.isoformat(),
                "model": model,
                "margin_percent": percent_text(margin),
                "return_allowance_percent": percent_text(allowance),
                "discount_funding_percent": percent_text(funding),
                "payment_days": days,
                "note": note,
            },
            handler=handler,
            resource_ids=[str(new_id)],
            subject_key=f"brand_terms:{new_id}",
        )
        row = BrandTermsVersion.objects.get(pk=str(result.resource_id))
        names = _names({row.actor_id})
        out = {
            "id": row.pk,
            "season_id": row.season_id,
            "season_code": season.code,
            "version": row.version,
            "applies_from": row.applies_from,
            "model": row.model,
            "margin_percent": percent_text(row.margin_percent),
            "return_allowance_percent": percent_text(row.return_allowance_percent),
            "discount_funding_percent": percent_text(row.discount_funding_percent),
            "payment_days": row.payment_days,
            "note": row.note,
            "status": WAITING,
            "proposed_by": names.get(row.actor_id, ""),
            "proposed_at": row.created_at,
            "mine": True,
            "decided_by": "",
            "decided_at": None,
            "decision_note": "",
            "in_force_today": False,
        }
        return Response(BrandTermsTermsRowSerializer(out).data, status=result.status_code)


class GoodsBrandPromotionProposeView(GoodsAPIView):
    @extend_schema(
        request=ProposePromotionRequestSerializer, responses=BrandTermsPromotionRowSerializer
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        user = request.user
        if not may_propose(user):
            raise Refusal("ACTION_DENIED", "Only the Brand Manager proposes brand terms.")
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"brand_id", "applies_from", "agreement", "note"},
            required=["brand_id"],
        )
        brand = _changeable_brand(user, _int_id(body["brand_id"], "brand_id"))
        require_switched_on(user)
        problems = Problems()
        applies = parse_day(body.get("applies_from"), problems)
        if not isinstance(body.get("agreement"), bool):
            problems.add("agreement", "INVALID", "Say Yes or No to the agreement.")
        note = parse_note(body.get("note"), problems)
        problems.raise_any()
        assert applies is not None
        agreement = bool(body["agreement"])
        new_id = uuid.uuid4()

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, ["brand-promotion", str(brand.pk)])
            rows = list(BrandPromotionVersion.objects.filter(tenant_id=run.tenant_id, brand=brand))
            check_revision(meta.expected_revision, len(rows) + 1)
            decided = decisions(run.tenant_id, PROMOTION, [row.pk for row in rows])
            _no_open_proposal(
                [row for row in rows if row.pk not in decided],
                f"{brand.name}'s promotion-services agreement",
            )
            approved = [
                Promotion(
                    id=row.pk,
                    brand_id=row.brand_id,
                    version=row.version,
                    applies_from=row.applies_from,
                    approved_at=decided[row.pk].created_at,
                    agreement=row.agreement,
                )
                for row in rows
                if row.pk in decided and decided[row.pk].outcome == APPROVED
            ]
            today = timezone.localdate(run.now)
            newest = newest_approved(approved)
            check_applies_from(applies, today, newest.applies_from if newest else None)
            run.audit_before = {
                "brand": brand.code,
                **_promotion_snapshot(in_force(approved, today)),
            }
            row = BrandPromotionVersion.objects.create(
                id=new_id,
                tenant_id=run.tenant_id,
                brand=brand,
                version=len(rows) + 1,
                applies_from=applies,
                agreement=agreement,
                note=note,
                actor_id=run.principal.human_id,
            )
            run.audit_after = {
                "brand": brand.code,
                **_promotion_snapshot(row),
                "status": WAITING,
                "note": note,
            }
            return CommandResult(
                resource_type="brand_promotion_version",
                resource_id=str(row.pk),
                revision=row.version,
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action=PROPOSE_PROMOTION_ACTION,
            meta=meta,
            business_input={
                "brand_id": brand.pk,
                "applies_from": applies.isoformat(),
                "agreement": agreement,
                "note": note,
            },
            handler=handler,
            resource_ids=[str(new_id)],
            subject_key=f"brand_promotion:{new_id}",
        )
        row = BrandPromotionVersion.objects.get(pk=str(result.resource_id))
        out = {
            "id": row.pk,
            "version": row.version,
            "applies_from": row.applies_from,
            "agreement": row.agreement,
            "note": row.note,
            "status": WAITING,
            "proposed_by": _names({row.actor_id}).get(row.actor_id, ""),
            "proposed_at": row.created_at,
            "mine": True,
            "decided_by": "",
            "decided_at": None,
            "decision_note": "",
            "in_force_today": False,
        }
        return Response(BrandTermsPromotionRowSerializer(out).data, status=result.status_code)


@dataclass(frozen=True)
class _Subject:
    """What differs between deciding terms and deciding a promotion agreement."""

    model: Any
    #: The audit subject-key prefix, shared with the proposal's own record.
    key: str
    snapshot: Callable[[Any], dict[str, Any]]
    #: The lock its proposals take, so a decision and a proposal never cross.
    lock: Callable[[Any], list[str]]
    #: The approved versions a new approval must not date before.
    approved: Callable[[Any, Any], list[Any]]


_SUBJECTS: dict[str, _Subject] = {
    TERMS: _Subject(
        model=BrandTermsVersion,
        key="brand_terms",
        snapshot=_terms_snapshot,
        lock=lambda row: ["brand-terms", str(row.brand_id), str(row.season_id)],
        approved=lambda tenant_id, row: list(
            approved_terms(tenant_id, brand_ids=[row.brand_id], season_ids=[row.season_id])
        ),
    ),
    PROMOTION: _Subject(
        model=BrandPromotionVersion,
        key="brand_promotion",
        snapshot=_promotion_snapshot,
        lock=lambda row: ["brand-promotion", str(row.brand_id)],
        approved=lambda tenant_id, row: list(
            approved_promotions(tenant_id, brand_ids=[row.brand_id])
        ),
    ),
}


def _check_decider(user: Any, row: Any, outcome: str, *, mine: bool) -> None:
    """The proposer withdraws; the Owner approves or rejects, never their own."""
    if outcome == WITHDRAWN:
        if not mine:
            raise Refusal("ACTION_DENIED", "Only the person who proposed a change withdraws it.")
        return
    if not may_approve(user):
        raise Refusal("ACTION_DENIED", "Only the Owner approves or rejects brand terms.")
    _changeable_brand(user, row.brand_id)
    if mine:
        raise Refusal(
            "SELF_APPROVAL",
            "You proposed this change, so another person must approve or reject it.",
        )
    if outcome == APPROVED:
        require_switched_on(user)


class GoodsBrandTermsDecideView(GoodsAPIView):
    @extend_schema(
        request=BrandTermsDecideRequestSerializer, responses=BrandTermsDecisionResultSerializer
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, {"kind", "id", "outcome", "note"}, required=["kind", "id", "outcome"]
        )
        kind = str(body["kind"])
        outcome = str(body["outcome"])
        if kind not in (TERMS, PROMOTION) or outcome not in (APPROVED, REJECTED, WITHDRAWN):
            raise Refusal("INVALID_REQUEST", "Say what is decided: approve, reject or withdraw.")
        try:
            subject_id = uuid.UUID(str(body["id"]))
        except ValueError:
            raise Refusal("INVALID_REQUEST", "id must be a proposal's id.") from None
        subject = _SUBJECTS[kind]
        row = subject.model.objects.filter(tenant_id=access.tenant_id, pk=subject_id).first()
        if row is None or not readable_brands(user).filter(pk=row.brand_id).exists():
            raise Refusal("NOT_FOUND", "No such proposal in your reach.")
        problems = Problems()
        note = parse_note(body.get("note"), problems, required=outcome == REJECTED)
        problems.raise_any()
        _check_decider(user, row, outcome, mine=row.actor_id == access.human_id)
        subject_key = f"{subject.key}:{row.pk}"
        snapshot = subject.snapshot

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, subject.lock(row))
            if BrandTermsDecision.objects.filter(
                tenant_id=run.tenant_id, subject_kind=kind, subject_id=row.pk
            ).exists():
                raise Refusal("STATE_CONFLICT", "This change has already been decided.")
            if outcome == APPROVED:
                today = timezone.localdate(run.now)
                newest = newest_approved(subject.approved(run.tenant_id, row))
                check_applies_from(row.applies_from, today, newest.applies_from if newest else None)
            run.audit_before = {**snapshot(row), "status": WAITING}
            BrandTermsDecision.objects.create(
                tenant_id=run.tenant_id,
                subject_kind=kind,
                subject_id=row.pk,
                outcome=outcome,
                note=note,
                actor_id=run.principal.human_id,
            )
            run.audit_after = {**snapshot(row), "status": outcome, "note": note}
            return CommandResult(
                resource_type=f"brand_{kind}_decision",
                resource_id=str(row.pk),
                revision=row.version,
                status_code=200,
            )

        result = self.run_command(
            request,
            access=access,
            action=DECIDE_ACTION,
            meta=meta,
            business_input={"kind": kind, "id": str(row.pk), "outcome": outcome, "note": note},
            handler=handler,
            resource_ids=[str(row.pk)],
            subject_key=subject_key,
        )
        return Response(
            BrandTermsDecisionResultSerializer(
                {"kind": kind, "id": row.pk, "status": outcome}
            ).data,
            status=result.status_code,
        )
