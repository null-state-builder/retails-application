# Railway image for the KDPS API and its goods worker (one container, see start.sh).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
# collectstatic needs settings to import; nothing here touches a database.
RUN DJANGO_DEBUG=1 DATABASE_URL=postgres://build@localhost/build \
    python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["sh", "start.sh"]
