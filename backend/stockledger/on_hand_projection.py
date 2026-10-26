"""Compatibility stock reads over one authoritative source per store.

Goods-v1 stores read canonical physical portions. Legacy-contract stores retain
their ledger projection. This never copies balances or writes business history.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
import uuid
from typing import Any

from accounts.principal import AccessContext, resolve_access
from core.refusals import Refusal
from masters.goods_identity_services import candidates_for
from masters.goods_models import SiteGuard
from masters.models import Store
from masters.scoping import active_brand_ids, active_store_ids, scope_by_store_and_brand
from sell.services.goods_stock import barcode_aliases
from stockledger import goods_reads as reads
from stockledger.goods_descriptions import origin_item_names
from stockledger.models import StockOnHand
from stockledger.goods_models import Origin

READ = "stock.view"
DIMENSIONS = ("brand", "design", "color", "size", "item", "season", "sku_code")


def canonical_sites(tenant_id: Any) -> set[int]:
    # Every goods-v1 site, whatever its selling mode: the legacy writer is fenced
    # there (CONTRACT_DISABLED), so its ledger projection is always empty and
    # reading it showed a stocked historical-mode store as holding nothing.
    return set(SiteGuard.objects.filter(
        tenant_id=tenant_id, stock_contract=SiteGuard.StockContract.GOODS_V1,
    ).values_list("site_id", flat=True))


def _value_granted(access: AccessContext, site_id: int, brand_id: int | None) -> bool:
    return access.covers_all({READ}, [(site_id, brand_id)], {"cost"})


def projected_rows(request: Any, *, availability: bool = False, values: bool = True,
                   store_code: str = "", describe: bool = True) -> list[dict[str, Any]]:
    """Trusted identities, quantities and optional row-scoped cost; no label grants.

    ``store_code`` skips building stores the caller filters out anyway; it only
    narrows, and ``filtered_rows`` still applies the same filter to every row.
    ``describe=False`` leaves out HSN and defers each goods row's item name to
    ``describe_items``, for a caller that names only the rows it shows.
    """
    access = resolve_access(request)
    access.require_action(READ)
    canonical = canonical_sites(access.tenant_id)
    found: list[dict[str, Any]] = []
    legacy = scope_by_store_and_brand(
        StockOnHand.objects.filter(net_qty__gt=0).exclude(store_id__in=canonical)
        .select_related("store").defer("net_value_paise"),
        request.user, "store_id", section="stock", minimum="view",
    )
    for item in legacy.order_by("store__code", "sku_code", "pk"):
        brand_id = getattr(item, "_access_brand_id", None)
        access.require(READ, site_id=item.store_id, brand_id=brand_id)
        row = {"store_id": item.store_id, "store_code": item.store.code, "store_name": item.store.name,
               "brand_id": brand_id, "sku_id": None, "record_contract": "legacy",
               **{name: getattr(item, name) for name in DIMENSIONS}, "hsn": item.hsn,
               "net_qty": item.net_qty, "skus": 1, "identity_complete": brand_id is not None}
        if values and _value_granted(access, item.store_id, brand_id):
            row["net_value_paise"] = item.net_value_paise
        found.append(row)

    selected = active_store_ids(request.user, section="stock", minimum="view")
    sites = Store.objects.filter(tenant_id=access.tenant_id, pk__in=canonical)
    if selected is not None:
        sites = sites.filter(pk__in=selected)
    if store_code:
        sites = sites.filter(code__iexact=store_code)
    selected_brands = active_brand_ids(request.user, section="stock", minimum="view")
    for site in sites.order_by("code", "pk"):
        query = reads.resolve_query(access, {"site_id": str(site.pk), "basis": "quantity"})
        # One load of the site's portions serves the quantity read and every
        # cost read below; each read still scopes and prices its own rows.
        snap = reads.load(query)
        # One cost read for the whole site when cost is granted for every brand
        # in it; each per-brand read rebuilds every row of the site, so a store
        # with hundreds of brands otherwise rebuilds its stock hundreds of
        # times. A narrower cost grant is refused here and keeps the per-brand
        # reads below. The per-row grant check still decides what is priced.
        site_costs: dict[tuple[Any, ...], dict[str, Any]] | None = None
        stock: list[dict[str, Any]] | None = None
        if values and not availability:
            try:
                valued_stock = reads.on_hand_rows(reads.resolve_query(access, {
                    "site_id": str(site.pk), "basis": "cost",
                }), snap)
            except Refusal as refusal:
                if refusal.code != "FIELD_DENIED":
                    raise
            else:
                site_costs = _unique_portions(valued_stock)
                # The basis only adds value columns: these are the quantity
                # read's rows, so the site is not built a second time.
                stock = valued_stock
        if stock is None:
            stock = reads.on_hand_rows(query, snap)
        hsn = {str(pk): value for pk, value in Origin.objects.filter(
            tenant_id=access.tenant_id, pk__in={row["origin_id"] for row in stock if row["origin_id"]},
        ).values_list("pk", "frozen_evidence__hsn")} if describe else {}
        identities = {item["sku_id"]: item for item in candidates_for(
            access.tenant_id, {row["sku_id"] for row in stock if row["sku_id"] is not None},
        )}
        item_names = origin_item_names(
            access.tenant_id, {row["origin_id"] for row in stock if row["origin_id"]},
        ) if describe else {}
        aliases = barcode_aliases(site, query.watermark)
        costs: dict[int, dict[tuple[Any, ...], dict[str, Any]]] = {}
        for item in stock:
            identity = identities.get(item["sku_id"], {})
            brand_id = identity.get("brand_id")
            if selected_brands is not None and brand_id not in selected_brands:
                continue
            if not access.can(READ, site_id=site.pk, brand_id=brand_id):
                continue
            qty = (max(item["ats_qty"], item["transferable_qty"]) if availability else item["physical_qty"])
            if qty <= 0:
                continue
            sku_id = item["sku_id"]
            barcode = str(aliases.get(uuid_value(sku_id), "")) if identity else ""
            row = {"store_id": site.pk, "store_code": site.code, "store_name": site.name,
                   "brand_id": brand_id, "sku_id": sku_id, "record_contract": "goods-v1",
                   "brand": identity.get("brand", ""), "design": identity.get("style", ""),
                   "color": identity.get("colour", ""), "size": identity.get("size", ""),
                   "item": item_names.get(str(item["origin_id"])) or identity.get("grade", ""), "season": item.get("season_label", ""),
                   "season_id": item.get("season_id"), "sku_code": barcode,
                   "net_qty": qty, "skus": 1, "identity_complete": bool(identity and barcode),
                   "ats_qty": item["ats_qty"], "transferable_qty": item["transferable_qty"],
                   "hsn": str(hsn.get(str(item.get("origin_id"))) or "") if identity else ""}
            if not describe:
                row["_describe"] = (item["origin_id"], identity.get("grade", ""))
                del row["hsn"]
            if (values and not availability and brand_id is not None
                    and _value_granted(access, site.pk, brand_id)):
                if site_costs is not None:
                    priced = site_costs
                else:
                    if brand_id not in costs:
                        costs[brand_id] = _unique_portions(reads.on_hand_rows(reads.resolve_query(access, {
                            "site_id": str(site.pk), "brand_id": str(brand_id), "basis": "cost",
                        }), snap))
                    priced = costs[brand_id]
                valued = priced.get(_portion_key(item), {}).get("cost_value_paise")
                if valued is not None:
                    row["net_value_paise"] = int(valued)
            found.append(row)
    return found


def uuid_value(value: Any) -> Any:
    return uuid.UUID(str(value)) if value is not None else None


def _portion_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in ("sku_id", "origin_id", "location_id", "condition"))


def _unique_portions(rows: list[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_portion_key(row)].append(row)
    # Older read DTOs omit the value-basis origin of adjustment portions. A
    # collision cannot be priced by picking a row. A SKU has one brand, so a
    # site-wide read collides exactly where the per-brand reads did.
    return {key: parts[0] for key, parts in grouped.items() if len(parts) == 1}


def filtered_rows(rows: list[dict[str, Any]], params: Any) -> list[dict[str, Any]]:
    for field, key in (("store_code", "store"), ("brand", "brand"), ("sku_code", "sku"), ("size", "size")):
        value = str(params.get(key) or "").strip()
        if value:
            rows = [row for row in rows if str(row[field]).casefold() == value.casefold()]
    term = str(params.get("q") or "").strip().casefold()
    if term:
        exact = [row for row in rows if str(row["sku_code"]).casefold() == term]
        rows = exact or [row for row in rows if any(term in str(row[field]).casefold()
                                                  for field in ("sku_code", "design", "brand", "item"))]
    return rows


def describe_items(tenant_id: Any, rows: list[dict[str, Any]]) -> None:
    """Fill the item name of rows ``projected_rows(describe=False)`` left unnamed."""
    pending = [row for row in rows if "_describe" in row]
    names = origin_item_names(tenant_id, {row["_describe"][0] for row in pending if row["_describe"][0]})
    for row in pending:
        origin_id, grade = row.pop("_describe")
        row["item"] = names.get(str(origin_id)) or grade


def on_hand_response(rows: list[dict[str, Any]], group: str, limit: int,
                     describe: Callable[[list[dict[str, Any]]], None] | None = None) -> dict[str, Any]:
    """Grouped lines and totals; ``describe`` names only the lines on the page."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group_key = ((row["store_id"], row["brand_id"], row["sku_id"] or row["sku_code"], row.get("season_id") or row["season"])
               if group == "sku" else (row["store_id"], row["brand_id"]) if group == "brand" else (row["store_id"],))
        groups[group_key].append(row)
    rendered = []
    firsts = []
    for parts in groups.values():
        first = parts[0]
        row = {key: first.get(key, "") for key in (*DIMENSIONS, "store_id", "store_code", "store_name",
                                                  "brand_id", "sku_id", "record_contract")}
        if group != "sku":
            for key in ("design", "color", "size", "item", "season", "sku_code"):
                row[key] = ""
            if group == "store":
                row["brand"] = ""
        row.update(net_qty=sum(part["net_qty"] for part in parts),
                   skus=len({(part["brand_id"], part["sku_id"] or part["sku_code"]) for part in parts}),
                   identity_complete=all(part["identity_complete"] for part in parts))
        if all("net_value_paise" in part for part in parts):
            row["net_value_paise"] = sum(part["net_value_paise"] for part in parts)
        rendered.append(row)
        firsts.append(first)
    order = sorted(range(len(rendered)), key=lambda i: (
        str(rendered[i]["store_code"]), str(rendered[i]["brand"]), str(rendered[i]["sku_code"]), str(rendered[i]["sku_id"])))
    rendered = [rendered[i] for i in order]
    if describe is not None and group == "sku":
        page = [firsts[i] for i in order[:limit]]
        describe(page)
        for row, first in zip(rendered[:limit], page, strict=True):
            row["item"] = first.get("item", "")
    summary = {"units_on_hand": sum(row["net_qty"] for row in rows), "lines": len(rendered),
               "displayed": min(limit, len(rendered)), "truncated": len(rendered) > limit,
               "scope": "current_access", "identity_complete": all(row["identity_complete"] for row in rows),
               "value_complete": bool(rows) and all("net_value_paise" in row for row in rows)}
    if summary["value_complete"]:
        summary["value_paise"] = sum(row["net_value_paise"] for row in rows)
    return {"group_by": group, "summary": summary, "rows": rendered[:limit]}


def require_value_request(request: Any, rows: list[dict[str, Any]]) -> None:
    if request.query_params.get("basis") in {"cost", "value", "valuation", "margin"} and (
        not rows or any("net_value_paise" not in row for row in rows)
    ):
        raise Refusal("FIELD_DENIED", "This stock request requires protected financial fields.", status=403)
