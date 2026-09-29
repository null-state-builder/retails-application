"""Official goods-v1 numbering with published ceilings (design §4.3).

A series is one counter per tenant + legal entity + document type + Indian FY and
renders ``ENTITY/TYPE/YY-YY/000001``. The counter lives in the existing
``VoucherSeries`` table, so its database guard (never rewind, +1 only) protects
the new series exactly as it protects the old ones.

A command may only hand out numbers at or below a *confirmed* ceiling: a block
of capacity whose limit was first written to write-once storage outside the
database. After a restore the series resumes above the published ceiling, so a
number that might have been issued in the lost window is never issued twice.
Publishing a ceiling is not issuing a number and can leave unused capacity.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from core.canonical import canonical_json
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.documents import VoucherSeries
from core.fiscal import financial_year
from core.offbox import OffboxError, get_store
from core.refusals import Refusal

#: Central goods-v1 document types (design §4.3).
DOC_TYPES = frozenset(
    {
        "BKG",
        "GRN",
        "CGRN",
        "RPT",
        "OPT",
        "TPT",
        "GAP",
        "MOV",
        "RTV",
        "ADJ",
        "WOF",
        # Goods ticket 15D: a disposal is numbered on its own series at approval.
        "DSP",
        "SHR",
        "HLD",
        "REL",
        "TSH",
        "CNT",
    }
)
CEILING_SERVICE = "ceiling-publisher"


def series_store_code(entity_id: int) -> str:
    return f"E{entity_id}"[:16]


def ensure_series(
    tenant_id: uuid.UUID,
    entity: Any,
    doc_type: str,
    fy: str | None = None,
    *,
    block_size: int = 1000,
) -> VoucherSeries:
    if doc_type not in DOC_TYPES:
        raise ValueError(f"{doc_type} is not a goods-v1 document type")
    fy = fy or financial_year()
    series, _ = VoucherSeries.objects.get_or_create(
        fy=fy,
        store_code=series_store_code(entity.pk),
        doc_type=doc_type,
        defaults={
            "tenant_id": tenant_id,
            "legal_entity_id": entity.pk,
            "scope_version": "entity_v1",
            "ceiling_block_size": block_size,
        },
    )
    return series


def confirmed_ceiling(series_id: int) -> int:
    from core.kernel_models import SeriesCeiling

    value = (
        SeriesCeiling.objects.filter(series_id=series_id)
        .order_by("-ceiling")
        .values_list("ceiling", flat=True)
        .first()
    )
    return int(value or 0)


def render_number(entity_code: str, doc_type: str, fy: str, seq: int) -> str:
    return f"{entity_code.upper()}/{doc_type}/{fy}/{seq:06d}"


def allocate(run: CommandRun, entity: Any, doc_type: str, *, on: date | None = None) -> str:
    """Take the next official number inside a running command (rank SERIES)."""
    fy = financial_year(on or timezone.localdate())
    series = VoucherSeries.objects.filter(
        fy=fy, store_code=series_store_code(entity.pk), doc_type=doc_type, scope_version="entity_v1"
    ).first()
    if series is None:
        raise Refusal(
            "SERIES_NOT_READY",
            f"No {doc_type} numbering series is set up for {entity.code} in FY {fy}.",
        )
    locked = run.lock(LockRank.SERIES, VoucherSeries.objects.filter(pk=series.pk))[0]
    ceiling = confirmed_ceiling(locked.pk)
    if locked.next_seq > ceiling:
        _request_ceiling(run, locked, ceiling)
        raise Refusal(
            "SERIES_NOT_READY",
            f"The {doc_type} series for {entity.code} is waiting for its next published ceiling.",
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_voucher_series SET next_seq = next_seq + 1 "
            "WHERE id = %s RETURNING next_seq - 1",
            [locked.pk],
        )
        row = cursor.fetchone()
    assert row is not None
    seq = int(row[0])
    if ceiling - seq < max(1, locked.ceiling_block_size // 2):
        _request_ceiling(run, locked, ceiling)
    return render_number(entity.code, doc_type, fy, seq)


def _request_ceiling(run: CommandRun, series: VoucherSeries, current: int) -> None:
    from core.kernel_models import OutboxIntent

    target = max(current, series.next_seq - 1) + series.ceiling_block_size
    request_key = uuid.uuid5(uuid.NAMESPACE_URL, f"ceiling:{series.pk}:{target}")
    if OutboxIntent.objects.filter(tenant_id=run.tenant_id, request_key=request_key).exists():
        return
    run.outbox(
        "ceiling",
        f"series:{series.pk}",
        {
            "subject_key": f"series:{series.pk}",
            "ceiling_spec": {"series_id": str(series.pk), "ceiling": target},
        },
        request_key=request_key,
    )


def publish_ceiling(tenant_id: uuid.UUID, series_id: int, target: int | None = None) -> int:
    """Write the next block's ceiling off-box, then record its confirmation.

    No database lock is held while the store is written. The confirmation is its
    own command, idempotent on the (series, ceiling) pair.
    """
    from core.kernel_models import SeriesCeiling

    series = VoucherSeries.objects.get(pk=series_id)
    current = confirmed_ceiling(series_id)
    wanted = (
        target
        if target is not None
        else max(current, series.next_seq - 1) + series.ceiling_block_size
    )
    if wanted <= current:
        return current
    key = f"ceilings/{tenant_id}/{series_id}/{wanted:020d}.json"
    body = canonical_json(
        {
            "tenant_id": str(tenant_id),
            "series_id": series_id,
            "fy": series.fy,
            "doc_type": series.doc_type,
            "legal_entity_id": series.legal_entity_id,
            "ceiling": wanted,
        }
    ).encode()
    try:
        stored = get_store().put(key, body)
    except OffboxError as exc:
        raise Refusal("SERIES_NOT_READY", f"The ceiling could not be published: {exc}") from exc

    def handler(run: CommandRun) -> CommandResult:
        locked = run.lock(LockRank.SERIES, VoucherSeries.objects.filter(pk=series_id))[0]
        highest = confirmed_ceiling(locked.pk)
        if wanted > highest:
            run.record(
                SeriesCeiling(
                    series_id=locked.pk,
                    ceiling=wanted,
                    object_key=stored.key,
                    object_version=stored.version,
                    sha256=stored.sha256,
                    confirmed_at=run.now,
                )
            )
        return CommandResult(resource_type="series_ceiling", resource_id=str(series_id))

    execute_command(
        Principal(tenant_id=tenant_id, service_code=CEILING_SERVICE),
        CommandSpec(
            action="series.ceiling.confirm",
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            business_input={"key": key, "sha256": stored.sha256},
            subject_key=f"series:{series_id}",
        ),
        handler,
    )
    return wanted


def prepare_series(
    tenant_id: uuid.UUID,
    entity: Any,
    doc_type: str,
    fy: str | None = None,
    *,
    block_size: int = 1000,
) -> VoucherSeries:
    """Create a series and publish its first ceiling (setup, seeds and tests)."""
    with transaction.atomic():
        series = ensure_series(tenant_id, entity, doc_type, fy, block_size=block_size)
    if confirmed_ceiling(series.pk) < series.next_seq:
        publish_ceiling(tenant_id, series.pk)
    return series
