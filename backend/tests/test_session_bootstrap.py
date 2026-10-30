"""`/api/auth/bootstrap` (caching plan Phase 2): one round trip, no new authority.

Every block must be byte-for-byte what its own endpoint answers the same person
in the same unit context, and a block that endpoint would refuse must be absent.
These tests hold that per role, across stores and across tenants, and check no
cost, margin or money field ever rides along.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from accounts.bootstrap import COUNT_SOURCES, REFERENCE_BLOCKS, SUMMARY_BLOCKS
from core.tenancy import tenant_context
from tests.test_personal_till_pin import session_for
from tests.test_so03_denials import TenantWorld, _assign, _person, worlds as worlds

BOOTSTRAP = "/api/auth/bootstrap"
ALL_BLOCKS = (*REFERENCE_BLOCKS, *SUMMARY_BLOCKS)
#: Field names that would mean cost, margin or money leaked into the payload.
MONEY_FIELD = re.compile(r"cost|margin|paise|price|amount|value|rate|mrp|payable|ledger", re.I)


def _client_for(world: TenantWorld, settings: Any, role: str, **scope: Any) -> Any:
    with tenant_context(world.tenant.pk):
        user, human = _person(world, f"boot-{role}")
        _assign(world, human, role, **scope)
        client, _session, _password = session_for(user, settings)
    return client


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= _keys(item)
        return found
    return set()


def _bootstrap(client: Any, **headers: Any) -> dict[str, Any]:
    response = client.get(BOOTSTRAP, **headers)
    assert response.status_code == 200, response.content
    payload: dict[str, Any] = response.json()
    return payload


def _assert_mirrors_sources(client: Any, payload: dict[str, Any], **headers: Any) -> None:
    """Each block equals its endpoint's answer; each absent block is refused there."""
    for url in ALL_BLOCKS:
        source = client.get(f"/api{url}", **headers)
        if url in payload["blocks"]:
            assert source.status_code == 200, url
            assert payload["blocks"][url] == source.json(), url
        else:
            assert source.status_code != 200, f"{url} answered {source.status_code} but was omitted"
    for name, url in COUNT_SOURCES.items():
        source = client.get(f"/api{url}", **headers)
        if name in payload["counts"]:
            assert source.status_code == 200, url
            assert payload["counts"][name] == len(source.json()), url
        else:
            assert source.status_code != 200, url


ROLES: dict[str, dict[str, Any]] = {
    # The cashier and the store manager are both `store_person` at one store.
    "store_person": {"sites": "first", "all_brands": True},
    "warehouse": {"sites": "second", "all_brands": True},
    "brand_manager": {"all_sites": True, "brands": "first"},
    "owner": {"all_sites": True, "all_brands": True},
}


def _scope(world: TenantWorld, spec: dict[str, Any]) -> dict[str, Any]:
    scope = dict(spec)
    if scope.get("sites") == "first":
        scope["sites"] = (world.sites[0],)
    elif scope.get("sites") == "second":
        scope["sites"] = (world.sites[1],)
    if scope.get("brands") == "first":
        scope["brands"] = (world.brands[0],)
    return scope


@pytest.mark.parametrize("role", list(ROLES))
def test_shape_and_every_block_equals_its_source_endpoint(worlds: Any, settings: Any, role: str) -> None:
    world, _ = worlds
    client = _client_for(world, settings, role, **_scope(world, ROLES[role]))
    payload = _bootstrap(client)
    assert set(payload) == {"contract_version", "ref_version", "blocks", "counts"}
    assert payload["contract_version"] == "bootstrap-v1"
    assert re.fullmatch(r"[0-9a-f]{32}", payload["ref_version"])
    assert set(payload["blocks"]) <= set(ALL_BLOCKS)
    assert set(payload["counts"]) <= set(COUNT_SOURCES)
    _assert_mirrors_sources(client, payload)


def test_owner_gets_every_block_and_the_whole_network(worlds: Any, settings: Any) -> None:
    world, other = worlds
    client = _client_for(world, settings, "owner", all_sites=True, all_brands=True)
    payload = _bootstrap(client)
    assert set(payload["blocks"]) == set(ALL_BLOCKS)
    assert set(payload["counts"]) == set(COUNT_SOURCES)
    store_ids = {row["id"] for row in payload["blocks"]["/masters/stores"]}
    assert {s.pk for s in world.sites} <= store_ids
    # Tenant wall: the other tenant's stores and brands never appear.
    assert not store_ids & {s.pk for s in other.sites}
    brand_ids = {row["id"] for row in payload["blocks"]["/masters/brands"]}
    assert not brand_ids & {b.pk for b in other.brands}


def test_each_role_gets_only_its_own_rows(worlds: Any, settings: Any) -> None:
    world, _ = worlds
    payloads = {
        role: _bootstrap(_client_for(world, settings, role, **_scope(world, spec)))
        for role, spec in ROLES.items()
    }

    def ids(role: str, url: str) -> set[int]:
        return {row["id"] for row in payloads[role]["blocks"][url]}

    every_site = {s.pk for s in world.sites}
    assert ids("owner", "/masters/stores") == every_site
    # Cashier / store manager and warehouse: never a store outside their own
    # (the steward list answers `setup` view, so a store person sees none).
    assert ids("store_person", "/masters/stores") <= {world.sites[0].pk}
    assert ids("warehouse", "/masters/stores") <= {world.sites[1].pk}
    # Brand manager: their own brand only, though every site.
    assert ids("brand_manager", "/masters/brands") == {world.brands[0].pk}
    assert ids("owner", "/masters/brands") == {b.pk for b in world.brands}
    # Locations is the deliberately network-wide destination picker (#147).
    assert ids("store_person", "/masters/locations") == every_site


def test_a_lapsed_assignment_loses_the_gated_blocks(worlds: Any, settings: Any) -> None:
    from datetime import timedelta

    world, _ = worlds
    client = _client_for(
        world, settings, "owner", all_sites=True, all_brands=True,
        starts=timedelta(days=-3), ends=timedelta(days=-1),
    )
    payload = _bootstrap(client)
    # `org.site.route` and the Home section are refused at the source, so the
    # blocks and counts they gate are absent, not empty.
    for url in ("/masters/locations", "/masters/seasons", "/masters/summary"):
        assert url not in payload["blocks"]
    assert "alerts" not in payload["counts"]
    assert not payload["blocks"].get("/masters/stores")
    _assert_mirrors_sources(client, payload)


def test_store_person_never_sees_another_store(worlds: Any, settings: Any) -> None:
    world, _ = worlds
    client = _client_for(world, settings, "store_person", sites=(world.sites[0],), all_brands=True)
    payload = _bootstrap(client)
    stores = payload["blocks"].get("/masters/stores", [])
    assert {row["id"] for row in stores} <= {world.sites[0].pk}
    # Asking for the other store's context narrows nothing open: it is refused
    # at the source, so it is absent here too, and never answered with its rows.
    foreign = {"HTTP_X_KDPS_UNIT": str(world.sites[1].pk)}
    response = client.get(BOOTSTRAP, **foreign)
    if response.status_code == 200:
        crossed = response.json()
        rows = crossed["blocks"].get("/masters/stores", [])
        assert world.sites[1].pk not in {row["id"] for row in rows}
        _assert_mirrors_sources(client, crossed, **foreign)
    else:
        assert response.status_code in (401, 403, 404)


def test_unit_context_narrows_like_the_source(worlds: Any, settings: Any) -> None:
    world, _ = worlds
    client = _client_for(world, settings, "owner", all_sites=True, all_brands=True)
    unit = {"HTTP_X_KDPS_UNIT": str(world.sites[0].pk)}
    payload = _bootstrap(client, **unit)
    # Whatever the unit context does at each source, the bootstrap does the same.
    _assert_mirrors_sources(client, payload, **unit)


@pytest.mark.parametrize("role", list(ROLES))
def test_no_cost_margin_or_money_field(worlds: Any, settings: Any, role: str) -> None:
    world, _ = worlds
    payload = _bootstrap(_client_for(world, settings, role, **_scope(world, ROLES[role])))
    leaked = {key for key in _keys(payload["blocks"]) if MONEY_FIELD.search(key)}
    assert not leaked
    assert all(isinstance(n, int) for n in payload["counts"].values())


def test_ref_version_moves_when_a_master_changes(worlds: Any, settings: Any) -> None:
    world, _ = worlds
    client = _client_for(world, settings, "owner", all_sites=True, all_brands=True)
    before = _bootstrap(client)["ref_version"]
    assert _bootstrap(client)["ref_version"] == before
    with tenant_context(world.tenant.pk):
        brand = world.brands[1]
        brand.name = f"{brand.name} renamed"
        brand.save(update_fields=["name", "updated_at"])
    assert _bootstrap(client)["ref_version"] != before


def test_signed_out_and_temporary_password_are_refused(worlds: Any, settings: Any) -> None:
    from rest_framework.test import APIClient

    world, _ = worlds
    assert APIClient().get(BOOTSTRAP).status_code in (401, 403)
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "boot-temp")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        client, _session, _password = session_for(user, settings)
        user.must_change_password = True
        user.save(update_fields=["must_change_password"])
    response = client.get(BOOTSTRAP)
    assert response.status_code == 403
    assert "blocks" not in response.json()
