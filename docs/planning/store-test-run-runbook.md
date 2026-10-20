# Store test run runbook: Vaishnavi Singh More + linked warehouse

The legacy software is the real system. This one runs alongside it on a
**synthetic test tenant** (see `store-test-run-decision.md`).

## One-time setup

1. **Tenant:** `python manage.py bootstrap_deployment --synthetic` on the test
   deployment. Load masters (`load_real_masters`).
2. **Sites** (`/setup/organisation`):
   - Create the warehouse in the store's legal entity.
   - Add a floor location to both sites.
   - On the store, choose **Linked warehouse**.
3. **People** (`/setup/people-access`):
   - store persons for the store, with all brands, for billing;
   - at least **two** warehouse managers, because whoever drafts a transfer
     cannot approve it.
4. **Configuration** (`/setup/configuration`), each approved by a second
   person:
   - series GRN, CGRN, RPT, **TPT**;
   - an approval policy for `pt.approve.transfer` with roles including
     `warehouse_manager`, covering both sites;
   - sell policy, business profile, working calendar.
5. **Tax and numbering:**
   - `/setup/tax-settings`;
   - `/setup/feature-switches`: switch on `tax-settings`, `sales-report` and
     `transfer-documents`;
   - `/setup/document-numbering`.
6. **Opening stock** (`/goods/opening`): upload the first legacy SOH, then
   review, approve and accept it.
7. **Counter** (`/setup/first-store`): register the counter, pair the
   browser, and approve selling.
8. **Check everything:**
   ```bash
   python manage.py setup_store_test_run --store <STORE> --warehouse <WH> --manager a@x --manager b@x
   ```
   This assigns the warehouse-manager role to both sites. It then lists every
   gate still missing, with the screen that fixes it, and exits with status 0
   only when everything is ready.

## Each day

- **Morning:** open `/sell` on the paired counter. Check the "Test
  environment" badge.
- **Day:**
  - Bill test sales. Receipts print "TEST – NOT A TAX INVOICE".
  - Try transfers between the warehouse and the store: draft (manager A),
    approve (manager B), dispatch, count and accept at the store.
- **Night** (`/goods/stock-sync`), once the legacy day is closed:
  - **Preferred:** export the legacy SOH and upload it as "SOH". Enter the
    export time, review the preview, then choose **Apply**.
  - **Or:** upload the day's "Sale report".
  - Review the exceptions. New items appear in `/goods/opening` as a top-up
    for review.
  - Any transfer still in transit at night is not in the legacy SOH. Finish
    it or expect it to show as an exception.

## Weekly

- Upload a full SOH even on weeks you used sales reports. It catches receipts
  and transfers made in the legacy system.

## Reset

- Stock: upload a fresh SOH.
- Everything else: re-create the synthetic tenant.

The sync never touches a real tenant.
