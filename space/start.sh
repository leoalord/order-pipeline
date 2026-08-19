#!/bin/sh
set -eu

HOME="${HOME:-/home/user}"
APP="${APP:-$HOME/app}"
PGDATA="${PGDATA:-$HOME/pgdata}"
PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | tail -n 1)"
if [ -n "$PG_BIN" ]; then
  PATH="$PG_BIN:$PATH"
fi
export PATH HOME PGDATA

mkdir -p "$HOME/data" "$PGDATA" /tmp/nginx_client_body /tmp/nginx_proxy \
  /tmp/nginx_fastcgi /tmp/nginx_uwsgi /tmp/nginx_scgi

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  initdb -D "$PGDATA" --auth-local=trust --auth-host=trust --username=user
  {
    echo "listen_addresses = '127.0.0.1'"
    echo "unix_socket_directories = '$PGDATA'"
    echo "port = 5432"
  } >> "$PGDATA/postgresql.conf"
fi

pg_ctl -D "$PGDATA" -l "$HOME/postgres.log" start
i=0
until pg_isready -h 127.0.0.1 -U user >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 50 ]; then
    echo "postgres did not become ready" >&2
    tail -n 50 "$HOME/postgres.log" >&2 || true
    exit 1
  fi
  sleep 0.2
done
if ! psql -h 127.0.0.1 -U user -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='order_pipeline'" | grep -q 1; then
  createdb -h 127.0.0.1 -U user order_pipeline
fi

DSN="postgresql+psycopg://user@127.0.0.1:5432/order_pipeline"
export API_DATABASE_URL="$DSN"
export API_RESTAURANT_ADMIN_URL="http://127.0.0.1:8081"
export API_COURIER_ADMIN_URL="http://127.0.0.1:8082"
export WORKER_DATABASE_URL="$DSN"
export WORKER_RESTAURANT_BASE_URL="http://127.0.0.1:8081"
export WORKER_COURIER_BASE_URL="http://127.0.0.1:8082"
export RSIM_LEDGER_PATH="$HOME/data/restaurant.sqlite"
export CSIM_LEDGER_PATH="$HOME/data/courier.sqlite"
export LOADGEN_API_BASE_URL="http://127.0.0.1:8000"
export LOADGEN_RESTAURANT_ADMIN_URL="http://127.0.0.1:8081"

cd "$APP"
alembic upgrade head

restaurant-sim &
courier-sim &
uvicorn order_pipeline.api.app:app --host 127.0.0.1 --port 8000 &
WORKER_HEALTH_PORT=8083 worker &
WORKER_HEALTH_PORT=8084 worker &
loadgen &

wait_http() {
  url="$1"
  i=0
  until curl -sf "$url" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 80 ]; then
      echo "timeout waiting for $url" >&2
      exit 1
    fi
    sleep 0.25
  done
}

wait_http http://127.0.0.1:8000/health
wait_http http://127.0.0.1:8081/health
wait_http http://127.0.0.1:8082/health
wait_http http://127.0.0.1:8090/health

exec nginx -c "$APP/space/nginx.conf" -g "daemon off;"
