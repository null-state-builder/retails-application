"""Generic document-extraction agents on Google Gemini (Emergent universal key).

Deterministic software does the PT mapping (later); AI is used only to *read*
human documents into a structured draft a person then reviews and confirms — it
is never the final writer (Rule 11 / inbound "AI vs Software"). One placement
today: the booking Receiving Reader (the legacy receiving invoice readers were
deleted in OPS-18).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import tempfile
from typing import Any

from dotenv import load_dotenv

load_dotenv()

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

logger = logging.getLogger(__name__)

_EXCEL_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}
_SUFFIX = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "text/csv": ".csv",
    "text/plain": ".txt",
}


def _excel_to_csv_bytes(content: bytes) -> bytes:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in ws.iter_rows(values_only=True):
        writer.writerow(["" if c is None else c for c in row])
    wb.close()
    return buf.getvalue().encode("utf-8")


def _spool(content: bytes, content_type: str) -> tuple[str, str]:
    """Write bytes to a temp file Gemini can read; returns (path, mime)."""
    mime = content_type
    if content_type in _EXCEL_TYPES:
        content = _excel_to_csv_bytes(content)
        mime = "text/csv"
    suffix = _SUFFIX.get(mime, ".bin")
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return path, mime


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed: dict[str, Any] = json.loads(text)
        return parsed
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            parsed_from_match: dict[str, Any] = json.loads(m.group(0))
            return parsed_from_match
        raise


async def _ask(system: str, prompt: str, file_path: str, mime: str) -> str:
    # No stubs/py.typed marker published for emergentintegrations.
    from emergentintegrations.llm.chat import (
        FileContentWithMimeType,
        LlmChat,
        UserMessage,
    )

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"kdps-extract-{os.path.basename(file_path)}",
        system_message=system,
    ).with_model("gemini", GEMINI_MODEL)
    part = FileContentWithMimeType(file_path=file_path, mime_type=mime)
    reply: str = await chat.send_message(UserMessage(text=prompt, file_contents=[part]))
    return reply


def extract(content: bytes, content_type: str, system: str, prompt: str) -> dict[str, Any]:
    """Run a Gemini extraction on a document blob, returning parsed JSON.

    Raises on API/parse failure so the caller can surface 'upload a clearer
    photo' instead of saving a bad draft. Callers interpolate the exception
    text into a user-facing reply, so gateway/SDK errors are re-raised as a
    clean, generic message — provider internals must never leak to the UI
    (the original cause is kept on ``__cause__`` for server-side logs only).
    """
    if not EMERGENT_LLM_KEY:
        raise RuntimeError("The document reader is not configured.")
    path, mime = _spool(content, content_type)
    try:
        raw = asyncio.run(_ask(system, prompt, path, mime))
    except Exception as exc:  # noqa: BLE001 - never surface provider/gateway internals to the UI
        # Log the real cause to the server only; the UI gets the generic message.
        logger.exception("document reader failed")
        raise RuntimeError("The document reader is temporarily unavailable.") from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return _parse_json(raw)
