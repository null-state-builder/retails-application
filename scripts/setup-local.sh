#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
python3 scripts/init-local.py
uv --cache-dir "$repo_dir/.local/uv-cache" sync --project backend --python 3.12 --locked
(
  cd frontend
  yarn install --frozen-lockfile --cache-folder "$repo_dir/.local/yarn-cache" --non-interactive
)
docker compose -f compose.local.yaml up -d --wait database
cd backend
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py seed_foundation
printf '\nLocal setup complete. Start with: python3 scripts/dev-local.py (from repo root).\n'
