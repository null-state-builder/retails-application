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
  count differences, uploads PT and SOH files, and has no money, setup or
  opening approval.
- **Transfers:** warehouse → store needs **no approval**. Submitting asks for the
  password again and the transfer is approved at once (reason
  `NO_APPROVAL_REQUIRED`, named after the submitter), ready to dispatch. Any
  other transfer (store → warehouse, corrections) waits for K D Singh or Priyo.
- **Openings** need no approval during the alpha: the uploader (Debanjan, the
  warehouse or Priyo) starts the load, and the `preparer` login records each
  step as `NO_APPROVAL_REQUIRED`.

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

Owner's process (28–29 Oct 2026): set up the store empty, open it from its PT
files (no scanning, cost = P RATE), then bring it to the legacy SOH. Synthetic
tenant only. During the alpha an opening needs **no approval**: `setup.json`
`"opening": {"approval": false}` publishes the `pt.approve.opening` policy with
`auto_approve_test_store_opening`, so the upload queues the load and the
`preparer` login records every decision as `NO_APPROVAL_REQUIRED` (no step-up).
Republishing the policy without the flag turns approvals back on.

1. **Normalise the PT files (on a laptop, before any upload):** the store's PT
   files (bill workbooks, no Season column) and its SOH go through
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
2. **Rehearse locally first** (Docker, never the server; `deploy/README.md`):

   ```bash
   scripts/rehearse_store_opening.sh --twice
   ```

   It builds the server's stack from empty, opens the store from the combined
   PT (restarting the API mid-load), syncs the SOH and checks the totals, twice.
   Go on only when it ends `REHEARSAL PASSED` on the revision you will deploy.
3. **On the server** (`cd ~/kdps/deploy`; each step needs the owner's go-ahead):
   1. **Back up** and copy the dump off the server:
      `docker compose exec -T db pg_dump -U kdps -Fc kdps > ~/backups/kdps-before-opening-$(date +%F).dump`
   2. **Deploy:** `cd ~/kdps && git pull`, then `cd deploy && docker compose up -d --build`.
      The API migrates on start; on a fresh database it shows `unhealthy` for
      about 6 minutes. That is expected.
   3. **Restore the pre-load dump** (the store and people, no stock), then
      bring it to this revision's migrations:
      ```bash
      docker compose stop api
      docker compose exec -T db dropdb -U kdps --force kdps
      docker compose exec -T db createdb -U kdps kdps
      docker compose exec -T db pg_restore -U kdps -d kdps --no-owner < /home/kdps/backups/kdps-before-pt-load-2026-10-28.dump
      docker compose run --rm --no-deps api python manage.py migrate --noinput
      ```
   4. **Publish the no-approval flag:** re-run `load_test_store --setup` with
      the API stopped, exactly as step 5 of the rebuild above, which ends by
      starting the API. Existing logins keep their passwords.
   5. **Open the store** (about 10–12 minutes) with the combined PT, as
      Debanjan. The goods worker runs it as a durable job, so a restart or a
      deploy mid-load only pauses it; `--wait` follows it and prints the totals:
      ```bash
      docker compose cp ~/WARDROBE_PT_combined.xlsx api:/tmp/WARDROBE_PT_combined.xlsx
      docker compose exec -T api python manage.py open_test_store --store WRR \
        --pt /tmp/WARDROBE_PT_combined.xlsx --uploader debanjan@wardrobe.test --wait
      ```
      Expect `Done: 15765 pieces on the floor at WRR (6331 barcodes, 0 left out)`.
      The same file again is the same source and queues nothing. Without
      `--pt` it resumes the store's open PT source.
   6. **Sync the legacy SOH:** export a fresh SOH from the legacy software
      **after** step 5 finished, and apply it with its real export time:
      ```bash
      EXPORTED_AT=2026-10-29T18:30+05:30   # the legacy export's real time, with its zone
      docker compose cp ~/SOH.xlsx api:/tmp/SOH.xlsx
      docker compose exec -T api python manage.py sync_soh --store WRR --soh /tmp/SOH.xlsx \
        --exported-at "$EXPORTED_AT" --uploader debanjan@wardrobe.test --apply
      ```
      Stock is brought to the SOH (legacy is master). An export at or before
      the last applied one is refused (`STOCK_SYNC_STALE`); the same file and
      time again is a no-op.
   7. **Review** the printed totals and the flagged rows with Debanjan: book
      units equal the SOH total. Every difference is one row in one
      `stock_sync_review` alert ("SOH sync needs review") for the store manager
      at `/goods/stock-sync`, by category:
      - `PT_ONLY`: in the PT, not in the SOH (sold or moved in legacy).
      - `SOH_BELOW` / `SOH_ABOVE`: the SOH has fewer / more pieces.
      - `SOH_ONLY`: in the SOH, in no PT. Flagged and **not sellable**; no item
        or stock is created. It becomes real when a PT containing it is opened.
      - `MRP_DIFFERS`: the SOH's MRP differs from the book's.

      **Acknowledge** only marks a row seen; it changes no stock or item.
      Sales made in legacy between the opening and the export show as
      `SOH_BELOW`; that is expected. Then go or no-go, and **sell** at `/sell`.

**In the browser** the same steps run as Debanjan (or the warehouse, or Priyo):
upload `WARDROBE_PT_combined.xlsx` at `/goods/opening` under "Opening from PT
files" (at most 40 files; the one combined file replaces them) and the load
starts by itself, with progress on the card; then upload the SOH with its
export time at `/goods/stock-sync`. A later PT opens only barcodes the store
does not have yet.

Local rehearsal (`scripts/rehearse_store_opening.sh`), with the combined PT and
`SOH_WARDROBE_28-10-2026.xlsx`:

| Step | Result |
| --- | --- |
| PT | 6,331 barcodes, 15,765 pieces, none left out |
| SOH | 13,678 pieces |
| Flagged (barcodes / pieces) | `PT_ONLY` 1,052 / 1,317; `SOH_BELOW` 329 / 772; `SOH_ABOVE` 1 / 2; `SOH_ONLY` 0; `MRP_DIFFERS` 1 |
| Stock after the sync | 13,678 pieces, the SOH's own total |

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
    export time, review the preview, then choose **Apply**.
  - **Or:** upload the day's "Sale report".
  - Review and acknowledge the flagged rows ("SOH sync needs review"). SOH-only
    barcodes stay flagged and unsellable until a PT containing them is opened.
  - Each SOH must be exported after the one applied before it
    (`STOCK_SYNC_STALE`); test bills since that one are put back first.
  - Any transfer still in transit at night is not in the legacy SOH. Finish
    it or expect it to show as an exception.

## Weekly

- Upload a full SOH even on weeks you used sales reports. It catches receipts
  and transfers made in the legacy system.

## Reset

- **Reset = restore a `pg_dump`** (as in step 3.3 of "Opening The Wardrobe").
  There is no per-store delete: documents, versions, stock postings and
  approval evidence are append-only. Take a dump before every step you may
  want to undo, and copy it off the server.
- Stock that has drifted from legacy: sync a fresh SOH, not a reset.
- Everything else: rebuild the synthetic tenant ("Test server (VPS)" above).

The sync never touches a real tenant.
