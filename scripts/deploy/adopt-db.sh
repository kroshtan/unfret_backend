#!/usr/bin/env bash
# Copy an older database accessory's data into its successor, once.
#
# Run after the new accessory is booted and before `kamal deploy`, with the old app already stopped (free-host.sh),
# so nothing writes to the old database while it is copied. When <old container> exists and <new container> has no
# tables yet, it starts the old one if stopped, pipes `pg_dump --no-owner --no-acl` from it into the new one in a
# single transaction (on the server: the data never leaves it), and stops, never removes, the old container. Each
# side's user and database come from its own POSTGRES_USER / POSTGRES_DB. Without an old container, or once the new
# database has tables, it does nothing, so every deploy can run it. With --require-data it fails instead of letting
# a deploy start on an empty database (production: an empty one would hide every balance).
#
# Usage: scripts/deploy/adopt-db.sh <old container> <new container> [--require-data]
#   e.g. scripts/deploy/adopt-db.sh <previous dev db> unfret-dev-db
set -euo pipefail

(($# == 2)) || [[ $# == 3 && "$3" == --require-data ]] \
  || { echo "usage: $0 <old container> <new container> [--require-data]" >&2; exit 2; }
server="${DEPLOY_SERVER:-kamal@git.kroshtan.com}"

# Same SSH options as free-host.sh, for the same reason.
ssh -o BatchMode=yes -o UpdateHostKeys=no -o CheckHostIP=no -o StrictHostKeyChecking=yes "$server" \
  bash -s -- "$1" "$2" "${3:-}" << 'REMOTE'
set -euo pipefail
old="$1"
new="$2"
require_data="${3:-}"  # ssh drops an empty argument, so it may be missing

env_of() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" | sed -n "s/^$2=//p"; }
tables_in() {
  docker exec "$1" psql -U "$(env_of "$1" POSTGRES_USER)" -d "$(env_of "$1" POSTGRES_DB)" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
}
wait_ready() {
  # Over TCP: the image's init-time server listens on the socket only, so this waits for the real one.
  for _ in $(seq 60); do
    docker exec "$1" pg_isready -q -h 127.0.0.1 && return 0
    sleep 1
  done
  echo "::error::$1 did not accept connections within a minute"
  return 1
}

wait_ready "$new"
tables="$(tables_in "$new")"
if ((tables > 0)); then
  echo "$new has $tables tables already: nothing to adopt"
  exit 0
fi
if ! docker container inspect "$old" > /dev/null 2>&1; then
  if [[ -n "$require_data" ]]; then
    echo "::error::$new is empty and there is no $old to copy from; refusing to deploy on an empty database"
    exit 1
  fi
  echo "no $old container: nothing to adopt"
  exit 0
fi

new_user="$(env_of "$new" POSTGRES_USER)"
new_db="$(env_of "$new" POSTGRES_DB)"
old_user="$(env_of "$old" POSTGRES_USER)"
old_db="$(env_of "$old" POSTGRES_DB)"
[[ "$(docker inspect -f '{{.State.Running}}' "$old")" == true ]] || docker start "$old" > /dev/null
wait_ready "$old"
echo "::notice::copying $old ($old_user/$old_db) into the empty $new ($new_user/$new_db)"
docker exec "$old" pg_dump -U "$old_user" -d "$old_db" --no-owner --no-acl \
  | docker exec -i "$new" psql -U "$new_user" -d "$new_db" -q -v ON_ERROR_STOP=1 --single-transaction > /dev/null
tables="$(tables_in "$new")"
if ((tables == 0)) && [[ -n "$require_data" ]]; then
  echo "::error::$old held no tables, so $new is still empty; refusing to deploy on an empty database"
  exit 1
fi
echo "$new now has $tables tables"
docker stop "$old" > /dev/null
echo "$old stopped (not removed)"
REMOTE
