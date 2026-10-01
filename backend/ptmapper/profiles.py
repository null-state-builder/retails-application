"""Mapping profiles — engine config per brand-file *archetype* (not per brand).

A profile says: how to recognise the file, which sheet/header to read, and which
source column feeds each KDPS field. The generic ALIASES below cover the ~60-70%
"plain rename" fields for any file; a profile only overrides the parts an
archetype does differently (e.g. Ginesys hides colour/size inside CATEGORY3/4).
"""

from __future__ import annotations

from typing import Any

# KDPS target Work Sheet columns, in order.
KDPS_COLUMNS = [
    "SEASON",
    "BRAND",
    "COLOR",
    "GENDER",
    "SUB CATEGORY",
    "TYPE",
    "ITEM",
    "FIT",
    "SIZE",
    "BARCODE",
    "DESIGN",
    "HSN",
    "QTY",
    "MRP",
    "BASIC",
    "P RATE",
    "INPUT TAX",
    "OUTPUT TAX",
    "NAG",
    "MARGIN",
    "SUGGESTED SUB CATEGORY",
    "SUGGESTED TYPE",
]

# KDPS controlled column → its vocabulary dimension (validated against Master Sheet).
CONTROLLED = {
    "SEASON": "season",
    "BRAND": "brand",
    "COLOR": "color",
    "GENDER": "gender",
    "SUB CATEGORY": "sub_category",
    "TYPE": "type",
    "ITEM": "item",
    "FIT": "fit",
    "SIZE": "size",
}

# Generic role → candidate source header names (normalised UPPER). First hit wins.
ALIASES: dict[str, list[str]] = {
    "BARCODE": [
        "BARCODE",
        "BAR CODE",
        "EAN",
        "EAN NO",
        "EANCODE",
        "EAN/UPC",
        "EAN CODE",
        "SUPPLIER BARCODE",
        "STOCK NO",
        "STOCK NO.",
        "EN CODE",
        "SKU NO",
        "BR-CODE",
        "BARCODE NO",
        "ADDITIONAL ITEM CODE",
        "STOCKNO",
    ],
    "DESIGN": [
        "STYLE",
        "STYLE CODE",
        "DESIGN",
        "STYLE NAME",
        "G ARTICLE",
        "ITEM NAME",
        "PRODUCT",
        "ITEM DESCRIPTION",
        "ITEM CODE",
    ],
    "BRAND_SRC": ["BRAND", "BRAND NAME", "FIRM", "OWNER SITE", "CATEGORY1", "COMPANY NAME"],
    "COLOR_SRC": ["COLOR", "COLOUR", "SHADE", "SHADE NAME", "ITEM GROUP"],
    "SIZE_SRC": ["SIZE", "SIZES", "AGE GROUP", "SIZE 1"],
    "QTY": [
        "QTY",
        "QUANTITY",
        "SALES QTY",
        "INVOICE QTY",
        "INVOICE_QUANTITY",
        "TOTAL QTY",
        "BILLED QUANTITY",
        "INVOICE QUANTITY",
    ],
    "MRP": [
        "MRP",
        "M.R.P",
        "M.R.P.",
        "UNIT MRP",
        "RETAIL PRICE",
        "RETAIL RATE",
        "TOTAL VALUE (MRP)",
        "MRP/",
        "PRICE",
    ],
    "PRATE_SRC": [
        "ITEM RATE",
        "INVOICE_RATE",
        "INVOICE RATE",
        "NET RATE",
        "RATE/UNIT",
        "COST PER UNIT",
        "NET UNIT COST",
        "DOC RATE",
        "RATE",
        "DP",
        "PURCHASE PRICE",
    ],
    "BASIC_SRC": [
        "ITEM BASE VALUE",
        "TAXABLE VALUE",
        "TAXABLE_AMOUNT",
        "BASIC AMOUNT",
        "COST",
        "MDP",
        "UNIT COST",
        "COST PRICE",
        "PURCHASE PRICE",
    ],
    "HSN": ["HSN", "HSN CODE", "HSNCODE", "HSN/SAC", "HSN_CODE"],
    "TAX_SRC": ["TAX PERCENTAGE", "GST %", "TAX_RATE", "TAX RATE", "GST RATE", "TAX"],
    "SEASON_SRC": ["SEASON", "ARTICLE SEASON", "SPSN"],
    "DATE_SRC": [
        "INVOICE DATE",
        "INVOICE_DATE",
        "VOUCHER DATE",
        "BILL DATE",
        "INV DATE",
        "DATE",
        "PDT",
        "BILLING DATE",
    ],
    "GENDER_SRC": ["GENDER", "GENDER + BODY", "SEX"],
    "FIT_SRC": ["FIT", "FIT TYPE", "FITTING", "FIT NAME"],
    # description used for taxonomy matching (concatenated, in order)
    "DESC_SRC": [
        "ITEM DESCRIPTION",
        "PRODUCT",
        "ITEM DESC",
        "DESCRIPTION",
        "DESCRIPTION OF GOODS",
        "CATEGORY",
        "CATEGORY NAME",
        "ITEM NAME",
        "CATEGORY5",
        "DEPARTMENT",
        "SECTION",
    ],
}

# Header keywords used to score / locate the header row in a messy sheet.
HEADER_KEYWORDS = {
    "barcode",
    "bar code",
    "ean",
    "ean no",
    "eancode",
    "ean/upc",
    "size",
    "sizes",
    "mrp",
    "m.r.p",
    "m.r.p.",
    "qty",
    "quantity",
    "sales qty",
    "hsn",
    "hsn code",
    "style",
    "rate",
    "color",
    "colour",
    "shade",
    "brand",
    "item",
    "design",
    "season",
    "cost",
    "tax",
    "product",
    "price",
    "retail",
    "stock no",
}


#: KDPS's own PT work sheet (the "<name> Work Sheet" tabs of the KDPS PT file): every
#: KDPS column is already there, so ITEM, SUB CATEGORY and TYPE are read from their own
#: cells rather than found in a description. The Master Sheet tab beside it is skipped.
WORK_SHEET_PROFILE = "kdps_work_sheet"
WORK_SHEET_HEADERS = ["SEASON", "SUB CATEGORY", "ITEM", "BARCODE", "BASIC", "P RATE", "NAG"]
MASTER_SHEET_NAME = "MASTER SHEET"

PROFILES: list[dict[str, Any]] = [
    {
        "code": WORK_SHEET_PROFILE,
        "name": "KDPS PT work sheet",
        "archetype": "K",
        "match": {"header_has": WORK_SHEET_HEADERS},
        "overrides": {
            "BARCODE": "BARCODE",
            "DESIGN": "DESIGN",
            "BRAND_SRC": "BRAND",
            "COLOR_SRC": "COLOR",
            "SIZE_SRC": "SIZE",
            "QTY": "QTY",
            "MRP": "MRP",
            "PRATE_SRC": "P RATE",
            "BASIC_SRC": "BASIC",
            "HSN": "HSN",
            "TAX_SRC": "INPUT TAX",
            "SEASON_SRC": "SEASON",
            "GENDER_SRC": "GENDER",
            "FIT_SRC": "FIT",
            "ITEM_SRC": "ITEM",
            "SUBCAT_SRC": "SUB CATEGORY",
            "TYPE_SRC": "TYPE",
            "DESC_SRC": ["ITEM", "DESIGN"],
        },
        "flags": {"explicit_attributes": True},
    },
    {
        "code": "ginesys_pt_email",
        "name": "Ginesys PT EMAIL (ABFRL distributor)",
        "archetype": "C",
        "match": {
            "sheet_contains": "PT EMAIL",
            "header_has": ["CATEGORY1", "CATEGORY2", "CATEGORY3", "CATEGORY4"],
        },
        "overrides": {
            "BARCODE": "BARCODE",
            "DESIGN": "CATEGORY2",
            "BRAND_SRC": "CATEGORY1",
            "COLOR_SRC": "CATEGORY3",
            "SIZE_SRC": "CATEGORY4",
            "QTY": "INVOICE_QUANTITY",
            "MRP": "MRP",
            "PRATE_SRC": "INVOICE_RATE",
            "TAX_SRC": "TAX_RATE",
            "HSN": "HSN CODE",
            "DATE_SRC": "INVOICE_DATE",
            "DESC_SRC": ["CATEGORY5", "DEPARTMENT", "SECTION", "DIVISION", "CATEGORY2"],
        },
        "flags": {
            "basic_from_taxable_per_unit": True,  # BASIC = TAXABLE_AMOUNT / QTY
            "color_strip_code_prefix": True,  # CATEGORY3 colour is "<code>-NAME", e.g. "16-BLUE"
        },
    },
    {
        "code": "ginesys_billwise",
        "name": "Ginesys PT (bill-no-wise variant)",
        "archetype": "C",
        "match": {
            "sheet_contains": "PT FILE",
            "header_has": ["INVOICE_RATE", "INVOICE_QUANTITY", "RSP", "DEPARTMENT"],
        },
        "overrides": {
            "BARCODE": "BARCODE",
            "DESIGN": "STYLE",
            "SIZE_SRC": "SIZE",
            "QTY": "INVOICE_QUANTITY",
            "MRP": "MRP",
            "PRATE_SRC": "INVOICE_RATE",
            "HSN": "HSN_CODE",
            "DATE_SRC": "INVOICE_DATE",
            "DESC_SRC": ["DEPARTMENT", "SECTION", "STYLE"],
        },
    },
    {
        "code": "tally_voucher",
        "name": "Tally / Vistaar sales voucher",
        "archetype": "B",
        "match": {
            "header_has": ["STOCK NO", "PRODUCT", "BRAND", "STYLE", "SHADE", "SALES QTY"],
        },
        "overrides": {
            "BARCODE": "STOCK NO",
            "DESIGN": "STYLE",
            "BRAND_SRC": "BRAND",
            "COLOR_SRC": "SHADE",
            "SIZE_SRC": "SIZE",
            "QTY": "SALES QTY",
            "MRP": "RETAIL PRICE",
            "PRATE_SRC": "ITEM RATE",
            "BASIC_SRC": "ITEM BASE VALUE",
            "DATE_SRC": "VOUCHER DATE",
            "DESC_SRC": ["PRODUCT", "ITEM DESCRIPTION"],
        },
    },
    {
        "code": "jockey_sap",
        "name": "Jockey / Page SAP export",
        "archetype": "D",
        "match": {
            "header_has": ["STOCKNO", "COLOUR CODE", "DOC RATE", "MDP"],
        },
        "brand_const": "JOCKEY",  # these exports carry no brand column
        "overrides": {
            "BARCODE": "STOCKNO",
            "DESIGN": "STYLE CODE",
            "SIZE_SRC": "SIZE",
            "QTY": "QTY",
            "PRATE_SRC": "DOC RATE",
            "BASIC_SRC": "COST",
            "TAX_SRC": "TAX PERC.",
            "HSN": "HSN CODE",
            "DATE_SRC": "BILL DATE",
            "DESC_SRC": ["ITEM DESCRIPTION", "STYLE CODE"],
            # COLOUR CODE is a numeric code (not a shade) → intentionally not mapped.
        },
    },
    {
        "code": "peter_england",
        "name": "Peter England / ABFRL wide export",
        "archetype": "E",
        "match": {
            "header_has": ["EAN NO", "FIT TYPE", "NET UNIT COST"],
        },
        "overrides": {
            "BARCODE": "EAN NO",
            "DESIGN": "MATERIAL",
            "BRAND_SRC": "BRAND",
            "COLOR_SRC": "COLOR",
            "SIZE_SRC": "SIZE",
            "FIT_SRC": "FIT TYPE",
            "QTY": "QUANTITY",
            "MRP": "MRP",
            "PRATE_SRC": "UNIT COST",
            "BASIC_SRC": "NET UNIT COST",
            "HSN": "HSN CODE",
            "DATE_SRC": "INV DATE",
            "DESC_SRC": ["PRODUCT TYPE", "CATEGORY", "PRODUCT", "MATERIAL DESCRIPTION"],
        },
        "flags": {
            # FIT TYPE is coded ("PJ RG OCTANEMIDSTR") — token 2 = fit family (RG/SL).
            "fit_code_tokens": True,
        },
    },
    {
        "code": "blackberry",
        "name": "Blackberrys invoice export",
        "archetype": "E",
        "match": {
            "header_has": ["EANCODE", "ARTICLE SEASON", "G ARTICLE"],
        },
        "overrides": {
            "BARCODE": "EANCODE",
            "DESIGN": "STYLE",
            "BRAND_SRC": "BRAND NAME",
            "COLOR_SRC": "COLOUR",
            "SIZE_SRC": "SIZES",
            "FIT_SRC": "FIT",
            "QTY": "INVOICE QTY",
            "MRP": "UNIT MRP",
            "PRATE_SRC": "COST PER UNIT",
            "BASIC_SRC": "BASIC AMOUNT",
            "HSN": "HSN CODE",
            "DATE_SRC": "INVOICE DATE",
            "SEASON_SRC": "ARTICLE SEASON",
            "DESC_SRC": ["CATEGORY", "GROUP CATEGORY", "ITEM DESCRIPTION", "STYLE"],
        },
    },
    {
        "code": "madura_sap",
        "name": "Madura Fashion SAP billing (.xlsb)",
        "archetype": "E",
        "match": {
            "header_has": ["EAN/UPC", "GENERIC MATERIAL", "BILLED QUANTITY"],
        },
        "overrides": {
            "BARCODE": "EAN/UPC",
            "DESIGN": "GENERIC MATERIAL",
            "SIZE_SRC": "SIZE 1",
            "QTY": "BILLED QUANTITY",
            "MRP": "MRP",
            "HSN": "HSN CODE",
            "DATE_SRC": "BILLING DATE",
            "DESC_SRC": ["GENERIC MATERIAL", "MATERIAL GRP"],
            # BRAND (AS/VH/LP in one file) and COLOR (price tier) need column rules
            # confirmed with KDPS → left for the review queue until then.
        },
    },
    {
        "code": "printed_invoice_pack",
        "name": "Printed invoice (pack/size column)",
        "archetype": "F",
        "match": {
            "header_has": ["ITEM CODE", "PACK / SIZE", "SALE RATE"],
        },
        "overrides": {
            "BARCODE": "ITEM CODE",
            "DESIGN": "ITEM NAME",
            "SIZE_SRC": "PACK / SIZE",
            "QTY": "TOTAL QTY",
            "MRP": "M.R.P.",
            "PRATE_SRC": "SALE RATE",
            "HSN": "HSN CODE",
            "DESC_SRC": ["ITEM NAME"],
        },
    },
]

GENERIC_PROFILE: dict[str, Any] = {
    "code": "generic",
    "name": "Generic (header-name auto-map)",
    "archetype": "A",
    "match": {},
    "overrides": {},
    "flags": {},
}

# Filename keyword → profile code (fast path before fingerprinting).
FILENAME_HINTS = {
    "BEEVEE": "ginesys_pt_email",
    "MUFTI": "ginesys_pt_email",
    "GO COLOURS": "ginesys_pt_email",
    "GO COLORS": "ginesys_pt_email",
    "HYPHEN": "ginesys_billwise",
    "PETER ENGLAND": "peter_england",
    "BLACKBERRY": "blackberry",
    "MADURA": "madura_sap",
    # NOTE: no blanket "JOCKEY" hint — JOCKEY.xlsx is a hand-made simple sheet that
    # maps fine generically; the SAP exports are caught by header fingerprint.
}
