#!/usr/bin/env bash
# Free a host on the server's kamal-proxy for the service about to be deployed to it.
#
# kamal-proxy gives a host to one service only, so `kamal deploy` fails while another service (an older or renamed
# one) still holds it. When <host> is routed to anything but <expected service>, this takes that route off and stops,
# never removes, the old service's app containers, so going back is `docker start` plus `kamal-proxy deploy`
# (docs/production-cutover.md). The old service is found by host, not by name. When the host is free or already the
# expected service's, it does nothing, so every deploy can run it. Traffic to the host is down from here until the new
# service passes its readiness check.
#
# Usage: scripts/deploy/free-host.sh <host> <expected kamal-proxy service>
#   e.g. scripts/deploy/free-host.sh dev.unfret.kroshtan.com unfret-web-dev
# Kamal names a role's proxy service <service>-<role>-<destination>: unfret-web-dev and unfret-web-production.
set -euo pipefail

(($# == 2)) || { echo "usage: $0 <host> <expected kamal-proxy service>" >&2; exit 2; }
server="${DEPLOY_SERVER:-kamal@git.kroshtan.com}"

# Plain OpenSSH next to Kamal's Net::SSH: never prompt and never touch known_hosts (the workflows re-install it after
# this anyway: an updated entry makes Net::SSH fail with HostKeyMismatch).
ssh -o BatchMode=yes -o UpdateHostKeys=no -o CheckHostIP=no -o StrictHostKeyChecking=yes "$server" \
  bash -s -- "$1" "$2" << 'REMOTE'
set -euo pipefail
host="$1"
expected="$2"

# Columns: Service Host Path Target State TLS, coloured; a service with several hosts lists them comma-separated.
holder="$(docker exec kamal-proxy kamal-proxy list | sed 's/\x1b\[[0-9;]*m//g' | awk -v host="$host" '
  $1 != "Service" { n = split($2, hosts, ","); for (i = 1; i <= n; i++) if (hosts[i] == host) { print $1, $4; exit } }')"
if [[ -z "$holder" ]]; then
  echo "$host is not routed: nothing to free"
  exit 0
fi
read -r service target <<< "$holder"
if [[ "$service" == "$expected" ]]; then
  echo "$host is routed to $expected already: nothing to free"
  exit 0
fi

# The routed container's Kamal labels name its service and destination; stop exactly those app containers.
container="${target%:*}"
label() { docker inspect -f "{{index .Config.Labels \"$1\"}}" "$container" 2> /dev/null || true; }
old_service="$(label service)"
old_destination="$(label destination)"

echo "::notice::$host is held by $service (Kamal service '${old_service:-?}'); taking it off for $expected"
docker exec kamal-proxy kamal-proxy remove "$service"
if [[ -z "$old_service" ]]; then
  echo "::warning::can't tell which service $container belongs to; stop its containers by hand if they still run"
  exit 0
fi
filters=(--filter "label=service=$old_service" --filter "label=role=web")
[[ -n "$old_destination" ]] && filters+=(--filter "label=destination=$old_destination")
ids="$(docker ps -q "${filters[@]}")"
if [[ -n "$ids" ]]; then
  echo "stopping (not removing), for a rollback:"
  docker ps "${filters[@]}" --format '  {{.Names}}'
  # shellcheck disable=SC2086 # one ID per word
  docker stop $ids > /dev/null
fi
REMOTE
