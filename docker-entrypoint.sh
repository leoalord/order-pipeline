#!/bin/sh
set -eu
# API runs Alembic. Restaurant has no DSN. Worker has a DSN but the API already
# migrated — both set SKIP_MIGRATIONS=1.
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  alembic upgrade head
fi
exec "$@"
