# KDPS PT design

What the PT file should be in the KDPS app: its columns, where each value comes from, the setup the owner must do before the first PT, and the changes the app needs. Built from the screen-by-screen walkthrough of the running app (synthetic data) and the backend reviews `00`–`09` in this folder. Revision reviewed: `5a38a1f`, 1 Nov 2026.

## 1. Where the PT sits

Booking → Goods arrived → Count → GRN → Discrepancies → **Prepare PT → Approve PT** → Labels → Accept & put away → sellable stock.

- The PT is made **from the GRN**. It gets exactly one row per item counted *good* and resolved to an item. Typing, pasting or uploading a file only fills those rows; it never adds one.
- Damaged, extra, wrong and unidentified pieces never reach an ordinary PT. Accepted extras go on a separate *supplement* PT.
- The warehouse prepares it; a different person (the Owner) approves it. Approval numbers it (series `RPT`), freezes every row and value, and values the stock.
- Labels print from the frozen BARCODE and MRP. The PT's MRP becomes the selling price and its P RATE the stock cost.
- After approval a PT is never edited. A fix means reverse and reissue.

## 2. Four ways to fill a PT

| Way | When |
|---|---|
| Start from the count | Goods with no PT file: type HSN, MRP, BASIC and the describing values, using the invoice. |
| Upload the brand's own PT file | Brand or vendor sent a PT file. Its columns are mapped to the KDPS columns by approved rules; values no rule settles wait as suggestions. |
| Upload a KDPS PT file | A filled KDPS work sheet (or the canonical layout). |
| Paste from a spreadsheet | Fills SEASON, BARCODE, HSN, QTY, MRP, BASIC, P RATE into the existing rows. |

## 3. Columns

Base: the current KDPS format. Changed only where broken. Not on the PT because the app already holds them: supplier, invoice number, invoice date (arrival); counted, damaged and held quantities (GRN).

**Source key:** *Booking* = from the booking/arrival; *Count* = from the count/GRN; *Item* = from the item master; *Typed*; *Picked* = chosen from a fixed list in the Master Sheet; *Server* = worked out by the app, never typed.

| # | Column | Source | Rule | App today |
|---|---|---|---|---|
| 1 | SEASON | Booking | The booking's season, e.g. SS-26. Must match the booking (R-PT-005). | Not prefilled; not checked against the booking; a closed season was accepted. **Fix.** |
| 2 | BRAND | Booking | From the arrival's brand. | Works. |
| 3 | COLOR | Picked | Real colours only, plus ASSORTED / MULTI. | Grid offers "Quick fill: Premium / Medium / Economy". **Remove; use PRICE TIER.** |
| 4 | GENDER | Picked | 5 values. | Picker works once the list is set up. |
| 5 | SUB CATEGORY | Picked | Fixed list. | Picker; not required at approval. |
| 6 | TYPE | Server | Worked out from ITEM. | Picked by hand today. |
| 7 | ITEM | Picked | Cleaned item list (2/3 PCS SUIT is an ITEM). | Picker; not required at approval. |
| 8 | FIT | Picked | True fits only. | Picker; not required at approval. |
| 9 | SIZE | Item / Count | Same size list as the booking. | Works. |
| 10 | BARCODE | Count | Vendor barcode as scanned; goods with none get a KDPS 7-digit code. | Vendor barcode works. **No code is ever generated**, so untagged goods can't be tagged. **Fix.** |
| 11 | DESIGN | Item | The style code. Must equal the booking's style code so counted goods link to the booking. | Works. |
| 12 | HSN | Typed | 6 or 8 digits. Ideally defaulted from ITEM. | Format checked only at approval. |
| 13 | QTY | Count | Pieces counted good. | Works. |
| 14 | MRP | Typed | From the tag or brand file. Never replaced by the app. | Default mode replaces it silently. **Fix.** |
| 15 | BASIC | Typed | What KDPS pays, from the invoice or brand file. | Works. |
| 16 | P RATE | Server | **BASIC × 1.2, always.** | Works only if rates = 20%; not enforced; code expects × 1.1. **Fix.** |
| 17 | INPUT TAX | Server | From HSN and value (see §6). | One slab by MRP sets both taxes. **Decide.** |
| 18 | OUTPUT TAX | Server | 5% if MRP ≤ ₹2,625, else 18% (GST from 22 Sep 2025). | Works with the right tax setup. |
| 19 | MARGIN | Server | (MRP − P RATE) ÷ MRP × 100. | Correct in "Both supplied" mode. |
| 20 | PRICE TIER | Server | New. Worked out from MRP bands per category; replaces the colour habit. | Not in the app. **Add.** |
| 21 | SLEEVE | Picked | New, optional. | Not in the app. |
| — | NAG | Server | = QTY. Keep only if the old POS needs it. | In the grid and export. **Decide.** |
| — | SUGGESTED SUB CATEGORY | — | Drop: a check on hand-picked values, not needed once lists are fixed. | Hard-coded in the export. **Drop.** |
| — | SUGGESTED TYPE | — | Drop: TYPE comes from ITEM. | Hard-coded in the export. **Drop.** |

Of the 21 columns, 12 fill themselves (2 from the booking, 2 from the count, 2 from the item, 6 worked out by the server), 6 are picked from fixed lists, and only 3 are typed: HSN, MRP and BASIC.

## 4. Setup needed before the first PT

None of this exists in a new company; each was missing in testing and blocked the flow.

1. **Pricing profile** with only the **Both supplied** mode (BASIC and MRP both entered). The other two modes overwrite one of them.
2. **Rates:** transport 20% (gives P RATE = BASIC × 1.2), pricing margin 0.
3. **Tax rates:** one rule per HSN used, effective from 22 Sep 2025: 5% up to MRP ₹2,625, 18% above.
4. **Receipt approval policy** (Owner approves, different person, password again) **and a reversal policy**, per site and brand.
5. **Label profile:** tag size, printer dpi, copies limit.
6. **Barcode range** for KDPS 7-digit codes (e.g. 1000000–1999999).
7. **Master lists** (vocabularies): season, colour, gender, sub category, item, fit, sleeve, size.
8. **Who prepares a store's PT:** the warehouse needs PT duty at each store it serves (open question OQ-28).
9. **Do it before goods arrive.** The PT uses the setup in force at the arrival time; setup made later can't price earlier deliveries.

## 5. Changes the app needs (ranked)

High:
1. **Approver must see the PT.** Show every row with MRP, BASIC, P RATE, tax, margin and the total value; today the panel shows none and "Value band: Unknown" (`goods_pt_services.py:1150`).
2. **Never replace a typed MRP.** Make *Both supplied* the default and only mode for KDPS (`PtPrepare.tsx:194`, `goods_calc.py:227`).
3. **Enforce P RATE = BASIC × 1.2** (confirmed by the owner, 1 Nov 2026). `EXPECTED_TRANSPORT_PCT` is now 20 (`masters/master_sheet_services.py:72`, changed 1 Nov 2026). Still to do: lock the rate, and block per-row mode switches (`allow_row_override` is never read).
4. **Generate KDPS barcodes** for goods without a vendor barcode, before approval (generator exists but no screen calls it).
5. **Require the merchandising columns** (SEASON, COLOR, GENDER, SUB CATEGORY, ITEM, FIT) before submit; the sample was approved with them blank.
6. **Prefill SEASON from the booking and check it.**
7. **No "good" goods without an item.** A scanned code not yet resolved must not be saved as good with no item; 16 pieces got stuck in testing with no warning.
8. **Reversal must work:** ship a reversal approval policy, so a wrong PT can be fixed.
9. **The till must sell each piece at its own PT's MRP.** Today it bills every piece of a barcode and season at the newest PT's MRP (`sell/services/goods_stock.py:315`).

Medium:
10. Tags show item, size and MRP (R-PT-014); copies default to QTY and can't exceed it; goods can't be accepted until their tags are printed.
11. Exports use frozen values only and take their columns from the profile, so NAG and the SUGGESTED columns can be dropped without code changes.
12. Warn about missing setup (profile, approval policy) before the preparer fills the PT, not at submit.
13. Brand files over 8,000 rows are refused (the Madura file); raise the limit.
14. "Send back" should reach the preparer, with a reason.

## 6. Open decisions for the owner

1. **NAG:** does the old POS still need it?
2. **Input tax:** keep the KDPS rule (BASIC ≤ ₹2,500 → 5%) or one slab by MRP for both taxes as the app does today? The CA should confirm for MRP between ₹2,626 and ₹2,950.
3. **PRICE TIER bands** per category.
4. **What the tag shows** besides barcode and MRP.
5. **Who prepares a store's PT** (OQ-28): warehouse with store duty, or a store role.
6. **Optional columns:** FABRIC, PATTERN, NECK.
7. ~~The × 1.1 note in the code vs the × 1.2 rule.~~ **Decided 1 Nov 2026: P RATE = BASIC × 1.2, always.** The "owner's decision, 2026-10-01" × 1.1 note in `masters/master_sheet_services.py:69` (`EXPECTED_TRANSPORT_PCT = 10`) is wrong and must become 20.

## Sources

`docs/review/00-build-audit.md`, `01-02-booking.md`, `03-arrival.md`, `04-count.md`, `05-grn.md`, `06-discrepancies.md`, `07-pt-prepare.md`, `08-pt-approve.md`, `09-labels.md`, `10-accept.md`. Current KDPS format: `docs/data-from-kdps/KDPS PT FILE SHEET.xlsx`; vendor layouts: `docs/data-notes/pt-file-layouts.md`.
