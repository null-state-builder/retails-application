"""Legacy season text → the two seasons a year, SS-YY and AW-YY (owner decision, 28 Oct 2026)."""

from __future__ import annotations

from datetime import datetime

import pytest

from ptmapper.legacy_season import normalize_season, season_name, season_order


@pytest.mark.parametrize(("raw", "season"), [
    ("AUTUMN WINTER(Oct-26)", "AW-26"),
    ("AUTUMN WINTER(Jul-25)", "AW-25"),
    ("AUTUMN WINTER(SEP-25)", "AW-25"),
    ("AUTUMN WINTER(Jan-26)", "AW-25"),  # January belongs to the winter that began the year before
    ("AUTUMN WINTER(Mar-26)", "AW-25"),
    ("AUTUMN WINTER(Jan-24)", "AW-23"),
    ("SPRING SUMMER(Jan-26)", "SS-26"),
    ("SPRING SUMMER(Jun-25)", "SS-25"),
    ("SS25", "SS-25"), ("SS-25", "SS-25"), ("AW-24", "AW-24"), ("aw25", "AW-25"), ("KAW24", "AW-24"),
    ("AW-26", "AW-26"),  # already normalised
    ("2025-06-27 00:00:00", "SS-25"), (datetime(2025, 6, 27), "SS-25"), ("25-06-2025", "SS-25"),
    ("15-10-2025", "AW-25"),
    ("", None), (None, None), ("  ", None), ("CORE", None), ("ALLSEASON", None), ("AUTUMN WINTER(Xyz-26)", None),
])
def test_every_legacy_spelling_lands_in_a_half_year_or_unknown(raw: object, season: str | None) -> None:
    assert normalize_season(raw) == season


def test_names_and_order() -> None:
    assert season_name("AW-26") == "Autumn-Winter 2026"
    assert season_name("SS-25") == "Spring-Summer 2025"
    assert season_order("SS-25") < season_order("AW-25") < season_order("SS-26") < season_order("AW-26")
