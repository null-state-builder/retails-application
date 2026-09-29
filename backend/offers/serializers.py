"""Reading and writing a rule, and refusing a shape the engine could not run.

Most of an offer is JSON, which is what makes a new mechanic a data change rather
than a release (D5 Q1). The price of that is here: every dial is checked on the
way in, because the alternative to a validator is a rule that saves cleanly,
syncs to fifty tills, and then silently discounts nothing - or discounts
everything. A rulebook nobody can trust is worse than no rulebook, and the
counter has no way to tell the difference at the till.

The write serializer therefore refuses, among other things: a brand-layer rule
with no brand (it would reach every brand in the shop), an add-on that the engine
cannot apply to a reduced price (it would be a silent no-op), a ladder with no
steps, and a store scope naming a store that does not exist.
"""

from __future__ import annotations

from typing import Any, TypeGuard

from rest_framework import serializers

from masters.models import Brand, Store
from offers import tax_settings as offer_tax_settings
from offers.models import Offer
from offers.resolution import (
    ADD_ON_LAYERS,
    ADD_ON_REWARDS,
    FALLBACK_REWARDS,
    REWARD_AMT_OFF,
    REWARD_FIXED_PRICE,
    REWARD_GIFT,
    REWARD_ITEM_FREE,
    REWARD_PCT_OFF,
    SCOPE_KEYS,
    TRIGGER_GROUP,
    TRIGGER_QTY,
    TRIGGER_SPEND,
    hundredths,
)

#: Everything `covers()` knows how to read. A key outside this set is a typo that
#: would narrow nothing and discount everything, so it is a refusal rather than
#: an ignored extra.
EXCLUDE_KEYS = frozenset(SCOPE_KEYS)
ITEM_SCOPE_KEYS = EXCLUDE_KEYS | {"mrp_min_paise", "mrp_max_paise", "exclude"}

#: draft is where a rule starts; live is where it stops changing.
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    Offer.Status.DRAFT: {Offer.Status.DRAFT, Offer.Status.APPROVED, Offer.Status.ENDED},
    Offer.Status.APPROVED: {
        Offer.Status.APPROVED,
        Offer.Status.DRAFT,
        Offer.Status.LIVE,
        Offer.Status.ENDED,
    },
    # A live rule's only move is to stop. Anything else is end-and-replace.
    Offer.Status.LIVE: {Offer.Status.LIVE, Offer.Status.ENDED},
    Offer.Status.ENDED: {Offer.Status.ENDED},
}


class OfferReadSerializer(serializers.ModelSerializer[Offer]):
    """The full rule, for the authoring screen and the store's read-only view."""

    brand = serializers.CharField(source="brand_name", read_only=True)
    brand_code = serializers.CharField(source="brand.code", read_only=True, default="")
    approved_by_name = serializers.CharField(
        source="approved_by.get_full_name", read_only=True, default=""
    )
    one_liner = serializers.SerializerMethodField()
    stores = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id",
            "name",
            "brand",
            "brand_code",
            "funder",
            "layer",
            "trigger_type",
            "trigger_config",
            "reward_type",
            "reward_config",
            "item_scope",
            "store_scope",
            "stores",
            "starts_on",
            "ends_on",
            "combinable",
            "priority",
            "is_fallback",
            "status",
            "approved_by_name",
            "replaces",
            "one_liner",
            "updated_at",
        ]

    def get_stores(self, offer: Offer) -> list[str]:
        return list((offer.store_scope or {}).get("stores") or [])

    def get_one_liner(self, offer: Offer) -> str:
        return one_liner(offer)


def reward_phrase(offer: Offer) -> str:
    """What the customer gets, in words - "40% off", "flat ₹2,599", "free gift".

    Split out of :func:`one_liner` for the Running Offers card (OPS-10, PRD §11),
    which shows what a rule gives and what it asks for on two separate lines. One
    statement of the wording, so the card and the one-liner can never describe the
    same rule differently.

    A ladder's *type* belongs to the rule and its *amount* to the step, so the
    phrase quotes the top step - the deepest thing this rule can do.
    """
    reward = offer.reward_config or {}
    steps = list((offer.trigger_config or {}).get("slabs") or [])
    top = steps[-1] if steps else {}
    params = {**reward, **{k: v for k, v in top.items() if not k.startswith("min_")}}

    if offer.reward_type == REWARD_PCT_OFF:
        return f"{_trim(params.get('percent', '0'))}% off"
    if offer.reward_type == REWARD_AMT_OFF:
        return f"₹{_rupees(params.get('amount_paise', 0))} off"
    if offer.reward_type == REWARD_FIXED_PRICE:
        return f"flat ₹{_rupees(params.get('price_paise', 0))}"
    if offer.reward_type == REWARD_ITEM_FREE:
        group = offer.trigger_config or {}
        return f"buy {group.get('buy', 0)} get {group.get('get', 0)} free"
    if offer.reward_type == REWARD_GIFT:
        token = params.get("token_price_paise", 0)
        return "free gift" if not token else f"gift at ₹{_rupees(token)}"
    return offer.reward_type  # pragma: no cover - the choices field has no other value


def trigger_phrase(offer: Offer) -> str:
    """What the bill must do first - the ladder's *bottom* step, or nothing at all.

    The bottom step, because that is the point at which the offer starts applying
    and the question a counter asks is "does this bill qualify yet". Empty means
    the rule asks nothing; the caller decides how to say that.
    """
    steps = list((offer.trigger_config or {}).get("slabs") or [])
    if offer.trigger_type == TRIGGER_SPEND and steps:
        return f"spend ₹{_rupees(steps[0].get('min_paise', 0))}"
    if offer.trigger_type == TRIGGER_QTY and steps:
        return f"buy {steps[0].get('min_qty', 0)}+"
    return ""


def one_liner(offer: Offer) -> str:
    """The rule in a phrase, for a card a store manager reads in the morning.

    Deliberately generated rather than typed: a hand-written summary drifts away
    from the rule it summarises, and the summary is what the shop floor believes.
    """
    what = reward_phrase(offer)
    asks = trigger_phrase(offer)
    if asks:
        what = f"{asks} → {what}"
    where = offer.brand_name or "storewide"
    return f"{where}: {what}"


def _trim(value: Any) -> str:
    text = str(value)
    return text[:-3] if text.endswith(".00") else text


def _rupees(paise: Any) -> str:
    return f"{int(paise or 0) // 100:,}"


class OfferWriteSerializer(serializers.ModelSerializer[Offer]):
    """Authoring. Every dial the engine reads is checked before it is stored."""

    brand = serializers.SlugRelatedField(
        slug_field="code", queryset=Brand.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = Offer
        fields = [
            "name",
            "brand",
            "funder",
            "layer",
            "trigger_type",
            "trigger_config",
            "reward_type",
            "reward_config",
            "item_scope",
            "store_scope",
            "starts_on",
            "ends_on",
            "combinable",
            "priority",
            "is_fallback",
            "status",
        ]

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("An offer needs a name a store manager can read.")
        return name

    def validate_item_scope(self, value: Any) -> dict[str, Any]:
        scope = _as_object(value, "item_scope")
        unknown = set(scope) - ITEM_SCOPE_KEYS
        if unknown:
            raise serializers.ValidationError(
                f"{', '.join(sorted(unknown))}: not something the engine narrows by. "
                f"It would have discounted everything."
            )
        exclude = _as_object(scope.get("exclude") or {}, "item_scope.exclude")
        unknown = set(exclude) - EXCLUDE_KEYS
        if unknown:
            raise serializers.ValidationError(
                f"exclude.{', '.join(sorted(unknown))}: not something the engine excludes by."
            )
        for key in ("mrp_min_paise", "mrp_max_paise"):
            if key in scope and not _is_int(scope[key]):
                raise serializers.ValidationError(f"{key} must be whole paise.")
        floor, ceiling = scope.get("mrp_min_paise"), scope.get("mrp_max_paise")
        if floor is not None and ceiling is not None and int(floor) > int(ceiling):
            raise serializers.ValidationError("The MRP band starts above where it ends.")
        return scope

    def validate_store_scope(self, value: Any) -> dict[str, Any]:
        """Resolve "all" to a list of stores, now (D5 Q4).

        A wildcard evaluated at billing time would enrol a shop that opened after
        the offer was costed. The list is the decision; `kind` is only a record
        of how the author expressed it.
        """
        scope = _as_object(value, "store_scope")
        kind = str(scope.get("kind") or "all")
        if kind not in ("all", "specific"):
            raise serializers.ValidationError("store_scope.kind is 'all' or 'specific'.")
        if kind == "all":
            codes = sorted(Store.objects.filter(is_active=True).values_list("code", flat=True))
            if not codes:
                raise serializers.ValidationError("There are no active stores to run this in.")
            return {"kind": "all", "stores": codes}

        raw = scope.get("stores") or []
        if not isinstance(raw, list) or not raw:
            raise serializers.ValidationError("Name at least one store, or choose 'all'.")
        codes = sorted({str(code).strip().upper() for code in raw if str(code).strip()})
        known = set(Store.objects.filter(code__in=codes).values_list("code", flat=True))
        missing = sorted(set(codes) - known)
        if missing:
            raise serializers.ValidationError(f"No store with code {', '.join(missing)}.")
        return {"kind": "specific", "stores": codes}

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        current = self.instance
        layer = attrs.get("layer") or (current.layer if current else Offer.Layer.BRAND)
        brand = attrs.get("brand", current.brand if current else None)
        trigger = attrs.get("trigger_type") or (
            current.trigger_type if current else Offer.Trigger.NONE
        )
        reward = attrs.get("reward_type") or (current.reward_type if current else "")
        trigger_config = _as_object(
            attrs.get("trigger_config", current.trigger_config if current else {}),
            "trigger_config",
        )
        reward_config = _as_object(
            attrs.get("reward_config", current.reward_config if current else {}), "reward_config"
        )
        attrs["trigger_config"] = trigger_config
        attrs["reward_config"] = reward_config

        _check_layer(layer, brand, reward)
        _check_trigger(trigger, reward, trigger_config)
        _check_reward(reward, trigger_config, reward_config)
        wrong = offer_tax_settings.check(layer, reward, reward_config)  # ticket 11
        if wrong:
            raise serializers.ValidationError({"reward_config": wrong})
        _check_dates(attrs, current)
        _check_status(attrs, current)
        return attrs


def _check_layer(layer: str, brand: Brand | None, reward: str) -> None:
    if layer == Offer.Layer.BRAND and brand is None:
        raise serializers.ValidationError(
            {"brand": "A brand-layer rule needs its brand, or it reaches every brand in the shop."}
        )
    if layer in ADD_ON_LAYERS and brand is not None:
        raise serializers.ValidationError(
            {"brand": f"A {layer} rule sits above the brands; leave its brand empty."}
        )
    if layer in ADD_ON_LAYERS and reward not in ADD_ON_REWARDS:
        raise serializers.ValidationError(
            {
                "reward_type": (
                    f"A {layer} add-on applies to a price a brand offer has already reduced, "
                    "so it can only take a further percentage or a further amount off. "
                    "As written it would do nothing at all."
                )
            }
        )


def _check_trigger(trigger: str, reward: str, config: dict[str, Any]) -> None:
    if trigger in (TRIGGER_SPEND, TRIGGER_QTY):
        key = "min_paise" if trigger == TRIGGER_SPEND else "min_qty"
        slabs = config.get("slabs")
        if not isinstance(slabs, list) or not slabs:
            raise serializers.ValidationError(
                {"trigger_config": f"A {trigger} ladder needs at least one step."}
            )
        seen: set[int] = set()
        for step in slabs:
            if not isinstance(step, dict) or not _is_int(step.get(key)) or int(step[key]) < 0:
                raise serializers.ValidationError(
                    {"trigger_config": f"Every step needs a whole '{key}'."}
                )
            if int(step[key]) in seen:
                raise serializers.ValidationError(
                    {"trigger_config": f"Two steps both start at {key} {step[key]}."}
                )
            seen.add(int(step[key]))
    if trigger == TRIGGER_GROUP:
        buy, get = config.get("buy"), config.get("get")
        if not _is_int(buy) or not _is_int(get) or buy < 1 or get < 1:
            raise serializers.ValidationError(
                {"trigger_config": "A buy-X-get-Y needs a whole 'buy' and 'get', both at least 1."}
            )
    if reward == REWARD_ITEM_FREE and trigger != TRIGGER_GROUP:
        raise serializers.ValidationError(
            {"reward_type": "Giving a piece free is a buy-X-get-Y; set the trigger to 'group'."}
        )


def _refuse(message: str) -> None:
    raise serializers.ValidationError({"reward_config": message})


def _check_percent(params: dict[str, Any]) -> None:
    try:
        percent = hundredths(params.get("percent", ""))
    except ValueError:
        raise serializers.ValidationError(
            {"reward_config": "'percent' is a two-decimal string, e.g. '30.00'."}
        ) from None
    if not 0 < percent <= 10_000:
        _refuse("'percent' must be above nought and at most 100.")


#: ₹10 crore, in paise. Nothing a garment retailer discounts comes near it, and
#: above it the two engines stop agreeing: the largest-remainder split multiplies
#: the amount by a line's value, and JavaScript's integers run out of exactness at
#: 2^53 where Python's do not. A ceiling nobody will meet beats a divergence
#: nobody would see.
MAX_MONEY_DIAL_PAISE = 100_000_000_000


def _check_money(params: dict[str, Any], key: str, *, above_nought: bool) -> None:
    value = params.get(key)
    if not _is_int(value) or value < (1 if above_nought else 0):
        _refuse(f"'{key}' must be whole paise{' above nought' if above_nought else ''}.")
    elif value > MAX_MONEY_DIAL_PAISE:
        _refuse(f"'{key}' is larger than any offer this business runs.")


def _check_amount(params: dict[str, Any]) -> None:
    _check_money(params, "amount_paise", above_nought=True)


def _check_price(params: dict[str, Any]) -> None:
    _check_money(params, "price_paise", above_nought=False)


def _check_gift(params: dict[str, Any]) -> None:
    if not str(params.get("gift_barcode") or "").strip():
        _refuse("A gift needs the barcode of the piece being given.")
    params = {**params, "token_price_paise": params.get("token_price_paise", 0)}
    _check_money(params, "token_price_paise", above_nought=False)
    fallback = params.get("fallback")
    if fallback is None:
        return
    if not isinstance(fallback, dict) or fallback.get("reward_type") not in FALLBACK_REWARDS:
        _refuse(
            "The out-of-stock fallback is a percentage, an amount or a fixed price - "
            "never another gift."
        )
    _check_reward(str(fallback["reward_type"]), {}, dict(fallback.get("reward_config") or {}))


#: One checker per reward. A reward with no entry here needs no dials.
_REWARD_CHECKS = {
    REWARD_PCT_OFF: _check_percent,
    REWARD_AMT_OFF: _check_amount,
    REWARD_FIXED_PRICE: _check_price,
    REWARD_GIFT: _check_gift,
}


def _check_reward(
    reward: str, trigger_config: dict[str, Any], reward_config: dict[str, Any]
) -> None:
    """Every step of a ladder must carry whatever the reward needs to be paid.

    A rule whose top step forgot its amount is a rule that discounts nought for
    the biggest spenders, and nobody notices until the month-end pack.
    """
    check = _REWARD_CHECKS.get(reward)
    if check is None:
        return
    steps = [step for step in (trigger_config.get("slabs") or []) if isinstance(step, dict)]
    for params in [{**reward_config, **step} for step in steps] or [reward_config]:
        check(params)


def _check_dates(attrs: dict[str, Any], current: Offer | None) -> None:
    starts = attrs.get("starts_on") or (current.starts_on if current else None)
    ends = attrs.get("ends_on", current.ends_on if current else None)
    if starts is None:
        raise serializers.ValidationError({"starts_on": "An offer needs a start date."})
    if ends is not None and ends < starts:
        raise serializers.ValidationError({"ends_on": "An offer cannot end before it starts."})


def _check_status(attrs: dict[str, Any], current: Offer | None) -> None:
    wanted = attrs.get("status")
    if wanted is None:
        return
    was = current.status if current else Offer.Status.DRAFT
    if current is None and wanted != Offer.Status.DRAFT:
        raise serializers.ValidationError(
            {"status": "A new offer starts as a draft; approving it is a second, named step."}
        )
    if wanted not in LEGAL_TRANSITIONS[was]:
        raise serializers.ValidationError({"status": f"An offer cannot go from {was} to {wanted}."})
    if wanted == Offer.Status.LIVE and current is not None and current.approved_by_id is None:
        raise serializers.ValidationError(
            {"status": "An offer goes live only after somebody has approved it (D5 Q9)."}
        )


def _as_object(value: Any, field: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError(f"{field} must be an object.")
    return dict(value)


def _is_int(value: Any) -> TypeGuard[int]:
    """Whole paise, or a whole count. `True` is an `int` in Python and is not one here."""
    return isinstance(value, int) and not isinstance(value, bool)
