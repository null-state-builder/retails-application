"""Canonical JSON and SHA-256 for goods-v1 evidence (design §4.1, §5.1).

A command fingerprint, a reviewed hash, an official content hash and a chained
row hash all have to mean "these exact business facts" on every machine and in
every later year. Python's ``json.dumps`` does not promise that by itself: key
order, float spelling and timestamp formats drift. This module is the one place
that decides the spelling, so two callers hashing the same facts always agree.

Floats are refused outright. Money is integer paise and percentages are
``Decimal``; a float reaching a hash means precision was already lost upstream.
Nothing here imports Django or HTTP code.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any


class CanonicalError(TypeError):
    """A value that has no single canonical spelling (a float, an unknown type)."""


def normalise(value: Any) -> Any:  # noqa: C901 - one branch per JSON-native type
    """Turn ``value`` into JSON-native data with exactly one spelling."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalError(
            f"refusing float {value!r} in canonical content; use integer paise or Decimal"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalError(f"refusing non-finite Decimal {value!r}")
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CanonicalError(f"refusing naive datetime {value!r}")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): normalise(item) for key, item in value.items()}
    if hasattr(value, "lower") and hasattr(value, "upper") and hasattr(value, "bounds"):
        # A database range (custody portion): spelt like PostgreSQL spells it.
        bounds = str(value.bounds or "[)")
        lower = "" if value.lower is None else normalise(value.lower)
        upper = "" if value.upper is None else normalise(value.upper)
        return f"{bounds[0]}{lower},{upper}{bounds[1]}"
    if isinstance(value, list | tuple):
        return [normalise(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((normalise(item) for item in value), key=canonical_json)
    raise CanonicalError(f"no canonical form for {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Sorted-key, whitespace-free JSON of ``value``."""
    return json.dumps(normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    """Lower-case hexadecimal SHA-256 of bytes, or of UTF-8 text."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def content_hash(value: Any) -> str:
    """The SHA-256 of ``value``'s canonical JSON."""
    return sha256_hex(canonical_json(value))
