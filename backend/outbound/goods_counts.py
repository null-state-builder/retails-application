"""Non-trading stock counts: blind capture and the endings that move nothing (goods ticket 17).

Goods PRD §14.9-§14.10 (GSA-R01, GSA-R02), GSA-T17 and design E102/E103,
E159-E166, E211, P18, as aligned in design §7.3. Four rules hold throughout:

* **Only an affirmatively non-trading site is counted.** A count starts only
  where the site carries a current approved non-trading declaration, is not
  sell-ready and still shows no tills or trading history. Absence of a till
  module is not evidence: unknown trading is not "none" (goods PRD §14.1).
* **Starting freezes and snapshots in one commit.** The site guard's
  ``freeze_id`` is set to the count and every physical portion in the counted
  scope is frozen as the count's as-of book, under the site guard's lock - so a
  movement committing at the same moment either lands wholly before the
  snapshot or is refused ``UNDER_COUNT`` and leaves nothing. The freeze stops
  sales and stock movements; it does not stop damage reporting (GSA-R02), which
  still quarantines at once through the ordinary lifecycle.
* **Counting is blind and durable.** Nothing a counter can read carries a book,
  on-hand, held, reserved or valued quantity. Scans are append-only
  observations with their own actor and time. A pass is submitted only with the
  counter's affirmation that the whole assigned scope was counted, including
  empty locations (GSA-T17), bound to the exact observations and scope
  reviewed; an unfinished pass stays resumable, a pass idle for 24 hours is
  stale and needs an explicit resume, and a recount is a new pass that never
  inherits an affirmation. Unscanned known stock is zero observed.
* **This ticket's endings move nothing.** A verified zero variance closes the
  count and releases the freeze in one commit with no quantity or value
  posting; cancellation keeps every observation and releases the freeze. A
  nonzero difference stays pending, frozen and visible for ticket 17A's review
  and Owner approval (GSA-R01) - it is never "fixed" by setting book stock to a
  scan total, and it is never shown as a completed count.

Lock order: SITE (the site guard) → DOCUMENT (the count, then its passes) →
SERIES. A scan locks only its own pass, and re-reads the count after it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import connection

from alerts.goods_services import open_exception, resolve_exceptions
from core.canonical import content_hash
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    database_now,
    execute_command,
    register_integrity_refusal,
)
from core.goods_documents import new_document, record_event
from core.goods_fields import bounds
from core.kernel_models import DocumentIdentity
from core.numbering import allocate
from core.refusals import Refusal, issue
from masters.goods_models import Location, SiteCapabilityEvent, SiteGuard
from outbound.goods_models import (
    CountAffirmation,
    CountDecision,
    CountSelection,
    CountSnapshot,
    GoodsCountPass,
    GoodsStocktake,
)
from stockledger import goods_engine as engine
from stockledger.goods_models import CONDITIONS, Position

RUN_ACTION = "count.run"
REVIEW_ACTION = "count.review"
DOC_KIND = "CNT"
#: GSA-T17 / design E165 step 10: a pass nobody touched for this long is stale.
STALE_AFTER = timedelta(hours=24)
STALE_KIND = "count_session_stale"
STALE_SERVICE = "count-stale-check"
STALE_ACTION = "stock.count.stale_check"
UNFINISHED = ("requested", "open", "review")
SCOPE_KINDS = ("site", "location", "brand")
COUNT_KINDS = ("cycle", "full")
CONDITION_VALUES = tuple(value for value, _label in CONDITIONS)
MAX_OBSERVATIONS = 500
MAX_RECOUNT_LINES = 200

#: The count's document events (registered in ``core.goods_history``).
EVENT_STARTED = "count_started"
EVENT_PASS_OPENED = "count_pass_opened"
EVENT_PASS_SUBMITTED = "count_pass_submitted"
EVENT_RECOUNT = "count_recount_requested"
EVENT_STALE = "count_pass_stale"
EVENT_RESUMED = "count_pass_resumed"
EVENT_CLOSED = "count_closed"
EVENT_CANCELLED = "count_cancelled"
EVENT_KINDS = frozenset(
    {
        EVENT_STARTED,
        EVENT_PASS_OPENED,
        EVENT_PASS_SUBMITTED,
        EVENT_RECOUNT,
        EVENT_STALE,
        EVENT_RESUMED,
        EVENT_CLOSED,
        EVENT_CANCELLED,
    }
)

#: The two routes that end a stale pass's owned work: finishing it (after an
#: explicit resume) or cancelling the count. Named as the API paths the count
#: screen already calls (ticket 08's registration rule).
STALE_RESOLUTIONS = [
    "outbound/count-sessions/{id}/resume",
    "outbound/stocktakes/{id}/cancel",
]

START_FIELDS = frozenset({"site_id", "scope"})
SCOPE_FIELDS = frozenset({"kind", "location_id", "brand_id", "count_kind"})
OPEN_FIELDS = frozenset({"location_id"})
SCAN_FIELDS = frozenset({"observations"})
OBSERVATION_FIELDS = frozenset(
    {
        "scan_key",
        "location_id",
        "sku_id",
        "description",
        "alias_value",
        "condition",
        "qty",
        "correction_of_id",
        "actual_at",
    }
)
SUBMIT_FIELDS = frozenset({"reviewed_hash", "scope_complete"})
RECOUNT_FIELDS = frozenset({"line_keys", "counter_id", "reason_code"})
CLOSE_FIELDS = frozenset({"reviewed_hash", "selected_pass_ids"})
CANCEL_FIELDS = frozenset({"reason_code", "note"})


def install() -> None:
    """Name the two database guards' refusals (called from ``OutboundConfig.ready``)."""
    register_integrity_refusal(
        "uq_stocktake_one_unfinished",
        "COUNT_ALREADY_OPEN",
        "Another count is already open at this site.",
    )
    register_integrity_refusal(
        "uq_countpass_one_open",
        "COUNT_SESSION_INVALID",
        "You already have an unfinished pass in this count. Continue it first.",
    )


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _bad(message: str, field_name: str, code: str = "INVALID") -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue(code, message, field=field_name)])


def _uuid(value: Any, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise _bad(f"{field_name} must be an ID.", field_name) from None


def _optional_uuid(value: Any, field_name: str) -> uuid.UUID | None:
    return None if value in (None, "") else _uuid(value, field_name)


def _int_id(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise _bad(f"{field_name} must be an ID.", field_name)
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise _bad(f"{field_name} must be an ID.", field_name) from None
    if parsed < 1:
        raise _bad(f"{field_name} must be an ID.", field_name)
    return parsed


def _text(value: Any, field_name: str, max_len: int, *, required: bool) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _bad(f"{field_name} is required.", field_name, "REQUIRED")
        return ""
    if not isinstance(value, str) or len(value) > max_len:
        raise _bad(f"{field_name} must be text of at most {max_len} characters.", field_name)
    return value.strip()


def _moment(value: Any, field_name: str, now: datetime) -> datetime:
    if value in (None, ""):
        return now
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise _bad(f"{field_name} must be a timestamp with a time-zone offset.", field_name)
    if parsed > now + timedelta(minutes=5):
        raise Refusal("EVENT_TIME_INVALID", "A scan cannot be dated in the future.", status=422)
    return parsed


def _closed(value: Any, allowed: frozenset[str], field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _bad(f"{field_name} must be an object.", field_name)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise Refusal(
            "INVALID_REQUEST",
            f"Unknown field(s) in {field_name}: {', '.join(unknown)}.",
            issues=[
                issue("UNKNOWN_FIELD", f"{name} is not accepted", field=f"{field_name}.{name}")
                for name in unknown
            ],
        )
    return value


# ---------------------------------------------------------------------------
# Scope: which locations and which goods a count or a pass speaks for
# ---------------------------------------------------------------------------


def parse_start(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """E159's body: the site and the CountScope (design §5.3)."""
    site_id = _int_id(body.get("site_id"), "site_id")
    raw = _closed(body.get("scope"), SCOPE_FIELDS, "scope")
    kind = raw.get("kind")
    if kind not in SCOPE_KINDS:
        raise _bad(f"scope.kind must be one of {', '.join(SCOPE_KINDS)}.", "scope.kind")
    count_kind = raw.get("count_kind") or ("full" if kind == "site" else "cycle")
    if count_kind not in COUNT_KINDS:
        raise _bad("scope.count_kind must be cycle or full.", "scope.count_kind")
    location_id = _optional_uuid(raw.get("location_id"), "scope.location_id")
    brand_id = (
        None if raw.get("brand_id") in (None, "") else _int_id(raw["brand_id"], "scope.brand_id")
    )
    if kind == "location" and location_id is None:
        raise _bad("A location count names its location.", "scope.location_id", "REQUIRED")
    if kind == "brand" and brand_id is None:
        raise _bad("A brand count names its brand.", "scope.brand_id", "REQUIRED")
    if kind != "location" and location_id is not None:
        raise _bad("Only a location count names a location.", "scope.location_id")
    if kind != "brand" and brand_id is not None:
        raise _bad("Only a brand count names a brand.", "scope.brand_id")
    return site_id, {
        "kind": kind,
        "location_id": str(location_id) if location_id else None,
        "brand_id": brand_id,
        "count_kind": count_kind,
    }


def _site_locations(site_id: int) -> dict[uuid.UUID, Location]:
    return {
        row.pk: row
        for row in Location.objects.filter(site_id=site_id, retired_at__isnull=True).order_by(
            "name"
        )
    }


def _subtree(locations: dict[uuid.UUID, Location], root: uuid.UUID) -> set[uuid.UUID]:
    """The location and everything under it (``Location.parent``)."""
    children: dict[uuid.UUID | None, list[uuid.UUID]] = defaultdict(list)
    for row in locations.values():
        children[row.parent_id].append(row.pk)
    out: set[uuid.UUID] = set()
    queue = [root]
    while queue:
        current = queue.pop()
        if current in out or current not in locations:
            continue
        out.add(current)
        queue.extend(children.get(current, []))
    return out


def scope_locations(stocktake: GoodsStocktake) -> set[uuid.UUID]:
    """Every location the count's scope covers - including empty ones (GSA-T17)."""
    locations = _site_locations(stocktake.site_id)
    scope = stocktake.scope or {}
    if scope.get("kind") == "location" and scope.get("location_id"):
        return _subtree(locations, uuid.UUID(str(scope["location_id"])))
    return set(locations)


def pass_coverage(stocktake: GoodsStocktake, count_pass: GoodsCountPass) -> set[uuid.UUID]:
    """The locations a counting pass is assigned. A recount's are its cells' locations."""
    scope = count_pass.scope or {}
    counted = scope_locations(stocktake)
    if scope.get("kind") == "location" and scope.get("location_id"):
        return _subtree(_site_locations(stocktake.site_id), uuid.UUID(scope["location_id"])) & (
            counted
        )
    if scope.get("kind") == "recount":
        return {uuid.UUID(cell["location_id"]) for cell in scope.get("cells") or []}
    return counted


def _brand_skus(brand_id: int) -> set[uuid.UUID]:
    from masters.goods_identity_models import ProductSku

    return set(ProductSku.objects.filter(style__brand_id=brand_id).values_list("pk", flat=True))


def identity_key(sku_id: Any, description: str) -> str:
    """What makes two pieces "the same item" on a count: the SKU, else the description."""
    if sku_id:
        return str(sku_id)
    return "desc:" + " ".join((description or "").split()).casefold()


def line_key(stocktake_id: uuid.UUID, identity: str, location_id: Any, condition: str) -> uuid.UUID:
    """A variance line's stable target: item, location and condition in this count."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"kdps:count:{stocktake_id}|{identity}|{location_id}|{condition}"
    )


# ---------------------------------------------------------------------------
# E159: start a count - declaration, freeze and snapshot in one commit
# ---------------------------------------------------------------------------


def current_declaration(
    site: Any, guard: SiteGuard | None
) -> tuple[SiteCapabilityEvent | None, list[dict[str, Any]]]:
    """The site's current approved non-trading declaration, or why there is none.

    Current means: the latest non-trading capability event is an approval, the
    guard still says so, selling is not enabled, and nothing now known says the
    site trades (tills declared or unknown, bills, returns or held bills). The
    last check is repeated here rather than trusted from the declaration's day.
    """
    from masters.goods_services import trading_not_excluded

    problems: list[dict[str, Any]] = []
    event = (
        SiteCapabilityEvent.objects.filter(site_id=site.pk, operation="non_trading")
        .order_by("-recorded_at", "-id")
        .first()
    )
    if (
        event is None
        or event.outcome != "approved"
        or guard is None
        or not guard.non_trading_confirmed
    ):
        problems.append(
            issue(
                "NO_NON_TRADING_DECLARATION",
                "This site has no current approved non-trading declaration.",
            )
        )
    if guard is not None and guard.sell_ready:
        problems.append(issue("SELL_READY", "This site is approved to sell."))
    problems.extend(trading_not_excluded(site))
    return (event if not problems else None), problems


def _check_site(guard: SiteGuard | None) -> SiteGuard:
    if guard is None or guard.stock_contract != SiteGuard.StockContract.GOODS_V1:
        raise Refusal("CONTRACT_DISABLED", "This site does not count goods through goods-v1.")
    if not guard.goods_ready or guard.lifecycle in ("planned", "closing", "closed"):
        raise Refusal("SITE_NOT_READY", "This site is not approved to move goods.")
    return guard


def start_count(run: CommandRun, site: Any, scope: dict[str, Any]) -> GoodsStocktake:
    """Install the freeze and the as-of snapshot atomically, and open the blind count."""
    guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site.pk))
    guard = _check_site(guards[0] if guards else None)
    if guard.freeze_id:
        raise Refusal("UNDER_COUNT", "Another stock count has already frozen this site.")
    if GoodsStocktake.objects.filter(site_id=site.pk, state__in=UNFINISHED).exists():
        raise Refusal("COUNT_ALREADY_OPEN", "Another count is already open at this site.")
    event, problems = current_declaration(site, guard)
    if event is None:
        raise Refusal(
            "TRADING_NOT_EXCLUDED",
            "A count can start only at a site affirmatively declared non-trading.",
            issues=problems,
        )
    locations = _site_locations(site.pk)
    if scope["kind"] == "location" and uuid.UUID(scope["location_id"]) not in locations:
        raise Refusal("NOT_FOUND", "That location was not found at this site.")
    if scope["kind"] == "brand":
        from masters.models import Brand

        if not Brand.objects.filter(pk=scope["brand_id"]).exists():
            raise Refusal("NOT_FOUND", "That brand was not found.")
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A count is started by a named person.")

    identity, _head = new_document(
        run,
        kind=DOC_KIND,
        purpose=DocumentIdentity.Purpose.COUNT,
        entity_id=site.gstin.legal_entity_id,
        site_id=site.pk,
    )
    stocktake = GoodsStocktake.objects.create(
        tenant_id=run.tenant_id,
        document=identity,
        site_id=site.pk,
        scope=scope,
        non_trading_event=event,
        frozen_at=run.now,
        state=GoodsStocktake.State.OPEN,
        last_activity_at=run.now,
    )
    # The as-of book: every physical portion in the counted scope, frozen now,
    # under the same site lock every movement takes first.
    covered = scope_locations(stocktake)
    positions = Position.objects.filter(
        site_id=site.pk, boundary="physical", location_id__in=sorted(covered, key=str)
    )
    if scope["kind"] == "brand":
        positions = positions.filter(sku_id__in=sorted(_brand_skus(scope["brand_id"]), key=str))
    pieces = 0
    for position in positions.order_by("lot_id", "portion"):
        lower, upper = bounds(position.portion)
        pieces += upper - lower
        run.record(
            CountSnapshot(
                stocktake=stocktake,
                site_id=site.pk,
                lot_id=position.lot_id,
                portion=position.portion,
                address={
                    **engine.Address.of(position).as_json(),
                    "description": position.description,
                },
            )
        )
    guard.freeze_id = stocktake.pk
    guard.save(update_fields=["freeze_id"])
    number = allocate(run, site.gstin.legal_entity, DOC_KIND, on=run.now.date())
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_documentidentity SET official_number = %s "
            "WHERE id = %s AND official_number IS NULL",
            [number, identity.pk],
        )
    identity.official_number = number
    record_event(
        run,
        identity.pk,
        EVENT_STARTED,
        payload={
            "to_state": "open",
            "scope": scope,
            "non_trading_event_id": str(event.pk),
            "frozen_at": run.now.isoformat(),
            "details": [],
        },
    )
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = site.pk
    run.audit_after = {
        "stocktake_id": str(stocktake.pk),
        "number": number,
        "scope": scope,
        "freeze_id": str(stocktake.pk),
        "snapshot_pieces": pieces,
    }
    return stocktake


# ---------------------------------------------------------------------------
# Locks and lookups
# ---------------------------------------------------------------------------


def stocktake_of(tenant_id: uuid.UUID, stocktake_id: uuid.UUID) -> GoodsStocktake:
    row = (
        GoodsStocktake.objects.select_related("document", "site")
        .filter(tenant_id=tenant_id, pk=stocktake_id)
        .first()
    )
    if row is None:
        raise Refusal("NOT_FOUND", "That count was not found.")
    return row


def pass_of(tenant_id: uuid.UUID, pass_id: uuid.UUID) -> GoodsCountPass:
    row = (
        GoodsCountPass.objects.select_related("stocktake", "stocktake__document")
        .filter(tenant_id=tenant_id, pk=pass_id)
        .first()
    )
    if row is None:
        raise Refusal("NOT_FOUND", "That count pass was not found.")
    return row


def _lock_stocktake(run: CommandRun, stocktake_id: uuid.UUID) -> GoodsStocktake:
    rows: list[GoodsStocktake] = run.lock(
        LockRank.DOCUMENT, GoodsStocktake.objects.filter(pk=stocktake_id)
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That count was not found.")
    return rows[0]


def _lock_pass(run: CommandRun, pass_id: uuid.UUID) -> GoodsCountPass:
    rows: list[GoodsCountPass] = run.lock(
        LockRank.DOCUMENT, GoodsCountPass.objects.filter(pk=pass_id)
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That count pass was not found.")
    return rows[0]


def _require_open(stocktake: GoodsStocktake) -> None:
    if stocktake.state != GoodsStocktake.State.OPEN:
        raise Refusal(
            "COUNT_NOT_OPEN",
            f"This count is {stocktake.state}. Nothing more can be counted on it.",
        )


def is_stale(count_pass: GoodsCountPass, now: datetime) -> bool:
    """A pass idle for 24 hours, or already marked stale, needs an explicit resume."""
    if count_pass.state != GoodsCountPass.State.OPEN:
        return False
    if count_pass.stale_at is not None:
        return True
    last = count_pass.last_activity_at or count_pass.created_at
    return last is not None and now - last >= STALE_AFTER


def _bump(run: CommandRun, stocktake: GoodsStocktake) -> None:
    stocktake.revision += 1
    stocktake.last_activity_at = run.now
    stocktake.save(update_fields=["revision", "last_activity_at"])


# ---------------------------------------------------------------------------
# Observations and their hash
# ---------------------------------------------------------------------------


def pass_observations(pass_ids: Iterable[uuid.UUID]) -> list[Any]:
    from inbound.goods_models import ScanObservation

    return list(
        ScanObservation.objects.filter(stocktake_pass_id__in=list(pass_ids)).order_by(
            "recorded_at", "scan_key"
        )
    )


def _observation_input(row: Any) -> dict[str, Any]:
    return {
        "scan_key": str(row.scan_key),
        "location_id": str(row.location_id) if row.location_id else None,
        "sku_id": str(row.sku_id) if row.sku_id else None,
        "description": row.description,
        "alias_value": row.alias_value,
        "condition": row.condition,
        "qty": row.qty,
        "correction_of_id": str(row.correction_of_id) if row.correction_of_id else None,
    }


def scope_hash(count_pass: GoodsCountPass) -> str:
    return content_hash({"pass_id": str(count_pass.pk), "scope": count_pass.scope or {}})


def observation_hash(count_pass: GoodsCountPass, rows: Sequence[Any]) -> str:
    """What a counter reviews before affirming: the pass, its scope and every scan in it."""
    return content_hash(
        {
            "pass_id": str(count_pass.pk),
            "scope": count_pass.scope or {},
            "observations": sorted(
                (_observation_input(row) for row in rows), key=lambda item: item["scan_key"]
            ),
        }
    )


# ---------------------------------------------------------------------------
# E160: open or resume a pass
# ---------------------------------------------------------------------------


def parse_open(body: dict[str, Any]) -> uuid.UUID | None:
    return _optional_uuid(body.get("location_id"), "location_id")


def open_pass(
    run: CommandRun, stocktake_id: uuid.UUID, location_id: uuid.UUID | None
) -> tuple[GoodsCountPass, bool]:
    """The caller's unfinished pass of this count, or a new blind pass over its assigned scope."""
    human_id = run.principal.human_id
    if human_id is None:
        raise Refusal("ACTION_DENIED", "A count is counted by a named person.")
    stocktake = _lock_stocktake(run, stocktake_id)
    _require_open(stocktake)
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = stocktake.site_id
    existing = run.lock(
        LockRank.DOCUMENT,
        GoodsCountPass.objects.filter(stocktake=stocktake, counter_id=human_id, state="open"),
    )
    if existing:
        row: GoodsCountPass = existing[0]
        run.audit_after = {"pass_id": str(row.pk), "resumed": True}
        return row, False
    counted = scope_locations(stocktake)
    if location_id is not None and location_id not in counted:
        raise Refusal(
            "NOT_FOUND",
            "That location is not part of what this count covers.",
            issues=[issue("LOCATION_OUT_OF_SCOPE", "Not in this count", field="location_id")],
        )
    scope: dict[str, Any] = (
        {"kind": "location", "location_id": str(location_id)}
        if location_id is not None
        else {"kind": "count"}
    )
    last = (
        GoodsCountPass.objects.filter(stocktake=stocktake, counter_id=human_id)
        .order_by("-pass_no")
        .values_list("pass_no", flat=True)
        .first()
    )
    row = GoodsCountPass(
        tenant_id=run.tenant_id,
        stocktake=stocktake,
        counter_id=human_id,
        entry_user_id=human_id,
        pass_no=int(last or 0) + 1,
        scope=scope,
        last_activity_at=run.now,
    )
    row.observation_hash = observation_hash(row, [])
    row.save()
    _bump(run, stocktake)
    record_event(
        run,
        stocktake.document_id,
        EVENT_PASS_OPENED,
        payload={
            "pass_id": str(row.pk),
            "counter_id": str(human_id),
            "scope": scope,
            "details": [],
        },
    )
    run.audit_after = {"pass_id": str(row.pk), "scope": scope}
    return row, True


# ---------------------------------------------------------------------------
# E161: scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    scan_key: uuid.UUID
    location_id: uuid.UUID
    sku_id: uuid.UUID | None
    description: str
    alias_value: str | None
    condition: str
    qty: int
    correction_of_id: uuid.UUID | None
    actual_at: datetime

    def as_input(self) -> dict[str, Any]:
        return {
            "scan_key": str(self.scan_key),
            "location_id": str(self.location_id),
            "sku_id": str(self.sku_id) if self.sku_id else None,
            "description": self.description,
            "alias_value": self.alias_value,
            "condition": self.condition,
            "qty": self.qty,
            "correction_of_id": str(self.correction_of_id) if self.correction_of_id else None,
        }


def parse_scans(body: dict[str, Any], now: datetime) -> list[Observation]:
    raw = body.get("observations")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OBSERVATIONS:
        raise _bad(f"observations is a list of 1 to {MAX_OBSERVATIONS} scans.", "observations")
    out: list[Observation] = []
    seen: set[uuid.UUID] = set()
    for index, item in enumerate(raw):
        at = f"observations[{index}]"
        obs = _closed(item, OBSERVATION_FIELDS, at)
        key = _uuid(obs.get("scan_key"), f"{at}.scan_key")
        if key in seen:
            raise _bad("A scan_key appears twice in this request.", f"{at}.scan_key")
        seen.add(key)
        condition = obs.get("condition")
        if condition not in CONDITION_VALUES:
            raise _bad(
                f"{at}.condition must be one of {', '.join(CONDITION_VALUES)}.", f"{at}.condition"
            )
        qty = obs.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or abs(qty) > 999_999:
            raise _bad(f"{at}.qty must be a whole number.", f"{at}.qty")
        out.append(
            Observation(
                scan_key=key,
                location_id=_uuid(obs.get("location_id"), f"{at}.location_id"),
                sku_id=_optional_uuid(obs.get("sku_id"), f"{at}.sku_id"),
                description=_text(obs.get("description"), f"{at}.description", 240, required=False),
                alias_value=_text(obs.get("alias_value"), f"{at}.alias_value", 128, required=False)
                or None,
                condition=str(condition),
                qty=qty,
                correction_of_id=_optional_uuid(
                    obs.get("correction_of_id"), f"{at}.correction_of_id"
                ),
                actual_at=_moment(obs.get("actual_at"), f"{at}.actual_at", now),
            )
        )
    return out


@dataclass
class _ScanRules:
    """What one pass may record: its assigned locations, brand, recount cells and corrections."""

    coverage: set[uuid.UUID]
    brand_skus: set[uuid.UUID] | None
    cells: set[tuple[str, str]] | None
    known_skus: set[uuid.UUID]
    originals: dict[uuid.UUID, Any]
    totals: dict[uuid.UUID, int]

    @classmethod
    def for_pass(
        cls,
        run: CommandRun,
        stocktake: GoodsStocktake,
        count_pass: GoodsCountPass,
        stored: list[Any],
        fresh: list[Observation],
    ) -> _ScanRules:
        from masters.goods_identity_models import ProductSku

        scope = stocktake.scope or {}
        pass_scope = count_pass.scope or {}
        totals: dict[uuid.UUID, int] = defaultdict(int)
        for row in stored:
            totals[row.correction_of_id or row.pk] += row.qty
        return cls(
            coverage=pass_coverage(stocktake, count_pass),
            brand_skus=(
                _brand_skus(int(scope["brand_id"])) if scope.get("kind") == "brand" else None
            ),
            cells=(
                {(cell["identity"], cell["location_id"]) for cell in pass_scope.get("cells") or []}
                if pass_scope.get("kind") == "recount"
                else None
            ),
            known_skus=set(
                ProductSku.objects.filter(
                    tenant_id=run.tenant_id, pk__in=[o.sku_id for o in fresh if o.sku_id]
                ).values_list("pk", flat=True)
            ),
            originals={row.pk: row for row in stored if row.correction_of_id is None},
            totals=totals,
        )

    def check(self, index: int, item: Observation) -> list[dict[str, Any]]:
        at = f"observations[{index}]"
        found = self._where_and_what(at, item)
        if item.correction_of_id is None:
            if not 1 <= item.qty <= 999_999:
                found.append(
                    issue("QTY_INVALID", "A scan counts 1 to 999,999 pieces.", field=f"{at}.qty")
                )
            return found
        return found + self._correction(at, item)

    def _where_and_what(self, at: str, item: Observation) -> list[dict[str, Any]]:
        """Is the piece somewhere this pass counts, and is it described well enough?"""
        cell = (identity_key(item.sku_id, item.description), str(item.location_id))
        rules = [
            (
                item.location_id not in self.coverage,
                "LOCATION_OUT_OF_SCOPE",
                "That location is not part of what this pass was assigned.",
                f"{at}.location_id",
            ),
            (
                item.sku_id is not None and item.sku_id not in self.known_skus,
                "UNKNOWN_SKU",
                "No SKU has that ID.",
                f"{at}.sku_id",
            ),
            (
                item.condition == "unidentified" and item.sku_id is not None,
                "UNIDENTIFIED_WITH_SKU",
                "Unidentified goods carry a description, not a SKU.",
                at,
            ),
            (
                item.sku_id is None and not item.description,
                "DESCRIPTION_REQUIRED",
                "Goods without a SKU need a description.",
                f"{at}.description",
            ),
            (
                self.brand_skus is not None
                and item.sku_id is not None
                and item.sku_id not in self.brand_skus,
                "BRAND_OUT_OF_SCOPE",
                "That item is not of the brand this count covers.",
                f"{at}.sku_id",
            ),
            (
                self.cells is not None and cell not in self.cells,
                "OUT_OF_RECOUNT_SCOPE",
                "This recount covers only the items and locations it names.",
                at,
            ),
        ]
        return [
            issue(code, message, field=where) for broken, code, message, where in rules if broken
        ]

    def _correction(self, at: str, item: Observation) -> list[dict[str, Any]]:
        target = self.originals.get(item.correction_of_id) if item.correction_of_id else None
        if target is None:
            return [
                issue(
                    "CORRECTION_TARGET",
                    "A correction must point at an original scan in this pass.",
                    field=f"{at}.correction_of_id",
                )
            ]
        same = (
            target.condition == item.condition
            and target.sku_id == item.sku_id
            and target.location_id == item.location_id
            and identity_key(target.sku_id, target.description)
            == identity_key(item.sku_id, item.description)
        )
        if item.qty == 0 or not same:
            return [
                issue(
                    "CORRECTION_INVALID",
                    "A correction changes only the quantity of the scan it corrects.",
                    field=at,
                )
            ]
        if self.totals[target.pk] + item.qty < 0:
            return [
                issue(
                    "CORRECTION_BELOW_ZERO",
                    "A correction cannot take a scan below zero.",
                    field=f"{at}.qty",
                )
            ]
        self.totals[target.pk] += item.qty
        return []


def _unacknowledged(stored: list[Any], observations: list[Observation]) -> list[Observation]:
    """The scans not yet kept. A resent scan counts once; a reused key with new content is not."""
    by_key = {row.scan_key: row for row in stored}
    fresh: list[Observation] = []
    for item in observations:
        known = by_key.get(item.scan_key)
        if known is None:
            fresh.append(item)
        elif _observation_input(known) != item.as_input():
            raise Refusal(
                "COMMAND_CONFLICT",
                "That scan_key was already used for a different scan in this pass.",
                issues=[issue("SCAN_KEY_REUSED", "Use a new scan_key", field=str(item.scan_key))],
            )
    return fresh


def scan(
    run: CommandRun,
    pass_id: uuid.UUID,
    observations: list[Observation],
    expected_revision: int | None,
) -> GoodsCountPass:
    """Append durable observations to the caller's open pass. Nothing moves; nothing is revealed.

    Every observation is checked before any is kept. A scan key already
    acknowledged with the same content counts once (such a request needs no
    current revision); with other content it is ``COMMAND_CONFLICT``.
    """
    from inbound.goods_models import ScanObservation

    count_pass = _lock_pass(run, pass_id)
    stocktake = GoodsStocktake.objects.get(pk=count_pass.stocktake_id)
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = stocktake.site_id
    if count_pass.counter_id != run.principal.human_id:
        raise Refusal("ACTION_DENIED", "Only the person counting this pass scans into it.")
    _require_open(stocktake)
    if count_pass.state != GoodsCountPass.State.OPEN:
        raise Refusal(
            "COUNT_NOT_OPEN", "This pass has been submitted. Its count can no longer change."
        )
    stored = pass_observations([count_pass.pk])
    fresh = _unacknowledged(stored, observations)
    if not fresh:
        run.audit_after = {"pass_id": str(count_pass.pk), "posted": []}
        return count_pass
    if is_stale(count_pass, run.now):
        raise Refusal(
            "COUNT_STALE",
            "This pass has been idle for 24 hours. Resume it before counting on.",
        )
    if expected_revision != count_pass.revision:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "This pass changed after you loaded it. Reload it and scan again.",
        )
    rules = _ScanRules.for_pass(run, stocktake, count_pass, stored, fresh)
    problems = [found for index, item in enumerate(fresh) for found in rules.check(index, item)]
    if problems:
        raise Refusal(
            "OBSERVATION_INVALID", "Some scans could not be recorded.", status=422, issues=problems
        )
    for item in fresh:
        run.record(
            ScanObservation(
                stocktake_pass_id=count_pass.pk,
                site_id=stocktake.site_id,
                scan_key=item.scan_key,
                sku_id=item.sku_id,
                description=item.description,
                alias_value=item.alias_value,
                condition=item.condition,
                qty=item.qty,
                location_id=item.location_id,
                correction_of_id=item.correction_of_id,
            ),
            event_at=item.actual_at,
        )
    count_pass.revision += 1
    count_pass.last_activity_at = run.now
    count_pass.observation_hash = observation_hash(count_pass, pass_observations([count_pass.pk]))
    count_pass.save(update_fields=["revision", "last_activity_at", "observation_hash"])
    run.audit_after = {
        "pass_id": str(count_pass.pk),
        "posted": sorted(str(item.scan_key) for item in fresh),
        "revision": count_pass.revision,
    }
    return count_pass


# ---------------------------------------------------------------------------
# E162: submit with the GSA-T17 affirmation
# ---------------------------------------------------------------------------


def parse_submit(body: dict[str, Any]) -> tuple[str, bool]:
    reviewed = body.get("reviewed_hash")
    if not isinstance(reviewed, str) or len(reviewed) != 64:
        raise _bad("reviewed_hash is the pass's observation_hash you reviewed.", "reviewed_hash")
    complete = body.get("scope_complete")
    if not isinstance(complete, bool):
        raise _bad("scope_complete is true or false.", "scope_complete")
    return reviewed, complete


def submit(
    run: CommandRun,
    pass_id: uuid.UUID,
    *,
    reviewed_hash: str,
    scope_complete: bool,
    expected_revision: int | None,
) -> GoodsCountPass:
    """Close a pass on the counter's word that the whole assigned scope was counted."""
    stocktake_id = (
        GoodsCountPass.objects.filter(pk=pass_id).values_list("stocktake_id", flat=True).first()
    )
    if stocktake_id is None:
        raise Refusal("NOT_FOUND", "That count pass was not found.")
    stocktake = _lock_stocktake(run, stocktake_id)
    count_pass = _lock_pass(run, pass_id)
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = stocktake.site_id
    if count_pass.counter_id != run.principal.human_id:
        raise Refusal("ACTION_DENIED", "Only the person counting this pass submits it.")
    _require_open(stocktake)
    if count_pass.state != GoodsCountPass.State.OPEN:
        raise Refusal("COUNT_NOT_OPEN", "This pass has already been submitted.")
    if is_stale(count_pass, run.now):
        raise Refusal(
            "COUNT_STALE",
            "This pass has been idle for 24 hours. Resume it and check it before submitting.",
        )
    rows = pass_observations([count_pass.pk])
    current = observation_hash(count_pass, rows)
    if expected_revision != count_pass.revision or reviewed_hash != current:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "This pass changed after you reviewed it. Reload it and check it again.",
        )
    if not scope_complete:
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "Confirm you have counted the whole assigned area, including empty locations, "
            "before submitting. An unfinished pass stays open to continue.",
            issues=[
                issue(
                    "SCOPE_NOT_AFFIRMED",
                    "The whole assigned scope was not affirmed as counted.",
                    field="scope_complete",
                )
            ],
        )
    run.record(
        CountAffirmation(
            count_pass=count_pass,
            counter_id=count_pass.counter_id,
            scope_hash=scope_hash(count_pass),
            observation_hash=current,
            observation_revision=count_pass.revision,
        )
    )
    count_pass.state = GoodsCountPass.State.SUBMITTED
    count_pass.submitted_at = run.now
    count_pass.observation_hash = current
    count_pass.revision += 1
    count_pass.save(update_fields=["state", "submitted_at", "observation_hash", "revision"])
    _bump(run, stocktake)
    record_event(
        run,
        stocktake.document_id,
        EVENT_PASS_SUBMITTED,
        payload={
            "pass_id": str(count_pass.pk),
            "observation_hash": current,
            "scope_hash": scope_hash(count_pass),
            "details": [],
        },
    )
    run.audit_after = {"pass_id": str(count_pass.pk), "observation_hash": current}
    return count_pass


# ---------------------------------------------------------------------------
# Resume a stale pass
# ---------------------------------------------------------------------------


def stale_event_key(count_pass: GoodsCountPass) -> uuid.UUID:
    assert count_pass.stale_at is not None
    return engine.event_key("count_pass_stale", count_pass.pk, count_pass.stale_at.isoformat())


def resume(run: CommandRun, pass_id: uuid.UUID, expected_revision: int | None) -> GoodsCountPass:
    """Explicitly take up a stale pass again. Its scans stay; its owned work resolves."""
    stocktake_id = (
        GoodsCountPass.objects.filter(pk=pass_id).values_list("stocktake_id", flat=True).first()
    )
    if stocktake_id is None:
        raise Refusal("NOT_FOUND", "That count pass was not found.")
    stocktake = _lock_stocktake(run, stocktake_id)
    count_pass = _lock_pass(run, pass_id)
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = stocktake.site_id
    _require_open(stocktake)
    if count_pass.state != GoodsCountPass.State.OPEN:
        raise Refusal("COUNT_NOT_OPEN", "This pass has already been submitted.")
    if expected_revision != count_pass.revision:
        raise Refusal("REVISION_SUPERSEDED", "This pass changed after you loaded it. Reload it.")
    if not is_stale(count_pass, run.now):
        raise Refusal("STATE_CONFLICT", "This pass is not stale. Carry on counting.")
    if count_pass.stale_at is not None:
        resolve_exceptions(
            run,
            kind=STALE_KIND,
            subject_key=f"stocktake:{stocktake.pk}",
            reason_code="COUNT_PASS_RESUMED",
            source_event_key=stale_event_key(count_pass),
        )
    count_pass.stale_at = None
    count_pass.last_activity_at = run.now
    count_pass.revision += 1
    count_pass.save(update_fields=["stale_at", "last_activity_at", "revision"])
    _bump(run, stocktake)
    record_event(
        run,
        stocktake.document_id,
        EVENT_RESUMED,
        payload={"pass_id": str(count_pass.pk), "details": []},
    )
    run.audit_after = {"pass_id": str(count_pass.pk), "resumed": True}
    return count_pass


def sweep_stale(tenant_id: uuid.UUID) -> int:
    """The worker's stale check: every open pass idle 24 hours becomes owned work.

    Idleness is discovered by time passing, never by an action, so this is the
    count's own scheduled command (``core.outbox.register_scheduled``). It marks
    the pass stale, records the history event and opens ``count_session_stale``
    at the site; it never submits, closes or cancels anything.
    """
    cutoff = database_now() - STALE_AFTER
    due = list(
        GoodsCountPass.objects.filter(
            tenant_id=tenant_id,
            state=GoodsCountPass.State.OPEN,
            stale_at__isnull=True,
            last_activity_at__lt=cutoff,
            stocktake__state=GoodsStocktake.State.OPEN,
        ).values_list("pk", flat=True)
    )
    if not due:
        return 0
    marked: list[str] = []

    def handler(run: CommandRun) -> CommandResult:
        passes = list(
            GoodsCountPass.objects.filter(pk__in=due).values_list("stocktake_id", flat=True)
        )
        stocktakes = {
            row.pk: row
            for row in run.lock(
                LockRank.DOCUMENT,
                GoodsStocktake.objects.filter(pk__in=sorted(set(passes), key=str)),
            )
        }
        for count_pass in run.lock(LockRank.DOCUMENT, GoodsCountPass.objects.filter(pk__in=due)):
            stocktake = stocktakes.get(count_pass.stocktake_id)
            if (
                stocktake is None
                or stocktake.state != GoodsStocktake.State.OPEN
                or count_pass.state != GoodsCountPass.State.OPEN
                or count_pass.stale_at is not None
                or not is_stale(count_pass, run.now)
            ):
                continue
            count_pass.stale_at = run.now
            count_pass.revision += 1
            count_pass.save(update_fields=["stale_at", "revision"])
            record_event(
                run,
                stocktake.document_id,
                EVENT_STALE,
                payload={"pass_id": str(count_pass.pk), "details": []},
            )
            open_exception(
                run,
                kind=STALE_KIND,
                site_id=stocktake.site_id,
                subject_key=f"stocktake:{stocktake.pk}",
                reason_code="COUNT_PASS_IDLE",
                source_event_key=stale_event_key(count_pass),
                allowed_resolution_actions=STALE_RESOLUTIONS,
                note=(
                    f"Pass {count_pass.pass_no} has been idle since "
                    f"{(count_pass.last_activity_at or count_pass.created_at).isoformat()}. "
                    "Resume it and finish, or cancel the count."
                ),
            )
            marked.append(str(count_pass.pk))
        run.audit_subject_key = "count:stale"
        run.audit_after = {"marked": marked}
        return CommandResult(resource_type="count_stale_check")

    execute_command(
        Principal(tenant_id=tenant_id, service_code=STALE_SERVICE),
        CommandSpec(
            action=STALE_ACTION,
            command_id=uuid.uuid4(),
            business_input={"cutoff": cutoff.isoformat(), "due": sorted(str(p) for p in due)},
            subject_key="count:stale",
        ),
        handler,
    )
    return len(marked)


# ---------------------------------------------------------------------------
# The variance: observed against the frozen as-of, never against live stock
# ---------------------------------------------------------------------------


@dataclass
class VarianceLine:
    line_key: uuid.UUID
    identity: str
    sku_id: str | None
    description: str
    location_id: str
    condition: str
    book_qty: int = 0
    observed_qty: int | None = 0
    #: The pass whose observations stand for this line; ``None`` when no
    #: selected pass covers its location.
    pass_id: str | None = None

    @property
    def delta(self) -> int | None:
        return None if self.observed_qty is None else self.observed_qty - self.book_qty


@dataclass
class Variance:
    lines: list[VarianceLine]
    selection: list[uuid.UUID]
    issues: list[dict[str, Any]] = field(default_factory=list)
    uncovered: list[uuid.UUID] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.uncovered and not self.issues

    @property
    def zero(self) -> bool:
        return self.complete and all(line.delta == 0 for line in self.lines)

    @property
    def hash(self) -> str:
        return content_hash(
            {
                "selection": sorted(str(p) for p in self.selection),
                "lines": [
                    [str(line.line_key), line.book_qty, line.observed_qty, line.pass_id]
                    for line in sorted(self.lines, key=lambda item: str(item.line_key))
                ],
                "issues": sorted(i["code"] for i in self.issues),
                "uncovered": sorted(str(u) for u in self.uncovered),
            }
        )


def _chain(passes: dict[uuid.UUID, GoodsCountPass], count_pass: GoodsCountPass) -> list[uuid.UUID]:
    """The passes a recount replaces, nearest first, up to its original counting pass."""
    out: list[uuid.UUID] = []
    current: GoodsCountPass | None = count_pass
    while (
        current is not None and current.replaces_id is not None and current.replaces_id not in out
    ):
        out.append(current.replaces_id)
        current = passes.get(current.replaces_id)
    return out


SUBMITTED_STATES = (
    GoodsCountPass.State.SUBMITTED,
    GoodsCountPass.State.SELECTED,
    GoodsCountPass.State.SUPERSEDED,
)


def _chosen(
    passes: dict[uuid.UUID, GoodsCountPass], selected: Sequence[uuid.UUID] | None
) -> list[GoodsCountPass]:
    if selected is None:
        chosen = [p for p in passes.values() if p.state in SUBMITTED_STATES]
    else:
        chosen = []
        for pass_id in selected:
            count_pass = passes.get(pass_id)
            if count_pass is None:
                raise Refusal("NOT_FOUND", "A selected pass is not part of this count.")
            if count_pass.state not in SUBMITTED_STATES:
                raise Refusal(
                    "COUNT_SELECTION_INVALID",
                    "Only submitted passes can be selected.",
                    status=422,
                    issues=[issue("PASS_NOT_SUBMITTED", "Not submitted", field=str(pass_id))],
                )
            chosen.append(count_pass)
    chosen.sort(key=lambda p: (p.submitted_at or p.created_at, str(p.pk)))
    return chosen


class _Selection:
    """Which selected pass stands for each item-at-a-location ("cell")."""

    def __init__(
        self,
        stocktake: GoodsStocktake,
        passes: dict[uuid.UUID, GoodsCountPass],
        chosen: list[GoodsCountPass],
    ) -> None:
        self.issues: list[dict[str, Any]] = []
        base = [p for p in chosen if p.replaces_id is None]
        self.recounts = [p for p in chosen if p.replaces_id is not None]
        self.covers: dict[uuid.UUID, uuid.UUID] = {}
        for count_pass in base:
            for location_id in pass_coverage(stocktake, count_pass):
                if location_id in self.covers and self.covers[location_id] != count_pass.pk:
                    self._issue(
                        "OVERLAPPING_PASSES",
                        "Two selected counting passes cover the same location. Select one.",
                        location_id,
                    )
                    continue
                self.covers[location_id] = count_pass.pk
        self.uncovered = sorted(scope_locations(stocktake) - set(self.covers), key=str)
        base_ids = {p.pk for p in base}
        self.chains = {r.pk: _chain(passes, r) for r in self.recounts}
        self.cells = {
            r.pk: {(c["identity"], c["location_id"]) for c in (r.scope or {}).get("cells") or []}
            for r in self.recounts
        }
        for recount in self.recounts:
            chain = self.chains[recount.pk]
            if not chain or chain[-1] not in base_ids:
                self._issue(
                    "RECOUNT_ORIGINAL_NOT_SELECTED",
                    "A selected recount's original counting pass is not selected.",
                    recount.pk,
                )

    def _issue(self, code: str, message: str, subject: Any) -> None:
        found = issue(code, message, field=str(subject))
        if found not in self.issues:
            self.issues.append(found)

    def supplier(self, cell: tuple[str, str]) -> uuid.UUID | None:
        """The counting pass at the cell's location, or its deepest selected recount."""
        base_pass = self.covers.get(uuid.UUID(cell[1]))
        if base_pass is None:
            return None
        candidates = [
            r for r in self.recounts if base_pass in self.chains[r.pk] and cell in self.cells[r.pk]
        ]
        if not candidates:
            return base_pass
        deepest = max(candidates, key=lambda r: len(self.chains[r.pk]))
        for other in candidates:
            if other.pk != deepest.pk and other.pk not in self.chains[deepest.pk]:
                self._issue(
                    "CONFLICTING_RECOUNTS",
                    "Two selected recounts cover the same item and location. Select one.",
                    other.pk,
                )
        return deepest.pk


def variance(stocktake: GoodsStocktake, selected: Sequence[uuid.UUID] | None = None) -> Variance:
    """Observed minus the frozen book, per item, location and condition.

    ``selected`` is the reviewer's choice of submitted passes; by default every
    submitted pass of the count. Counting passes (not recounts) must not
    overlap; together they must cover every location of the count's scope, or
    the variance is incomplete. A selected recount stands for exactly its cells
    in place of the pass it replaces - never summed with it (design E164) - and
    two recounts of one cell must be one the other's recount. Unscanned known
    stock is zero observed.
    """
    passes = {p.pk: p for p in GoodsCountPass.objects.filter(stocktake=stocktake)}
    chosen = _chosen(passes, selected)
    selection = _Selection(stocktake, passes, chosen)
    lines: dict[tuple[str, str, str], VarianceLine] = {}

    def line_for(
        identity: str, sku_id: Any, description: str, location_id: str, condition: str
    ) -> VarianceLine:
        key = (identity, location_id, condition)
        if key not in lines:
            lines[key] = VarianceLine(
                line_key=line_key(stocktake.pk, identity, location_id, condition),
                identity=identity,
                sku_id=str(sku_id) if sku_id else None,
                description=description,
                location_id=location_id,
                condition=condition,
            )
        return lines[key]

    # Book: the frozen as-of snapshot, never the live position.
    for row in CountSnapshot.objects.filter(stocktake=stocktake):
        address = row.address or {}
        lower, upper = bounds(row.portion)
        description = address.get("description") or ""
        line = line_for(
            identity_key(address.get("sku_id"), description),
            address.get("sku_id"),
            description,
            str(address.get("location_id")),
            str(address.get("condition") or "good"),
        )
        line.book_qty += upper - lower

    observed: dict[uuid.UUID, dict[tuple[str, str, str], int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for row in pass_observations([p.pk for p in chosen]):
        identity = identity_key(row.sku_id, row.description)
        key = (identity, str(row.location_id), row.condition)
        observed[row.stocktake_pass_id][key] += row.qty
        line_for(identity, row.sku_id, row.description, key[1], key[2])

    suppliers = {
        cell: selection.supplier(cell)
        for cell in sorted({(identity, location) for identity, location, _c in lines})
    }
    for (identity, location, condition), line in lines.items():
        source = suppliers.get((identity, location))
        line.pass_id = str(source) if source is not None else None
        line.observed_qty = (
            None
            if source is None
            else observed.get(source, {}).get((identity, location, condition), 0)
        )
    return Variance(
        lines=sorted(lines.values(), key=lambda v: (v.location_id, v.identity, v.condition)),
        selection=[p.pk for p in chosen],
        issues=selection.issues,
        uncovered=selection.uncovered,
    )


# ---------------------------------------------------------------------------
# E164: a scoped recount
# ---------------------------------------------------------------------------


def parse_recount(body: dict[str, Any]) -> tuple[list[uuid.UUID], uuid.UUID, str]:
    raw = body.get("line_keys")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RECOUNT_LINES:
        raise _bad(f"line_keys is a list of 1 to {MAX_RECOUNT_LINES} line keys.", "line_keys")
    keys = [_uuid(item, f"line_keys[{index}]") for index, item in enumerate(raw)]
    if len(set(keys)) != len(keys):
        raise _bad("A line key appears twice.", "line_keys")
    counter_id = _uuid(body.get("counter_id"), "counter_id")
    reason = _text(body.get("reason_code"), "reason_code", 60, required=True)
    return keys, counter_id, reason


def person_can_count(tenant_id: uuid.UUID, human_id: uuid.UUID, site_id: int) -> bool:
    from accounts.goods_models import HumanIdentity
    from accounts.principal import AccessContext, effective_grants

    human = HumanIdentity.objects.filter(tenant_id=tenant_id, pk=human_id, active=True).first()
    if human is None:
        return False
    grants = [g for g in effective_grants(human.pk) if RUN_ACTION in g.actions]
    context = AccessContext(
        user=None, human_id=human.pk, tenant_id=tenant_id, session=None, grants=grants
    )
    return context.can(RUN_ACTION, site_id=site_id)


def request_recount(
    run: CommandRun,
    stocktake_id: uuid.UUID,
    *,
    line_keys: list[uuid.UUID],
    counter_id: uuid.UUID,
    reason_code: str,
    expected_revision: int | None,
) -> GoodsCountPass:
    """Assign a new blind pass over exactly these lines' items and locations.

    The earlier pass and its observations stay; the recount stands in for it on
    those cells only when a reviewer selects it, never summed with it.
    """
    stocktake = _lock_stocktake(run, stocktake_id)
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = stocktake.site_id
    _require_open(stocktake)
    if expected_revision != stocktake.revision:
        raise Refusal("REVISION_SUPERSEDED", "This count changed after you loaded it. Reload it.")
    report = variance(stocktake)
    if report.issues:
        raise Refusal(
            "COUNT_SELECTION_INVALID",
            "The submitted passes do not give one answer per line yet.",
            status=422,
            issues=report.issues,
        )
    by_key = {line.line_key: line for line in report.lines}
    wanted = []
    for key in line_keys:
        line = by_key.get(key)
        if line is None:
            raise Refusal("NOT_FOUND", "A line to recount is not on this count's variance.")
        if line.pass_id is None:
            raise Refusal(
                "COUNT_SESSION_INVALID",
                "A line to recount has not been counted yet.",
                issues=[issue("LINE_NOT_COUNTED", "Not counted yet", line_key=key)],
            )
        wanted.append(line)
    sources = {line.pass_id for line in wanted}
    if len(sources) != 1:
        raise Refusal(
            "INVALID_REQUEST",
            "One recount covers lines counted in one pass. Ask for one recount per pass.",
            issues=[issue("LINES_FROM_SEVERAL_PASSES", "Split the recount", field="line_keys")],
        )
    if not person_can_count(run.tenant_id, counter_id, stocktake.site_id):
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "That person may not count at this site.",
            issues=[issue("COUNTER_NOT_AUTHORISED", "Not a counter here", field="counter_id")],
        )
    if GoodsCountPass.objects.filter(
        stocktake=stocktake, counter_id=counter_id, state="open"
    ).exists():
        raise Refusal(
            "COUNT_SESSION_INVALID",
            "That person already has an unfinished pass in this count. It must be submitted first.",
            issues=[issue("COUNTER_BUSY", "Unfinished pass", field="counter_id")],
        )
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for line in wanted:
        cells.setdefault(
            (line.identity, line.location_id),
            {
                "identity": line.identity,
                "sku_id": line.sku_id,
                "description": line.description,
                "location_id": line.location_id,
            },
        )
    replaces = uuid.UUID(str(next(iter(sources))))
    scope = {
        "kind": "recount",
        "line_keys": sorted(str(key) for key in line_keys),
        "cells": [cells[key] for key in sorted(cells)],
    }
    last = (
        GoodsCountPass.objects.filter(stocktake=stocktake, counter_id=counter_id)
        .order_by("-pass_no")
        .values_list("pass_no", flat=True)
        .first()
    )
    row = GoodsCountPass(
        tenant_id=run.tenant_id,
        stocktake=stocktake,
        counter_id=counter_id,
        entry_user_id=counter_id,
        pass_no=int(last or 0) + 1,
        replaces_id=replaces,
        scope=scope,
        reason_code=reason_code,
        last_activity_at=run.now,
    )
    row.observation_hash = observation_hash(row, [])
    row.save()
    _bump(run, stocktake)
    record_event(
        run,
        stocktake.document_id,
        EVENT_RECOUNT,
        reason_code=reason_code,
        payload={
            "pass_id": str(row.pk),
            "replaces_pass_id": str(replaces),
            "counter_id": str(counter_id),
            "line_keys": scope["line_keys"],
            "details": [],
        },
    )
    run.audit_after = {"pass_id": str(row.pk), "replaces": str(replaces), "lines": len(wanted)}
    return row


# ---------------------------------------------------------------------------
# The two endings that move nothing: verified zero variance, and cancellation
# ---------------------------------------------------------------------------


def parse_close(body: dict[str, Any]) -> tuple[str, list[uuid.UUID]]:
    reviewed = body.get("reviewed_hash")
    if not isinstance(reviewed, str) or len(reviewed) != 64:
        raise _bad("reviewed_hash is the variance_hash you reviewed.", "reviewed_hash")
    raw = body.get("selected_pass_ids")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise _bad("selected_pass_ids lists 1 to 200 submitted passes.", "selected_pass_ids")
    ids = [_uuid(item, f"selected_pass_ids[{index}]") for index, item in enumerate(raw)]
    if len(set(ids)) != len(ids):
        raise _bad("A pass is selected twice.", "selected_pass_ids")
    return reviewed, ids


def _release(run: CommandRun, stocktake: GoodsStocktake, guard: SiteGuard | None) -> None:
    if guard is not None and guard.freeze_id == stocktake.pk:
        guard.freeze_id = None
        guard.save(update_fields=["freeze_id"])
    resolve_exceptions(
        run,
        kind=STALE_KIND,
        subject_key=f"stocktake:{stocktake.pk}",
        reason_code="COUNT_ENDED",
    )


def _lock_site_and_count(
    run: CommandRun, stocktake_id: uuid.UUID
) -> tuple[GoodsStocktake, SiteGuard | None]:
    site_id = (
        GoodsStocktake.objects.filter(pk=stocktake_id).values_list("site_id", flat=True).first()
    )
    if site_id is None:
        raise Refusal("NOT_FOUND", "That count was not found.")
    guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site_id))
    stocktake = _lock_stocktake(run, stocktake_id)
    return stocktake, (guards[0] if guards else None)


def close_zero(
    run: CommandRun,
    stocktake_id: uuid.UUID,
    *,
    reviewed_hash: str,
    selected: list[uuid.UUID],
    expected_revision: int | None,
) -> GoodsStocktake:
    """P18's zero-variance ending: close and release the freeze, posting nothing."""
    stocktake, guard = _lock_site_and_count(run, stocktake_id)
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = stocktake.site_id
    _require_open(stocktake)
    if expected_revision != stocktake.revision:
        raise Refusal("REVISION_SUPERSEDED", "This count changed after you loaded it. Reload it.")
    passes = list(run.lock(LockRank.DOCUMENT, GoodsCountPass.objects.filter(stocktake=stocktake)))
    if still := [p for p in passes if p.state == GoodsCountPass.State.OPEN]:
        raise Refusal(
            "COUNT_SESSION_INVALID",
            f"{len(still)} pass{'' if len(still) == 1 else 'es'} of this count "
            f"{'is' if len(still) == 1 else 'are'} still being counted. Finish or cancel first.",
            issues=[issue("PASS_OPEN", "Still counting", field=str(p.pk)) for p in still],
        )
    report = variance(stocktake, selected)
    if report.issues or report.uncovered:
        raise Refusal(
            "COUNT_SELECTION_INVALID",
            "The selected passes do not count the whole scope exactly once.",
            status=422,
            issues=report.issues
            + [
                issue("SCOPE_NOT_COVERED", "No selected pass covers this location", field=str(u))
                for u in report.uncovered
            ],
        )
    if reviewed_hash != report.hash:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The variance changed after you reviewed it. Reload it and review it again.",
        )
    if not report.zero:
        raise Refusal(
            "VARIANCE_PENDING",
            "The count differs from the book. Differences wait for review and the Owner's "
            "approval; this count cannot close as matching.",
            issues=[
                issue("DIFFERENCE", "Counted differs from the book", line_key=line.line_key)
                for line in report.lines
                if line.delta != 0
            ][:50],
        )
    human_id = run.principal.human_id
    if human_id is None:
        raise Refusal("ACTION_DENIED", "A count is closed by a named person.")
    decision = run.record(
        CountDecision(
            stocktake=stocktake,
            observation_hash=report.hash,
            variances=[
                {
                    "line_key": str(line.line_key),
                    "sku_id": line.sku_id,
                    "description": line.description,
                    "location_id": line.location_id,
                    "condition": line.condition,
                    "book_qty": line.book_qty,
                    "observed_qty": line.observed_qty,
                    "delta": 0,
                    "pass_id": line.pass_id,
                }
                for line in report.lines
            ],
            movement=None,
            approver_id=human_id,
        )
    )
    chosen = {p.pk for p in passes if p.pk in set(report.selection)}
    for count_pass in passes:
        if count_pass.pk in chosen:
            run.record(
                CountSelection(
                    decision=decision, count_pass=count_pass, scope=count_pass.scope or {}
                )
            )
            count_pass.state = GoodsCountPass.State.SELECTED
        elif count_pass.state == GoodsCountPass.State.SUBMITTED:
            count_pass.state = GoodsCountPass.State.SUPERSEDED
        else:
            continue
        count_pass.revision += 1
        count_pass.save(update_fields=["state", "revision"])
    stocktake.state = GoodsStocktake.State.CLOSED
    stocktake.save(update_fields=["state"])
    _bump(run, stocktake)
    _release(run, stocktake, guard)
    record_event(
        run,
        stocktake.document_id,
        EVENT_CLOSED,
        payload={
            "to_state": "closed",
            "decision_id": str(decision.pk),
            "variance_hash": report.hash,
            "selected_pass_ids": sorted(str(p) for p in chosen),
            "details": [],
        },
    )
    run.audit_after = {
        "stocktake_id": str(stocktake.pk),
        "state": "closed",
        "zero_variance": True,
        "freeze_released": True,
    }
    return stocktake


def parse_cancel(body: dict[str, Any]) -> tuple[str, str]:
    return (
        _text(body.get("reason_code"), "reason_code", 60, required=True),
        _text(body.get("note"), "note", 500, required=False),
    )


def cancel(
    run: CommandRun,
    stocktake_id: uuid.UUID,
    *,
    reason_code: str,
    note: str,
    expected_revision: int | None,
) -> GoodsStocktake:
    """End an unfinished count: every observation stays, nothing posts, the freeze lifts."""
    stocktake, guard = _lock_site_and_count(run, stocktake_id)
    run.audit_subject_key = f"stocktake:{stocktake.pk}"
    run.audit_site_id = stocktake.site_id
    if stocktake.state not in UNFINISHED:
        raise Refusal(
            "COUNT_NOT_OPEN", f"This count is already {stocktake.state}; it cannot change now."
        )
    if expected_revision != stocktake.revision:
        raise Refusal("REVISION_SUPERSEDED", "This count changed after you loaded it. Reload it.")
    # The passes too, as a scan takes its pass's lock: a scan either commits
    # before the cancellation or finds the count cancelled - never lands after it.
    passes = run.lock(LockRank.DOCUMENT, GoodsCountPass.objects.filter(stocktake=stocktake))
    unfinished = sum(p.state == GoodsCountPass.State.OPEN for p in passes)
    stocktake.state = GoodsStocktake.State.CANCELLED
    stocktake.save(update_fields=["state"])
    _bump(run, stocktake)
    _release(run, stocktake, guard)
    record_event(
        run,
        stocktake.document_id,
        EVENT_CANCELLED,
        reason_code=reason_code,
        payload={
            "to_state": "cancelled",
            "note": note,
            "unfinished_passes": unfinished,
            "details": [],
        },
    )
    run.audit_after = {
        "stocktake_id": str(stocktake.pk),
        "state": "cancelled",
        "reason_code": reason_code,
        "freeze_released": True,
    }
    return stocktake
