# Store test run runbook: Vaishnavi Singh More + linked warehouse

The legacy software is the real system. This one runs alongside it on a
**synthetic test tenant** (see `store-test-run-decision.md`).

## Test server (VPS)

The test runs on `https://kdps-pos.vercel.app` (Vercel), whose `/api` goes to
the VPS (`deploy/README.md`). The VPS holds one **synthetic** tenant,
`KDPS-TEST`, built once with:

```bash
cd ~/kdps/deploy
docker compose exec -T -e KDPS_BOOTSTRAP_ADMIN_PASSWORD=... api python manage.py bootstrap_deployment \
  --synthetic --code KDPS-TEST --name "KDPS Lifestyle (test)" \
  --admin-email syn.owner@synthetic.demo --admin-name "Synthetic Company Owner" --admin-staff-code OWN-01
# then: seed_working_calendar(tenant, owner) in `manage.py shell` (the loader needs a calendar)
docker compose stop api
docker compose run --rm --no-deps -v ~/soh:/soh api python manage.py load_test_store --soh /soh/<file>.xlsx
docker compose start api
```

The bootstrap admin must be `syn.owner@synthetic.demo`: the loader approves as
that owner. After the load, the feature switches `tax-settings`,
`sales-report` and `transfer-documents`, a tax-settings version and the
warehouse operator's staff placement were made through the API as the owner.

Every login has its own password, kept only in `~kdps/test-logins.txt` (mode
600) on the server; `Synthetic@123` is not used there.

The sign-in page's demo chips sign straight in as these logins (one tap, no
password typing). Their passwords come from `KDPS_DEMO_LOGINS` in
`deploy/.env`, built from `test-logins.txt`, so anyone who opens the site can
sign in to the test store. After rotating passwords, rebuild that line and run
`docker compose up -d api`, or the chips fail to sign in.

| Login (`@synthetic.demo`) | Role | Used for |
| --- | --- | --- |
| `syn.owner` | owner + IT admin | setup and every approval (SOH, opening top-ups, transfers) |
| `vas.store` | store person | receive, accept, sell, the nightly SOH upload |
| `vas.cashier` | store person | billing |
| `vas.warehouse` | warehouse | warehouse operations |
| `vas.whm1`, `vas.whm2` | warehouse manager | transfers: one drafts, the other approves |
| `vas.preparer` | warehouse + IT admin | prepares the opening top-ups the SOH sync stages |

The store bills in counter (`historical`) mode, as the loader leaves it, so
the online-mode gates in `setup_store_test_run` (invoice prefix, sell policy,
online bill series, online selling) stay open there by design.

## Quick start: one command

On a synthetic tenant, this builds the store `VAS-SGMR` and its linked
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
