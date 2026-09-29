# KDPS local development

The local setup runs Django, its background worker, React/Vite and an isolated
PostgreSQL **17.11** container. It uses synthetic development accounts and masters.

## First setup

Requirements: Python 3, uv (provides Python 3.12), Node 22, Yarn 1.22.22, and a
running Docker engine with Compose.

From the repository root:

```sh
bash scripts/setup-local.sh
python3 scripts/dev-local.py
```

Open **http://127.0.0.1:5173**. On the sign-in screen, click **Owner**, or use
`owner@kdps.demo` / `Owner@123`. These are public development credentials for the
localhost-only synthetic database. All seeded logins are in
`.local/test_credentials.md`. For the synthetic operations store, use
`ops.store@synthetic.demo` / `Synthetic@123`.

## Subsequent starts and stopping

```sh
docker compose -f compose.local.yaml up -d --wait database
python3 scripts/dev-local.py
```

Ctrl-C in the launcher stops the API, worker and frontend. To stop the database:

```sh
docker compose -f compose.local.yaml stop database
```

Database files stay in `.local/postgres`; stopping does not erase data. Do not
delete that directory to troubleshoot startup. The local database uses port
55432; the API uses 8000 and the frontend uses 5173, all bound to 127.0.0.1.

Logs are `.local/backend.log`, `.local/worker.log` and `.local/frontend.log`.
Private settings are `backend/.env` and `.local/postgres.env`. Setup creates them
once, checks the local database target and never overwrites existing settings.

## Checks and current limits

```sh
cd backend
.venv/bin/python manage.py check
cd ../frontend
yarn build
yarn typecheck
```

The dependency manifests and lockfiles were reconstructed from the supplied
source; the original manifests were absent. Startup verification passed Django
system checks, the fresh-database migrations, foundation seeding, frontend build,
and browser Owner login/dashboard requests.

The required strict TypeScript settings currently report 192 errors in the
existing source with this dependency baseline. They are not disabled; `typecheck`
remains a separate failing check. Successful local startup is not production or
store-workflow acceptance. The original PNG app icons are also absent, so PWA
installation/assets still need verification. IBM Plex Mono is restored through
its font package.

The seed supplies accounts and masters, not a reconciled real-store opening.
Feature approval gates remain in place. Payment, mail and AI providers are not
configured; no provider keys are needed to log in and explore the local app.
