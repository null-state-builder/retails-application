"""One row of the access matrix: how to read it, replace it and diff it (#173).

``Role.section_access`` is a JSON blob, and four different callers had grown
their own idea of what a cell is - the grid endpoint, the replacement the editor
sends, the validator, and the floor. Each coerced "capability, fail closed to
none" its own way, which is three chances for one of them to fail *open*.

So the shape lives here, once, and everything that touches a row goes through
it. The rules the whole module holds to:

· a row states **all thirteen sections**, always. A short stored row - a
  hand-made role, or one seeded before a section existed - reads as ``none`` on
  the sections it omits, which is the same answer ``require_section`` gives it.
· anything unreadable is ``none``. A cell that is not a dict, a capability that
  is not a string, a rung off the ladder: all of them narrow access, never
  widen it.
· a replacement **replaces**. A section the caller left out is removed, not
  kept, because the other reading would make a half-sent form silently grant
  whatever the role held before.
· a **seed adds only**. The ratified sheet is starting content, so it fills the
  sections a stored row does not state and never overwrites one it does.
"""

from __future__ import annotations

from typing import Any

from accounts.sections import (
    CAP_NONE,
    CAPABILITY_ORDER,
    CAPABILITY_WORDS,
    SECTION_CODES,
    is_valid_capability,
    is_valid_section,
)

#: ``{section: {capability, label}}`` - the stored shape, and the wire shape.
Row = dict[str, dict[str, str]]


def capability_of(entry: Any) -> str:
    """The rung a cell holds or asks for, fail-closed to ``none``."""
    if not isinstance(entry, dict):
        return CAP_NONE
    capability = entry.get("capability", CAP_NONE)
    if not isinstance(capability, str) or not is_valid_capability(capability):
        return CAP_NONE
    return capability


def stored_row(role: Any) -> Row:
    """A role's stored access, stated for all thirteen sections."""
    stored = role.section_access if isinstance(role.section_access, dict) else {}
    return {
        section: {
            "capability": capability_of(stored.get(section)),
            "label": _label_of(stored.get(section)),
        }
        for section in SECTION_CODES
    }


def invalid_cells(requested: dict[str, Any]) -> list[str]:
    """Every unknown section and off-ladder rung in one answer, not the first.

    Read on the *raw* body rather than through ``capability_of``, because a
    typo'd rung must be refused out loud - silently coercing "manger" to
    ``none`` would answer 202 to an edit that did the opposite of what was asked.
    """
    problems = []
    for section, entry in requested.items():
        if not is_valid_section(section):
            problems.append(f"Unknown section {section!r}")
            continue
        capability = entry.get("capability") if isinstance(entry, dict) else None
        if not is_valid_capability(capability or ""):
            problems.append(f"{section}: capability must be one of {', '.join(CAPABILITY_ORDER)}")
    return problems


def replacement_row(requested: dict[str, Any], *, current: Row) -> Row:
    """The row to store, from the row the screen sent.

    The ratified sheet wording ("Expenses only (create)") is **kept on every
    cell whose rung did not move** - it is the ratified "why this role sees what
    it sees", and an editor that rewrote all thirteen labels because a human
    touched one would erase the sheet a cell at a time. A cell that *did* move
    has no sheet sentence left that is true of it, so it takes the ladder's own
    plain word instead.
    """
    out: Row = {}
    for section in SECTION_CODES:
        entry = requested.get(section)
        capability = capability_of(entry)
        held = current.get(section, {})
        if capability == held.get("capability") and held.get("label"):
            label = str(held["label"])
        else:
            label = _label_of(entry) or CAPABILITY_WORDS.get(capability, capability)
        out[section] = {"capability": capability, "label": label[:120]}
    return out


def seeded_row(stored: Any, *, default: Row) -> Row:
    """The row to store when seeding a role that may already exist (#224).

    Additive only, which is the one shape that shuts both traps:

    · a cell the stored row already states is kept **verbatim**. An access
      change two administrators agreed and audited used to be written back to
      the sheet by any redeploy that re-ran the seed, silently and with nothing
      on any screen to say so;
    · a cell it does not state is filled from the ratified sheet, so a section
      added after a role was seeded still reaches that role. A seed that simply
      skipped existing roles would trade this defect for the opposite one.

    "States it" is presence of the key, exactly as the backfill migrations read
    it - not readability. A cell stored unreadable already grants nothing
    (``capability_of`` fail-closes), and re-granting the sheet's rung over it
    would be this function widening access on its own initiative. For the same
    reason a key outside ``SECTION_CODES`` rides along untouched: no gate reads
    it, and removing a key is a migration's job, not a seed's.

    Two things this does *not* do. It never narrows a kept cell to a floor that
    has since tightened - that is ``floors.clamp_to_floors``, which the seed
    applies on top. And it never re-ratifies the sheet: a cell whose ratified
    value changes reaches live rows as an explicit migration carrying the new
    value (see ``0005_add_staff_section_access``), never as a silent re-seed.
    """
    out: Row = dict(stored) if isinstance(stored, dict) else {}
    for section in SECTION_CODES:
        if section not in out:
            out[section] = dict(default[section])
    return out


def diff(before: Row, after: Row) -> list[dict[str, str]]:
    """The cells whose rung moved, old to new - the audit trail of one edit."""
    return [
        {
            "section": section,
            "from": before[section]["capability"],
            "to": after[section]["capability"],
        }
        for section in SECTION_CODES
        if before[section]["capability"] != after[section]["capability"]
    ]


def _label_of(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    label = entry.get("label")
    return label.strip() if isinstance(label, str) else ""
