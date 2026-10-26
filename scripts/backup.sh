#!/usr/bin/env bash
# Nightly database backup: dump, check the dump, prune old dumps, copy them off the server.
#
# Run it from the host crontab, not from the worker container, because the dump goes
# through docker compose. Settings come from the environment, or from .env:
#   BACKUP_DIR     where the dumps are kept on this server (default /var/backups/finland-news)
#   BACKUP_REMOTE  rsync destination for the off-site copy, for example storagebox:ft-backups
#   HC_BACKUP_URL  healthchecks.io ping URL for this backup
set -euo pipefail

cd "$(dirname "$0")/.."

env_value() {
    grep -E "^$1=" .env 2>/dev/null | head -n 1 | cut -d= -f2- || true
}

BACKUP_DIR=${BACKUP_DIR:-$(env_value BACKUP_DIR)}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/finland-news}
BACKUP_REMOTE=${BACKUP_REMOTE:-$(env_value BACKUP_REMOTE)}
HC_BACKUP_URL=${HC_BACKUP_URL:-$(env_value HC_BACKUP_URL)}

compose() {
    docker compose -f docker-compose.yml "$@"
}

ping_healthcheck() {
    # The URL is a secret, so curl's output is discarded and only a generic message is printed.
    [ -n "$HC_BACKUP_URL" ] || return 0
    curl -fsS -m 10 --retry 3 "${HC_BACKUP_URL%/}$1" > /dev/null 2>&1 \
        || echo "backup: healthcheck ping failed" >&2
}

# Any failure, including one caught by set -e, sends the /fail signal. Success pings at the end.
trap 'status=$?; if [ "$status" -ne 0 ]; then ping_healthcheck /fail; fi' EXIT

mkdir -p "$BACKUP_DIR"
rm -f "$BACKUP_DIR"/*.partial

dump="$BACKUP_DIR/ft-$(date +%F).dump"
partial="$dump.partial"

compose exec -T db pg_dump -U ft -Fc ft > "$partial"
# A dump that cannot be listed cannot be restored, so it must never replace a good one.
compose exec -T db pg_restore --list < "$partial" > /dev/null
mv "$partial" "$dump"
echo "backup: wrote $dump ($(du -h "$dump" | cut -f1))"

python3 -m worker.backup prune "$BACKUP_DIR"

if [ -z "$BACKUP_REMOTE" ]; then
    echo "backup: BACKUP_REMOTE is not set, so there is no off-site copy" >&2
    exit 1
fi
# --delete makes the off-site copy match the pruned local directory.
rsync -a --delete "$BACKUP_DIR/" "${BACKUP_REMOTE%/}/"
echo "backup: copied to ${BACKUP_REMOTE%%:*} off-site storage"

ping_healthcheck ""
echo "backup: done"
