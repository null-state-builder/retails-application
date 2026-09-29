"""The one mapping rulebook for goods-v1 PT work (store and warehouse operations PRD §5.4).

A brand's words become KDPS values through one governed rulebook: the tenant's
approved vocabulary (``vocabulary`` configuration, one list per dimension) and
its effective crosswalks (``masters.SourceCrosswalk``). A crosswalk whose kind is
a vocabulary dimension is an *attribute rule*: (issuer, column, source text) ->
one approved value; ``masters.goods_identity_services`` documents the issuers.
Every rule is governed - a preparer proposes, the product-master owner confirms -
and only an effective rule with a still-approved target is ever applied.

Where no rule applies, :func:`close_matches` offers deterministic close matches
(normalised equal, a known abbreviation such as ``NVY`` for ``NAVY``, a close
spelling) as *suggestions*. A suggestion is never applied: a person accepts it.
No AI or language model is involved anywhere here.

:class:`Rulebook` is a read-only snapshot, loaded once per mapping run
(:meth:`Rulebook.load`) or built directly from values in a test.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Q

from masters.goods_identity_models import GovernanceState, SourceCrosswalk
from masters.goods_identity_services import (
    ANY_ISSUER,
    CROSSWALK_KINDS,
    brand_issuer,
    normalise_text,
    vocabulary,
)

#: Where a mapped cell's value came from (the PT grid colours these; OPS-16).
FILE = "file"
RULE = "rule"
SUGGESTION = "suggestion"
NONE = "none"
ORIGINS = (FILE, RULE, SUGGESTION, NONE)

#: Why a close match is offered. Deterministic reasons only.
NORMALISED_EQUAL = "normalised_equal"
KNOWN_ABBREVIATION = "known_abbreviation"
CLOSE_SPELLING = "close_spelling"
NAMED_IN_TEXT = "named_in_text"
CONFLICTING_RULES = "conflicting_rules"

#: A close spelling needs at least this ``difflib`` ratio on the compacted text.
CLOSE_SPELLING_RATIO = 0.8
#: At most this many close matches are offered for one text.
SUGGESTION_LIMIT = 5

_VOWELS = frozenset("aeiou")


@dataclass(frozen=True)
class RuleValue:
    """One approved vocabulary value (or, for the brand column, one brand master)."""

    id: str
    dimension: str
    value_key: str
    label: str


@dataclass(frozen=True)
class Rule:
    """One effective crosswalk whose target is approved now."""

    id: str
    dimension: str
    issuer_key: str
    source_key: str
    target: RuleValue


@dataclass(frozen=True)
class Candidate:
    """A close match offered to a person - never applied on its own."""

    value: RuleValue
    reason: str
    score: float


def match_key(text: str) -> str:
    """The form two texts are compared in: NFC, whitespace collapsed, case folded."""
    return normalise_text(text)


def compact(text: str) -> str:
    """Letters and digits only, case folded: ``Navy-Blue`` and ``NAVY BLUE`` agree."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _is_abbreviation(short: str, full: str) -> bool:
    """``short`` is a consonant skeleton of ``full``: ``NVY``/``NAVY``, ``BLK``/``BLACK``.

    It starts with the same letter, carries no vowel after its first letter, is
    at least two characters and is an in-order subsequence of ``full``.
    """
    if len(short) < 2 or len(short) >= len(full) or short[0] != full[0]:
        return False
    if any(ch in _VOWELS for ch in short[1:]) or not short.isalpha():
        return False
    if len(short) * 5 < len(full) * 2:  # under 40% of the word is a guess, not an abbreviation
        return False
    rest = iter(full)
    return all(ch in rest for ch in short)


def _initials(text: str) -> str:
    words = [w for w in re.split(r"[^0-9a-z]+", text.casefold()) if w]
    return "".join(w[0] for w in words) if len(words) >= 2 else ""


def close_match(texts: Sequence[str], value: RuleValue) -> tuple[str, float] | None:
    """(reason, score) when ``value`` is a close match for any of ``texts``; else None.

    ``texts`` are the source text and its mechanical clean-ups (for example the
    size clean-up that reads ``2XL`` as ``XXL``). Exact equality is not a close
    match - the caller has already applied it.
    """
    best: tuple[str, float] | None = None
    targets = {compact(value.value_key), compact(value.label)} - {""}
    for text in texts:
        short = compact(text)
        if not short:
            continue
        for target in targets:
            if short == target:
                found: tuple[str, float] = (NORMALISED_EQUAL, 1.0)
            elif _is_abbreviation(short, target) or (
                len(short) >= 2 and short == _initials(value.label)
            ):
                found = (KNOWN_ABBREVIATION, 0.9)
            else:
                ratio = difflib.SequenceMatcher(None, short, target, autojunk=False).ratio()
                if ratio < CLOSE_SPELLING_RATIO:
                    continue
                found = (CLOSE_SPELLING, round(ratio, 3))
            if best is None or found[1] > best[1]:
                best = found
    return best


def close_matches(
    texts: Sequence[str],
    values: Iterable[RuleValue],
    *,
    exclude: Iterable[str] = (),
    limit: int = SUGGESTION_LIMIT,
) -> list[Candidate]:
    """Deterministic close matches, best first (score, then value key); never applied."""
    skip = set(exclude)
    found: list[Candidate] = []
    for value in values:
        if value.id in skip:
            continue
        hit = close_match(texts, value)
        if hit is not None:
            found.append(Candidate(value=value, reason=hit[0], score=hit[1]))
    found.sort(key=lambda c: (-c.score, c.value.value_key, c.value.id))
    return found[:limit]


def spaced(text: str) -> str:
    """Upper-case words separated by single spaces (the keyword matcher's form)."""
    return re.sub(r"[^A-Z0-9]+", " ", " ".join(str(text).split()).upper()).strip()


def keyword_in(pattern: str, text: str) -> bool:
    """``pattern`` occurs in ``text`` as whole words.

    ``PANT`` hits ``TRACK PANT`` but never ``PANTIE`` or ``LAPTOP``. A glued match
    is allowed only inside an SAP finished-goods token (``FGTROUSER``), which is
    how Madura packs the item into its material code.
    """
    words = spaced(text)
    key = spaced(pattern)
    if not key or not words:
        return False
    if f" {key} " in f" {words} ":
        return True
    return (
        " " not in key
        and len(key) >= 5
        and any(key in token for token in words.split() if token.startswith("FG"))
    )


@dataclass
class Rulebook:
    """A read-only snapshot of approved vocabulary and effective rules.

    ``values`` holds each governed dimension's approved, unretired values;
    ``rules`` the effective attribute rules whose target is one of them;
    ``brands`` the live brand masters (the BRAND column's values) and
    ``brand_rules`` the effective brand crosswalks. ``stale`` lists effective
    rules that are skipped because their target is no longer approved.
    """

    values: dict[str, tuple[RuleValue, ...]]
    rules: tuple[Rule, ...] = ()
    brands: tuple[RuleValue, ...] = ()
    brand_rules: tuple[Rule, ...] = ()
    stale: tuple[str, ...] = ()
    _exact: dict[tuple[str, str], list[RuleValue]] = field(default_factory=dict, repr=False)
    _index: dict[tuple[str, str, str], list[Rule]] = field(default_factory=dict, repr=False)
    _keywords: dict[tuple[str, str], list[Rule]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for dimension, values in self.values.items():
            for value in values:
                for text in {match_key(value.value_key), match_key(value.label)}:
                    bucket = self._exact.setdefault((dimension, text), [])
                    if value not in bucket:
                        bucket.append(value)
        for brand in self.brands:
            for text in {match_key(brand.value_key), match_key(brand.label)}:
                self._exact.setdefault(("brand", text), []).append(brand)
        for rule in (*self.rules, *self.brand_rules):
            key = (rule.dimension, rule.issuer_key, match_key(rule.source_key))
            self._index.setdefault(key, []).append(rule)
            self._keywords.setdefault((rule.dimension, rule.issuer_key), []).append(rule)
        for ordered in self._keywords.values():
            ordered.sort(key=lambda r: (-len(spaced(r.source_key)), spaced(r.source_key), r.id))

    # -- reads -------------------------------------------------------------

    def governed(self, dimension: str) -> bool:
        return dimension == "brand" or dimension in self.values

    def choices(self, dimension: str) -> tuple[RuleValue, ...]:
        return self.brands if dimension == "brand" else self.values.get(dimension, ())

    def exact(self, dimension: str, text: str) -> RuleValue | None:
        """The one approved value whose key or label equals ``text``; None if none or two."""
        hits = self._exact.get((dimension, match_key(text)), [])
        return hits[0] if len(hits) == 1 else None

    def lookup(
        self, dimension: str, text: str, issuers: Sequence[str]
    ) -> tuple[Rule | None, tuple[RuleValue, ...]]:
        """(rule, conflicting targets) for ``text`` read by the first issuer that knows it.

        Two effective rules of one issuer naming different targets are a
        conflict: none is applied and both targets are returned for a person.
        """
        key = match_key(text)
        if not key:
            return None, ()
        for issuer in issuers:
            hits = self._index.get((dimension, issuer, key), [])
            if hits:
                return _one(hits)
        return None, ()

    def keyword(
        self, dimension: str, text: str, issuers: Sequence[str]
    ) -> tuple[Rule | None, tuple[RuleValue, ...]]:
        """The longest rule source found as whole words in ``text`` (issuers in order)."""
        for issuer in issuers:
            candidates = self._keywords.get((dimension, issuer), [])
            for index, rule in enumerate(candidates):
                if not keyword_in(rule.source_key, text):
                    continue
                same = [
                    other
                    for other in candidates[index:]
                    if spaced(other.source_key) == spaced(rule.source_key)
                ]
                return _one(same)
        return None, ()

    def named_in(self, dimension: str, text: str) -> list[RuleValue]:
        """Approved values of ``dimension`` named as whole words in ``text``."""
        return [
            value
            for value in self.values.get(dimension, ())
            if len(spaced(value.value_key)) >= 3 and keyword_in(value.value_key, text)
        ]

    # -- loading -------------------------------------------------------------

    @classmethod
    def load(cls, tenant_id: uuid.UUID, as_of: datetime) -> Rulebook:
        """The tenant's approved vocabulary and effective rules at ``as_of``."""
        from masters.models import Brand

        values: dict[str, tuple[RuleValue, ...]] = {
            dimension: tuple(
                RuleValue(str(v.id), dimension, v.value_key, v.label)
                for v in listed
                if not v.retired
            )
            for dimension, listed in vocabulary(tenant_id, as_of).items()
        }
        by_id = {value.id: value for listed in values.values() for value in listed}
        brands = tuple(
            RuleValue(str(pk), "brand", code, name)
            for pk, code, name in Brand.objects.filter(is_active=True)
            .order_by("name", "pk")
            .values_list("pk", "code", "name")
        )
        by_id.update({brand.id: brand for brand in brands})
        rules: list[Rule] = []
        brand_rules: list[Rule] = []
        stale: list[str] = []
        crosswalks = (
            SourceCrosswalk.objects.filter(
                tenant_id=tenant_id, governance_state=GovernanceState.EFFECTIVE
            )
            .filter(Q(retired_at__isnull=True) | Q(retired_at__gt=as_of))
            .exclude(kind__in=sorted(CROSSWALK_KINDS - {"brand"}))
            .order_by("kind", "issuer_key", "source_key", "id")
        )
        for row in crosswalks:
            target = by_id.get(row.target_key)
            if target is None or target.dimension != row.kind:
                # A brand rule names a live brand; an attribute rule an approved value
                # of its own dimension. Anything else is no longer a rule to apply.
                stale.append(str(row.pk))
                continue
            rule = Rule(str(row.pk), row.kind, row.issuer_key, row.source_key, target)
            (brand_rules if row.kind == "brand" else rules).append(rule)
        return cls(
            values=values,
            rules=tuple(rules),
            brands=brands,
            brand_rules=tuple(brand_rules),
            stale=tuple(stale),
        )


def _one(hits: list[Rule]) -> tuple[Rule | None, tuple[RuleValue, ...]]:
    targets: list[RuleValue] = []
    for hit in hits:
        if hit.target not in targets:
            targets.append(hit.target)
    if len(targets) == 1:
        return min(hits, key=lambda r: r.id), ()
    return None, tuple(targets)


def issuers_for(brand_id: int | None, *, issuer_key: str | None = None) -> tuple[str, ...]:
    """The issuers a brand file's own words are read by, most specific first."""
    out: list[str] = []
    if issuer_key:
        out.append(issuer_key)
    if brand_id is not None:
        out.append(brand_issuer(brand_id))
    out.append(ANY_ISSUER)
    return tuple(dict.fromkeys(out))
