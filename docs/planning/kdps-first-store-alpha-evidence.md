# KDPS first-store alpha foundation: historical implementation evidence

## Starting revision and preserved state

Implementation began at `e6cfc9734e5c20b779d9849473394b66c412b973`, with a clean working tree.
The SHA-256 over sorted tracked and nonignored untracked paths, each encoded as
`path + NUL + byte length + NUL + file bytes + NUL`, was
`bbb6eed6d3accade33ccfa7b8e0466a455aec5e7ce333c7b0d7dab41719df9d4` (1,304 files).
Private/ignored runtime settings are outside that source fingerprint.

## Positively identified proof environment

Before database-dependent implementation checks on 30 September 2026:

- Compose source: this checkout's `compose.proof.yaml`.
- Project: `kdps-proof-ff7f4a176291`.
- Container: `kdps-proof-ff7f4a176291-database-1`.
- Image: `postgres:17.11`; server version number: `170011`.
- Host binding: `127.0.0.1:55433` to container port `5432`.
- Database and user: `kdps_proof`; disposable test database: `kdps_proof_test`.
- Container and host system identifier: `7690899938284695588`.
- `scripts/proof.py.assert_database()` verified identical container/host identity.

The existing proof volume was preserved. Its user password was aligned with the
generated proof credential file through the owned container after authentication
failed. Credentials were not printed. No developer or real tenant database was
probed or changed. All later database checks must use `scripts/proof.py run`,
which rechecks that identity before dispatch.

## Status

The earlier observations below are retained as historical implementation
evidence. Their provisional status and failures are superseded by the current
[manager alpha delivery and verification record](kdps-manager-alpha-delivery.md).
The fictional opening, browser sale, cash count and unchanged stock/money
reconciliation are verified there. The real workbook remains staged inactive;
general trading counts, transfer-family consolidation and real cutover retain
their explicit owners and acceptance gaps. Earlier green checks do not validate
later source changes.

## Separate blank-installation browser rehearsal

The normal proof fixture was preserved. Before the clean migration and every
subsequent rehearsal command, `scripts/first-store-proof.py` verifies this sibling
database against the owned container and its recorded host system identifier:

- Project/container/image/port/user: the proof cluster listed above.
- Database: `kdps_rehearsal_first_store_ce4e1b2ba562`.
- PostgreSQL: 17.11; host/container system identifier `7690899938284695588`.
- Deployment identity and generated example.test credentials: private local files
  with mode 0600; neither credentials nor deployment keys appear in this record.
- Clean migrations and `initialise_installation` completed without demo seeding.
- Local backend: `backend/.venv/bin/python scripts/first-store-proof.py run --
  backend/.venv/bin/python -m uvicorn config.asgi:application --host 127.0.0.1
  --port 8000` (restarted without hot reload for stable browser acceptance).
- Frontend: `npm run dev` in `frontend`, at `http://127.0.0.1:5173`.

The in-app browser exercised the actual blank homepage, one signup form,
proposed Store Person, exact six-role access summary, separate fixture Owner and
Admin authentication/signatures, and permanent signup closure. The first Owner
login redirected to `/change-password`; opening `/setup/first-store` directly
could not bypass it. Password replacements were exercised through the actual API
in the proof fixture, preserving the browser credential-change handoff rule.
These are simulated people, not real initial signatories or trading approvals.

The proof helper then used ordinary API commands for an Admin-drafted,
Owner-approved calendar, Owner-created manager staff/login/store assignment,
Admin's separate Warehouse assignment and independent review. Fresh logins
verified session invalidation and mandatory password replacement. Owner's setup
checklist visibly lists outstanding configuration, opening, numbering and counter
gates, and opens the typed, scoped selling-policy form.

Browser screenshots, which contain only fictional proof data, are retained in
`.local/first-store-browser-evidence/` (`01-registration-closed.jpg`,
`02-password-gate.jpg`, `03-setup-gates.jpg`). The stock and POS browser journey is
still in progress; these screenshots do not establish sale acceptance.

The fictional opening source was separately claimed/prepared by Admin's scoped
Warehouse assignment, reviewed by Owner at revision 4, materialised into one
manifest and approved OPT `ALPHA/OPT/26-27/000001`. The manager scanned barcode
`ALPHA000123`, verified ticket MRP ₹1,000 and three good units, placed all three
in the fictional sales floor and completed the acceptance session. The canonical
stock screen displays physical 3, valued 3 and accepted 3. Owner then approved
goods readiness using the ordinary password-confirmed action. Evidence:
`08-opening-accepted.png` and `09-goods-readiness-active.png`.

Browser testing found the primary Inventory screen still reading only the old
balance projection (zero while canonical stock was three). Its C06 read-only
projection adapter and directly connected search/availability consumers are
being corrected; no second stock writer is being introduced. The cash summary
already reads accepted Sale/SaleTender data, but lacked whole-store Money scope
and a delivery recheck. That guard has been added; its tests remain pending.

A readiness evidence reference longer than the model's 60-character limit
initially returned a 500. The transaction rolled back without approving readiness.
The API now refuses invalid type/length before writing, and the UI limits input.
The shorter reviewed reference succeeded through the browser; targeted controls
tests pass. Retained temporary hot-reload and test-fixture failures are diagnostic
evidence, not hidden successful runs.

On 30 September, the sibling proof database identity was reverified before
`backend/.venv/bin/python scripts/first-store-proof.py run --
backend/.venv/bin/python backend/manage.py migrate --noinput` applied only new
`outbound.0047_soh_reconciliation` successfully. Existing opening, source, approval
and stock records were preserved; the backend restarted without hot reload.

## Provisional behavior evidence and remaining verification

The reviewed-source integration currently passes parser and real stock-writer
tests: source review, independent OPT approval, manager physical acceptance,
quantity reconciliation and replay without another posting. The online integration
has exercised the same accepted stock through a discounted bill, one stock
decrement, one tender/cash collection, balanced value effects, exact lost-response
replay and no-change refusals. Additional scope, concurrency, delivery and endpoint
tests are being added. Final command output and a complete source fingerprint must
supersede these provisional observations after the implementation stops changing.

The inventory refresh records first-store registration under SO-03, identities and
readiness under SO-04, the parent SOH/PT workflow under SO-07 (with SO-08 custody
dependencies), commercial configuration under SO-06 and online issue under SO-09.
New traces remain pending until their current-revision evidence is recorded.
No other workflow retirement, SBU retirement or full offline till cutover is
inferred from this implementation.

Real-shop activation still requires reviewed legal identity, stable item/brand
and season mappings, HSN, evidenced valuation basis, explained Amount
differences, fresh cutover data, physical verification and the retained CA gate
for statutory tax configuration. The supplied workbook remains unchanged. Its
25,689 rows, 11,161 positive records and 19,896 units are parsing evidence only;
they have not been posted into a real tenant. Online late-return exceptions stay
inactive until they have separately authorised, recorded approval evidence.

## Real workbook staging and confirmed inputs

The user confirmed **Vaishnavi at Singh More, Ranchi** and that **Rate is purchase
cost before tax**. These confirmations do not establish a legal company, trusted
product/brand ownership, HSN, physical verification or a fresh cutoff. The user
will supply a new SOH export for final opening.

The unchanged repository and Downloads files have SHA-256
`033b5d6dbd7943ba3e8cd07861d1ea1ba0822df65e560698220b6745d8ffb715`
and size 2,590,284 bytes. The in-app browser uploaded the exact repository file
through the normal SOH upload endpoint into the positively identified sibling
proof database. It remains **uploaded**, revision 1, with no mappings, approval,
manifest application or accepted stock. The destination is separate planned
proof site 2, **Source preview — destination unconfirmed**, under fictional Alpha
proof company. This does not bind the real shop to the fictional legal entity.
The preview-site helper uses the ordinary Owner site API, verifies preservation
of FIRST stock/history/configuration/access and is repeatable without duplication.
Browser evidence: `.local/first-store-browser-evidence/07-real-soh-staged.jpg`.
An exact-source read-only query through the verified sibling wrapper also
confirmed uploaded/revision 1, zero opening batches, zero preview-site positions,
`sell_ready=false` and planned lifecycle.

The normal browser response confirms 25,689 rows, 11,161 positive stock records,
19,896 units and 14,528 zero-stock catalogue records. Read-only parsing confirms
all row movement totals and the footer quantity; source Amount differs from
quantity × Rate by **−₹0.38 across 99 rows**. All 11,161 positive rows need an
evidenced HSN. Positive mapping workload includes 623 brand labels, 90 season
source values, 143 sizes and 145 categories. There are 976 positive rows without
season, 17 without size and nine price anomalies covering 13 units, including
one zero-MRP unit. Labels and source dates do not establish stable identity.

## Later SOH snapshots: implemented fence and remaining contract

The user also requires later full SOH imports to update the selected store.
That is a **retained C08/SO-08 count/correction dependency**, separate from first
opening. Later source staging remains available as inactive evidence. A SiteGuard
fence now permits one materialised initial parent, its deterministic child
batches, interruption recovery and exact replay; another full source cannot
post a second opening. A later unmaterialised source does not invalidate an
already reconciled initial opening. This fence is implemented. A distinct retained
count-owned snapshot service/UI is now implemented and under verification:
zero changes and independently reviewed shortages use audited P13 quantity/value
legs against original origins. Unsupported gains, ambiguous origins and
encumbrances remain inactive with explicit reasons and cancellation evidence.
This does not yet fulfil unrestricted later SOH updates or establish acceptance.

The existing non-trading count path cannot be reused as a trading-store shortcut:
it refuses trading/till history and nonzero count-owned posting remains an
unfinished 17A contract. Generic adjustments refuse count-owned changes. The
completion contract must pin the complete source, trusted store/identities,
explicit cutoff, all zero/omitted rows, freeze and journal watermark, exact
layers/holds, physical evidence, revision and independent review. It must append
audited differences and preserve sale/cost/posting history. An old snapshot must
not recreate units sold after its cutoff. Unknown ownership, cost, movement
classification or encumbrances remain owned inactive exclusions.

## Current focused checks (provisional until final freeze)

- `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_first_store_online.py tests/test_first_store_online_concurrency.py tests/test_online_sale_submission.py tests/test_first_store_controls.py -q --reuse-db --maxfail=1 --tb=short`: **37 passed**, 109.34s. Includes actual issue, exact retry/concurrency, cash effects and nonoverridable trusted source-costing readiness.
- `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_soh_import.py -q --reuse-db --maxfail=1 --tb=short`: **20 passed**, 10.58s. Includes independent review, custody, projection, interrupted batches, competing initial parents and second-opening no-change denial.
- `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_so03_denials.py::test_online_sale_replay_refuses_foreign_store_before_revealing_bill -q --reuse-db --maxfail=1 --tb=short`: **1 passed**, 0.65s. The earlier full-backend run's test double was updated to accept the production writer's new keyword arguments; no production denial was weakened.
- `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_first_store_controls.py -q --reuse-db --maxfail=1 --tb=short`: **7 passed**, 1.16s, including invalid readiness references with zero capability writes.
- `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_first_store_snapshot_boundaries.py -q --reuse-db --maxfail=1 --tb=short`: **11 passed**, 36.55s. Exact all-till pause/frontier, pending and retired allocations, frozen mutations and accepted replay boundaries; rejected outcomes remain evidence.
- `python3 scripts/baseline.py frontend`: **passed** TypeScript, ESLint (20 existing warnings), formatting and 25 tests in seven files.
- Earlier `python3 scripts/baseline.py backend`: checks/types/import contracts passed; pytest **231 passed, 1 failed** on the corrected test double above. This is retained failure evidence, not a current full-backend pass.

An intermediate `python3 scripts/baseline.py api-generate` refused two unresolved
OpenAPI request-schema warnings in the new SOH reconciliation views. No generated
contract was accepted from that run; the exact schemas must be repaired and the
command rerun after source freeze.

Final baseline, migration rehearsal, revision fingerprint and the complete
first-store browser sale/print/cash journey remain outstanding. Real statutory
tax approval remains a required setup dependency. The current unmodified default
format date is 1 April 2027, so a blank installation can configure numbering
normally and use the retained current format. Existing installations whose saved
new-format start is already past retain the mid-year format-change restriction;
this must not be misreported as a blocker for the blank-installation scenario.
Any explicitly synthetic commercial tax fixture used for the browser sale will
be recorded separately; it cannot prove real-store tax approval.
