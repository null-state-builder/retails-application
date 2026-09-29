"""Code 128 label rendering: quiet zones, DPI/module-width fit, frozen SVG (design §8.4).

Pure Python: no Django, no HTTP. A label's own numbers - alias, MRP, module width -
render the same SVG byte for byte given the same inputs, so an old print job replays
its original frozen output even after masters, rates or vocabulary change.

The bar pattern itself comes from ``python-barcode`` (``Code128.build()``), which
already encodes Code 128's subset-switching (A/B/C) correctly - hand-rolling that
symbol table is exactly the kind of silent, unverifiable mistake a scanner would
only catch on real hardware. Everything downstream of the raw module bits - quiet
zones, the DPI/minimum-module-width fit check, and the actual SVG markup - is ours.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from barcode.codex import Code128
from barcode.errors import BarcodeError

#: ISO/IEC 15417: a Code 128 quiet zone is at least 10x the narrow module width.
QUIET_ZONE_MODULES = 10
MM_PER_INCH = Decimal("25.4")
PRINTABLE_MIN = 32
PRINTABLE_MAX = 126
#: Minimum bar height and per-line text height a label needs to stay legible and
#: scannable. Not tenant-configured: the profile pins width/height/DPI/module size,
#: but a label this short cannot carry a real symbol plus its two text lines at all.
MIN_BAR_HEIGHT_MM = Decimal("8")
MIN_TEXT_LINE_MM = Decimal("2.2")
TEXT_LINE_GAP_MM = Decimal("1")
#: Room below the last line's text baseline: an SVG `y` is the baseline, not the
#: glyph's bottom, so a lowercase descender (g, p, y, ...) in the alias would
#: clip against the label's own edge without this.
BOTTOM_MARGIN_MM = Decimal("0.5")
_THREE_PLACES = Decimal("0.001")


class LabelAliasInvalid(Exception):
    """The alias cannot be encoded as Code 128 printable ASCII (``PRINT_ALIAS_INVALID``)."""


class LabelLayoutInvalid(Exception):
    """The symbol, text or MRP cannot fit the pinned label without shrinking it.

    ``PRINT_LAYOUT_INVALID``.
    """


@dataclass(frozen=True)
class RenderedLabel:
    svg: str
    symbol_modules: int
    module_width_mm: Decimal


def _validate_alias(alias: str, max_payload_characters: int) -> None:
    if not alias:
        raise LabelAliasInvalid("The alias is empty.")
    if len(alias) > max_payload_characters:
        raise LabelAliasInvalid(
            f"The alias is {len(alias)} characters; this label profile allows at most "
            f"{max_payload_characters}."
        )
    if any(not (PRINTABLE_MIN <= ord(ch) <= PRINTABLE_MAX) for ch in alias):
        raise LabelAliasInvalid(
            "The alias uses a character outside Code 128's supported printable ASCII."
        )


def _bits(alias: str) -> str:
    try:
        built = Code128(alias).build()
    except BarcodeError as exc:
        raise LabelAliasInvalid(str(exc)) from exc
    return str(built[0])


def _rupees(paise: int) -> str:
    """Whole rupees with Lakh/Crore grouping, paise kept - ``15000000`` → ``₹1,50,000``.

    A shopper reads this off the tag, so it is grouped the Indian way and reads
    the same as the ₹ the screen shows for the same line (`lib/format.tsx`).
    The grouping is `outbound.maker_checker._inr`'s, repeated rather than
    imported: that one is a local audit-row formatter that drops paise, and a
    price tag may not. Negative paise never reach here - `_frozen_mrp_paise`
    refuses them, as `core.goods_money.amount_text` does.
    """
    rupees, remainder = divmod(paise, 100)
    grouped = str(rupees)
    if len(grouped) > 3:
        head, tail = grouped[:-3], grouped[-3:]
        # Indian grouping: every two digits above the last three.
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        grouped = ",".join(filter(None, [head, *groups, tail]))
    return f"₹{grouped}" if remainder == 0 else f"₹{grouped}.{remainder:02d}"


def _bars(bits: str, *, module_width_mm: Decimal, x0: Decimal, bar_height_mm: Decimal) -> str:
    parts: list[str] = []
    x = x0
    index = 0
    total = len(bits)
    while index < total:
        end = index
        while end < total and bits[end] == bits[index]:
            end += 1
        run = end - index
        width = (module_width_mm * run).quantize(_THREE_PLACES, rounding=ROUND_HALF_UP)
        if bits[index] == "1":
            parts.append(
                f'<rect x="{x.quantize(_THREE_PLACES, rounding=ROUND_HALF_UP)}" y="0" '
                f'width="{width}" height="{bar_height_mm}" fill="#000"/>'
            )
        x += module_width_mm * run
        index = end
    return "".join(parts)


def render_label_svg(
    *,
    alias: str,
    mrp_paise: int,
    width_mm: int,
    height_mm: int,
    printer_dpi: int,
    min_module_dots: int,
    max_payload_characters: int,
) -> RenderedLabel:
    """The frozen label for one printed line: an alias, its MRP, at a pinned profile.

    Refuses (never shrinks) when the symbol or its text cannot fit the label at the
    profile's DPI and minimum module width (design "labels that would not fit the
    configured printer are refused rather than shrunk").
    """
    _validate_alias(alias, max_payload_characters)
    bits = _bits(alias)
    module_width_mm = ((Decimal(min_module_dots) / Decimal(printer_dpi)) * MM_PER_INCH).quantize(
        _THREE_PLACES, rounding=ROUND_HALF_UP
    )
    symbol_width_mm = module_width_mm * len(bits)
    quiet_width_mm = module_width_mm * QUIET_ZONE_MODULES
    required_width_mm = symbol_width_mm + 2 * quiet_width_mm
    required_height_mm = (
        MIN_BAR_HEIGHT_MM + 2 * MIN_TEXT_LINE_MM + 2 * TEXT_LINE_GAP_MM + BOTTOM_MARGIN_MM
    )
    if required_width_mm > width_mm:
        raise LabelLayoutInvalid(
            f"The symbol needs {required_width_mm}mm of width including quiet zones; "
            f"the label is only {width_mm}mm wide."
        )
    if required_height_mm > height_mm:
        raise LabelLayoutInvalid(
            f"The label needs at least {required_height_mm}mm of height for the symbol "
            f"and its text; the label is only {height_mm}mm tall."
        )
    bar_height_mm = (
        Decimal(height_mm) - 2 * MIN_TEXT_LINE_MM - 2 * TEXT_LINE_GAP_MM - BOTTOM_MARGIN_MM
    )
    bars_x0 = (Decimal(width_mm) - symbol_width_mm) / 2
    alias_text = html.escape(alias)
    mrp_text = html.escape(_rupees(mrp_paise))
    text_y1 = bar_height_mm + TEXT_LINE_GAP_MM + MIN_TEXT_LINE_MM
    text_y2 = text_y1 + TEXT_LINE_GAP_MM + MIN_TEXT_LINE_MM
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm}mm" height="{height_mm}mm" '
        f'viewBox="0 0 {width_mm} {height_mm}">'
        f'<rect width="100%" height="100%" fill="#fff"/>'
        f"{_bars(bits, module_width_mm=module_width_mm, x0=bars_x0, bar_height_mm=bar_height_mm)}"
        f'<text x="{Decimal(width_mm) / 2}" y="{text_y1}" text-anchor="middle" '
        f'font-family="monospace" font-size="{MIN_TEXT_LINE_MM}">{alias_text}</text>'
        f'<text x="{Decimal(width_mm) / 2}" y="{text_y2}" text-anchor="middle" '
        f'font-family="monospace" font-size="{MIN_TEXT_LINE_MM}">{mrp_text}</text>'
        f"</svg>"
    )
    return RenderedLabel(svg=svg, symbol_modules=len(bits), module_width_mm=module_width_mm)
