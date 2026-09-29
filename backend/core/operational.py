"""Operational inventory postings: paired quantity, value and encumbrance legs (design §7.1).

Pure structures and structural validation only - no Django models, no HTTP. The
stock ledger registers the adapter that validates domain rules and appends the
journal rows; ``core.posting.post_entries`` is still the only way in.

Every pair has exactly one negative source leg and one equal positive destination
leg. Quantities are half-open integer ranges of one custody lot. Value is the
operational memo value in integer paise - never a general-ledger amount, payable
or ownership statement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from core.canonical import content_hash

VALUE_BUCKETS = frozenset(
    {"stock", "transit", "origin_evidence", "adjustment_evidence", "external"}
)
AXES = frozenset({"hold", "reservation"})
GATES = frozenset({"inactive", "active"})
BOUNDARIES = frozenset(
    {"physical", "external", "disposed", "returned", "consumed", "transit", "matched_observation"}
)
#: P01-P18 are the goods-to-store design's own catalogue. P19 is the store sale
#: (OPS-07): accepted stock leaving at the counter, and an exchange's piece coming
#: back into the store's custody, as one posting. It is a kind of its own rather
#: than P13's adjustment-down, because a sale is not a write-off and must not be
#: readable as one - see the ticket's note for the product owner. P20 is the
#: write-off (goods ticket 15C): recognising the loss of an established recorded
#: value while the goods stay physically where they are, so it carries a value
#: leg and a hold but never a quantity leg. P21 is the disposal (goods ticket
#: 15D): quarantined pieces actually destroyed or handed over for scrap leave
#: custody for the ``disposed`` boundary, with a value leg only for a portion
#: whose loss no write-off has already recognised.
POSTING_KINDS = frozenset(f"P{number:02d}" for number in range(1, 22))

#: The sale posting, named once so the engine's consumption test and the sell
#: service cannot drift apart.
SALE_POSTING = "P19"
#: The write-off posting (goods ticket 15C), named once for the same reason.
WRITE_OFF_POSTING = "P20"
#: The disposal posting (goods ticket 15D), named once for the same reason.
DISPOSAL_POSTING = "P21"


class OperationalBatchError(ValueError):
    """A batch that is not a balanced operational posting."""


@dataclass(frozen=True)
class QuantityPair:
    lot_id: uuid.UUID
    lower: int
    upper: int
    source: dict[str, Any]
    destination: dict[str, Any]
    pair_key: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def qty(self) -> int:
        return self.upper - self.lower


@dataclass(frozen=True)
class ValuePair:
    origin_id: uuid.UUID
    amount: int
    source_bucket: str
    destination_bucket: str
    source_site_id: int | None = None
    destination_site_id: int | None = None
    lot_id: uuid.UUID | None = None
    lower: int | None = None
    upper: int | None = None
    pair_key: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(frozen=True)
class EncumbrancePair:
    axis: str
    control_key: uuid.UUID
    lot_id: uuid.UUID
    lower: int
    upper: int
    source_gate: str
    destination_gate: str
    pair_key: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def qty(self) -> int:
        return self.upper - self.lower


@dataclass(frozen=True)
class OperationalBatch:
    event_key: uuid.UUID
    posting_kind: str
    version_id: uuid.UUID
    quantity_pairs: tuple[QuantityPair, ...] = ()
    value_pairs: tuple[ValuePair, ...] = ()
    encumbrance_pairs: tuple[EncumbrancePair, ...] = ()

    def content_hash(self) -> str:
        """Hash of the business content; pair keys are identifiers and stay out."""
        return content_hash(
            {
                "kind": self.posting_kind,
                "version": str(self.version_id),
                "quantity": sorted(
                    [
                        [str(p.lot_id), p.lower, p.upper, p.source, p.destination]
                        for p in self.quantity_pairs
                    ],
                    key=str,
                ),
                "value": sorted(
                    [
                        [
                            str(p.origin_id),
                            p.amount,
                            p.source_bucket,
                            p.destination_bucket,
                            p.source_site_id,
                            p.destination_site_id,
                            str(p.lot_id) if p.lot_id else None,
                            p.lower,
                            p.upper,
                        ]
                        for p in self.value_pairs
                    ],
                    key=str,
                ),
                "encumbrance": sorted(
                    [
                        [
                            p.axis,
                            str(p.control_key),
                            str(p.lot_id),
                            p.lower,
                            p.upper,
                            p.source_gate,
                            p.destination_gate,
                        ]
                        for p in self.encumbrance_pairs
                    ],
                    key=str,
                ),
            }
        )


def _range_ok(lower: int | None, upper: int | None) -> bool:
    return (
        isinstance(lower, int)
        and isinstance(upper, int)
        and not isinstance(lower, bool)
        and not isinstance(upper, bool)
        and lower >= 0
        and upper > lower
    )


def validate_structure(batch: OperationalBatch) -> None:
    if batch.posting_kind not in POSTING_KINDS:
        raise OperationalBatchError(f"unknown posting kind {batch.posting_kind!r}")
    if not (batch.quantity_pairs or batch.value_pairs or batch.encumbrance_pairs):
        raise OperationalBatchError("an operational posting cannot be empty")
    keys: set[uuid.UUID] = set()
    pairs: list[QuantityPair | ValuePair | EncumbrancePair] = [
        *batch.quantity_pairs,
        *batch.value_pairs,
        *batch.encumbrance_pairs,
    ]
    for pair in pairs:
        if pair.pair_key in keys:
            raise OperationalBatchError(f"duplicate pair {pair.pair_key}")
        keys.add(pair.pair_key)
    for q in batch.quantity_pairs:
        if not _range_ok(q.lower, q.upper):
            raise OperationalBatchError(f"invalid quantity range [{q.lower},{q.upper})")
        for side in (q.source, q.destination):
            if side.get("boundary") not in BOUNDARIES:
                raise OperationalBatchError(f"unknown boundary {side.get('boundary')!r}")
        if q.source == q.destination:
            raise OperationalBatchError("a quantity pair must move between two different addresses")
    for v in batch.value_pairs:
        if isinstance(v.amount, bool) or not isinstance(v.amount, int) or v.amount <= 0:
            raise OperationalBatchError("a value pair carries a positive integer paise amount")
        if v.source_bucket not in VALUE_BUCKETS or v.destination_bucket not in VALUE_BUCKETS:
            raise OperationalBatchError("unknown value bucket")
        if (v.source_bucket, v.source_site_id) == (v.destination_bucket, v.destination_site_id):
            raise OperationalBatchError("a value pair must move between two different addresses")
        if (v.lower is None) != (v.upper is None) or (
            v.lower is not None and not _range_ok(v.lower, v.upper)
        ):
            raise OperationalBatchError("invalid value range")
    for e in batch.encumbrance_pairs:
        if e.axis not in AXES or e.source_gate not in GATES or e.destination_gate not in GATES:
            raise OperationalBatchError("unknown encumbrance axis or gate")
        if e.source_gate == e.destination_gate:
            raise OperationalBatchError("an encumbrance pair changes its gate")
        if not _range_ok(e.lower, e.upper):
            raise OperationalBatchError("invalid encumbrance range")
