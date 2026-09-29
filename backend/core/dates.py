"""Reading a day off the wire, once.

`?date=2026-07-31` reaches four places now - the cash summary, the flag list, the
daily-check command and (as a cursor) the till's dataset - and each of them has
to catch the *two* different ways Django's `parse_date` refuses:

* it **answers nothing** for text that is not a date at all ("yesterday");
* it **raises** for text correctly shaped and impossible ("2026-02-30").

Miss the second and a well-formed nonsense date is a 500 rather than a sentence.
It was written out four times before this module existed, comment and all, and a
fifth caller was one copy-paste away from getting only half of it.

The bell's two history reads (#226) ask the same question under a different
name, `?since=`, and share a refusal *body* as well as the parse - one popup
handles both answers, so they must not be two sentences with two codes.
"""

from __future__ import annotations

from datetime import date

from django.utils.dateparse import parse_date

from core.refusals import refusal_body

#: The one code both history reads answer with for an unreadable `?since=`
#: (#226, `api-contract.md`). Named here rather than spelled twice, because the
#: bell handles the refusal in one place and would not survive the two drifting.
INVALID_SINCE = "INVALID_SINCE"


def bad_since(text: str) -> dict[str, str] | None:
    """The refusal `text` earns as a `?since=`, or `None` if it names a day.

    Blank asks nothing and so refuses nothing - both history reads treat an
    absent parameter as their own default window rather than as an error.

    A body, not a response: `core` stays out of HTTP (see `core.refusals`), so
    the view beside the branch that chose it still writes its own `400`.
    """
    if text and parse_day(text) is None:
        return refusal_body(INVALID_SINCE, f"'{text}' is not a date (use 2026-07-31).")
    return None


def parse_day(text: str) -> date | None:
    """The day `text` names, or `None` if it names none.

    One answer for both refusals, so a caller has one branch to write. What to
    *do* about `None` stays the caller's: a query parameter earns a 400, and the
    till's opaque cursor deliberately self-heals into a full bootstrap instead.
    """
    try:
        return parse_date(text)
    except ValueError:
        return None
