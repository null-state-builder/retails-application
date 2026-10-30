# Hosting the KDPS API on one VPS

The frontend stays on Vercel (project `kdps-pos`). Its `frontend/vercel.json`
rewrites `/api/*` to this server, so browsers only ever talk to the Vercel origin.

This server runs, with Docker Compose:

| Service | What it is | Exposed |
| --- | --- | --- |
| `db` | Postgres 18, data in the `pgdata` volume | no |
| `api` | Django API + goods worker (`backend/start.sh`); migrates on every start; files in the `appdata` volume | no |
| `caddy` | HTTPS for `DOMAIN` (gets and renews certificates), proxies to the API | 80, 443 |

## First-time server setup (as root)

```bash
apt update && apt -y upgrade
apt install -y docker.io docker-compose-v2 git ufw
adduser kdps && usermod -aG sudo,docker kdps
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable
```

If root logs in with an SSH key, give the same key to `kdps`:
`rsync --archive --chown=kdps:kdps ~/.ssh /home/kdps`. Then log in as `kdps`.

Also open ports 80 and 443 in the hosting panel's firewall, if it has one.

## API hostname

`DOMAIN` must resolve to the server. Either:

- add an `A` record such as `api.example.com` → the server's IPv4; or
- use `<ip-with-dashes>.sslip.io` (for example `187-126-127-252.sslip.io`),
  which needs no DNS and still gets a real certificate.

`frontend/vercel.json` must rewrite `/api/:path*` to `https://<DOMAIN>/api/:path*`.

## Code (as `kdps`)

The repository is private, so the server needs a read-only deploy key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "kdps-vps"
printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
cat ~/.ssh/github_deploy.pub
```

Add the printed key in GitHub → repository → Settings → Deploy keys (read-only),
then:

```bash
git clone git@github.com:null-state-builder/retails-application.git ~/kdps
cd ~/kdps/deploy && cp env.example .env && chmod 600 .env
```

Fill in `.env` (the comments say how to generate each value).

## Start

```bash
cd ~/kdps/deploy && docker compose up -d --build
docker compose ps
curl -s https://$(grep ^DOMAIN= .env | cut -d= -f2)/api/health
```

Then redeploy the Vercel project with the updated `vercel.json`, open the
Vercel URL and register the company. Until a company is registered, the goods
worker restarts with "no tenant yet"; that is expected.

## Day to day

| Task | Command |
| --- | --- |
| Update | `cd ~/kdps && git pull && cd deploy && docker compose up -d --build` |
| Logs | `docker compose logs -f api` (or `caddy`, `db`) |
| Django shell | `docker compose exec api python manage.py shell` |

Back up nightly with `crontab -e`:

```cron
0 2 * * * mkdir -p ~/backups && cd ~/kdps/deploy && docker compose exec -T db pg_dump -U kdps -Fc kdps > ~/backups/kdps-$(date +\%F).dump && find ~/backups -name 'kdps-*.dump' -mtime +14 -delete
```

Copy `~/backups` off the server regularly. A backup that only lives on the same
VPS is lost with it.

## Frontend build stamp

Each frontend build is stamped with a build id, and the back office throws away
any copy of reference data it saved on the device under a different build. The
id comes from `KDPS_BUILD_ID`, else Vercel's `VERCEL_GIT_COMMIT_SHA` (set
automatically on Vercel), else the local git commit, else only the package
version. A frontend built anywhere else without `.git` (a server-side or Docker
build) must set `KDPS_BUILD_ID`, for example to the commit being deployed:
`KDPS_BUILD_ID=$(git rev-parse --short=12 HEAD) yarn build`. Without it, every
such build shares one stamp and a new deploy can show the previous build's saved
copy until it is refreshed.

## Revalidated reads (304)

The back office's hottest reads answer with an `ETag` and `Cache-Control:
no-store, private`, and the app sends `If-None-Match` itself, so a refresh that
finds nothing changed is a `304` with no body (`backend/core/conditional_get.py`).
Caddy's `encode` and Vercel's `/api` rewrite must pass `ETag` and
`If-None-Match` through; the API also accepts an ETag that Caddy marked with its
encoding (`"…-gzip"`). To check after a deploy, open the back office, reload a
screen such as Setup → Stores, and look for `304` on `/api/masters/stores` in
the browser's network panel.

## Rehearsal (on a laptop, before touching the server)

`deploy/compose.rehearsal.yaml` runs this same stack locally: the same Postgres 18
and the same API image with its goods worker, no Caddy, the API on
`127.0.0.1:58080` only, and its own volumes (project `kdps-rehearsal`). With the
git-ignored PT and SOH files in `docs/wardrobe-ratu-road/` (or `REHEARSAL_PT` /
`REHEARSAL_SOH` pointing at them):

```bash
scripts/rehearse_store_opening.sh --twice
```

It rebuilds from empty, bootstraps the synthetic tenant and The Wardrobe as the
store test-run runbook does, takes a baseline dump, opens the store from the PT
(restarting the API mid-load), re-uploads the same PT, syncs the SOH, repeats it,
tries an older SOH, and runs `scripts/rehearsal_assert.py`; `--twice` restores the
baseline and does it all again. It ends with a PASS/FAIL table and exits non-zero
on any FAIL. Secrets go to `deploy/.env.rehearsal` (generated from
`rehearsal.env.example`), dumps, logs and the synthetic logins to
`.local/rehearsal/`; both are git-ignored. Remove it all with
`docker compose -p kdps-rehearsal down -v`.
