"""Protected evidence intake and scoped download (design §4.4, E120-E121).

Intake order: validate the size and signature, reserve the scoped upload intent,
write the bytes to the write-once store under a deterministic key with no database
lock held, confirm the stored hash, then record the confirmed evidence object.
Business documents link evidence later, and only confirmed evidence.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import timedelta
from typing import Any

from core.canonical import sha256_hex
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.offbox import OffboxError, get_store
from core.refusals import Refusal, issue
from files.goods_models import MAX_EVIDENCE_BYTES, EvidenceObject, UploadIntent

#: ``pt_brand`` is a brand's own PT file (OPS-16): the one kind whose accepted types
#: differ - .xlsx, .xls, .xlsb or .csv, the spreadsheets a brand sends (store and
#: warehouse operations PRD §5.4, "brand files as they come"). Every other kind keeps
#: the general types in ``SIGNATURES``.
PT_BRAND = "pt_brand"
KINDS = frozenset(
    {
        "booking",
        "invoice",
        "pt",
        PT_BRAND,
        "manifest",
        "transport",
        "other",
        "label",
        "count",
        "recovery",
    }
)
RETENTION = timedelta(days=8 * 366)
MAX_ZIP_MEMBERS = 2000
MAX_EXPANDED = 200 * 1024 * 1024
MAX_MEMBER = 100 * 1024 * 1024
MAX_XLSX_ROWS = 50_000

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS = "application/vnd.ms-excel"
XLSB = "application/vnd.ms-excel.sheet.binary.macroEnabled.12"
CSV = "text/csv"

SIGNATURES = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    XLSX: (b"PK\x03\x04",),
}
#: Legacy Excel (.xls) is an OLE2 compound file.
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_media_type(data: bytes, filename: str, kind: str = "") -> str:
    if kind == PT_BRAND:
        return _brand_file_type(data, filename)
    for media_type, prefixes in SIGNATURES.items():
        if any(data.startswith(prefix) for prefix in prefixes):
            if media_type == XLSX and not filename.lower().endswith(".xlsx"):
                break
            return media_type
    raise Refusal(
        "FILE_INVALID",
        "Only PDF, PNG, JPEG and XLSX files are accepted.",
        status=422,
        issues=[
            issue("SIGNATURE", "the file content does not match an accepted type", field="file")
        ],
    )


def _brand_file_type(data: bytes, filename: str) -> str:
    """A brand's PT file: its extension and its content must name the same type."""
    name = filename.lower()
    if name.endswith(".xlsx") and data.startswith(b"PK\x03\x04"):
        return XLSX
    if name.endswith(".xlsb") and data.startswith(b"PK\x03\x04"):
        return XLSB
    if name.endswith(".xls") and data.startswith(OLE2):
        return XLS
    if name.endswith(".csv") and _is_text(data):
        return CSV
    raise Refusal(
        "FILE_INVALID",
        "A brand file must be an .xlsx, .xls, .xlsb or .csv spreadsheet.",
        status=422,
        issues=[
            issue("SIGNATURE", "the file content does not match an accepted type", field="file")
        ],
    )


def _is_text(data: bytes) -> bool:
    """Plain text a CSV reader can take: no NUL byte and no binary file's signature."""
    if b"\x00" in data or any(data.startswith(p) for ps in SIGNATURES.values() for p in ps):
        return False
    if data.startswith(OLE2):
        return False
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # A spreadsheet saved in a single-byte code page; still text, never binary.
        return all(byte >= 0x20 or byte in (0x09, 0x0A, 0x0D) for byte in data)
    return True


def check_workbook(data: bytes, *, binary: bool = False) -> None:
    """A safe OOXML workbook: .xlsx, or .xlsb with ``binary`` (its workbook part is .bin)."""
    workbook, what = ("xl/workbook.bin", "an XLSB") if binary else ("xl/workbook.xml", "an XLSX")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise Refusal("FILE_INVALID", "The workbook is not readable.", status=422) from exc
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS:
        raise Refusal("FILE_INVALID", "The workbook has too many parts.", status=422)
    expanded = 0
    names = set()
    for member in members:
        name = member.filename
        if name.startswith("/") or ".." in name.split("/") or "\\" in name:
            raise Refusal("FILE_INVALID", "The workbook contains an unsafe path.", status=422)
        if member.flag_bits & 0x1:
            raise Refusal("FILE_INVALID", "Encrypted workbooks are not accepted.", status=422)
        if member.file_size > MAX_MEMBER:
            raise Refusal(
                "FILE_INVALID", "A workbook part expands beyond the allowed size.", status=422
            )
        expanded += member.file_size
        names.add(name)
    if expanded > MAX_EXPANDED:
        raise Refusal("FILE_INVALID", "The workbook expands beyond the allowed size.", status=422)
    if "[Content_Types].xml" not in names or workbook not in names:
        raise Refusal("FILE_INVALID", f"This is not {what} workbook.", status=422)
    if any(name.startswith("xl/externalLinks/") for name in names):
        raise Refusal("FILE_INVALID", "Workbooks with external links are not accepted.", status=422)


def stage_upload(
    principal: Principal,
    *,
    command_id: uuid.UUID,
    data: bytes,
    filename: str,
    kind: str,
    scope: dict[str, Any],
    expected_sha256: str,
    contains_fields: list[str],
) -> EvidenceObject:
    if len(data) > MAX_EVIDENCE_BYTES:
        raise Refusal("FILE_TOO_LARGE", "Files larger than 20 MB are not accepted.", status=413)
    if not data:
        raise Refusal("FILE_INVALID", "The file is empty.", status=422)
    if kind not in KINDS:
        raise Refusal("INVALID_REQUEST", "Unknown evidence kind.")
    safe_name = filename.replace("\\", "/").split("/")[-1][:255] or "evidence"
    media_type = detect_media_type(data, safe_name, kind)
    if media_type in (XLSX, XLSB):
        check_workbook(data, binary=media_type == XLSB)
    digest = sha256_hex(data)
    if digest != expected_sha256.lower():
        raise Refusal(
            "FILE_INVALID",
            "The file's SHA-256 does not match what the client declared.",
            status=422,
        )
    object_key = f"evidence/{principal.tenant_id}/{command_id}/{digest}"
    fingerprint_input = {
        "sha256": digest,
        "size": len(data),
        "kind": kind,
        "scope": scope,
        "filename": safe_name,
    }

    def reserve(run: CommandRun) -> CommandResult:
        uploader = run.principal.human_id
        if uploader is None:
            raise Refusal("ACTION_DENIED", "Evidence is uploaded by a named person.")
        intent = UploadIntent.objects.filter(
            tenant_id=run.tenant_id, uploader_id=uploader, command_id=command_id
        ).first()
        if intent is None:
            intent = UploadIntent.objects.create(
                tenant_id=run.tenant_id,
                command_id=command_id,
                uploader_id=uploader,
                scope=scope,
                kind=kind,
                expected_hash=digest,
                expected_size=len(data),
                object_key=object_key,
            )
        return CommandResult(resource_type="upload_intent", resource_id=str(intent.pk))

    reserved = execute_command(
        principal,
        CommandSpec("files.upload.reserve", uuid.uuid5(command_id, "reserve"), fingerprint_input),
        reserve,
    )
    try:
        stored = get_store().put(object_key, data)
    except OffboxError as exc:
        raise Refusal(
            "EVIDENCE_UNAVAILABLE", "The file could not be stored; nothing was recorded."
        ) from exc
    if stored.sha256 != digest or stored.size != len(data):
        raise Refusal("EVIDENCE_UNAVAILABLE", "The stored file could not be confirmed.")

    holder: dict[str, EvidenceObject] = {}

    def confirm(run: CommandRun) -> CommandResult:
        intent = UploadIntent.objects.select_for_update().get(
            pk=uuid.UUID(str(reserved.resource_id))
        )
        existing = EvidenceObject.objects.filter(upload_id=intent.pk).first()
        if existing is None:
            intent.state = UploadIntent.State.CONFIRMED
            intent.save(update_fields=["state"])
            evidence = EvidenceObject(
                upload_id=intent.pk,
                kind=kind,
                filename=safe_name,
                media_type=media_type,
                size=stored.size,
                sha256=stored.sha256,
                object_key=stored.key,
                object_version=stored.version,
                retention_until=run.now + RETENTION,
                scope=scope,
                contains_fields=sorted(set(contains_fields)),
            )
            run.record(evidence)
            holder["evidence"] = evidence
            return CommandResult(
                resource_type="evidence", resource_id=str(evidence.pk), status_code=201
            )
        holder["evidence"] = existing
        return CommandResult(
            resource_type="evidence", resource_id=str(existing.pk), status_code=201
        )

    execute_command(principal, CommandSpec("files.upload", command_id, fingerprint_input), confirm)
    return holder.get("evidence") or EvidenceObject.objects.get(upload__command_id=command_id)


def evidence_dto(evidence: EvidenceObject) -> dict[str, Any]:
    return {
        "filename": evidence.filename,
        "media_type": evidence.media_type,
        "size": evidence.size,
        "sha256": evidence.sha256,
        "download_url": f"/api/goods-v1/files/{evidence.pk}/download",
        "scope": evidence.scope,
        "kind": evidence.kind,
    }


READERS = frozenset(
    {
        "pt.view",
        "stock.view",
        "receive.arrival",
        "booking.manage",
        "pt.prepare",
        "stock.accept",
        "receipt.disposition.decide",
    }
)


def scope_cells(
    scope: dict[str, Any],
) -> tuple[frozenset[tuple[int | None, int | None]], int | None]:
    """Every ``(site, brand)`` cell a declared scope may contain, and its entity.

    Declared sites and brands may hold any pairing, so every pair is a cell; an SBU
    adds its own site/brand pair. A missing dimension is a cell with no site or no
    brand, which only a grant unlimited in that dimension covers. An unreadable or
    unknown scope yields no cells, and no cells are covered by nobody.

    A produced export (GSA-T18) records its cells directly instead, because the
    cartesian product of ConfigScope's arrays cannot say "site A for brand X and
    site B for brand Y" without also claiming A x Y - which would refuse a
    download to the very person the file was produced for.
    """
    from masters.goods_models import Sbu

    if scope.get("scope_kind") == "cells":
        try:
            pairs = {
                (
                    int(cell[0]) if cell[0] is not None else None,
                    int(cell[1]) if cell[1] is not None else None,
                )
                for cell in scope.get("cells") or []
            }
        except (TypeError, ValueError, IndexError):
            return frozenset(), None
        return frozenset(pairs), None

    try:
        site_ids = sorted({int(s) for s in scope.get("site_ids") or []})
        brand_ids = sorted({int(b) for b in scope.get("brand_ids") or []})
        sbu_ids = sorted({str(uuid.UUID(str(s))) for s in scope.get("sbu_ids") or []})
        entity_id = int(scope["entity_id"]) if scope.get("entity_id") else None
    except (TypeError, ValueError):
        return frozenset(), None
    cells: set[tuple[int | None, int | None]] = set()
    if site_ids or brand_ids or not sbu_ids:
        sites: list[int | None] = list(site_ids) or [None]
        brands: list[int | None] = list(brand_ids) or [None]
        cells = {(site, brand) for site in sites for brand in brands}
    if sbu_ids:
        found = list(Sbu.objects.filter(pk__in=sbu_ids).values_list("site_id", "brand_id"))
        if len(found) != len(sbu_ids):
            return frozenset(), None
        cells |= set(found)
    return frozenset(cells), entity_id


def covers_cell(scope: dict[str, Any], site_id: int | None, brand_id: int | None) -> bool:
    """Whether a declared scope reaches one ``(site, brand)`` cell of goods.

    A dimension the scope never names is unbounded there, so an entity-wide or
    brand-wide file still covers the cell; a named dimension has to contain the
    value itself.  An unreadable or unknown scope yields no cells and so reaches
    nothing, which is the same fail-closed answer ``scope_cells`` gives.
    """
    return any(
        (cell_site is None or cell_site == site_id)
        and (cell_brand is None or cell_brand == brand_id)
        for cell_site, cell_brand in scope_cells(scope)[0]
    )


def readable_by(access: Any, evidence: EvidenceObject) -> bool:
    """The original bytes, only for someone covering every cell of the declared scope.

    Each cell needs one grant that holds a reading action and every sensitive field
    the file contains; access to part of a multi-site or multi-brand file is none.
    """
    cells, entity_id = scope_cells(evidence.scope or {})
    fields = evidence.contains_fields or []
    return bool(access.covers_all(READERS, cells, fields, entity_id=entity_id))
