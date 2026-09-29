"""Sealing immutable evidence rows into per-partition hash chains (design §4.4, §5.1).

A command does not insert evidence as it goes. It hands each row to the sealer,
which at the end of the command - after every lower-ranked lock, including the
numbering series - locks the chain heads the rows belong to, fills the actor,
command and time columns, chains each row's SHA-256 to the previous one in its
partition and inserts the rows. Foreign keys are deferred, so rows may reference
one another in any order.

A partition is one table family at one site (or the tenant for site-less rows),
so unrelated sites do not queue behind one another.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from django.db import models

from core.canonical import canonical_json, sha256_hex
from core.goods_base import EvidenceRow

SCHEMA_VERSION = "goods-v1"
GENESIS = "0" * 64


def row_content(row: models.Model) -> dict[str, Any]:
    excluded: frozenset[str] = getattr(row, "HASH_EXCLUDED", frozenset())
    content: dict[str, Any] = {}
    for field in row._meta.concrete_fields:
        if field.name in excluded:
            continue
        if field.attname == "row_hash":
            # A row is hashed before its own hash exists. Where a table does not
            # exclude the column outright (``CommandAttempt``), the sealer saw it
            # blank, so the verifier must too - reading back the stored hash and
            # hashing that would make every such row look tampered.
            content["row_hash"] = ""
            continue
        value = getattr(row, field.attname)
        if hasattr(value, "resolve_expression"):
            continue
        content[field.attname] = value
    return content


def chain_hash(previous: str | None, table: str, content: dict[str, Any]) -> str:
    return sha256_hex(f"{previous or ''}|{SCHEMA_VERSION}|{table}|{canonical_json(content)}")


def partition_key(row: models.Model) -> str:
    site_id = getattr(row, "site_id", None)
    return f"{row._meta.db_table}:{site_id if site_id is not None else '-'}"[:160]


class EvidenceSealer:
    """Collects a command's evidence rows and writes them once, chained."""

    def __init__(
        self,
        *,
        tenant_id: uuid.UUID,
        command_key_id: uuid.UUID | None,
        actor_id: uuid.UUID | None,
        service_code: str | None,
        now: datetime,
    ) -> None:
        self.tenant_id = tenant_id
        self.command_key_id = command_key_id
        self.actor_id = actor_id
        self.service_code = service_code
        self.now = now
        self.pending: list[models.Model] = []

    def add(self, row: EvidenceRow, *, event_at: datetime | None = None) -> Any:
        row.tenant_id = self.tenant_id
        if self.command_key_id is not None:
            row.command_key_id = self.command_key_id
        row.actor_id = self.actor_id
        row.service_code = None if self.actor_id else self.service_code
        row.event_at = event_at or getattr(row, "event_at", None) or self.now
        row.recorded_at = self.now
        self.pending.append(row)
        return row

    def add_all(self, rows: Iterable[EvidenceRow]) -> None:
        for row in rows:
            self.add(row)

    def discard(self) -> None:
        self.pending = []

    def flush(self) -> None:
        if not self.pending:
            return
        rows = self.pending
        self.pending = []
        seal_rows(self.tenant_id, rows)


def seal_rows(tenant_id: uuid.UUID, rows: list[models.Model]) -> None:
    """Chain and insert ``rows`` (already carrying actor/command/time)."""
    from core.kernel_models import ChainHead

    partitions = sorted({partition_key(row) for row in rows})
    ChainHead.objects.bulk_create(
        [
            ChainHead(tenant_id=tenant_id, partition_key=key, last_hash=GENESIS)
            for key in partitions
        ],
        ignore_conflicts=True,
    )
    heads = {
        head.partition_key: head
        for head in ChainHead.objects.select_for_update()
        .filter(tenant_id=tenant_id, partition_key__in=partitions)
        .order_by("partition_key")
    }
    by_model: dict[type[models.Model], list[models.Model]] = defaultdict(list)
    for row in rows:
        head = heads[partition_key(row)]
        previous = head.last_hash if head.last_event_id is not None else None
        row.previous_hash = previous  # type: ignore[attr-defined]
        row.row_hash = chain_hash(previous, row._meta.db_table, row_content(row))  # type: ignore[attr-defined]
        head.last_hash = row.row_hash  # type: ignore[attr-defined]
        head.last_event_id = row.pk
        by_model[type(row)].append(row)
    journal_written = False
    for model, batch in sorted(by_model.items(), key=lambda item: _journal_order(item[0])):
        if getattr(model, "PROTECTION", "") == "journal":
            if not journal_written:
                _append_operational_batch(by_model)
                journal_written = True
            continue
        model._default_manager.bulk_create(batch, batch_size=2000)
    ChainHead.objects.bulk_update(list(heads.values()), ["last_hash", "last_event_id"])


#: The argument order of the one ledger-owned operational batch function.
JOURNAL_TABLES = (
    "stockledger_journalbatch",
    "stockledger_quantityleg",
    "stockledger_valueleg",
    "stockledger_encumbranceleg",
)


def _journal_order(model: type[models.Model]) -> int:
    order = list(JOURNAL_TABLES)
    table = model._meta.db_table
    return order.index(table) if table in order else -1


def _column_value(value: Any) -> Any:
    from psycopg.types.range import Range as NumericRange

    if isinstance(value, NumericRange):
        lower = "" if value.lower is None else value.lower
        upper = "" if value.upper is None else value.upper
        return f"{'[' if value.lower_inc else '('}{lower},{upper}{']' if value.upper_inc else ')'}"
    return value


def _journal_payload(rows: list[models.Model]) -> list[dict[str, Any]]:
    from core.canonical import normalise

    payload = []
    for row in rows:
        record: dict[str, Any] = {}
        for column in row._meta.concrete_fields:
            value = getattr(row, column.attname)
            if hasattr(value, "resolve_expression"):
                value = cast(Any, row).recorded_at if column.attname == "created_at" else None
            record[str(column.column)] = normalise(_column_value(value))
        payload.append(record)
    return payload


def _append_operational_batch(
    rows_by_model: dict[type[models.Model], list[models.Model]],
) -> None:
    from django.db import connection

    payloads: dict[str, list[dict[str, Any]]] = {table: [] for table in JOURNAL_TABLES}
    for model, rows in rows_by_model.items():
        table = model._meta.db_table
        if table in payloads:
            payloads[table] = _journal_payload(rows)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT kdps_append_operational_batch(%s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)",
            [canonical_json(payloads[table]) for table in JOURNAL_TABLES],
        )


@dataclass
class PartitionCheck:
    """One partition's verification: how many rows were read and what did not hold."""

    partition_key: str
    rows: int
    #: Row ids (or ``head:<partition>``) that do not verify, in a stable order.
    broken: list[str]
    #: Every row hash the partition really holds, so an off-box anchor can be
    #: looked up in the chain rather than only in the mutable head.
    hashes: set[str]


def partition_rows(tenant_id: uuid.UUID, model: type[models.Model], site_id: Any) -> Any:
    """Exactly the rows ``partition_key`` puts in this partition.

    A site-less partition of a table that also has site rows (``core_auditevent:-``
    beside ``core_auditevent:64``) holds only the rows whose site is empty. Reading
    every row of the table there is what made the verifier see several genesis
    rows sharing one (empty) predecessor and report a healthy database as broken.
    """
    qs = model._default_manager.filter(tenant_id=tenant_id)
    has_site = any(field.attname == "site_id" for field in model._meta.concrete_fields)
    if site_id is not None:
        qs = qs.filter(site_id=site_id)
    elif has_site:
        qs = qs.filter(site_id__isnull=True)
    return qs


def check_partition(
    tenant_id: uuid.UUID, model: type[models.Model], site_id: Any = None
) -> PartitionCheck:
    """Recompute one partition's chain, checking every row it holds.

    Each row is re-hashed from its own stored predecessor, so no row can be
    skipped by the walk. The links are then checked separately: exactly one
    genesis row, no two rows claiming the same predecessor (a fork), every row
    reachable from the genesis, and the last link equal to the chain head. A row
    that fails any of these is reported by id; nothing is dropped because a
    dictionary could hold only one row per predecessor.
    """
    from core.kernel_models import ChainHead

    table = model._meta.db_table
    key = f"{table}:{site_id if site_id is not None else '-'}"[:160]
    rows = list(partition_rows(tenant_id, model, site_id).order_by("recorded_at", "id"))
    hashes = {str(getattr(row, "row_hash", "")) for row in rows}
    head = ChainHead.objects.filter(tenant_id=tenant_id, partition_key=key).first()
    if head is None:
        return PartitionCheck(key, len(rows), [str(row.pk) for row in rows], hashes)

    broken: list[str] = []
    children: dict[str | None, list[models.Model]] = defaultdict(list)
    for row in rows:
        previous = getattr(row, "previous_hash", None)
        children[previous].append(row)
        if chain_hash(previous, table, row_content(row)) != getattr(row, "row_hash", None):
            broken.append(str(row.pk))
    for siblings in children.values():
        if len(siblings) > 1:
            broken.extend(str(row.pk) for row in siblings)

    # Walk the links from the genesis. A fork was reported above; the walk takes
    # the first branch only so that everything else shows up as unreachable.
    reached: set[str] = set()
    previous_hash: str | None = None
    while True:
        following = children.get(previous_hash)
        if not following:
            break
        row = following[0]
        if str(row.pk) in reached:  # a cycle: only possible through forged hashes
            break
        reached.add(str(row.pk))
        previous_hash = getattr(row, "row_hash", None)
    broken.extend(str(row.pk) for row in rows if str(row.pk) not in reached)
    ended = previous_hash if rows else None
    expected_end = head.last_hash if head.last_event_id is not None else None
    if ended != expected_end:
        broken.append(f"head:{key}")
    # One id per problem row, in first-seen order.
    return PartitionCheck(key, len(rows), list(dict.fromkeys(broken)), hashes)


def verify_partition(
    tenant_id: uuid.UUID, model: type[models.Model], site_id: Any = None
) -> list[str]:
    """Recompute one partition's chain; return the ids whose hash or link does not hold."""
    return check_partition(tenant_id, model, site_id).broken
