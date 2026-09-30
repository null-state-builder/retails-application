"""The one effective-configuration resolver (change PRD §14.4, overall PRD §15.2.1 rule 5).

Every goods decision that depends on configuration asks here which approved version
applies. A version applies to one target - a tenant, a site, the brands involved, a
purpose - at one business instant when all of these hold:

* it is the named kind in the caller's tenant;
* it was not withdrawn for invalidity;
* its scope covers the target: an empty dimension is an explicit tenant-wide choice,
  a filled one must contain the target's value, and a target with no value in a
  filled dimension is not covered;
* its effective period ``[effective_from, effective_to)`` holds the instant - the
  start is valid, the end is not, and a null end is open-ended.

Exactly one such version may match. Missing, wrong-scope, future, expired, withdrawn
and overlapping matches fail closed. A pinned version is checked the same way at its
fixed business instant, so a later version never silently replaces it.

Approval of a version is where mutual exclusion is enforced: a version may not overlap
another of the same kind, discriminator (the payload keys in ``EXCLUSIVE_BY``) and an
intersecting scope. The earlier version of the same draft or the same exact scope is
superseded instead: its period ends where the new one starts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.refusals import Refusal, issue
from masters.goods_models import ConfigVersion, EffectiveVersionPeriod

SCOPE_KINDS = ("tenant", "entity", "sites", "sbus", "brands")
SCOPE_LISTS = ("site_ids", "sbu_ids", "brand_ids", "purposes")
SCOPE_FIELDS = frozenset({"scope_kind", "entity_id", *SCOPE_LISTS})

#: Payload keys that, with the kind and an intersecting scope, make two versions
#: mutually exclusive. A kind not listed may have several versions in force at once.
EXCLUSIVE_BY: dict[str, tuple[str, ...]] = {
    "approval": ("action",),
    "profile": ("family",),
    "identity_profile": ("family",),
    "vocabulary": ("dimension",),
    "rates": (),
    "tax_rates": (),
    "reasons": ("action",),
    "series": ("entity_id", "type", "fy"),
    "label": (),
    "notifications": ("event",),
    "workflow": ("operation",),
    "business_profile": (),
    "non_trading": ("site_id",),
    # GSA-T08: one calendar in force at a time, tenant-wide — never several
    # candidate calendars a caller would have to pick between.
    "working_calendar": (),
    "sell_policy": (),
}

#: Kinds read tenant-wide by every caller (no site, brand or purpose context), so their
#: versions must have a tenant scope.
TENANT_WIDE_KINDS = frozenset(
    {"vocabulary", "identity_profile", "barcode_range", "business_profile", "working_calendar"}
)

CONFIGURATION = EffectiveVersionPeriod.TargetKind.CONFIGURATION


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _scope_invalid(path: str, message: str) -> Refusal:
    return Refusal(
        "CONFIG_INVALID",
        f"{path} {message}.",
        status=422,
        issues=[issue("INVALID", message, field=path)],
    )


def _id_list(raw: Any, path: str, *, parse: Any) -> list[str]:
    if not isinstance(raw, list) or len(raw) > 1000:
        raise _scope_invalid(path, "must be a list of at most 1000 identifiers")
    out: set[str] = set()
    for index, value in enumerate(raw):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise _scope_invalid(f"{path}[{index}]", "must be an identifier")
        try:
            out.add(parse(value))
        except (TypeError, ValueError):
            raise _scope_invalid(f"{path}[{index}]", "must be an identifier") from None
    return sorted(out)


def _int_text(value: Any) -> str:
    text = str(value)
    if not text.isdigit():
        raise ValueError(text)
    return str(int(text))


def _uuid_text(value: Any) -> str:
    return str(uuid.UUID(str(value)))


def _purpose_text(value: Any) -> str:
    text = str(value)
    if not text or len(text) > 60 or not text.replace("_", "").replace(".", "").isalnum():
        raise ValueError(text)
    return text


def normalise_scope(raw: Any) -> dict[str, Any]:
    """The closed, canonical ``ConfigScope``: ``{scope_kind, entity_id, site_ids, sbu_ids,
    brand_ids, purposes}``.

    Lists are sorted and de-duplicated, so equal scopes have one form and one hash.
    ``scope_kind`` names the scope's main dimension and must agree with it: ``tenant``
    has no organisation dimension, ``entity`` names an entity, ``sites``/``sbus``/
    ``brands`` name at least one. Other dimensions may narrow further. ``purposes``
    is optional; empty means every purpose.
    """
    if not isinstance(raw, dict):
        raise _scope_invalid("scope", "must be an object")
    unknown = sorted(set(raw) - SCOPE_FIELDS)
    if unknown:
        raise _scope_invalid(f"scope.{unknown[0]}", "is not a field of a configuration scope")
    kind = raw.get("scope_kind")
    if kind not in SCOPE_KINDS:
        raise _scope_invalid("scope.scope_kind", f"must be one of {', '.join(SCOPE_KINDS)}")
    entity = raw.get("entity_id")
    entity_id: str | None = None
    if entity not in (None, ""):
        try:
            entity_id = _int_text(entity)
        except ValueError:
            raise _scope_invalid("scope.entity_id", "must be an identifier") from None
    scope: dict[str, Any] = {
        "scope_kind": kind,
        "entity_id": entity_id,
        "site_ids": _id_list(raw.get("site_ids") or [], "scope.site_ids", parse=_int_text),
        "sbu_ids": _id_list(raw.get("sbu_ids") or [], "scope.sbu_ids", parse=_uuid_text),
        "brand_ids": _id_list(raw.get("brand_ids") or [], "scope.brand_ids", parse=_int_text),
        "purposes": _id_list(raw.get("purposes") or [], "scope.purposes", parse=_purpose_text),
    }
    organisation = entity_id or scope["site_ids"] or scope["sbu_ids"] or scope["brand_ids"]
    needs = {"entity": entity_id, "sites": scope["site_ids"], "sbus": scope["sbu_ids"]}
    needs["brands"] = scope["brand_ids"]
    if kind == "tenant" and organisation:
        raise _scope_invalid("scope.scope_kind", "tenant cannot name an entity, site, SBU or brand")
    if kind in needs and not needs[kind]:
        raise _scope_invalid("scope.scope_kind", f"{kind} needs at least one matching identifier")
    return scope


def check_scope_references(scope: dict[str, Any]) -> None:
    """Every identifier in ``scope`` names a record of the trusted tenant."""
    from masters.goods_models import Sbu
    from masters.models import Brand, LegalEntity, Store

    checks: list[tuple[str, Any, list[str]]] = [
        ("scope.site_ids", Store, scope["site_ids"]),
        ("scope.brand_ids", Brand, scope["brand_ids"]),
        ("scope.sbu_ids", Sbu, scope["sbu_ids"]),
    ]
    if scope["entity_id"] is not None:
        checks.append(("scope.entity_id", LegalEntity, [scope["entity_id"]]))
    for path, model, ids in checks:
        if ids and model.objects.filter(pk__in=ids).count() != len(ids):
            raise _scope_invalid(path, "names a record that does not exist")


def read_scope(raw: Any) -> dict[str, Any]:
    """A stored scope in canonical form, without refusing old shapes."""
    try:
        return normalise_scope(raw)
    except Refusal:
        source = raw if isinstance(raw, dict) else {}
        return {
            "scope_kind": source.get("scope_kind") or "tenant",
            "entity_id": str(source["entity_id"]) if source.get("entity_id") else None,
            **{name: sorted({str(v) for v in source.get(name) or []}) for name in SCOPE_LISTS},
        }


def scope_hash(scope: dict[str, Any]) -> str:
    return content_hash(normalise_scope(scope))


# ---------------------------------------------------------------------------
# Targets and matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigTarget:
    """What a decision is about: a site, every brand it involves, a purpose and an instant."""

    at: datetime
    site_id: int | None = None
    brand_ids: tuple[int | None, ...] = ()
    purpose: str | None = None
    _entity: list[int | None] = field(default_factory=list, compare=False, repr=False)

    @classmethod
    def of(
        cls,
        at: datetime,
        *,
        site_id: int | None = None,
        brand_ids: Iterable[int | None] = (),
        purpose: str | None = None,
    ) -> ConfigTarget:
        ordered = tuple(sorted(set(brand_ids), key=lambda b: (b is None, b or 0)))
        return cls(at=at, site_id=site_id, brand_ids=ordered, purpose=purpose)

    @property
    def entity_id(self) -> int | None:
        if not self._entity:
            from masters.models import Store

            entity = None
            if self.site_id is not None:
                entity = (
                    Store.objects.filter(pk=self.site_id)
                    .values_list("gstin__legal_entity_id", flat=True)
                    .first()
                )
            self._entity.append(entity)
        return self._entity[0]

    def describe(self) -> dict[str, Any]:
        return {
            "site_ids": [str(self.site_id)] if self.site_id is not None else [],
            "brand_ids": [str(b) for b in self.brand_ids if b is not None],
            "purpose": self.purpose,
            "at": self.at.isoformat(),
        }


def covers(scope: dict[str, Any], target: ConfigTarget) -> bool:
    """``scope`` reaches the target in every dimension it fills."""
    if scope["purposes"] and target.purpose not in scope["purposes"]:
        return False
    if scope["entity_id"] is not None and str(target.entity_id) != scope["entity_id"]:
        return False
    if scope["site_ids"] and str(target.site_id) not in scope["site_ids"]:
        return False
    if scope["brand_ids"]:
        if not target.brand_ids or any(
            b is None or str(b) not in scope["brand_ids"] for b in target.brand_ids
        ):
            return False
    return not scope["sbu_ids"] or _covers_sbus(scope["sbu_ids"], target)


def _covers_sbus(sbu_ids: list[str], target: ConfigTarget) -> bool:
    from masters.goods_models import Sbu

    if target.site_id is None or not target.brand_ids:
        return False
    for brand in target.brand_ids:
        if brand is None:
            return False
        sbu = Sbu.objects.filter(site_id=target.site_id, brand_id=brand).first()
        if sbu is None or str(sbu.pk) not in sbu_ids:
            return False
    return True


def scopes_intersect(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Some target could be covered by both scopes (conservative: when unsure, yes)."""
    for name in SCOPE_LISTS:
        if a[name] and b[name] and not set(a[name]) & set(b[name]):
            return False
    if a["entity_id"] and b["entity_id"] and a["entity_id"] != b["entity_id"]:
        return False
    from masters.models import Store

    for one, other in ((a, b), (b, a)):
        if one["entity_id"] and other["site_ids"]:
            inside = Store.objects.filter(
                pk__in=other["site_ids"], gstin__legal_entity_id=one["entity_id"]
            ).exists()
            if not inside:
                return False
    return True


def discriminator(kind: str, payload: Any) -> list[Any] | None:
    keys = EXCLUSIVE_BY.get(kind)
    if keys is None:
        return None
    source = payload if isinstance(payload, dict) else {}
    return [str(source[k]) if source.get(k) is not None else None for k in keys]


def exclusivity_key(kind: str, payload: Any, scope: dict[str, Any]) -> str | None:
    marker = discriminator(kind, payload)
    if marker is None:
        return None
    return f"{kind}:{content_hash(marker)[:24]}:{scope_hash(scope)[:32]}"


@dataclass(frozen=True)
class Candidate:
    version: ConfigVersion
    scope: dict[str, Any]
    effective_from: datetime
    effective_to: datetime | None
    withdrawn_at: datetime | None

    def failure(self, target: ConfigTarget) -> str | None:
        """Why this version does not apply to ``target``, or ``None`` when it does."""
        if self.withdrawn_at is not None:
            return "CONFIG_WITHDRAWN"
        if not covers(self.scope, target):
            return "CONFIG_WRONG_SCOPE"
        if self.effective_from > target.at:
            return "CONFIG_FUTURE"
        if self.effective_to is not None and self.effective_to <= target.at:
            return "CONFIG_EXPIRED"
        return None

    def state(self, now: datetime) -> str:
        if self.withdrawn_at is not None:
            return "withdrawn"
        if self.effective_from > now:
            return "scheduled"
        if self.effective_to is not None and self.effective_to <= now:
            return "ended"
        return "effective"


def candidates(versions: Iterable[ConfigVersion]) -> list[Candidate]:
    rows = list(versions)
    periods = {
        period.target_id: period
        for period in EffectiveVersionPeriod.objects.filter(
            target_kind=CONFIGURATION, target_id__in=[row.pk for row in rows]
        )
    }
    out: list[Candidate] = []
    for row in rows:
        period = periods.get(row.pk)
        out.append(
            Candidate(
                version=row,
                scope=read_scope(row.scope),
                effective_from=period.effective_from if period else row.effective_from,
                effective_to=period.effective_to if period else None,
                withdrawn_at=period.withdrawn_at if period else None,
            )
        )
    return out


def candidate(version: ConfigVersion) -> Candidate:
    return candidates([version])[0]


def in_force(version: ConfigVersion, at: datetime) -> bool:
    """Not withdrawn and effective at ``at``, whatever its scope (tenant-wide callers)."""
    found = candidate(version)
    if found.withdrawn_at is not None or found.effective_from > at:
        return False
    return found.effective_to is None or found.effective_to > at


MESSAGES = {
    "CONFIG_NOT_FOUND": "is not an approved {kind} configuration of this business",
    "CONFIG_WITHDRAWN": "was withdrawn",
    "CONFIG_WRONG_SCOPE": "does not cover this site, brand or purpose",
    "CONFIG_FUTURE": "is not effective yet at this time",
    "CONFIG_EXPIRED": "is no longer effective at this time",
    "CONFIG_MISSING": "has no effective {kind} configuration for this site, brand and purpose",
    "CONFIG_AMBIGUOUS": "has more than one effective {kind} configuration here",
}


def refusal(code: str, reason: str, kind: str, path: str, *, status: int = 422) -> Refusal:
    message = MESSAGES[reason].format(kind=kind)
    return Refusal(
        code,
        f"The {kind} configuration {message}.",
        status=status,
        issues=[issue(reason, message, field=path)],
    )


def _group(tenant_id: uuid.UUID, kind: str, payload: Any) -> list[Candidate]:
    marker = discriminator(kind, payload)
    rows = ConfigVersion.objects.filter(tenant_id=tenant_id, kind=kind)
    return [c for c in candidates(rows) if discriminator(kind, c.version.payload) == marker]


def check_pinned(
    tenant_id: uuid.UUID,
    kind: str,
    raw_id: Any,
    target: ConfigTarget,
    *,
    code: str,
    path: str,
    status: int = 422,
) -> ConfigVersion:
    """The pinned version ``raw_id`` applies to ``target`` at its business instant."""
    try:
        version_id = uuid.UUID(str(raw_id))
    except ValueError:
        raise refusal(code, "CONFIG_NOT_FOUND", kind, path, status=status) from None
    version = ConfigVersion.objects.filter(tenant_id=tenant_id, pk=version_id, kind=kind).first()
    if version is None:
        raise refusal(code, "CONFIG_NOT_FOUND", kind, path, status=status)
    pinned = candidate(version)
    reason = pinned.failure(target)
    if reason is not None:
        raise refusal(code, reason, kind, path, status=status)
    if kind in EXCLUSIVE_BY:
        rivals = [
            c
            for c in _group(tenant_id, kind, version.payload)
            if c.version.pk != version.pk and c.failure(target) is None
        ]
        if rivals:
            raise refusal(code, "CONFIG_AMBIGUOUS", kind, path, status=status)
    return version


def resolve(
    tenant_id: uuid.UUID,
    kind: str,
    target: ConfigTarget,
    *,
    match: dict[str, Any],
    code: str,
    path: str,
    status: int = 422,
) -> ConfigVersion:
    """The one version of ``kind`` whose payload has ``match`` and that applies to ``target``."""
    rows = ConfigVersion.objects.filter(tenant_id=tenant_id, kind=kind)
    for key, value in match.items():
        rows = rows.filter(**{f"payload__{key}": value})
    found = [c for c in candidates(rows) if c.failure(target) is None]
    if not found:
        raise refusal(code, "CONFIG_MISSING", kind, path, status=status)
    if len(found) > 1:
        raise refusal(code, "CONFIG_AMBIGUOUS", kind, path, status=status)
    return found[0].version


def effective_at(tenant_id: uuid.UUID, kind: str, at: datetime) -> list[ConfigVersion]:
    """Tenant-wide lookups (vocabulary): versions in force at ``at``, whatever their scope."""
    rows = ConfigVersion.objects.filter(tenant_id=tenant_id, kind=kind, effective_from__lte=at)
    return [
        c.version
        for c in candidates(rows)
        if c.withdrawn_at is None and (c.effective_to is None or c.effective_to > at)
    ]


def approved_working_calendar(tenant_id: uuid.UUID, at: datetime) -> ConfigVersion | None:
    """The one approved working calendar in force at ``at`` (GSA-T08), or ``None``.

    ``working_calendar`` is exclusive and tenant-wide (``EXCLUSIVE_BY``,
    ``TENANT_WIDE_KINDS``), so at most one version can ever be in force — there is
    no "which one" to pick between. ``None`` means no tenant has approved one yet:
    callers must not invent a default from that absence (GSA-T08's own rule), and
    the synthetic tenant's Monday-Saturday calendar is a seeded fixture, never a
    fallback this function supplies on its own.
    """
    versions = effective_at(tenant_id, "working_calendar", at)
    return versions[0] if versions else None


# ---------------------------------------------------------------------------
# Approval of a version: mutual exclusion, supersession, withdrawal
# ---------------------------------------------------------------------------


def _overlaps(
    start: datetime, end: datetime | None, other_start: datetime, other_end: datetime | None
) -> bool:
    return (end is None or other_start < end) and (other_end is None or start < other_end)


def _overlap_refusal(other: ConfigVersion) -> Refusal:
    """Name the version this clashes with, and the period it clashes over.

    The clashing version usually belongs to a different draft line, so it is not
    on the screen the refusal lands on: without its dates the person is told only
    that something conflicts, and given a UUID to go and look it up by.
    """
    period = candidate(other)
    until = period.effective_to.isoformat() if period.effective_to else "open-ended"
    message = (
        f"overlaps approved {other.kind} version {other.version} "
        f"({other.pk}), in force from {period.effective_from.isoformat()} "
        f"until {until}, for an intersecting scope"
    )
    return Refusal(
        "CONFIG_INVALID",
        f"This configuration {message}.",
        status=422,
        issues=[issue("CONFIG_OVERLAP", message, field="effective_from")],
    )


def conflicts(
    tenant_id: uuid.UUID,
    *,
    kind: str,
    payload: Any,
    scope: dict[str, Any],
    effective_from: datetime,
    effective_to: datetime | None,
    draft_id: uuid.UUID | None,
    exclude: uuid.UUID | None = None,
) -> tuple[list[Candidate], list[Candidate]]:
    """(versions this one would supersede, versions it would overlap). Pure; no writes."""
    if effective_to is not None and effective_to <= effective_from:
        raise Refusal(
            "CONFIG_INVALID",
            "effective_to must be after effective_from.",
            status=422,
            issues=[issue("INVALID", "must be after effective_from", field="effective_to")],
        )
    if kind not in EXCLUSIVE_BY:
        return [], []
    canonical = normalise_scope(scope)
    superseded: list[Candidate] = []
    overlapping: list[Candidate] = []
    for other in _group(tenant_id, kind, payload):
        if other.version.pk == exclude or other.withdrawn_at is not None:
            continue
        if not _overlaps(effective_from, effective_to, other.effective_from, other.effective_to):
            continue
        same_draft = draft_id is not None and other.version.draft_id == draft_id
        # A later version of the same draft replaces it whatever scope either names, so an
        # edited scope never leaves the old one in force; otherwise a clash needs scopes
        # that can meet.
        if not same_draft and not scopes_intersect(canonical, other.scope):
            continue
        same_line = same_draft or other.scope == canonical
        # A time-limited successor must not end a longer predecessor for good.
        cuts_off = effective_to is not None and (
            other.effective_to is None or other.effective_to > effective_to
        )
        if same_line and other.effective_from < effective_from and not cuts_off:
            superseded.append(other)
        else:
            overlapping.append(other)
    return superseded, overlapping


def refuse_conflicts(
    tenant_id: uuid.UUID,
    *,
    kind: str,
    payload: Any,
    scope: dict[str, Any],
    effective_from: datetime,
    effective_to: datetime | None,
    draft_id: uuid.UUID | None,
) -> None:
    _superseded, overlapping = conflicts(
        tenant_id,
        kind=kind,
        payload=payload,
        scope=scope,
        effective_from=effective_from,
        effective_to=effective_to,
        draft_id=draft_id,
    )
    if overlapping:
        raise _overlap_refusal(overlapping[0].version)


def activate(
    run: CommandRun,
    version: ConfigVersion,
    *,
    effective_to: datetime | None,
    effective_from: datetime | None = None,
    not_before: datetime | None = None,
) -> list[str]:
    """Give a just-approved version its effective period; returns what it superseded.

    Serialised per kind and discriminator, so two approvals of rival versions cannot both
    pass the overlap check. The database exclusion constraint backs the same rule for
    versions of one exact scope.

    A version that ends an earlier one starts no earlier than ``not_before`` (its approval):
    ending a predecessor in the past would rewrite what decisions under it relied on. A
    first version changes no history and keeps its drafted start.
    """
    marker = discriminator(version.kind, version.payload)
    if marker is not None:
        run.advisory_lock(
            LockRank.DRAFT, [f"config-exclusive:{version.kind}:{content_hash(marker)}"]
        )
    scope = normalise_scope(version.scope)
    starts = effective_from or version.effective_from

    def check(start: datetime) -> tuple[list[Candidate], list[Candidate]]:
        return conflicts(
            run.tenant_id,
            kind=version.kind,
            payload=version.payload,
            scope=scope,
            effective_from=start,
            effective_to=effective_to,
            draft_id=version.draft_id,
            exclude=version.pk,
        )

    superseded, overlapping = check(starts)
    if superseded and not_before is not None and starts < not_before:
        starts = not_before
        superseded, overlapping = check(starts)
    if overlapping:
        raise _overlap_refusal(overlapping[0].version)
    closed: list[str] = []
    if superseded:
        periods = run.lock(
            LockRank.DRAFT,
            EffectiveVersionPeriod.objects.filter(
                target_kind=CONFIGURATION, target_id__in=[c.version.pk for c in superseded]
            ),
        )
        for period in periods:
            period.effective_to = starts
            period.save(update_fields=["effective_to"])
            closed.append(str(period.target_id))
    EffectiveVersionPeriod.objects.create(
        tenant_id=run.tenant_id,
        target_kind=CONFIGURATION,
        target_id=version.pk,
        scope_key=exclusivity_key(version.kind, version.payload, scope) or f"version:{version.pk}",
        effective_from=starts,
        effective_to=effective_to,
    )
    return sorted(closed)


def withdraw(
    run: CommandRun, version: ConfigVersion, *, reason_code: str
) -> EffectiveVersionPeriod:
    """Withdraw an approved version for invalidity: no later use, pinned or not."""
    locked = run.lock(
        LockRank.DRAFT,
        EffectiveVersionPeriod.objects.filter(target_kind=CONFIGURATION, target_id=version.pk),
    )
    period: EffectiveVersionPeriod
    if locked:
        period = locked[0]
    else:
        period = EffectiveVersionPeriod.objects.create(
            tenant_id=run.tenant_id,
            target_kind=CONFIGURATION,
            target_id=version.pk,
            scope_key=f"version:{version.pk}",
            effective_from=version.effective_from,
        )
    if period.withdrawn_at is not None:
        raise Refusal("STATE_CONFLICT", "This configuration version is already withdrawn.")
    period.withdrawn_at = run.now
    period.withdrawn_reason = reason_code[:60]
    period.withdrawn_by_id = run.principal.human_id
    period.save(update_fields=["withdrawn_at", "withdrawn_reason", "withdrawn_by"])
    return period
