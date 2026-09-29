"""Goods-v1 receipt PT: prepare, review, submit, approve, reverse, reissue (design E122-E131).

A PT draft is an immutable revision chain of canonical lines. Submission freezes
the exact revision for a distinct checker; approval, inside one command, rechecks
the revision, rows, profile, reconciliation and live coverage under locks, gives
the document its number, freezes official lines and origin value, and covers the
counted GRN portions (P04). Approval is not acceptance: covered goods stay held in
receiving or quarantine until scanned into a location. No GL, vendor or cash row
is written. Reversal counters coverage (P06) only while no portion has left.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils.dateparse import parse_datetime

from alerts.goods_services import notify, open_exception, resolve_exceptions
from approvals.goods_models import ApprovalRequest
from approvals.goods_policy import Amounts, pin
from approvals.goods_services import (
    DecisionContext,
    create_request,
    register_subject_cells,
    register_subject_handler,
    supersede_pending,
)
from core.canonical import content_hash
from core.commands import CommandRun, register_integrity_refusal
from core.goods_documents import (
    MAX_LINES,
    append_revision,
    lock_heads,
    new_document,
    officialise,
    pending_lines,
    record_event,
    revision_lines,
    set_state,
)
from core.goods_fields import MAX_LINE_QTY, bounds
from core.goods_money import MoneyInvalid, paise_from_json
from core.kernel_models import (
    DocumentHead,
    DocumentIdentity,
    DraftRevision,
    OfficialLine,
    OfficialVersion,
)
from core.numbering import allocate
from core.refusals import Refusal, issue
from inbound import goods_services as receiving
from inbound.goods_models import CounterGrnDraft, GoodsGrn
from masters.goods_config import ConfigTarget, check_pinned
from masters.goods_identity_models import ProductSku
from masters.goods_identity_services import normalise_text, profile_from_version
from masters.goods_models import ConfigVersion, SiteGuard
from masters.hsn import refuse_missing_hsn
from masters.models import Brand, Season
from ptmapper import goods_calc
from ptmapper import goods_item_match as items
from ptmapper.goods_models import GoodsPt
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import (
    ActiveHold,
    ActiveReservation,
    CoverageEvent,
    CustodyLot,
    LiveCoverage,
    Origin,
    Position,
)

RECEIPT = "receipt"
PRIMARY = "primary"
SUPPLEMENT = "supplement"
RECEIPT_DOC_TYPE = "RPT"
APPROVE_RECEIPT = "pt.approve.receipt"
APPROVE_REVERSAL = "pt.reversal.approve"
#: Every active hold excludes the portion it covers from routine PT coverage, as the stock
#: engine's ``eligible_portions`` subtracts all of them: a receiving disposition's
#: ``receipt_excess``/``receipt_damage``, an acceptance ``damage`` hold, or any later kind.
#: Held excess joins a supplement only after an approved ``accept_excess`` releases it;
#: damaged goods never go on a PT, held or not (overall PRD §15.2.1 rules 2-4).
MONEY_COLUMNS = frozenset({"basic_paise", "mrp_paise", "check_p_rate_paise", "check_mrp_paise"})
TEXT_LIMITS = {"hsn": 24, "alias_as_used": 128, "source_ref": 200}
EDITABLE_COLUMNS = MONEY_COLUMNS | frozenset(
    {
        "sku_id",
        "season_id",
        "hsn",
        "qty",
        "alias_id",
        "alias_as_used",
        "attributes",
        "direction_override",
        "source_ref",
        "coverage_requests",
        # The describing values a row holds besides its attributes (OPS-15):
        # stored under ``describing``, so a drafted new item keeps its brand and design.
        "brand_id",
        "design",
    }
)
CALCULATED_COLUMNS = frozenset({"p_rate_paise", "margin_pct", "input_tax_pct", "output_tax_pct"})
LINE_KEYS = frozenset(
    {
        "line_key",
        "sku_id",
        "attributes",
        "season_id",
        # Set by the opening path from the manifest row and carried, never a
        # cell a person may edit (it is not in ``EDITABLE_COLUMNS``): the
        # declaration belongs to the row's maker, not to whoever opens the PT.
        "season_unknown_historical",
        "alias_id",
        "alias_as_used",
        "qty",
        "coverage_requests",
        "supplied",
        "hsn",
        "direction_override",
        "source_ref",
        # OPS-15 (store and warehouse operations PRD §5.4). ``describing`` holds the
        # row's brand and design; ``origins`` where each KDPS column's value came
        # from (file, rule, suggestion, person or none); ``suggestions`` the close
        # matches waiting on a cell - never applied until a person picks one; and
        # ``match`` how the row's item was found, with the server's notes on it.
        # The last three are the server's own: a typed line never supplies them.
        "describing",
        "origins",
        "suggestions",
        "match",
    }
)
#: Line keys only the server writes: dropped from lines a client types.
INTAKE_KEYS = frozenset({"origins", "suggestions", "match"})
ORIGINS = frozenset({"file", "rule", "suggestion", "person", "none"})
DESIGN_TEXT = 120
#: Keys the server writes (or the detail DTO adds); accepted on input and dropped.
SERVER_KEYS = frozenset(
    {
        "calculated",
        "issues",
        "profile_version_id",
        "rate_version_id",
        "tax_version_id",
        "review_hash",
        "selected_origin_ids",
        "manifest_row_id",
        "row_hash",
        "reviewed",
        # What the grid read adds beside a line (OPS-16): resolved cells, pending item.
        "cells",
        "item_pending",
        "item_rejected",
    }
)
#: A row edit: typed ``fields``, or pasted ``canonical`` cells (ticket 06A), or ``delete``.
UPDATE_KEYS = frozenset({"line_key", "fields", "canonical", "source_evidence_id", "delete"})
#: AttributeValues bounds: closed keys, bounded text and a bounded count per line.
ATTRIBUTE_KEYS = frozenset({"field_id", "vocabulary_value_id", "supplied_text", "unknown"})
ATTRIBUTE_TEXT = 240
MAX_ATTRIBUTES = 100


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _row_invalid(message: str, line_key: Any = None) -> Refusal:
    return Refusal(
        "ROW_INVALID",
        message,
        status=422,
        issues=[issue("ROW_INVALID", message, line_key=str(line_key) if line_key else None)],
    )


def _int_or_none(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _str_or_none(value: int | None) -> str | None:
    return None if value is None else str(value)


def _dec_or_none(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Refusal(
            "PROFILE_INVALID", "The profile's rates are not valid numbers.", status=422
        ) from None


# ---------------------------------------------------------------------------
# Profile, rates and tax
# ---------------------------------------------------------------------------


@dataclass
class ProfileContext:
    version: ConfigVersion
    rates_version: ConfigVersion
    tax_version: ConfigVersion
    rates: goods_calc.Rates
    directions: list[str]
    tolerance: int
    #: The business instant every pinned version and HSN rule is judged at (actual arrival).
    at: datetime

    def slabs(self, hsn: str | None) -> list[goods_calc.Slab]:
        if not hsn:
            return []
        rules = [
            rule
            for rule in self.tax_version.payload.get("hsn_rules") or []
            if str(rule.get("hsn")) == hsn and _rule_effective(rule, self.at)
        ]
        if len(rules) != 1:
            return []
        return [_slab(slab) for slab in rules[0].get("slabs") or []]


def _rule_effective(rule: dict[str, Any], at: datetime) -> bool:
    """``[effective_from, effective_to)`` by exact instant; an unreadable start never applies."""
    start = parse_datetime(str(rule.get("effective_from") or ""))
    end = parse_datetime(str(rule["effective_to"])) if rule.get("effective_to") else None
    if start is None or start.tzinfo is None or start > at:
        return False
    return end is None or (end.tzinfo is not None and at < end)


def _slab(slab: dict[str, Any]) -> goods_calc.Slab:
    upper = slab.get("upper_paise")
    return goods_calc.Slab(
        int(slab["lower_paise"]),
        int(str(upper)) if upper not in (None, "") else None,
        bool(slab.get("lower_inclusive", True)),
        bool(slab.get("upper_inclusive", False)),
        Decimal(str(slab["input_pct"])),
        Decimal(str(slab["output_pct"])),
    )


def _config(tenant_id: uuid.UUID, pk: Any, kind: str) -> ConfigVersion | None:
    parsed = _uuid_or_none(pk)
    if parsed is None:
        return None
    return ConfigVersion.objects.filter(tenant_id=tenant_id, pk=parsed, kind=kind).first()


def pt_target(grn: GoodsGrn, brand_ids: Iterable[int | None] = ()) -> ConfigTarget:
    """What a receipt PT's configuration must apply to: the GRN's site, the arrival brand and
    every line brand, the receipt purpose, at the actual arrival time."""
    brands = {grn.arrival.brand_id, *brand_ids}
    return ConfigTarget.of(
        grn.arrival.actual_arrival_at,
        site_id=grn.document.site_id,
        brand_ids=brands,
        purpose=RECEIPT,
    )


def document_target(document_id: uuid.UUID) -> ConfigTarget:
    goods_pt = (
        GoodsPt.objects.select_related(
            "document", "grn__document", "grn__arrival", "manifest_version"
        )
        .filter(document_id=document_id)
        .first()
    )
    if goods_pt is None:
        raise Refusal("NOT_FOUND", "That PT was not found.")
    if goods_pt.grn is not None:
        return pt_target(goods_pt.grn, [brand for _site, brand in pt_cells(goods_pt)])
    if goods_pt.manifest_version is not None:
        from ptmapper.goods_manifest_services import manifest_target

        return manifest_target(
            goods_pt.manifest_version.cutoff_at,
            goods_pt.document.held_site_id,
            [brand for _site, brand in pt_cells(goods_pt)],
        )
    raise Refusal("NOT_FOUND", "That PT was not found.")


def load_profile(
    tenant_id: uuid.UUID,
    profile_version_id: Any,
    target: ConfigTarget,
    *,
    code: str = "PROFILE_INVALID",
) -> ProfileContext:
    """The pinned profile with its pinned rate, tax and vocabulary versions.

    Each must be approved, not withdrawn, cover the PT's site, brands and purpose, and be
    effective at its business instant, with no rival version matching (change PRD §14.4).
    """
    profile = check_pinned(
        tenant_id, "profile", profile_version_id, target, code=code, path="profile_version_id"
    )
    payload = profile.payload if isinstance(profile.payload, dict) else {}
    rates = check_pinned(
        tenant_id, "rates", payload.get("rates_version_id"), target, code=code, path="rates"
    )
    tax = check_pinned(
        tenant_id, "tax_rates", payload.get("tax_version_id"), target, code=code, path="tax"
    )
    for column in payload.get("columns") or []:
        if isinstance(column, dict) and column.get("vocabulary_version_id"):
            check_pinned(
                tenant_id,
                "vocabulary",
                column["vocabulary_version_id"],
                target,
                code=code,
                path="vocabulary",
            )
    directions = [
        d for d in payload.get("directions") or goods_calc.DIRECTIONS if d in goods_calc.DIRECTIONS
    ]
    tolerance = max(
        [
            int(column.get("tolerance_minor_units") or 0)
            for column in payload.get("columns") or []
            if column.get("key") in ("p_rate", "mrp")
        ],
        default=0,
    )
    return ProfileContext(
        version=profile,
        rates_version=rates,
        tax_version=tax,
        rates=goods_calc.Rates(
            _decimal(rates.payload.get("transport_pct", "0")),
            _decimal(rates.payload.get("pricing_margin_pct", "0")),
        ),
        directions=directions,
        tolerance=tolerance,
        at=target.at,
    )


def price_line(line: dict[str, Any], profile: ProfileContext, direction: str) -> dict[str, Any]:
    """The line with server-owned calculated values and their row issues."""
    supplied = line.get("supplied") or {}
    chosen = line.get("direction_override") or direction
    result = goods_calc.calculate(
        goods_calc.RowInput(
            chosen,
            basic_paise=_int_or_none(supplied.get("basic_paise")),
            mrp_paise=_int_or_none(supplied.get("mrp_paise")),
            check_p_rate_paise=_int_or_none(supplied.get("check_p_rate_paise")),
            check_mrp_paise=_int_or_none(supplied.get("check_mrp_paise")),
            tolerance_minor_units=profile.tolerance,
        ),
        profile.rates,
        profile.slabs(line.get("hsn")),
    )
    issues = [
        issue(i.code, i.message, field=i.field, line_key=line["line_key"]) for i in result.issues
    ]
    # The item, coverage and season notes the brand-file intake or a row edit left (OPS-15).
    issues += [
        issue(note["code"], note["message"], field=note.get("field"), line_key=line["line_key"])
        for note in (line.get("match") or {}).get("notes") or []
    ]
    if chosen not in profile.directions:
        issues.append(
            issue(
                "DIRECTION_NOT_ALLOWED",
                "The profile does not allow this direction",
                line_key=line["line_key"],
            )
        )
    priced = dict(line)
    priced["profile_version_id"] = str(profile.version.pk)
    priced["rate_version_id"] = str(profile.rates_version.pk)
    priced["tax_version_id"] = str(profile.tax_version.pk)
    priced["calculated"] = {
        "p_rate_paise": _str_or_none(result.p_rate_paise),
        "mrp_paise": _str_or_none(result.mrp_paise),
        "basic_paise": _str_or_none(result.basic_paise),
        "input_tax_pct": _dec_or_none(result.input_tax_pct),
        "output_tax_pct": _dec_or_none(result.output_tax_pct),
        "margin_pct": _dec_or_none(result.margin_pct),
        "pricing_margin_pct": _dec_or_none(result.pricing_margin_pct),
        "transport_pct": _dec_or_none(result.transport_pct),
        "direction": chosen,
    }
    priced["issues"] = issues
    return priced


# ---------------------------------------------------------------------------
# Line shape
# ---------------------------------------------------------------------------


def normalise_line(line: Any) -> dict[str, Any]:
    """The closed PtLine a client may supply, with typed values; server keys dropped."""
    if not isinstance(line, dict):
        raise _row_invalid("Each PT line must be an object.")
    key = _uuid_or_none(line.get("line_key") or uuid.uuid4())
    if key is None:
        raise _row_invalid("line_key must be a UUID.")
    unknown = set(line) - LINE_KEYS - SERVER_KEYS
    if unknown:
        raise _row_invalid(f"Unknown line field(s): {', '.join(sorted(unknown))}.", key)
    out = {k: line[k] for k in LINE_KEYS if k in line}
    out["line_key"] = str(key)
    out["attributes"] = _attributes(line.get("attributes"), key)
    out["coverage_requests"] = _coverage_requests(line.get("coverage_requests"), key)
    out["supplied"] = _supplied(line.get("supplied"), key)
    for name, check in _INTAKE_SHAPES.items():
        if name in line:
            out[name] = check(line[name], key)
    _check_references(out, key)
    _check_scalars(out, key)
    return out


_DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}$")


def _field_id(value: Any, key: uuid.UUID) -> str:
    """A field is an ID, or the vocabulary dimension it is named by - as SKU attributes
    name theirs (``colour``, ``size``), so a line and its item compare field by field."""
    if isinstance(value, str) and _DIMENSION_NAME.match(value):
        return value
    return _attribute_id(value, key)


def _attribute_id(value: Any, key: uuid.UUID) -> str:
    """An AttributeValues reference: a UUID or a positive decimal identifier."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit() and len(text) <= 20 and int(text) >= 1:
            return str(int(text))
        parsed = _uuid_or_none(text)
        if parsed is not None:
            return str(parsed)
    raise _row_invalid("An attribute field or vocabulary value must be an ID.", key)


def _attributes(value: Any, key: uuid.UUID) -> list[dict[str, Any]]:
    """The closed AttributeValues shape (design §5): never free-form JSON."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTRIBUTES:
        raise _row_invalid(f"attributes must be a list of at most {MAX_ATTRIBUTES} values.", key)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - ATTRIBUTE_KEYS or "field_id" not in item:
            raise _row_invalid(
                "An attribute is {field_id, vocabulary_value_id?, supplied_text?, unknown}.", key
            )
        unknown = item.get("unknown", False)
        vocabulary = item.get("vocabulary_value_id")
        text = item.get("supplied_text")
        if not isinstance(unknown, bool) or (
            text is not None and (not isinstance(text, str) or len(text) > ATTRIBUTE_TEXT)
        ):
            raise _row_invalid(
                f"unknown is true or false and supplied_text is at most {ATTRIBUTE_TEXT} "
                "characters.",
                key,
            )
        forms = int(vocabulary is not None) + int(text is not None)
        if forms != (0 if unknown else 1):
            raise _row_invalid("An attribute has exactly one value or is explicitly unknown.", key)
        entry: dict[str, Any] = {"field_id": _field_id(item["field_id"], key)}
        if entry["field_id"] in seen:
            raise _row_invalid("An attribute field appears twice on one line.", key)
        seen.add(entry["field_id"])
        if vocabulary is not None:
            entry["vocabulary_value_id"] = _attribute_id(vocabulary, key)
        if text is not None:
            entry["supplied_text"] = text
        entry["unknown"] = unknown
        out.append(entry)
    return out


def _text(value: Any, limit: int, what: str, key: uuid.UUID, *, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise _row_invalid(f"{what} must be text of at most {limit} characters.", key)
    return value


def _describing(value: Any, key: uuid.UUID) -> dict[str, Any]:
    """{brand_id?, design?}: the row's brand master and design, each present only when stated."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"brand_id", "design"}:
        raise _row_invalid("describing holds only brand_id and design.", key)
    out: dict[str, Any] = {}
    if "brand_id" in value:
        brand = value["brand_id"]
        parsed = _int_or_none(brand)
        if brand not in (None, "") and (parsed is None or parsed < 1):
            raise _row_invalid("brand_id must be an ID.", key)
        out["brand_id"] = parsed
    if "design" in value:
        design = _text(value["design"], DESIGN_TEXT, "design", key, nullable=True)
        out["design"] = design.strip() or None if isinstance(design, str) else None
    return out


def _origins(value: Any, key: uuid.UUID) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        column in items.LINE_COLUMNS and origin in ORIGINS for column, origin in value.items()
    ):
        raise _row_invalid("origins maps a KDPS column to file, rule, suggestion or person.", key)
    return {column: str(value[column]) for column in items.LINE_COLUMNS if column in value}


def _choice(value: Any, key: uuid.UUID) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"value_id", "value", "label", "reason"}:
        raise _row_invalid("A suggestion is {value_id, value, label, reason}.", key)
    return {name: _text(value[name], 240, name, key) for name in sorted(value)}


def _suggestions(value: Any, key: uuid.UUID) -> dict[str, dict[str, Any]]:
    """{column: {source, choices}}: what the file said and the matches waiting for a person."""
    if not isinstance(value, dict) or not set(value) <= set(items.LINE_COLUMNS):
        raise _row_invalid("suggestions are keyed by KDPS column.", key)
    out: dict[str, dict[str, Any]] = {}
    for column in items.LINE_COLUMNS:
        entry = value.get(column)
        if column not in value:
            continue
        if not isinstance(entry, dict) or set(entry) != {"source", "choices"}:
            raise _row_invalid("A column's suggestions are {source, choices}.", key)
        choices = entry["choices"]
        if not isinstance(choices, list) or len(choices) > 10:
            raise _row_invalid("A column holds at most ten suggestions.", key)
        out[column] = {
            "source": _text(entry["source"], 240, "source", key),
            "choices": [_choice(choice, key) for choice in choices],
        }
    return out


def _note(value: Any, key: uuid.UUID) -> dict[str, str]:
    if not isinstance(value, dict) or not {"code", "message"} <= set(value) <= {
        "code",
        "message",
        "field",
    }:
        raise _row_invalid("A note is {code, message, field?}.", key)
    note = {"code": _text(value["code"], 60, "code", key)}
    note["message"] = _text(value["message"], 300, "message", key)
    if "field" in value:
        note["field"] = _text(value["field"], 100, "field", key)
    return note


def _match(value: Any, key: uuid.UUID) -> dict[str, Any]:
    """{by, candidates, notes}: how the row's item was found and the server's notes."""
    if not isinstance(value, dict) or not set(value) <= {"by", "candidates", "notes"}:
        raise _row_invalid("match is {by, candidates, notes}.", key)
    by = value.get("by")
    if by is not None and by not in items.MATCH_KINDS:
        raise _row_invalid("match.by is not a known way of finding an item.", key)
    candidates = value.get("candidates") or []
    notes = value.get("notes") or []
    if not isinstance(candidates, list) or len(candidates) > items.MAX_CANDIDATES:
        raise _row_invalid("match.candidates is a short list of SKU IDs.", key)
    if not isinstance(notes, list) or len(notes) > 10:
        raise _row_invalid("match.notes is a short list.", key)
    parsed = [_uuid_or_none(candidate) for candidate in candidates]
    if None in parsed:
        raise _row_invalid("match.candidates holds SKU IDs.", key)
    return {
        "by": by,
        "candidates": [str(candidate) for candidate in parsed],
        "notes": [_note(note, key) for note in notes],
    }


_INTAKE_SHAPES = {
    "describing": _describing,
    "origins": _origins,
    "suggestions": _suggestions,
    "match": _match,
}


def _coverage_requests(value: Any, key: uuid.UUID) -> list[dict[str, Any]]:
    """Each request names exactly one source: receipt uses a custody lot, opening
    uses the approved manifest row (design §5.3)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _row_invalid("coverage_requests must be a list.", key)
    out: list[dict[str, Any]] = []
    for request in value:
        if not isinstance(request, dict) or set(request) - {"lot_id", "manifest_row_id", "qty"}:
            raise _row_invalid(
                "A coverage request names one custody lot or manifest row, and a quantity.", key
            )
        lot_id = _uuid_or_none(request.get("lot_id"))
        manifest_row_id = _uuid_or_none(request.get("manifest_row_id"))
        qty = request.get("qty")
        if (lot_id is None) == (manifest_row_id is None):
            raise _row_invalid(
                "A coverage request names exactly one of lot_id or manifest_row_id.", key
            )
        if isinstance(qty, bool) or not isinstance(qty, int):
            raise _row_invalid("A coverage request needs an integer qty.", key)
        if not 1 <= qty <= MAX_LINE_QTY:
            raise _row_invalid("A coverage request quantity must be 1 to 999,999.", key)
        entry: dict[str, Any] = {"qty": qty}
        if lot_id is not None:
            entry["lot_id"] = str(lot_id)
        else:
            entry["manifest_row_id"] = str(manifest_row_id)
        out.append(entry)
    return out


def _supplied(value: Any, key: uuid.UUID) -> dict[str, str | None]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - MONEY_COLUMNS:
        raise _row_invalid("supplied holds only BASIC, MRP and derived check values.", key)
    out: dict[str, str | None] = {}
    for column, amount in value.items():
        if amount in (None, ""):
            out[column] = None
            continue
        try:
            parsed = paise_from_json(amount)
        except MoneyInvalid as refused:
            raise _row_invalid(f"{column} {refused.message}.", key) from None
        out[column] = str(parsed)
    return out


def _check_references(out: dict[str, Any], key: uuid.UUID) -> None:
    for column in ("sku_id", "alias_id"):
        if out.get(column) in (None, ""):
            out[column] = None
            continue
        parsed = _uuid_or_none(out[column])
        if parsed is None:
            raise _row_invalid(f"{column} must be an ID.", key)
        out[column] = str(parsed)
    season = out.get("season_id")
    if season in (None, ""):
        out["season_id"] = None
    elif _int_or_none(season) is None or int(str(season)) < 1:
        raise _row_invalid("season_id must be an ID.", key)
    else:
        out["season_id"] = str(int(str(season)))


def _check_scalars(out: dict[str, Any], key: uuid.UUID) -> None:
    qty = out.get("qty")
    if qty is not None and (isinstance(qty, bool) or not isinstance(qty, int)):
        raise _row_invalid("qty must be an integer.", key)
    unknown_season = out.get("season_unknown_historical")
    if unknown_season is not None and not isinstance(unknown_season, bool):
        raise _row_invalid("season_unknown_historical must be true or false.", key)
    for column, limit in TEXT_LIMITS.items():
        value = out.get(column)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > limit:
            raise _row_invalid(f"{column} must be text of at most {limit} characters.", key)
        out[column] = value.strip() or None
    direction = out.get("direction_override")
    if direction not in (None, "") and direction not in goods_calc.DIRECTIONS:
        raise _row_invalid("direction_override is not a supported direction.", key)
    if direction == "":
        out["direction_override"] = None


def _unique_keys(lines: list[dict[str, Any]]) -> None:
    keys = [line["line_key"] for line in lines]
    if len(set(keys)) != len(keys):
        raise _row_invalid("Two lines share one line_key.")


# ---------------------------------------------------------------------------
# GRN custody for coverage
# ---------------------------------------------------------------------------


def grn_lots(grn_document_id: uuid.UUID) -> list[CustodyLot]:
    """The GRN's custody lots, then the lots its approved counter-GRNs opened.

    A counter-GRN increase adds a linked lot instead of editing the original count
    (design P02), and a supplement PT covers that lot, so it is the GRN's custody too.
    """
    counters = CounterGrnDraft.objects.filter(grn__document_id=grn_document_id).order_by(
        "document__created_at", "document_id"
    )
    lots: list[CustodyLot] = []
    for document_id in [grn_document_id, *counters.values_list("document_id", flat=True)]:
        lots.extend(
            CustodyLot.objects.filter(
                source_kind="grn", source_line__version__document_id=document_id
            ).order_by("source_line__line_no", "id")
        )
    return lots


PieceMap = dict[str, list[ranges.Interval]]


def source_conditions(grn_document_id: uuid.UUID) -> dict[str, str]:
    """The counted condition of each GRN line after approved counter-GRNs, by line key."""
    return {
        str(key): str(entry["condition"])
        for key, entry in receiving.grn_line_state(grn_document_id).items()
    }


def _pieces(queryset: Any) -> PieceMap:
    """Normalised portions per lot (keyed by lot ID text) of a portion-bearing queryset."""
    out: PieceMap = defaultdict(list)
    for lot_id, stored in queryset.values_list("lot_id", "portion"):
        out[str(lot_id)].append(bounds(stored))
    return {lot: ranges.normalise(pieces) for lot, pieces in out.items()}


def _coverable(
    lot_ids: list[uuid.UUID],
    receipt_kind: str,
    source_by_lot: dict[str, str],
) -> dict[tuple[str, str], list[ranges.Interval]]:
    """Per (lot, resolved SKU), the portions an ordinary receipt PT of this kind may cover.

    Overall PRD §15.2.1 rule 2: physically here, counted ``good`` at the GRN, resolved to a
    SKU, not live-covered and not under any active hold. Excess released
    by an approved ``accept_excess`` is coverable by a supplement only. Goods counted wrong
    or unidentified qualify only where a different person approved ``accept_wrong`` once
    their identity was resolved (CH-2026-09-24-01, ticket 05D) - resolving the identity
    alone never does - for a primary or a supplement alike. Goods counted damaged never
    qualify, and neither do good goods later found damaged - not even by the reissue of a
    reversed PT (change PRD §14.5 J5: damaged value uses only ``value_damage``). Read in a
    fixed number of queries whatever the number of lots.
    """
    good = [lot for lot in lot_ids if source_by_lot.get(str(lot)) == "good"]
    disputed = [lot for lot in lot_ids if source_by_lot.get(str(lot)) in receiving.WRONG_CONDITIONS]
    kept = receiving.accepted_wrong(disputed) if disputed else {}
    lots = [*good, *(lot for lot in disputed if kept.get(lot))]
    if not lots:
        return {}
    covered = _pieces(LiveCoverage.objects.filter(lot_id__in=lots))
    excluded = _pieces(ActiveHold.objects.filter(lot_id__in=lots))
    accepted = receiving.accepted_excess(good) if receipt_kind != SUPPLEMENT else {}
    by_key: dict[tuple[str, str], list[ranges.Interval]] = defaultdict(list)
    for lot_id, sku_id, stored in Position.objects.filter(
        lot_id__in=lots, boundary="physical", condition="good", sku_id__isnull=False
    ).values_list("lot_id", "sku_id", "portion"):
        by_key[(str(lot_id), str(sku_id))].append(bounds(stored))
    out: dict[tuple[str, str], list[ranges.Interval]] = {}
    for (lot, sku), pieces in by_key.items():
        taken = [
            *covered.get(lot, []),
            *excluded.get(lot, []),
            *accepted.get(uuid.UUID(lot), []),
        ]
        free = ranges.subtract(ranges.normalise(pieces), taken)
        if uuid.UUID(lot) in kept:
            free = ranges.intersect(free, kept[uuid.UUID(lot)])
        if free:
            out[(lot, sku)] = free
    return out


def coverable(
    lot_id: uuid.UUID,
    *,
    receipt_kind: str = PRIMARY,
    sku_id: Any = None,
) -> list[ranges.Interval]:
    """Portions of one lot a receipt PT of ``receipt_kind`` may cover, for one SKU or any."""
    lot = CustodyLot.objects.select_related("source_line__version").filter(pk=lot_id).first()
    if lot is None or lot.source_line is None:
        return []
    document = lot.source_line.version.document_id
    grn_document_id = (
        CounterGrnDraft.objects.filter(document_id=document)
        .values_list("grn__document_id", flat=True)
        .first()
        or document
    )
    source = source_conditions(grn_document_id).get(str(lot.source_line.stable_line_key), "")
    free = _coverable([lot_id], receipt_kind, {str(lot_id): source})
    return ranges.normalise(
        piece
        for (_lot, sku), pieces in free.items()
        if sku_id is None or sku == str(sku_id)
        for piece in pieces
    )


@dataclass
class ReceiptPool:
    """Everything one GRN offers ordinary receipt coverage of one kind, read once.

    Undecided excess is kept under the excess hold, so ``free`` already leaves it out;
    ``excess`` is only what could not be held yet, withheld per GRN line as a quantity.
    """

    receipt_kind: str
    lots: list[CustodyLot]
    line_of: dict[str, str]
    #: Counted condition per GRN line key.
    conditions: dict[str, str]
    free: dict[tuple[str, str], list[ranges.Interval]]
    excess: dict[str, int]
    _line_totals: dict[str, int] = dataclass_field(default_factory=dict, init=False)
    _skus: dict[str, list[str]] = dataclass_field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        totals: dict[str, int] = defaultdict(int)
        skus: dict[str, list[str]] = defaultdict(list)
        for (lot, sku), pieces in self.free.items():
            totals[self.line_of.get(lot, "")] += ranges.total(pieces)
            skus[lot].append(sku)
        self._line_totals = dict(totals)
        self._skus = {lot: sorted(found) for lot, found in skus.items()}

    def portions(self, lot_id: Any, sku_id: Any) -> list[ranges.Interval]:
        return self.free.get((str(lot_id), str(sku_id)), [])

    def skus(self, lot_id: Any) -> list[str]:
        return self._skus.get(str(lot_id), [])

    def line_room(self, line_key: str | None) -> int:
        key = line_key or ""
        return max(self._line_totals.get(key, 0) - self.excess.get(key, 0), 0)

    def take(self, lot_id: Any, sku_id: Any, qty: int) -> list[ranges.Interval]:
        """Remove and return ``qty`` pieces; ``ValueError`` when the pool cannot give them."""
        key = (str(lot_id), str(sku_id))
        line = self.line_of.get(key[0], "")
        if qty > self.line_room(line):
            raise ValueError("not enough eligible pieces on this GRN line")
        taken = ranges.take(self.free.get(key, []), qty)
        self.free[key] = ranges.subtract(self.free.get(key, []), taken)
        self._line_totals[line] = self._line_totals.get(line, 0) - qty
        return taken


def receipt_pool(grn_document_id: uuid.UUID, receipt_kind: str) -> ReceiptPool:
    """The pool of pieces a PT of ``receipt_kind`` may cover on this GRN."""
    grn = GoodsGrn.objects.select_related("arrival", "document").get(document_id=grn_document_id)
    lots = grn_lots(grn_document_id)
    keys = dict(
        OfficialLine.objects.filter(
            pk__in=[lot.source_line_id for lot in lots if lot.source_line_id]
        ).values_list("pk", "stable_line_key")
    )
    line_of = {str(lot.pk): str(keys[lot.source_line_id]) for lot in lots if lot.source_line_id}
    conditions = source_conditions(grn_document_id)
    source_by_lot = {lot: conditions.get(key, "") for lot, key in line_of.items()}
    return ReceiptPool(
        receipt_kind=receipt_kind,
        lots=lots,
        line_of=line_of,
        conditions=conditions,
        free=_coverable([lot.pk for lot in lots], receipt_kind, source_by_lot),
        excess=receiving.undecided_excess(grn),
    )


def coverage_lots(
    pool: ReceiptPool, claimed: Iterable[dict[str, Any]] = ()
) -> list[dict[str, Any]]:
    """Each counted lot with what is still free per SKU and per GRN line, after the
    coverage ``claimed`` lines already ask for. ``match_lot`` spends from it."""
    room = {key: pool.line_room(key) for key in set(pool.line_of.values())}
    lots: list[dict[str, Any]] = [
        {
            "lot": lot,
            "line_key": pool.line_of.get(str(lot.pk), ""),
            "free": {sku: ranges.total(pool.portions(lot.pk, sku)) for sku in pool.skus(lot.pk)},
            "room": room,
        }
        for lot in pool.lots
    ]
    by_id = {str(entry["lot"].pk): entry for entry in lots}
    for line in claimed:
        for request in line.get("coverage_requests") or []:
            entry = by_id.get(str(request.get("lot_id")))
            if entry is None:
                continue
            qty = int(request.get("qty") or 0)
            sku = str(line.get("sku_id"))
            if sku in entry["free"]:
                entry["free"][sku] -= qty
            room[entry["line_key"]] = room.get(entry["line_key"], 0) - qty
    return lots


def match_lot(lots: list[dict[str, Any]], sku_id: Any, qty: Any) -> list[dict[str, Any]]:
    """The first lot whose goods eligible for this PT are counted as ``sku_id`` (never another)."""
    if not sku_id or not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        return []
    for entry in lots:
        room = entry["room"]
        if entry["free"].get(str(sku_id), 0) >= qty and room.get(entry["line_key"], 0) >= qty:
            entry["free"][str(sku_id)] -= qty
            room[entry["line_key"]] -= qty
            return [{"lot_id": str(entry["lot"].pk), "qty": qty}]
    return []


def uncovered_reason(lots: list[dict[str, Any]], sku_id: Any, line: dict[str, Any]) -> str:
    """Why ``match_lot`` found no counted lot for a row, in plain words."""
    qty = line.get("qty")
    if not sku_id:
        return "No counted goods can be matched to this row until it names its item"
    if not isinstance(qty, int) or qty <= 0:
        return "The row has no quantity to match to counted goods"
    free = sum(
        max(min(entry["free"].get(str(sku_id), 0), entry["room"].get(entry["line_key"], 0)), 0)
        for entry in lots
    )
    if free == 0:
        return "No counted piece of this item on this GRN is free for this PT"
    if free < qty:
        return (
            f"Only {free} counted piece(s) of this item are free for this PT; the row claims "
            f"{qty}, and a PT never adds pieces the count did not find"
        )
    return f"No single counted lot holds all {qty} piece(s); split the row by lot"


def _requested_by_lot(lines: Iterable[dict[str, Any]]) -> dict[str, int]:
    requests: dict[str, int] = defaultdict(int)
    for line in lines:
        for request in line.get("coverage_requests") or []:
            requests[str(request["lot_id"])] += int(request["qty"])
    return requests


def _requested_by_sku(lines: Iterable[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Requested quantity per (lot, the requesting line's SKU)."""
    requests: dict[tuple[str, str], int] = defaultdict(int)
    for line in lines:
        for request in line.get("coverage_requests") or []:
            requests[(str(request["lot_id"]), str(line.get("sku_id")))] += int(request["qty"])
    return requests


def _hold_reason(kind: str) -> str:
    """Plain words for one hold kind, so a new kind reads sensibly without a code change."""
    if kind == receiving.EXCESS_HOLD:
        return "held as excess above the invoice"
    if "damage" in kind:
        return "under a damage hold"
    return f"under a {kind.replace('_', ' ')} hold"


def _held_reasons(lot_id: str, pool: ReceiptPool, covered: list[ranges.Interval]) -> list[str]:
    """Why a lot's uncovered goods are out of reach of this PT, for the refusal message."""
    reasons: set[str] = set()
    source = pool.conditions.get(pool.line_of.get(lot_id, ""))
    if source in receiving.WRONG_CONDITIONS:
        reasons.add(f"counted {source} and not accepted for a PT")
    elif source != "good":
        reasons.add(f"counted {source or 'without a GRN line'}")
    for position in Position.objects.filter(lot_id=uuid.UUID(lot_id), boundary="physical"):
        if not ranges.subtract([bounds(position.portion)], covered):
            continue
        if position.condition != "good":
            reasons.add(f"{position.condition}")
        if position.sku_id is None:
            reasons.add("identity unresolved")
    for kind in (
        ActiveHold.objects.filter(lot_id=uuid.UUID(lot_id))
        .values_list("kind", flat=True)
        .distinct()
    ):
        reasons.add(_hold_reason(str(kind)))
    if pool.receipt_kind != SUPPLEMENT and receiving.accepted_excess([uuid.UUID(lot_id)]):
        reasons.add("accepted excess, which goes on a supplement")
    return sorted(reasons)


@dataclass(frozen=True)
class _LotFacts:
    covered: PieceMap
    disposed: PieceMap
    physical: PieceMap

    @classmethod
    def load(cls, lot_ids: list[uuid.UUID]) -> _LotFacts:
        return cls(
            covered=_pieces(LiveCoverage.objects.filter(lot_id__in=lot_ids)),
            disposed=_pieces(
                Position.objects.filter(lot_id__in=lot_ids, boundary__in=["returned", "disposed"])
            ),
            physical=_pieces(Position.objects.filter(lot_id__in=lot_ids, boundary="physical")),
        )


def _reconcile_lot(
    lot: CustodyLot, pool: ReceiptPool, requests: dict[tuple[str, str], int], facts: _LotFacts
) -> dict[str, Any]:
    lot_key = str(lot.pk)
    live = facts.covered.get(lot_key, [])
    covered = ranges.total(live)
    disposed = ranges.total(facts.disposed.get(lot_key, []))
    uncovered = ranges.total(ranges.subtract(facts.physical.get(lot_key, []), live))
    mine = {sku: qty for (lot_id, sku), qty in requests.items() if lot_id == lot_key}
    proposed = sum(mine.values())
    held = lot.issued_qty - covered - disposed - proposed
    problems = []
    for _sku, qty in sorted(mine.items()):
        available = ranges.total(pool.portions(lot.pk, _sku))
        if qty <= available:
            continue
        if qty <= uncovered:
            # The pieces are here and uncovered, but this PT may not take them.
            reasons = _held_reasons(lot_key, pool, live) or ["not eligible for this PT"]
            problems.append(
                issue(
                    "COVERAGE_NOT_ELIGIBLE",
                    f"Only {available} piece(s) of this lot can go on this PT; the rest are "
                    f"{', '.join(reasons)}",
                    quantity=available,
                )
            )
        else:
            problems.append(
                issue(
                    "COVERAGE_EXCEEDS_COUNT",
                    f"Only {available} counted piece(s) of this lot can still be covered",
                    quantity=available,
                )
            )
    if held < 0 and not problems:
        problems.append(
            issue(
                "COVERAGE_EXCEEDS_COUNT",
                "The PT asks for more pieces of this lot than were counted",
                quantity=max(lot.issued_qty - covered - disposed, 0),
            )
        )
    return {
        "line_key": pool.line_of.get(lot_key),
        "lot_id": lot_key,
        "claimed_qty": None,
        "counted_qty": lot.issued_qty,
        "proposed_qty": proposed,
        "already_covered_qty": covered,
        "held_uncovered_qty": max(held, 0),
        "disposed_uncovered_qty": disposed,
        "issues": problems,
    }


def _excess_problems(
    pool: ReceiptPool, requests: dict[tuple[str, str], int]
) -> list[dict[str, Any]]:
    """A GRN line cannot give a PT the excess pieces that could not be held yet."""
    asked: dict[str, int] = defaultdict(int)
    for (lot_id, _sku), qty in requests.items():
        if lot_id in pool.line_of:
            asked[pool.line_of[lot_id]] += qty
    return [
        issue(
            "EXCESS_UNDECIDED",
            f"{pool.excess[line_key]} piece(s) counted above the invoice need an excess "
            f"decision before a PT can cover them; {pool.line_room(line_key)} can be covered now",
            line_key=line_key,
            quantity=pool.line_room(line_key),
        )
        for line_key, qty in sorted(asked.items())
        if pool.excess.get(line_key) and qty > pool.line_room(line_key)
    ]


def reconcile_receipt(
    grn_document_id: uuid.UUID,
    lines: list[dict[str, Any]],
    revision_hash: str,
    receipt_kind: str = PRIMARY,
) -> dict[str, Any]:
    """Proposed coverage against the GRN's counts, live coverage, holds and decisions."""
    pool = receipt_pool(grn_document_id, receipt_kind)
    requests = _requested_by_lot(lines)
    by_sku = _requested_by_sku(lines)
    lots = pool.lots
    facts = _LotFacts.load([lot.pk for lot in lots])
    unknown = sorted(set(requests) - {str(lot.pk) for lot in lots})
    out_lines = [_reconcile_lot(lot, pool, by_sku, facts) for lot in lots]
    keys = (
        "counted_qty",
        "proposed_qty",
        "already_covered_qty",
        "held_uncovered_qty",
        "disposed_uncovered_qty",
    )
    totals: dict[str, int | None] = {key: sum(line[key] for line in out_lines) for key in keys}
    issues = [i for line in out_lines for i in line["issues"]]
    issues += _excess_problems(pool, by_sku)
    issues += [
        issue("COVERAGE_SOURCE_INVALID", f"Lot {lot_id} is not counted on this GRN")
        for lot_id in unknown
    ]
    return {
        "purpose": RECEIPT,
        "revision_hash": revision_hash,
        "source_hashes": [],
        "lines": out_lines,
        "totals": {**totals, "claimed_qty": None, "value_paise": None},
        "passed": not issues,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------


def row_hash(line: dict[str, Any]) -> str:
    stable = {k: v for k, v in line.items() if k not in ("issues", "review_hash", "row_hash")}
    return content_hash(stable)


@dataclass
class _RowFacts:
    """What row validation reads, loaded once for all lines."""

    skus: dict[str, ProductSku]
    #: Seasons a *receipt* line may name: every real mapped season except the
    #: unknown historical one, which belongs to opening alone (store and
    #: warehouse operations PRD §4). Goods arriving from a vendor have a cohort;
    #: a receipt line that names the unknown one is refused SEASON_REQUIRED.
    seasons: set[str]
    #: The unknown historical season, when this tenant's master has one.
    unknown_seasons: set[str]
    #: (lot ID, SKU ID) pairs with physical custody.
    lot_skus: set[tuple[str, str]]
    identity_profiles: dict[uuid.UUID, Any] = dataclass_field(default_factory=dict)
    #: The PT document the validated revision belongs to (decision I2: its pending
    #: proposals are its own, whichever of its revisions proposed them).
    document_id: uuid.UUID | None = None

    @classmethod
    def load(cls, lines: list[dict[str, Any]], lot_ids: set[str]) -> _RowFacts:
        sku_ids = {_uuid_or_none(line.get("sku_id")) for line in lines} - {None}
        season_ids = {_int_or_none(line.get("season_id")) for line in lines} - {None}
        unknown = {
            str(pk)
            for pk in Season.objects.filter(historical_unknown=True).values_list("pk", flat=True)
        }
        requested = {
            uuid.UUID(str(request.get("lot_id")))
            for line in lines
            for request in line.get("coverage_requests") or []
            if str(request.get("lot_id")) in lot_ids
        }
        return cls(
            skus={
                str(sku.pk): sku
                for sku in ProductSku.objects.select_related(
                    "style", "identity_profile", "originating_revision"
                ).filter(pk__in=list(sku_ids))
            },
            seasons={
                str(pk)
                for pk in Season.objects.filter(pk__in=list(season_ids)).values_list(
                    "pk", flat=True
                )
            }
            - unknown,
            unknown_seasons=unknown,
            lot_skus={
                (str(lot_id), str(sku_id))
                for lot_id, sku_id in Position.objects.filter(
                    lot_id__in=list(requested), boundary="physical", sku_id__isnull=False
                )
                .values_list("lot_id", "sku_id")
                .distinct()
            },
        )

    def sku(self, line: dict[str, Any]) -> ProductSku | None:
        sku_id = _uuid_or_none(line.get("sku_id"))
        return self.skus.get(str(sku_id)) if sku_id else None

    def identity_profile(self, sku: ProductSku) -> Any:
        version_id = sku.identity_profile_id
        if version_id is None or sku.identity_profile is None:
            return None
        if version_id not in self.identity_profiles:
            self.identity_profiles[version_id] = profile_from_version(sku.identity_profile)
        return self.identity_profiles[version_id]


def validate_rows(
    lines: list[dict[str, Any]],
    *,
    revision_id: uuid.UUID | None,
    grn_document_id: uuid.UUID,
    profile: ProfileContext | None = None,
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not lines:
        problems.append(issue("NO_LINES", "A PT needs at least one line"))
    lot_ids = {str(lot.pk) for lot in grn_lots(grn_document_id)}
    facts = _RowFacts.load(lines, lot_ids)
    if revision_id is not None:
        facts.document_id = (
            DraftRevision.objects.filter(pk=revision_id)
            .values_list("document_id", flat=True)
            .first()
        )
    for line in lines:
        problems.extend(_line_problems(line, revision_id, lot_ids, profile, facts))
    return problems


def _line_problems(
    line: dict[str, Any],
    revision_id: uuid.UUID | None,
    lot_ids: set[str],
    profile: ProfileContext | None,
    facts: _RowFacts,
) -> list[dict[str, Any]]:
    key = line.get("line_key")
    problems: list[dict[str, Any]] = list(line.get("issues") or [])
    qty = line.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_LINE_QTY:
        problems.append(
            issue("QTY_INVALID", "Quantity must be 1 to 999,999", field="qty", line_key=key)
        )
    identity = _identity_problems(line, revision_id, facts)
    problems.extend(identity)
    if not identity:
        problems.extend(_profile_problems(line, profile, facts))
        problems.extend(_lot_sku_problems(line, lot_ids, facts))
    season = _int_or_none(line.get("season_id"))
    if season is None or str(season) not in facts.seasons:
        # Includes the unknown historical season: goods arriving from a vendor
        # have a cohort, so a receipt needs a real mapped season.
        problems.append(
            issue(
                "SEASON_REQUIRED",
                "A real mapped season is required",
                field="season_id",
                line_key=key,
            )
        )
    if line.get("season_unknown_historical"):
        problems.append(
            issue(
                "SEASON_UNKNOWN_MISMATCH",
                "Only an opening row may declare an unknown historical season",
                field="season_unknown_historical",
                line_key=key,
            )
        )
    if not line.get("hsn"):
        problems.append(issue("HSN_REQUIRED", "HSN is required", field="hsn", line_key=key))
    calculated = line.get("calculated") or {}
    if not _int_or_none(calculated.get("p_rate_paise")) or not _int_or_none(
        calculated.get("mrp_paise")
    ):
        problems.append(
            issue("VALUE_MISSING", "P RATE and MRP must be calculated and positive", line_key=key)
        )
    problems.extend(_coverage_problems(line, lot_ids))
    return problems


def _identity_problems(
    line: dict[str, Any], revision_id: uuid.UUID | None, facts: _RowFacts
) -> list[dict[str, Any]]:
    key = line.get("line_key")
    sku = facts.sku(line)
    if sku is None:
        return [
            issue(
                "IDENTITY_UNRESOLVED", "The line has no resolved SKU", field="sku_id", line_key=key
            )
        ]
    if sku.governance_state == "pending":
        # Decision I2: a proposal made while preparing this PT draft is this draft's own,
        # whichever of its revisions proposed it; another document never uses it. Either
        # way nothing submits or approves on an item the product-master owner has not
        # confirmed (design E127 step 10; store and warehouse operations PRD §5.4).
        origin = sku.originating_revision
        own = (
            revision_id is not None
            and origin is not None
            and (
                origin.pk == revision_id
                or (facts.document_id is not None and origin.document_id == facts.document_id)
            )
        )
        return [
            issue(
                "PROPOSAL_UNCONFIRMED",
                "New item - waiting for the product-master owner's confirmation"
                if own
                else "The SKU is another document's unconfirmed proposal",
                field="sku_id",
                line_key=key,
            )
        ]
    if sku.governance_state == "retired":
        message = (
            "The product-master owner rejected this new item; give the row another item"
            if sku.originating_revision_id is not None
            else "The SKU is retired"
        )
        return [issue("IDENTITY_RETIRED", message, field="sku_id", line_key=key)]
    return []


def _attribute_value(entry: dict[str, Any]) -> tuple[str, str] | None:
    if entry.get("vocabulary_value_id"):
        return ("value", str(entry["vocabulary_value_id"]))
    if entry.get("supplied_text") is not None:
        return ("text", normalise_text(str(entry["supplied_text"])))
    return None


def _profile_problems(
    line: dict[str, Any], profile: ProfileContext | None, facts: _RowFacts
) -> list[dict[str, Any]]:
    """The line's SKU belongs to the PT profile's family, and the line does not contradict it.

    The SKU's identity profile (when it names one) must be of the same family with every
    defining attribute known (size may stay unknown, as the identity rules allow); an
    attribute the line states for a field the SKU defines must be the SKU's own value
    (overall PRD §15.2.1 rule 2).
    """
    sku = facts.sku(line)
    if profile is None or sku is None:
        return []
    key = line.get("line_key")
    family = str((profile.version.payload or {}).get("family") or "")
    mismatch = issue(
        "IDENTITY_PROFILE_MISMATCH",
        "The SKU is not of the product family this PT's profile is for",
        field="sku_id",
        line_key=key,
    )
    if not family or sku.style.profile_family != family:
        return [mismatch]
    by_field = {str(e.get("field_id")): e for e in sku.attrs or [] if isinstance(e, dict)}
    problems: list[dict[str, Any]] = []
    defining: tuple[str, ...] = ()
    if sku.identity_profile_id is not None:
        identity_profile = facts.identity_profile(sku)
        if identity_profile is None or identity_profile.family != family:
            return [mismatch]
        defining = identity_profile.distinguishing
        problems.extend(
            issue(
                "DEFINING_ATTRIBUTE_UNKNOWN",
                f"The SKU's defining {dimension} is not known",
                field="sku_id",
                line_key=key,
            )
            for dimension in defining
            if dimension != identity_profile.size_dimension
            and _attribute_value(by_field.get(dimension) or {}) is None
        )
    problems.extend(_attribute_conflicts(line, by_field, defining))
    return problems


def _attribute_conflicts(
    line: dict[str, Any], by_field: dict[str, dict[str, Any]], defining: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Attributes the line states differently from its SKU; unknown conflicts on defining ones."""
    key = line.get("line_key")
    problems: list[dict[str, Any]] = []
    for entry in line.get("attributes") or []:
        own = by_field.get(str(entry.get("field_id")))
        if own is None or _attribute_value(own) is None:
            continue
        stated = _attribute_value(entry)
        if stated != _attribute_value(own) and (
            stated is not None or entry["field_id"] in defining
        ):
            problems.append(
                issue(
                    "ATTRIBUTE_MISMATCH",
                    f"The line's {entry['field_id']} is not the SKU's",
                    field="attributes",
                    line_key=key,
                )
            )
    return problems


def _lot_sku_problems(
    line: dict[str, Any], lot_ids: set[str], facts: _RowFacts
) -> list[dict[str, Any]]:
    """A PT line covers only custody counted or resolved as its own SKU; no relabelling."""
    sku_id = str(_uuid_or_none(line.get("sku_id")))
    requested = {
        str(request.get("lot_id"))
        for request in line.get("coverage_requests") or []
        if str(request.get("lot_id")) in lot_ids
    }
    return [
        issue(
            "COVERAGE_SKU_MISMATCH",
            "A PT line must use the SKU its counted goods resolved to",
            field="sku_id",
            line_key=line.get("line_key"),
        )
        for lot_id in sorted(requested)
        if (lot_id, sku_id) not in facts.lot_skus
    ]


def _coverage_problems(line: dict[str, Any], lot_ids: set[str]) -> list[dict[str, Any]]:
    key = line.get("line_key")
    requests = line.get("coverage_requests") or []
    problems = [
        issue("COVERAGE_SOURCE_INVALID", "Coverage must use a lot of this GRN", line_key=key)
        for request in requests
        if str(request.get("lot_id")) not in lot_ids
    ]
    total = sum(int(request.get("qty") or 0) for request in requests)
    if isinstance(line.get("qty"), int) and total != line["qty"]:
        problems.append(
            issue(
                "COVERAGE_QTY_MISMATCH",
                "Coverage requests must add up to the line quantity",
                line_key=key,
            )
        )
    return problems


def checked_lines(
    run: CommandRun,
    payload: dict[str, Any],
    lines: list[dict[str, Any]],
    profile: ProfileContext,
    *,
    stale_code: str,
) -> list[dict[str, Any]]:
    """Re-price every stored line under its pinned profile; a changed result is stale."""
    direction = str(payload.get("direction") or goods_calc.BASE_TO_TICKET)
    fresh: list[dict[str, Any]] = []
    for line in lines:
        priced = price_line(normalise_line(line), profile, direction)
        if priced["calculated"] != line.get("calculated"):
            raise Refusal(
                stale_code,
                "A row's calculated values no longer match its pinned profile; reprice and review.",
                status=422 if stale_code == "PT_ROWS_INVALID" else None,
                issues=[issue("ROW_STALE", "Reprice this row", line_key=line.get("line_key"))],
            )
        fresh.append(priced)
    return fresh


# ---------------------------------------------------------------------------
# Site and parent checks
# ---------------------------------------------------------------------------


def check_contract(site_id: int) -> SiteGuard:
    guard = SiteGuard.objects.filter(site_id=site_id).first()
    if guard is None or guard.stock_contract != SiteGuard.StockContract.GOODS_V1:
        raise Refusal("CONTRACT_DISABLED", "This site still runs the legacy stock contract.")
    return guard


def check_site_for_goods(site_id: int) -> None:
    """Approval-time fence: goods-v1 contract, goods capability, no count freeze."""
    guard = check_contract(site_id)
    if not guard.goods_ready:
        raise Refusal("SITE_NOT_READY", "This site is not approved for goods work.")
    if guard.freeze_id:
        raise Refusal("UNDER_COUNT", "A count freezes this site.")


def _check_grn_parent(grn: GoodsGrn) -> None:
    head = DocumentHead.objects.filter(document_id=grn.document_id).first()
    if head is None or head.state != DocumentHead.State.OFFICIAL or head.live_version_id is None:
        raise Refusal("PT_PARENT_INVALID", "A receipt PT needs an issued GRN.", status=422)
    check_contract(grn.document.held_site_id)


def _check_cardinality(grn: GoodsGrn, receipt_kind: str, duplicate_code: str) -> None:
    has_primary = GoodsPt.objects.filter(grn=grn, receipt_kind="primary").exists()
    if receipt_kind == "primary" and has_primary:
        status = 422 if duplicate_code == "PT_PARENT_INVALID" else None
        raise Refusal(
            duplicate_code, "This GRN already has its primary PT; use a supplement.", status=status
        )
    if receipt_kind == "supplement" and not has_primary:
        raise Refusal("PT_PARENT_INVALID", "A supplement follows the GRN's primary PT.", status=422)


def draft_payload(head: DocumentHead) -> dict[str, Any]:
    revision = head.draft_revision
    return dict(revision.payload) if revision is not None else {}


def require_editable(head: DocumentHead) -> DraftRevision:
    if head.state in (DocumentHead.State.OFFICIAL, DocumentHead.State.REVERSED):
        raise Refusal("OFFICIAL_IMMUTABLE", "An official or reversed PT cannot be edited.")
    if head.draft_revision is None:
        raise Refusal("STATE_CONFLICT", "This PT has no draft to edit.")
    return head.draft_revision


# ---------------------------------------------------------------------------
# Draft commands
# ---------------------------------------------------------------------------


def create_receipt_draft(
    run: CommandRun,
    *,
    grn: GoodsGrn,
    receipt_kind: str,
    profile: ProfileContext,
    direction: str,
    lines: list[Any] | None,
    source: str,
    evidence_id: uuid.UUID | None,
    duplicate_code: str = "PRIMARY_EXISTS",
) -> tuple[DocumentIdentity, DocumentHead, GoodsPt]:
    """E122/E123: a numberless draft on its GRN; no stock or books effect."""
    _check_grn_parent(grn)
    _check_cardinality(grn, receipt_kind, duplicate_code)
    if direction not in profile.directions:
        raise Refusal("PROFILE_INVALID", "The profile does not allow this direction.", status=422)
    parent = grn.document
    identity, head = new_document(
        run,
        kind=RECEIPT_DOC_TYPE,
        purpose=RECEIPT,
        entity_id=parent.entity_id,
        site_id=parent.site_id,
    )
    goods_pt = GoodsPt.objects.create(
        tenant_id=run.tenant_id,
        document=identity,
        grn=grn,
        receipt_kind=receipt_kind,
        preparer_id=identity.maker_id,
    )
    source_lines = prefill_from_grn(grn, receipt_kind) if lines is None else lines
    priced = [price_line(normalise_line(line), profile, direction) for line in source_lines]
    _unique_keys(priced)
    header = {
        "purpose": RECEIPT,
        "receipt_kind": receipt_kind,
        "grn_id": str(grn.document_id),
        "site_id": str(parent.site_id),
        "profile_version_id": str(profile.version.pk),
        "direction": direction,
        "source": source,
        "source_evidence_ids": [str(evidence_id)] if evidence_id else [],
        "disposition_ids": [],
    }
    append_revision(
        run,
        head,
        header=header,
        replace_lines=[(uuid.UUID(line["line_key"]), line) for line in priced],
    )
    record_event(run, identity.pk, "draft_created", payload={"to_state": "draft", "details": []})
    run.audit_subject_key = f"document:{identity.pk}"
    run.audit_site_id = parent.site_id
    return identity, head, goods_pt


def prefill_from_grn(grn: GoodsGrn, receipt_kind: str = PRIMARY) -> list[dict[str, Any]]:
    """One line per GRN lot and resolved SKU, for the quantity this PT kind may cover.

    Damaged, held and undecided excess goods are never offered, and neither are wrong or
    unidentified goods until their acceptance is approved (ticket 05D); accepted excess is
    offered to a supplement only.
    """
    pool = receipt_pool(grn.document_id, receipt_kind)
    room = {key: pool.line_room(key) for key in set(pool.line_of.values())}
    lines: list[dict[str, Any]] = []
    for lot in pool.lots:
        line_key = pool.line_of.get(str(lot.pk), "")
        identity = lot.initial_identity or {}
        for sku_id in pool.skus(lot.pk):
            qty = min(ranges.total(pool.portions(lot.pk, sku_id)), room.get(line_key, 0))
            if qty <= 0:
                continue
            room[line_key] -= qty
            counted_as = str(identity.get("sku_id")) == sku_id
            lines.append(
                {
                    "line_key": str(uuid.uuid4()),
                    "sku_id": sku_id,
                    "attributes": (identity.get("attributes") or []) if counted_as else [],
                    "alias_as_used": (
                        str(identity["raw_alias"])[:128] if identity.get("raw_alias") else None
                    ),
                    "qty": qty,
                    "coverage_requests": [{"lot_id": str(lot.pk), "qty": qty}],
                    "supplied": {},
                    "source_ref": (
                        str(identity["description"])[:200] if identity.get("description") else None
                    ),
                }
            )
    return lines


def current_lines(run: CommandRun, head: DocumentHead) -> list[dict[str, Any]]:
    return [dict(state.payload) for state in pending_lines(run, head)]


def reviewed_map(head: DocumentHead) -> dict[str, dict[str, Any]]:
    if head.draft_revision is None:
        return {}
    return {str(r["line_key"]): r for r in head.draft_revision.reviewed_rows or []}


def edit_rows(
    run: CommandRun,
    head: DocumentHead,
    *,
    updates: list[Any],
    reviews: list[Any],
) -> None:
    """E124: supplied cells change, rows re-price, review marks of changed rows clear."""
    revision = require_editable(head)
    header = dict(revision.payload)
    profile = load_profile(
        run.tenant_id, header.get("profile_version_id"), document_target(head.document_id)
    )
    if head.state == DocumentHead.State.SUBMITTED:
        supersede_pending(run, "document", str(head.document_id), APPROVE_RECEIPT)
    lines = {str(line["line_key"]): line for line in current_lines(run, head)}
    changed = _apply_updates(run, header, lines, updates, profile, head=head)
    kept = _review_marks(run, head, lines, changed, reviews)
    revision = append_revision(run, head, header=header, line_changes=changed, reviewed_rows=kept)
    record_event(
        run,
        head.document_id,
        "draft_corrected",
        revision_id=revision.pk,
        payload={"to_state": head.state, "details": []},
    )


def _apply_updates(
    run: CommandRun,
    header: dict[str, Any],
    lines: dict[str, dict[str, Any]],
    updates: list[Any],
    profile: ProfileContext,
    *,
    head: DocumentHead | None = None,
) -> dict[uuid.UUID, dict[str, Any] | None]:
    direction = str(header.get("direction") or goods_calc.BASE_TO_TICKET)
    changed: dict[uuid.UUID, dict[str, Any] | None] = {}
    edits = _EditContext(run, header, profile, head)
    paste = _PasteContext(lines)
    for update in updates:
        if not isinstance(update, dict) or set(update) - UPDATE_KEYS:
            raise _row_invalid("A row edit is {line_key, fields} or {line_key, canonical}.")
        key = _uuid_or_none(update.get("line_key"))
        if key is None:
            raise _row_invalid("line_key must be a UUID.")
        if "canonical" in update:
            pasted = paste.apply(run, edits, key, update)
            if pasted is not None:
                changed[key] = lines[str(key)] = price_line(pasted, profile, direction)
            continue
        if update.get("delete"):
            if lines.pop(str(key), None) is not None:
                changed[key] = None
            continue
        before = dict(lines.get(str(key)) or {"line_key": str(key)})
        line = dict(before)
        edited = _apply_fields(line, update.get("fields"), key)
        line = normalise_line(line)
        edits.settle(before, line, edited, lines)
        priced = price_line(line, profile, direction)
        changed[key] = priced
        lines[str(key)] = priced
    paste.finish(edits)
    return changed


class _PasteContext:
    """The pasted rows of one E124 edit (ticket 06A; ``goods_paste``).

    Rows are numbered as the grid shows them - the draft's own order - so every
    refused cell names the row the preparer sees. Problems are gathered across the
    whole paste and refuse it once, as a canonical upload refuses its whole file.
    """

    def __init__(self, lines: dict[str, dict[str, Any]]) -> None:
        self.lines = lines
        self.rows = {key: number for number, key in enumerate(lines, start=1)}
        self.problems: list[dict[str, Any]] = []
        #: Rows whose quantity went up, by row number: checked against the count at the end.
        self.raised: dict[str, int] = {}

    def apply(
        self, run: CommandRun, edits: _EditContext, key: uuid.UUID, update: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The pasted row, normalised; ``None`` when the paste left it as it was."""
        from ptmapper import goods_paste

        if "fields" in update or update.get("delete") or update.get("source_evidence_id"):
            # Source evidence belongs to typed or uploaded cells; a paste is the person's own.
            raise _row_invalid("A row edit gives typed fields or pasted cells, not both.", key)
        if not edits.is_receipt:
            raise _row_invalid("Canonical values are pasted into a receipt PT's rows only.", key)
        problem = goods_paste.shape_problem(update["canonical"])
        if problem:
            raise _row_invalid(problem, key)
        if str(key) not in self.lines:
            raise _row_invalid(
                "A paste fills the rows already on this PT, one per counted lot; it never "
                "adds a row.",
                key,
            )
        before = dict(self.lines[str(key)])
        line = dict(before)
        row = self.rows[str(key)]
        edited = goods_paste.paste_row(run.tenant_id, line, update["canonical"], row, self.problems)
        line = normalise_line(line)
        if (line.get("qty") or 0) > (before.get("qty") or 0):
            self.raised[str(key)] = row
        edits.mark_person(before, line, edited)
        if {k: v for k, v in line.items() if k not in SERVER_KEYS} == {
            k: v for k, v in before.items() if k not in SERVER_KEYS
        }:
            return None  # the same values again: the row keeps its review mark
        return line

    def finish(self, edits: _EditContext) -> None:
        from ptmapper import goods_paste

        if self.raised:
            self.problems += goods_paste.room_problems(edits.pool(), self.lines, self.raised)
        if self.problems:
            raise Refusal(
                "ROW_INVALID",
                "Some pasted cells cannot be used; nothing was saved.",
                status=422,
                issues=self.problems[:1000],
            )


def _apply_fields(line: dict[str, Any], fields: Any, key: uuid.UUID) -> set[str]:
    """Apply one row's cell edits; the set of column keys it named."""
    if not isinstance(fields, list):
        raise _row_invalid("fields must be a list.", key)
    edited: set[str] = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {"column_key", "value"}:
            raise _row_invalid("A field edit is {column_key, value}.", key)
        column = field.get("column_key")
        value = field.get("value")
        if column in CALCULATED_COLUMNS:
            raise _row_invalid(f"{column} is calculated, not editable.", key)
        if column not in EDITABLE_COLUMNS:
            raise _row_invalid(f"{column} is not an editable PT column.", key)
        edited.add(str(column))
        if column in MONEY_COLUMNS:
            line["supplied"] = {**(line.get("supplied") or {}), column: value}
        elif column == "qty":
            parsed = _int_or_none(value)
            if value not in (None, "") and parsed is None:
                raise _row_invalid("qty must be an integer.", key)
            line["qty"] = parsed
        elif column in ("brand_id", "design"):
            line["describing"] = {**(line.get("describing") or {}), str(column): value}
        else:
            line[str(column)] = value
    return edited


def _column_value(line: dict[str, Any], column: str) -> Any:
    if column in MONEY_COLUMNS:
        return (line.get("supplied") or {}).get(column)
    if column in ("brand_id", "design"):
        return (line.get("describing") or {}).get(column)
    return line.get(column)


def _changed_columns(before: dict[str, Any], after: dict[str, Any], edited: set[str]) -> set[str]:
    """The KDPS columns (PRD order names) whose value this edit changed."""
    out = {
        items.EDIT_COLUMNS[column]
        for column in edited & set(items.EDIT_COLUMNS)
        if _column_value(before, column) != _column_value(after, column)
    }
    if "attributes" in edited:
        old = items.by_field(before.get("attributes") or [])
        new = items.by_field(after.get("attributes") or [])
        out |= {
            items.DIMENSION_COLUMNS[dimension]
            for dimension in set(old) | set(new)
            if dimension in items.DIMENSION_COLUMNS
            and items.attribute_value(old.get(dimension))
            != items.attribute_value(new.get(dimension))
        }
    return out


class _EditContext:
    """What settling a row edit reads - the draft's lineage, its GRN's pool of counted
    pieces and the candidate items - each read once, only when an edit needs it."""

    def __init__(
        self, run: CommandRun, header: dict[str, Any], profile: ProfileContext, head: Any
    ) -> None:
        self.run = run
        self.header = header
        self.profile = profile
        self.head = head
        self._lineage: list[uuid.UUID] | None = None
        self._index: items.ItemIndex | None = None
        self._pool: ReceiptPool | None = None

    @property
    def lineage(self) -> list[uuid.UUID]:
        if self._lineage is None:
            from masters.goods_identity_services import lineage_revision_ids

            revision = getattr(self.head, "draft_revision_id", None)
            self._lineage = lineage_revision_ids(self.run.tenant_id, revision)
        return self._lineage

    @property
    def index(self) -> items.ItemIndex:
        if self._index is None:
            family = str((self.profile.version.payload or {}).get("family") or "")
            self._index = items.ItemIndex(self.run.tenant_id, family, self.run.now, self.lineage)
        return self._index

    @property
    def is_receipt(self) -> bool:
        return self.header.get("purpose") == RECEIPT and bool(self.header.get("grn_id"))

    def pool(self) -> ReceiptPool:
        if self._pool is None:
            self._pool = receipt_pool(
                uuid.UUID(str(self.header["grn_id"])),
                str(self.header.get("receipt_kind") or PRIMARY),
            )
        return self._pool

    def settle(
        self,
        before: dict[str, Any],
        line: dict[str, Any],
        edited: set[str],
        lines: dict[str, dict[str, Any]],
    ) -> None:
        """A person's edit: its cells become *person*, its item and coverage follow it."""
        columns = self.mark_person(before, line, edited)
        brand = (line.get("describing") or {}).get("brand_id")
        if "brand_id" in edited and brand and not Brand.objects.filter(pk=brand).exists():
            raise _row_invalid("That brand does not exist.", line.get("line_key"))
        moved = self._settle_item(before, line, edited, columns)
        self._settle_coverage(before, line, edited, lines, moved=moved)

    def mark_person(
        self, before: dict[str, Any], line: dict[str, Any], edited: set[str]
    ) -> set[str]:
        """The KDPS columns this edit changed become the person's own, and the server's
        notes on the fields it set are settled. Typed and pasted edits alike."""
        columns = _changed_columns(before, line, edited)
        if columns:
            origins = dict(line.get("origins") or {})
            suggestions = dict(line.get("suggestions") or {})
            for column in columns:
                origins[column] = "person"
                suggestions.pop(column, None)
            line["origins"] = origins
            line["suggestions"] = suggestions
        if line.get("match"):
            for column in edited - {"sku_id"}:
                if column == "coverage_requests" or column in _NOTED_FIELDS:
                    for note in list(line["match"].get("notes") or []):
                        if note.get("field") == column:
                            items.set_note(line, note["code"], None, field=column)
        return columns

    def _settle_item(
        self, before: dict[str, Any], line: dict[str, Any], edited: set[str], columns: set[str]
    ) -> bool:
        """Follow the row to its item; whether the server moved it to another one."""
        sku_id = _uuid_or_none(line.get("sku_id"))
        if "sku_id" in edited:
            if sku_id is not None and items.pending_elsewhere(sku_id, self.lineage):
                raise _row_invalid(
                    "That item is another document's unconfirmed proposal; it cannot be used "
                    "on this PT.",
                    line.get("line_key"),
                )
            if line.get("match"):
                items.apply_match(line, items.ItemMatch(items.BY_PERSON, _str_uuid(sku_id)))
            return False
        describing_columns = {
            items.EDIT_COLUMNS.get(column, "") for column in items.DESCRIBING_EDITS
        } | set(items.DIMENSION_COLUMNS.values())
        if not columns & describing_columns:
            return False
        current = (
            ProductSku.objects.select_related("style", "identity_profile").filter(pk=sku_id).first()
            if sku_id
            else None
        )
        brand_id, design = items.describing(line, current)
        attributes = items.identity_attributes(line, current)
        stated = items.by_field(attributes)
        if (
            current is not None
            and "BARCODE" not in columns
            and current.style.brand_id == brand_id
            and design is not None
            and normalise_text(current.style.style_code) == normalise_text(design)
            and items.fits(current, stated, self.index)
        ):
            return False  # the row still describes the item it is
        barcode = line.get("alias_as_used") if "BARCODE" in columns else None
        barcodes = (
            items.barcode_skus(
                self.run.tenant_id,
                [barcode],
                site_id=self._site_id(),
                as_of=self.run.now,
                lineage=self.lineage,
            )
            if barcode
            else {}
        )
        found = items.match_item(
            barcode=barcode,
            brand_id=brand_id,
            design=design,
            attributes=attributes,
            index=self.index,
            barcodes=barcodes,
        )
        items.apply_match(line, found)
        return str(line.get("sku_id")) != str(before.get("sku_id"))

    def _site_id(self) -> int | None:
        raw = self.header.get("site_id")
        return int(str(raw)) if raw else None

    def _settle_coverage(
        self,
        before: dict[str, Any],
        line: dict[str, Any],
        edited: set[str],
        lines: dict[str, dict[str, Any]],
        *,
        moved: bool,
    ) -> None:
        """A row whose item or (intake) quantity changed is matched to counted lots again,
        as the brand-file intake matches it - unless the person set its coverage."""
        if not self.is_receipt or "coverage_requests" in edited:
            return
        sku_changed = str(line.get("sku_id")) != str(before.get("sku_id"))
        qty_changed = line.get("qty") != before.get("qty")
        if not (moved or (line.get("match") and (sku_changed or qty_changed))):
            return
        others = [other for key, other in lines.items() if key != line["line_key"]]
        lots = coverage_lots(self.pool(), others)
        sku_id = line.get("sku_id")
        line["coverage_requests"] = match_lot(lots, sku_id, line.get("qty"))
        reason = None if line["coverage_requests"] else uncovered_reason(lots, sku_id, line)
        items.set_note(line, items.NOTE_UNCOVERED, reason, field="coverage_requests")


#: Line fields a server note may be about; editing the field settles its note.
_NOTED_FIELDS = frozenset(
    {"season_id", "qty", "basic_paise", "mrp_paise", "check_p_rate_paise", "hsn", "brand_id"}
)


def _str_uuid(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _review_marks(
    run: CommandRun,
    head: DocumentHead,
    lines: dict[str, dict[str, Any]],
    changed: dict[uuid.UUID, dict[str, Any] | None],
    reviews: list[Any],
) -> list[dict[str, Any]]:
    kept = [
        review
        for key, review in reviewed_map(head).items()
        if key in lines
        and uuid.UUID(key) not in changed
        and review.get("row_hash") == row_hash(lines[key])
    ]
    for review in reviews:
        if not isinstance(review, dict):
            raise Refusal("INVALID_REQUEST", "reviewed_rows holds {line_key, row_hash} objects.")
        key = str(review.get("line_key"))
        if key not in lines or review.get("row_hash") != row_hash(lines[key]):
            raise Refusal(
                "REVISION_SUPERSEDED", "A reviewed row changed before your review was saved."
            )
        kept = [r for r in kept if str(r["line_key"]) != key]
        kept.append(
            {
                "line_key": key,
                "row_hash": review["row_hash"],
                "reviewed_by": str(run.principal.human_id),
                "reviewed_at": run.now.isoformat(),
            }
        )
    return kept


def reprice(
    run: CommandRun, head: DocumentHead, *, profile_version_id: Any, direction: str
) -> None:
    """E125/E126: a full new revision under an approved profile; all review marks clear."""
    revision = require_editable(head)
    profile = load_profile(run.tenant_id, profile_version_id, document_target(head.document_id))
    if direction not in profile.directions:
        raise Refusal("PROFILE_INVALID", "The profile does not allow this direction.", status=422)
    if head.state == DocumentHead.State.SUBMITTED:
        supersede_pending(run, "document", str(head.document_id), APPROVE_RECEIPT)
    header = {
        **revision.payload,
        "profile_version_id": str(profile.version.pk),
        "direction": direction,
    }
    priced = [
        price_line(normalise_line(line), profile, direction) for line in current_lines(run, head)
    ]
    revision = append_revision(
        run,
        head,
        header=header,
        replace_lines=[(uuid.UUID(line["line_key"]), line) for line in priced],
        reviewed_rows=[],
    )
    record_event(
        run,
        head.document_id,
        "repriced",
        revision_id=revision.pk,
        payload={"to_state": head.state, "details": []},
    )


def submit(
    run: CommandRun, head: DocumentHead, *, reviewed_hash: str, goods_pt: GoodsPt
) -> tuple[dict[str, Any], ApprovalRequest]:
    """E127: the exact reviewed revision goes to a distinct checker."""
    revision = head.draft_revision
    if revision is None or head.state != DocumentHead.State.DRAFT:
        raise Refusal("STATE_CONFLICT", "Only a draft can be submitted.")
    grn = goods_pt.grn
    if grn is None:
        raise Refusal("PT_PARENT_INVALID", "A receipt PT needs its GRN.", status=422)
    if reviewed_hash != revision.content_hash:
        raise Refusal("REVISION_SUPERSEDED", "What you reviewed is no longer the current draft.")
    lines = current_lines(run, head)
    _require_reviewed(head, lines)
    target = pt_target(grn, [brand for _site, brand in pt_cells(goods_pt)])
    profile = load_profile(
        run.tenant_id, revision.payload.get("profile_version_id"), target, code="PT_ROWS_INVALID"
    )
    fresh = checked_lines(run, revision.payload, lines, profile, stale_code="REVIEW_INCOMPLETE")
    problems = validate_rows(
        fresh, revision_id=revision.pk, grn_document_id=grn.document_id, profile=profile
    )
    if problems:
        raise Refusal(
            "PT_ROWS_INVALID", "Some rows are not valid yet.", status=422, issues=problems[:1000]
        )
    reconciliation = reconcile_receipt(
        grn.document_id,
        fresh,
        revision.content_hash,
        goods_pt.receipt_kind or PRIMARY,
    )
    run.reconciliation = reconciliation
    if not reconciliation["passed"]:
        raise Refusal(
            "RECONCILIATION_FAILED",
            "The PT does not reconcile with the counted GRN quantities.",
            issues=reconciliation["issues"][:1000],
        )
    policy = pin(
        run,
        action=APPROVE_RECEIPT,
        purpose=RECEIPT,
        site_id=head.document.site_id,
        brand_ids=list(target.brand_ids),
        amounts=pt_amounts(fresh),
    )
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    request = create_request(
        run,
        subject_kind="document",
        subject_key=str(head.document_id),
        revision=head.revision,
        reviewed_hash=revision.content_hash,
        requested_action=APPROVE_RECEIPT,
        site_id=head.document.site_id,
        brand_id=grn.arrival.brand_id,
        reconciliation=reconciliation,
        title=f"Receipt PT for GRN {grn.document.official_number or grn.document_id}",
        policy=policy,
    )
    resolve_exceptions(run, kind="grn_awaiting_pt", subject_key=f"grn:{grn.document_id}")
    run.audit_subject_key = f"document:{head.document_id}"
    return reconciliation, request


def pt_amounts(lines: Iterable[dict[str, Any]]) -> Amounts:
    """Pieces and cost value (quantity × P RATE); the value is unknown if any rate is."""
    qty = 0
    value: int | None = 0
    for line in lines:
        pieces = int(line.get("qty") or 0)
        qty += pieces
        rate = (line.get("calculated") or {}).get("p_rate_paise")
        if value is not None:
            value = None if rate in (None, "") else value + pieces * int(str(rate))
    return Amounts(qty, value)


def _require_reviewed(head: DocumentHead, lines: list[dict[str, Any]]) -> None:
    reviews = reviewed_map(head)
    unreviewed = [
        line["line_key"]
        for line in lines
        if reviews.get(str(line["line_key"]), {}).get("row_hash") != row_hash(line)
    ]
    if unreviewed:
        raise Refusal(
            "REVIEW_INCOMPLETE",
            f"{len(unreviewed)} row(s) are not reviewed at their current values.",
            issues=[
                issue("ROW_NOT_REVIEWED", "Review this row", line_key=key)
                for key in unreviewed[:1000]
            ],
        )


def recall(run: CommandRun, head: DocumentHead, *, reason_code: str, goods_pt: GoodsPt) -> None:
    """E128: the preparer withdraws; the reviewed revision is kept."""
    if head.state != DocumentHead.State.SUBMITTED:
        raise Refusal("STATE_CONFLICT", "Only a submitted PT can be withdrawn.")
    if goods_pt.preparer_id != run.principal.human_id:
        raise Refusal("STATE_CONFLICT", "Only the preparer can withdraw a submitted PT.")
    supersede_pending(run, "document", str(head.document_id), APPROVE_RECEIPT)
    set_state(run, head, DocumentHead.State.DRAFT, event="withdrawn", reason_code=reason_code)


# ---------------------------------------------------------------------------
# Approval: officialise, value and cover (P04)
# ---------------------------------------------------------------------------


def _refuse_self_decision(context: DecisionContext, makers: Iterable[Any]) -> None:
    """E129 step 12 / E234 step 10: the checker made none of what is being decided.

    ``decide`` compares the checker with whoever pressed send; the PT's preparer,
    its document maker and everyone who marked a row reviewed are makers too.
    """
    if str(context.checker_id) in {str(maker) for maker in makers if maker}:
        raise Refusal(
            "SELF_APPROVAL", "Someone who prepared or reviewed this PT cannot also decide it."
        )


def _submitted_subject(
    run: CommandRun, context: DecisionContext
) -> tuple[DocumentHead, GoodsGrn, DraftRevision]:
    document_id = _uuid_or_none(context.request.subject_key)
    heads = lock_heads(run, [document_id]) if document_id else {}
    head = heads.get(document_id) if document_id else None
    goods_pt = (
        GoodsPt.objects.select_related("grn", "grn__document", "grn__arrival")
        .filter(document_id=document_id)
        .first()
    )
    if head is None or goods_pt is None or goods_pt.grn is None:
        raise Refusal("NOT_FOUND", "That PT was not found.")
    revision = head.draft_revision
    if head.state != DocumentHead.State.SUBMITTED or revision is None:
        raise Refusal("STATE_CONFLICT", "Only a submitted PT can be decided.")
    if revision.content_hash != context.request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The submitted PT changed after it was reviewed.")
    reviewers = [row.get("reviewed_by") for row in revision.reviewed_rows or []]
    _refuse_self_decision(context, [goods_pt.preparer_id, head.document.maker_id, *reviewers])
    return head, goods_pt.grn, revision


def pt_cells(goods_pt: GoodsPt) -> frozenset[tuple[int | None, int | None]]:
    """The PT's complete scope: its site with its parent's brand and every line SKU's brand.

    A PT's lines can name SKUs of another brand than its GRN's arrival, so reading,
    exporting, changing or deciding it needs each of those brands at the site - one
    grant per cell - and a partially authorised person gets nothing.
    """
    return pt_cells_many([goods_pt])[goods_pt.document_id]


def pt_cells_many(
    goods_pts: Iterable[GoodsPt],
) -> dict[uuid.UUID, frozenset[tuple[int | None, int | None]]]:
    """``pt_cells`` for many PTs in three queries (their ``document`` and ``grn__arrival``
    should be selected already)."""
    from core.kernel_models import DraftLine, OfficialLine

    pts_list = list(goods_pts)
    ids = [goods_pt.document_id for goods_pt in pts_list]
    skus: dict[Any, set[str]] = defaultdict(set)
    for document_id, sku_id in OfficialLine.objects.filter(
        version__document_id__in=ids
    ).values_list("version__document_id", "payload__sku_id"):
        if sku_id:
            skus[document_id].add(str(sku_id))
    drafted: dict[Any, set[int]] = defaultdict(set)
    for document_id, sku_id, brand_id in DraftLine.objects.filter(document_id__in=ids).values_list(
        "document_id", "payload__sku_id", "payload__describing__brand_id"
    ):
        if sku_id:
            skus[document_id].add(str(sku_id))
        elif _int_or_none(brand_id):
            # A drafted new item has no SKU yet; its brand is the one it names (OPS-15).
            drafted[document_id].add(int(str(brand_id)))
    wanted = sorted({sku for found in skus.values() for sku in found})
    brand_of = {
        str(pk): brand_id
        for pk, brand_id in ProductSku.objects.filter(pk__in=wanted).values_list(
            "pk", "style__brand_id"
        )
    }
    out: dict[uuid.UUID, frozenset[tuple[int | None, int | None]]] = {}
    for goods_pt in pts_list:
        arrival_brand = goods_pt.grn.arrival.brand_id if goods_pt.grn is not None else None
        brands: set[int | None] = {arrival_brand}
        brands |= {brand_of[sku] for sku in skus[goods_pt.document_id] if sku in brand_of}
        brands |= drafted[goods_pt.document_id]
        out[goods_pt.document_id] = frozenset(
            (goods_pt.document.site_id, brand) for brand in brands
        )
    return out


def require_complete_scope(access: Any, document_id: uuid.UUID, action: str) -> None:
    """``action`` over every cell of the PT, checked (and so replayed) inside the command."""
    if access is None:
        return
    goods_pt = (
        GoodsPt.objects.select_related("document", "grn__arrival")
        .filter(document_id=document_id)
        .first()
    )
    if goods_pt is None:
        return
    if not access.holds(action):
        raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
    if not access.covers_all({action}, pt_cells(goods_pt)):
        raise Refusal(
            "ACTION_DENIED", "This PT holds goods of a brand or site outside your authority."
        )


def _approve_receipt(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    head, grn, revision = _submitted_subject(run, context)
    require_complete_scope(context.access, head.document_id, APPROVE_RECEIPT)
    context.enforce_policy(run)
    site_id = head.document.held_site_id
    if context.decision == "reject":
        set_state(
            run, head, DocumentHead.State.DRAFT, event="rejected", reason_code=context.reason_code
        )
        notify(
            run,
            event_kind="pt.rejected",
            subject_key=f"document:{head.document_id}",
            site_id=site_id,
            title="A receipt PT was returned to draft",
            roles=["C-WHO"],
        )
        return {"state": "draft"}
    check_site_for_goods(site_id)
    profile = load_profile(
        run.tenant_id,
        revision.payload.get("profile_version_id"),
        document_target(head.document_id),
        code="PT_ROWS_INVALID",
    )
    stored = [dict(state.payload) for state in revision_lines(head.document_id, revision.revision)]
    lines = checked_lines(run, revision.payload, stored, profile, stale_code="PT_ROWS_INVALID")
    # Ticket 12: where the switch is on, a line with no HSN stops approval, by name.
    refuse_missing_hsn(lines, site_id)
    problems = validate_rows(
        lines, revision_id=revision.pk, grn_document_id=grn.document_id, profile=profile
    )
    if problems:
        raise Refusal(
            "PT_ROWS_INVALID", "Some rows are not valid.", status=422, issues=problems[:1000]
        )
    receipt_kind = (
        GoodsPt.objects.filter(document_id=head.document_id)
        .values_list("receipt_kind", flat=True)
        .first()
        or PRIMARY
    )
    # Eligibility is judged again under the lot locks: approval never makes goods eligible.
    engine.lock_lots(run, [uuid.UUID(lot_id) for lot_id in _requested_by_lot(lines)])
    reconciliation = reconcile_receipt(grn.document_id, lines, revision.content_hash, receipt_kind)
    run.reconciliation = reconciliation
    if not reconciliation["passed"]:
        raise Refusal(
            "RECONCILIATION_FAILED",
            "The PT no longer reconciles with the GRN.",
            issues=reconciliation["issues"][:1000],
        )
    identity = head.document
    number = identity.official_number or allocate(run, identity.entity, RECEIPT_DOC_TYPE)
    frozen = [{k: v for k, v in line.items() if k != "issues"} for line in lines]
    version, official_lines = officialise(
        run,
        head,
        approved_by_id=context.checker_id,
        canonical_header={**revision.payload, "number": number},
        lines=[(uuid.UUID(line["line_key"]), line) for line in frozen],
        authority={**run.authority, "maker_id": str(identity.maker_id)},
        reconciliation=reconciliation,
        profile_version_id=profile.version.pk,
        number=number,
    )
    _value_and_cover(
        run,
        version=version,
        official_lines=official_lines,
        lines=frozen,
        site_id=site_id,
        source_time=grn.arrival.actual_arrival_at,
        number=number,
        profile=profile,
        receipt_kind=receipt_kind,
        grn_document_id=grn.document_id,
    )
    run.audit_subject_key = f"document:{head.document_id}"
    run.audit_site_id = site_id
    open_exception(
        run,
        kind="acceptance_remaining",
        site_id=site_id,
        subject_key=f"document:{head.document_id}",
        reason_code="AWAITING_ACCEPTANCE",
        source_event_key=version.pk,
        allowed_resolution_actions=["stockledger/acceptance-sessions"],
    )
    return {"number": number, "version": version.version, "official_version_id": str(version.pk)}


def _value_and_cover(
    run: CommandRun,
    *,
    version: OfficialVersion,
    official_lines: list[OfficialLine],
    lines: list[dict[str, Any]],
    site_id: int,
    source_time: datetime,
    number: str,
    profile: ProfileContext,
    receipt_kind: str,
    grn_document_id: uuid.UUID,
) -> None:
    """P04: one origin per official line, coverage of its exact requested portions.

    The portions come from one pool read under the lot locks, taken line by line so two
    lines of this PT never claim the same pieces.
    """
    plan = engine.Plan("P04", version.pk, engine.event_key("P04", version.pk))
    previous = _previous_origins(version.document_id)
    pool = receipt_pool(grn_document_id, receipt_kind)
    for official, line in zip(official_lines, lines, strict=True):
        prior = previous.get(str(official.stable_line_key))
        origin = _origin(official, line, site_id, source_time, number, profile, prior)
        run.record(origin)
        for request in line.get("coverage_requests") or []:
            lot_id = uuid.UUID(str(request["lot_id"]))
            try:
                portions = pool.take(lot_id, line["sku_id"], int(request["qty"]))
            except ValueError as exc:
                raise Refusal(
                    "COVERAGE_CONFLICT", "Part of the requested quantity is already covered."
                ) from exc
            for interval in portions:
                engine.cover(
                    run,
                    plan,
                    lot_id=lot_id,
                    interval=interval,
                    origin=origin,
                    pt_version_id=version.pk,
                    pt_line_id=official.pk,
                    site_id=site_id,
                )
    batch_id = engine.post(run, version, plan)
    _record_posting(run, version.document_id, version.pk, "P04", batch_id)


def _record_posting(
    run: CommandRun,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    posting_kind: str,
    batch_id: uuid.UUID | None,
) -> None:
    """History shows the posting an official version caused, not only the version."""
    if batch_id is None:
        return
    record_event(
        run,
        document_id,
        "posted",
        version_id=version_id,
        payload={
            "posting_kind": posting_kind,
            "journal_batch_id": str(batch_id),
            "details": [],
        },
    )


def _previous_origins(document_id: uuid.UUID) -> dict[str, Origin]:
    """The latest earlier origin per stable line key, so reissues keep their lineage."""
    out: dict[str, Origin] = {}
    for origin in (
        Origin.objects.select_related("official_line")
        .filter(official_line__version__document_id=document_id)
        .order_by("official_line__version__version")
    ):
        official_line = origin.official_line
        assert official_line is not None
        out[str(official_line.stable_line_key)] = origin
    return out


def _origin(
    official: OfficialLine,
    line: dict[str, Any],
    site_id: int,
    source_time: datetime,
    number: str,
    profile: ProfileContext,
    prior: Origin | None,
) -> Origin:
    calculated = line["calculated"]
    cost = int(str(calculated["p_rate_paise"]))
    mrp = int(str(calculated.get("mrp_paise") or (line.get("supplied") or {}).get("mrp_paise")))
    return Origin(
        lineage_key=prior.lineage_key if prior else uuid.uuid4(),
        official_line_id=official.pk,
        site_id=site_id,
        source_time=source_time,
        source_kind=Origin.SourceKind.RECEIPT,
        sku_id=uuid.UUID(str(line["sku_id"])),
        unit_cost=cost,
        mrp=mrp,
        opening_qty=sum(int(r["qty"]) for r in line.get("coverage_requests") or []),
        previous_origin_id=prior.pk if prior else None,
        frozen_evidence={
            "receipt_site_id": str(site_id),
            "source_ref": line.get("source_ref") or number,
            "source_time_basis": "arrival",
            "identity_as_used": {
                "sku_id": line["sku_id"],
                "attributes": line.get("attributes") or [],
            },
            "alias_as_used": line.get("alias_as_used"),
            "season_id": line.get("season_id"),
            "hsn": line.get("hsn"),
            "profile_version_id": str(profile.version.pk),
            "rate_version_id": str(profile.rates_version.pk),
            "tax_version_id": str(profile.tax_version.pk),
            "cost_paise": str(cost),
            "mrp_paise": str(mrp),
        },
    )


# ---------------------------------------------------------------------------
# Reversal (P06) and reissue
# ---------------------------------------------------------------------------


def reversal_blockers(version_id: uuid.UUID) -> list[str]:
    """CONSUMED if any covered portion ever left outbound; RESERVED if any is reserved now."""
    intervals: dict[uuid.UUID, list[ranges.Interval]] = defaultdict(list)
    for event in CoverageEvent.objects.filter(pt_version_id=version_id, effect="cover"):
        intervals[event.lot_id].append(bounds(event.portion))
    blockers = []
    if engine.outbound_consumed(list(intervals), intervals):
        blockers.append("CONSUMED")
    for reservation in ActiveReservation.objects.filter(lot_id__in=list(intervals)):
        if ranges.intersect([bounds(reservation.portion)], intervals[reservation.lot_id]):
            blockers.append("RESERVED")
            break
    return blockers


def _refuse_unsafe(version_id: uuid.UUID) -> None:
    blockers = reversal_blockers(version_id)
    if blockers:
        raise Refusal(
            "REVERSAL_UNSAFE",
            "Some of this PT's stock has already been used or reserved.",
            issues=[issue(code, code.capitalize()) for code in blockers],
        )


def request_reversal(
    run: CommandRun,
    head: DocumentHead,
    *,
    reason_code: str,
    evidence_ids: list[uuid.UUID],
    brand_id: int | None,
) -> ApprovalRequest:
    """E130: a reversal request bound to the live version; nothing is reversed yet.

    The request carries the PT's brand, as submission does, so only approvers whose
    grant covers that brand see or decide it.
    """
    live = head.live_version
    if head.state != DocumentHead.State.OFFICIAL or live is None:
        raise Refusal("STATE_CONFLICT", "Only an official PT can be reversed.")
    _refuse_unsafe(live.pk)
    from core.kernel_models import OfficialLine

    target = document_target(head.document_id)
    frozen = [
        dict(line.payload)
        for line in OfficialLine.objects.filter(version=live).only("payload").order_by("pk")
    ]
    policy = pin(
        run,
        action=APPROVE_REVERSAL,
        purpose=RECEIPT,
        site_id=head.document.site_id,
        brand_ids=list(target.brand_ids),
        amounts=pt_amounts(frozen),
    )
    request = create_request(
        run,
        subject_kind="document",
        subject_key=str(head.document_id),
        revision=head.revision,
        reviewed_hash=live.content_hash,
        requested_action=APPROVE_REVERSAL,
        site_id=head.document.site_id,
        brand_id=brand_id,
        title=f"Reverse {head.document.official_number}",
        policy=policy,
    )
    record_event(
        run,
        head.document_id,
        "reversal_requested",
        version_id=live.pk,
        reason_code=reason_code,
        payload={"official_version_id": str(live.pk), "details": []},
    )
    for evidence_id in evidence_ids:
        record_event(
            run,
            head.document_id,
            "reversal_evidence",
            version_id=live.pk,
            evidence_id=evidence_id,
            payload={"evidence_id": str(evidence_id), "details": []},
        )
    return request


def _approve_reversal(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    document_id = _uuid_or_none(context.request.subject_key)
    head = lock_heads(run, [document_id]).get(document_id) if document_id else None
    live = head.live_version if head is not None else None
    if head is None or live is None or head.state != DocumentHead.State.OFFICIAL:
        raise Refusal("STATE_CONFLICT", "This PT is no longer official.")
    if live.content_hash != context.request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The official version changed after review.")
    preparer = (
        GoodsPt.objects.filter(document_id=head.document_id)
        .values_list("preparer_id", flat=True)
        .first()
    )
    _refuse_self_decision(context, [preparer, head.document.maker_id])
    require_complete_scope(context.access, head.document_id, APPROVE_REVERSAL)
    context.enforce_policy(run)
    if context.decision == "reject":
        record_event(run, head.document_id, "reversal_rejected", reason_code=context.reason_code)
        return {"state": head.state}
    guard = check_contract(head.document.held_site_id)
    if guard.freeze_id:
        raise Refusal("UNDER_COUNT", "A count freezes this site.")
    lot_ids = CoverageEvent.objects.filter(pt_version_id=live.pk, effect="cover").values_list(
        "lot_id", flat=True
    )
    goods_pt = GoodsPt.objects.select_related("grn__document", "grn__arrival").get(
        document_id=head.document_id
    )
    grn = goods_pt.grn
    grn_lot_ids = [lot.pk for lot in grn_lots(grn.document_id)] if grn is not None else []
    engine.lock_lots(run, [*lot_ids, *grn_lot_ids])
    _refuse_unsafe(live.pk)
    plan = engine.Plan("P06", live.pk, engine.event_key("P06", live.pk))
    engine.counter_coverage(run, plan, pt_version_id=live.pk)
    _record_posting(run, head.document_id, live.pk, "P06", engine.post(run, live, plan))
    if grn is not None:
        # Uncovered pieces may be excess again: they go back under the excess hold.
        receiving.sync_excess_holds(run, grn, trigger=live.pk, lock=False)
    head.live_version = None
    head.save(update_fields=["live_version", "updated_at"])
    set_state(
        run, head, DocumentHead.State.REVERSED, event="reversed", reason_code=context.reason_code
    )
    resolve_exceptions(run, kind="acceptance_remaining", subject_key=f"document:{head.document_id}")
    run.audit_subject_key = f"document:{head.document_id}"
    run.audit_site_id = head.document.site_id
    return {"state": "reversed", "official_version_id": str(live.pk)}


def reissue(
    run: CommandRun, head: DocumentHead, *, corrected: dict[str, Any], reason_code: str
) -> None:
    """E131: the next draft revision under the original identity and number."""
    if head.state != DocumentHead.State.REVERSED or head.live_version_id is not None:
        raise Refusal("REISSUE_NOT_READY", "Only a safely reversed PT can be reissued.")
    header = draft_payload(head)
    profile = load_profile(
        run.tenant_id,
        corrected.get("profile_version_id") or header.get("profile_version_id"),
        document_target(head.document_id),
    )
    direction = str(corrected.get("direction") or header.get("direction") or "")
    if direction not in profile.directions:
        raise Refusal("PROFILE_INVALID", "The profile does not allow this direction.", status=422)
    lines = corrected.get("lines")
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise Refusal("INVALID_REQUEST", "corrected.lines must be a list of PT lines.")
    priced = [price_line(normalise_line(line), profile, direction) for line in lines]
    _unique_keys(priced)
    header.update({"profile_version_id": str(profile.version.pk), "direction": direction})
    append_revision(
        run,
        head,
        header=header,
        replace_lines=[(uuid.UUID(line["line_key"]), line) for line in priced],
        reviewed_rows=[],
    )
    record_event(
        run,
        head.document_id,
        "reissue_started",
        reason_code=reason_code,
        payload={"from_state": "reversed", "to_state": "draft", "details": []},
    )


def _request_cells(requests: list[Any]) -> dict[Any, frozenset[tuple[int | None, int | None]]]:
    """PT approval requests cover their PT's complete scope, every line brand included."""
    by_document: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for request in requests:
        try:
            by_document[uuid.UUID(str(request.subject_key))].append(request)
        except ValueError:
            continue
    goods_pts = GoodsPt.objects.select_related("document", "grn__arrival").filter(
        document_id__in=list(by_document)
    )
    return {
        request.pk: cells
        for document_id, cells in pt_cells_many(goods_pts).items()
        for request in by_document[document_id]
    }


def install() -> None:
    register_subject_handler("document", APPROVE_RECEIPT, _approve_receipt)
    register_subject_handler("document", APPROVE_REVERSAL, _approve_reversal)
    register_subject_cells("document", APPROVE_RECEIPT, _request_cells)
    register_subject_cells("document", APPROVE_REVERSAL, _request_cells)
    register_integrity_refusal(
        "uq_goodspt_primary_per_grn", "PRIMARY_EXISTS", "This GRN already has its primary PT."
    )
