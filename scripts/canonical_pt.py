"""Rewrite a store's normalised PT file in the new canonical PT format (store test run).

The KDPS sheet packs several meanings into one column: GENDER holds age
(KIDS MALE), SUB CATEGORY holds occasion, product class and legacy price-tier
codes (CASUAL WEAR, INNERWEAR, SETM), FIT holds true fits, brand cut names,
sleeve and neck, set size, fabric and margin-band codes (SLIM, KANO, HALF
SLEEVE, 3 PCE SET, SILK SAREE, LM), SIZE mixes measurement systems. The
canonical format gives every column one meaning, from closed lists, and keeps
each row's original text on a "Source values" sheet so nothing is lost:

* DIVISION and CATEGORY follow from ITEM (``ITEMS``); ITEM is one closed list
  with aliases (TROUSERS → TROUSER, DUPTTA → DUPATTA);
* GENDER (Male, Female, Unisex) and AGE GROUP (Adult, Kids, Infant) are apart;
* OCCASION, FIT, SLEEVE, NECK, PIECES, FABRIC, STYLE NAME, PRICE TIER and
  MARGIN BAND are read out of the legacy SUB CATEGORY and FIT text;
  CONSTRUCTION (knit/woven) comes only from an invoice HSN of 6 or more digits;
* SIZE is cleaned and its SIZE SYSTEM named; SEASON is ``SS-YY`` / ``AW-YY``
  with the ARRIVAL MONTH the legacy text carried;
* BASIC is the supplier's basic cost; P RATE = BASIC × (1 + LANDED %), the
  cost KDPS values stock at. LANDED % is the bill's own (P Rate ÷ Basic) where
  a bill shows both, else 20% (owner decision, 31 Oct 2026): a source with only
  P RATE (an SOH Rate) gets BASIC = P RATE ÷ 1.2. INPUT TAX, OUTPUT TAX and MARGIN % are
  derived (the KDPS sheet's ladder); the app takes tax from Tax settings.

Blank always means unknown: nothing is guessed from another row.

    backend/.venv/bin/python scripts/canonical_pt.py \\
        --pt docs/wardrobe-ratu-road/normalized/WARDROBE_PT_combined.xlsx \\
        --out docs/wardrobe-ratu-road/normalized --name WARDROBE_PT_canonical.xlsx

The outputs hold cost data: keep them out of git.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ptmapper.legacy_season import MONTHS, normalize_season
from scripts.normalize_pt_files import EXAMPLES, _table, number, text

LANDED_PCT = Decimal("20")
CENT = Decimal("0.01")

HEADER = ["BARCODE", "BRAND", "DESIGN", "STYLE NAME", "DIVISION", "CATEGORY", "ITEM", "GENDER", "AGE GROUP",
          "OCCASION", "FIT", "SLEEVE", "NECK", "PIECES", "FABRIC", "CONSTRUCTION", "COLOUR", "PRICE TIER",
          "MARGIN BAND", "SIZE", "SIZE SYSTEM", "SEASON", "ARRIVAL MONTH", "HSN", "QTY", "MRP", "BASIC", "LANDED %", "P RATE",
          "INPUT TAX", "OUTPUT TAX", "MARGIN %"]
DERIVED = {"DIVISION", "CATEGORY", "SIZE SYSTEM", "P RATE", "INPUT TAX", "OUTPUT TAX", "MARGIN %"}
SOURCE_HEADER = ["BARCODE", "Item", "Sub Category", "Gender", "Fit", "Size", "Legacy Season", "Basic", "P Rate",
                 "Source"]

_ITEMS: dict[tuple[str, str], list[str]] = {
    ("Apparel", "Topwear"): ["SHIRT", "T-SHIRT", "TOP", "CROP TOP", "KURTI", "KURTA", "BLOUSE", "SWEATSHIRT",
                             "SWEATER", "CARDIGAN"],
    ("Apparel", "Outerwear"): ["JACKET", "COAT", "BLAZER", "WAISTCOAT", "BUNDY"],
    ("Apparel", "Bottomwear"): ["JEANS", "TROUSER", "PANT", "CARGO", "LOWER", "TRACK PANT", "JOGGER", "SHORTS",
                                "HOT PANT", "LEGGING", "JEGGING", "SKIRT"],
    ("Apparel", "Set"): ["KURTI SET", "KURTA SET", "SALWAR SUIT", "PALAZZO SET", "CO-ORD SET", "SET", "BUNDY SET",
                         "BABA SUIT", "SUIT", "SKIRT SET", "CROP SET", "PANT SET", "LEHENGA", "SARARA", "GARARA",
                         "PATIALA", "INFANT SET"],
    ("Apparel", "One-piece"): ["DRESS", "FROCK", "GOWN", "MIDY", "JUMPSUIT", "DUNGAREE", "BABY ROMPER"],
    ("Apparel", "Saree"): ["SAREE"],
    ("Apparel", "Innerwear"): ["BRA", "BRIEF", "TRUNK", "BOXER", "PANTIE", "HIPSTER", "VEST", "THERMAL", "BLOOMER",
                               "PETTICOAT"],
    ("Apparel", "Nightwear"): ["NIGHTY", "NIGHT SUIT"],
    ("Accessories", "Headwear"): ["CAP"],
    ("Accessories", "Scarves & stoles"): ["STOLE", "DUPATTA", "SHAWL", "MUFFLER", "SCARF"],
    ("Accessories", "Socks"): ["SOCKS"],
    ("Accessories", "Belts & wallets"): ["BELT", "WALLET"],
    ("Accessories", "Small accessories"): ["HANDKERCHIEF", "TIES & BOWTIE", "ARM SLEEVE"],
    ("Bags & Luggage", "Bags"): ["BACKPACK", "BAG", "DUFFLE BAG", "CARRY BAG"],
    ("Bags & Luggage", "Luggage"): ["TROLLEY"],
    ("Footwear", "Footwear"): ["FLIP FLOP"],
    ("Home", "Bed & bath"): ["BEDSHEET", "TOWEL", "BATH MAT", "PILLOW", "DOHAR", "SLEEPING BAG"],
    ("Beauty", "Fragrance"): ["PERFUME", "DEO"],
    ("Electronics", "Appliances"): ["SPEAKER", "MIXER GRINDER"],
}
#: ITEM → (DIVISION, CATEGORY).
ITEMS = {item: place for place, items in _ITEMS.items() for item in items}
#: Legacy item text → (ITEM, what the old name also said).
ALIASES: dict[str, tuple[str, dict[str, Any]]] = {
    "TROUSERS": ("TROUSER", {}), "T SHIRT": ("T-SHIRT", {}), "SWEAT SHIRT": ("SWEATSHIRT", {}),
    "SHORT": ("SHORTS", {}), "LEGGINGS": ("LEGGING", {}), "JEGGINGS": ("JEGGING", {}), "MIDDI": ("MIDY", {}),
    "DRESSES": ("DRESS", {}), "JUMP SUIT": ("JUMPSUIT", {}), "PANTY": ("PANTIE", {}), "PLAZO SET": ("PALAZZO SET", {}),
    "WAIST COAT": ("WAISTCOAT", {}), "SAREES": ("SAREE", {}), "TANAT SAREE": ("SAREE", {"FABRIC": "Cotton"}),
    "DUPTTA": ("DUPATTA", {}), "L DUPTTA": ("DUPATTA", {}), "HANDKERCHIEFS": ("HANDKERCHIEF", {}),
    "TROLLY": ("TROLLEY", {}), "DUFFLE TROLLY": ("TROLLEY", {}), "DUFFEL BAG": ("DUFFLE BAG", {}),
    "BACKPACKS": ("BACKPACK", {}), "ARM SHIELD": ("ARM SLEEVE", {}), "ARM SLIDE": ("ARM SLEEVE", {}),
    "NIGHT WEAR": ("NIGHT SUIT", {}), "NIEGHTWEAR": ("NIGHT SUIT", {}),
    "KURTA PAJAMA": ("KURTA SET", {"PIECES": 2}),
    "BOY SET": ("SET", {"GENDER": "Male", "AGE GROUP": "Kids"}),
    "GIRL SET": ("SET", {"GENDER": "Female", "AGE GROUP": "Kids"}),
    "KIDS SET": ("SET", {"AGE GROUP": "Kids"}), "KIDS THERMAL SET": ("THERMAL", {"AGE GROUP": "Kids"}),
    "INFANT SET": ("INFANT SET", {"AGE GROUP": "Infant"}), "BABY ROMPER": ("BABY ROMPER", {"AGE GROUP": "Infant"}),
    "WINTER TOP": ("TOP", {"OCCASION": "Winter"}), "WINTER CAP": ("CAP", {"OCCASION": "Winter"}),
    "WINTER LOWER": ("LOWER", {"OCCASION": "Winter"}), "WINTER SET": ("SET", {"OCCASION": "Winter"}),
    "WINTER KURTI SET": ("KURTI SET", {"OCCASION": "Winter"}),
    "JEANS": ("JEANS", {"FABRIC": "Denim"}),
}
#: Rows whose item text names nothing: a CARLTON "BEADPLWABLU" is a bead travel pillow.
BARCODE_ITEM = {"8901188630770": "PILLOW"}

GENDERS = {"MALE": ("Male", "Adult"), "FEMALE": ("Female", "Adult"), "FMALE": ("Female", "Adult"),
           "FEAMAL": ("Female", "Adult"), "FEAMLE": ("Female", "Adult"), "FMAMLE": ("Female", "Adult"),
           "KIDS MALE": ("Male", "Kids"), "KIDSM": ("Male", "Kids"), "KIDM": ("Male", "Kids"),
           "KIDEM": ("Male", "Kids"), "KIDS FEMALE": ("Female", "Kids"), "KIDSF": ("Female", "Kids"),
           "FEMALE KIDS": ("Female", "Kids"), "K FEMALE": ("Female", "Kids"), "KIDS": ("", "Kids"),
           "UNISEX": ("Unisex", ""), "MIX": ("Unisex", "")}
OCCASIONS = {"CASUAL WEAR": "Casual", "FORMAL WEAR": "Formal", "FORMAL WAER": "Formal", "PARTY WEAR": "Party & festive",
             "PARTY WAER": "Party & festive", "TRADITIONAL WEAR": "Party & festive", "SPORTS WEAR": "Sports",
             "SEASONAL WEAR": "Winter", "WINTER WEAR": "Winter", "WINTER SET": "Winter", "NIGHTWEAR": "Night"}
#: Legacy SUB CATEGORY text that names a fabric or finish, not a category.
CATEGORY_FABRIC = {"SILK SAREE": "Silk", "SY SAREE": "Synthetic", "SY  SAREE": "Synthetic", "KOTA": "Kota",
                   "TANAT SAREE": "Cotton"}
#: Legacy price-tier codes end in E, M or P (Economy, Medium, Premium); these words only look like codes.
TIERS = {"E": "Economy", "M": "Medium", "P": "Premium"}
_TIER_CODE = re.compile(r"^[A-Z]{1,6}[EMP]\.?$")
NOT_TIER = {"TOP", "SAREE", "BLOUSE", "LDUP", "SAREM"}

FITS = {"SLIM": "Slim", "SLIM FIT": "Slim", "REGULAR": "Regular", "REGULER": "Regular", "RELAXED": "Relaxed",
        "SKINNY": "Skinny", "SUPPER SKINNY": "Super skinny", "SUPER SLIM": "Super slim", "STRAIGHT": "Straight",
        "SLIM STRAIGHT": "Slim straight", "REGULAR STRAIGHT": "Regular straight", "SLIM TEPAR": "Slim tapered",
        "REGULAR TEPAR": "Regular tapered", "BOOTCUT": "Bootcut", "LOSS": "Loose", "FREE FIT": "Free",
        "ANKLE": "Ankle length"}
SLEEVES = {"FULL SHIRT": "Full", "HALF SHIRT": "Half", "HALF SLEEVE": "Half", "TANK TOP": "Sleeveless"}
NECKS = {"ROUND NECK": "Round", "POLO NECK": "Polo", "V NECK": "V", "HIGH NECK": "High", "HOODED NECK": "Hooded"}
FIT_FABRIC = {"SILK SAREE": "Silk", "COTTON SAREE": "Cotton", "COTTON KURTI": "Cotton"}
PIECES = {"CORD SET": 2, "3 PCE SET": 3, "DHOTI KURTA SET": 2}
BANDS = {"VLM": "Very low", "LM": "Low", "LM A": "Low", "A LM": "Low", "MM": "Medium", "MM A": "Medium",
         "HM": "High", "VHM": "Very high"}
#: Fit text that names a product variant or the brand's cut: kept as STYLE NAME.
STYLES = {"KANO", "ROVER", "ROVAR", "CHICO", "RICARDO", "REGALLO", "KRIVO", "DENVER", "NICO", "TRACK PANT",
          "PAJAMA", "CARGO", "TRUNK", "BOXER", "SF"}
#: Peter England range codes: ``PE RG Mainline`` is a Regular fit in the Mainline range.
_PE_CODE = re.compile(r"^P[A-Z] (RG|SL|SS)\b")
_PE_FIT = {"RG": "Regular", "SL": "Slim", "SS": "Super slim"}
#: Fit text that is not a fit at all (a size, a promotion, a blank).
NO_FIT = {"", ".", "FREE SIZE", "FS", "GIFT", "PROMO", "FULL SET"}

LISTS = {
    "GENDER": ["Male", "Female", "Unisex"],
    "AGE GROUP": ["Adult", "Kids", "Infant"],
    "OCCASION": ["Casual", "Formal", "Party & festive", "Sports", "Night", "Winter"],
    "FIT": sorted(set(FITS.values()) | set(_PE_FIT.values())),
    "SLEEVE": ["Full", "Half", "Sleeveless"],
    "NECK": ["Round", "Polo", "V", "High", "Hooded"],
    "FABRIC": ["Cotton", "Silk", "Synthetic", "Denim", "Kota"],
    "CONSTRUCTION": ["Knit", "Woven"],
    "PRICE TIER": ["Economy", "Medium", "Premium"],
    "MARGIN BAND": ["Very low", "Low", "Medium", "High", "Very high"],
    "SIZE SYSTEM": ["Alpha", "Numeric", "Age (years)", "Age (months)", "Cup", "Length (cm)", "Volume (ml)",
                    "Dimensions", "Range", "Free size"],
}


def _key(value: Any) -> str:
    return " ".join(text(value).upper().split())


def item_of(raw: Any, barcode: str) -> tuple[str, dict[str, Any]]:
    """The canonical ITEM and what the legacy name also said, or ``("", {})`` when it is not on the list."""
    if barcode in BARCODE_ITEM:
        return BARCODE_ITEM[barcode], {}
    name = _key(raw)
    if name in ALIASES:
        return ALIASES[name]
    return (name, {}) if name in ITEMS else ("", {})


def price_tier(category: Any) -> str:
    code = _key(category)
    if code in NOT_TIER or not _TIER_CODE.match(code):
        return ""
    return TIERS[code.rstrip(".")[-1]]


def read_fit(raw: Any) -> dict[str, Any]:
    """The meanings packed into one legacy FIT value, or ``{"unknown": text}``."""
    value = _key(raw)
    if value in NO_FIT:
        return {}
    for table, column in ((FITS, "FIT"), (SLEEVES, "SLEEVE"), (NECKS, "NECK"), (FIT_FABRIC, "FABRIC"),
                          (PIECES, "PIECES"), (BANDS, "MARGIN BAND")):
        if value in table:
            return {column: table[value]}
    found = _PE_CODE.match(text(raw))
    if found:
        return {"FIT": _PE_FIT[found[1]], "STYLE NAME": text(raw)}
    if value in STYLES:
        return {"STYLE NAME": "ROVER" if value == "ROVAR" else value}
    return {"unknown": text(raw)}


_RANGE = re.compile(r"^(\d+)\s*(?:-|TO|\*|/|\s)\s*(\d+)$")
_YEARS = re.compile(r"^(\d+)\s*-\s*(\d+)\s*(?:Y|YR|YRS)$|^(\d+)\s*(?:Y|YR)$")
_MONTHS = re.compile(r"^(\d+)\s*-\s*(\d+)\s*M$")
_ALPHA = {"XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL", "5XL", "6XL"}


def size_of(raw: Any) -> tuple[str, str]:
    """(SIZE, SIZE SYSTEM); a blank, ``.`` or ``NA`` is an unknown size."""
    value = _key(raw)
    if value in {"", ".", "..", "-", "NA"}:
        return "", ""
    if value in {"FS", "FREE", "FREE SIZE"}:
        return "FREE SIZE", "Free size"
    if value in {"XXXL", "3XXL"}:
        return "3XL", "Alpha"
    if value in _ALPHA:
        return value, "Alpha"
    if value.isdigit():
        return value, "Numeric"
    if re.fullmatch(r"\d{2}[A-G]", value):
        return value, "Cup"
    found = _YEARS.match(value)
    if found:
        return (f"{found[1]}-{found[2]} Y" if found[1] else f"{found[3]} Y"), "Age (years)"
    found = _MONTHS.match(value)
    if found:
        return f"{found[1]}-{found[2]} M", "Age (months)"
    found = re.fullmatch(r"(\d+)\s*CM", value)
    if found:
        return f"{found[1]} CM", "Length (cm)"
    if re.fullmatch(r"\d+\s*ML", value):
        return value.replace("ML", " ML").replace("  ", " "), "Volume (ml)"
    if " X " in value:
        return value, "Dimensions"
    found = _RANGE.match(value)
    if found:
        return f"{found[1]}-{found[2]}", "Range"
    if re.fullmatch(r"[A-Z0-9]+\s*(?:\*|/|TO)\s*[A-Z0-9]+", value):
        return re.sub(r"\s*(?:\*|/|TO)\s*", "-", value), "Range"
    return value, ""


_LABEL = re.compile(r"\(\s*([A-Za-z]{3})[A-Za-z]*\s*-\s*(\d{2})\s*\)")
_DATE = re.compile(r"^(\d{4})-(\d{1,2})-\d{1,2}|^\d{1,2}[-/.](\d{1,2})[-/.](\d{4})$")


def season_of(legacy: Any) -> tuple[str, str]:
    """(SEASON, ARRIVAL MONTH). A season after next year is a legacy typo (AW67) and stays unknown."""
    raw = text(legacy)
    season = normalize_season(raw) or ""
    if season and int(season[3:]) > date.today().year % 100 + 1:
        season = ""
    month = ""
    found = _LABEL.search(raw)
    if found and found[1].casefold() in MONTHS:
        month = f"20{found[2]}-{MONTHS[found[1].casefold()]:02d}"
    found = _DATE.match(raw)
    if found:
        year, mon = (found[1], found[2]) if found[1] else (found[4], found[3])
        month = f"{year}-{int(mon):02d}"
    return season, month


def money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(CENT, rounding=ROUND_HALF_UP)


def taxes(division: str, item: str, basic: Decimal, mrp: Decimal) -> tuple[int, int]:
    """INPUT TAX and OUTPUT TAX by the KDPS sheet's ladder (sarees 5; belts, wallets, bags, beauty, electronics 18)."""
    if item == "SAREE":
        return 5, 5
    if item in {"BELT", "WALLET"} or division in {"Bags & Luggage", "Beauty", "Electronics"}:
        return 18, 18
    return (5 if basic <= 2500 else 18), (5 if mrp <= Decimal("2625") else 18)


@dataclass
class Result:
    rows: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    report: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def convert(pt_path: Path, landed_pct: Decimal = LANDED_PCT) -> Result:
    factor = 1 + landed_pct / 100
    lines = _table(pt_path, {"barcode", "qty", "mrp", "hsn"})
    out: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    unknown_items: Counter[str] = Counter()
    unknown_fits: Counter[str] = Counter()
    unknown_sizes: Counter[str] = Counter()
    mapped: dict[str, Counter[str]] = defaultdict(Counter)
    cost_changed: list[str] = []
    rounded: list[str] = []
    over_mrp: list[str] = []
    value_before = value_after = Decimal(0)
    landed_seen: Counter[str] = Counter()
    for line in lines:
        barcode = text(line.get("barcode"))
        qty = int(number(line.get("qty")) or 0)
        mrp = number(line.get("mrp"))
        basic_in, rate_in = number(line.get("basic")), number(line.get("prate"))
        landed = landed_pct
        if basic_in is not None:
            basic = money(basic_in)
            if basic != basic_in:
                rounded.append(f"{barcode}: BASIC {text(basic_in)} → {basic}")
        else:
            basic = money(rate_in / factor) if rate_in is not None else None
        if basic_in is not None and rate_in is not None and basic:
            # The bill states both: its P Rate is the real landed cost, and LANDED % only describes it.
            p_rate = money(rate_in)
            landed = money((rate_in / basic - 1) * 100) + 0  # + 0 turns -0.00 into 0.00
        else:
            p_rate = money(basic * (1 + landed / 100)) if basic is not None else None
        landed_seen[str(landed)] += qty
        if rate_in is not None and p_rate is not None:
            value_before += rate_in * qty
            value_after += p_rate * qty
            if abs(p_rate - rate_in) > CENT:
                cost_changed.append(f"{barcode}: source P Rate {text(rate_in)}, BASIC {basic} × {factor} = {p_rate}")
        if p_rate is not None and mrp is not None and p_rate > mrp:
            over_mrp.append(f"{barcode}: P RATE {p_rate} above MRP {text(mrp)}")

        item, implied = item_of(line.get("item"), barcode)
        if not item:
            unknown_items[_key(line.get("item"))] += qty
        division, category = ITEMS.get(item, ("", ""))
        gender, age = GENDERS.get(_key(line.get("gender")), ("", ""))
        legacy_category = _key(line.get("subcategory"))
        fit = read_fit(line.get("fit"))
        if "unknown" in fit:
            unknown_fits[fit.pop("unknown")] += qty
        size, system = size_of(line.get("size"))
        if size and not system:
            unknown_sizes[size] += qty
        if system == "Age (months)" and age in {"", "Kids"}:
            age = "Infant"
        season, month = season_of(line.get("legacyseason") or line.get("season"))
        hsn = text(line.get("hsn"))
        construction = ""
        if len(hsn) >= 6 and hsn[:2] in {"61", "62"}:
            construction = "Knit" if hsn.startswith("61") else "Woven"
        row: dict[str, Any] = {
            "BARCODE": barcode, "BRAND": text(line.get("brand")), "DESIGN": text(line.get("design")),
            "STYLE NAME": fit.get("STYLE NAME", ""), "DIVISION": division, "CATEGORY": category, "ITEM": item,
            "GENDER": gender, "AGE GROUP": age, "OCCASION": OCCASIONS.get(legacy_category, ""),
            "FIT": fit.get("FIT", ""), "SLEEVE": fit.get("SLEEVE", ""), "NECK": fit.get("NECK", ""),
            "PIECES": fit.get("PIECES"), "FABRIC": CATEGORY_FABRIC.get(legacy_category) or fit.get("FABRIC", ""),
            "CONSTRUCTION": construction, "COLOUR": "", "PRICE TIER": price_tier(legacy_category),
            "MARGIN BAND": fit.get("MARGIN BAND", ""), "SIZE": size, "SIZE SYSTEM": system, "SEASON": season,
            "ARRIVAL MONTH": month, "HSN": hsn, "QTY": qty, "MRP": mrp, "BASIC": basic, "LANDED %": landed, "P RATE": p_rate,
        }
        for column, value in implied.items():  # what the item's own name said (BOY SET is for boys)
            if not row.get(column):
                row[column] = value
        if basic is not None and mrp:
            row["INPUT TAX"], row["OUTPUT TAX"] = taxes(division, item, basic, mrp)
            row["MARGIN %"] = money((mrp - p_rate) / mrp * 100)
        for column in ("OCCASION", "FIT", "SLEEVE", "NECK", "FABRIC", "PRICE TIER", "MARGIN BAND", "AGE GROUP"):
            mapped[column][row[column] or "(unknown)"] += qty
        out.append(row)
        sources.append({"BARCODE": barcode, "Item": text(line.get("item")), "Sub Category": text(line.get("subcategory")),
                        "Gender": text(line.get("gender")), "Fit": text(line.get("fit")), "Size": text(line.get("size")),
                        "Legacy Season": text(line.get("legacyseason") or line.get("season")),
                        "Basic": basic_in, "P Rate": rate_in,
                        "Source": text(line.get("sourcefile")) or f"{pt_path.name} row {line['_row']}"})

    pieces = sum(row["QTY"] for row in out)
    report = [f"Source: {pt_path.name}; rows {len(out)}; barcodes {len({r['BARCODE'] for r in out})}; pieces {pieces}",
              f"P RATE = BASIC × (1 + LANDED %); LANDED % from the bill, else {landed_pct}%",
              "Pieces by LANDED %: " + ", ".join(f"{pct}% {qty}" for pct, qty in landed_seen.most_common()), ""]
    report += [f"Stock value at P RATE: source {money(value_before)}, now {money(value_after)} "
               f"(difference {money(value_after - value_before)})",
               f"Rows whose P RATE moved by more than 1 paisa ({len(cost_changed)}; first {EXAMPLES}):",
               *(f"  {line}" for line in cost_changed[:EXAMPLES])]
    if rounded:
        report += ["", f"BASIC rounded to paise ({len(rounded)}):", *(f"  {line}" for line in rounded[:EXAMPLES])]
    if over_mrp:
        report += ["", f"P RATE above MRP ({len(over_mrp)}); these rows cannot open:", *(f"  {line}" for line in over_mrp)]
    report += ["", "Pieces by value (unknown = not in the source):"]
    for column, counts in mapped.items():
        report.append(f"  {column}: " + ", ".join(f"{value} {qty}" for value, qty in counts.most_common()))
    for title, counts in (("ITEM not on the item list", unknown_items), ("FIT text not understood", unknown_fits),
                          ("SIZE with no size system", unknown_sizes)):
        report += ["", f"{title} ({sum(counts.values())} pieces):",
                   *(f"  {value or '(blank)'}: {qty}" for value, qty in counts.most_common())]
    stats = {"rows": len(out), "pieces": pieces, "unknown_items": sum(unknown_items.values()),
             "over_mrp": len(over_mrp), "cost_changed": len(cost_changed)}
    return Result(out, sources, report, stats)


_HEAD = Font(bold=True)
_FILL = PatternFill("solid", fgColor="FFD9E2F3")
_DERIVED_FILL = PatternFill("solid", fgColor="FFEDEDED")


def _number(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value


def write_workbook(result: Result, path: Path, landed_pct: Decimal = LANDED_PCT) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "PT"
    sheet.append(HEADER)
    for cell in sheet[1]:
        cell.font, cell.fill = _HEAD, _DERIVED_FILL if cell.value in DERIVED else _FILL
    for row in result.rows:
        sheet.append([_number(row.get(column)) for column in HEADER])
    for column in ("BARCODE", "HSN", "SIZE", "DESIGN"):
        index = HEADER.index(column) + 1
        for cells in sheet.iter_rows(min_row=2, min_col=index, max_col=index):
            cells[0].number_format = "@"
    for column in ("MRP", "BASIC", "P RATE"):
        index = HEADER.index(column) + 1
        for cells in sheet.iter_rows(min_row=2, min_col=index, max_col=index):
            cells[0].number_format = "0.00"
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions

    lists = workbook.create_sheet("Lists")
    lists.append(["ITEM", "DIVISION", "CATEGORY"])
    for item, (division, category) in sorted(ITEMS.items(), key=lambda kv: (kv[1], kv[0])):
        lists.append([item, division, category])
    width = 5
    for offset, (name, values) in enumerate(LISTS.items()):
        lists.cell(1, width + offset, name)
        for index, value in enumerate(values, start=2):
            lists.cell(index, width + offset, value)
    for cell in lists[1]:
        if cell.value:
            cell.font, cell.fill = _HEAD, _FILL

    source = workbook.create_sheet("Source values")
    source.append(SOURCE_HEADER)
    for row in result.sources:
        source.append([_number(row.get(column)) for column in SOURCE_HEADER])
    for cell in source[1]:
        cell.font = _HEAD

    about = workbook.create_sheet("About")
    for line in [
        "Canonical PT format: one column, one meaning; every list value from the Lists sheet. Blank = unknown.",
        "Grey headers are derived: DIVISION and CATEGORY from ITEM, SIZE SYSTEM from SIZE, money and tax from BASIC.",
        "BASIC is the supplier's basic cost. P RATE = BASIC × (1 + LANDED %) is the landed cost KDPS values stock at.",
        f"LANDED % is the bill's own where it shows both BASIC and P RATE, else the default {landed_pct}%.",
        "INPUT TAX / OUTPUT TAX follow the KDPS sheet's ladder for reference; the app charges tax from Tax settings.",
        "SEASON is the house season (SS-YY / AW-YY); ARRIVAL MONTH is the month the legacy season text carried.",
        "CONSTRUCTION is filled only from an invoice HSN (61 = knit, 62 = woven).",
        "The 'Source values' sheet keeps each row's original text and cost.",
    ]:
        about.append([line])
    workbook.save(path)


def self_check(path: Path) -> dict[str, Any]:
    """What the PT opening upload's reader makes of the file."""
    from ptmapper.pt_opening import parse_pt_files

    metadata, rows, problems = parse_pt_files([(path.name, path.read_bytes())])
    opening = metadata["pt_opening"]
    return {"barcodes": len(rows), "pieces": metadata["quantity"], "problems": len(problems),
            "conflicts": opening["conflict_count"], "unreadable": opening["unreadable_count"],
            "problem_list": opening["problems"][:20]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pt", type=Path, required=True, help="a normalised PT .xlsx (normalize_pt_files / soh_to_pt)")
    parser.add_argument("--out", type=Path, required=True, help="folder for the canonical file and report")
    parser.add_argument("--name", default="PT_canonical.xlsx")
    parser.add_argument("--landed-pct", type=Decimal, default=LANDED_PCT,
                        help="LANDED %% where the source shows no BASIC (default 20)")
    args = parser.parse_args(argv)
    result = convert(args.pt, args.landed_pct)
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / args.name
    write_workbook(result, target, args.landed_pct)
    check = self_check(target)
    lines = [*result.report, "", "Self-check with the PT opening reader:",
             (f"  barcodes {check['barcodes']}, pieces {check['pieces']}, problems {check['problems']}, "
              f"price conflicts {check['conflicts']}, unreadable {check['unreadable']}"),
             *(f"  problem: {p['barcode']} {p['reason']}" for p in check["problem_list"])]
    report = args.out / f"{target.stem}-report.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:5] + lines[-2 - len(check["problem_list"]):]))
    print(f"Wrote {target} and {report}")
    return 1 if check["problems"] or check["unreadable"] or result.stats["unknown_items"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
