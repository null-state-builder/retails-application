"""Create private local configuration once; never overwrite existing settings."""

from pathlib import Path
import secrets
from urllib.parse import urlsplit

root = Path(__file__).resolve().parent.parent
local = root / ".local"
local.mkdir(exist_ok=True)
postgres_env = local / "postgres.env"
backend_env = root / "backend" / ".env"

if not postgres_env.exists() and not backend_env.exists():
    password = secrets.token_hex(24)
    postgres_env.write_text(
        f"POSTGRES_USER=kdps_local\nPOSTGRES_DB=kdps_local\nPOSTGRES_PASSWORD={password}\n"
    )
    backend_env.write_text(
        f"DATABASE_URL=postgresql://kdps_local:{password}@127.0.0.1:55432/kdps_local\n"
        f"DJANGO_SECRET_KEY={secrets.token_hex(48)}\n"
        "DJANGO_DEBUG=1\nDJANGO_ALLOWED_HOSTS=127.0.0.1,localhost\n"
        "KDPS_COOKIE_SECURE=0\n"
        "CSRF_TRUSTED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173\n"
        "SEED_DEMO=1\n"
        f"SEED_CREDENTIALS_PATH={local / 'test_credentials.md'}\n"
    )
    postgres_env.chmod(0o600)
    backend_env.chmod(0o600)
    print("Created local database and backend configuration.")
elif not postgres_env.exists() or not backend_env.exists():
    raise SystemExit("Only one local environment file exists. Reconcile it manually; nothing overwritten.")
else:
    print("Existing local configuration preserved.")

def values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#") and "=" in line)

database = values(postgres_env)
backend = values(backend_env)
url = urlsplit(backend.get("DATABASE_URL", ""))
if not (
    url.scheme == "postgresql"
    and url.hostname == "127.0.0.1"
    and url.port == 55432
    and url.path == "/kdps_local"
    and url.username == database.get("POSTGRES_USER") == "kdps_local"
    and database.get("POSTGRES_DB") == "kdps_local"
    and url.password == database.get("POSTGRES_PASSWORD")
):
    raise SystemExit("Configuration does not match the isolated local demo database; setup refused.")
