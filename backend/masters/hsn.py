"""HSN on every item (store operations PRD §6 ST-CMP-3, ticket 12).

Every sellable item carries an HSN from its official PT. A missing HSN is caught
at PT approval, never at the counter:

* ``refuse_missing_hsn(lines, site_id)`` - PT approval (receipt and opening)
  calls it where the ``hsn-on-every-item`` switch is on at the PT's site. A line
  with no HSN refuses the approval, and the refusal names every such line.
* The till never refuses a bill for it: a line with no HSN takes its tax
  version's "no rule" rate and the bill is flagged ``tax_rule_missing`` (B26).

"No HSN" is blank or not an HSN at all ("NA", "-", "6205.20"): an HSN is digits
only, 4, 6 or 8 of them by default (``settings.KDPS_HSN_DIGITS``, a setting for
Anand to confirm). The counter uses the same definition: under saved tax
settings a line with no HSN matches no rule (``SavedTaxVersion.rule_for``).
The switch is off by default; off, approval works exactly as before (B5).
Its CA sign-off gate closed on 1 October 2026.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from django.conf import settings

from core.refusals import Refusal, issue
from masters.store_feature_registry import HSN_ON_EVERY_ITEM
from masters.store_features import is_feature_on

FEATURE_KEY = HSN_ON_EVERY_ITEM
#: How many lines the refusal's sentence names; every one is in its issues.
NAMED_IN_MESSAGE = 10


def hsn_digits() -> list[int]:
    """The lengths an HSN may have (``settings.KDPS_HSN_DIGITS``); sent to the till."""
    return sorted(set(settings.KDPS_HSN_DIGITS))


def is_hsn(value: Any) -> bool:
    """Is this a well-formed HSN code (digits only, of an allowed length)?"""
    code = str(value or "").strip()
    return re.fullmatch(r"[0-9]+", code) is not None and len(code) in settings.KDPS_HSN_DIGITS


def hsn_rules_on(site_id: int | None) -> bool:
    """Is the switch on at this site?"""
    return site_id is not None and is_feature_on(site_id, FEATURE_KEY)


def line_name(index: int, line: dict[str, Any]) -> str:
    """How a PT line is named to a person: its row number and what it is."""
    describing = line.get("describing") or {}
    what = [
        str(value).strip()
        for value in (line.get("alias_as_used"), describing.get("design"))
        if value and str(value).strip()
    ]
    return f"row {index}" + (f" ({', '.join(what)})" if what else "")


def missing_hsn_lines(lines: Iterable[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """``(row number, line)`` for every line with no HSN, in the PT's own order."""
    return [
        (index, line) for index, line in enumerate(lines, start=1) if not is_hsn(line.get("hsn"))
    ]


def refuse_missing_hsn(lines: list[dict[str, Any]], site_id: int | None) -> None:
    """Refuse a PT approval while any line has no HSN, naming the lines.

    Only where the switch is on at the PT's site; otherwise a no-op (B5).
    """
    if not hsn_rules_on(site_id):
        return
    missing = missing_hsn_lines(lines)
    if not missing:
        return
    names = [line_name(index, line) for index, line in missing]
    shown = ", ".join(names[:NAMED_IN_MESSAGE])
    more = len(names) - NAMED_IN_MESSAGE
    count = f"{len(names)} line{'s' if len(names) != 1 else ''}"
    raise Refusal(
        "PT_HSN_MISSING",
        f"This PT cannot be approved: {count} {'have' if len(names) != 1 else 'has'} no HSN - "
        f"{shown}{f' and {more} more' if more > 0 else ''}. "
        "Send it back so the HSN from the brand's invoice is added.",
        status=422,
        issues=[
            issue(
                "HSN_MISSING",
                f"{line_name(index, line)} has no HSN",
                field="hsn",
                line_key=line.get("line_key"),
            )
            for index, line in missing
        ],
    )
