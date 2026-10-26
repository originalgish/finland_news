#!/usr/bin/env bash
# Deploy main on the server: pull, build, migrate, start, then check /healthz.
#
# Every compose call names docker-compose.yml explicitly. The dev override file would
# publish the database and web ports on the public host.
set -euo pipefail

cd "$(dirname "$0")/.."

compose() {
    docker compose -f docker-compose.yml "$@"
}

for name in POSTGRES_PASSWORD SITE_DOMAIN BASIC_AUTH_USER BASIC_AUTH_HASH; do
    if ! grep -qE "^${name}=.+" .env; then
        echo "deploy: ${name} is not set in .env" >&2
        exit 1
    fi
done
if ! grep -qE "^COMPOSE_PROFILES=prod" .env; then
    echo "deploy: COMPOSE_PROFILES=prod is not set in .env, so Caddy would not start" >&2
    exit 1
fi

git pull --ff-only

# Migrate with the new image before the containers are replaced. The migration runner
# skips migrations that have already run.
compose build web worker
compose run --rm web python -m app.migrate
compose up -d

for attempt in 1 2 3 4 5; do
    if compose exec -T web python -c "import sys, urllib.request as r; sys.exit(0 if r.urlopen('http://127.0.0.1:8000/healthz', timeout=10).read() == b'ok' else 1)"; then
        echo "deploy: web answers /healthz"
        compose ps
        exit 0
    fi
    sleep 2
done

echo "deploy: web did not answer /healthz; check: docker compose -f docker-compose.yml logs web" >&2
exit 1
