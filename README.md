# KDPS local development

The supported development setup is macOS with Python 3.12, Node 22, Yarn 1.22.22, uv, and a running Docker engine with Compose. CI uses Linux and the same root verification commands. Local and proof PostgreSQL containers use version 17.11. The seeded users and store data are synthetic.

## First setup and startup

From the repository root:

```sh
npm run setup
npm run dev
```

Setup checks prerequisites, installs backend and frontend dependencies from the committed lockfiles, installs pinned Chromium into `.local/playwright-browsers`, starts the local database, applies migrations and seeds the foundation. Re-running setup preserves `backend/.env`, `.local/postgres.env`, existing accounts, passwords and access settings. The local database uses 127.0.0.1:55432; the API uses 127.0.0.1:8000 and the frontend uses 127.0.0.1:5173.

Open [the local app](http://127.0.0.1:5173). The synthetic Owner login is `owner@kdps.demo` / `Owner@123`; the operations-store login is `ops.store@synthetic.demo` / `Synthetic@123`. Setup writes the other synthetic credentials to `.local/test_credentials.md`. The local launcher reports ready only after the API and frontend respond.

## Stop, restart and troubleshoot

Ctrl-C in the launcher stops its API, worker and frontend children. It retains database files in `.local/postgres`. For a later start:

```sh
docker compose -f compose.local.yaml up -d --wait database
npm run dev
```

To stop only the local database, run `docker compose -f compose.local.yaml stop database`. Do not remove `.local/postgres` to solve a startup problem. `npm run dev` identifies missing setup, an unavailable or wrong database, an occupied port, or a child that exits; application logs are `.local/backend.log`, `.local/worker.log` and `.local/frontend.log`. If a dependency is missing or changed, run `npm run setup` again. Private settings stay in `backend/.env` and `.local/postgres.env`.

## Engineering verification

From the root, `npm run verify` starts and validates an independent disposable PostgreSQL proof stack on port 55433, checks foundation reseeding and launcher failure/restart behaviour, then runs every required backend, API, frontend, build, asset and Chromium check. It refuses a development database URL as a proof target. The proof stack is separate from the working synthetic demo data.

| Command | Checks |
| --- | --- |
| `npm run check:backend` | Django system and migration drift, Ruff, strict mypy, two import contracts and pytest on the proof database. |
| `npm run check:frontend` | Strict TypeScript, ESLint, Prettier and nonempty Vitest suite. |
| `npm run api:generate` | Generate `frontend/src/lib/api-schema.ts` from Django/DRF OpenAPI. Do not edit that file by hand. |
| `npm run api:check` | Fail on schema errors or warnings and on generated-client drift without changing files. |
| `npm run build` | Production frontend and PWA asset validation. |
| `npm run test:browser` | Built-app Chromium journeys against the isolated backend. |
| `npm run verify` | All required checks above, plus seed and launcher proofs. |

For targeted proof work, use `npm run proof:up`, `npm run proof:prepare` and `npm run proof:down`. `proof:down` removes only the disposable proof volume. Browser reports and failure traces stay under `frontend/playwright-report` and `frontend/test-results`; CI uploads them on failure. The detailed results and remaining limits are in the [SO-02 evidence record](docs/planning/so-02-engineering-baseline-evidence.md).

Passing SO-02 verifies the engineering baseline. Real opening stock, provider activation, permissions and store workflows have separate owners and gates in the [store requirement register](docs/planning/store-operations-requirement-register.md).

Before changing a store workflow, use the [consolidation contribution checklist](CONTRIBUTING.md) and its maintained SO-03 inventory to identify the target implementation, callers, data migration and retirement gate.
