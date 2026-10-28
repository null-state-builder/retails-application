# Store test run runbook: The Wardrobe, Ratu Road + its warehouse

The legacy software is the real system. This one runs alongside it on a
**synthetic test tenant** (see `store-test-run-decision.md`). One store is
functional: **The Wardrobe** (`WRR`, Ratu Road, Ranchi), with its linked
warehouse **Warehouse Ranchi** (`RNC-WH`). Store, company and people come from
[`docs/wardrobe-ratu-road/setup.json`](../wardrobe-ratu-road/setup.json).

## People

| Person | Login (`@wardrobe.test`) | Role | Sites |
| --- | --- | --- | --- |
| K D Singh | `kdsingh` | owner + IT admin (bootstrap admin) | all |
| Debanjan (store manager) | `debanjan` | store person; warehouse | store; warehouse |
| Cashier 1, Cashier 2 | `cashier1`, `cashier2` | store person | store |
| Priyo | `priyo` | operations head | all |
| Tanmay, Mahender | `tanmay`, `mahender` | warehouse | warehouse |
| (system) | `preparer` | warehouse + IT admin | prepares openings; no sign-in chip |

- Debanjan and both cashiers are the store's salespeople (picked on each bill).
- **Operations head** sees home, sell, stock and reports, approves transfers and
  count differences, and has no money, setup or opening approval.
- **Transfers:** warehouse → store needs **no approval**. Submitting asks for the
  password again and the transfer is approved at once (reason
  `NO_APPROVAL_REQUIRED`, named after the submitter), ready to dispatch. Any
  other transfer (store → warehouse, corrections) waits for K D Singh or Priyo.
- An opening needs three different people: the uploader (Debanjan), the
  preparer (`preparer`, used by the server) and the approver (K D Singh).

## Test server (VPS): rebuild for The Wardrobe

The test runs on `https://kdps-pos.vercel.app` (Vercel), whose `/api` goes to
the VPS (`deploy/README.md`). The rebuild replaces the earlier Vaishnavi test
data with a fresh DB that holds only The Wardrobe and its warehouse. Vercel
needs no change.

1. **Back up** and copy the dump off the server before going further:
   ```bash
   cd ~/kdps/deploy
   mkdir -p ~/backups
   docker compose exec -T db pg_dump -U kdps -Fc kdps > ~/backups/kdps-before-wardrobe-$(date +%F).dump
   ```
2. **Update the code:** `cd ~/kdps && git pull`.
3. **Start empty** (irreversible; only once the dump is safe elsewhere):
   ```bash
   cd ~/kdps/deploy
   docker compose down
   docker volume rm kdps_pgdata kdps_appdata
   docker compose up -d --build        # the API migrates the empty DB
   ```
4. **Tenant and owner** (choose K D Singh's password; it is not written anywhere
   by the server):
   ```bash
   read -s KDPS_BOOTSTRAP_ADMIN_PASSWORD; export KDPS_BOOTSTRAP_ADMIN_PASSWORD
   docker compose exec -T -e KDPS_BOOTSTRAP_ADMIN_PASSWORD api python manage.py bootstrap_deployment \
     --synthetic --code KDPS-TEST --name "The Wardrobe (test)" \
     --admin-email kdsingh@wardrobe.test --admin-name "K D Singh" --admin-staff-code OWN-01
   ```
5. **Store, warehouse and people** (about a minute; the working calendar,
   approval policies, counter and salespeople are included):
   ```bash
   docker compose stop api
   mkdir -p ~/kdps-secrets
   docker compose run --rm --no-deps -e KDPS_BOOTSTRAP_ADMIN_PASSWORD \
     -v ~/kdps/docs/wardrobe-ratu-road/setup.json:/setup/setup.json:ro -v ~/kdps-secrets:/out \
     api python manage.py load_test_store --setup /setup/setup.json --password-file /out/test-logins.txt
   docker compose start api
   ```
   Every new login gets its own random password, written only to
   `~/kdps-secrets/test-logins.txt` (mode 600; `sudo chown $USER` it if the
   container wrote it as root). `Synthetic@123` is not used on the server.
   Hand each person their own password.
6. **No sign-in chips on the server** (owner decision, 28 Oct 2026): delete any
   `KDPS_DEMO_LOGINS` line from `deploy/.env`. The deployed API ignores it
   anyway, and the deployed site shows no chips; everyone types their email and
   password.
7. **Switches** (`/setup/feature-switches`, as K D Singh): `tax-settings`,
   `sales-report` and `transfer-documents`.
8. **Pair the counter** at `/setup/first-store` on the shop's browser.
9. **Open the store:** follow "Opening The Wardrobe" below.

To restore the old data instead: `docker compose down`, remove the volumes,
`up -d`, then `pg_restore -U kdps -d kdps --clean` the dump through
`docker compose exec -T db`.

## Opening The Wardrobe

Owner's process (28 Oct 2026): set up the store empty, open it from its PT files
(no scanning, cost = P RATE), then bring it to the legacy SOH. Synthetic tenant
only.

1. **Normalise the PT files (on a laptop, before any upload):** the store's PT
   files (152 bill workbooks, no Season column) and its SOH go through
   `scripts/normalize_pt_files.py`, which writes one upload-ready workbook and a
   report of every change:

   ```bash
   backend/.venv/bin/python scripts/normalize_pt_files.py --pt "docs/wardrobe-ratu-road/PT FILE" \
     --soh docs/wardrobe-ratu-road/SOH/SOH_WARDROBE_28-10-2026.xlsx --out docs/wardrobe-ratu-road/normalized
   ```

   - A bill already read from another file is dropped (`lp1522.xlsx` repeats
     bill S-1522).
   - Item, brand, size, design, category, gender, fit, season and MRP come from
     the SOH; quantity, P RATE (the latest bill's), HSN and tax from the PT.
     Brands are not merged. An SOH MRP below cost keeps the PT's MRP.
   - Seasons are two a year, `SS-YY` and `AW-YY` (owner decision, 28 Oct 2026;
     `backend/ptmapper/legacy_season.py`): `AUTUMN WINTER(Oct-26)` → AW-26, an
     autumn-winter label dated January–June → the winter before
     (`AUTUMN WINTER(Jan-26)` → AW-25), `SS25`/`SS-25` → SS-25, a June date →
     SS-25. Blank, `CORE` and `ALLSEASON` go to the unknown historical season.
   - An HSN that is not 4, 6 or 8 digits takes the one most used by the same
     brand and item.
   - The script ends by reading its output with the upload's own reader; it
     exits non-zero if any row would be left out. Re-run it whenever PT files
     are added. The PT, SOH and normalised folders are git-ignored (cost data).
2. **Upload the PT file (Debanjan):** at `/goods/opening`, pick The Wardrobe
   and, under "Opening from PT files", choose `WARDROBE_PT_combined.xlsx` (the
   upload takes at most 40 files; the one combined file replaces them). The
   preview shows barcodes, pieces, value at MRP and any rows that would be left
   out. A store person does not see the P RATE value.
3. **Approve and load (K D Singh):** same screen, **Approve and load**. Brands
   and sizes are mapped by their spelling; seasons go through the same SS-YY /
   AW-YY rule, so the seasons are AW-26, AW-25, SS-26, SS-25... and unknown
   historical. Each row's HSN and P RATE are used. Everything goes onto the
   Floor. The card shows progress.
4. **Match the legacy SOH (Debanjan):** export the SOH from the legacy software
   **after** step 3 finished, and upload it as exported (its seasons go
   through the same rule), then upload it at `/goods/stock-sync` with that
   export time. Sold pieces leave, extra pieces of known items are added, and
   barcodes with no PT are staged as new items.
   - An SOH exported before the PT load is refused (`SOH_BEFORE_STOCK`): it
     cannot see those items, and would bring them in a second time.
   - A wrong export time: **Discard** the preview and upload the same file
     again with the right time.
5. **Load the new items (K D Singh):** **Approve and load** on "New items from
   the SOH sync" at `/goods/opening` (cost = SOH Rate). Stock then equals the
   SOH.
6. **Sell** at `/sell` (Cashier 1 or 2), then follow "Each day" below.

Local rehearsal, 28 Oct 2026, with the real files (152 PT files normalised into
one, and `SOH_WARDROBE_28-10-2026.xlsx` uploaded as exported):

| Step | Result |
| --- | --- |
| Normalise | 6,671 lines, 6,311 barcodes, 15,734 pieces; `lp1522.xlsx` dropped (37 lines); 6 HSN, 12 MRP and 11 P RATE fixes; reader self-check: 0 problems, 0 conflicts |
| PT preview | 6,311 barcodes, 15,734 pieces, none left out; MRP ₹2,52,92,047, P RATE ₹1,57,55,304.17 |
| PT load | about 8 minutes, two batches; 15,734 pieces on the floor |
| Seasons | AW-26 10,956, AW-25 2,440, SS-26 1,794, SS-25 302, AW-23 8, AW-24 7, SS-24 4, unknown historical 223 pieces |
| SOH sync | 2,089 pieces out, 2 in; 20 new barcodes (31 pieces) staged |
| Top-up load | 31 pieces of 20 barcodes |
| Stock after | 13,678 pieces of 5,279 barcodes, the SOH's own total |
| One test sale | 8909342558205 at ₹3,199 (the SOH MRP; the PT said ₹2,999); 13,677 pieces |

Run the same rehearsal locally, on a throwaway DB (never `kdps_local`):

```bash
export KDPS_REHEARSAL_DB=kdps_rehearsal_wardrobe   # in backend/, with the local Postgres up
.venv/bin/python manage.py migrate
.venv/bin/python manage.py bootstrap_deployment --synthetic --code KDPS-TEST --name "The Wardrobe (test)" \
  --admin-email kdsingh@wardrobe.test --admin-name "K D Singh" --admin-staff-code OWN-01   # with KDPS_BOOTSTRAP_ADMIN_PASSWORD set
.venv/bin/python manage.py load_test_store --setup ../docs/wardrobe-ratu-road/setup.json
```

Locally every login uses `Synthetic@123`. The sign-in chips show only on the
local dev server; for The Wardrobe's, put this in `backend/.env` (the chips of
a login that is not in the running database are left out):

```bash
KDPS_DEMO_LOGINS=[{"label":"Owner","email":"kdsingh@wardrobe.test","password":"Synthetic@123"}, ...]
```

## Quick start from a legacy SOH (Vaishnavi, local only)

The earlier test store. On a synthetic tenant, this builds the store `VAS-SGMR` and its linked
warehouse `VAS-WH`, creates the people, and loads the legacy SOH through the
reviewed opening path:

```bash
python manage.py load_test_store --soh ../docs/VAS-SGMR_SOH_28-09-2026.xlsx
```

Logins all use `Synthetic@123`:

| Login | Role |
| --- | --- |
| `vas.store` | store person |
| `vas.cashier` | cashier |
| `vas.warehouse` | warehouse operator |
| `vas.preparer` | opening preparer |
| `vas.whm1`, `vas.whm2` | warehouse managers |

Test-only choices:
- Sizes and seasons are taken as the file spells them.
- HSN comes from the category (the file has none).
- Verification uses the legacy quantity, not a count.
- 9 rows with bad cost or MRP are excluded.

It is safe to re-run. Stop the goods worker while it loads, because the large
opening documents conflict with it.

## One-time setup (by hand)

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
  - Try transfers: warehouse → store is drafted and submitted at the
    warehouse (Tanmay or Mahender) with no approval, dispatched, then counted
    and accepted at the store (Debanjan). A store → warehouse return waits for
    K D Singh or Priyo to approve.
- **Night** (`/goods/stock-sync`), once the legacy day is closed:
  - **Preferred:** export the legacy SOH and upload it as "SOH". Enter the
    export time, review the preview, then choose **Apply**. New items it stages
    need K D Singh's **Approve and load** at `/goods/opening`.
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
