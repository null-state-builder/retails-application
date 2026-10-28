"""Load one functional test store (store test run, synthetic only).

Builds a store and its linked warehouse on the synthetic tenant and, given a
legacy SOH, brings it in through the product's own reviewed opening path - stage,
prepare, verify, approve, opening PT, approve, accept onto the floor - with a
different synthetic person at each step, exactly as the proof fixtures do.
Without an SOH the store is left sell-ready with no stock; its opening then comes
from its PT files at /goods/opening (``ptmapper.pt_opening_services``), which
reuse ``catalogue_config`` and ``OpeningDriver`` below. Nothing here writes stock
directly.

Test-only decisions, recorded on every record they touch:

* sizes are taken as the legacy file spells them (one vocabulary value per
  distinct text); seasons are the two a year, ``SS-YY`` and ``AW-YY``
  (``ptmapper.legacy_season``), and text naming no half-year is the deliberate
  unknown historical season;
* HSN comes from a keyword map over the legacy category (the file has no HSN);
* the "physical verification" is the legacy quantity, not a count;
* rows with no positive cost/MRP, or cost above MRP, are excluded with a reason.

Refuses unless the tenant is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.utils import timezone

from accounts.goods_demo import (
    CATALOGUE_IDENTITY_PROFILE,
    CATALOGUE_SIZES,
    SEED_REASON,
    SYNTHETIC_DOMAIN,
    SYNTHETIC_PASSWORD,
    PersonaSpec,
    SiteSpec,
    _build_sites,
    _ensure_ops_series,
    _ensure_working_locations,
    _open_ops_site,
    _owner_human_id,
    owner_user,
    _publish_site_master_records,
    _publish_tenant_config as _publish_config,
    seed_goods_personas,
    seed_ops_selling,
    seed_working_calendar,
)
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal
from ptmapper.legacy_season import normalize_season, season_name, season_order

#: Keyword → HSN for the test tenant only (the legacy SOH carries no HSN).
HSN_RULES: tuple[tuple[str, str], ...] = (
    ("SAREE", "5407"),
    ("ACCE", "6217"),
    ("INNER", "6108"),
    ("NIGH", "6108"),
    ("SPORT", "6211"),
    ("WINTER", "6201"),
    ("BLZ", "6203"),
    ("SUT", "6203"),
    ("KUR", "6204"),
    ("TOP", "6206"),
)
DEFAULT_HSN = "6211"
#: Fixed so a re-run finds the same configuration instead of publishing a new one.
CONFIG_FROM = "2024-04-01T00:00:00+05:30"
TEST_NOTE = "Store test run: loaded from the legacy SOH on a synthetic tenant, not a reviewed real opening."
PT_NOTE = "Store test run: opened from the store's PT files on a synthetic tenant (cost = P RATE), not a reviewed real opening."
SOH_REASON = "Store test run: legacy SOH quantity, not a physical count"
PT_REASON = "Store test run: PT file quantity, not a physical count"


#: On the ``pt.approve.opening`` policy: a test store's opening needs no second person
#: (``ptmapper.opening_auto`` reads it; a setup file's ``opening.approval: false`` sets it).
OPENING_AUTO_FLAG = "auto_approve_test_store_opening"

#: Roles a setup file may name; scopes are "store", "warehouse", a list of both, or "all".
SETUP_ROLES = frozenset({"owner", "store_person", "warehouse", "it_admin", "accounts", "brand_manager",
                         "warehouse_manager", "operations_head"})
SITE_SCOPES = frozenset({"store", "warehouse"})


@dataclass(frozen=True)
class Person:
    """One named login from a setup file (``--setup``)."""

    email: str
    name: str
    staff_code: str
    #: (role code, "all" or a tuple of "store"/"warehouse")
    roles: tuple[tuple[str, str | tuple[str, ...]], ...]
    label: str = ""
    chip: bool = True
    #: Placed at the store and listed on its till as someone who sells.
    salesperson: bool = False


@dataclass(frozen=True)
class Plan:
    store_code: str
    store_name: str
    warehouse_code: str
    warehouse_name: str
    entity_code: str
    entity_name: str
    pan: str
    state_code: str
    state_name: str
    city: str
    prefix: str  # login prefix, e.g. "vas"
    #: Named people from a setup file; empty means the ``{prefix}.store/cashier/...`` set.
    people: tuple[Person, ...] = ()
    #: False: a warehouse → store transfer needs no second person (``approval_policies``); None keeps what is in force.
    warehouse_to_store_approval: bool | None = None
    #: False: a test store's opening (source, manifests, opening PTs) needs no second person
    #: (``approval_policies(auto_opening=True)``); None keeps what is in force.
    opening_approval: bool | None = None


def plan_from_setup(path: Path) -> Plan:
    """Read a store setup file (see ``docs/wardrobe-ratu-road/setup.json``)."""
    data = json.loads(path.read_text())
    entity, store, warehouse = data["entity"], data["store"], data["warehouse"]
    people: list[Person] = []
    emails: set[str] = set()
    for row in data["people"]:
        email = str(row["email"]).strip().lower()
        if email in emails:
            raise Refusal("INVALID_REQUEST", f"{email} is listed twice.")
        emails.add(email)
        roles: list[tuple[str, str | tuple[str, ...]]] = []
        for grant in row["roles"]:
            role, sites = str(grant["role"]), grant["sites"]
            if role not in SETUP_ROLES:
                raise Refusal("INVALID_REQUEST", f"{email}: unknown role {role}.")
            scope: str | tuple[str, ...] = "all" if sites == "all" else tuple([sites] if isinstance(sites, str) else sites)
            if scope != "all" and (not scope or set(scope) - SITE_SCOPES):
                raise Refusal("INVALID_REQUEST", f"{email}: sites must be store, warehouse or all.")
            roles.append((role, scope))
        people.append(Person(email=email, name=str(row["name"]), staff_code=str(row["staff_code"]),
                             roles=tuple(roles), label=str(row.get("label") or row["name"]),
                             chip=bool(row.get("chip", True)), salesperson=bool(row.get("salesperson", False))))
    if not any(role == "store_person" and scope != "all" and "store" in scope
               for person in people for role, scope in person.roles):
        raise Refusal("INVALID_REQUEST", "Name at least one store person at the store.")
    if not any(_is_preparer(person.email) for person in people):
        raise Refusal("INVALID_REQUEST", "Name an opening preparer login (an email whose name ends in 'preparer').")
    opening = data.get("opening") or {}
    if "approval" in opening and not isinstance(opening["approval"], bool):
        raise Refusal("INVALID_REQUEST", "opening.approval must be true or false.")
    return Plan(store_code=store["code"], store_name=store["name"], warehouse_code=warehouse["code"],
                warehouse_name=warehouse["name"], entity_code=entity["code"], entity_name=entity["name"],
                pan=entity["pan"], state_code=entity["state_code"], state_name=entity["state_name"],
                city=entity["city"], prefix=store["code"].lower(), people=tuple(people),
                warehouse_to_store_approval=bool((data.get("transfers") or {}).get("warehouse_to_store_approval", True)),
                opening_approval=opening.get("approval"))


def _is_preparer(email: str) -> bool:
    """The opening preparer login: its local part ends in ``preparer`` (``vas.preparer@``, ``preparer@``)."""
    return email.split("@", 1)[0].lower().endswith("preparer")


def _publish_tenant_config(tenant: Any, approver: Any, *, kind: str, payload: dict[str, Any], label: str) -> Any:
    """Publish once; on a re-run return the version already in force with this payload."""
    from core.commands import database_now
    from masters.goods_config import in_force
    from masters.goods_models import ConfigVersion

    from core.canonical import content_hash

    # The label keys the seed command: a changed payload is a new decision.
    version = _publish_config(tenant, approver, kind=kind, payload=payload, label=f"{label}-{content_hash(payload)[:12]}")
    if version is not None:
        return version

    def same(row: Any) -> bool:
        current = row.payload if isinstance(row.payload, dict) else {}
        return all(current.get(k) == v for k, v in payload.items() if k != "effective_from")

    found = [row for row in ConfigVersion.objects.filter(tenant_id=tenant.pk, kind=kind).order_by("-version")
             if in_force(row, database_now()) and same(row)]
    if not found:
        raise Refusal("STATE_CONFLICT", f"The test {kind} configuration ({label}) is not in force.")
    return found[0]


def _note_workbook(note: str) -> bytes:
    import io

    from openpyxl import Workbook

    book = Workbook()
    assert book.active is not None
    book.active.append(["Store test run evidence", note])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def _hsn(category: str) -> str:
    text = category.upper()
    return next((hsn for key, hsn in HSN_RULES if key in text), DEFAULT_HSN)


def _key(text: str, prefix: str) -> str:
    clean = re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")
    return clean[:60] if clean else f"{prefix}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"


def _slug(text: str, limit: int) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"
    return clean[:limit] if len(clean) <= limit else f"{clean[:limit - 9]}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"


def access_for(email: str) -> Any:
    """A fresh, just-stepped-up session for a synthetic login (approvals need a recent step-up)."""
    from accounts.goods_models import SecurityGuard, ServerSession
    from accounts.models import User
    from accounts.principal import resolve_access

    user = User.objects.get(email__iexact=email)
    now = timezone.now()
    guard, _ = SecurityGuard.objects.get_or_create(tenant=user.tenant, human=user.human, defaults={"epoch": 1})
    session = ServerSession.objects.create(
        tenant=user.tenant, user=user, token_hash=uuid.uuid4().hex * 2, csrf_hash=uuid.uuid4().hex * 2,
        issued_at=now, last_seen_at=now, expires_at=now + timedelta(hours=2), security_epoch=guard.epoch,
        step_up_at=now)
    from types import SimpleNamespace
    return resolve_access(SimpleNamespace(user=user, auth=session))


def act(access: Any, action: str, handler: Callable[..., Any]) -> Any:
    from core.commands import CommandResult as Result
    holder: dict[str, Any] = {}

    def run(cmd: CommandRun) -> Result:
        holder["v"] = handler(cmd)
        return Result(resource_type="test_store", resource_id=str(getattr(holder["v"], "pk", "x")))

    execute_command(access.principal(), CommandSpec(action=action, command_id=uuid.uuid4(), business_input={}), run)
    return holder.get("v")


def _in_force_payload(tenant_id: Any, kind: str, dimension: str | None = None) -> dict[str, Any]:
    """The latest tenant-wide ``kind`` payload in force, so a second store adds to it instead of replacing it."""
    from core.commands import database_now
    from masters.goods_config import effective_at

    versions = [v for v in effective_at(tenant_id, kind, database_now())
                if isinstance(v.payload, dict) and (dimension is None or v.payload.get("dimension") == dimension)]
    return dict(max(versions, key=lambda v: v.version).payload) if versions else {}


def catalogue_config(tenant: Any, owner_id: Any, sites: list[Any], rows: list[Any], *, cost: str,
                     log: Callable[[str], None]) -> dict[str, Any]:
    """Brands, seasons, sizes, HSN tax rules and the opening profile for ``rows``; the prepare configuration.

    ``cost="soh"``: a legacy SOH - HSN from the category keyword map, Rate as basic.
    ``cost="pt"``: KDPS PT files - each row's own HSN, P RATE as cost (owner decision, 28 Oct 2026),
    and the rows the PT reader marked with a ``problem`` left out with that reason.
    """
    from masters.goods_identity_services import vocabulary_value_id
    from masters.goods_services import ensure_site_sbus
    from masters.models import Brand, Season

    stocked = [r for r in rows if r.quantity > 0]
    # Sizes: what is in force (other stores' sizes stay), plus every spelling in this file.
    values = list(_in_force_payload(tenant.pk, "vocabulary", "size").get("values") or CATALOGUE_SIZES)
    known = {v["value_key"] for v in values}
    size_map: dict[str, str] = {}
    for raw in sorted({str(r.source.get("size") or "").strip() for r in stocked} - {""}):
        key = _key(raw, "SZ")
        if key not in known:
            values.append({"value_key": key, "label": raw[:160], "sort_order": 100 + len(values), "retired": False})
            known.add(key)
        size_map[raw] = str(vocabulary_value_id("size", key))
    _publish_tenant_config(tenant, owner_id, kind="vocabulary",
                           payload={"dimension": "size", "values": values, "effective_from": CONFIG_FROM},
                           label=f"test-store-size-{len(values)}")
    identity = _publish_tenant_config(tenant, owner_id, kind="identity_profile",
                                      payload=CATALOGUE_IDENTITY_PROFILE, label="fashion-identity-profile")
    problems = {r.barcode: str(r.source.get("problem")) for r in stocked if r.source.get("problem")}
    if cost == "pt":
        row_hsn = {r.barcode: str(r.source.get("hsn") or "") for r in stocked if r.barcode not in problems}
    else:
        row_hsn = {r.barcode: _hsn(str(r.source.get("category") or "")) for r in stocked}
    slabs = [{"lower_paise": "0", "upper_paise": "250000", "lower_inclusive": True, "upper_inclusive": True,
              "input_pct": "5", "output_pct": "5"},
             {"lower_paise": "250000", "upper_paise": None, "lower_inclusive": False, "upper_inclusive": False,
              "input_pct": "18", "output_pct": "18"}]
    rules = {str(rule["hsn"]): rule for rule in _in_force_payload(tenant.pk, "tax_rates").get("hsn_rules", [])}
    for hsn in set(row_hsn.values()) - set(rules):
        rules[hsn] = {"hsn": hsn, "effective_from": CONFIG_FROM, "slabs": slabs}
    tax = _publish_tenant_config(tenant, owner_id, kind="tax_rates", payload={
        "currency": "INR", "hsn_rules": [rules[h] for h in sorted(rules)]}, label="test-store-tax")
    rates = _publish_tenant_config(tenant, owner_id, kind="rates",
                                   payload={"transport_pct": "0", "pricing_margin_pct": "0"}, label="test-store-rates")
    profile = _publish_tenant_config(tenant, owner_id, kind="profile", payload={
        "family": "fashion", "columns": [], "directions": ["both_supplied"], "allow_row_override": True,
        "rates_version_id": str(rates.pk), "tax_version_id": str(tax.pk),
        "opening_rules": {"season_required": True, "both_supplied": True}}, label="test-store-profile")
    approval_policies(tenant, owner_id)
    # Brands: match by name, else create.
    by_name = {b.name.casefold(): b for b in Brand.objects.filter(tenant_id=tenant.pk)}
    brand_map: dict[str, str] = {}
    for raw in sorted({r.brand for r in stocked}):
        brand = by_name.get(raw.casefold())
        if brand is None:
            brand, _ = Brand.objects.get_or_create(tenant_id=tenant.pk, code=_slug(raw, 32),
                                                   defaults={"name": raw[:120] or "Unknown"})
        brand_map[raw] = str(brand.pk)
    brand_ids = sorted({int(v) for v in brand_map.values()})
    for site in sites:
        ensure_site_sbus(tenant.pk, site, brand_ids)
    # Seasons: two a year, SS-YY and AW-YY (owner decision, 28 Oct 2026), whatever the
    # legacy spelling; text naming no half-year → the deliberate unknown historical season.
    unknown = Season.objects.get(historical_unknown=True)
    season_map: dict[str, str] = {"": str(unknown.pk)}
    unknown_sources = [""]
    current = season_order(f"SS-{timezone.now().year % 100:02d}")
    for raw in sorted({str(r.source.get("season") or "").strip() for r in stocked} - {""}):
        code = normalize_season(raw)
        if code is None:
            season_map[raw] = str(unknown.pk)
            unknown_sources.append(raw)
            continue
        order = season_order(code)
        season, _ = Season.objects.get_or_create(code=code, defaults={
            "name": season_name(code), "sort_order": order,
            "status": Season.Status.OPEN if order >= current else Season.Status.CLOSED})
        season_map[raw] = str(season.pk)
    _season_versions(tenant, [int(v) for v in season_map.values()])
    overrides: dict[str, dict[str, Any]] = {}
    for r in stocked:
        if r.barcode in problems:
            overrides[r.barcode] = {"exclude_reason": problems[r.barcode]}
        elif not r.rate or not r.mrp or r.rate <= 0 or r.mrp <= 0 or r.rate > r.mrp:
            overrides[r.barcode] = {"exclude_reason": "Store test run: no positive cost/MRP, or cost above MRP, in the source."}
        elif cost == "pt":
            overrides[r.barcode] = {"hsn": row_hsn[r.barcode], "basic_paise": str(r.rate)}
    excluded = sum("exclude_reason" in o for o in overrides.values())
    note = PT_NOTE if cost == "pt" else TEST_NOTE
    log(f"Masters ready: {len(brand_map)} brands, {len(season_map)} seasons, {len(size_map)} sizes, "
        f"{len(set(row_hsn.values()))} HSN codes; {excluded} rows excluded.")
    categories = {str(r.source.get("category") or "") for r in stocked}
    return {"brand_mappings": brand_map, "season_mappings": season_map, "size_mappings": size_map,
            "hsn_mappings": {} if cost == "pt" else {c: _hsn(c) for c in categories}, "row_overrides": overrides,
            "unknown_season_sources": unknown_sources, "identity_profile_id": str(identity.pk),
            "profile_version_id": str(profile.pk), "rate_meaning": "reviewed_row_values" if cost == "pt" else "basic_ex_tax",
            "source_store_confirmed": True, "fresh_source_confirmed": True,
            "quality_note": note, "external_reconciliation_note": note}


def approval_policies(tenant: Any, owner_id: Any, *, auto_warehouse_to_store: bool | None = None,
                      auto_opening: bool | None = None) -> None:
    """The test tenant's approval routes: openings and receipts by the owner; transfers by the owner,
    a warehouse manager or an operations head (whichever roles exist).

    ``auto_warehouse_to_store`` (a setup file's ``transfers.warehouse_to_store_approval: false``) lets a
    warehouse → store transfer through on submit. ``auto_opening`` (``opening.approval: false``) marks
    ``pt.approve.opening`` with ``OPENING_AUTO_FLAG``: a test store's opening source, manifests and
    opening PTs are decided by their maker with no second person
    (``approvals.goods_services.decide_without_second_person``). ``None`` keeps what is in force, so a
    later opening load does not undo the store's decision.
    """
    from accounts.models import Role
    from outbound.transfer_authority import AUTO_FLAG

    if auto_warehouse_to_store is None:
        auto_warehouse_to_store = bool(_in_force_approval(tenant.pk, "pt.approve.transfer").get(AUTO_FLAG))
    if auto_opening is None:
        auto_opening = opening_auto_in_force(tenant.pk)
    custom = set(Role.objects.filter(tenant_id=tenant.pk, is_active=True,
                                     code__in=("warehouse_manager", "operations_head")).values_list("code", flat=True))
    transfer_roles = ["owner", *sorted(custom)]
    for action, roles in (("pt.approve.opening", ["owner"]), ("pt.approve.receipt", ["owner"]),
                          ("pt.approve.transfer", transfer_roles)):
        payload: dict[str, Any] = {
            "action": action, "roles": roles, "site_ids": [], "brand_ids": [], "require_distinct": True,
            "qty_max": 1_000_000, "value_max": "100000000000", "step_up": True, "unknown_value": "refuse"}
        if action == "pt.approve.transfer" and auto_warehouse_to_store:
            payload[AUTO_FLAG] = True
        if action == "pt.approve.opening":
            # Always explicit: republishing the plain payload would find the earlier seed of it
            # and leave the flagged version in force, so approvals could not be turned back on.
            payload[OPENING_AUTO_FLAG] = bool(auto_opening)
        _publish_tenant_config(tenant, owner_id, kind="approval", payload=payload, label=f"test-store-{action}")


def opening_auto_in_force(tenant_id: Any) -> bool:
    """Whether the ``pt.approve.opening`` policy in force lets a test store open with no second person."""
    return bool(_in_force_approval(tenant_id, "pt.approve.opening").get(OPENING_AUTO_FLAG))


def _in_force_approval(tenant_id: Any, action: str) -> dict[str, Any]:
    from core.commands import database_now
    from masters.goods_config import effective_at

    versions = [v for v in effective_at(tenant_id, "approval", database_now())
                if isinstance(v.payload, dict) and v.payload.get("action") == action]
    return dict(max(versions, key=lambda v: v.version).payload) if versions else {}


def _season_versions(tenant: Any, season_ids: list[int]) -> None:
    from core.kernel_models import SubjectRevision
    from masters.goods_models import MasterVersion
    from masters.goods_services import append_master_version, start_revision
    from masters.models import Season

    missing = [s for s in Season.objects.filter(pk__in=season_ids)
               if not MasterVersion.objects.filter(tenant_id=tenant.pk, kind="season", target_key=str(s.pk)).exists()]
    if not missing:
        return

    def handler(run: CommandRun) -> None:
        for season in missing:
            if not SubjectRevision.objects.filter(tenant_id=run.tenant_id, family="master:season", subject_key=str(season.pk)).exists():
                start_revision(run.tenant_id, "master:season", str(season.pk))
            append_master_version(run, kind="season", target_key=str(season.pk), revision=1,
                                  payload={"name": season.name, "historical_unknown": season.historical_unknown},
                                  reason_code=SEED_REASON)

    seed_command(tenant, "seasons", f"{len(missing)}:{min(s.pk for s in missing)}", handler)


def seed_command(tenant: Any, action: str, key: str, handler: Callable[[CommandRun], Any]) -> Any:
    from accounts.goods_setup import service_principal

    holder: dict[str, Any] = {}

    def run(cmd: CommandRun) -> CommandResult:
        holder["v"] = handler(cmd)
        return CommandResult(resource_type="test_store", resource_id=key[:60])

    execute_command(service_principal(tenant.pk, "seed"),
                    CommandSpec(action=f"seed.test_store_{action}",
                                command_id=uuid.uuid5(tenant.deployment_key, f"test-store:{action}:{key}"),
                                business_input={"key": key}), run)
    return holder.get("v")


class WaitingForApproval(Exception):
    """A step needs a second person's decision: approvals are on for this store's openings."""


def _source(source_id: Any) -> Any:
    from ptmapper.soh_models import SohImport

    return SohImport.objects.get(pk=source_id)


class OpeningDriver:
    """Runs one staged opening source through every reviewed step, one step per ``next_step`` call.

    ``stager`` records the file quantities and accepts onto the floor; ``preparer`` claims,
    prepares, submits, applies each batch and drafts and submits each opening PT. Each is
    a callable returning an access context. Every decision (source, each manifest, each
    opening PT) goes to ``approver`` when one is given (``load_test_store --soh``: the
    owner, a second person); without one the preparer, who made the request, asks for
    ``approvals.goods_services.decide_without_second_person``, which the store's opening
    policy allows or refuses (``WaitingForApproval``).

    Nothing is carried between steps: each call reads the source, its batches, manifests
    and PTs as they stand, so a crash or a restart resumes at the first unfinished step.
    Command handlers are given ids and re-read (and re-lock) inside, so a retried
    attempt never sees what a rolled-back one changed.
    """

    def __init__(self, tenant: Any, store: Any, *, stager: Callable[[], Any], preparer: Callable[[], Any],
                 log: Callable[[str], None], approver: Callable[[], Any] | None = None,
                 configure: Callable[[], dict[str, Any]] | None = None, note: str = PT_NOTE,
                 reason: str = PT_REASON) -> None:
        self.tenant, self.store, self.log = tenant, store, log
        self.stager, self.preparer, self.approver = stager, preparer, approver
        self.configure, self.note, self.reason = configure, note, reason

    def stage(self, data: bytes, filename: str, metadata: dict[str, Any], parsed: list[Any]) -> Any:
        from core.canonical import sha256_hex
        from files.goods_services import stage_upload
        from ptmapper import soh_services
        from ptmapper.soh_models import SohImport

        found = SohImport.objects.filter(tenant_id=self.tenant.pk, site=self.store, source_hash=sha256_hex(data)).first()
        if found is not None:
            return found
        stager = self.stager()
        evidence = stage_upload(stager.principal(), command_id=uuid.uuid4(), data=data, filename=filename,
                                kind="manifest", scope={"scope_kind": "sites", "site_ids": [self.store.pk], "brand_ids": [],
                                                        "sensitive_fields": ["cost", "financial"]},
                                expected_sha256=sha256_hex(data), contains_fields=["cost", "financial"])
        source = act(stager, soh_services.STAGE, lambda run: soh_services.create_source(
            run, site_id=self.store.pk, evidence=evidence, metadata=metadata, parsed=parsed))
        self.log(f"Source staged: {metadata['stocked_rows']} stocked rows, {metadata['quantity']} pieces.")
        return source

    def run(self, source_id: Any) -> None:
        """Every step until the source is on the floor (``WaitingForApproval`` if a decision is not ours)."""
        while self.next_step(source_id) is not None:
            pass

    def next_step(self, source_id: Any) -> str | None:
        """Do the first unfinished step and say what it was; ``None`` once every batch is on the floor."""
        from ptmapper import soh_services

        source = _source(source_id)
        if source.state == "withdrawn":
            raise Refusal("SOH_SOURCE_WITHDRAWN", "This source was withdrawn.", status=409)
        self._opening_ready()
        if source.state == "uploaded":
            return self._prepare(source)
        if source.state == "review":
            return self._verify_or_submit(source)
        if source.state == "submitted":
            request = source.approval_request
            if request is None or request.state != "pending":
                raise Refusal("STATE_CONFLICT", "The submitted source has no pending approval.", status=409)
            self._decide(request.pk, "the source")
            self.log("Source approved.")
            return "Source approved"
        if source.state not in {"approved", "applied"}:
            raise Refusal("STATE_CONFLICT", f"The source is {source.state}; it cannot be loaded.", status=409)
        included = source.rows.filter(quantity__gt=0, exclusion_reason="").count()
        total = (included + soh_services.MAX_BATCH - 1) // soh_services.MAX_BATCH
        for index in range(1, total + 1):
            label = self._batch_step(source, index, total)
            if label is not None:
                return f"Batch {index} of {total}: {label}"
        return None

    # -- the source ------------------------------------------------------------

    def _opening_ready(self) -> None:
        from masters.goods_models import SiteGuard

        guard = SiteGuard.objects.get(site=self.store)
        if not guard.opening_setup_ready:
            guard.opening_setup_ready = True
            guard.lifecycle = SiteGuard.Lifecycle.OPENING
            guard.save(update_fields=["opening_setup_ready", "lifecycle"])

    def _prepare(self, source: Any) -> str:
        import time

        from core.canonical import sha256_hex
        from core.commands import database_now
        from files.goods_services import stage_upload
        from ptmapper import soh_services

        preparer = self.preparer()
        source_id, revision = source.pk, source.revision
        if source.prepared_by_id is None:
            act(preparer, soh_services.PREPARE, lambda run: soh_services.claim_preparation(
                run, preparer, _source(source_id), revision))
            return "Preparation claimed"
        if self.configure is None:
            raise Refusal("STATE_CONFLICT", "This source has no mappings yet and none were given.", status=409)
        config = self.configure()
        note_bytes = _note_workbook(self.note)
        evidence = stage_upload(preparer.principal(), command_id=uuid.uuid4(), data=note_bytes, filename="test-load-note.xlsx",
                                kind="other", scope={"scope_kind": "sites", "site_ids": [self.store.pk], "brand_ids": [],
                                                     "sensitive_fields": ["cost", "financial"]},
                                expected_sha256=sha256_hex(note_bytes), contains_fields=["cost", "financial"])
        # After every configuration the source pins took effect, and in the past.
        cutoff = max(timezone.now(), database_now())
        while database_now() <= cutoff:
            time.sleep(0.01)
        full = {**config, "cutoff_at": cutoff.isoformat(), "valuation_evidence_id": str(evidence.pk),
                "reconciliation_evidence_id": str(evidence.pk)}
        act(preparer, soh_services.PREPARE, lambda run: soh_services.prepare(run, preparer, _source(source_id), full, revision))
        self.log("Source prepared (mappings, costs, HSN).")
        return "Mappings, costs and HSN prepared"

    def _verify_or_submit(self, source: Any) -> str:
        from ptmapper import soh_services

        source_id, revision = source.pk, source.revision
        included = source.rows.filter(quantity__gt=0, exclusion_reason="")
        unverified = list(included.filter(verification={}).order_by("ordinal")
                          .values_list("barcode", "quantity")[:soh_services.MAX_BATCH])
        if unverified:
            stager = self.stager()
            observations = [{"barcode": b, "observed_qty": q, "observed_condition": "good", "reason": self.reason}
                            for b, q in unverified]
            act(stager, soh_services.STAGE, lambda run: soh_services.verify_rows(
                run, stager, _source(source_id), observations, revision))
            done, total = included.exclude(verification={}).count(), included.count()
            self.log(f"Quantities recorded for {done} of {total} rows.")
            return f"Quantities recorded ({done} of {total} rows)"
        request = source.approval_request
        if request is not None and request.state == "rejected" and request.revision == source.revision:
            raise Refusal("SOH_SOURCE_REJECTED", "An approver rejected this source; withdraw it and upload a corrected one.",
                          status=409)
        preparer = self.preparer()
        act(preparer, soh_services.PREPARE, lambda run: soh_services.submit(run, preparer, _source(source_id), revision))
        self.log("Source submitted.")
        return "Source submitted"

    def _decide(self, request_id: Any, what: str) -> None:
        """Approve one pending request: as ``approver``, else as its maker if the policy needs no second person."""
        from importlib import import_module

        from approvals.goods_models import ApprovalRequest
        from approvals.goods_services import decide

        request = ApprovalRequest.objects.get(pk=request_id)
        action, reviewed_hash = request.requested_action, request.reviewed_hash
        if self.approver is not None:
            approver = self.approver()
            act(approver, action, lambda run: decide(run, access=approver, request_id=request_id, decision="approve",
                                                     reviewed_hash=reviewed_hash, reason_code=None))
            return
        auto = getattr(import_module("approvals.goods_services"), "decide_without_second_person", None)
        if auto is None:
            raise WaitingForApproval(f"Waiting for approval of {what}")
        preparer = self.preparer()
        try:
            act(preparer, action, lambda run: auto(run, request_id))
        except Refusal as exc:
            if exc.code == "AUTO_APPROVAL_NOT_ALLOWED":
                raise WaitingForApproval(f"Waiting for approval of {what}") from exc
            raise

    # -- one batch -------------------------------------------------------------

    def _batch_step(self, source: Any, index: int, total: int) -> str | None:
        from approvals.goods_models import ApprovalRequest
        from core.kernel_models import DocumentHead
        from ptmapper import goods_manifest_services as manifests
        from ptmapper import soh_services
        from ptmapper.goods_models import GoodsPt, OpeningManifest

        batch = source.batches.filter(batch_index=index).first()
        if batch is None:
            preparer = self.preparer()
            source_id, revision = source.pk, source.revision
            act(preparer, soh_services.PREPARE, lambda run: soh_services.apply_batch(
                run, preparer, _source(source_id), index, revision))
            return "opening manifest made"
        manifest = OpeningManifest.objects.get(pk=batch.manifest_id)
        if manifest.approved_version_id is None:
            pending = ApprovalRequest.objects.filter(subject_kind="manifest", subject_key=str(manifest.pk),
                                                     state="pending").first()
            if pending is None:
                raise Refusal("STATE_CONFLICT", f"Batch {index}'s manifest is neither approved nor waiting for approval.",
                              status=409)
            self._decide(pending.pk, f"batch {index}'s opening manifest")
            return "manifest approved"
        pt = GoodsPt.objects.filter(manifest_version_id=manifest.approved_version_id).first()
        if pt is None:
            preparer = self.preparer()
            manifest_id, profile = manifest.pk, source.configuration["profile_version_id"]
            evidence_id = source.source_evidence_id
            act(preparer, "pt.prepare.opening", lambda run: manifests.create_opening_draft(
                run, manifest=OpeningManifest.objects.get(pk=manifest_id), profile_version_id=profile,
                evidence_id=evidence_id))
            return "opening PT drafted"
        head = DocumentHead.objects.get(document_id=pt.document_id)
        if head.state == DocumentHead.State.DRAFT:
            preparer = self.preparer()
            document_id, pt_id = pt.document_id, pt.pk
            act(preparer, "pt.prepare.opening", lambda run: _submit_opening(run, document_id, pt_id))
            return "opening PT submitted"
        if head.state == DocumentHead.State.SUBMITTED:
            pending = ApprovalRequest.objects.filter(subject_kind="document", subject_key=str(pt.document_id),
                                                     state="pending").first()
            if pending is None:
                raise Refusal("STATE_CONFLICT", f"Batch {index}'s opening PT is submitted with no pending approval.",
                              status=409)
            self._decide(pending.pk, f"batch {index}'s opening PT")
            return "opening PT approved"
        if head.state != DocumentHead.State.OFFICIAL or head.live_version_id is None:
            raise Refusal("STATE_CONFLICT", f"Batch {index}'s opening PT is {head.state}.", status=409)
        return self._accept(head.live_version_id, index, total)

    def _accept(self, version_id: Any, index: int, total: int) -> str | None:
        """Accept onto the floor up to one scan request of the lines nobody has scanned yet."""
        from core.kernel_models import OfficialLine
        from masters.goods_models import Location
        from stockledger import goods_acceptance
        from stockledger.goods_models import AcceptanceEvent

        scanned = set(AcceptanceEvent.objects.filter(official_line__version_id=version_id)
                      .values_list("official_line_id", flat=True))
        lines = list(OfficialLine.objects.filter(version_id=version_id).order_by("line_no"))
        waiting = [line for line in lines if line.pk not in scanned]
        if not waiting:
            return None
        stager, store_id = self.stager(), self.store.pk
        floor = Location.objects.get(site=self.store, name="Floor")
        session, _created = act(stager, "stock.accept", lambda run: goods_acceptance.open_session(
            run, site_id=store_id, version_id=version_id))
        session_id, session_revision = session.pk, session.revision
        chunk = waiting[:goods_acceptance.MAX_OBSERVATIONS]
        scans: list[dict[str, Any]] = []
        for line in chunk:
            payload = line.payload
            mrp = payload.get("mrp_paise") or (payload.get("supplied") or {}).get("mrp_paise")
            scans.append({"scan_key": str(uuid.uuid5(version_id, f"accept:{line.pk}")), "official_line_id": str(line.pk),
                          "alias_value": payload["alias_as_used"], "observed_ticket_mrp_paise": str(mrp),
                          "chosen_sku_id": str(payload["sku_id"]), "qty": int(payload["qty"]), "condition": "good",
                          "location_id": str(floor.pk), "outcome": "accepted_good", "actual_at": timezone.now().isoformat()})
        parsed = goods_acceptance.parse_scans(scans)
        act(stager, "stock.accept", lambda run: goods_acceptance.scan(run, session_id, parsed, session_revision))
        done = len(lines) - len(waiting) + len(chunk)
        if done == len(lines):
            self.log(f"Batch {index}/{total}: manifest and opening PT approved, {len(lines)} lines accepted onto the floor.")
        return f"putting stock on the floor ({done} of {len(lines)} lines)"


def _submit_opening(run: CommandRun, document_id: Any, goods_pt_id: Any) -> Any:
    """Submit a draft opening PT, re-reading its head under lock (a retry never reuses a stale head)."""
    from core.goods_documents import lock_heads
    from core.kernel_models import DocumentHead
    from ptmapper import goods_manifest_services as manifests
    from ptmapper.goods_models import GoodsPt

    head = lock_heads(run, [document_id]).get(document_id)
    if head is None or head.state != DocumentHead.State.DRAFT or head.draft_revision is None:
        raise Refusal("STATE_CONFLICT", "Only a draft opening PT can be submitted.", status=409)
    goods_pt = GoodsPt.objects.select_related("manifest_version__manifest").get(pk=goods_pt_id)
    return manifests.submit_opening(run, head, reviewed_hash=head.draft_revision.content_hash, goods_pt=goods_pt)


class Loader:
    def __init__(self, tenant: Any, plan: Plan, soh: bytes | None, log: Callable[[str], None], *,
                 password_for: Callable[[str], str] | None = None) -> None:
        self.tenant, self.plan, self.soh, self.log = tenant, plan, soh, log
        self.owner_id = _owner_human_id(tenant)
        self.password_for = password_for or (lambda _email: SYNTHETIC_PASSWORD)
        #: Logins this run created, with the password each was given.
        self.created: dict[str, str] = {}

    def seed_command(self, action: str, key: str, handler: Callable[[CommandRun], Any]) -> Any:
        return seed_command(self.tenant, action, key, handler)

    def email(self, who: str) -> str:
        """The login for a job: a setup file's named person, else ``{prefix}.{who}``."""
        if self.plan.people:
            if who == "preparer":
                return next(p.email for p in self.plan.people if _is_preparer(p.email))
            if who == "store":
                return next(p.email for p in self.plan.people
                            if any(r == "store_person" and s != "all" and "store" in s for r, s in p.roles))
        return f"{self.plan.prefix}.{who}@{SYNTHETIC_DOMAIN}"

    # -- steps -------------------------------------------------------------

    def sites(self) -> tuple[Any, Any]:
        p = self.plan
        specs = tuple(SiteSpec(code=code, name=name, kind=kind, entity_code=p.entity_code, entity_name=p.entity_name,
                               pan=p.pan, state_code=p.state_code, state_name=p.state_name, city=p.city)
                      for code, name, kind in ((p.warehouse_code, p.warehouse_name, "warehouse"),
                                               (p.store_code, p.store_name, "store")))
        sites = _build_sites(self.tenant, specs)
        warehouse, store = sites[p.warehouse_code], sites[p.store_code]
        for spec in specs:
            site = sites[spec.code]
            _ensure_working_locations(self.tenant.pk, site)
            _publish_site_master_records(self.tenant, spec, site)
            _ensure_ops_series(self.tenant, site)
        self._link(store, warehouse)
        self.log(f"Sites ready: {warehouse.code} (warehouse) and {store.code} (store), linked.")
        return warehouse, store

    def _link(self, store: Any, warehouse: Any) -> None:
        from core.kernel_models import SubjectRevision
        from masters.goods_services import (
            append_master_version,
            latest_master_version,
            linked_warehouse_id,
            start_revision,
        )
        from masters.goods_views import _site_fallback

        if linked_warehouse_id(store) == warehouse.pk:
            return
        latest = latest_master_version(self.tenant.pk, "site", str(store.pk))
        payload = {**_site_fallback(store), **((latest.payload if latest else None) or {}),
                   "type": "store", "linked_warehouse_id": str(warehouse.pk)}

        def handler(run: CommandRun) -> None:
            if not SubjectRevision.objects.filter(tenant_id=run.tenant_id, family="master:site", subject_key=str(store.pk)).exists():
                start_revision(run.tenant_id, "master:site", str(store.pk))
            append_master_version(run, kind="site", target_key=str(store.pk),
                                  revision=(latest.revision + 1) if latest else 1, payload=payload, reason_code=SEED_REASON)

        self.seed_command("link", f"{store.pk}:{warehouse.pk}", handler)

    def people(self, warehouse: Any, store: Any) -> None:
        if self.plan.people:
            self.named_people(warehouse, store)
            return
        from accounts import warehouse_manager
        from accounts.goods_setup import seed_role_assignment
        from accounts.models import User

        sites = {warehouse.code: warehouse, store.code: store}
        p = self.plan.prefix
        personas = (
            PersonaSpec(f"{p}.store", f"{store.name} store person", "M-STR", site_code=store.code,
                        note="receives, verifies, accepts and sells at the store"),
            PersonaSpec(f"{p}.cashier", f"{store.name} cashier", "M-CSH", site_code=store.code, note="bills at the counter"),
            PersonaSpec(f"{p}.warehouse", f"{warehouse.name} operator", "C-WHO", site_code=warehouse.code,
                        note="warehouse operator"),
        )
        seed_goods_personas(self.tenant, sites, personas)
        extra = ((f"{p}.preparer", "Opening preparer (test load)"), (f"{p}.whm1", "Warehouse manager 1"),
                 (f"{p}.whm2", "Warehouse manager 2"))
        seed_goods_personas(self.tenant, sites, tuple(
            PersonaSpec(code, name, "C-WHO", site_code=None, extra=(("C-WHO", None),)) for code, name in extra))
        now = timezone.now()
        preparer = User.objects.get(email__iexact=self.email("preparer")).human
        # Opening preparation needs warehouse at the store (PT preparer) and catalogue authority.
        seed_role_assignment(self.tenant, preparer, "warehouse", source_key=f"test-store:{p}.preparer:wh",
                             site_ids=(store.pk, warehouse.pk), all_brands=True, effective_from=now)
        # Product-master authority is checked per brand with no site, so it is tenant-wide here.
        self._tenant_wide(preparer, "it_admin", now)
        warehouse_manager.ensure_policy(self.tenant)
        for who in ("whm1", "whm2"):
            human = User.objects.get(email__iexact=self.email(who)).human
            warehouse_manager.assign(self.tenant, human, site_ids=[warehouse.pk, store.pk])
        self.log("People ready: store, cashier, warehouse, preparer and two warehouse managers.")

    def named_people(self, warehouse: Any, store: Any) -> None:
        """The setup file's people: one login each, then each listed role over its sites."""
        from accounts import operations_head, warehouse_manager
        from accounts.goods_setup import (
            create_person,
            seed_role_assignment,
            service_principal,
        )
        from accounts.models import User

        now = timezone.now()
        site_pk = {"store": store.pk, "warehouse": warehouse.pk}
        for person in self.plan.people:
            if not User.objects.filter(email__iexact=person.email).exists():
                password = self.password_for(person.email)

                def handler(run: CommandRun, person: Person = person, password: str = password) -> CommandResult:
                    human, _user = create_person(run, email=person.email, display_name=person.name,
                                                 staff_code=person.staff_code, password=password)
                    return CommandResult(resource_type="human", resource_id=str(human.pk), status_code=201)

                execute_command(service_principal(self.tenant.pk, "seed"), CommandSpec(
                    action="seed.goods_persona",
                    command_id=uuid.uuid5(self.tenant.deployment_key, f"test-store-person:{person.email}"),
                    business_input={"staff_code": person.staff_code}), handler)
                self.created[person.email] = password
            human = User.objects.get(email__iexact=person.email).human
            for role, scope in person.roles:
                every = scope == "all"
                sites = [] if every else sorted(site_pk[name] for name in scope)
                if role == warehouse_manager.ROLE_CODE:
                    warehouse_manager.assign(self.tenant, human, site_ids=sites or list(site_pk.values()))
                elif role == operations_head.ROLE_CODE:
                    operations_head.assign(self.tenant, human, site_ids=sites, all_sites=every)
                elif every:
                    self._tenant_wide(human, role, now)
                else:
                    seed_role_assignment(self.tenant, human, role, source_key=f"test-store:{person.email}:{role}",
                                         site_ids=sites, all_brands=True, effective_from=now)
            if person.salesperson:
                self._salesperson(human, store, now)
        self.log(f"People ready: {', '.join(p.name for p in self.plan.people)}.")

    def _salesperson(self, human: Any, store: Any, now: Any) -> None:
        """Mark the person's staff record as a salesperson placed at ``store`` (once), as People would."""
        from accounts.goods_admin_services import (
            ACTION_STAFF_UPDATE,
            NO_PLACEMENT,
            StaffFields,
            placements,
            update_staff,
        )
        from accounts.goods_models import Staff
        from accounts.goods_setup import service_principal

        staff = Staff.objects.get(tenant_id=self.tenant.pk, human=human)
        placed = placements([staff.pk]).get(staff.pk, NO_PLACEMENT).site_id == store.pk
        if staff.salesperson and placed:
            return
        present = {"salesperson"} | (set() if placed else {"site_id", "effective_from"})
        fields = StaffFields(present=frozenset(present), salesperson=True,
                             site_id=None if placed else store.pk, effective_from=None if placed else now)

        def handler(run: CommandRun) -> CommandResult:
            update_staff(run, staff_id=staff.pk, expected_revision=None, fields=fields)
            return CommandResult(resource_type="staff", resource_id=str(staff.pk))

        execute_command(service_principal(self.tenant.pk, "seed"), CommandSpec(
            action=ACTION_STAFF_UPDATE, command_id=uuid.uuid5(self.tenant.deployment_key, f"test-store-seller:{staff.pk}"),
            business_input=fields.as_json(), site_id=store.pk), handler)

    def _tenant_wide(self, human: Any, role_code: str, now: Any) -> None:
        """One open all-sites, all-brands assignment of ``role_code``; a narrower open one is closed."""
        from accounts.goods_models import RoleAssignment
        from accounts.models import Role

        role = Role.objects.get(tenant_id=self.tenant.pk, code=role_code)
        open_rows = RoleAssignment.objects.filter(tenant_id=self.tenant.pk, human_id=human.pk, role_id=role.pk,
                                                  effective_to__isnull=True, revoked_at__isnull=True)
        if any(row.all_sites and row.all_brands for row in open_rows):
            return
        open_rows.update(effective_to=now)
        RoleAssignment.objects.create(tenant_id=self.tenant.pk, human_id=human.pk, role_id=role.pk, all_sites=True,
                                      site_ids=[], all_brands=True, brand_ids=[], effective_from=now)

    def opening(self, store: Any, config: dict[str, Any], metadata: dict[str, Any], rows: list[Any]) -> None:
        assert self.soh is not None
        driver = OpeningDriver(self.tenant, store, stager=lambda: access_for(self.email("store")),
                               preparer=lambda: access_for(self.email("preparer")),
                               approver=lambda: access_for(owner_user(self.tenant).email), log=self.log,
                               configure=lambda: config, note=TEST_NOTE, reason=SOH_REASON)
        source = driver.stage(self.soh, "legacy-soh.xlsx", metadata, rows)
        driver.run(source.pk)

    def open_and_sell(self, warehouse: Any, store: Any) -> None:
        from accounts.goods_demo import OPS_TILL_COUNTER
        from masters.goods_models import SiteGuard
        from sell.models import RegisteredTill

        for site in (warehouse, store):
            spec = SiteSpec(code=site.code, name=site.name, kind=site.store_type, entity_code=self.plan.entity_code,
                            entity_name=self.plan.entity_name, pan=self.plan.pan, state_code=self.plan.state_code,
                            state_name=self.plan.state_name, city=self.plan.city)
            _open_ops_site(self.tenant, spec, site, self.owner_id)
        guard = SiteGuard.objects.get(site=store)
        if guard.lifecycle == SiteGuard.Lifecycle.OPENING:
            # Every opening batch is approved and accepted above; a historical-mode store
            # has no product step that finishes opening, so the seed records it.
            def activate(run: CommandRun) -> None:
                row = SiteGuard.objects.get(site=store)
                row.lifecycle = SiteGuard.Lifecycle.ACTIVE
                row.revision += 1
                row.save()

            self.seed_command("activate", str(store.pk), activate)
            guard.refresh_from_db()
        if not RegisteredTill.objects.filter(store=store, active=True).exists():
            RegisteredTill.objects.create(store=store, counter_id=OPS_TILL_COUNTER, active=True,
                                          registered_by=owner_user(self.tenant),
                                          series_prefix=f"{store.code}-{OPS_TILL_COUNTER}")
        # Named salespeople replace the placeholder "Counter One/Two" sellers.
        seed_ops_selling(store, counters=not any(p.salesperson for p in self.plan.people))
        self.log(f"Opened for business: {warehouse.code} goods-ready; {store.code} goods- and sell-ready "
                 f"(lifecycle {guard.lifecycle}), counter {OPS_TILL_COUNTER} registered.")

    def run(self) -> tuple[Any, Any]:
        from core.tenancy import tenant_context
        from masters.store_features import is_real_store
        from ptmapper.soh_parser import parse_soh

        parsed = parse_soh(self.soh) if self.soh is not None else None
        with tenant_context(self.tenant.pk):
            # Deadlines need an approved working calendar; idempotent on a tenant that has one.
            seed_working_calendar(self.tenant, self.owner_id)
            warehouse, store = self.sites()
            if is_real_store(store):
                raise Refusal("TEST_TENANT_ONLY", "The test store loader runs only on a synthetic tenant.")
            self.people(warehouse, store)
            transfer, opening = self.plan.warehouse_to_store_approval, self.plan.opening_approval
            if transfer is not None or opening is not None:
                approval_policies(self.tenant, self.owner_id,
                                  auto_warehouse_to_store=None if transfer is None else not transfer,
                                  auto_opening=None if opening is None else not opening)
            if parsed is not None:
                metadata, rows = parsed
                config = catalogue_config(self.tenant, self.owner_id, [store, warehouse], rows, cost="soh", log=self.log)
                self.opening(store, config, metadata, rows)
            self.open_and_sell(warehouse, store)
        return warehouse, store
