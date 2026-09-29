"""Closed-input parsing shared by the booking and receiving slices (design §5.3, §6.1).

Shape problems (wrong type, unknown key, out-of-range number) are
``INVALID_REQUEST``; whether a referenced vendor, site or SKU exists is a domain
question the services answer with their own codes. Nothing here touches models
beyond bumping a document head's revision.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from django.utils.dateparse import parse_date, parse_datetime

from accounts.goods_api import check_revision as check_revision
from core.goods_fields import MAX_LINE_QTY
from core.goods_money import MoneyInvalid, paise_from_json
from core.kernel_models import DocumentHead
from core.refusals import Refusal, issue

_DIGITS = re.compile(r"^[0-9]+$")


def bad(message: str, field: str, code: str = "INVALID") -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue(code, message, field=field)])


def closed(
    value: Any, allowed: Iterable[str], field: str, *, required: Iterable[str] = ()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise bad(f"{field} must be an object.", field)
    allowed_set = set(allowed)
    unknown = sorted(set(value) - allowed_set)
    if unknown:
        raise Refusal(
            "INVALID_REQUEST",
            f"Unknown field(s) in {field}: {', '.join(unknown)}.",
            issues=[
                issue("UNKNOWN_FIELD", f"{name} is not accepted", field=f"{field}.{name}")
                for name in unknown
            ],
        )
    missing = [name for name in required if value.get(name) is None]
    if missing:
        raise Refusal(
            "INVALID_REQUEST",
            f"Missing field(s) in {field}: {', '.join(missing)}.",
            issues=[
                issue("REQUIRED", f"{name} is required", field=f"{field}.{name}")
                for name in missing
            ],
        )
    return value


def text(value: Any, field: str, max_len: int, *, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip() and not required):
        if required:
            raise bad(f"{field} is required.", field, "REQUIRED")
        return None
    if not isinstance(value, str) or len(value) > max_len or not value.strip():
        raise bad(f"{field} must be text of 1 to {max_len} characters.", field)
    return value


def plain_text(value: Any, field: str, max_len: int) -> str:
    """Text that must be present but may be empty (e.g. a description beside a SKU)."""
    if not isinstance(value, str) or len(value) > max_len:
        raise bad(f"{field} must be text of at most {max_len} characters.", field)
    return value


def whole(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise bad(f"{field} must be a whole number from {low} to {high}.", field)
    return int(value)


def quantity(value: Any, field: str) -> int:
    return whole(value, field, 1, MAX_LINE_QTY)


def uuid_value(value: Any, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise bad(f"{field} must be an ID.", field) from None


def optional_uuid(value: Any, field: str) -> uuid.UUID | None:
    return None if value is None else uuid_value(value, field)


def legacy_id(value: Any, field: str) -> int:
    """A retained BIGINT master identifier, sent as a number or a decimal string."""
    if isinstance(value, bool):
        raise bad(f"{field} must be an ID.", field)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _DIGITS.match(value):
        parsed = int(value)
    else:
        raise bad(f"{field} must be an ID.", field)
    if parsed < 1:
        raise bad(f"{field} must be an ID.", field)
    return parsed


def optional_legacy_id(value: Any, field: str) -> int | None:
    return None if value is None else legacy_id(value, field)


def vocabulary_id(value: Any, field: str) -> str | None:
    """A vocabulary value reference: either kind of ID, kept as its string form."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(legacy_id(value, field))
    if isinstance(value, str) and (_DIGITS.match(value) or _is_uuid(value)):
        return value
    raise bad(f"{field} must be an ID.", field)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def timestamp(value: Any, field: str) -> datetime:
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise bad(f"{field} must be a timestamp with a time-zone offset.", field)
    return parsed


def day(value: Any, field: str) -> date:
    parsed = parse_date(value) if isinstance(value, str) else None
    if parsed is None:
        raise bad(f"{field} must be a date (YYYY-MM-DD).", field)
    return parsed


def optional_day(value: Any, field: str) -> date | None:
    return None if value is None else day(value, field)


def money(value: Any, field: str) -> str | None:
    """Integer paise as a base-10 string (design §3.4); null stays unknown, never zero."""
    try:
        paise = paise_from_json(value)
    except MoneyInvalid:
        raise bad(f"{field} must be integer paise written as a string.", field) from None
    return None if paise is None else str(paise)


def id_list(value: Any, field: str, *, limit: int = 1000) -> list[uuid.UUID]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise bad(f"{field} must be a list of at most {limit} IDs.", field)
    return [uuid_value(item, f"{field}[{index}]") for index, item in enumerate(value)]


def object_list(value: Any, field: str, *, limit: int = 50_000) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise bad(f"{field} must be a list of at most {limit} entries.", field)
    return value


def save_head_revision(head: DocumentHead) -> None:
    head.revision += 1
    head.save(update_fields=["revision", "updated_at"])
