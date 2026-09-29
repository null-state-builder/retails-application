"""Pure interval arithmetic for custody portions (design §3.2, §7.3).

A portion is a half-open integer range ``[lower, upper)`` inside one custody lot.
These functions never touch Django or the database, so the pairing and
eligibility rules that depend on them can be tested exhaustively on their own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

Interval = tuple[int, int]


def valid(interval: Interval) -> bool:
    lower, upper = interval
    return lower >= 0 and upper > lower


def length(interval: Interval) -> int:
    return interval[1] - interval[0]


def total(intervals: Iterable[Interval]) -> int:
    return sum(length(i) for i in normalise(intervals))


def normalise(intervals: Iterable[Interval]) -> list[Interval]:
    """Sorted, merged, non-empty intervals (the union)."""
    ordered = sorted((i for i in intervals if i[1] > i[0]), key=lambda i: i[0])
    merged: list[Interval] = []
    for lower, upper in ordered:
        if merged and lower <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
        else:
            merged.append((lower, upper))
    return merged


def intersect(a: Iterable[Interval], b: Iterable[Interval]) -> list[Interval]:
    left = normalise(a)
    right = normalise(b)
    out: list[Interval] = []
    i = j = 0
    while i < len(left) and j < len(right):
        lower = max(left[i][0], right[j][0])
        upper = min(left[i][1], right[j][1])
        if upper > lower:
            out.append((lower, upper))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract(a: Iterable[Interval], b: Iterable[Interval]) -> list[Interval]:
    """Portions of ``a`` not covered by ``b``."""
    result: list[Interval] = []
    remove = normalise(b)
    for lower, upper in normalise(a):
        cursor = lower
        for r_lower, r_upper in remove:
            if r_upper <= cursor or r_lower >= upper:
                continue
            if r_lower > cursor:
                result.append((cursor, min(r_lower, upper)))
            cursor = max(cursor, r_upper)
            if cursor >= upper:
                break
        if cursor < upper:
            result.append((cursor, upper))
    return result


def overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def contains(outer: Iterable[Interval], inner: Iterable[Interval]) -> bool:
    return not subtract(inner, outer)


def take(intervals: Sequence[Interval], quantity: int) -> list[Interval]:
    """The first ``quantity`` units of ``intervals`` in order, as intervals."""
    if quantity <= 0:
        return []
    taken: list[Interval] = []
    remaining = quantity
    for lower, upper in intervals:
        if remaining <= 0:
            break
        size = upper - lower
        if size <= remaining:
            taken.append((lower, upper))
            remaining -= size
        else:
            taken.append((lower, lower + remaining))
            remaining = 0
    if remaining > 0:
        raise ValueError(f"only {quantity - remaining} of {quantity} units are available")
    return taken


def split(interval: Interval, cut: Iterable[Interval]) -> tuple[list[Interval], list[Interval]]:
    """``(inside, outside)``: the parts of ``interval`` within ``cut`` and the remainder."""
    inside = intersect([interval], cut)
    outside = subtract([interval], cut)
    return inside, outside


@dataclass(frozen=True)
class Candidate:
    """One eligible portion for FIFO selection (design §7.3)."""

    source_time: float  # epoch seconds, or any totally ordered stamp
    lineage_key: str
    lot_id: str
    lower: int
    upper: int

    @property
    def order_key(self) -> tuple[float, str, str, int]:
        return (self.source_time, self.lineage_key, self.lot_id, self.lower)


def fifo_select(candidates: Iterable[Candidate], quantity: int) -> list[Candidate]:
    """Pick ``quantity`` units: oldest source time, then lineage, lot and range start."""
    ordered = sorted(candidates, key=lambda c: c.order_key)
    chosen: list[Candidate] = []
    remaining = quantity
    for candidate in ordered:
        if remaining <= 0:
            break
        size = candidate.upper - candidate.lower
        if size <= remaining:
            chosen.append(candidate)
            remaining -= size
        else:
            chosen.append(
                Candidate(
                    candidate.source_time,
                    candidate.lineage_key,
                    candidate.lot_id,
                    candidate.lower,
                    candidate.lower + remaining,
                )
            )
            remaining = 0
    if remaining > 0:
        raise ValueError(f"only {quantity - remaining} of {quantity} units are eligible")
    return chosen
