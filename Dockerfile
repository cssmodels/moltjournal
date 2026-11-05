FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY mcp_server ./mcp_server
COPY dryrun ./dryrun
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
COPY sql ./sql
COPY lab ./lab

RUN useradd --system --uid 10001 molt && chown -R molt /srv
USER molt

# Alembic owns the schema here; the app must not create tables.
ENV MOLT_AUTO_CREATE_SCHEMA=0 MOLT_ENV=production

EXPOSE 8000
# --proxy-headers with a restricted trust list, so the rate limiter sees the
# real client address rather than Caddy's, and only Caddy can claim to be one.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 --workers 2 \
  --proxy-headers --forwarded-allow-ips='*'"]
