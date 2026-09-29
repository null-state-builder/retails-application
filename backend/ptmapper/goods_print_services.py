"""Print jobs for official PT lines: real Code 128 labels, printer outcomes, and
missing-copy reprints bound to their evidenced shortfall (design E182-E184, GSA-T11).

A print job freezes what it printed - alias, MRP, the pinned label profile - from
the official line and label configuration at the moment of printing. A later
master, rate or vocabulary change never reaches back into an already-created job;
a reprint is a new, explicitly linked and reasoned job, never a silent rerun of
the old one. No stock effect: labels only append their own evidence (design §7.2).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from core.commands import CommandRun, LockRank
from core.kernel_models import DocumentIdentity, OfficialLine, OfficialVersion
from core.refusals import Refusal, issue
from masters.goods_config import ConfigTarget, check_pinned
from ptmapper.goods_barcode import LabelAliasInvalid, LabelLayoutInvalid, render_label_svg
from ptmapper.goods_models import GoodsPt, PrintEvent, PrintJob
from ptmapper.goods_pt_services import check_contract

#: A print job may only print an official receipt or opening line: those are the
#: purposes acceptance (§E141) resolves label evidence against. A transfer line
#: is proven at destination through its own dispatch-scan contract, not a print job.
SOURCE_PURPOSES = frozenset({DocumentIdentity.Purpose.RECEIPT, DocumentIdentity.Purpose.OPENING})
MAX_LINES_PER_JOB = 200
OUTCOMES = frozenset({"attempted", "confirmed", "partial", "failed", "unknown", "scan_verified"})
#: The subset of OUTCOMES that are themselves a `PrintJob.Status`.
#: `scan_verified` is deliberately excluded: it is corroborating sample
#: evidence recorded as a `PrintEvent`, never a job-level status of its own.
JOB_STATUS_OUTCOMES = frozenset({"attempted", "confirmed", "partial", "failed", "unknown"})
#: Outcomes GSA-T11 requires a full per-line count for: every requested line
#: reports its own usable quantity, known or explicitly unknown - never inferred.
COUNT_REQUIRED_OUTCOMES = frozenset({"confirmed", "partial"})
#: Outcomes that *may* carry counts. §5.8: "a known shortfall permits
#: partial/failed as appropriate" - a printer that failed part-way still leaves
#: a countable number of usable labels, and that count is what sizes the
#: reprint. Counts stay optional here (a failure with nothing to count is
#: ordinary), but a partial list is not: supply one entry or one per line.
COUNT_OPTIONAL_OUTCOMES = frozenset({"failed", "unknown"})
#: The only outcomes carrying enough physical-count evidence to resolve a
#: `label_print_failed` exception - a bare `attempted` dialog signal never does,
#: and neither does a `failed` that merely counted how little came out. Held
#: apart from COUNT_REQUIRED_OUTCOMES, whose members it happens to share, so
#: that widening "may carry counts" can never widen "closes the exception".
RESOLVING_OUTCOMES = frozenset({"confirmed", "partial"})


def load_version(pk: uuid.UUID, *, tenant_id: uuid.UUID) -> OfficialVersion:
    version = (
        OfficialVersion.objects.select_related("document")
        .filter(tenant_id=tenant_id, pk=pk)
        .first()
    )
    if version is None:
        raise Refusal("NOT_FOUND", "That PT version was not found.")
    return version


def source_brand_for_job(job: PrintJob) -> int | None:
    document_id = (
        OfficialVersion.objects.filter(pk=job.pt_version_id)
        .values_list("document_id", flat=True)
        .first()
    )
    return source_brand(document_id) if document_id else None


def source_brand(document_id: uuid.UUID) -> int | None:
    """The brand of a source PT (its GRN arrival's brand); ``None`` when it has none.

    Mirrors ``stockledger.goods_views.source_brand`` exactly - not imported from
    there, since that module already imports ``ptmapper.goods_models.GoodsPt`` and
    a reverse import would cycle the two apps.
    """
    brand_id: int | None = (
        GoodsPt.objects.filter(document_id=document_id)
        .values_list("grn__arrival__brand_id", flat=True)
        .first()
    )
    return brand_id


def _frozen_mrp_paise(line: OfficialLine) -> int:
    payload = line.payload
    calculated = payload.get("calculated") or {}
    supplied = payload.get("supplied") or {}
    raw = calculated.get("mrp_paise") or supplied.get("mrp_paise")
    if raw in (None, ""):
        raise Refusal("PRINT_SOURCE_INVALID", "This line has no MRP to print.", status=422)
    try:
        mrp_paise = int(str(raw))
    except ValueError:
        raise Refusal(
            "PRINT_SOURCE_INVALID", "This line's MRP is not a whole number.", status=422
        ) from None
    if mrp_paise < 0:
        # A price tag cannot carry a minus sign; refuse rather than print one,
        # as `core.goods_money.amount_text` refuses negative paise.
        raise Refusal("PRINT_SOURCE_INVALID", "This line's MRP is negative.", status=422)
    return mrp_paise


def parse_lines(raw: Any) -> list[tuple[uuid.UUID, int]]:
    if not isinstance(raw, list) or not raw:
        raise Refusal("INVALID_REQUEST", "lines must be a non-empty list.")
    if len(raw) > MAX_LINES_PER_JOB:
        raise Refusal("INVALID_REQUEST", f"A print job covers at most {MAX_LINES_PER_JOB} lines.")
    parsed: list[tuple[uuid.UUID, int]] = []
    seen: set[uuid.UUID] = set()
    for index, row in enumerate(raw):
        if not isinstance(row, dict) or set(row) - {"official_line_id", "copies"}:
            raise Refusal("INVALID_REQUEST", f"lines[{index}] is not a valid line entry.")
        try:
            line_id = uuid.UUID(str(row.get("official_line_id")))
        except (TypeError, ValueError):
            raise Refusal(
                "INVALID_REQUEST", f"lines[{index}].official_line_id must be an ID."
            ) from None
        copies = row.get("copies")
        if isinstance(copies, bool) or not isinstance(copies, int) or copies < 1:
            raise Refusal("INVALID_REQUEST", f"lines[{index}].copies must be a positive integer.")
        if line_id in seen:
            raise Refusal("INVALID_REQUEST", f"lines[{index}].official_line_id is repeated.")
        seen.add(line_id)
        parsed.append((line_id, copies))
    return parsed


def _check_copies_within_profile(
    profile: dict[str, Any], lines: list[tuple[uuid.UUID, int]]
) -> None:
    """Both bounds the approved label profile puts on how much one job prints.

    §5.3 bounds the "sum of copies" by the profile, and §5.2 bounds each line's
    "positive copies" by it too: without the sum, many lines each just under the
    per-label ceiling would together clear it. Checked before anything renders,
    so an oversized job costs no symbols.
    """
    copies_limit = int(profile["copies_limit"])
    total_copies = sum(copies for _, copies in lines)
    if total_copies > copies_limit:
        raise Refusal(
            "PRINT_ALIAS_INVALID",
            f"This job's {total_copies} copies exceed this label profile's limit "
            f"of {copies_limit}.",
            status=422,
        )
    if any(copies > copies_limit for _, copies in lines):
        raise Refusal(
            "PRINT_ALIAS_INVALID",
            f"copies exceeds this label profile's limit of {copies_limit}.",
            status=422,
        )


def _latest_counted_event(job: PrintJob) -> PrintEvent | None:
    """The newest event that actually counted something - §5.8's "reviewed prior
    outcome" that a missing-copy reprint pins.

    Recognised by shape, not by outcome name. An `attempted` (a browser dialog)
    and a `scan_verified` (one sample) carry no counts at all, so both are
    skipped structurally: that *is* §5.8's "Scan-back alone does not populate
    unobserved copy counts". An `unknown` does carry a full list of nulls, so a
    later correction to "I cannot vouch for this" rightly supersedes an earlier
    `partial` rather than being stepped over - §5.8's "Corrections append
    events" and "uncertainty stays unknown, never zero".
    """
    for event in job.events.order_by("-recorded_at", "-pk"):
        if event.details.get("usable_counts"):
            return event
    return None


def _validate_missing_copy_reprint(
    reprint_of: PrintJob, lines: list[tuple[uuid.UUID, int]]
) -> None:
    """GSA-T11: a reprint covers only each line's evidenced missing quantity,
    net of whatever any *earlier* reprint of this same job already claimed -
    two separate reprints of one shortfall must not each claim the whole of it.

    Unknown usable quantity must be checked (a fresh outcome recorded) before it
    can be reprinted - never automatically repeated as if the whole job failed.
    Callers must hold `reprint_of` locked (``LockRank.DOCUMENT``) before calling,
    so this reads a consistent view of its outcomes and existing reprints.
    """
    latest = _latest_counted_event(reprint_of)
    usable_by_line: dict[str, int | None] = {}
    if latest is not None:
        for entry in latest.details.get("usable_counts") or []:
            usable_by_line[str(entry.get("official_line_id"))] = entry.get("qty")
    requested_by_line = {
        str(entry.get("official_line_id")): entry.get("copies") for entry in reprint_of.lines
    }
    already_reprinted: dict[str, int] = defaultdict(int)
    for sibling in PrintJob.objects.filter(reprint_of_id=reprint_of.pk):
        for entry in sibling.lines:
            already_reprinted[str(entry.get("official_line_id"))] += int(entry.get("copies") or 0)
    for line_id, copies in lines:
        key = str(line_id)
        requested = requested_by_line.get(key)
        if requested is None:
            raise Refusal(
                "PRINT_SOURCE_INVALID",
                "This line was not on the job being reprinted.",
                status=422,
            )
        usable = usable_by_line.get(key)
        if usable is None:
            raise Refusal(
                "PRINT_ALIAS_INVALID",
                "This line's usable count is unknown; check it before reprinting.",
                status=422,
            )
        missing = max(int(requested) - int(usable) - already_reprinted[key], 0)
        if copies > missing:
            raise Refusal(
                "PRINT_ALIAS_INVALID",
                f"A reprint cannot exceed the evidenced missing quantity ({missing}).",
                status=422,
            )


def create_print_job(
    run: CommandRun,
    *,
    version: OfficialVersion,
    site_id: int,
    lines: list[tuple[uuid.UUID, int]],
    template_version_id: uuid.UUID,
    reprint_of_id: uuid.UUID | None,
    reason_code: str | None,
) -> PrintJob:
    check_contract(site_id)
    if version.document.purpose not in SOURCE_PURPOSES:
        raise Refusal(
            "PRINT_SOURCE_INVALID",
            "Labels print only for an official receipt or opening PT line.",
            status=422,
        )
    official_lines = {
        line.pk: line
        for line in OfficialLine.objects.filter(
            version_id=version.pk, pk__in=[line_id for line_id, _ in lines]
        )
    }
    missing_lines = [str(line_id) for line_id, _ in lines if line_id not in official_lines]
    if missing_lines:
        raise Refusal(
            "PRINT_SOURCE_INVALID",
            "One or more lines are not on this PT version.",
            status=422,
            issues=[
                issue("NOT_FOUND", "not on this PT version", field="lines", line_key=line_id)
                for line_id in missing_lines
            ],
        )

    template = check_pinned(
        run.tenant_id,
        "label",
        template_version_id,
        ConfigTarget.of(run.now, site_id=site_id),
        code="PRINT_SOURCE_INVALID",
        path="template_version_id",
        status=422,
    )
    profile = template.payload
    _check_copies_within_profile(profile, lines)

    reprint_of: PrintJob | None = None
    if reprint_of_id is not None:
        # Locked (DOCUMENT rank): validating against its usable counts and
        # sibling reprints must see a consistent snapshot, not race a
        # concurrent outcome or reprint against the same job.
        locked = run.lock(
            LockRank.DOCUMENT, PrintJob.objects.filter(pk=reprint_of_id, pt_version_id=version.pk)
        )
        reprint_of = locked[0] if locked else None
        if reprint_of is None:
            raise Refusal(
                "PRINT_SOURCE_INVALID",
                "The job being reprinted was not found on this PT version.",
                status=422,
            )
        if not reason_code:
            raise Refusal("PRINT_ALIAS_INVALID", "A reprint needs a reason.", status=422)
        _validate_missing_copy_reprint(reprint_of, lines)

    rendered_lines: list[dict[str, Any]] = []
    for line_id, copies in lines:
        official_line = official_lines[line_id]
        alias = str(official_line.payload.get("alias_as_used") or "")
        mrp_paise = _frozen_mrp_paise(official_line)
        try:
            rendered = render_label_svg(
                alias=alias,
                mrp_paise=mrp_paise,
                width_mm=int(profile["width_mm"]),
                height_mm=int(profile["height_mm"]),
                printer_dpi=int(profile["printer_dpi"]),
                min_module_dots=int(profile["min_module_dots"]),
                max_payload_characters=int(profile["max_payload_characters"]),
            )
        except LabelAliasInvalid as exc:
            raise Refusal("PRINT_ALIAS_INVALID", str(exc), status=422) from exc
        except LabelLayoutInvalid as exc:
            raise Refusal("PRINT_LAYOUT_INVALID", str(exc), status=422) from exc
        rendered_lines.append(
            {
                "official_line_id": str(line_id),
                "alias_as_used": alias,
                "mrp_paise": str(mrp_paise),
                "copies": copies,
                "svg": rendered.svg,
            }
        )

    return PrintJob.objects.create(
        tenant_id=run.tenant_id,
        pt_version_id=version.pk,
        site_id=site_id,
        lines=rendered_lines,
        template_version_id=template.pk,
        label_profile=profile,
        reprint_of=reprint_of,
        reason_code=reason_code,
        status=PrintJob.Status.PREPARED,
        command_key_id=run.key_id,
    )


def _usable_count_entry(
    entry: Any, index: int, requested_by_line: dict[str, int], seen: set[str]
) -> dict[str, Any]:
    at = f"usable_counts[{index}]"
    if not isinstance(entry, dict) or set(entry) - {"official_line_id", "qty"}:
        raise Refusal("INVALID_REQUEST", f"{at} is not valid.")
    try:
        line_id = str(uuid.UUID(str(entry.get("official_line_id"))))
    except (TypeError, ValueError):
        raise Refusal("INVALID_REQUEST", f"{at}.official_line_id must be an ID.") from None
    if line_id not in requested_by_line:
        raise Refusal("INVALID_REQUEST", f"{at} names a line not on this job.")
    if line_id in seen:
        raise Refusal("INVALID_REQUEST", f"{at} repeats a line.")
    seen.add(line_id)
    qty = entry.get("qty")
    if qty is not None:
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
            raise Refusal("INVALID_REQUEST", f"{at}.qty must be a non-negative integer or null.")
        if qty > requested_by_line[line_id]:
            raise Refusal("INVALID_REQUEST", f"{at}.qty cannot exceed the requested copies.")
    return {"official_line_id": line_id, "qty": qty}


def _check_outcome_consistency(outcome: str, validated: list[dict[str, Any]]) -> None:
    """§5.8: "All full counts permit confirmed; a known shortfall permits
    partial/failed as appropriate; uncertainty stays unknown, never zero." The
    chosen outcome must not contradict what the counts themselves show."""
    if not validated:
        return
    known = [entry for entry in validated if entry["qty"] is not None]
    shortfall = any(entry["qty"] < entry["requested"] for entry in known)
    if outcome == "confirmed" and (shortfall or len(known) != len(validated)):
        raise Refusal(
            "INVALID_REQUEST",
            "confirmed requires every line's usable count to be known and equal to its "
            "requested copies.",
        )
    if outcome == "partial" and not shortfall:
        raise Refusal(
            "INVALID_REQUEST",
            "partial requires at least one line's known usable count to fall short of its "
            "requested copies.",
        )
    if outcome == "failed" and known and not shortfall:
        # A failure that counted a full set of usable labels contradicts itself.
        # Counts stay optional for `failed`, so this only judges what was given.
        raise Refusal(
            "INVALID_REQUEST",
            "failed cannot report every line's usable count as complete.",
        )


def _validate_usable_counts(
    raw: Any, requested_by_line: dict[str, int], outcome: str
) -> list[dict[str, Any]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise Refusal("INVALID_REQUEST", "usable_counts must be a list.")
    seen: set[str] = set()
    validated = [
        _usable_count_entry(entry, index, requested_by_line, seen)
        for index, entry in enumerate(raw)
    ]
    counts_required = outcome in COUNT_REQUIRED_OUTCOMES
    # Optional for `failed`/`unknown`, but never half a picture: once any line is
    # counted, every line is, so the evidence a reprint is sized from is whole.
    if (counts_required or (validated and outcome in COUNT_OPTIONAL_OUTCOMES)) and len(
        validated
    ) != len(requested_by_line):
        raise Refusal(
            "INVALID_REQUEST",
            "A physical-count confirmation needs one usable_counts entry per job line.",
        )
    _check_outcome_consistency(
        outcome,
        [
            {**entry, "requested": requested_by_line[entry["official_line_id"]]}
            for entry in validated
        ],
    )
    return validated


def _verify_scan_back(
    job: PrintJob, scanned_alias: str | None, matched_line_id: str | None, outcome: str
) -> None:
    """E183 step 9: a scanned sample must match the frozen alias of the line it
    claims to verify. A mismatch is a discrepancy (PRINT_VERIFY_FAILED), never
    an automatic master change - and it proves only that one sample, never
    every requested copy (GSA-T11)."""
    if scanned_alias is None and matched_line_id is None:
        # `scan_verified` is the one outcome whose entire content is the scan:
        # it carries no counts and takes no status, so without the scan it would
        # record an empty claim that a sample was verified. Every other outcome
        # stands on its own evidence and may omit the scan.
        if outcome == "scan_verified":
            raise Refusal(
                "INVALID_REQUEST",
                "A scan-verified outcome needs the scanned alias and the line it matched.",
            )
        return
    if scanned_alias is None or matched_line_id is None:
        raise Refusal("INVALID_REQUEST", "scanned_alias and matched_line_id are required together.")
    line = next(
        (entry for entry in job.lines if str(entry.get("official_line_id")) == matched_line_id),
        None,
    )
    if line is None:
        raise Refusal("INVALID_REQUEST", "matched_line_id names a line not on this job.")
    if line.get("alias_as_used") != scanned_alias:
        raise Refusal(
            "PRINT_VERIFY_FAILED",
            "The scanned alias does not match this line's frozen alias.",
            status=409,
        )


def _status_advances_to(current: str, outcome: str) -> bool:
    """Whether `outcome` may become the job's status.

    §5.2: "status changes are PrintEvent projections" - but the projection is of
    the strongest physical evidence recorded, not of the last signal received.
    `scan_verified` is corroborating sample evidence with no status of its own
    (`PrintJob.Status` has no such member, and the value does not even fit the
    column). `attempted` is a browser dialog: §5.2 says "browser afterprint alone
    cannot yield confirmed", and its converse holds just as strongly - pressing
    Print again must not unmake a counted outcome. Left unguarded it also walks a
    *failed* job back into `stockledger.goods_acceptance.PRINTED`, letting a
    print that failed vouch for goods at acceptance. Every counted outcome still
    overwrites freely, so §5.8's "Corrections append events" keeps working.
    """
    if outcome not in JOB_STATUS_OUTCOMES:
        return False
    if outcome == "attempted":
        return current in {PrintJob.Status.PREPARED, PrintJob.Status.ATTEMPTED}
    return True


def _resolve_reprinted_ancestors(run: CommandRun, job: PrintJob) -> None:
    """A reprint that actually printed closes the failure it was made to fix.

    The exception is keyed to the job that failed, and a reprint is always a new
    row, so the successor must name its predecessor's own key - the same shape as
    `goods_manifest_services._close_superseded_investigations`. Nothing is ever
    re-pointed. The whole chain resolves, not one hop: a reprint of a reprint
    evidences that the root shortfall is covered too, and a root left open runs
    its SLA clock against an owner with nothing left to do.
    """
    seen: set[uuid.UUID] = set()
    ancestor_id = job.reprint_of_id
    while ancestor_id is not None and ancestor_id not in seen:
        seen.add(ancestor_id)
        resolve_exceptions(
            run,
            kind="label_print_failed",
            subject_key=f"print_job:{ancestor_id}",
            reason_code="REPRINTED",
        )
        ancestor_id = (
            PrintJob.objects.filter(pk=ancestor_id).values_list("reprint_of_id", flat=True).first()
        )


def record_outcome(
    run: CommandRun,
    *,
    job_id: uuid.UUID,
    expected_revision: int | None,
    outcome: str,
    usable_counts: Any,
    reason_code: str | None,
    scanned_alias: str | None = None,
    matched_line_id: str | None = None,
) -> PrintJob:
    jobs = run.lock(LockRank.DOCUMENT, PrintJob.objects.filter(pk=job_id))
    if not jobs:
        raise Refusal("NOT_FOUND", "That print job was not found.")
    job: PrintJob = jobs[0]
    check_contract(job.site_id)
    if job.revision != expected_revision:
        raise Refusal("REVISION_SUPERSEDED", "This print job changed after you loaded it.")

    requested_by_line = {
        str(entry.get("official_line_id")): entry.get("copies") for entry in job.lines
    }
    validated_counts = _validate_usable_counts(usable_counts, requested_by_line, outcome)
    _verify_scan_back(job, scanned_alias, matched_line_id, outcome)

    run.record(
        PrintEvent(
            job=job,
            outcome=outcome,
            details={
                "usable_counts": validated_counts,
                "reason_code": reason_code,
                "scanned_alias": scanned_alias,
                "matched_line_id": matched_line_id,
            },
            event_at=run.now,
        )
    )
    job.revision += 1
    update_fields = ["revision"]
    if _status_advances_to(job.status, outcome):
        job.status = outcome
        update_fields.append("status")
    job.save(update_fields=update_fields)

    subject_key = f"print_job:{job.pk}"
    if outcome == "failed":
        open_exception(
            run,
            kind="label_print_failed",
            site_id=job.site_id,
            subject_key=subject_key,
            reason_code=reason_code or "PRINT_FAILED",
            # Per-occurrence, not per-job: `open_exception` returns any row -
            # open or resolved - that already carries this key (design's
            # idempotent-open contract), so a stable per-job key would silently
            # no-op a second failure after the first one had already resolved.
            source_event_key=run.key_id,
            allowed_resolution_actions=["ptmapper/print-jobs"],
        )
    elif outcome in RESOLVING_OUTCOMES:
        # Only genuine physical-count evidence resolves this - not a bare
        # `attempted` dialog signal, which is exactly the evidence GSA-T11 says
        # never proves a physical outcome on its own.
        resolve_exceptions(
            run, kind="label_print_failed", subject_key=subject_key, reason_code="REPRINTED"
        )
        _resolve_reprinted_ancestors(run, job)

    return job


def print_job_data(job: PrintJob) -> dict[str, Any]:
    return {
        "pt_version_id": str(job.pt_version_id),
        "site_id": job.site_id,
        "state": job.status,
        "template_version_id": str(job.template_version_id) if job.template_version_id else None,
        "reprint_of_id": str(job.reprint_of_id) if job.reprint_of_id else None,
        "reason_code": job.reason_code,
        "labels": job.lines,
        "events": [
            {
                "id": str(event.pk),
                "outcome": event.outcome,
                "actor_id": str(event.actor_id) if event.actor_id else None,
                "recorded_at": event.recorded_at.isoformat(),
                "usable_counts": event.details.get("usable_counts") or [],
                "reason_code": event.details.get("reason_code"),
                "scanned_alias": event.details.get("scanned_alias"),
                "matched_line_id": event.details.get("matched_line_id"),
            }
            for event in job.events.order_by("recorded_at", "pk")
        ],
    }
