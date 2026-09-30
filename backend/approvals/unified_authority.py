"""Versioned authority for retained approval documents.

The old policy/route tables remain evidence. They cannot grant a decision.
An adapter must prove the source's complete scope before a request is active.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import models

from accounts.principal import AccessContext
from approvals.goods_policy import Amounts, band_failure
from core.canonical import content_hash
from core.commands import database_now
from core.refusals import Refusal
from core.tenancy import require_tenant_id
from masters.brand_identity import identity_id
from masters.goods_config import ConfigTarget, resolve
from masters.goods_models import EffectiveVersionPeriod
from masters.models import Brand, Season, Store

ACTION = "approval.decide"
# Explicit adapters, not guesses from a model's fields or presentation label.
FAMILIES: dict[str, tuple[str, frozenset[str], str | None]] = {
    "sell.pettycashspend": ("petty_cash", frozenset({"financial"}), "amount_paise"),
    "inbound.debitnote": ("debit_note", frozenset({"cost", "financial"}), "total_paise"),
    "vendors.opentobuyask": ("open_to_buy", frozenset({"cost"}), "over_paise"),
    "vendors.booking": ("booking", frozenset({"cost"}), None),
    **{f"outbound.{model}": (kind, frozenset({"cost"}), None) for model, kind in (
        ("writeoff", "writeoff"), ("stockadjustment", "adjustment"),
        ("storetransfer", "transfer"), ("stockrequest", "stock_request"),
        ("markdamaged", "damage"), ("returntovendor", "return_to_brand"),
    )},
}


@dataclass(frozen=True)
class Source:
    kind: str
    cells: tuple[tuple[int | None, int | None], ...]
    fields: frozenset[str]
    qty: int
    value: int | None
    digest: str


def _values(row: models.Model) -> dict[str, Any]:
    # Lifecycle stamps are changed by the decision itself; every business input,
    # revision, attachment identity and human link stays in the review hash.
    stamps = {"created_at", "updated_at", "status", "docstatus", "approved_by_id",
              "authorized_by_id", "confirmed_by_id", "decided_by_id", "decided_at",
              "counts_from", "reject_reason"}
    return {field.attname: str(getattr(row, field.attname))
            for field in row._meta.concrete_fields if field.attname not in stamps}


def source(subject: Any, *, lock: bool = False) -> Source:
    tenant = require_tenant_id()
    label = subject._meta.label_lower
    if label not in FAMILIES:
        raise Refusal("APPROVAL_SOURCE_INACTIVE", "This approval family has no reviewed ownership adapter.", status=409)
    query = type(subject).objects.filter(pk=subject.pk)
    subject = (query.select_for_update() if lock else query).get()
    kind, fields, amount_field = FAMILIES[label]
    booking_digest = None
    booking_qty = 0
    sites: set[int | None] = set()
    for name in ("store", "site", "source_store", "destination_store", "requesting_store", "fulfilling_store", "destination_store"):
        site = getattr(subject, name, None)
        if site is not None:
            if site.tenant_id != tenant:
                raise Refusal("NOT_FOUND", "That approval source was not found.")
            sites.add(site.pk)
    brand = getattr(subject, "brand", None)
    if isinstance(brand, Brand) and brand.tenant_id != tenant:
        raise Refusal("NOT_FOUND", "That approval source was not found.")
    if label == "vendors.opentobuyask":
        # The header alone does not cover a multi-site booking or a changed draft.
        from vendors.goods_services import booking_content_hash, booking_head, booking_lines
        booking = subject.booking
        if (booking.tenant_id != tenant or booking.brand_id != subject.brand_id
                or booking.document.tenant_id != tenant):
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "The booking's stable ownership conflicts with this request.", status=409)
        head = booking_head(booking)
        booking_digest = booking_content_hash(booking, head)
        if booking_digest != subject.draft_hash:
            raise Refusal("APPROVAL_STALE", "The booking changed; submit its current inputs again.", status=409)
        sites.add(booking.document.site_id)
        header, booked_lines, _roots = booking_lines(booking, head)
        if (str(header.get("brand_id")) != str(subject.brand_id)
                or str(header.get("season_id")) != str(subject.season_id)
                or not Season.objects.filter(pk=subject.season_id).exists()):
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "The booking's stable brand or season conflicts with this request.", status=409)
        destinations = {int(row["destination_site_id"]) for row in [header, *booked_lines]
                        if row.get("destination_site_id") is not None}
        if Store.objects.filter(tenant_id=tenant, pk__in=destinations).count() != len(destinations):
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "The booking's complete site ownership is unresolved.", status=409)
        sites.update(destinations)
        owned_sites = {site for site in sites if site is not None}
        if Store.objects.filter(tenant_id=tenant, pk__in=owned_sites).count() != len(owned_sites):
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "The booking's complete site ownership is unresolved.", status=409)
        booking_qty = sum(int(row.get("qty") or 0) for row in booked_lines)
    if not sites:
        if getattr(subject, "tenant_id", None) != tenant:
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "The source's tenant and scope are unresolved.", status=409)
        sites.add(None)
    lines_query = getattr(subject, "lines", None)
    lines = list((lines_query.select_for_update() if lock else lines_query).all()) if lines_query is not None else []
    brands: set[int | None] = set()
    qty, value = booking_qty, 0
    unknown = False
    for line in lines:
        linked = getattr(line, "brand_ref_id", None)
        if linked is None:
            if line._meta.label_lower.startswith("outbound."):
                linked = identity_id(line, tenant)
            elif isinstance(brand, Brand):
                linked = brand.pk
        if linked is None or not Brand.objects.filter(pk=linked, tenant_id=tenant).exists():
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "A line's stable brand ownership is unresolved.", status=409)
        brands.add(linked)
        pieces = next((abs(int(getattr(line, name) or 0)) for name in ("adj_qty", "qty", "qty_planned", "booked_qty") if hasattr(line, name)), 0)
        qty += pieces
        cost = getattr(line, "unit_cost_paise", None)
        if pieces and (cost is None or int(cost) <= 0):
            unknown = True
        else:
            value += pieces * int(cost or 0)
    if not brands:
        if isinstance(brand, Brand):
            brands.add(brand.pk)
        elif label == "sell.pettycashspend":
            brands.add(None)  # Explicitly whole-site cash; no brand dimension.
        else:
            raise Refusal("APPROVAL_SOURCE_INACTIVE", "The source's stable brands are unresolved.", status=409)
    if amount_field:
        raw_value = getattr(subject, amount_field)
        amount = int(raw_value) if raw_value is not None else None
    else:
        amount = None if unknown or not lines else value
    cells = tuple(sorted(((site, b) for site in sites for b in brands), key=repr))
    digest_input = {"source": label, "id": str(subject.pk), "header": _values(subject),
                    "lines": [_values(line) for line in sorted(lines, key=lambda row: str(row.pk))],
                    "cells": cells, "qty": qty, "value": str(amount) if amount is not None else None}
    if booking_digest is not None:
        digest_input["booking_hash"] = booking_digest
    digest = content_hash(digest_input)
    return Source(kind, cells, fields, qty, amount, digest)


def policy(src: Source, *, lock: bool = False) -> Any:
    at = database_now()
    versions = []
    for site, brand in src.cells:
        versions.append(resolve(require_tenant_id(), "approval",
            ConfigTarget.of(at, site_id=site, brand_ids=[brand], purpose=f"legacy.{src.kind}"),
            match={"action": ACTION, "purpose": f"legacy.{src.kind}"},
            code="APPROVAL_POLICY_BLOCKED", path="approval_policy"))
    if len({version.pk for version in versions}) != 1:
        raise Refusal("APPROVAL_POLICY_BLOCKED", "One version must cover every source cell.", status=409)
    version = versions[0]
    if lock:
        list(EffectiveVersionPeriod.objects.select_for_update().filter(target_kind="configuration", target_id=version.pk))
        # Re-resolve after taking the activation lock; a supersession never reinterprets a pin.
        current = policy(src)
        if current.pk != version.pk:
            raise Refusal("APPROVAL_STALE", "Approval policy changed; submit again.", status=409)
    payload = version.payload
    if not payload.get("require_distinct", True) or not payload.get("roles"):
        raise Refusal("APPROVAL_POLICY_BLOCKED", "This family requires separate authorised people.", status=409)
    steps = payload.get("steps") or [{"label": "Approval", "roles": payload["roles"]}]
    if not isinstance(steps, list) or not steps or any(
        not isinstance(step, dict) or not isinstance(step.get("roles"), list)
        or not step.get("roles") for step in steps
    ):
        raise Refusal("APPROVAL_POLICY_BLOCKED", "Every approval step needs an explicit authorised role.", status=409)
    for band in [payload, *steps]:
        failure = band_failure(band, Amounts(src.qty, src.value))
        if failure:
            raise Refusal("APPROVAL_POLICY_BLOCKED", "The source exceeds the configured limit or has an unknown value.", status=422)
    return version


def pin(subject: Any, maker: Any, requester: Any) -> dict[str, Any]:
    src = source(subject, lock=True)
    version = policy(src, lock=True)
    if not getattr(maker, "human_id", None) or not getattr(requester, "human_id", None):
        raise Refusal("APPROVAL_SOURCE_INACTIVE", "Named maker and requester identities are required.", status=409)
    return {"source_hash": src.digest, "cells": [list(cell) for cell in src.cells],
            "fields": sorted(src.fields), "policy_version_id": str(version.pk),
            "maker_id": str(maker.human_id), "requester_id": str(requester.human_id),
            "steps": version.payload.get("steps") or [{"label": "Approval", "roles": version.payload["roles"]}],
            "qty": src.qty, "value_paise": str(src.value) if src.value is not None else None}


def validate(approval: Any, access: AccessContext | None = None, *, lock: bool = False) -> Source:
    evidence = approval.authority_pin or {}
    if not evidence:
        raise Refusal("APPROVAL_STALE", "This historical request has no input and policy pin; validated resubmission is required.", status=409)
    src = source(approval.subject, lock=lock)
    version = policy(src, lock=lock)
    if (src.digest != evidence.get("source_hash") or str(version.pk) != evidence.get("policy_version_id")
        or [list(cell) for cell in src.cells] != evidence.get("cells") or sorted(src.fields) != evidence.get("fields")):
        raise Refusal("APPROVAL_STALE", "Inputs, scope or policy changed; submit again.", status=409)
    if access is not None:
        steps = evidence["steps"]
        index = approval.current_step
        if index < 0 or index >= len(steps):
            raise Refusal("APPROVAL_STALE", "The approval route is invalid.", status=409)
        people = {evidence["maker_id"], evidence["requester_id"]}
        people.update(str(person) for person in approval.step_decisions.values_list("decided_by__human_id", flat=True))
        if str(access.human_id) in people:
            raise Refusal("SELF_APPROVAL", "Each step requires a different authorised person.", status=403)
        if not access.covers_all_actions({ACTION}, src.cells, src.fields, roles=steps[index]["roles"]):
            raise Refusal("ACTION_DENIED", "The policy's role, fields and full scope must be covered together.", status=403)
        if version.payload.get("step_up"):
            access.require_step_up()
    return src
