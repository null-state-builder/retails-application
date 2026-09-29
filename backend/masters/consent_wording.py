"""The customer consent wording: which version is current (ticket 15, ST-CMP-6).

Version 1 is the first wording (baseline B13) and has no row. Every version Admin
saves after it is a ``ConsentWording`` row numbered 2 and up, and applies from
the moment it is saved: the newest is current. The till holds the current one
and records its number on every answer it takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.refusals import Refusal, issue
from masters.consent_wording_models import ConsentWording

FIRST_VERSION = 1
#: Longest a question may be: it has to fit on the customer display.
MAX_TEXT = 200


@dataclass(frozen=True)
class Wording:
    version: int
    bill: str
    age: str
    offers: str

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "bill": self.bill,
            "age": self.age,
            "offers": self.offers,
        }


#: Baseline B13: plain words, easy to replace.
VERSION_ONE = Wording(
    version=FIRST_VERSION,
    bill="Send my bill to this number",
    age="Are you under 18?",
    offers="Send me offers and news",
)


def wording_of(row: ConsentWording) -> Wording:
    return Wording(
        version=row.version, bill=row.bill_text, age=row.age_text, offers=row.offers_text
    )


def current_wording(tenant_id: Any) -> Wording:
    """The wording the counter asks with now: the newest saved, else version 1."""
    row = ConsentWording.objects.filter(tenant_id=tenant_id).order_by("-version").first()
    return wording_of(row) if row is not None else VERSION_ONE


def latest_version_number(tenant_id: Any) -> int:
    return current_wording(tenant_id).version


def version_exists(tenant_id: Any, version: int) -> bool:
    if version == FIRST_VERSION:
        return True
    return ConsentWording.objects.filter(tenant_id=tenant_id, version=version).exists()


def parse_text(value: Any, field: str) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > MAX_TEXT:
        raise Refusal(
            "INVALID_REQUEST",
            f"Each question needs words, at most {MAX_TEXT} characters.",
            issues=[issue("INVALID", f"{field} is 1-{MAX_TEXT} characters", field=field)],
        )
    return text
