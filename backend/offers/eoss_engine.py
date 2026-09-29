"""The EOSS markdown decision - sell-through vs target, ladder, margin floor.

Deliberately rules, not ML (docs/05-offers/markdown-ai-research): a small
retailer's first win is timely, explainable markdowns, not a forecasting
model. This module only *proposes* - `EossRecommendation.status` starts and
stays `pending` until a named person decides, exactly the human-in-the-loop
gate the research calls the industry norm.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Min, Sum
from django.utils import timezone

from masters.models import Brand, Cohort, Season, Sku
from offers.models import EossLadderStep, EossRecommendation, SellThroughTarget
from stockledger.models import StockLedgerEntry, StockOnHand

#: Below this many pieces ever seen (sold + on hand), a sell-through percentage
#: is noise - one piece sold out of two is "50%" and means nothing. The ladder
#: falls back to age in that case (docs/05-offers: "age as a fallback for low
#: sales data").
MIN_UNITS_FOR_SELL_THROUGH = 5


def _target_pct(brand: Brand | None, week: int) -> Decimal:
    qs = SellThroughTarget.objects.filter(week_number__lte=week)
    row = qs.filter(brand=brand).order_by("-week_number").first() if brand else None
    if row is None:
        row = qs.filter(brand__isnull=True).order_by("-week_number").first()
    return row.target_pct if row else Decimal("0")


def _ladder_discount(brand: Brand | None, *, gap: Decimal, age_weeks: int) -> Decimal:
    steps = EossLadderStep.objects.filter(brand=brand)
    if brand is not None and not steps.exists():
        steps = EossLadderStep.objects.filter(brand__isnull=True)
    best = Decimal("0")
    for step in steps:
        if (
            step.trigger_type == EossLadderStep.Trigger.SELL_THROUGH_GAP
            and gap >= step.trigger_value
        ):
            best = max(best, step.discount_pct)
        elif (
            step.trigger_type == EossLadderStep.Trigger.AGE_WEEKS
            and Decimal(age_weeks) >= step.trigger_value
        ):
            best = max(best, step.discount_pct)
    return best


def _margin_floor_pct(design: str, color: str, brand_name: str, season_code: str) -> Decimal | None:
    """The deepest % off before the price falls below unit cost, from the
    cheapest cohort cost seen for this style (the safest floor across sizes)."""
    barcodes = list(
        Sku.objects.filter(design=design, color=color, brand=brand_name).values_list(
            "barcode", flat=True
        )
    )
    if not barcodes:
        return None
    cohorts = Cohort.objects.filter(barcode__in=barcodes, season=season_code).exclude(
        mrp_paise__isnull=True
    )
    floors = [
        (Decimal(1) - Decimal(c.unit_cost_paise) / Decimal(c.mrp_paise)) * 100
        for c in cohorts
        if c.mrp_paise
    ]
    return min(floors).quantize(Decimal("0.01")) if floors else None


def _reason(
    *,
    sell_through: Decimal,
    week: int,
    target: Decimal,
    gap: Decimal,
    discount: Decimal,
    broken_size: bool,
    floor: Decimal | None,
    capped: bool,
) -> str:
    bits = [
        f"{sell_through:.0f}% sold by week {week} (target {target:.0f}%) — "
        f"{'behind by' if gap > 0 else 'ahead by'} {abs(gap):.0f} pts."
    ]
    if broken_size:
        bits.append("Size run is broken (some sizes gone, others still full).")
    if discount <= 0:
        bits.append("On track — no markdown recommended.")
    else:
        bits.append(f"Recommend {discount:.0f}% off.")
    if capped and floor is not None:
        bits.append(f"Capped at the {floor:.0f}% margin floor (unit cost).")
    return " ".join(bits)


@transaction.atomic
def generate_recommendations(season_code: str) -> int:
    """(Re)compute every style-colour's recommendation for `season_code`.

    Returns the number of rows written. A row already decided (`approved` /
    `rejected`) is skipped entirely — regenerating a plan never overturns a
    human's call on a style already actioned.
    """
    season = Season.objects.get(code=season_code)
    today = timezone.localdate()

    groups = (
        StockOnHand.objects.filter(season=season_code, net_qty__gt=0)
        .values("brand", "design", "color", "item")
        .annotate(on_hand=Sum("net_qty"))
    )

    written = 0
    for group in groups:
        brand_name, design, color, item = (
            group["brand"],
            group["design"],
            group["color"],
            group["item"],
        )
        brand = Brand.objects.filter(name=brand_name).first() if brand_name else None
        on_hand = int(group["on_hand"] or 0)

        ledger = StockLedgerEntry.objects.filter(
            season=season_code, brand=brand_name, design=design, color=color
        )
        sold_qty = -int(
            ledger.filter(kind=StockLedgerEntry.Kind.SALE_OUT).aggregate(t=Sum("qty"))["t"] or 0
        )
        first_in = ledger.filter(kind=StockLedgerEntry.Kind.PT_INWARD).aggregate(
            t=Min("created_at")
        )["t"]
        weeks_in_stock = max(1, (today - first_in.date()).days // 7) if first_in else 1

        total_units = sold_qty + on_hand
        sell_through = (
            (Decimal(sold_qty) / Decimal(total_units) * 100) if total_units else Decimal("0")
        )
        target = _target_pct(brand, weeks_in_stock)
        gap = (target - sell_through).quantize(Decimal("0.01"))

        by_size = (
            StockOnHand.objects.filter(
                season=season_code, brand=brand_name, design=design, color=color
            )
            .values("size")
            .annotate(t=Sum("net_qty"))
        )
        sizes = list(by_size)
        broken_size = len(sizes) > 1 and 0 < sum(1 for s in sizes if (s["t"] or 0) > 0) < len(sizes)

        # Low-volume styles: sell-through is noise, so trigger on age instead.
        use_gap = total_units >= MIN_UNITS_FOR_SELL_THROUGH
        ladder_pct = _ladder_discount(
            brand, gap=gap if use_gap else Decimal("0"), age_weeks=weeks_in_stock
        )
        floor = _margin_floor_pct(design, color, brand_name, season_code)
        capped = floor is not None and ladder_pct > floor
        recommended = (
            max(Decimal("0"), min(ladder_pct, floor))
            if capped and floor is not None
            else ladder_pct
        )

        existing = EossRecommendation.objects.filter(
            season=season, brand=brand, design=design, color=color
        ).first()
        if existing is not None and existing.status != EossRecommendation.Status.PENDING:
            continue  # a human already decided this style — never overwritten

        EossRecommendation.objects.update_or_create(
            season=season,
            brand=brand,
            design=design,
            color=color,
            defaults=dict(
                item=item,
                weeks_in_stock=weeks_in_stock,
                on_hand_qty=on_hand,
                sold_qty=sold_qty,
                sell_through_pct=sell_through.quantize(Decimal("0.01")),
                target_pct=target,
                gap_pct=gap,
                broken_size_run=broken_size,
                margin_floor_pct=floor,
                recommended_discount_pct=recommended,
                reason=_reason(
                    sell_through=sell_through,
                    week=weeks_in_stock,
                    target=target,
                    gap=gap,
                    discount=recommended,
                    broken_size=broken_size,
                    floor=floor,
                    capped=capped,
                ),
                status=EossRecommendation.Status.PENDING,
            ),
        )
        written += 1
    return written
