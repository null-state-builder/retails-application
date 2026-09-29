"""Trusted access context for a goods-v1 request (design §4.2, §3.1 ``resolve_scope``).

Everything here is read from server records: the session, the person behind the
login and that person's effective grants. Nothing a client sends can widen it.
Each grant is one complete entitlement - its actions and fields inside its own
site and brand reach - and grants combine only as a union of those. An empty grant
set is an empty scope, never "everything"; a record's missing dimension narrows,
never widens. Checks are judged by database time, and a command replays them under
lock before it commits (``AccessContext.revalidate``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import connection
from django.db.models import Q

from accounts.sessions import step_up_valid_until
from core.commands import CommandRun, LockRank, Principal, database_now
from core.refusals import Refusal


@dataclass(frozen=True)
class GrantView:
    id: uuid.UUID
    role_code: str
    scope_kind: str
    entity_id: int | None
    site_id: int | None
    sbu_id: uuid.UUID | None
    sbu_site_id: int | None
    sbu_brand_id: int | None
    brand_id: int | None
    actions: frozenset[str]
    fields: frozenset[str]
    #: When this grant stops by itself (end date, closing period or a dated revocation).
    ends_at: datetime | None = None
    #: Granted deliberately narrower than its role template (a prepare-only grant
    #: at another site, say), so a re-seed must not top it up to the template.
    narrowed: bool = False
    #: Written by the deployment's own seed. Only these may a later re-seed widen
    #: to a role template that has grown since; a grant somebody made on the
    #: People screen is their decision, not the seed's, at whatever width.
    seeded: bool = False


#: One authorisation a request relied on, replayed against fresh grants before commit.
Demand = tuple[Any, ...]


@dataclass
class AccessContext:
    """What one person may do right now, grant by grant.

    A grant is a complete entitlement: its actions and fields apply only inside its
    own site/entity and brand reach. Grants combine as a union of those whole
    entitlements, so a site from one grant never pairs with a brand from another.

    A record's missing dimension means the record has none. ``can(action,
    site_id=S)`` asks about a site record that carries no brand, which only a grant
    not limited to a brand covers; it never means "at S, for any brand".
    """

    user: Any
    human_id: uuid.UUID
    tenant_id: uuid.UUID
    session: Any
    grants: list[GrantView] = field(default_factory=list)
    _entity_sites: dict[int, set[int]] = field(default_factory=dict)
    #: Positive checks this request relied on; commands replay them before commit.
    _demands: set[Demand] = field(default_factory=set)

    # -- identity --------------------------------------------------------
    def principal(self) -> Principal:
        guarded = self.session is not None and hasattr(self.session, "token_hash")
        return Principal(
            tenant_id=self.tenant_id,
            human_id=self.human_id,
            user_id=getattr(self.user, "pk", None),
            session_id=getattr(self.session, "pk", None),
            role_grant_ids=tuple(str(g.id) for g in self.grants),
            step_up_at=getattr(self.session, "step_up_at", None),
            guard=self.revalidate if guarded else None,
        )

    # -- one grant's reach -------------------------------------------------
    def _sites_of_entity(self, entity_id: int) -> set[int]:
        if entity_id not in self._entity_sites:
            from masters.models import Store

            self._entity_sites[entity_id] = set(
                Store.objects.filter(gstin__legal_entity_id=entity_id).values_list("id", flat=True)
            )
        return self._entity_sites[entity_id]

    @staticmethod
    def brand_limit(grant: GrantView) -> int | None:
        """The one brand a grant is limited to; ``None`` when it reaches every brand."""
        if grant.scope_kind == "brand":
            return grant.brand_id
        if grant.scope_kind == "sbu":
            return grant.sbu_brand_id
        return None

    def reaches_site(
        self, grant: GrantView, site_id: int | None, entity_id: int | None = None
    ) -> bool:
        """The site dimension alone. ``site_id=None`` is a record held at no site."""
        kind = grant.scope_kind
        if kind in ("tenant", "brand"):
            return True
        if kind == "entity":
            if site_id is not None:
                return site_id in self._sites_of_entity(int(grant.entity_id or 0))
            return entity_id is not None and entity_id == grant.entity_id
        if kind == "site":
            return site_id is not None and site_id == grant.site_id
        if kind == "sbu":
            return site_id is not None and site_id == grant.sbu_site_id
        return False

    def reaches_brand(self, grant: GrantView, brand_id: int | None) -> bool:
        """The brand dimension alone. ``brand_id=None`` is a record with no brand."""
        limit = self.brand_limit(grant)
        return limit is None or (brand_id is not None and brand_id == limit)

    def grant_covers(
        self,
        grant: GrantView,
        site_id: int | None,
        brand_id: int | None,
        *,
        entity_id: int | None = None,
    ) -> bool:
        return self.reaches_site(grant, site_id, entity_id) and self.reaches_brand(grant, brand_id)

    # -- checks ------------------------------------------------------------
    def _holds(self, action: str) -> bool:
        return any(action in g.actions for g in self.grants)

    def _can(
        self, action: str, site_id: int | None, brand_id: int | None, entity_id: int | None
    ) -> bool:
        return any(
            action in g.actions and self.grant_covers(g, site_id, brand_id, entity_id=entity_id)
            for g in self.grants
        )

    def _covers_all(
        self,
        actions: frozenset[str],
        cells: frozenset[tuple[int | None, int | None]],
        fields: frozenset[str],
        entity_id: int | None,
    ) -> bool:
        return bool(cells) and all(
            any(
                g.actions & actions
                and fields <= g.fields
                and self.grant_covers(g, site, brand, entity_id=entity_id)
                for g in self.grants
            )
            for site, brand in cells
        )

    def holds(self, action: str) -> bool:
        """Permission for ``action`` somewhere. Never authority over a particular record."""
        found = self._holds(action)
        if found:
            self._demands.add(("holds", action))
        return found

    def holds_any(self, actions: Iterable[str]) -> bool:
        return any(self.holds(action) for action in actions)

    def require_action(self, action: str) -> None:
        if not self.holds(action):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")

    def can(
        self,
        action: str,
        *,
        site_id: int | None = None,
        brand_id: int | None = None,
        entity_id: int | None = None,
    ) -> bool:
        """Authority for ``action`` over a record with exactly these dimensions."""
        found = self._can(action, site_id, brand_id, entity_id)
        if found:
            self._demands.add(("can", action, site_id, brand_id, entity_id))
        return found

    def require(
        self,
        action: str,
        *,
        site_id: int | None = None,
        brand_id: int | None = None,
        entity_id: int | None = None,
    ) -> None:
        if not self.holds(action):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        if not self.can(action, site_id=site_id, brand_id=brand_id, entity_id=entity_id):
            raise Refusal("NOT_FOUND", "That record was not found.")

    def can_reach_site(self, action: str, site_id: int) -> bool:
        """Site identity only (routing targets, lookup context): the site, whatever brand.

        Use it for the site record itself, never for anything held at the site.
        """
        found = any(action in g.actions and self.reaches_site(g, site_id) for g in self.grants)
        if found:
            self._demands.add(("site", action, site_id))
        return found

    def can_reach_brand(self, actions: Iterable[str], brand_id: int) -> bool:
        """A brand record held at no site (a product master): any grant reaching the brand."""
        wanted = frozenset(actions)
        found = any(g.actions & wanted and self.reaches_brand(g, brand_id) for g in self.grants)
        if found:
            self._demands.add(("brand", wanted, brand_id))
        return found

    def covers_all(
        self,
        actions: Iterable[str],
        cells: Iterable[tuple[int | None, int | None]],
        fields: Iterable[str] = (),
        *,
        entity_id: int | None = None,
    ) -> bool:
        """Every ``(site, brand)`` cell is covered by one grant holding an action and every field.

        This is the subset rule for anything holding many rows: a file, an export or
        a multi-brand document. An empty cell set covers nothing.
        """
        wanted = frozenset(actions)
        grid = frozenset(cells)
        needed = frozenset(fields)
        found = self._covers_all(wanted, grid, needed, entity_id)
        if found:
            self._demands.add(("cells", wanted, grid, needed, entity_id))
        return found

    def grants_with_roles(
        self,
        action: str,
        cells: Iterable[tuple[int | None, int | None]],
        roles: Iterable[str],
    ) -> list[str]:
        """The grants of ``roles`` holding ``action`` that cover the cells; empty unless every
        cell is covered by one of them. These are the grants that justify an approval."""
        wanted = frozenset(roles)
        grid = frozenset(cells)
        found = self._grants_with_roles(action, grid, wanted)
        if found:
            self._demands.add(("roles", action, grid, wanted))
        return found

    def _grants_with_roles(
        self, action: str, cells: frozenset[tuple[int | None, int | None]], roles: frozenset[str]
    ) -> list[str]:
        used: set[str] = set()
        for site, brand in cells:
            matching = [
                g
                for g in self.grants
                if action in g.actions
                and g.role_code in roles
                and self.grant_covers(g, site, brand)
            ]
            if not matching:
                return []
            used |= {str(g.id) for g in matching}
        return sorted(used) if cells else []

    # -- store-only records ------------------------------------------------
    # Staff, locations, exceptions and brand-less exception notifications belong to a
    # store, not a brand. Any grant at the store covers them, whatever brand it is limited
    # to (Anand's ruling, 14 September 2026). A brand-only grant is at no store and covers
    # none. Use these only for such records, never for goods, documents or money.

    @staticmethod
    def _at_store(grant: GrantView) -> bool:
        return grant.scope_kind != "brand"

    def _covers_store(
        self, actions: frozenset[str], site_id: int | None, fields: frozenset[str]
    ) -> bool:
        return site_id is not None and any(
            g.actions & actions
            and fields <= g.fields
            and self._at_store(g)
            and self.reaches_site(g, site_id)
            for g in self.grants
        )

    def covers_store(
        self, actions: Iterable[str], site_id: int | None, fields: Iterable[str] = ()
    ) -> bool:
        """One grant at the store holds an action and every field, for a store-only record."""
        wanted, needed = frozenset(actions), frozenset(fields)
        if site_id is None:
            # Held at no store (an unplaced person): the ordinary rule for a site-less record.
            return self.covers_all(wanted, [(None, None)], needed)
        found = self._covers_store(wanted, site_id, needed)
        if found:
            self._demands.add(("store", wanted, site_id, needed))
        return found

    def can_at_store(self, action: str, site_id: int | None) -> bool:
        return self.covers_store({action}, site_id)

    def require_at_store(self, action: str, site_id: int | None) -> None:
        if not self.holds(action):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        if not self.can_at_store(action, site_id):
            raise Refusal("NOT_FOUND", "That record was not found.")

    def store_site_ids(self, action: str) -> set[int] | None:
        """Sites whose store-only records ``action`` covers; ``None`` means every site."""
        sites: set[int] = set()
        for grant in self.grants:
            if action not in grant.actions or not self._at_store(grant):
                continue
            if grant.scope_kind == "tenant":
                return None
            if grant.scope_kind == "entity" and grant.entity_id is not None:
                sites |= self._sites_of_entity(grant.entity_id)
            elif grant.scope_kind == "site" and grant.site_id is not None:
                sites.add(grant.site_id)
            elif grant.scope_kind == "sbu" and grant.sbu_site_id is not None:
                sites.add(grant.sbu_site_id)
        return sites

    def site_ids(self, action: str) -> set[int] | None:
        """Sites where ``action`` covers records that carry no brand; ``None`` means every site."""
        sites: set[int] = set()
        for grant in self.grants:
            if action not in grant.actions or self.brand_limit(grant) is not None:
                continue
            if grant.scope_kind == "tenant":
                return None
            if grant.scope_kind == "entity" and grant.entity_id is not None:
                sites |= self._sites_of_entity(grant.entity_id)
            elif grant.scope_kind == "site" and grant.site_id is not None:
                sites.add(grant.site_id)
            elif grant.scope_kind == "sbu" and grant.sbu_site_id is not None:
                sites.add(grant.sbu_site_id)
        return sites

    def site_reach(self, action: str) -> set[int] | None:
        """Sites any grant holding ``action`` reaches, whatever brand it is limited to.

        A prefilter only: the caller must still check each row's brand grant by grant.
        """
        sites: set[int] = set()
        for grant in self.grants:
            if action not in grant.actions:
                continue
            if grant.scope_kind in ("tenant", "brand"):
                return None
            if grant.scope_kind == "entity" and grant.entity_id is not None:
                sites |= self._sites_of_entity(grant.entity_id)
            elif grant.scope_kind == "site" and grant.site_id is not None:
                sites.add(grant.site_id)
            elif grant.scope_kind == "sbu" and grant.sbu_site_id is not None:
                sites.add(grant.sbu_site_id)
        return sites

    def site_filter(self, action: str, field_name: str = "site_id") -> Q:
        sites = self.site_ids(action)
        if sites is None:
            return Q()
        return Q(**{f"{field_name}__in": sorted(sites)})

    def field_grants(
        self,
        *,
        site_id: int | None = None,
        brand_id: int | None = None,
        entity_id: int | None = None,
        actions: Iterable[str] | None = None,
        store_record: bool = False,
    ) -> set[str]:
        """Fields granted over this record, by grants that cover it (and hold ``actions``)."""
        wanted = frozenset(actions) if actions is not None else None
        fields: set[str] = set()
        for grant in self.grants:
            if wanted is not None and not grant.actions & wanted:
                continue
            if store_record:
                covered = self._at_store(grant) and self.reaches_site(grant, site_id)
            else:
                covered = self.grant_covers(grant, site_id, brand_id, entity_id=entity_id)
            if covered:
                fields |= grant.fields
        return fields

    def all_actions(self) -> set[str]:
        out: set[str] = set()
        for grant in self.grants:
            out |= grant.actions
        return out

    # -- step-up ---------------------------------------------------------
    def step_up_until(self, now: datetime | None = None) -> datetime | None:
        return step_up_valid_until(self.session, now or database_now())

    def require_step_up(self, now: datetime | None = None) -> None:
        if self.step_up_until(now) is None:
            raise Refusal("STEP_UP_REQUIRED", "Confirm your password to continue.")
        self._demands.add(("step_up",))

    # -- commit-time revalidation ----------------------------------------
    def revalidate(self, run: CommandRun, final: bool = False) -> None:
        """Re-check this person's authority inside the command, by database time.

        The first pass share-locks the person's security guard at rank SECURITY, so a
        revocation, role change or retirement (each bumps that row) either commits
        before this command reads it or waits for this command to commit. It then
        reloads the session, person and grants and replays every check the request
        relied on. The final pass runs after the business effects, just before the
        outcome is written, so anything that expired meanwhile still stops the write.
        """
        if final:
            now = self._expire_in_place()
        else:
            run.claim_rank(LockRank.SECURITY)
            now = self._reload(lock=True)
        for demand in sorted(self._demands, key=repr):
            self._replay(demand, now)
        valid = [str(g.id) for g in self.grants]
        named = run.authority.get("role_grant_ids")
        if final and isinstance(named, list):
            # The grants the command relied on (all of them, or the narrower set that
            # justified a decision), less any that ended meanwhile.
            run.authority["role_grant_ids"] = [g for g in named if g in set(valid)]
        else:
            run.authority["role_grant_ids"] = valid
        step_up_at = getattr(self.session, "step_up_at", None)
        run.authority["step_up_at"] = step_up_at.isoformat() if step_up_at is not None else None

    def _expire_in_place(self) -> datetime:
        """The final pass judges time alone, against what the first pass loaded.

        This command's own writes - an access change that bumps the person's epoch or
        clears their step-up - never withdraw the authority it is running under, and
        every other writer of that state is held off by the first pass's share lock.
        What can still end meanwhile is time: the session, a grant or the step-up.
        """
        from accounts.sessions import IDLE_LIFE

        now = database_now()
        if self.session.expires_at <= now or self.session.last_seen_at <= now - IDLE_LIFE:
            raise Refusal("SESSION_EXPIRED", "Your session has ended. Sign in again.")
        self.grants = [g for g in self.grants if g.ends_at is None or g.ends_at > now]
        return now

    def refresh(self) -> bool:
        """Reload session and grants by database time for a long-lived read (the SSE stream).

        ``False`` once the session or the person's access has ended; the caller stops.
        """
        try:
            self._reload(lock=False)
        except Refusal:
            return False
        return True

    def _reload(self, *, lock: bool) -> datetime:
        """The session, person and grants as they stand now; refuses if access has ended."""
        from accounts.goods_models import ServerSession
        from accounts.sessions import IDLE_LIFE, person_retired

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT epoch FROM accounts_securityguard WHERE human_id = %s"
                + (" FOR SHARE" if lock else ""),
                [self.human_id],
            )
            guard = cursor.fetchone()
        now = database_now()
        row = (
            ServerSession.objects.filter(pk=self.session.pk)
            .values(
                "revoked_at",
                "expires_at",
                "last_seen_at",
                "security_epoch",
                "step_up_at",
                "user__is_active",
                "user__human__active",
            )
            .first()
        )
        if (
            row is None
            or guard is None
            or row["revoked_at"] is not None
            or int(guard[0]) != int(row["security_epoch"])
            or not row["user__is_active"]
            or not row["user__human__active"]
            or person_retired(self.human_id, now)
        ):
            raise Refusal("AUTH_REQUIRED", "Your access changed. Sign in again.")
        if row["expires_at"] <= now or row["last_seen_at"] <= now - IDLE_LIFE:
            raise Refusal("SESSION_EXPIRED", "Your session has ended. Sign in again.")
        self.session.step_up_at = row["step_up_at"]
        self.session.expires_at = row["expires_at"]
        self.session.last_seen_at = row["last_seen_at"]
        self.grants = effective_grants(self.human_id, now)
        return now

    def _allows(self, demand: Demand) -> bool:
        kind = demand[0]
        if kind == "can":
            _kind, action, site_id, brand_id, entity_id = demand
            return self._can(action, site_id, brand_id, entity_id)
        if kind == "site":
            return any(
                demand[1] in g.actions and self.reaches_site(g, demand[2]) for g in self.grants
            )
        if kind == "brand":
            return any(
                g.actions & demand[1] and self.reaches_brand(g, demand[2]) for g in self.grants
            )
        if kind == "store":
            _kind, actions, site_id, fields = demand
            return self._covers_store(actions, site_id, fields)
        if kind == "roles":
            _kind, action, cells, roles = demand
            return bool(self._grants_with_roles(action, cells, roles))
        if kind == "cells":
            _kind, actions, cells, fields, entity_id = demand
            return self._covers_all(actions, cells, fields, entity_id)
        return True

    def _replay(self, demand: Demand, now: datetime) -> None:
        kind = demand[0]
        if kind == "step_up":
            if step_up_valid_until(self.session, now) is None:
                raise Refusal("STEP_UP_REQUIRED", "Confirm your password to continue.")
            return
        action_names = demand[1] if kind in ("cells", "brand", "store") else {demand[1]}
        if not any(self._holds(action) for action in action_names):
            raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
        allowed = self._allows(demand)
        if not allowed:
            raise Refusal("NOT_FOUND", "That record was not found.")


def effective_grants(human_id: uuid.UUID, now: datetime | None = None) -> list[GrantView]:
    from accounts.goods_models import RoleGrant
    from masters.goods_models import EffectiveVersionPeriod

    moment = now or database_now()
    rows = list(
        RoleGrant.objects.select_related("role", "sbu")
        .filter(human_id=human_id, effective_from__lte=moment, revokes__isnull=True)
        .filter(role__is_active=True)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=moment))
    )
    if not rows:
        return []
    maxima = configured_role_access(row.role.code for row in rows)
    revocations: dict[uuid.UUID, datetime] = {}
    for target, starts in RoleGrant.objects.filter(revokes__in=[r.pk for r in rows]).values_list(
        "revokes_id", "effective_from"
    ):
        if target not in revocations or starts < revocations[target]:
            revocations[target] = starts
    closed = {
        period.target_id: period
        for period in EffectiveVersionPeriod.objects.filter(
            target_kind="grant", target_id__in=[r.pk for r in rows]
        )
    }
    grants: list[GrantView] = []
    for row in rows:
        revoked_at = revocations.get(row.pk)
        if revoked_at is not None and revoked_at <= moment:
            continue
        if row.sbu is not None and row.sbu.retired_at is not None and row.sbu.retired_at <= moment:
            continue  # a retired SBU scopes nothing (its retirement also ends the grant)
        period = closed.get(row.pk)
        if period is not None and period.effective_to is not None and period.effective_to <= moment:
            continue
        ends = [
            end
            for end in (
                row.effective_to,
                period.effective_to if period is not None else None,
                revoked_at,
            )
            if end is not None
        ]
        actions = row.action_set.get("actions", []) if isinstance(row.action_set, dict) else []
        fields = row.field_set if isinstance(row.field_set, list) else []
        maximum = maxima.get(row.role.code)
        if maximum is not None:
            # An administrator narrowed this role (E080): grants never exceed it.
            if maximum.scope_kinds and row.scope_kind not in maximum.scope_kinds:
                continue
            actions = [a for a in actions if a in maximum.actions]
            fields = [f for f in fields if f in maximum.fields]
        grants.append(
            GrantView(
                id=row.pk,
                role_code=row.role.code,
                scope_kind=row.scope_kind,
                entity_id=row.entity_id,
                site_id=row.site_id,
                sbu_id=row.sbu_id,
                sbu_site_id=row.sbu.site_id if row.sbu is not None else None,
                sbu_brand_id=row.sbu.brand_id if row.sbu is not None else None,
                brand_id=row.brand_id,
                actions=frozenset(actions),
                fields=frozenset(fields),
                ends_at=min(ends) if ends else None,
                narrowed=bool(isinstance(row.action_set, dict) and row.action_set.get("narrowed")),
                seeded=bool(isinstance(row.action_set, dict) and row.action_set.get("seeded")),
            )
        )
    return grants


@dataclass(frozen=True)
class RoleMaximum:
    """What an administrator narrowed one role to (E080); never wider than its template."""

    actions: frozenset[str]
    fields: frozenset[str]
    scope_kinds: frozenset[str]


def configured_role_access(role_codes: Iterable[str]) -> dict[str, RoleMaximum]:
    """The latest administrator-set maximum per role code; a role absent here uses its template."""
    from masters.goods_models import MasterVersion

    codes = sorted(set(role_codes))
    if not codes:
        return {}
    found: dict[str, RoleMaximum] = {}
    seen: set[str] = set()
    rows = (
        MasterVersion.objects.filter(kind="role", target_key__in=codes)
        .order_by("target_key", "-revision")
        .values_list("target_key", "payload")
    )
    for key, payload in rows:
        if key in seen:
            continue
        seen.add(key)
        block = payload.get("access") if isinstance(payload, dict) else None
        if isinstance(block, dict):
            found[key] = RoleMaximum(
                actions=frozenset(block.get("actions") or []),
                fields=frozenset(block.get("fields") or []),
                scope_kinds=frozenset(block.get("scope_kinds") or []),
            )
    return found


def resolve_access(request: Any) -> AccessContext:
    """The authenticated person's access context, or ``AUTH_REQUIRED``."""
    session = getattr(request, "auth", None)
    user = getattr(request, "user", None)
    if (
        session is None
        or user is None
        or not getattr(user, "is_authenticated", False)
        or getattr(user, "human_id", None) is None
        or not hasattr(session, "token_hash")
    ):
        raise Refusal("AUTH_REQUIRED", "Sign in to continue.")
    cached = getattr(request, "_goods_access", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    context = AccessContext(
        user=user,
        human_id=user.human_id,
        tenant_id=user.tenant_id,
        session=session,
        grants=effective_grants(user.human_id, database_now()),
    )
    request._goods_access = context
    return context
