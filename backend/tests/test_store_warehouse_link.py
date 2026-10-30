"""Store ↔ warehouse link: validation, destination order and readiness (store-warehouse-link).

Synthetic proof only: disposable tenants and fictional sites.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.utils import timezone

from accounts.goods_models import HumanIdentity
from accounts.models import User
from core.canonical import content_hash
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.numbering import prepare_series
from core.tenancy import tenant_context
from masters.goods_config import activate, normalise_scope
from masters.goods_models import ConfigDraft, ConfigVersion, SiteGuard
from masters.goods_services import compute_readiness_checks, start_revision
from masters.goods_views import GoodsSiteDetailView
from masters.models import Gstin, LegalEntity, Store
from outbound.transfers import destinations
from tests.test_first_store_configuration import _owner, _request
from tests.test_so03_denials import TenantWorld
from tests.test_so03_denials import worlds as worlds


def _site(world: TenantWorld, code: str, name: str, kind: str, gstin: Gstin | None = None) -> Store:
    site = Store.objects.create(tenant=world.tenant, gstin=gstin or world.sites[0].gstin,
                                code=code, name=name, store_type=kind)
    start_revision(world.tenant.pk, "master:site", str(site.pk))
    return site


def _patch(user: User, site: Store, revision: int, **body: Any) -> Any:
    request, _ = _request(user, "patch", {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
                                          "expected_revision": revision, **body})
    return GoodsSiteDetailView.as_view()(request, pk=site.pk)


def _link(user: User, store: Store, warehouse: Store | None, revision: int = 1) -> Any:
    return _patch(user, store, revision, linked_warehouse_id=str(warehouse.pk) if warehouse else None)


def _refused(response: Any, code: str) -> None:
    assert response.status_code == 422, response.data
    assert response.data["code"] == "LINKED_WAREHOUSE_INVALID"
    assert [item["code"] for item in response.data["details"]["issues"]] == [code]


@pytest.fixture
def linked(worlds: tuple[TenantWorld, TenantWorld]) -> Any:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _owner(world, "link-owner")
        store = world.sites[0]
        start_revision(world.tenant.pk, "master:site", str(store.pk))
        warehouse = _site(world, "link-wh", "Zeta linked warehouse", "warehouse")
        yield world, user, human, store, warehouse


def test_store_links_to_same_entity_warehouse(linked: Any) -> None:
    world, user, _human, store, warehouse = linked
    response = _link(user, store, warehouse)
    assert response.status_code == 200, response.data
    assert response.data["data"]["linked_warehouse_id"] == str(warehouse.pk)
    cleared = _link(user, store, None, revision=2)
    assert cleared.status_code == 200 and cleared.data["data"]["linked_warehouse_id"] is None


def test_link_refuses_a_site_that_is_not_a_warehouse(linked: Any) -> None:
    world, user, _human, store, _warehouse = linked
    _refused(_link(user, store, world.sites[1]), "NOT_A_WAREHOUSE")


def test_link_refuses_a_warehouse_of_another_entity(linked: Any) -> None:
    world, user, _human, store, _warehouse = linked
    entity = LegalEntity.objects.create(tenant=world.tenant, code="ent-other", name="Other entity", pan="ABCOT1234F")
    gstin = Gstin.objects.create(tenant=world.tenant, legal_entity=entity, gstin=f"10{uuid.uuid4().int % 10**13:013d}",
                                 state_code="10", state_name="Bihar")
    foreign = _site(world, "other-wh", "Other entity warehouse", "warehouse", gstin)
    _refused(_link(user, store, foreign), "OTHER_ENTITY")


def test_link_refuses_a_warehouse_naming_a_warehouse(linked: Any) -> None:
    world, user, _human, _store, warehouse = linked
    second = _site(world, "link-wh2", "Second warehouse", "warehouse")
    _refused(_link(user, warehouse, second), "NOT_A_STORE_SITE")


def test_linked_warehouse_cannot_stop_being_a_warehouse(linked: Any) -> None:
    _world, user, _human, store, warehouse = linked
    assert _link(user, store, warehouse).status_code == 200
    _refused(_patch(user, warehouse, 1, type="store"), "WAREHOUSE_IN_USE")
    warehouse.refresh_from_db()
    assert warehouse.store_type == "warehouse"


def _moving(*sites: Store) -> None:
    for site in sites:
        SiteGuard.objects.update_or_create(site=site, defaults={
            "tenant_id": site.tenant_id, "lifecycle": SiteGuard.Lifecycle.ACTIVE,
            "stock_contract": SiteGuard.StockContract.GOODS_V1, "goods_ready": True})


def test_destinations_put_linked_sites_first_and_keep_the_rest(linked: Any) -> None:
    world, user, _human, store, warehouse = linked
    other_store = world.sites[1]
    other_warehouse = _site(world, "link-wh0", "Alpha other warehouse", "warehouse")
    _moving(store, warehouse, other_store, other_warehouse)
    unlinked = destinations(world.tenant.pk, store.pk)
    assert [row["id"] for row in unlinked][0] != str(warehouse.pk)
    assert not any(row["linked"] for row in unlinked)
    assert _link(user, store, warehouse).status_code == 200
    rows = destinations(world.tenant.pk, store.pk)
    assert rows[0] == {"id": str(warehouse.pk), "code": warehouse.code, "name": warehouse.name,
                       "kind": "warehouse", "linked": True}
    assert {row["id"] for row in rows[1:]} == {str(other_store.pk), str(other_warehouse.pk)}
    assert not any(row["linked"] for row in rows[1:])
    from_warehouse = destinations(world.tenant.pk, warehouse.pk)
    assert from_warehouse[0]["id"] == str(store.pk) and from_warehouse[0]["linked"] is True
    assert not any(row["linked"] for row in from_warehouse[1:])
    assert {row["id"] for row in from_warehouse} == {str(store.pk), str(other_store.pk), str(other_warehouse.pk)}


def _check(site: Store, key: str) -> dict[str, Any]:
    return next(row for row in compute_readiness_checks(site, timezone.now()) if row["key"] == key)


def test_readiness_requires_the_transfer_pt_series(linked: Any) -> None:
    world, _user, _human, store, _warehouse = linked
    for doc_type in ("GRN", "CGRN", "RPT"):
        prepare_series(world.tenant.pk, store.gstin.legal_entity, doc_type)
    check = _check(store, "document_series")
    assert not check["passed"] and check["reason"].endswith(": TPT.")
    prepare_series(world.tenant.pk, store.gstin.legal_entity, "TPT")
    assert _check(store, "document_series")["passed"]


def _transfer_policy(world: TenantWorld, human: HumanIdentity, scope: dict[str, Any]) -> ConfigVersion:
    payload = {"action": "pt.approve.transfer", "roles": ["owner"], "site_ids": [], "brand_ids": [],
               "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True,
               "unknown_value": "refuse"}
    canonical = normalise_scope(scope)
    holder: dict[str, ConfigVersion] = {}

    def handler(run: CommandRun) -> CommandResult:
        scope_key = content_hash(canonical)
        draft = ConfigDraft.objects.create(tenant_id=run.tenant_id, kind="approval", scope=canonical, scope_key=scope_key,
                                           payload=payload, effective_from=run.now, maker=human,
                                           state=ConfigDraft.State.APPROVED)
        version = run.record(ConfigVersion(
            draft=draft, kind="approval",
            version=ConfigVersion.objects.filter(tenant_id=run.tenant_id, kind="approval").count() + 1,
            scope=canonical, scope_key=scope_key, payload=payload, effective_from=run.now, approved_by=human,
            source_revision=draft.revision, source_hash=content_hash(payload)))
        activate(run, version, effective_to=None)
        holder["version"] = version
        return CommandResult(resource_type="configuration", resource_id=str(version.pk))

    execute_command(Principal(tenant_id=world.tenant.pk, service_code="proof.configuration"),
                    CommandSpec(action="proof.configuration", command_id=uuid.uuid4(), business_input={}), handler)
    return holder["version"]


def test_readiness_requires_one_transfer_policy_over_store_and_linked_warehouse(linked: Any) -> None:
    world, user, human, store, warehouse = linked
    # No link: nothing to prove.
    assert _check(store, "transfer_approval")["passed"]
    assert _link(user, store, warehouse).status_code == 200
    check = _check(store, "transfer_approval")
    assert not check["passed"] and check["overridable"] and "the site" in check["reason"]
    # A policy for the store alone does not cover its warehouse.
    _transfer_policy(world, human, {"scope_kind": "sites", "site_ids": [store.pk]})
    assert not _check(store, "transfer_approval")["passed"]
    _transfer_policy(world, human, {"scope_kind": "sites", "site_ids": [warehouse.pk]})
    # Two different policies, one per site, are not one shared route.
    assert not _check(store, "transfer_approval")["passed"]


def test_readiness_passes_with_a_shared_transfer_policy(linked: Any) -> None:
    world, user, human, store, warehouse = linked
    assert _link(user, store, warehouse).status_code == 200
    _transfer_policy(world, human, {"scope_kind": "sites", "site_ids": [store.pk, warehouse.pk]})
    assert _check(store, "transfer_approval")["passed"]
