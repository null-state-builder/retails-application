"""Customer consent at the counter (store operations ticket 15, §6 ST-CMP-6).

The counter asks two separate questions, each off until the customer says yes:
**send my bill** and **send me offers**. The customer answers them on the
customer display; before offers they are asked whether they are under 18, and a
yes there keeps offers off. Staff can withdraw either answer at the counter but
can never say yes for the customer. The phone number stays optional: nothing
here touches a bill.

The till sends one answer at a time, from a queue it keeps on the device, so an
answer given offline arrives later with the time it was given. Each answer is
its own row (``ConsentAnswer``) and what stands for a number is its newest by
that time, so a late-arriving offline answer never overrides a later one, and a
withdrawal takes effect the moment it arrives.

Rules on arrival (refused with a code, never guessed):

* a yes must come from the customer display (``CONSENT_NOT_CUSTOMERS``);
* a yes to offers needs a no to the under-18 question (``CONSENT_UNDER_18``);
* a yes needs the store's ``customer-consent`` switch on (``FEATURE_OFF``), and
  its time must not be ahead of the server's clock by more than a few minutes;
* the number must be a 10-digit mobile and the wording version must exist.

A no or a withdrawal is taken whatever the switch says, and whatever the till's
clock says: a till clock running fast only moves its time back to the server's
(the till's own time is kept in the audit). Refusing a no could only keep
sending a customer messages they said no to.

A replay of an answer already recorded answers with that row before any rule is
looked at again, so a switch turned off after the first arrival cannot turn a
recorded answer into a refused one on the till.

Answers belong to one company: what stands for a number is read only from that
company's stores.

Every answer is audited with the number's standing answer before and after. The
number appears masked in the audit log (last four digits): the log is read by
people who have no need of a customer's phone number.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.refusals import Refusal
from masters.consent_wording import version_exists
from masters.models import Store
from masters.store_feature_registry import CUSTOMER_CONSENT
from masters.store_features import is_feature_on
from sell.models import ConsentAnswer
from sell.services.customers import normalise_mobile

ANSWER_ACTION = "sell.customer_consent.answer"
#: How far ahead of the server's clock a till's time may be before a yes is refused.
CLOCK_SKEW = timedelta(minutes=5)
#: Raised inside the command when another copy of the same answer got there first.
_ALREADY = "CONSENT_ALREADY_RECORDED"


def mobile_or_refuse(raw: str) -> str:
    """The bare 10-digit mobile, or ``VALIDATION``."""
    mobile = normalise_mobile(raw)
    if len(mobile) != 10:
        raise Refusal(
            "VALIDATION",
            "A consent answer needs the customer's 10-digit mobile number.",
            status=400,
        )
    return mobile


def masked(mobile: str) -> str:
    """The number as the audit log shows it: the last four digits only."""
    return "*" * max(len(mobile) - 4, 0) + mobile[-4:]


def _standing(row: ConsentAnswer | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "given": row.given,
        "how": row.how,
        "under_18": row.under_18,
        "wording_version": row.wording_version,
        "answered_at": row.answered_at.isoformat(),
    }


def newest(tenant_id: Any, mobile: str, question: str) -> ConsentAnswer | None:
    """The answer that stands for this number and question in this company."""
    return (
        ConsentAnswer.objects.filter(store__tenant_id=tenant_id, mobile=mobile, question=question)
        .order_by("-answered_at", "-received_at", "-id")
        .first()
    )


def consent_state(tenant_id: Any, mobile: str) -> dict[str, Any]:
    """Both answers standing for this number. ``None`` means never asked (off)."""
    return {
        "mobile": mobile,
        "bill": _standing(newest(tenant_id, mobile, ConsentAnswer.Question.BILL)),
        "offers": _standing(newest(tenant_id, mobile, ConsentAnswer.Question.OFFERS)),
    }


@dataclass(frozen=True)
class Recorded:
    answer: ConsentAnswer
    created: bool


@dataclass(frozen=True)
class _Facts:
    row: dict[str, Any]
    #: The time the till sent, when it was ahead and a no was moved back to now.
    till_clock: datetime | None


def _facts(data: dict[str, Any], now: datetime) -> _Facts:
    """The answer as it will be stored, normalised. No rule is judged here."""
    question = data["question"]
    given = bool(data["given"])
    answered_at: datetime = data["answered_at"]
    till_clock = None
    if not given and answered_at > now + CLOCK_SKEW:
        till_clock, answered_at = answered_at, now
    return _Facts(
        row={
            "mobile": mobile_or_refuse(data["mobile"]),
            "question": question,
            "given": given,
            "how": data["how"],
            "under_18": None if question == ConsentAnswer.Question.BILL else data.get("under_18"),
            "wording_version": int(data["wording_version"]),
            "answered_at": answered_at,
            "till_number": str(data.get("till_number") or "")[:64],
        },
        till_clock=till_clock,
    )


def _same(row: ConsentAnswer, store: Store, facts: _Facts) -> bool:
    f = facts.row
    return (
        row.store_id == store.pk
        and row.mobile == f["mobile"]
        and row.question == f["question"]
        and row.given == f["given"]
        and row.how == f["how"]
        and row.under_18 == f["under_18"]
        and row.wording_version == f["wording_version"]
        # A no moved back to the server's clock carries a different time on
        # every replay; the till's own time was never stored for it.
        and (facts.till_clock is not None or row.answered_at == f["answered_at"])
    )


def _replay(answer_id: uuid.UUID, store: Store, facts: _Facts) -> Recorded | None:
    existing = ConsentAnswer.objects.filter(pk=answer_id).first()
    if existing is None:
        return None
    if not _same(existing, store, facts):
        raise Refusal(
            "CONSENT_CONFLICT",
            "This answer was already recorded with different details.",
            status=409,
        )
    return Recorded(answer=existing, created=False)


def _check(store: Store, facts: _Facts, now: datetime) -> None:
    f = facts.row
    if f["given"] and f["how"] != ConsentAnswer.How.DISPLAY:
        raise Refusal(
            "CONSENT_NOT_CUSTOMERS",
            "Only the customer can say yes, on the customer display. Staff can record a "
            "withdrawal but never a yes.",
            status=400,
        )
    if f["given"] and f["question"] == ConsentAnswer.Question.OFFERS and f["under_18"] is not False:
        raise Refusal(
            "CONSENT_UNDER_18",
            "Offers can be agreed only after the customer says they are not under 18.",
            status=400,
        )
    if f["given"] and f["answered_at"] > now + CLOCK_SKEW:
        raise Refusal(
            "VALIDATION",
            "The answer's time is ahead of head office's clock. Check the till's date and time.",
            status=400,
        )
    if not version_exists(store.tenant_id, f["wording_version"]):
        raise Refusal(
            "VALIDATION",
            "That consent wording version does not exist. Sync the till, then ask again.",
            status=400,
        )
    if f["given"] and not is_feature_on(store, CUSTOMER_CONSENT):
        raise Refusal(
            "FEATURE_OFF",
            "Customer consent is switched off at this store, so a yes cannot be recorded. "
            "Admin can switch it on in Setup, Feature Switches.",
            status=403,
        )


def _principal(store: Store, actor: Any) -> Principal:
    """The person recording it, or - for a login that is not a person - the till's sync."""
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    user_id = getattr(actor, "pk", None)
    if human_id is not None and tenant_id is not None:
        return Principal(tenant_id=tenant_id, human_id=human_id, user_id=user_id)
    return Principal(tenant_id=store.tenant_id, service_code="till-sync", user_id=user_id)


def record_answer(store: Store, actor: Any, data: dict[str, Any]) -> Recorded:
    """Record one answer from the till, once, and audit it.

    A replay of an answer already recorded (the same id and the same facts)
    answers with that row and writes nothing. The same id with different facts
    is refused (``CONSENT_CONFLICT``): the till never reuses an id.
    """
    answer_id: uuid.UUID = data["id"]
    now = timezone.now()
    facts = _facts(data, now)
    replayed = _replay(answer_id, store, facts)
    if replayed is not None:
        return replayed
    _check(store, facts, now)
    f = facts.row
    subject = f"customer_consent:{answer_id}"

    def handler(run: CommandRun) -> CommandResult:
        # The lock is per company and number (the kernel keys it by tenant), so
        # two copies of one answer, or two answers for one number, take turns.
        run.advisory_lock(LockRank.DOCUMENT, [f"consent:{f['mobile']}"])
        if ConsentAnswer.objects.filter(pk=answer_id).exists():
            raise Refusal(_ALREADY, "This answer is already recorded.", status=409)
        before = newest(store.tenant_id, f["mobile"], f["question"])
        row = ConsentAnswer.objects.create(id=answer_id, store=store, staff=actor, **f)
        after = newest(store.tenant_id, f["mobile"], f["question"])
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {
            "mobile": masked(f["mobile"]),
            "question": f["question"],
            "standing": _standing(before),
        }
        run.audit_after = {
            "mobile": masked(f["mobile"]),
            "question": f["question"],
            "answer": {
                **(_standing(row) or {}),
                "till_number": row.till_number,
                **(
                    {"till_clock": facts.till_clock.isoformat()}
                    if facts.till_clock is not None
                    else {}
                ),
            },
            "standing": _standing(after),
        }
        return CommandResult(
            resource_type="consent_answer", resource_id=str(row.pk), status_code=201
        )

    try:
        result = execute_command(
            _principal(store, actor),
            CommandSpec(
                action=ANSWER_ACTION,
                command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"consent-answer:{answer_id}"),
                business_input={
                    "id": str(answer_id),
                    "store": store.code,
                    **{k: v for k, v in f.items() if k != "answered_at"},
                    "answered_at": (facts.till_clock or f["answered_at"]).isoformat(),
                },
                site_id=store.pk,
                subject_key=subject,
            ),
            handler,
        )
    except Refusal as refusal:
        if refusal.code != _ALREADY:
            raise
        # Another copy got there first, under the same lock: answer as a replay.
        again = _replay(answer_id, store, facts)
        if again is None:  # pragma: no cover - the handler just saw it
            raise
        return again
    return Recorded(answer=ConsentAnswer.objects.get(pk=answer_id), created=not result.replayed)
