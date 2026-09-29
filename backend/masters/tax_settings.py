"""Versioned tax settings: which version a bill is taxed under (ticket 03, §6).

The questions every caller asks, answered in one place:

* ``store_uses_settings(store)`` - is the tax-settings switch on here? Off (the
  default, and always at a real store while the CA sign-off gate is open) means
  version 1, the slab table, exactly as before this ticket (baseline B5).
* ``in_force(versions, day, at)`` - which saved version applies to a bill made
  on ``day`` (India) at the moment ``at``. The newest version whose applies-from
  date has come *and* that had been saved by then; ``None`` means version 1. A
  version saved in the afternoon therefore never claims a bill rung up that
  morning, even when it applies from today.
* ``SavedTaxVersion.rule_for(hsn)`` - the rule a line's HSN falls under: the
  longest matching prefix, else the rule with a blank prefix (every HSN), else
  none. A line with no HSN - blank, or not an HSN at all (``masters.hsn``) -
  matches nothing, so it is flagged rather than guessed.

Two kinds of rule (ticket 12, §6 ST-CMP-3):

* ``price_line`` - apparel: the lower rate at or under a price line, the higher
  rate over it (ST-CMP-1).
* ``flat_rate`` - the rate schedule: one rate for an HSN whatever the price,
  for accessories (belts, bags, wallets, perfumes and the like). The item's HSN
  comes from its PT; the rate is whatever Admin enters here. No accessory, HSN
  or rate is written in code.

No tax figure lives in this module. The numbers are the saved rows (or, for
version 1, ``GstSlab`` rows).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from core.fiscal import financial_year_months
from core.refusals import Refusal, issue
from masters.hsn import is_hsn
from masters.models import GstSlab, Store
from masters.store_features import is_feature_on
from masters.tax_setting_models import TaxSettingVersion

#: The store feature (ticket 01's registry) that switches the settings on.
FEATURE_KEY = "tax-settings"
#: The slab table every bill used before this ticket (baseline B5). No row.
LEGACY_VERSION = 1
#: A rate that depends on the price: the lower rate at or under a price line.
PRICE_LINE = "price_line"
#: Ticket 12: the rate schedule - one rate for an HSN, whatever the price.
FLAT_RATE = "flat_rate"
RULE_KINDS = (PRICE_LINE, FLAT_RATE)

#: Ticket 11: the invoice total is rounded to the nearest rupee, with a
#: round-off line (§6 Rounding; baseline, CA to confirm). A version's
#: ``options["round_total_paise"]``; 1 means no rounding. Tax itself is always
#: to the paisa on each line.
ROUND_TOTAL_KEY = "round_total_paise"
DEFAULT_ROUND_TOTAL_PAISE = 100
ROUND_TOTAL_CHOICES = (1, DEFAULT_ROUND_TOTAL_PAISE)

#: Ticket 13 (§6 ST-CMP-2): may a store take back a bill another GSTIN issued?
#: Off (baseline, CA to confirm). ``options["cross_gstin_returns"]``.
CROSS_GSTIN_KEY = "cross_gstin_returns"
#: Ticket 13 (B10): the date Accounts recorded each annual return as filed,
#: ``options["annual_return_filed"] = {gstin: {"26-27": "YYYY-MM-DD"}}``. A
#: credit note for a bill of that year and GSTIN reduces tax only up to 30
#: November after the year, or this date when it is earlier.
FILED_KEY = "annual_return_filed"
#: Ticket 14 (ST-CMP-7): is a gift with purchase gift stock, whose input tax
#: credit is reversed, rather than part of the sale price? Yes (baseline, CA to
#: confirm). ``options["gift_with_purchase_is_gift"]``. A piece given free with
#: no gift offer is gift stock either way.
GIFT_KEY = "gift_with_purchase_is_gift"
DEFAULT_GIFT = True
OPTION_KEYS = frozenset({ROUND_TOTAL_KEY, CROSS_GSTIN_KEY, FILED_KEY, GIFT_KEY})

_GSTIN = re.compile(r"^[0-9]{2}[0-9A-Z]{13}$")

_HSN_PREFIX = re.compile(r"^[0-9]{0,8}$")
_MAX_RATE = Decimal("100")


@dataclass(frozen=True)
class TaxRule:
    """One HSN's rate rule: rate_below when P ÷ (1 + rate_below) ≤ threshold.

    A ``flat_rate`` rule is held as the same shape with no price line (0) and
    one rate both sides, so a till built before ticket 12 that reads it as a
    price line still charges the scheduled rate (P × 100 ≤ 0 is never true, and
    the rate over the line is the rate).
    """

    hsn_prefix: str
    name: str
    threshold_paise: int
    rate_below: Decimal
    rate_above: Decimal
    kind: str = PRICE_LINE

    @property
    def is_flat(self) -> bool:
        return self.kind == FLAT_RATE

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "hsn_prefix": self.hsn_prefix,
            "name": self.name,
            "threshold_paise": self.threshold_paise,
            "rate_below": rate_text(self.rate_below),
            "rate_above": rate_text(self.rate_above),
        }
        if self.is_flat:
            out["rate"] = rate_text(self.rate_above)
        return out


def flat_rate(hsn_prefix: str, name: str, rate: Decimal) -> TaxRule:
    """A rate-schedule rule: ``rate`` for this HSN, whatever the price."""
    return TaxRule(
        hsn_prefix=hsn_prefix,
        name=name,
        threshold_paise=0,
        rate_below=rate,
        rate_above=rate,
        kind=FLAT_RATE,
    )


@dataclass(frozen=True)
class SavedTaxVersion:
    """A saved version (2 and up), as plain data the till and the server share."""

    version: int
    applies_from: date
    saved_at: datetime
    rules: tuple[TaxRule, ...]
    unmatched_rate: Decimal
    #: Ticket 11: what the invoice total is rounded to, in paise.
    round_total_paise: int = DEFAULT_ROUND_TOTAL_PAISE
    #: Ticket 13: returns across GSTINs (off, the baseline).
    cross_gstin_returns: bool = False
    #: Ticket 13: ``{gstin: {"26-27": "YYYY-MM-DD"}}``, as Accounts recorded them.
    annual_return_filed: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Ticket 14: a gift with purchase is gift stock (the baseline).
    gift_with_purchase_is_gift: bool = DEFAULT_GIFT

    def rule_for(self, hsn: str) -> TaxRule | None:
        code = (hsn or "").strip()
        if not is_hsn(code):
            # Blank, or not an HSN at all ("NA", "6205.20"): no rule, never a
            # guess - the "no rule" rate and a flag (ticket 12, B26).
            return None
        prefixed = [r for r in self.rules if r.hsn_prefix and code.startswith(r.hsn_prefix)]
        if prefixed:
            return max(prefixed, key=lambda r: len(r.hsn_prefix))
        return next((r for r in self.rules if not r.hsn_prefix), None)

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "applies_from": self.applies_from.isoformat(),
            # The till leaves out a version saved after the moment it bills, the
            # same comparison the server makes against the bill's own clock.
            "saved_at": self.saved_at.isoformat(),
            "rules": [rule.as_json() for rule in self.rules],
            "unmatched_rate": rate_text(self.unmatched_rate),
            "options": self.options_json(),
        }

    def options_json(self) -> dict[str, Any]:
        """The version's non-rate choices, every key present (tickets 11, 13)."""
        return {
            ROUND_TOTAL_KEY: self.round_total_paise,
            CROSS_GSTIN_KEY: self.cross_gstin_returns,
            FILED_KEY: {gstin: dict(years) for gstin, years in self.annual_return_filed.items()},
            GIFT_KEY: self.gift_with_purchase_is_gift,
        }


def rate_text(rate: Decimal) -> str:
    """A rate as the two-decimal string the till and the bill quote."""
    return f"{Decimal(rate).quantize(Decimal('0.01'))}"


def rule_from_json(raw: dict[str, Any]) -> TaxRule:
    """A stored (or golden-case) rule, either kind."""
    if raw.get("kind") == FLAT_RATE:
        return flat_rate(
            str(raw.get("hsn_prefix", "")), str(raw.get("name", "")), Decimal(str(raw["rate"]))
        )
    return TaxRule(
        hsn_prefix=str(raw.get("hsn_prefix", "")),
        name=str(raw.get("name", "")),
        threshold_paise=int(raw["threshold_paise"]),
        rate_below=Decimal(str(raw["rate_below"])),
        rate_above=Decimal(str(raw["rate_above"])),
        kind=str(raw.get("kind", PRICE_LINE)),
    )


def as_saved(row: TaxSettingVersion) -> SavedTaxVersion:
    return SavedTaxVersion(
        version=row.version,
        applies_from=row.applies_from,
        saved_at=row.created_at,
        rules=tuple(rule_from_json(r) for r in row.rules),
        unmatched_rate=Decimal(row.unmatched_rate),
        round_total_paise=round_total_of(row.options),
        cross_gstin_returns=cross_gstin_of(row.options),
        annual_return_filed=filed_of(row.options),
        gift_with_purchase_is_gift=gift_of(row.options),
    )


def round_total_of(options: Any) -> int:
    """A saved version's rounding unit; a version from before ticket 11 has none,
    and rounds to the rupee as every bill always has."""
    value = (options or {}).get(ROUND_TOTAL_KEY) if isinstance(options, dict) else None
    return value if value in ROUND_TOTAL_CHOICES else DEFAULT_ROUND_TOTAL_PAISE


def cross_gstin_of(options: Any) -> bool:
    """A saved version's cross-GSTIN choice; one from before ticket 13 is off."""
    return isinstance(options, dict) and options.get(CROSS_GSTIN_KEY) is True


def gift_of(options: Any) -> bool:
    """A saved version's gift choice; one from before ticket 14 has the baseline."""
    value = options.get(GIFT_KEY) if isinstance(options, dict) else None
    return value if isinstance(value, bool) else DEFAULT_GIFT


def filed_of(options: Any) -> dict[str, dict[str, str]]:
    """A saved version's filing dates; one from before ticket 13 has none."""
    raw = (options or {}).get(FILED_KEY) if isinstance(options, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        str(gstin): {str(fy): str(day) for fy, day in years.items()}
        for gstin, years in raw.items()
        if isinstance(years, dict)
    }


def options_of(options: Any) -> dict[str, Any]:
    """A saved version's options, every key present (what the pages show)."""
    return {
        ROUND_TOTAL_KEY: round_total_of(options),
        CROSS_GSTIN_KEY: cross_gstin_of(options),
        FILED_KEY: filed_of(options),
        GIFT_KEY: gift_of(options),
    }


def default_options() -> dict[str, Any]:
    """Version 1's options: the baselines."""
    return {
        ROUND_TOTAL_KEY: DEFAULT_ROUND_TOTAL_PAISE,
        CROSS_GSTIN_KEY: False,
        FILED_KEY: {},
        GIFT_KEY: DEFAULT_GIFT,
    }


def _filing_day(fy: str, value: Any, today: date) -> tuple[date | None, str]:
    """One filing date, read: ``(day, "")`` or ``(None, why not)``."""
    try:
        months = financial_year_months(fy)
    except ValueError:
        return None, f"{fy} is not a year like 25-26"
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return None, f"{value} is not a date"
    if day <= date(months[-1].year, 3, 31):
        return None, (
            f"The {fy} return cannot be filed before that year ends on 31 March {months[-1].year}."
        )
    if day > today:
        return None, f"{day} is still to come"
    return day, ""


def _parse_filed(raw: Any, today: date) -> dict[str, dict[str, str]]:
    """The filing dates as Accounts typed them, checked (ticket 13, B10).

    A GSTIN is 15 characters; a year is spelled as the bills spell it (``26-27``);
    a date is a real day after that year ended and not in the future - a return
    cannot be filed before its year is over, nor on a day still to come.
    """
    field_name = f"options.{FILED_KEY}"
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Refusal(
            "INVALID_REQUEST",
            "The annual return filing dates need fixing.",
            issues=[issue("INVALID", "filing dates must be an object", field=field_name)],
        )
    out: dict[str, dict[str, str]] = {}
    problems: list[dict[str, Any]] = []
    for gstin, years in raw.items():
        code = str(gstin).strip().upper()
        if not _GSTIN.match(code):
            problems.append(issue("INVALID", f"{gstin} is not a GSTIN", field=field_name))
            continue
        if not isinstance(years, dict):
            problems.append(issue("INVALID", f"{code}: years must be an object", field=field_name))
            continue
        for fy, value in years.items():
            where = f"{field_name}.{code}.{fy}"
            day, problem = _filing_day(str(fy), value, today)
            if problem:
                problems.append(issue("INVALID", problem, field=where))
            elif day is not None:
                out.setdefault(code, {})[str(fy)] = day.isoformat()
    if problems:
        raise Refusal(
            "INVALID_REQUEST", "The annual return filing dates need fixing.", issues=problems
        )
    return out


def parse_options(raw: Any, today: date | None = None) -> dict[str, Any]:
    """The version's options as Admin submitted them, every key filled in.

    Absent means the baseline (the total to the nearest rupee; no cross-GSTIN
    returns; no filing date recorded). Anything else is refused with the field
    named, never guessed.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise Refusal(
            "INVALID_REQUEST",
            "The options need fixing.",
            issues=[issue("INVALID", "options must be an object", field="options")],
        )
    unknown = sorted(set(raw) - OPTION_KEYS)
    if unknown:
        raise Refusal(
            "INVALID_REQUEST",
            "The options need fixing.",
            issues=[issue("UNKNOWN_FIELD", f"{', '.join(unknown)} not accepted", field="options")],
        )
    value = raw.get(ROUND_TOTAL_KEY, DEFAULT_ROUND_TOTAL_PAISE)
    if isinstance(value, bool) or value not in ROUND_TOTAL_CHOICES:
        raise Refusal(
            "INVALID_REQUEST",
            "Round the invoice total to the nearest rupee, or not at all.",
            issues=[
                issue(
                    "INVALID",
                    "round_total_paise is 100 (nearest rupee) or 1 (no rounding)",
                    field=f"options.{ROUND_TOTAL_KEY}",
                )
            ],
        )
    cross = raw.get(CROSS_GSTIN_KEY, False)
    if not isinstance(cross, bool):
        raise Refusal(
            "INVALID_REQUEST",
            "Say yes or no to cross-GSTIN returns.",
            issues=[
                issue(
                    "INVALID",
                    "cross_gstin_returns is true or false",
                    field=f"options.{CROSS_GSTIN_KEY}",
                )
            ],
        )
    gift = raw.get(GIFT_KEY, DEFAULT_GIFT)
    if not isinstance(gift, bool):
        raise Refusal(
            "INVALID_REQUEST",
            "Say yes or no to treating a gift with purchase as a gift.",
            issues=[
                issue(
                    "INVALID",
                    "gift_with_purchase_is_gift is true or false",
                    field=f"options.{GIFT_KEY}",
                )
            ],
        )
    filed = _parse_filed(raw.get(FILED_KEY), today or timezone.localdate())
    return {ROUND_TOTAL_KEY: value, CROSS_GSTIN_KEY: cross, FILED_KEY: filed, GIFT_KEY: gift}


def saved_versions(tenant_id: Any) -> list[SavedTaxVersion]:
    """Every saved version for the tenant, oldest first."""
    rows = TaxSettingVersion.objects.filter(tenant_id=tenant_id).order_by("version")
    return [as_saved(row) for row in rows]


def in_force(
    versions: list[SavedTaxVersion], day: date, at: datetime | None = None
) -> SavedTaxVersion | None:
    """The saved version a bill of ``day`` (made at ``at``) is taxed under.

    ``None`` is version 1. ``at`` leaves out a version saved after the bill was
    made, so an earlier bill on its applies-from day is not claimed by it.
    """
    live = [v for v in versions if v.applies_from <= day and (at is None or v.saved_at <= at)]
    return max(live, key=lambda v: v.version) if live else None


def store_uses_settings(store: Store) -> bool:
    """Is the tax-settings switch on at this store (and not held by a gate)?"""
    return is_feature_on(store, FEATURE_KEY)


def latest_version_number(tenant_id: Any) -> int:
    row = TaxSettingVersion.objects.filter(tenant_id=tenant_id).order_by("-version").first()
    return row.version if row is not None else LEGACY_VERSION


def legacy_rules() -> list[dict[str, Any]]:
    """Version 1 for display: the slab rows, each with its own date."""
    return [
        {
            "kind": PRICE_LINE,
            "hsn_prefix": row.hsn_prefix,
            "name": row.name,
            "threshold_paise": int(row.threshold_paise or 0),
            "rate_below": rate_text(row.rate_below),
            "rate_above": rate_text(row.rate_above),
            "effective_from": row.effective_from.isoformat(),
        }
        for row in GstSlab.objects.order_by("effective_from", "hsn_prefix")
    ]


# --- checking what Admin typed ------------------------------------------------


def _rate(value: Any, field: str, problems: list[dict[str, Any]]) -> Decimal | None:
    try:
        rate = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        problems.append(issue("INVALID", "a rate must be a number", field=field))
        return None
    if (
        not rate.is_finite()
        or rate < 0
        or rate > _MAX_RATE
        or rate != rate.quantize(Decimal("0.01"))
    ):
        problems.append(
            issue(
                "INVALID", "a rate is a percentage from 0 to 100, two decimals at most", field=field
            )
        )
        return None
    return rate


_RULE_FIELDS = frozenset(
    {"kind", "hsn_prefix", "name", "threshold_paise", "rate_below", "rate_above"}
)
_FLAT_FIELDS = frozenset({"kind", "hsn_prefix", "name", "rate"})


def _rule_from_form(
    row: dict[str, Any], where: str, problems: list[dict[str, Any]]
) -> TaxRule | None:
    """One submitted rule, checked; every problem is added to ``problems``."""
    kind = row.get("kind", PRICE_LINE)
    if kind not in RULE_KINDS:
        problems.append(
            issue("INVALID", "kind must be price_line or flat_rate", field=f"{where}.kind")
        )
        return None
    unknown = sorted(set(row) - (_FLAT_FIELDS if kind == FLAT_RATE else _RULE_FIELDS))
    if unknown:
        problems.append(issue("UNKNOWN_FIELD", f"{', '.join(unknown)} not accepted", field=where))
    prefix = str(row.get("hsn_prefix", "") or "").strip()
    if not _HSN_PREFIX.match(prefix):
        problems.append(
            issue("INVALID", "an HSN prefix is up to 8 digits", field=f"{where}.hsn_prefix")
        )
    elif kind == FLAT_RATE and not prefix:
        # The schedule names the HSN it covers; "every HSN at one rate" is not a
        # schedule entry, and would silently outrank no price line at all.
        problems.append(
            issue(
                "REQUIRED",
                "a rate-schedule rule names the HSN it covers",
                field=f"{where}.hsn_prefix",
            )
        )
    name = str(row.get("name", "") or "").strip()
    if not name or len(name) > 80:
        problems.append(
            issue("INVALID", "a rule needs a name of 1-80 characters", field=f"{where}.name")
        )
    if kind == FLAT_RATE:
        rate = _rate(row.get("rate"), f"{where}.rate", problems)
        if rate is None or not prefix or not _HSN_PREFIX.match(prefix):
            return None
        return flat_rate(prefix, name, rate)
    threshold = row.get("threshold_paise")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        problems.append(
            issue(
                "INVALID",
                "the price line is a whole number of paise, 0 or more",
                field=f"{where}.threshold_paise",
            )
        )
        threshold = None
    below = _rate(row.get("rate_below"), f"{where}.rate_below", problems)
    above = _rate(row.get("rate_above"), f"{where}.rate_above", problems)
    if threshold is None or below is None or above is None:
        return None
    return TaxRule(
        hsn_prefix=prefix, name=name, threshold_paise=threshold, rate_below=below, rate_above=above
    )


def parse_rules(raw: Any) -> tuple[TaxRule, ...]:
    """The rules Admin submitted, checked; a ``Refusal`` names every problem."""
    if not isinstance(raw, list) or not raw:
        raise Refusal(
            "INVALID_REQUEST",
            "Add at least one HSN rule.",
            issues=[issue("REQUIRED", "rules must be a non-empty list", field="rules")],
        )
    problems: list[dict[str, Any]] = []
    rules: list[TaxRule] = []
    seen: set[str] = set()
    for index, row in enumerate(raw):
        where = f"rules[{index}]"
        if not isinstance(row, dict):
            problems.append(issue("INVALID", "each rule is an object", field=where))
            continue
        prefix = str(row.get("hsn_prefix", "") or "").strip()
        if prefix in seen:
            problems.append(
                issue(
                    "DUPLICATE", "two rules cover the same HSN prefix", field=f"{where}.hsn_prefix"
                )
            )
        seen.add(prefix)
        rule = _rule_from_form(row, where, problems)
        if rule is not None:
            rules.append(rule)
    if problems:
        raise Refusal("INVALID_REQUEST", "Some HSN rules need fixing.", issues=problems)
    return tuple(rules)


def parse_rate(value: Any, field: str) -> Decimal:
    problems: list[dict[str, Any]] = []
    rate = _rate(value, field, problems)
    if rate is None:
        raise Refusal(
            "INVALID_REQUEST", "The rate for an HSN with no rule needs fixing.", issues=problems
        )
    return rate
