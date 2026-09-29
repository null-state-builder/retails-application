"""Anchoring chain heads off-box and verifying them (design §4.4, G17).

Every changed chain head is written to the write-once store at least every five
minutes. The verifier recomputes a partition's chain from its rows and compares
the result with the most recent anchor; a mismatch, a missing anchor that should
exist, or a stale anchor opens an evidence-protection exception for X-PLT and
C-OWN.

On the local adapter this proves the mechanism only. Five-minute anchoring on a
real provider, a separate verifier account, continuous audit-log shipping and
the ten-minute owner alert are release proofs that remain to be run.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.apps import apps
from django.db import models
from django.utils import timezone

from core.canonical import canonical_json
from core.evidence import check_partition
from core.offbox import OffboxError, get_store


def anchor_key(tenant_id: uuid.UUID, partition: str, event_id: Any) -> str:
    safe = partition.replace(":", "=").replace("/", "_")
    return f"anchors/{tenant_id}/{safe}/{event_id}.json"


def anchor_changed_heads(tenant_id: uuid.UUID) -> int:
    from core.kernel_models import ChainHead

    store = get_store()
    written = 0
    for head in ChainHead.objects.filter(tenant_id=tenant_id).exclude(last_event_id__isnull=True):
        if head.anchored_hash == head.last_hash:
            continue
        body = canonical_json(
            {
                "tenant_id": str(tenant_id),
                "partition_key": head.partition_key,
                "last_hash": head.last_hash,
                "last_event_id": str(head.last_event_id),
                "anchored_at": timezone.now().isoformat(),
            }
        ).encode()
        try:
            store.put(anchor_key(tenant_id, head.partition_key, head.last_event_id), body)
        except OffboxError:
            continue
        ChainHead.objects.filter(pk=head.pk).update(
            anchored_at=timezone.now(), anchored_hash=head.last_hash
        )
        written += 1
    return written


@dataclass
class VerificationFinding:
    partition_key: str
    problem: str
    #: A stable reason code for the owned exception this finding opens.
    reason_code: str = "CHAIN_VERIFICATION_FAILED"


@dataclass
class VerificationReport:
    """What one verification pass actually covered, and what it found."""

    partitions: int = 0
    rows: int = 0
    anchors: int = 0
    findings: list[VerificationFinding] = field(default_factory=list)

    @property
    def scope(self) -> str:
        return (
            f"{self.partitions} partition(s), {self.rows} row(s) re-hashed, "
            f"{self.anchors} off-box anchor(s) compared"
        )


def sealed_models() -> list[type[models.Model]]:
    """Every table whose rows the sealer chains (they carry ``row_hash``)."""
    return [
        model
        for model in apps.get_models()
        if not model._meta.abstract
        and any(f.attname == "row_hash" for f in model._meta.concrete_fields)
        and any(f.attname == "tenant_id" for f in model._meta.concrete_fields)
    ]


def _partitions_holding_rows(tenant_id: uuid.UUID, model: type[models.Model]) -> set[str]:
    table = model._meta.db_table
    if not any(f.attname == "site_id" for f in model._meta.concrete_fields):
        exists = model._default_manager.filter(tenant_id=tenant_id).exists()
        return {f"{table}:-"} if exists else set()
    sites = model._default_manager.filter(tenant_id=tenant_id).values_list("site_id", flat=True)
    return {f"{table}:{site if site is not None else '-'}"[:160] for site in sites.distinct()}


def _anchored_hashes(store: Any, tenant_id: uuid.UUID, partition: str) -> list[str]:
    safe = partition.replace(":", "=").replace("/", "_")
    return [
        str(json.loads(store.get(obj.key)).get("last_hash"))
        for obj in store.list(f"anchors/{tenant_id}/{safe}")
    ]


def verify_tenant_report(tenant_id: uuid.UUID) -> VerificationReport:
    """Recompute every partition, and hold each against its off-box anchors.

    Three things are checked, each for every protected row:

    * every row re-hashes to its stored hash, and the links form one chain that
      ends at the head (``core.evidence.check_partition``);
    * every hash ever anchored off-box for the partition is still in that chain -
      rewriting history and the head together cannot hide from an anchor the
      database cannot reach - and the head's own anchored hash was written;
    * no protected row sits in a partition with no chain head at all.

    Reads only. The worker runs it on its own clock (``alerts.goods_health``);
    a monitoring page reads the recorded result and never starts one.
    """
    from core.kernel_models import ChainHead

    store = get_store()
    report = VerificationReport()
    models_by_table = {model._meta.db_table: model for model in sealed_models()}
    heads = list(ChainHead.objects.filter(tenant_id=tenant_id).order_by("partition_key"))
    known = {head.partition_key for head in heads}
    for sealed in models_by_table.values():
        for key in sorted(_partitions_holding_rows(tenant_id, sealed) - known):
            report.findings.append(
                VerificationFinding(key, "protected rows with no chain head", "UNCHAINED_ROWS")
            )
    for head in heads:
        table, _, site = head.partition_key.partition(":")
        model = models_by_table.get(table)
        if model is None:
            report.findings.append(
                VerificationFinding(head.partition_key, "unknown table", "UNKNOWN_PARTITION")
            )
            continue
        site_id: Any = None if site in ("", "-") else site
        check = check_partition(tenant_id, model, site_id)
        report.partitions += 1
        report.rows += check.rows
        if check.broken:
            report.findings.append(
                VerificationFinding(head.partition_key, f"chain mismatch: {check.broken[:5]}")
            )
        anchored = _anchored_hashes(store, tenant_id, head.partition_key)
        report.anchors += len(anchored)
        lost = [value for value in anchored if value not in check.hashes]
        if lost:
            report.findings.append(
                VerificationFinding(
                    head.partition_key,
                    f"{len(lost)} anchored hash(es) no longer in the chain",
                    "ANCHORED_HISTORY_MISSING",
                )
            )
        if head.anchored_hash is not None and head.anchored_hash not in anchored:
            report.findings.append(
                VerificationFinding(head.partition_key, "anchor missing off-box", "ANCHOR_MISSING")
            )
    return report


def verify_tenant(tenant_id: uuid.UUID) -> list[VerificationFinding]:
    """Every finding of one verification pass (``verify_tenant_report``)."""
    return verify_tenant_report(tenant_id).findings
