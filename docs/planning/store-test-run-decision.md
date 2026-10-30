# Store test run: coexistence decision (2026-10-20)

**Decision (product owner, in session):** Vaishnavi Singh More is tested in-store
on a **separate synthetic tenant**. The existing legacy software stays the
**only real writer** of sales, stock, invoices and money. This system is used
alongside it for testing only.

## What runs where

| Activity | Real book | Test system |
| --- | --- | --- |
| Customer bills, GST invoices, cash | Legacy software | Test bills, every print marked "TEST – NOT A TAX INVOICE" |
| Store ↔ warehouse transfers | Legacy software | Exercised end to end on the linked warehouse |
| Stock position | Legacy SOH | Re-synced every night (below) |

## Nightly sync, two options

Both run under one P22 journal posting kind and refuse a real tenant
(`TEST_TENANT_ONLY`).

1. **SOH upload, a full replace.** The day's test bills go back on the shelf
   first. The store is then matched to the legacy SOH:
   - fewer pieces leave oldest-first;
   - extra pieces of known items arrive at their recorded cost, accepted on
     the floor;
   - barcodes the store has never held are staged as a reviewed top-up
     opening import (C05).
2. **Sales report, incremental.** Test bills go back, sold pieces leave, and
   legacy returns come back only against an earlier legacy sale.

Guards:
- A test bill is never reversed twice.
- A sales report for days that an applied SOH already covers is refused.
- An SOH older than the last applied one is refused.

## Consequences

- No bill, tax document or GL entry is created by a sync. Test bills stay as
  test history.
- A goods-v1 bill cancel does not restore stock today. The sync therefore
  reverses test bills from each bill's own sale journal. Fixing cancel for
  real stores is separate work.
- Owner: C08 / SO-08 (stock effect), with C05 (top-up opening) and C15
  (reporting).
- Retirement: at real-store cutover (SO-22) the synthetic tenant is closed.
  The surfaces are classified `temporary_supported_legacy` (STORE-TEST-*).
- Open: real-store activation gates (ACT-01, CASH-01/02, DAY-01) are unchanged
  and still block a real cutover.
