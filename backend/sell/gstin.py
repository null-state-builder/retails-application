"""Is this thing a GSTIN, and which state is it in (#187, grill Q8).

Two questions, deliberately separate, because the counter answers them at two
different moments and only one of them may ever stop anything.

* **Which state.** The first two characters are the state, and that is what
  decides whether the customer's copy prints CGST + SGST or IGST. It is worked
  out from the characters as typed, and it is worked out the same way here and in
  `till/gstin.ts` - the bill is printed offline, minutes before this server sees
  it, and the two answers have to agree or the paper and the books disagree about
  a tax split.
* **Is it well formed.** A separate check, and a *soft* one: it flags, it never
  refuses (Rule 8, and the acceptance criterion on #187 says so in as many
  words). The customer is standing at the counter holding the garment; a cashier
  who mistyped one character of a fifteen-character code has made a bill head
  office must correct, not a sale the shop should decline.

The rules are the real ones, not a length check. A GSTIN is:

    2 digits   state code (01-38 are the states and union territories; 97 is
               "other territory" and 99 the UN/embassy series)
    10 chars   the holder's PAN - five letters, four digits, one letter
    1 char     the registration count for that PAN in that state (1-9, then A-Z)
    1 char     'Z' by convention, on every registration issued to date
    1 char     a check digit over the first fourteen, mod 36

The checksum is what earns this module its keep. Structure alone passes a
transposed pair of digits, which is the commonest way a GSTIN is mistyped and the
one that quietly costs a customer their input credit.

Nothing here touches the database, so it is a plain function the tests can drive
one string at a time.
"""

from __future__ import annotations

import re

#: The 14 characters the check digit is computed over, in the alphabet the
#: GSTN's own algorithm uses: digits are worth their value, letters carry on
#: from ten. Base 36 throughout, which is why 'Z' is 35 and not a special case.
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: Structure only. The checksum is checked separately so the flag can say which
#: of the two failed - "this is not shaped like a GSTIN" and "this is shaped like
#: one but a character is wrong" send a head-office clerk to different remedies.
_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

#: State codes the GSTN has issued. 01-38 are the states and union territories
#: (38 = Ladakh, the most recent); 97 is "other territory" and 99 the UN and
#: embassy series. Held as data rather than as a range because the set is a
#: government list that grows, and a range would silently bless 39 the day
#: somebody typos it.
VALID_STATE_CODES = frozenset(
    [f"{n:02d}" for n in range(1, 39)] + ["97", "99"],
)

#: What `describe` answers when there is nothing wrong. Empty rather than a word,
#: so a caller can write `if reason:` and mean it.
WELL_FORMED = ""


def normalise(gstin: str) -> str:
    """A GSTIN as it is stored and compared - trimmed, upper case.

    The one place the counter's typing is tidied. A cashier types in whatever
    case the keyboard is in, and a GSTIN differing from itself by case would flag
    every second B2B bill.
    """
    return (gstin or "").strip().upper()


def state_code(gstin: str) -> str:
    """The state the buyer is registered in - the first two characters.

    Taken as typed, without asking whether they are a state code at all, and that
    is on purpose. This is what decides the split printed on the customer's copy,
    the till computes it offline with exactly this rule, and a server that
    second-guessed a malformed one would print one tax on the paper and record
    another in the books. A GSTIN that fails `describe` is flagged for a human;
    it is not silently re-taxed.
    """
    return normalise(gstin)[:2]


def check_digit(first_fourteen: str) -> str:
    """The GSTN's mod-36 check character over the first fourteen.

    Each character's value is multiplied by 1 or 2 alternately (2 on the
    even-numbered positions, counting from one), the product's quotient and
    remainder in base 36 are added together, and the check digit is whatever
    takes the running total up to the next multiple of 36.
    """
    total = 0
    for index, char in enumerate(first_fourteen):
        value = _ALPHABET.index(char)
        product = value * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return _ALPHABET[(36 - total % 36) % 36]


def describe(gstin: str) -> str:
    """Why this is not a GSTIN, or `WELL_FORMED` if it is.

    A sentence rather than a boolean, because what comes out of here is written
    into a `ContinuityFlag`'s details and read by a person at head office who has
    to decide whether to ring the customer or fix a typo.
    """
    value = normalise(gstin)
    if not value:
        return WELL_FORMED
    if len(value) != 15:
        return f"A GSTIN is 15 characters; this one is {len(value)}."
    if not _SHAPE.match(value):
        return "Not shaped like a GSTIN (2 digits, a PAN, an entity character, Z, a check digit)."
    if value[:2] not in VALID_STATE_CODES:
        return f"'{value[:2]}' is not a state code the GSTN issues."
    if value[14] != check_digit(value[:14]):
        return "The check digit does not match - a character is probably mistyped."
    return WELL_FORMED


def is_well_formed(gstin: str) -> bool:
    """`describe` as a yes or no, for callers that only branch on it."""
    return describe(gstin) == WELL_FORMED
