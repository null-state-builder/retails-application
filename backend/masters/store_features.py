"""Whether a switchable feature is on at a store, and the refusal when it is not.

The one server-side question every store-operations feature asks (ST-OPS-6):

    from masters.store_features import require_feature
    require_feature(store, "split-sale")   # raises Refusal FEATURE_OFF when off

Use it to refuse *new* work only. A feature that is switched off keeps every
record it made, and a bill made while it was on stays valid (PRD §30 item 4), so
never gate the reading, printing, returning or correcting of an existing record
on the switch.

A feature is on at a store when its stored switch (or, with none stored, its
registered default) is on, **and** it is not waiting on an open gate at a real
store. The second half is checked on every read, not only when the switch is
changed, so a gated feature can never be on at a real store, whatever a row,
seed or migration says (baseline B1: a real store is one whose tenant is not
synthetic).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.refusals import Refusal
from masters.goods_models import Tenant
from masters.models import Store
from masters.store_feature_models import StoreFeatureSwitch
from masters.store_feature_registry import StoreFeature, registered_features

MANUAL = StoreFeatureSwitch.Mode.MANUAL.value
CONNECTED = StoreFeatureSwitch.Mode.CONNECTED.value


@dataclass(frozen=True)
class SwitchState:
    """One feature at one store, as the server treats it right now."""

    feature: StoreFeature
    site_id: int
    #: What the server enforces.
    enabled: bool
    #: What Admin chose (or the default, if nobody has chosen).
    chosen: bool
    mode: str
    #: The stored row's revision; 0 when the default is in force.
    revision: int
    #: Why the switch cannot be turned on here, or None.
    locked_reason: str | None


def feature(key: str) -> StoreFeature:
    """The registered feature ``key``. An unknown key is a programming error."""
    for registered in registered_features():
        if registered.key == key:
            return registered
    raise LookupError(f"no store feature is registered as {key!r}")


def gate_lock(feature_: StoreFeature, *, real: bool) -> str | None:
    """Why ``feature_`` cannot be switched on at a store, or None if it can."""
    if feature_.gate and real:
        return (
            f"Waiting for {feature_.gate}. It cannot be switched on for a real store "
            "until that gate is closed."
        )
    return None


def _real_by_tenant(tenant_ids: Iterable[Any]) -> dict[Any, bool]:
    rows = Tenant.objects.filter(pk__in=set(tenant_ids)).values_list("pk", "synthetic")
    return {pk: not synthetic for pk, synthetic in rows}


def real_stores(stores: Iterable[Store]) -> dict[int, bool]:
    """``{store id: real?}`` in one query (baseline B1)."""
    stores = list(stores)
    real = _real_by_tenant(store.tenant_id for store in stores)
    return {store.pk: real.get(store.tenant_id, True) for store in stores}


def is_real_store(store: Store) -> bool:
    """Baseline B1: a store is real unless its tenant is marked synthetic."""
    return real_stores([store])[store.pk]


def switch_states(
    stores: Iterable[Store], features: Iterable[StoreFeature] | None = None
) -> list[SwitchState]:
    """Every (store, feature) pair, stores outermost, in the order given."""
    stores = list(stores)
    chosen_features = list(registered_features() if features is None else features)
    real = _real_by_tenant(store.tenant_id for store in stores)
    rows = {
        (row.site_id, row.feature_key): row
        for row in StoreFeatureSwitch.objects.filter(
            site_id__in=[store.pk for store in stores],
            feature_key__in=[f.key for f in chosen_features],
        )
    }
    out: list[SwitchState] = []
    for store in stores:
        store_real = real.get(store.tenant_id, True)
        for f in chosen_features:
            row = rows.get((store.pk, f.key))
            chosen = row.enabled if row is not None else f.default_on
            lock = gate_lock(f, real=store_real)
            out.append(
                SwitchState(
                    feature=f,
                    site_id=store.pk,
                    enabled=chosen and lock is None,
                    chosen=chosen,
                    mode=row.mode if row is not None else MANUAL,
                    revision=row.revision if row is not None else 0,
                    locked_reason=lock,
                )
            )
    return out


def _store(store: Store | int) -> Store:
    return store if isinstance(store, Store) else Store.objects.get(pk=store)


def state_at(store: Store | int, key: str) -> SwitchState:
    return switch_states([_store(store)], [feature(key)])[0]


def is_feature_on(store: Store | int, key: str) -> bool:
    return state_at(store, key).enabled


def feature_mode(store: Store | int, key: str) -> str | None:
    """``manual`` or ``connected`` where the feature is on and has modes; else None."""
    state = state_at(store, key)
    if not (state.enabled and state.feature.has_modes):
        return None
    return state.mode


def require_feature(store: Store | int, key: str) -> None:
    """Refuse the request with ``FEATURE_OFF`` unless ``key`` is on at ``store``."""
    site = _store(store)
    state = state_at(site, key)
    if state.enabled:
        return
    raise Refusal(
        "FEATURE_OFF",
        f"{state.feature.name} is switched off for {site.name}. "
        "Admin can switch it on in Setup, Feature Switches.",
        status=403,
    )


def features_on_by_site(site_ids: Iterable[int]) -> dict[str, list[str]]:
    """``{feature key: [site id, ...]}`` for every feature on at any of ``site_ids``.

    What the app's menus read to hide a feature that is off where the person works.
    """
    stores = Store.objects.filter(pk__in=list(site_ids)).order_by("code")
    out: dict[str, list[str]] = {}
    for state in switch_states(stores):
        if state.enabled:
            out.setdefault(state.feature.key, []).append(str(state.site_id))
    return out
