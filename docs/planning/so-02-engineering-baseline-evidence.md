# SO-02 engineering baseline evidence

**Date:** 29 September 2026
**Branch:** `codex/so02-baseline`
**Verified worktree:** based on `fd1a780`; final code commit is recorded after the clean checkout rehearsal.
**Requirement links:** Main PRD R-QLT-008, R-QLT-009 and applicable R-QLT-010; §§16.2–16.3 and 16.9. The [store requirement register](store-operations-requirement-register.md) keeps workflow ownership and activation gates.

SO-02 proves an engineering path for later delivery. These new baseline tests do not close a store workflow or approve a store opening. The original source had no backend, frontend or browser test suite; the suites below are new, bounded evidence.

## Dependency and environment baseline

| Item | Observed source and version | Verification |
| --- | --- | --- |
| Python | 3.12.14; uv 0.12.13 | [`backend/pyproject.toml`](../../backend/pyproject.toml) requires Python 3.12; [`backend/uv.lock`](../../backend/uv.lock) resolves 64 package records. An empty Python environment installed 61 packages with `uv sync --locked` and did not change the lock. |
| Node / Yarn | Node 22.23.2; Yarn 1.22.22 | [`frontend/package.json`](../../frontend/package.json) declares 7 runtime and 19 development dependencies. An empty directory installed with `yarn install --frozen-lockfile`; the copied and working lockfiles had the same SHA-256, `72dae3ce5e4e09be3873768885b682859fa53ccd5cf8084fb06b64c198368f9c`. |
| PostgreSQL | 17.11 (`server_version_num=170011`) | [`compose.proof.yaml`](../../compose.proof.yaml) uses a distinct `kdps_proof` database, credentials, port 55433 and volume. [`scripts/proof.py`](../../scripts/proof.py) checks its identity before migrations or tests and supplies the proof URL after development `.env` loading. A working-demo URL in proof mode is refused. |
| Browser | Playwright 1.56.1; Chromium 141.0.7390.37 | [`scripts/setup-local.sh`](../../scripts/setup-local.sh) and [`scripts/baseline.py`](../../scripts/baseline.py) share an ignored repository-local browser cache. The first aggregate run caught a default-cache mismatch; after that repair the full run passed. |

Dependency changes followed the official compatibility guidance for [Vitest 4](https://vitest.dev/guide/migration.html), [Playwright](https://playwright.dev/docs/intro), [typescript-eslint](https://typescript-eslint.io/users/dependency-versions/) and [Vite PWA's Workbox peers](https://github.com/vite-pwa/vite-plugin-pwa/blob/main/package.json). Existing Django, DRF, Vite, TypeScript and Python major/minor requirements were retained. Generated migrations are excluded from strict mypy as historical generated programs; `makemigrations --check --dry-run` checks migration drift.

## Verification record

All Mac results below are from the isolated proof stack on this branch. The command is the stable root entry point unless the row names a negative probe. The expected result, observation and evidence location are recorded together so a later SO item can repeat the check.

| Requirement / check | Command and expected result | Observed result | Evidence location / status |
| --- | --- | --- | --- |
| R-QLT-009 / frozen backend install | Empty Python 3.12 environment, `uv sync --locked`; lock unchanged | Passed; 61 packages installed; lock unchanged | [`backend/uv.lock`](../../backend/uv.lock); pass |
| R-QLT-009 / frozen frontend install | Empty Node 22 environment, `yarn install --frozen-lockfile`; lock unchanged | Passed; no unresolved peer warnings; hash above unchanged | [`frontend/yarn.lock`](../../frontend/yarn.lock); pass |
| PRD §16.2 / isolated database and repeatable seed | `npm run check:seed`; no demo target, duplicate masters or overwritten password/access setting | Passed on PostgreSQL 17.11; user, role, store and brand counts and edited password/access setting preserved | [`scripts/check-seed-repeatability.py`](../../scripts/check-seed-repeatability.py), [`backend/tests/test_database_foundation.py`](../../backend/tests/test_database_foundation.py); pass |
| PRD §16.3 / startup and restart | `npm run check:launcher`; refuse failed prerequisites and stop owned children | Passed missing-runtime, unavailable proof database, occupied-port, worker-crash cleanup, readiness, shutdown and two start cycles | [`scripts/check-launcher.py`](../../scripts/check-launcher.py); pass |
| PRD §16.2 / backend | `npm run check:backend`; Django check, migration drift, Ruff, strict mypy, import contracts and pytest all pass | Passed: 0 Django issues, no migration changes, Ruff clean, mypy clean across 532 maintained files, 2 import contracts kept, 19/19 pytest cases | [`backend/pyproject.toml`](../../backend/pyproject.toml), [`backend/tests/`](../../backend/tests/); pass |
| PRD §16.2 / frontend | `npm run check:frontend`; strict TypeScript, ESLint, Prettier and Vitest pass with nonempty test discovery | Passed: 0 TypeScript errors, 0 ESLint errors, formatting clean, 9/9 Vitest cases | [`frontend/tsconfig.json`](../../frontend/tsconfig.json), [`frontend/src/lib/api.test.ts`](../../frontend/src/lib/api.test.ts), [`frontend/src/till/money-boundaries.test.ts`](../../frontend/src/till/money-boundaries.test.ts), [`frontend/src/pwa/config.test.ts`](../../frontend/src/pwa/config.test.ts); pass |
| PRD §16.2 / generated API contract | `npm run api:generate` then `npm run api:check`; byte-for-byte match and no Django schema diagnostics | Passed: 502 paths, zero warnings/errors, generated client matches; a deliberately stale file failed `api:check` and was restored | [`scripts/api-contract.py`](../../scripts/api-contract.py), [`frontend/src/lib/api-schema.ts`](../../frontend/src/lib/api-schema.ts); pass |
| PRD §16.9 / build and PWA assets | `npm run build`; production compile and referenced icon sizes/safe area pass | Passed; all four PNGs and editable SVG are present in source and built output | [`frontend/scripts/check-assets.mjs`](../../frontend/scripts/check-assets.mjs), [`frontend/public/kdps-mark.svg`](../../frontend/public/kdps-mark.svg); pass |
| R-QLT-008 / Chromium smoke | `npm run test:browser`; real-backend login/denial, protected access, dashboard, logout, assets and offline shell/API boundary pass | Passed: 2/2 Chromium journeys against proof PostgreSQL and built app | [`frontend/browser/baseline.spec.ts`](../../frontend/browser/baseline.spec.ts); pass |
| R-QLT-009 / complete Mac baseline | `npm run verify`; every required stage passes | Passed after repository-local Chromium cache repair; includes seed, launcher, backend, contract, frontend, build/assets and browser | [`scripts/baseline.py`](../../scripts/baseline.py); pass |
| R-QLT-009 / Linux backend rehearsal | Empty Linux/aarch64 container; `uv sync --locked --python 3.12`, Ruff, strict mypy and import contracts pass | Passed: 61 installed packages, 532 mypy files, 2 contracts kept; no lock mutation | Disposable `.local/linux-rehearsal/backend` copy; partial Linux evidence |
| R-QLT-009 / hosted Linux CI | GitHub Actions runs frozen setup and `npm run verify` | Not run; workflow is committed locally without a hosted run | [`.github/workflows/so-02-baseline.yml`](../../.github/workflows/so-02-baseline.yml); pending hosted execution |
| R-QLT-009 / clean checkout rehearsal | From committed source, `npm run setup` then `npm run verify` twice; tracked files unchanged | Pending final commit and disposable checkout | This report; pending |

The negative probes also confirmed relevant failures: TypeScript rejects an invalid assignment; ESLint rejects explicit `any`; Prettier rejects a style violation; Vitest rejects empty test discovery; asset validation rejects a missing icon; and `api:check` rejects stale generated output. [`backend/tests/`](../../backend/tests/) covers accepted and refused money inputs, login/session denial and success, real persistence, rollback, restricted-role RLS and tenant isolation. [`frontend/src/pwa/config.test.ts`](../../frontend/src/pwa/config.test.ts) checks that service-worker navigation caching excludes API responses.

Current non-failing diagnostics are 18 ESLint React-hook or stale-disable warnings, one Django test warning for an absent local `staticfiles` directory, and Vite's large-chunk warning. These are visible in the check output; no warning was suppressed to obtain the pass.

## Acceptance boundary

The clean disposable checkout and second verification remain to be recorded before SO-02 is marked accepted. Hosted Linux CI is separately pending; the partial Linux container pass does not represent a complete CI run. Store-device coverage, business-workflow acceptance and real-store activation remain with their later SO owners.
