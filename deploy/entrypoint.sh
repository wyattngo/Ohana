#!/bin/sh
# Wait for postgres → exec command.
#
# KHÔNG chạy migration ở đây. Migration là job explicit (compose service `ohana-migrate`
# với profile `migrate`) — auto-migrate ở entrypoint = mỗi lần scale up 1 replica sẽ đá
# alembic một lần, race condition + double-lock rủi ro cao. Migration là hành động con
# người, không phải side-effect của "container khởi động".

set -eu

DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-60}"

echo "entrypoint: waiting for postgres ${DB_HOST}:${DB_PORT} (timeout ${DB_WAIT_TIMEOUT}s)..."
i=0
until pg_isready -h "$DB_HOST" -p "$DB_PORT" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge "$DB_WAIT_TIMEOUT" ]; then
        echo "entrypoint: postgres KHÔNG lên trong ${DB_WAIT_TIMEOUT}s — bỏ cuộc" >&2
        exit 1
    fi
    sleep 1
done
echo "entrypoint: postgres ok — exec $*"

exec "$@"
