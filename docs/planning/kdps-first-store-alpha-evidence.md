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


## Signup UX and progress panel — 1 October 2026

Implemented on HEAD `3a16c616c259376598b293d4e89e9bc9e3899a66` plus the working-tree changes. Runtime source
fingerprint: `1ae2f6c81177dcadba4ddb84c8b0e371afa62a21e5b9282eedde718ca33ef23a`. Reproduce by sorting the unique tracked and
untracked non-ignored file paths from `git ls-files -z --cached --others
--exclude-standard`, retaining `backend/`, `frontend/`, `scripts/`; hash each file
with SHA-256, encode the ordered `{"path":...,"sha256":...}` records as JSON
with sorted keys and separators `(',', ':')`, then SHA-256 that UTF-8 JSON.
The complete working-tree manifest is retained privately at
`.local/signup-working-tree-fingerprint.json`; neither credentials nor the raw
SOH workbook are included in the changes for delivery.

Implemented: Company / Regional settings / First store / People form, local
review and independent confirmation; six-step numbered progress tiles with
scroll-aware current section; Jharkhand/Bihar state and city suggestions plus
explicit custom city; India/INR/Kolkata/en-IN dropdowns; new/existing radio cards;
server-side omitted-code allocation under the existing installation lock;
explicit proposed-email/stable-human reservation claims during normal staff
administration; named confirmation status, responsive spacing and theme tokens.
The staff-link extension adds optional `registration_email` to the existing
staff command. It grants no login or assignment; the initial login must match
the explicitly claimed proposal. Existing installations without the new
reservation event keep their previous staff path. Existing registration
summaries and hashes are not rewritten; no schema migration was introduced.

Actual affected chain: Signup → registration view/serializer → installation
lock → resolved codes → summary hash and existing personal confirmations →
canonical tenant/staff bootstrap. Proposed codes are reserved in registration
history only at completion. Authorised staff command → code/person locks →
explicit proposal-email check → stable human claim event and canonical Staff
creation; login creation validates that link before issuing credentials. No
additional report/export/print/event delivery surface is introduced. Existing
registration summaries remain private and scope-projected by their existing
readers. New error-link navigation is registered under A00; C01/C02 dependencies
remain with their existing owners.

Proof identity verified before database checks: project
`kdps-proof-ff7f4a176291`, container `kdps-proof-ff7f4a176291-database-1`,
host `127.0.0.1:55433`, database/user `kdps_proof`, PostgreSQL 17.11,
host/container system identifier `7690899938284695588`. Django tests use the
separate disposable `kdps_proof_test` database.

Current verification:

- `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_installation_registration.py tests/test_so03_admin_delivery.py tests/test_unified_admin_projection.py -q`: **43 passed**, two existing staticfiles warnings. Includes code allocation/replay/revision, code/email reservation, public metadata privacy, initial-person concurrency and scoped administration regressions. Log: `.local/signup-registration-tests.log`.
- `cd frontend && node_modules/.bin/vitest run`: **51 passed**, 13 files. Log: `.local/signup-frontend-tests.log`.
- `cd frontend && PLAYWRIGHT_BROWSERS_PATH=../.local/playwright-browsers node_modules/.bin/playwright test --config playwright.signup.config.ts`: **4 passed**, at 1440/1366/768/375px, light/dark screenshots. Empty/populated/review/error/denied/loading/closed states, code previews, state/city reset, radio alternatives, retained fields, named confirmations, reload privacy and progress navigation. Log: `.local/signup-browser-tests.log`; screenshots under `.local/signup-browser/`.
- Frontend `tsc --noEmit`, ESLint and Prettier checks passed; full ESLint retains 20 pre-existing warnings outside changed signup code. Targeted changed-file ESLint passed without warnings.
- Ruff on changed Python files and mypy on the four affected registration/admin modules passed.
- `python3 scripts/api-contract.py check`: **505 paths match**.
- `backend/.venv/bin/python scripts/check_so03_consolidation.py --write` followed by `--check`: reviewed generated changes; **3036 surfaces match**.
- `backend/.venv/bin/python scripts/check_so03_boundaries.py` and `backend/.venv/bin/python -m unittest discover -s scripts -p 'test_*so03*.py'`: passed; **20 checker tests**.
- `python3 scripts/proof.py run --cwd backend -- .venv/bin/python manage.py makemigrations --check --dry-run`: **No changes detected**.
- `git diff --check`: passed.

Limitations: browser journeys above deliberately stub transport to test UI
states; they are not a real-backend end-to-end registration acceptance claim.
The ordinary registration and administration services/API boundaries were
exercised on PostgreSQL by the backend suite. In-app browser inspection later
failed with a browser focus timeout; test-generated screenshots were inspected
instead. `python3 scripts/baseline.py verify` passed proof preparation and seed
repeatability but stopped at launcher smoke because the user's running app owns
8000/5173. Log: `.local/signup-baseline.log`. A full baseline green result and
full first-store alpha acceptance are not claimed. Local app processes were
restarted with their existing launcher/configuration; no local/real tenant was
registered, reset or cut over by this work.
# Temporary-password and validation-feedback follow-up — 1 October 2026

At the user's request, initial signup temporary passwords accept any non-empty
value up to the existing 128-character limit, without strength checks. Owner
and Admin still use different credentials and independently confirm the same
revision. Hashing, private summaries, first-sign-in replacement and permanent
password validation remain enforced. No global password settings changed.

Signup field errors now use one labelled, linked summary rather than repeating
the first error in a second banner. All messages for a field are retained,
corrected field errors clear on editing, and service failures retain their
separate fallback when no field errors are available. The populated local form
was revalidated through Review setup; no registration was submitted. The local
API was restarted with its unchanged command/configuration while preserving the
frontend and worker. API health returned `ok`.

HEAD: `3a16c616c259376598b293d4e89e9bc9e3899a66`. Tested slice fingerprint:
`02749cf156e2f3af55b3f445d787b3d5060bf1fd8694ec3d0c0f4621764a5045`.
Reproduce by sorting the following paths, then feeding each UTF-8 path, a NUL
byte and its binary SHA-256 content digest into one SHA-256 accumulator:
`backend/accounts/registration_serializers.py`,
`backend/tests/test_installation_registration.py`,
`frontend/src/pages/Signup.tsx`, `frontend/src/pages/Signup.test.tsx`,
`frontend/browser/signup-ux.spec.ts`,
`docs/planning/so-03-consolidation-inventory.json`,
`docs/planning/so-03-consolidation-register.md`,
`docs/planning/so-03-dependency-map.md`.

Before database tests, `python3 scripts/proof.py up` verified the owned project
`kdps-proof-ff7f4a176291`, container `kdps-proof-ff7f4a176291-database-1`,
host port 55433, database/user `kdps_proof`, PostgreSQL 17.11 and matching
container/host system identifier `7690899938284695588`. Tests used only the
disposable sibling `kdps_proof_test`.

- `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest tests/test_installation_registration.py -q --reuse-db`: **30 passed**, two staticfiles warnings. Includes short/common/numeric/whitespace temporary credentials, blank/shared denial, successful joint confirmation and refusal of a weak permanent replacement without changing the credential.
- `cd frontend && node_modules/.bin/vitest run src/pages/Signup.test.tsx`: **5 passed**; single error summary, labelled field links, complete field messages and service-error fallback.
- `cd frontend && PLAYWRIGHT_BROWSERS_PATH=../.local/playwright-browsers node_modules/.bin/playwright test --config playwright.signup.config.ts`: **4 passed** at 1440/1366/768/375px. Mocked synthetic transport verifies simple passwords, retained fields, one validation summary, error focus, edit-to-clear and retry alongside existing signup states. This is presentation proof, not a real-tenant acceptance claim.
- Frontend `node_modules/.bin/tsc --noEmit`, changed-file ESLint and Prettier checks: passed.
- `python3 scripts/proof.py run --cwd backend -- .venv/bin/mypy accounts/registration_serializers.py`: passed. An initial root-directory invocation did not load the backend configuration and is not valid type-check evidence.
- `backend/.venv/bin/ruff check backend/accounts/registration_serializers.py backend/tests/test_installation_registration.py`: passed.
- `python3 scripts/proof.py run -- backend/.venv/bin/python scripts/api-contract.py check`: **505 API paths matched**.
- Inventory refresh reviewed: removal of the serializer's unused `accounts.models` import and its derived static candidates; classifications and workflow owners unchanged. `backend/.venv/bin/python scripts/check_so03_consolidation.py --check`: **3036 surfaces matched**.
- `backend/.venv/bin/python scripts/check_so03_boundaries.py` and `git diff --check`: passed.

No migrations or full baseline were run for this follow-up; earlier baseline
results do not verify this revision. Existing unrelated work and the raw workbook
were preserved; no commit or tenant cutover was performed.

## Visible pending-setup editing — 1 October 2026

Superseded by the direct-back correction below. This section records the
intermediate credential-gated editor, which the user rejected.

The confirmation header now always offers `Edit setup` while registration is
pending. It verifies the saved Owner credentials before restoring the saved
form through the existing revision path. An Admin cannot enter the editor.
Verified Owner credentials carry forward to the existing PATCH guard without
asking for the same password twice. New temporary credentials remain blank;
saving changes still invalidates both previous confirmations. No recovery or
unauthenticated overwrite path was added; backend/API contracts are unchanged.

HEAD remains `3a16c616c259376598b293d4e89e9bc9e3899a66`. Frontend slice fingerprint
`d380b819f207992f399d3f057189f25844c812fc1d49b47324049e00c1d37659`
uses the preceding SHA-256 algorithm over sorted paths
`frontend/src/pages/Signup.tsx`, `frontend/src/pages/Signup.css`,
`frontend/src/pages/Signup.test.tsx` and `frontend/browser/signup-ux.spec.ts`.

Current checks: `cd frontend && node_modules/.bin/vitest run src/pages/Signup.test.tsx`
**5 passed**; `PLAYWRIGHT_BROWSERS_PATH=../.local/playwright-browsers node_modules/.bin/playwright test --config playwright.signup.config.ts`
**4 passed** at 1440/1366/768/375px, with mocked synthetic transport. Covers a
visible edit option on private reload, Admin denial, Owner verification,
restored fields/codes, blank new passwords and cancellation back to confirmation.
Frontend `tsc --noEmit`, changed-file ESLint and Prettier passed. Consolidation
`--check` matched **3036 surfaces**; boundary checks and `git diff --check` passed.
The live user tab shows the edit option; it was not used to revise or save the
user's pending registration. No database-dependent checks, migration or full
baseline were run for this frontend-only follow-up.

## Direct return to the filled setup — 1 October 2026

The confirmation screen now has one `Back to setup` button that immediately
opens the populated form. Owner authentication remains at saving a revision,
through the existing PATCH guard; it is no longer a navigation prerequisite.
Saving still requires new temporary credentials and invalidates both prior
confirmations. `Back to confirmation` retains the draft without submitting it.

An explicit whitelist retains company, store and proposed-person details in
the originating tab's session storage across reloads. Passwords are excluded,
and the draft is removed when registration completes. A private unauthenticated
reload may display that local draft, labelled `Your entered details`; server
confirmation statuses and saved identifying details still require credential
verification. Another browser without a local draft cannot read the private
server summary. Storage failure leaves the current in-memory form usable.

The previous update had already lost the live tab's in-memory form. Its agreed
synthetic KDPS/Vaishnavi QA details were restored through the form. A live
confirmation → setup → confirmation roundtrip preserved names, regional values,
addresses, codes and proposed team. No credentials were entered, and no saved
registration was revised or confirmed during this correction.

HEAD remains `3a16c616c259376598b293d4e89e9bc9e3899a66`. Current frontend slice
fingerprint `4c0c4387d892dc5cc662a65a746ce3d26c5364609e44fcc0b891b477a329fc64`
uses the preceding sorted-path/NUL/binary-content-SHA256 algorithm over the
same four frontend paths.

- `cd frontend && node_modules/.bin/vitest run src/pages/Signup.test.tsx`: **5 passed**.
- `cd frontend && PLAYWRIGHT_BROWSERS_PATH=../.local/playwright-browsers node_modules/.bin/playwright test --config playwright.signup.config.ts`: **4 passed** at 1440/1366/768/375px, mocked synthetic transport. Covers direct back navigation, reload retention, populated fields/codes, blank passwords, no stored password keys, return to confirmation and draft removal after registration, alongside the existing validation/confirmation tests.
- Frontend `node_modules/.bin/tsc --noEmit`, changed-file ESLint and Prettier: passed.
- `backend/.venv/bin/python scripts/check_so03_consolidation.py --check`: **3036 surfaces matched**.
- `backend/.venv/bin/python scripts/check_so03_boundaries.py` and `git diff --check`: passed.

No database-dependent checks, migration or full baseline were run for this
frontend-only correction. Browser transport tests do not establish real-store
acceptance. Existing unrelated work was preserved.
