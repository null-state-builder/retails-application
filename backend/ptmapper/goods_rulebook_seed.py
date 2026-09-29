"""The KDPS starter rulebook, and the loader that puts it into goods-v1 (OPS-14).

Carries into the one governed rulebook (``ptmapper.goods_rulebook``) what the legacy
PT mapper knew; its rule tables were deleted with legacy receiving (OPS-18):

* the real KDPS vocabulary - the master sheet(s) under
  ``docs/data-from-kdps/05-reference-data/`` (read by path, never moved), plus a
  rolling window of month seasons - becomes approved ``vocabulary`` configuration,
  one line per dimension. Values already in force are kept; a value already present
  by key or label is not added twice;
* the master sheet's ITEM -> SUB CATEGORY / TYPE helper becomes ``item`` rules;
* the starter aliases and keyword rules (the data below, first written for the
  legacy mapper's seed) become confirmed attribute rules.

A rule whose target is not an approved value (or, for BRAND, not a brand master)
is reported, never forced. Everything is idempotent: a second run adds nothing.

Like ``accounts.goods_demo``, this writes approved configuration and effective
masters directly under the ``seed`` service - so it runs for synthetic tenants
only. A real tenant's vocabulary and rules go through Configuration and
PT Work -> Mapping rules, where a person proposes and another approves.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import openpyxl

from core.canonical import content_hash
from core.commands import CommandResult, CommandRun, CommandSpec, database_now, execute_command
from core.refusals import Refusal
from masters.goods_identity_models import GovernanceState, SourceCrosswalk
from masters.goods_identity_services import (
    ANY_ISSUER,
    DEFAULT_SOURCE,
    ITEM_ISSUER,
    KEYWORD_ISSUER,
    brand_issuer,
    record_master_version,
    save_master,
    vocabulary,
)
from ptmapper.goods_mapper import season_label
from ptmapper.goods_rulebook import Rulebook, compact, match_key

#: Master sheet column -> goods-v1 vocabulary dimension. BRAND (column 1) names
#: brand masters, not a vocabulary; GST % (column 9) is not a mapped PT column.
DIM_COLS = {
    0: "season",
    2: "colour",
    3: "gender",
    4: "sub_category",
    5: "type",
    6: "item",
    7: "fit",
    8: "size",
}
#: Columns 10 and 11 of the master sheet: ITEM's suggested SUB CATEGORY and TYPE.
HELPER_COLS = {10: "sub_category", 11: "type"}

#: Dimensions whose rules read a column's own cell: a rule that maps a value to
#: itself adds nothing there, because an approved value already matches exactly.
CELL_DIMENSIONS = frozenset({"season", "colour", "gender", "fit", "size", "brand"})

SEASON_WINDOW_START = date(2025, 1, 1)
SEASON_WINDOW_MONTHS_AHEAD = 18

SEED_SERVICE = "seed"
SEED_REASON = "KDPS_RULEBOOK_SEED"


# ----------------------------------------------------------------------------- starter data
# First written for the legacy mapper's seed; this module is now its only home.

BRAND_ALIASES = {
    "FM": "FLYING MACHINE",
    "PJ": "PETER ENGLAND",
    "PE": "PETER ENGLAND",
    "GO COLORS": "GOCOLORS",
    "GO COLOURS": "GOCOLORS",
    "BLACKBERRY": "BLACKBERRYS",
    "BLACKBERRY'S": "BLACKBERRYS",
    "VH": "VAN HEUSEN",
    "AS": "ALLEN SOLLY",
    "LP": "LOUIS PHILIPPE",
    "US POLO": "U S POLO ASSN",
    "USPOLO": "U S POLO ASSN",
    "U.S. POLO ASSN": "U S POLO ASSN",
    "JOCKEY PARAS": "JOCKEY",
    "JOCKEY NARAYANI": "JOCKEY",
    "JOCKEY NARVADA": "JOCKEY",
    "JOCKEY DD SALES": "JOCKEY",
    "SPYKAR": "SPYKAR",
    "KILLER JUNIOR": "KILLER",
    "JUNIOR KILLER": "KILLER",
}

# Brand shade string → one of KDPS's ~18 real colour buckets. (PREMIUM / ECONOMY /
# MEDIUM are price tiers that already live in the COLOR vocabulary and pass through.)
COLOR_ALIASES = {
    # BLUE family
    "LIGHT BLUE": "BLUE",
    "DARK BLUE": "BLUE",
    "SKY BLUE": "BLUE",
    "SKY": "BLUE",
    "ROYAL BLUE": "BLUE",
    "ROYAL": "BLUE",
    "MID BLUE": "BLUE",
    "BLUE BLACK": "BLUE",
    # NB: no bare "DENIM" alias — "DENIM" is as often a fabric/category as a shade, and
    # some files expose a product group in the colour column. "DENIM BLUE"/"DENIM BLU"
    # are unambiguous shades and stay.
    "DENIM BLUE": "BLUE",
    "DENIM BLU": "BLUE",
    "BLUE DN": "BLUE",
    "L BLU": "BLUE",
    "BL": "BLUE",
    "INDIGO": "BLUE",
    "INDIGO BLUE": "BLUE",
    "INDIGO MEL": "BLUE",
    "MID INDIGO": "BLUE",
    "DARK INDIGO BLUE": "BLUE",
    "DEEP INDIGO BLUE": "BLUE",
    "LIGHT INDIGO": "BLUE",
    "SULPHUR BLUE": "BLUE",
    "SULPHUR": "BLUE",
    "AIRFORCE": "BLUE",
    "AIR FORCE": "BLUE",
    "AIRFORCE BLUE": "BLUE",
    "BT AIRFORCE": "BLUE",
    "POWDER BLUE": "BLUE",
    "POWDER": "BLUE",
    "CORN BLUE": "BLUE",
    "COBALT": "BLUE",
    "ELECTRIC BLUE": "BLUE",
    "STEEL BLUE": "BLUE",
    "ICE BLUE": "BLUE",
    "AQUA BLUE": "BLUE",
    "DENIM BLU DN": "BLUE",
    # NAVY
    "NAVY BLUE": "NAVY",
    "TNAVY": "NAVY",
    "NVY": "NAVY",
    "DARK NAVY": "NAVY",
    "INK BLUE": "NAVY",
    "INK": "NAVY",
    "MIDNIGHT": "NAVY",
    "MIDNIGHT BLUE": "NAVY",
    "NAVI": "NAVY",
    # TEAL
    "TEAL BLUE": "TEAL",
    "TEAL GREEN": "TEAL",
    "TURQUOISE": "TEAL",
    "TURQUOISE BLUE": "TEAL",
    "TURQ": "TEAL",
    "PEACOCK": "TEAL",
    "PEACOCK BLUE": "TEAL",
    "PEACOCK GREEN": "TEAL",
    "AQUA": "TEAL",
    "AQUA GREEN": "TEAL",
    "CYAN": "TEAL",
    # GREEN
    "SEA GREEN": "GREEN",
    "PISTA": "GREEN",
    "PISTA GREEN": "GREEN",
    "LEAF GREEN": "GREEN",
    "LIME": "GREEN",
    "LIME GREEN": "GREEN",
    "BOTTLE GREEN": "GREEN",
    "DARK GREEN": "GREEN",
    "LIGHT GREEN": "GREEN",
    "FOREST GREEN": "GREEN",
    "FOREST": "GREEN",
    "PARROT GREEN": "GREEN",
    "PARROT": "GREEN",
    "EMERALD": "GREEN",
    "MINT": "GREEN",
    "MINT GREEN": "GREEN",
    "FERN": "GREEN",
    "KIWI": "GREEN",
    "NEON GREEN": "GREEN",
    "APPLE GREEN": "GREEN",
    "GREEN MEL": "GREEN",
    # OLIVE
    "OLIVE GREEN": "OLIVE",
    "ARMY": "OLIVE",
    "ARMY GREEN": "OLIVE",
    "MILITARY": "OLIVE",
    "MILITARY GREEN": "OLIVE",
    "MOSS": "OLIVE",
    "MOSS GREEN": "OLIVE",
    "MEHENDI": "OLIVE",
    "MEHANDI": "OLIVE",
    "KHAKI": "OLIVE",
    "KHAKHI": "OLIVE",
    "DARK OLIVE": "OLIVE",
    "OIL GREEN": "OLIVE",
    # RED
    "BRIGHT RED": "RED",
    "DARK RED": "RED",
    "CHERRY": "RED",
    "CHERRY RED": "RED",
    "SCARLET": "RED",
    "CRIMSON": "RED",
    "BLOOD RED": "RED",
    "FERRARI RED": "RED",
    "TOMATO": "RED",
    "TOMATO RED": "RED",
    "CHILLI": "RED",
    "CHILLI RED": "RED",
    # MAROON
    "WINE": "MAROON",
    "WINE RED": "MAROON",
    "DEEP MAROON": "MAROON",
    "DARK MAROON": "MAROON",
    "BURGUNDY": "MAROON",
    "MEHROON": "MAROON",
    "MAHROON": "MAROON",
    "DK WNE": "MAROON",
    "WNE": "MAROON",
    "OXBLOOD": "MAROON",
    "CLARET": "MAROON",
    # ORANGE
    "PEACH": "ORANGE",
    "DEEP PEACH": "ORANGE",
    "LIGHT PEACH": "ORANGE",
    "LT PEACH": "ORANGE",
    "LT. PEACH": "ORANGE",
    "CORAL": "ORANGE",
    "CORAL ORANGE": "ORANGE",
    "TANGERINE": "ORANGE",
    "APRICOT": "ORANGE",
    "SALMON": "ORANGE",
    "NEON ORANGE": "ORANGE",
    "CARROT": "ORANGE",
    "GAJARI": "ORANGE",
    # RUST
    "BRICK": "RUST",
    "BRICK RED": "RUST",
    "COPPER": "RUST",
    "TERRACOTTA": "RUST",
    "BURNT ORANGE": "RUST",
    "RUST ORANGE": "RUST",
    "CLAY": "RUST",
    # PINK
    "BLUSH": "PINK",
    "BABY PINK": "PINK",
    "LIGHT PINK": "PINK",
    "HOT PINK": "PINK",
    "DARK PINK": "PINK",
    "MEDIUM PINK": "PINK",
    "RANI": "PINK",
    "RANI PINK": "PINK",
    "FUCHSIA": "PINK",
    "FUSCHIA": "PINK",
    "MAGENTA": "PINK",
    "LIPSTICK": "PINK",
    "LIPSTICK PINK": "PINK",
    "ROSE": "PINK",
    "ROSE PINK": "PINK",
    "DUSTY ROSE": "PINK",
    "DUSTY PINK": "PINK",
    "PASTEL PINK": "PINK",
    "FLAMINGO": "PINK",
    "ONION": "PINK",
    "ONION PINK": "PINK",
    "CARNATION": "PINK",
    "BUBBLEGUM": "PINK",
    "SALMON PINK": "PINK",
    # PURPLE
    "LILAC": "PURPLE",
    "LAVENDER": "PURPLE",
    "LAVENDAR": "PURPLE",
    "MAUVE": "PURPLE",
    "VIOLET": "PURPLE",
    "PLUM": "PURPLE",
    "GRAPE": "PURPLE",
    "AUBERGINE": "PURPLE",
    "EGGPLANT": "PURPLE",
    "ORCHID": "PURPLE",
    "AMETHYST": "PURPLE",
    # YELLOW
    "MUSTARD": "YELLOW",
    "LEMON": "YELLOW",
    "LEMON YELLOW": "YELLOW",
    "GOLD": "YELLOW",
    "GOLDEN": "YELLOW",
    "YELLOW OCHRE": "YELLOW",
    "OCHRE": "YELLOW",
    "CORN": "YELLOW",
    "CORN YELLOW": "YELLOW",
    "SUNFLOWER": "YELLOW",
    "MANGO": "YELLOW",
    "HONEY": "YELLOW",
    "AMBER": "YELLOW",
    "CANARY": "YELLOW",
    "TURMERIC": "YELLOW",
    # BROWN
    "COFFEE": "BROWN",
    "CHOCOLATE": "BROWN",
    "CHOCO": "BROWN",
    "TAN": "BROWN",
    "CAMEL": "BROWN",
    "MOCHA": "BROWN",
    "COCOA": "BROWN",
    "WALNUT": "BROWN",
    "TOBACCO": "BROWN",
    "LIGHT BROWN": "BROWN",
    "DARK BROWN": "BROWN",
    "TEA LEAF": "BROWN",
    "TEAK": "BROWN",
    "RUST BROWN": "BROWN",
    "MUD": "BROWN",
    "MUD BROWN": "BROWN",
    "ESPRESSO": "BROWN",
    "HAZEL": "BROWN",
    "TAUPE": "BROWN",
    # CREAM
    "BEIGE": "CREAM",
    "LIGHT BEIGE": "CREAM",
    "LT BEIGE": "CREAM",
    "LT. BEIGE": "CREAM",
    "OFF WHITE": "CREAM",
    "OFF-WHITE": "CREAM",
    "IVORY": "CREAM",
    "ECRU": "CREAM",
    "FAWN": "CREAM",
    "SAND": "CREAM",
    "OATS": "CREAM",
    "OAT": "CREAM",
    "OATMEAL": "CREAM",
    "NUDE": "CREAM",
    "SKIN": "CREAM",
    "NATURAL": "CREAM",
    "CHALK": "CREAM",
    "PEARL": "CREAM",
    "VANILLA": "CREAM",
    "LINEN": "CREAM",
    "BONE": "CREAM",
    "WHEAT": "CREAM",
    "CHAMPAGNE": "CREAM",
    "BUFF": "CREAM",
    "BISCUIT": "CREAM",
    # CHIKU (tan-khaki bucket KDPS keeps distinct)
    "CHIKOO": "CHIKU",
    "CHICOO": "CHIKU",
    "CHIKKU": "CHIKU",
    # WHITE
    "PURE WHITE": "WHITE",
    "BRIGHT WHITE": "WHITE",
    "MILK": "WHITE",
    "MILKY WHITE": "WHITE",
    "SNOW": "WHITE",
    "SNOW WHITE": "WHITE",
    "CLOUD": "WHITE",
    "OPTIC WHITE": "WHITE",
    "WHITE MEL": "WHITE",
    # GREY
    "GRAY": "GREY",
    "GREY MEL": "GREY",
    "GREY MELANGE": "GREY",
    "MELANGE GREY": "GREY",
    "M GREY": "GREY",
    "MID GREY": "GREY",
    "LIGHT GREY": "GREY",
    "DARK GREY": "GREY",
    "CHARCOAL": "GREY",
    "CHARCOAL GREY": "GREY",
    "SLV GY": "GREY",
    "SILVER": "GREY",
    "SILVER GREY": "GREY",
    "STONE": "GREY",
    "STONE GREY": "GREY",
    "ASH": "GREY",
    "ASH GREY": "GREY",
    "SMOKE": "GREY",
    "SMOKE GREY": "GREY",
    "STEEL": "GREY",
    "STEEL GREY": "GREY",
    "ANTHRACITE": "GREY",
    "GRINDLE": "GREY",
    "MILANGE": "GREY",
    "MELANGE": "GREY",
    "GRAPHITE": "GREY",
    "SLATE": "GREY",
    "GUNMETAL": "GREY",
    "CARBON": "GREY",
    # BLACK
    "JET BLACK": "BLACK",
    "PURE BLACK": "BLACK",
    "PITCH BLACK": "BLACK",
    "DENIM BLACK": "BLACK",
    "BLACK MEL": "BLACK",
    "COAL": "BLACK",
}

# Most size variants are handled by normalize_size(); these are the few word forms.
SIZE_ALIASES = {
    "FREE SIZE": "FREE SIZE",
    "STANDARD": "FREE SIZE",
}

# Brand-level default GENDER, used only as the LAST fallback when a row has no
# gender column, no gender word in the description, and no rule default (see
# goods_mapper._map_gender). Seed only brands that are unambiguously single-gender in
# KDPS's assortment; mixed brands (Allen Solly, Van Heusen, USPA, Jockey…) stay
# unseeded so a wrong default is never silently applied.
BRAND_GENDER = {
    "PETER ENGLAND": "MALE",
    "LOUIS PHILIPPE": "MALE",
    "BLACKBERRYS": "MALE",
    "MUFTI": "MALE",
    "GOCOLORS": "FEMALE",
}

# Brand fit-column value → KDPS master FIT.
FIT_ALIASES = {
    "SLIM FIT": "SLIM",
    "REGULAR FIT": "REGULAR",
    "COMFORT FIT": "RELAXED",
    "COMFORT": "RELAXED",
    "RELAXED FIT": "RELAXED",
    "SKINNY FIT": "SKINNY",
    "STRAIGHT FIT": "STRAIGHT",
    "BOOTCUT FIT": "BOOTCUT",
    "TAILORED FIT": "TAILORED",
    "CLASSIC": "CLASSIC FIT",
    "SUPER SLIM FIT": "SUPER SLIM",
    "SLIM TAPERED": "SLIM TEPAR",
    "SLIM STRAIGHT FIT": "SLIM STRAIGHT",
    "REGULAR STRAIGHT FIT": "REGULAR STRAIGHT",
    "CREW": "CREW NECK",
    "ROUND": "ROUND NECK",
    "POLO": "POLO NECK",
    # Peter England / ABFRL coded FIT TYPE ("PJ RG OCTANEMIDSTR"): token 2 is the
    # fit family — RG = Regular, SL = Slim (see goods_mapper.fit_code_candidates).
    # NB: no "SNUG" alias — the Master FIT list has no SNUG and rows carrying it
    # ("PC SL Snug") already resolve via the SL token; mapping it would be a guess.
    "RG": "REGULAR",
    "SL": "SLIM",
    # NB: no "V NECK" alias — the Master FIT list has no V-neck, and mapping it to
    # ROUND NECK would be deterministically wrong. Leave it for a person.
}

GENDER_MAP = {
    "MALE": "MALE",
    "MEN": "MALE",
    "MENS": "MALE",
    "MAN": "MALE",
    "M": "MALE",
    "GENTS": "MALE",
    "GENT": "MALE",
    "BOYS": "KIDS MALE",
    "BOY": "KIDS MALE",
    "KIDS MALE": "KIDS MALE",
    "KIDS BOYS": "KIDS MALE",
    "JUNIOR BOYS": "KIDS MALE",
    "FEMALE": "FEMALE",
    "WOMEN": "FEMALE",
    "WOMENS": "FEMALE",
    "WOMAN": "FEMALE",
    "LADIES": "FEMALE",
    "LADY": "FEMALE",
    "F": "FEMALE",
    "GIRLS": "KIDS FEMALE",
    "GIRL": "KIDS FEMALE",
    "KIDS FEMALE": "KIDS FEMALE",
    "KIDS GIRLS": "KIDS FEMALE",
    "JUNIOR GIRLS": "KIDS FEMALE",
    "UNISEX": "UNISEX",
    "KIDS": "UNISEX",
    "KID": "UNISEX",
    "INFANT": "UNISEX",
}

# (pattern, gender, item, fit). SUB CATEGORY / TYPE auto-fill from the ITEM→helper.
# gender here is only a default for invariant items — a gender column or a gender word
# in the description overrides it (see goods_mapper._map_gender).
RULE_SEED = [
    # --- top wear
    ("T-SHIRT", "", "T-SHIRT", ""),
    ("T SHIRT", "", "T-SHIRT", ""),
    ("TSHIRT", "", "T-SHIRT", ""),
    ("TEE", "", "T-SHIRT", ""),
    ("POLO", "", "T-SHIRT", "POLO NECK"),
    ("FORMAL SHIRT", "", "SHIRT", ""),
    ("CASUAL SHIRT", "", "SHIRT", ""),
    ("SHIRT", "", "SHIRT", ""),
    ("KURTI", "FEMALE", "KURTI", ""),
    ("TUNIC", "FEMALE", "KURTI", ""),
    ("KURTA SET", "", "KURTI SET", ""),
    ("KURTI SET", "FEMALE", "KURTI SET", ""),
    ("KURTA", "", "KURTA", ""),
    ("KURTHA", "", "KURTA", ""),
    ("CROP TOP", "FEMALE", "CROP TOP", ""),
    ("CAMISOLE", "FEMALE", "CAMISOLE", ""),
    ("CAMI", "FEMALE", "CAMISOLE", ""),
    ("BLOUSE", "FEMALE", "BLOUSE", ""),
    ("TOP", "", "TOP", ""),
    ("SWEATSHIRT", "", "SWEATSHIRT", ""),
    ("SWEAT SHIRT", "", "SWEATSHIRT", ""),
    ("HOODIE", "", "SWEATSHIRT", ""),
    ("SWEATER", "", "SWEATER", ""),
    ("PULLOVER", "", "SWEATER", ""),
    ("CARDIGAN", "", "CARDIGAN", ""),
    ("BLAZER", "", "BLAZER", ""),
    ("WAISTCOAT", "", "BLAZER", ""),
    ("NEHRU JACKET", "MALE", "NEHRU JACKET", ""),
    ("JACKET", "", "JACKET", ""),
    ("SHACKET", "", "SHACKET", ""),
    ("WINDCHEATER", "", "WINDCHEATER", ""),
    ("WIND CHEATER", "", "WINDCHEATER", ""),
    ("RAINCOAT", "", "RAINCOAT", ""),
    ("THERMAL", "", "THERMAL", ""),
    # --- one-piece
    ("GOWN", "FEMALE", "GOWN", ""),
    ("DRESS", "FEMALE", "DRESSES", ""),
    ("FROCK", "KIDS FEMALE", "FROCK", ""),
    ("MIDI", "FEMALE", "MIDY", ""),
    ("MIDY", "FEMALE", "MIDY", ""),
    ("JUMPSUIT", "", "JUMP SUIT", ""),
    ("JUMP SUIT", "", "JUMP SUIT", ""),
    ("DUNGAREE", "", "DUNGAREE", ""),
    ("NIGHTY", "FEMALE", "NIGHTY", ""),
    ("SAREE", "FEMALE", "SAREE", ""),
    ("LEHENGA", "FEMALE", "LEHENGA", ""),
    ("SALWAR", "FEMALE", "SALWAR SUIT", ""),
    ("SHERWANI", "MALE", "SHERWANI", ""),
    # --- bottom wear
    ("JEANS", "", "JEANS", ""),
    ("JEAN", "", "JEANS", ""),
    ("JEGGING", "FEMALE", "JEGGING", ""),
    ("JEGGINGS", "FEMALE", "JEGGING", ""),
    ("TROUSER", "", "TROUSER", ""),
    ("TROUSERS", "", "TROUSER", ""),
    ("CHINO", "", "TROUSER", ""),
    ("FORMAL PANT", "", "TROUSER", ""),
    ("PANT", "", "TROUSER", ""),
    ("TRACK PANT", "", "JOGGER", "TRACK PANT"),
    ("TRACKPANT", "", "JOGGER", "TRACK PANT"),
    ("JOGGER", "", "JOGGER", ""),
    ("JOGGERS", "", "JOGGER", ""),
    ("LOWER", "", "LOWER", ""),
    ("LOWERS", "", "LOWER", ""),
    ("SHORTS", "", "SHORTS", ""),
    ("BERMUDA", "", "SHORTS", "BERMUDA"),
    ("CAPRI", "", "CAPRI", ""),
    ("CAPRIS", "", "CAPRI", ""),
    ("LEGGING", "FEMALE", "LEGGING", ""),
    ("LEGGINGS", "FEMALE", "LEGGING", ""),
    ("PALAZZO", "FEMALE", "PALAZZO", ""),
    ("PALAZZO SET", "FEMALE", "PALAZZO SET", ""),
    ("PLAZO", "FEMALE", "PALAZZO", ""),
    ("PLAZZO", "FEMALE", "PALAZZO", ""),
    ("PLAZOO", "FEMALE", "PALAZZO", ""),
    ("SKIRT", "FEMALE", "SKIRT", ""),
    ("CARGO", "", "CARGO", ""),
    ("DHOTI", "MALE", "DHOTI", ""),
    ("PATIALA", "FEMALE", "PATIALA", ""),
    ("PETTICOAT", "FEMALE", "PETTICOAT", ""),
    # --- full set
    ("CO-ORD", "", "CO-ORD SET", ""),
    ("CO ORD", "", "CO-ORD SET", ""),
    ("COORD", "", "CO-ORD SET", ""),
    ("NIGHT SUIT", "", "NIGHT SUIT", ""),
    ("NIGHTSUIT", "", "NIGHT SUIT", ""),
    ("TRACKSUIT", "", "TRACKSUIT", ""),
    ("TRACK SUIT", "", "TRACKSUIT", ""),
    ("BABA SUIT", "KIDS MALE", "BABA SUIT", ""),
    ("PANT SET", "", "CO-ORD SET", "PANT SET"),
    ("SHORTS SET", "", "CO-ORD SET", "SHORTS SET"),
    ("DRESS MATERIAL", "", "DRESS MATERIAL", ""),
    ("SUIT", "", "SUIT", ""),
    # --- innerwear
    ("BRIEF", "", "BRIEF", ""),
    ("TRUNK", "MALE", "BRIEF", "TRUNK"),
    ("BOXER", "", "BRIEF", "BOXER"),
    ("VEST", "", "VEST", ""),
    ("BRA", "FEMALE", "BRA", ""),
    ("PANTIE", "FEMALE", "PANTIE", ""),
    ("PANTY", "FEMALE", "PANTIE", ""),
    ("BLOOMER", "", "BLOOMER", ""),
    ("SOCKS", "", "SOCKS", ""),
    ("SOCK", "", "SOCKS", ""),
    # --- accessories
    ("BELT", "", "BELT", ""),
    ("WALLET", "", "WALLET", ""),
    ("CAP", "", "CAP", ""),
    ("HANDBAG", "", "HANDBAG", ""),
    ("HAND BAG", "", "HANDBAG", ""),
    ("BACKPACK", "", "BACKPACK", ""),
    ("DUFFLE", "", "DUFFLE BAG", ""),
    ("DUPATTA", "FEMALE", "DUPATTA", ""),
    ("STOLE", "", "STOLE", ""),
    ("SCARF", "", "SCARVES", ""),
    ("SCARVES", "", "SCARVES", ""),
    ("MUFFLER", "", "MUFFLER", ""),
    ("TIE", "", "TIES & BOWTIE", ""),
    ("HANDKERCHIEF", "", "HANDKERCHIEF", ""),
    ("POCKET SQUARE", "", "POCKET SQUARE", ""),
    ("SHOES", "", "SHOES", ""),
    ("SLIPPER", "", "SLIPPERS", ""),
    ("JUTI", "", "JUTI", ""),
    ("TOWEL", "", "TOWEL", ""),
    ("BEDSHEET", "", "BEDSHEET", ""),
    ("PERFUME", "", "PERFUME", ""),
    ("DEODRANT", "", "DEODRENT", ""),
    ("DEODORANT", "", "DEODRENT", ""),
    # --- ethnic top variants
    ("INDO WESTERN", "", "KURTI SET", "INDO WESTERN"),
    # --- Madura SAP glued material codes the substring matcher can't split
    ("TROUSR", "", "TROUSER", ""),
    ("FGKTROUSR", "", "TROUSER", ""),
    ("FGSUIT", "", "SUIT", ""),
    ("FGTRAKPNT", "", "JOGGER", "TRACK PANT"),
    ("FGTOPS", "", "TOP", ""),
    ("FGCKNTOP", "", "TOP", ""),
    ("FGTOP", "", "TOP", ""),
]


# Per-rule SUB CATEGORY override, for items that span categories so the single
# ITEM→helper row would otherwise collapse the distinction (a SHIRT defaults to
# FORMAL WEAR in the Master helper, but a "casual shirt" must stay CASUAL WEAR).
SUB_OVERRIDE = {
    "FORMAL SHIRT": "FORMAL WEAR",
    "CASUAL SHIRT": "CASUAL WEAR",
    "SHIRT": "CASUAL WEAR",
    "FORMAL PANT": "FORMAL WEAR",
}


# ----------------------------------------------------------------------------- the sheet


def cell_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip()


@dataclass
class MasterSheet:
    """The KDPS master sheet's vocabularies, ITEM helper and brand names."""

    values: dict[str, set[str]] = field(default_factory=dict)
    item_helper: dict[str, tuple[str, str]] = field(default_factory=dict)
    brands: set[str] = field(default_factory=set)
    read: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def read_master_sheets(paths: Iterable[Path]) -> MasterSheet:
    """Union every sheet's values; for the ITEM helper the first sheet wins."""
    sheet = MasterSheet(values={dim: set() for dim in DIM_COLS.values()})
    for path in paths:
        if not path.exists():
            sheet.missing.append(str(path))
            continue
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [list(r) for r in workbook["Master Sheet"].iter_rows(values_only=True)]
        workbook.close()
        sheet.read.append(str(path))
        for row in rows[1:]:
            cells = [cell_text(c) for c in row]
            for index, dimension in DIM_COLS.items():
                if index < len(cells) and cells[index]:
                    sheet.values[dimension].add(cells[index])
            if len(cells) > 1 and cells[1]:
                sheet.brands.add(cells[1])
            item = cells[6] if len(cells) > 6 else ""
            if item and item not in sheet.item_helper:
                helper = tuple(cells[i] if i < len(cells) else "" for i in HELPER_COLS)
                sheet.item_helper[item] = (helper[0], helper[1])
    return sheet


def rolling_season_values(today: date) -> set[str]:
    """Every month's season from Jan-25 to 18 months past ``today``, so a season read
    from any plausible invoice date is an approved value (the sheet's own list stops)."""
    out: set[str] = set()
    y, m = SEASON_WINDOW_START.year, SEASON_WINDOW_START.month
    end_m = today.month + SEASON_WINDOW_MONTHS_AHEAD
    end_y, end_m = today.year + (end_m - 1) // 12, (end_m - 1) % 12 + 1
    while (y, m) <= (end_y, end_m):
        out.add(season_label(date(y, m, 1)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def pick_valid(cell: str, valid: set[str]) -> str:
    """From a helper cell that may hold several '/'-separated options
    ('FORMAL / CASUAL/ PARTY WEAR'), the first that is a real value (exact, else a
    value the token starts: 'CASUAL' -> 'CASUAL WEAR')."""
    for token in re.split(r"[/,]", cell or ""):
        token = token.strip()
        if not token:
            continue
        if token in valid:
            return token
        for value in sorted(valid):  # sorted: deterministic when a token prefixes several
            if value.startswith(token):
                return value
    return ""


_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}
_SEASON_RE = re.compile(r"\((\w{3})-(\d{2})\)\s*$")


def sort_key(dimension: str, text: str) -> tuple[Any, ...]:
    """A natural order for new values: seasons by month, numbers by size."""
    if dimension == "season":
        found = _SEASON_RE.search(text.upper())
        if found and found.group(1) in _MONTHS:
            return (0, int(found.group(2)), _MONTHS[found.group(1)], text)
    parts = re.split(r"(\d+(?:\.\d+)?)", text.upper())
    return (
        1,
        *[
            (0, float(p), "") if re.fullmatch(r"\d+(?:\.\d+)?", p) else (1, 0.0, p)
            for p in parts
            if p
        ],
    )


# ----------------------------------------------------------------------------- vocabulary


def _approver(tenant: Any, email: str | None) -> Any:
    from accounts.goods_demo import SYNTHETIC_DOMAIN
    from accounts.models import User

    wanted = email or f"syn.owner@{SYNTHETIC_DOMAIN}"
    user = User.objects.filter(email__iexact=wanted).first()
    if user is None or user.human_id is None:
        raise Refusal("STATE_CONFLICT", f"{wanted} must be seeded before the rulebook is loaded.")
    return user.human_id


def publish_vocabulary(
    tenant: Any, approver_id: Any, dimension: str, texts: Iterable[str]
) -> tuple[int, str | None]:
    """Add the values ``texts`` names that the dimension does not already carry.

    Returns (values added, new version ID). Keeps every published value (the
    vocabulary rule: retire, never remove); skips a text equal to an existing
    value's key or label. Nothing to add writes nothing.
    """
    current = vocabulary(tenant.pk, database_now(), dimension).get(dimension, [])
    known = {match_key(v.value_key) for v in current} | {match_key(v.label) for v in current}
    fresh: dict[str, str] = {}
    for text in texts:
        key = match_key(text)
        if key and key not in known and key not in fresh and len(text) <= 100:
            fresh[key] = text
    if not fresh:
        return 0, None
    values = [
        {
            "value_key": v.value_key,
            "label": v.label,
            "sort_order": v.sort_order,
            "retired": v.retired,
        }
        for v in current
    ]
    start = max((v.sort_order for v in current), default=-1) + 1
    for offset, text in enumerate(sorted(fresh.values(), key=lambda t: sort_key(dimension, t))):
        values.append(
            {"value_key": text, "label": text, "sort_order": start + offset, "retired": False}
        )
    holder: dict[str, str] = {}

    def handler(run: CommandRun) -> CommandResult:
        holder["id"] = _approve_vocabulary(run, approver_id, dimension, values)
        return CommandResult(resource_type="configuration", resource_id=holder["id"])

    execute_command(
        _service(tenant),
        CommandSpec(
            action="seed.kdps_vocabulary",
            command_id=uuid.uuid5(
                tenant.deployment_key, f"kdps-vocabulary:{dimension}:{content_hash(values)}"
            ),
            business_input={"dimension": dimension, "values_hash": content_hash(values)},
        ),
        handler,
    )
    return len(fresh), holder.get("id")


def _approve_vocabulary(
    run: CommandRun, approver_id: Any, dimension: str, values: list[dict[str, Any]]
) -> str:
    """One approved vocabulary version, validated as the product validates a draft."""
    from masters.goods_config import activate, normalise_scope
    from masters.goods_models import ConfigDraft, ConfigVersion
    from masters.goods_services import config_scope_key, validate_config_payload

    owner = (
        ConfigVersion.objects.filter(
            tenant_id=run.tenant_id, kind="vocabulary", payload__dimension=dimension
        )
        .order_by("-effective_from", "-version")
        .first()
    )
    scope = normalise_scope(owner.scope if owner else {"scope_kind": "tenant"})
    scope_key = owner.scope_key if owner else config_scope_key(scope)
    payload = {"dimension": dimension, "values": values, "effective_from": run.now.isoformat()}
    validate_config_payload(
        "vocabulary",
        payload,
        tenant_id=run.tenant_id,
        scope_key=scope_key,
        as_of=run.now,
        scope=scope,
    )
    draft = ConfigDraft.objects.create(
        tenant_id=run.tenant_id,
        kind="vocabulary",
        scope=scope,
        scope_key=scope_key,
        payload=payload,
        effective_from=run.now,
        maker_id=approver_id,
        state=ConfigDraft.State.APPROVED,
    )
    last = (
        ConfigVersion.objects.filter(
            tenant_id=run.tenant_id, kind="vocabulary", scope_key=scope_key
        )
        .order_by("-version")
        .first()
    )
    version = run.record(
        ConfigVersion(
            draft_id=draft.pk,
            kind="vocabulary",
            version=(last.version + 1) if last else 1,
            scope=scope,
            scope_key=scope_key,
            payload=payload,
            effective_from=run.now,
            approved_by_id=approver_id,
            source_revision=1,
            source_hash=content_hash(payload),
        )
    )
    activate(run, version, effective_to=None)
    return str(version.pk)


def _service(tenant: Any) -> Any:
    from accounts.goods_setup import service_principal

    return service_principal(tenant.pk, SEED_SERVICE)


# ----------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class RuleSpec:
    """A rule to carry: (kind, issuer, source) -> the target's own text."""

    kind: str
    issuer_key: str
    source_key: str
    target: str
    origin: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.kind, self.issuer_key, match_key(self.source_key)


@dataclass(frozen=True)
class NotCarried:
    spec: RuleSpec
    reason: str

    def line(self) -> str:
        s = self.spec
        rule = f"{s.kind} [{s.issuer_key}] {s.source_key!r} -> {s.target!r}"
        return f"{rule} ({s.origin}): {self.reason}"


BrandIssuer = Callable[[str], str | None]


def _upper(text: str) -> str:
    return " ".join(text.split()).upper()


def starter_specs(
    sheet: MasterSheet, brand_of: BrandIssuer
) -> tuple[list[RuleSpec], list[NotCarried]]:
    """The legacy seed's own starter rules and the master sheet's ITEM helper."""
    specs: list[RuleSpec] = []
    skipped: list[NotCarried] = []

    def add(kind: str, issuer: str, source: str, target: str) -> None:
        if target:
            specs.append(RuleSpec(kind, issuer, _upper(source), target, "starter"))

    for table, kind in (
        (BRAND_ALIASES, "brand"),
        (COLOR_ALIASES, "colour"),
        (SIZE_ALIASES, "size"),
        (FIT_ALIASES, "fit"),
        (GENDER_MAP, "gender"),
    ):
        for source, target in table.items():
            add(kind, ANY_ISSUER, source, target)
    for brand, gender in BRAND_GENDER.items():
        issuer = brand_of(brand)
        spec = RuleSpec("gender", issuer or f"brand:{brand}", DEFAULT_SOURCE, gender, "starter")
        if issuer is None:
            skipped.append(NotCarried(spec, "no brand master of that name"))
        else:
            specs.append(spec)
    for pattern, gender, item, fit in RULE_SEED:
        specs.extend(
            _keyword_specs(pattern, item, gender, SUB_OVERRIDE.get(pattern, ""), "", fit, "starter")
        )
    for item, (sub, kind) in sorted(sheet.item_helper.items()):
        add("sub_category", ITEM_ISSUER, item, pick_valid(sub, sheet.values["sub_category"]))
        add("type", ITEM_ISSUER, item, pick_valid(kind, sheet.values["type"]))
    return specs, skipped


def _keyword_specs(
    pattern: str, item: str, gender: str, sub: str, kind: str, fit: str, origin: str
) -> list[RuleSpec]:
    """A description keyword rule: the ITEM it names, and the defaults it carries."""
    if not item:
        return []
    source = _upper(pattern)
    out = [RuleSpec("item", ANY_ISSUER, source, item, origin)]
    for dimension, target in (
        ("gender", gender),
        ("sub_category", sub),
        ("type", kind),
        ("fit", fit),
    ):
        if target:
            out.append(RuleSpec(dimension, KEYWORD_ISSUER, source, target, origin))
    return out


@dataclass
class LoadReport:
    vocabulary_added: dict[str, int] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    created: int = 0
    already: int = 0
    redundant: int = 0
    not_carried: list[NotCarried] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    missing_sheets: list[str] = field(default_factory=list)


def _brand_index() -> dict[str, int]:
    from masters.models import Brand

    index: dict[str, int] = {}
    for pk, code, name in (
        Brand.objects.filter(is_active=True).order_by("pk").values_list("pk", "code", "name")
    ):
        for text in (code, name):
            index.setdefault(compact(text), pk)
    return index


def carry_rules(tenant: Any, specs: list[RuleSpec], report: LoadReport) -> None:
    """Write each carried rule as an effective crosswalk, once; report what cannot be."""
    now = database_now()
    book = Rulebook.load(tenant.pk, now)
    versions = {
        dim: rows[0].config_version_id for dim, rows in vocabulary(tenant.pk, now).items() if rows
    }
    brands = _brand_index()
    existing = {
        (kind, issuer, match_key(source))
        for kind, issuer, source in SourceCrosswalk.objects.filter(tenant_id=tenant.pk)
        .exclude(governance_state=GovernanceState.RETIRED)
        .values_list("kind", "issuer_key", "source_key")
    }
    planned: dict[tuple[str, str, str], tuple[RuleSpec, str]] = {}
    counted: set[tuple[str, str, str]] = set()
    for spec in specs:
        if _identity(spec):
            # A value mapped to itself adds nothing: the value, when approved,
            # already matches exactly (legacy seeded one for every value).
            report.redundant += 1
            continue
        target, reason = _target(book, brands, spec)
        if reason:
            report.not_carried.append(NotCarried(spec, reason))
            continue
        if (
            spec.kind in CELL_DIMENSIONS
            and spec.issuer_key != KEYWORD_ISSUER
            and (_already_exact(book, spec, target))
        ):
            report.redundant += 1
            continue
        if spec.key in existing:
            counted.add(spec.key)
            continue
        if spec.key in planned:
            if planned[spec.key][1] != target:
                kept = planned[spec.key][0]
                report.not_carried.append(
                    NotCarried(spec, f"conflicts with {kept.target!r} ({kept.origin}), kept")
                )
            continue
        planned[spec.key] = (spec, target)
    report.already += len(counted)
    if planned:
        _write_rules(tenant, planned, versions)
        report.created += len(planned)


def _identity(spec: RuleSpec) -> bool:
    return (
        spec.kind in CELL_DIMENSIONS
        and spec.issuer_key != KEYWORD_ISSUER
        and spec.source_key != DEFAULT_SOURCE
        and compact(spec.source_key) == compact(spec.target)
    )


def _target(book: Rulebook, brands: dict[str, int], spec: RuleSpec) -> tuple[str, str]:
    if spec.kind == "brand":
        pk = brands.get(compact(spec.target))
        return (str(pk), "") if pk is not None else ("", "no brand master of that name")
    if not book.governed(spec.kind):
        return "", f"no approved {spec.kind} vocabulary"
    value = book.exact(spec.kind, spec.target)
    return (value.id, "") if value is not None else ("", "not an approved value")


def _already_exact(book: Rulebook, spec: RuleSpec, target: str) -> bool:
    if spec.source_key == DEFAULT_SOURCE:
        return False
    value = book.exact(spec.kind, spec.source_key)
    return value is not None and value.id == target


def _write_rules(
    tenant: Any, planned: dict[tuple[str, str, str], tuple[RuleSpec, str]], versions: dict[str, Any]
) -> None:
    ordered = sorted(planned.items())
    fingerprint = content_hash([[*key, target] for key, (_spec, target) in ordered])

    def handler(run: CommandRun) -> CommandResult:
        for (kind, issuer, _key), (spec, target) in ordered:
            row = SourceCrosswalk(
                tenant_id=run.tenant_id,
                kind=kind,
                issuer_key=issuer,
                source_key=spec.source_key[:240],
                target_key=target,
                config_version_id=versions.get(kind),
                governance_state=GovernanceState.EFFECTIVE,
            )
            save_master(row, conflict="That rule already exists.")
            record_master_version(run, "crosswalk", row, reason_code=SEED_REASON)
        return CommandResult(resource_type="crosswalk", resource_id=str(len(ordered)))

    execute_command(
        _service(tenant),
        CommandSpec(
            action="seed.kdps_rulebook",
            command_id=uuid.uuid5(tenant.deployment_key, f"kdps-rulebook:{fingerprint}"),
            business_input={"rules": len(ordered), "fingerprint": fingerprint},
        ),
        handler,
    )


# ----------------------------------------------------------------------------- the load


def default_sheet_paths() -> list[Path]:
    """The canonical PT file format first (its master sheet carries the dropdowns), then
    the older KDPS PT file sheet, unioned so nothing the legacy seed loaded is lost."""
    from django.conf import settings

    data = Path(settings.BASE_DIR).parent.parent / "docs" / "data-from-kdps"
    return [data / "05-reference-data" / "pt-file-format.xlsx", data / "KDPS PT FILE SHEET.xlsx"]


def load_rulebook(
    tenant: Any,
    *,
    approver_email: str | None = None,
    paths: list[Path] | None = None,
    today: date | None = None,
) -> LoadReport:
    """Load the real KDPS vocabulary and carry the starter rules into ``tenant`` (idempotent)."""
    if not tenant.synthetic:
        raise Refusal(
            "ACTION_DENIED",
            "A real tenant's vocabulary and rules are approved through Configuration and "
            "PT Work, not loaded by a seed.",
        )
    approver = _approver(tenant, approver_email)
    sheet = read_master_sheets(paths or default_sheet_paths())
    report = LoadReport(sheets=sheet.read, missing_sheets=sheet.missing)
    if not sheet.read:
        return report
    sheet.values["season"] |= rolling_season_values(today or database_now().date())
    for dimension in sorted(sheet.values):
        added, version = publish_vocabulary(tenant, approver, dimension, sheet.values[dimension])
        report.vocabulary_added[dimension] = added
        if version:
            report.versions[dimension] = version
    index = _brand_index()

    def brand_of(name: str) -> str | None:
        pk = index.get(compact(name))
        return brand_issuer(pk) if pk is not None else None

    specs, skipped = starter_specs(sheet, brand_of)
    report.not_carried.extend(skipped)
    carry_rules(tenant, specs, report)
    unique: dict[tuple[tuple[str, str, str], str, str], NotCarried] = {}
    for entry in report.not_carried:
        unique.setdefault((entry.spec.key, entry.spec.target, entry.reason), entry)
    report.not_carried = list(unique.values())
    return report
