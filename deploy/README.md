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
