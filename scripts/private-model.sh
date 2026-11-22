#!/usr/bin/env bash
# The scorer a deploy runs: the open baseline in src/unfret/scoring/, or a retrained one from the private model store.
# Used by the deploy workflows (.forgejo/workflows/cd.yaml, deploy-production.yaml); README, "The model".
#
#   scripts/private-model.sh fetch DIR             the store's current release into DIR (model.json, model-quick.json);
#                                                  DIR stays empty (the baseline) without MODEL_READ_TOKEN or when
#                                                  current.json names none
#   scripts/private-model.sh expected DIR          the versions a server with DIR mounted runs: "<full> <quick>"
#   scripts/private-model.sh install DEST DIR      DIR onto the server, where config/deploy.DEST.yml mounts it
#                                                  (/home/kamal/unfret-model/DEST → /app/private-model, read-only)
#   scripts/private-model.sh env DIR               the UNFRET_MODEL_PATH / UNFRET_MODEL_QUICK_PATH lines for DIR
#   scripts/private-model.sh verify URL DIR        wait until URL/v1/health reports the expected versions
#
# Settings (environment): MODEL_READ_TOKEN (a read-only token for the store; never printed), MODEL_STORE
# (owner/repo, default kroshtan/unfret-corpus), MODEL_STORE_REF (default main), FORGEJO_URL (default
# https://git.kroshtan.com), MODEL_HOST (default kamal@git.kroshtan.com).
set -euo pipefail

STORE="${MODEL_STORE:-kroshtan/unfret-corpus}"
REF="${MODEL_STORE_REF:-main}"
FORGE="${FORGEJO_URL:-https://git.kroshtan.com}"
HOST="${MODEL_HOST:-kamal@git.kroshtan.com}"
MOUNT=/app/private-model
SCORING="$(cd "$(dirname "$0")/.." && pwd)/src/unfret/scoring"
SSH_OPTIONS=(-o BatchMode=yes -o UpdateHostKeys=no -o CheckHostIP=no)

die() {
  echo "::error::$*" >&2
  exit 1
}

# A JSON file's top-level key ("" for null or missing).
json_key() {
  ruby -rjson -e 'v = JSON.parse(File.read(ARGV[0]))[ARGV[1]]; print(v.nil? ? "" : v.to_s)' "$1" "$2"
}

# One file of the store at REF, into $2; fails on any HTTP error (a missing token or file reads as 404).
store_get() {
  local status
  status="$(curl -sS -o "$2" -w '%{http_code}' -m 30 -H "Authorization: token $MODEL_READ_TOKEN" \
    "$FORGE/api/v1/repos/$STORE/media/$1?ref=$REF")" || die "model store: could not reach $FORGE"
  [[ "$status" == 200 ]] || die "model store: $STORE/$1 at $REF answered $status (is MODEL_READ_TOKEN allowed to read it?)"
}

fetch() {
  local dir="$1" pointer slot file version
  rm -rf "$dir" && mkdir -p "$dir"
  if [[ -z "${MODEL_READ_TOKEN:-}" ]]; then
    echo "::notice::MODEL_READ_TOKEN is not set: the open baseline scorer runs"
    return
  fi
  pointer="$(mktemp)"
  store_get models/current.json "$pointer"
  for slot in model model_quick; do
    version="$(json_key "$pointer" "$slot")"
    [[ -n "$version" ]] || continue
    [[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "model store: current.json names '$version' as $slot"
    [[ "$slot" == model ]] && file="$dir/model.json" || file="$dir/model-quick.json"
    store_get "models/$version/model.json" "$file"
    [[ "$(json_key "$file" version)" == "$version" ]] || die "model store: models/$version/model.json is not $version"
    echo "$slot: $version (sha256 $(sha256sum "$file" | cut -c1-16)…)"
  done
  rm -f "$pointer"
  [[ -n "$(ls -A "$dir")" ]] || echo "::notice::the model store's current.json names no release: the open baseline scorer runs"
}

expected() {
  local dir="$1" full quick
  full="$(json_key "${dir}/model.json" version 2> /dev/null || json_key "$SCORING/model.json" version)"
  if [[ -f "$dir/model-quick.json" ]]; then
    quick="$(json_key "$dir/model-quick.json" version)"
  else
    quick="$(json_key "$SCORING/model-quick.json" version)"
  fi
  echo "$full $quick"
}

env_lines() {
  local dir="$1"
  if [[ -f "$dir/model.json" ]]; then echo "UNFRET_MODEL_PATH=$MOUNT/model.json"; else echo "UNFRET_MODEL_PATH="; fi
  if [[ -f "$dir/model-quick.json" ]]; then echo "UNFRET_MODEL_QUICK_PATH=$MOUNT/model-quick.json"; else echo "UNFRET_MODEL_QUICK_PATH="; fi
}

install() {
  local dest="$1" dir="$2"
  [[ "$dest" =~ ^(dev|production)$ ]] || die "install: unknown destination '$dest'"
  # Swapped in whole: a running container keeps the directory it mounted (and has its scorer in memory anyway).
  tar -C "$dir" -cf - . | ssh "${SSH_OPTIONS[@]}" "$HOST" -- "set -eu
    base=\$HOME/unfret-model; mkdir -p \"\$base\"; chmod 700 \"\$base\"
    new=\"\$base/$dest.new\"; rm -rf \"\$new\"; mkdir -m 755 \"\$new\"; tar -xf - -C \"\$new\"
    find \"\$new\" -type f -exec chmod 644 {} +
    rm -rf \"\$base/$dest.old\"; if [ -e \"\$base/$dest\" ]; then mv \"\$base/$dest\" \"\$base/$dest.old\"; fi
    mv \"\$new\" \"\$base/$dest\""
  echo "scorer files for $dest: $(cd "$dir" && ls | tr '\n' ' ')"
}

verify() {
  local url="$1" dir="$2" want got deadline=$((SECONDS + 180))
  want="$(expected "$dir")"
  until got="$(curl -fsS -m 10 "$url/v1/health" | ruby -rjson -e 'h = JSON.parse($stdin.read); print "#{h["model_version"]} #{h["quick_model_version"]}"')" \
    && [[ "$got" == "$want" ]]; do
    ((SECONDS < deadline)) || die "$url runs scorers '${got:-?}', expected '$want'"
    sleep 5
  done
  echo "$url runs scorers $want"
}

case "${1:-}" in
  fetch) fetch "${2:?DIR}" ;;
  expected) expected "${2:?DIR}" ;;
  install) install "${2:?DEST}" "${3:?DIR}" ;;
  env) env_lines "${2:?DIR}" ;;
  verify) verify "${2:?URL}" "${3:?DIR}" ;;
  *) die "usage: $0 fetch|expected|install|env|verify …" ;;
esac
