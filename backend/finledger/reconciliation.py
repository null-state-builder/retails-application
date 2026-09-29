"""Bank Reconciliation — matches an uploaded statement's rows against the
`CashLedgerEntry` rows already in the system (BANK/UPI accounts only: CASH
never shows up on a bank statement, and CARD is still-clearing at the point
it posts, a different timing problem this doesn't try to solve).

Purely rule-based (amount + date-window + narration-substring scoring) — a
stopgap while waiting on the bank's own statement API. That is the point of
splitting parsing (`parse_statement`) from matching (`find_candidates`) this
finely: once a bank API is available, it only has to produce the same
`ParsedRow` shape this module already turns CSV/XLSX into, and every line
downstream — matching, review, the receipt — runs unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.db import transaction

from finledger.access import bank_imports, cash_entries
from finledger.models import BankStatementImport, BankStatementLine, CashLedgerEntry

# `UnsupportedFormat` is re-exported deliberately: a statement this module cannot
# read is a reconciliation failure to every caller (`finledger.views` turns it into
# a 400), and none of them should have to reach into the PT-mapper to name it.
from ptmapper.goods_mapper import UnsupportedFormat as UnsupportedFormat
from ptmapper.goods_mapper import read_sheets

#: A candidate must land within this many days of the statement row to be
#: considered at all — wide enough for a NEFT that posted a day or two either
#: side of the bank's value date, narrow enough that a same-amount coincidence
#: three weeks later doesn't get offered.
CANDIDATE_WINDOW_DAYS = 10
#: A single, clearly-best candidate at or above this score auto-links.
AUTO_MATCH_THRESHOLD = 140
#: Below this, a candidate isn't worth showing at all.
SUGGEST_THRESHOLD = 40
#: An auto-match also needs to beat the next-best candidate by this margin —
#: two equally-plausible entries should go to review, not a coin toss.
AUTO_MATCH_MARGIN = 20
MAX_CANDIDATES = 3

_HEADER_SYNONYMS: dict[str, set[str]] = {
    "date": {"date", "txn date", "transaction date", "value date", "posting date", "tran date"},
    "narration": {
        "narration",
        "description",
        "particulars",
        "details",
        "transaction details",
        "remarks",
        "narrative",
    },
    "debit": {"debit", "withdrawal", "withdrawal amt", "withdrawal amount", "debit amount", "dr"},
    "credit": {"credit", "deposit", "deposit amt", "deposit amount", "credit amount", "cr"},
    "balance": {"balance", "closing balance", "available balance", "running balance"},
}

_DATE_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d/%m/%y",
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%m/%d/%Y",
)


@dataclass
class ParsedRow:
    txn_date: date
    narration: str
    debit_paise: int
    credit_paise: int
    balance_paise: int | None


def _norm(cell: object) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(cell or "").strip().lower()).strip()


def _score_header_row(row: list[Any]) -> tuple[int, dict[str, int]]:
    cols: dict[str, int] = {}
    score = 0
    for i, cell in enumerate(row):
        v = _norm(cell)
        if not v:
            continue
        for field, synonyms in _HEADER_SYNONYMS.items():
            if field not in cols and v in synonyms:
                cols[field] = i
                score += 1
    return score, cols


def _detect_header(rows: list[list[Any]]) -> tuple[int, dict[str, int]] | None:
    """The header row isn't always row 0 — bank exports often preamble with
    account info/title rows first, so this scans the first 30 rows for the
    best-scoring one that has at minimum a date and a debit or credit column."""
    best: tuple[int, int, dict[str, int]] | None = None
    for i, row in enumerate(rows[:30]):
        score, cols = _score_header_row(row)
        if "date" in cols and ("debit" in cols or "credit" in cols):
            if best is None or score > best[0]:
                best = (score, i, cols)
    return None if best is None else (best[1], best[2])


def _parse_amount(value: object) -> int:
    """→ paise. Exports write amounts as `"1,234.50"`, blank, or `"-"` for
    whichever of debit/credit doesn't apply to a given row."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return round(value * 100)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "—"}:
        return 0
    try:
        return round(float(text) * 100)
    except ValueError:
        return 0


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_statement(content: bytes, filename: str, content_type: str) -> list[ParsedRow]:
    sheets = read_sheets(content, filename, content_type)
    rows: list[list[Any]] = max((sheet_rows for _, sheet_rows in sheets), key=len, default=[])
    if not rows:
        raise UnsupportedFormat("The file has no rows.")
    header = _detect_header(rows)
    if header is None:
        raise UnsupportedFormat(
            "Could not find a Date + Debit/Credit header row — expected columns "
            "like Date, Narration/Description, Debit/Withdrawal, Credit/Deposit."
        )
    header_i, cols = header
    parsed: list[ParsedRow] = []

    def cell(row: list[Any], field: str) -> object:
        i = cols.get(field)
        return row[i] if i is not None and i < len(row) else None

    for row in rows[header_i + 1 :]:
        txn_date = _parse_date(cell(row, "date"))
        if txn_date is None:
            continue
        debit = _parse_amount(cell(row, "debit"))
        credit = _parse_amount(cell(row, "credit"))
        if debit == 0 and credit == 0:
            continue
        parsed.append(
            ParsedRow(
                txn_date=txn_date,
                narration=str(cell(row, "narration") or "").strip(),
                debit_paise=debit,
                credit_paise=credit,
                balance_paise=_parse_amount(cell(row, "balance")) if "balance" in cols else None,
            )
        )
    if not parsed:
        raise UnsupportedFormat("No transaction rows found under the header.")
    return parsed


def _score(row: ParsedRow, entry: CashLedgerEntry) -> int:
    """0 means "not a candidate at all" — amount and date must both be within
    tolerance before narration/vendor text can add anything."""
    signed_row_amount = row.credit_paise - row.debit_paise
    if entry.amount == signed_row_amount:
        score = 100
    elif abs(entry.amount - signed_row_amount) <= 100:  # ≤ ₹1 rounding slack
        score = 60
    else:
        return 0

    day_diff = abs((entry.created_at.date() - row.txn_date).days)
    if day_diff > CANDIDATE_WINDOW_DAYS:
        return 0
    score += 50 if day_diff == 0 else max(0, 50 - day_diff * 6)

    narration = row.narration.lower()
    if narration:
        doc_suffix = entry.doc_number.split("/")[-1] if entry.doc_number else ""
        if doc_suffix and doc_suffix.lower() in narration:
            score += 15
        for token in re.findall(r"[a-z0-9]{3,}", (entry.description or "").lower()):
            if token in narration:
                score += 8
                break
        vendor_name = entry.vendor.name if entry.vendor else ""
        if vendor_name and any(
            part.lower() in narration for part in vendor_name.split() if len(part) > 3
        ):
            score += 20
    return score


def find_candidates(row: ParsedRow) -> list[tuple[CashLedgerEntry, int]]:
    window_start = row.txn_date - timedelta(days=CANDIDATE_WINDOW_DAYS)
    window_end = row.txn_date + timedelta(days=CANDIDATE_WINDOW_DAYS)
    pool = cash_entries().select_related("vendor").filter(
        account__in=[CashLedgerEntry.Account.BANK, CashLedgerEntry.Account.UPI],
        created_at__date__gte=window_start,
        created_at__date__lte=window_end,
        bank_statement_line__isnull=True,
    )
    scored = sorted(
        ((e, s) for e in pool if (s := _score(row, e)) > 0),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return scored[:MAX_CANDIDATES]


@transaction.atomic
def process_import(batch: BankStatementImport, rows: list[ParsedRow]) -> None:
    """Scores every row against the still-open candidate pool and files each
    one as matched/review/unmatched. Runs inside one transaction so a row
    matched earlier in this same batch is excluded from a later row's
    candidates — Postgres lets a transaction see its own uncommitted writes,
    so `find_candidates`' `bank_statement_line__isnull=True` filter already
    catches this without extra bookkeeping."""
    if not bank_imports().filter(pk=batch.pk).exists():
        raise ValueError("The bank statement import is not owned by the current tenant.")
    matched = 0
    for row in rows:
        candidates = find_candidates(row)
        line = BankStatementLine(
            import_batch=batch,
            txn_date=row.txn_date,
            narration=row.narration,
            debit_paise=row.debit_paise,
            credit_paise=row.credit_paise,
            balance_paise=row.balance_paise,
        )
        if candidates:
            top_entry, top_score = candidates[0]
            runner_up = candidates[1][1] if len(candidates) > 1 else 0
            line.candidates = [
                {
                    "entry_id": e.id,
                    "score": s,
                    "doc_number": e.doc_number,
                    "description": e.description,
                    "amount_paise": e.amount,
                    "date": e.created_at.date().isoformat(),
                }
                for e, s in candidates
            ]
            line.match_confidence = top_score
            if top_score >= AUTO_MATCH_THRESHOLD and top_score - runner_up >= AUTO_MATCH_MARGIN:
                line.status = BankStatementLine.Status.MATCHED
                line.matched_entry = top_entry
                matched += 1
            elif top_score >= SUGGEST_THRESHOLD:
                line.status = BankStatementLine.Status.REVIEW
        line.save()
    batch.row_count = len(rows)
    batch.matched_count = matched
    batch.save(update_fields=["row_count", "matched_count"])
