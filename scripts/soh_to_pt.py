"""Build one normalised PT file from a store's legacy SOH alone (store test run).

``scripts/normalize_pt_files.py`` combines a store's real PT bills with its SOH.
A store with no PT files (Vaishnavi, SGRM) has only its SOH; this writes the
same 21-column workbook from it, which the PT upload at /goods/opening reads
unchanged, and a report of every choice. The inputs are only read.

* Item, Brand, Size, Design, Sub Category, Gender and Fit are the SOH's own
  (brands are not merged); a Design No that Excel turned into a date is written
  as ``YYYY-MM-DD``;
* Season is the SOH's, as ``SS-YY`` / ``AW-YY`` (``ptmapper.legacy_season``);
* Qty is Tqty, P RATE the SOH Rate (owner decision, 31 Oct 2026), MRP the SOH's;
* Bill Date and BillNo stay blank: the opening turns a BillNo into the stock's
  "PT bill" origin (``soh_services.older_origin_ref``), and the SOH names none;
* rows with no stock, negative stock, or no usable cost/MRP are left out and listed;
* HSN, as the SOH has none: the same barcode's in a ``--hsn-from`` PT, else the
  HSN most used there by the same brand, item and gender, else ``HSN_MAP``.

    backend/.venv/bin/python scripts/soh_to_pt.py --soh docs/vashnavi-singh-more/SGRM_SOH_30-10-2026.xlsx \\
        --hsn-from docs/wardrobe-ratu-road/normalized/WARDROBE_PT_combined.xlsx \\
        --out docs/vashnavi-singh-more/normalized --name SGRM_PT_from_SOH.xlsx

The outputs hold cost data: keep them out of git.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ptmapper.legacy_season import normalize_season
from scripts.normalize_pt_files import EXAMPLES, _key, _table, number, self_check, text, valid_hsn, write_workbook

SOH_NEEDS = {"barcode", "tqty", "mrp", "rate"}
#: Footer columns compared with the item rows' sums.
TOTALS = ("opqty", "purchase", "sale", "adjustment", "slret", "sttrf", "stfreciept", "purret", "tqty", "amount")

#: Item name → HSN heading, by gender (``M`` men and boys, ``F`` women and girls) or for all.
#: Researched 31 Oct 2026 for the SGRM SOH, which names no fibre and no knit or weave, so
#: these are 4-digit headings: woven chapter 62 unless the item is usually knitted (61).
#: Sarees are classified as fabric. Review with the CA before a real opening.
_MF = lambda m, f: {"M": m, "F": f}  # noqa: E731
HSN_MAP: dict[str, str | dict[str, str]] = {
    **dict.fromkeys(["SAREE", "SAREES"], "5407"),
    "TANAT SAREE": "5208",
    **dict.fromkeys(["SHIRT", "KURTA"], _MF("6205", "6206")),
    **dict.fromkeys(["TOP", "CROP TOP", "BLOUSE", "KURTI"], "6206"),
    **dict.fromkeys(["KURTI SET", "WINTER KURTI SET", "SALWAR SUIT", "PALAZZO SET", "PLAZO SET", "SARARA", "GARARA",
                     "PATIALA", "LEHENGA", "CO-ORD SET", "SET", "SKIRT SET", "CROP SET", "PANT SET", "GIRL SET",
                     "DRESSES", "FROCK", "GOWN", "MIDY", "MIDDI", "JUMP SUIT", "SKIRT", "BABA SUIT", "KURTA SET",
                     "KURTA PAJAMA", "BUNDY", "BUNDY SET", "WAISTCOAT", "WAIST COAT", "BLAZER", "SUIT", "KIDS SET",
                     "BOY SET", "JEANS", "TROUSERS", "TROUSER", "PANT", "CARGO", "SHORTS", "SHORT", "HOT PANT", "BOXER",
                     "DUNGAREE"], _MF("6203", "6204")),
    **dict.fromkeys(["LOWER", "TRACK PANT", "JOGGER", "WINTER LOWER", "WINTER SET"], _MF("6103", "6104")),
    **dict.fromkeys(["LEGGINGS", "JEGGING", "JEGGINGS"], "6104"),
    **dict.fromkeys(["T-SHIRT", "T SHIRT", "VEST", "THERMAL", "KIDS THERMAL SET"], "6109"),
    **dict.fromkeys(["SWEATSHIRT", "SWEAT SHIRT", "CARDIGAN", "SWEATER", "WINTER TOP"], "6110"),
    **dict.fromkeys(["JACKET", "COAT"], _MF("6201", "6202")),
    **dict.fromkeys(["INFANT SET", "BABY ROMPER", "BLOOMER"], "6111"),
    **dict.fromkeys(["PETTICOAT", "NIGHTY", "NIGHT SUIT", "NIGHT WEAR", "NIEGHTWEAR"], "6208"),
    "BRA": "6212",
    **dict.fromkeys(["PANTY", "PANTIE", "HIPSTER"], "6108"),
    **dict.fromkeys(["BRIEF", "TRUNK"], "6107"),
    "SOCKS": "6115",
    **dict.fromkeys(["CAP", "WINTER CAP"], "6505"),
    **dict.fromkeys(["STOLE", "DUPATTA", "DUPTTA", "L DUPTTA", "SHAWL", "MUFFLER", "SCARF"], "6214"),
    **dict.fromkeys(["HANDKERCHIEF", "HANDKERCHIEFS"], "6213"),
    **dict.fromkeys(["ARM SHIELD", "ARM SLIDE"], "6117"),
    **dict.fromkeys(["BEDSHEET", "TOWEL", "BATH MAT"], "6302"),
    **dict.fromkeys(["DOHAR", "PILLOW", "SLEEPING BAG"], "9404"),
    **dict.fromkeys(["TROLLEY", "TROLLY", "DUFFLE TROLLY", "DUFFEL BAG", "BACKPACK", "BACKPACKS", "BAG", "WALLET"], "4202"),
    "BELT": "4203",
    "UMBRELLA": "6601",
    "FLIP FLOP": "6402",
    "MIXER GRINDER": "8509",
    "SPEAKER": "8518",
    "CARRY BAG": "3923",
}
#: Rows whose item name says too little: a CARLTON "BEADPLWABLU" is a bead travel pillow.
BARCODE_HSN = {"8901188630770": "9404"}
#: Mapped, but the SOH does not say enough to be sure.
CONFIRM = {"CARRY BAG": "confirm the material (plastic 3923, paper 4819, jute or cloth 6305)"}


def gender_group(raw: Any) -> str | None:
    """``M`` (men, boys), ``F`` (women, girls) or ``None`` (unisex, an accessory, a typo like ``.``)."""
    value = _key(raw).upper()
    if "KID" in value:
        rest = value.replace("KIDS", "").replace("KID", "")
        return "F" if "F" in rest else "M" if "M" in rest else None
    if value.startswith("F"):
        return "F"
    if value.startswith("M") and not value.startswith("MIX"):
        return "M"
    return None


def _item(value: Any) -> str:
    return " ".join(text(value).upper().split())


def _day(value: Any) -> Any:
    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat() if isinstance(value, date) else value


@dataclass
class Evidence:
    """HSN and cost from real PT files (another store's, from the same warehouse)."""
    by_barcode: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    by_key: dict[tuple[str, str, str | None], Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    rate: dict[str, str] = field(default_factory=dict)


def evidence(paths: list[Path]) -> Evidence:
    found = Evidence()
    for path in paths:
        for row in _table(path, {"barcode", "hsn"}):
            barcode, hsn = text(row.get("barcode")), text(row.get("hsn"))
            if not barcode or not valid_hsn(hsn):
                continue
            found.by_barcode[barcode][hsn] += 1
            key = (text(row.get("brand")).upper(), _item(row.get("item")), gender_group(row.get("gender")))
            found.by_key[key][hsn] += 1
            if text(row.get("prate")):
                found.rate[barcode] = text(row.get("prate"))
    return found


@dataclass
class Result:
    rows: list[dict[str, Any]]
    report: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def convert(soh_path: Path, hsn_from: list[Path]) -> Result:
    table = _table(soh_path, SOH_NEEDS)
    items = [row for row in table if text(row.get("barcode"))]
    footers = [row for row in table if not text(row.get("barcode"))]
    known = evidence(hsn_from)

    # An unisex or mistyped gender takes the item's usual one.
    usual: dict[str, Counter[str]] = defaultdict(Counter)
    for row in items:
        group = gender_group(row.get("gender"))
        if group:
            usual[_item(row.get("itemname"))][group] += 1

    def group_of(row: dict[str, Any]) -> str | None:
        found = gender_group(row.get("gender"))
        if found:
            return found
        item = _item(row.get("itemname"))
        counts = usual.get(item)
        if counts:
            return counts.most_common(1)[0][0]
        return "M" if "BOY" in item else "F" if "GIRL" in item else None

    def hsn_for(row: dict[str, Any], barcode: str, group: str | None) -> tuple[str, str]:
        if barcode in known.by_barcode:
            return known.by_barcode[barcode].most_common(1)[0][0], "same barcode in a PT"
        key = (text(row.get("brand")).upper(), _item(row.get("itemname")), group)
        if key in known.by_key:
            return known.by_key[key].most_common(1)[0][0], "same brand, item and gender in a PT"
        if barcode in BARCODE_HSN:
            return BARCODE_HSN[barcode], "researched map"
        mapped = HSN_MAP.get(_item(row.get("itemname")))
        if isinstance(mapped, dict):
            mapped = mapped.get(group or "")
        return (mapped, "researched map") if mapped else ("", "no HSN")

    out: list[dict[str, Any]] = []
    left_out: dict[str, list[str]] = defaultdict(list)
    left_pieces: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    hsn_table: Counter[tuple[str, str, str, str]] = Counter()
    seasons: dict[tuple[str, str], set[str]] = defaultdict(set)
    design_dates: list[str] = []
    short_numbers: list[str] = []
    rate_differs: list[str] = []
    missing_hsn: list[str] = []
    for row in items:
        barcode = text(row.get("barcode"))
        qty, mrp, rate = number(row.get("tqty")), number(row.get("mrp")), number(row.get("rate"))
        where = f"{barcode} (row {row['_row']})"
        reason = ("no stock (Tqty 0)" if not qty else "negative Tqty" if qty < 0 else
                  "no MRP" if mrp is None or mrp <= 0 else "no Rate" if rate is None or rate <= 0 else
                  "Rate above MRP" if rate > mrp else "")
        if reason:
            left_out[reason].append(f"{where}: Tqty {text(qty)}, MRP {text(mrp)}, Rate {text(rate)}")
            left_pieces[reason] += int(qty or 0)
            continue
        if isinstance(row.get("barcode"), int) and len(barcode) < 8:
            short_numbers.append(barcode)
        design = row.get("designno")
        if isinstance(design, (datetime, date)):
            design_dates.append(f"{barcode}: {_day(design)}")
        group = group_of(row)
        hsn, tier = hsn_for(row, barcode, group)
        tiers[tier] += 1
        hsn_table[(_item(row.get("itemname")), group or "-", hsn or "(none)", tier)] += int(qty)
        if not hsn:
            missing_hsn.append(f"{where} {_item(row.get('itemname'))}")
        if barcode in known.rate and number(known.rate[barcode]) != rate:
            rate_differs.append(f"{barcode}: SOH Rate {text(rate)}, PT P RATE {known.rate[barcode]}")
        legacy = text(_day(row.get("season")))
        season = normalize_season(legacy) or ""
        seasons[(legacy, season or "unknown historical")].add(barcode)
        out.append({"Bill Date": None, "BillNo": None, "Item": text(row.get("itemname")), "Brand": text(row.get("brand")),
                    "Size": text(row.get("size")), "Design": text(_day(design)), "Sub Category": text(row.get("category")),
                    "Gender": text(row.get("gender")), "Fit": text(row.get("fit")), "Season": season,
                    "Legacy Season": legacy, "Barcode": barcode, "HSN": hsn, "Qty": int(qty), "Basic": None,
                    "P Rate": row.get("rate"), "MRP": row.get("mrp"), "Input Tax": None, "Output Tax": None,
                    "Bill Amount": row.get("amount"), "Source File": f"{soh_path.name} row {row['_row']}"})

    pieces = sum(row["Qty"] for row in out)
    report = [f"SOH: {soh_path.name}; item rows {len(items)}; rows written: {len(out)}; pieces: {pieces}",
              f"HSN evidence from: {', '.join(p.name for p in hsn_from) or 'none'}", ""]
    report.append(f"Rows left out ({sum(len(v) for v in left_out.values())}):")
    for reason, lines in left_out.items():
        more = f" (first {EXAMPLES})" if len(lines) > EXAMPLES and reason == "no stock (Tqty 0)" else ""
        report.append(f"  {reason}: {len(lines)} rows, {left_pieces[reason]} pieces{more}")
        shown = lines[:EXAMPLES] if more else lines
        report += [f"    {line}" for line in shown]
    if footers:
        report += ["", "SOH totals row against its item rows:"]
        for column in TOTALS:
            footer = sum((number(f.get(column)) or 0) for f in footers)
            rows_sum = sum((number(r.get(column)) or 0) for r in items)
            mark = "" if footer == rows_sum else "   <- differs"
            report.append(f"  {column:11} totals {text(footer):>14}  rows {text(rows_sum):>14}{mark}")
    report += ["", "HSN source (rows):", *(f"  {tier}: {count}" for tier, count in tiers.most_common())]
    report += ["", "HSN by item and gender (item | gender | HSN | source: pieces):"]
    report += [f"  {item:20} | {group} | {hsn:8} | {tier}: {qty}"
               for (item, group, hsn, tier), qty in sorted(hsn_table.items())]
    confirm = [f"  {item}: {why}" for item, why in CONFIRM.items() if any(k[0] == item and k[3] == "researched map" for k in hsn_table)]
    if confirm:
        report += ["", "HSN to confirm:", *confirm]
    if missing_hsn:
        report += ["", f"No HSN found ({len(missing_hsn)}); add the item to HSN_MAP:", *(f"  {m}" for m in missing_hsn)]
    report += ["", f"SOH Rate differs from the PT's P RATE for the same barcode ({len(rate_differs)}; the SOH Rate is used):",
               *(f"  {line}" for line in rate_differs)]
    report += ["", f"Design No that Excel turned into a date ({len(design_dates)}; written as the date):",
               *(f"  {line}" for line in design_dates[:EXAMPLES])]
    report += ["", f"Number barcodes shorter than 8 digits ({len(short_numbers)}): check a few against the "
               f"printed tags for lost leading zeros. First {EXAMPLES}: {', '.join(short_numbers[:EXAMPLES])}"]
    report += ["", "Seasons (legacy text → season: barcodes):"]
    report += [f"  {legacy or '(blank)'!s:32} → {season}: {len(codes)}"
               for (legacy, season), codes in sorted(seasons.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    per_season: Counter[str] = Counter()
    for row in out:
        per_season[row["Season"] or "unknown historical"] += row["Qty"]
    report += ["", "Pieces per season:", *(f"  {season}: {qty}" for season, qty in sorted(per_season.items()))]
    later = sorted(s for s in per_season if s[:3] in {"SS-", "AW-"} and int(s[3:]) > date.today().year % 100 + 1)
    if later:
        report += ["", f"Seasons after next year, probably legacy typos (check them): {', '.join(later)}"]
    prefixes = sorted({row["HSN"][:2] for row in out if row["HSN"]})
    report += ["", f"Tax settings need a rule for each HSN chapter used ({', '.join(prefixes)}); an HSN with no "
               "rule takes the fallback rate and its bill is flagged."]
    stats = {"rows": len(out), "pieces": pieces, "left_out": {k: len(v) for k, v in left_out.items()},
             "tiers": dict(tiers), "missing_hsn": len(missing_hsn)}
    return Result(out, report, stats)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--soh", type=Path, required=True, help="the store's legacy SOH .xlsx")
    parser.add_argument("--hsn-from", type=Path, action="append", default=[],
                        help="a normalised PT .xlsx whose HSN and P RATE are evidence (repeatable)")
    parser.add_argument("--out", type=Path, required=True, help="folder for the PT file and report")
    parser.add_argument("--name", default="PT_from_SOH.xlsx")
    args = parser.parse_args(argv)
    result = convert(args.soh, args.hsn_from)
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / args.name
    write_workbook(result.rows, target)
    check = self_check(target)
    lines = [*result.report, "", "Self-check with the PT upload's reader:",
             (f"  barcodes {check['barcodes']}, pieces {check['pieces']}, rows {check['pt_rows']}, "
              f"problems {check['problems']}, price conflicts {check['conflicts']}, unreadable {check['unreadable']}"),
             *(f"  problem: {p['barcode']} {p['reason']}" for p in check["problem_list"])]
    (args.out / "normalize-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:1] + lines[-2 - len(check["problem_list"]):]))
    print(f"Wrote {target} and {args.out / 'normalize-report.txt'}")
    return 1 if check["problems"] or check["unreadable"] or result.stats["missing_hsn"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
