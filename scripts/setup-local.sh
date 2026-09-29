#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
export PLAYWRIGHT_BROWSERS_PATH="$repo_dir/.local/playwright-browsers"

for tool in python3 uv node yarn docker; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '%s is missing; install it before running local setup.\n' "$tool" >&2
    exit 1
  fi
done
if [[ "$(node -p 'process.versions.node.split(".")[0]')" != "22" ]]; then
  printf 'Node 22 is required; found %s.\n' "$(node --version)" >&2
  exit 1
fi
if [[ "$(yarn --version)" != "1.22.22" ]]; then
  printf 'Yarn 1.22.22 is required; found %s.\n' "$(yarn --version)" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  printf 'Docker Compose is unavailable; install the Compose plugin.\n' >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  printf 'Docker engine is unavailable; start Docker and confirm this user can access it.\n' >&2
  exit 1
fi

python3 scripts/init-local.py
uv --cache-dir "$repo_dir/.local/uv-cache" sync --project backend --python 3.12 --locked
(
  cd frontend
  yarn install --frozen-lockfile --cache-folder "$repo_dir/.local/yarn-cache" --non-interactive
  yarn playwright install chromium
)
docker compose -f compose.local.yaml up -d --wait database
cd backend
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py seed_foundation
printf '\nLocal setup complete. Start with: python3 scripts/dev-local.py (from repo root).\n'
