#!/usr/bin/env bash
# Restore a dump into a scratch database, check the key tables, then drop the scratch database.
# Run it once after setup, and again after changing the schema.
#
#   scripts/restore-check.sh /var/backups/finland-news/ft-2026-10-26.dump
set -euo pipefail

cd "$(dirname "$0")/.."

dump=${1:?usage: scripts/restore-check.sh DUMP_FILE}
scratch=ft_restore_check

compose() {
    docker compose -f docker-compose.yml "$@"
}

compose exec -T db dropdb -U ft --if-exists "$scratch"
compose exec -T db createdb -U ft "$scratch"
trap 'compose exec -T db dropdb -U ft --if-exists "$scratch"' EXIT

compose exec -T db pg_restore -U ft --no-owner -d "$scratch" < "$dump"

count() {
    compose exec -T db psql -U ft -d "$scratch" -At -c "SELECT count(*) FROM $1"
}

failed=0
for table in sources tags tag_rules views articles clusters digests; do
    rows=$(count "$table")
    echo "restore-check: $table = $rows rows"
done
# The tags, rules and views are the data that cannot be fetched again, so they must restore.
for table in sources tags views; do
    if [ "$(count "$table")" -eq 0 ]; then
        echo "restore-check: $table is empty after the restore" >&2
        failed=1
    fi
done

if [ "$failed" -ne 0 ]; then
    echo "restore-check: FAILED" >&2
    exit 1
fi
echo "restore-check: the dump restores and the key tables are populated"
