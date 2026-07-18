#!/usr/bin/env bash
set -Eeuo pipefail

port=${NBLB_SMOKE_PORT:-32464}
pg_port=${NBLB_PG_SMOKE_PORT:-35440}
work=$(mktemp -d)
name="nblb-pg-smoke-$$"
master=$(printf '0a%.0s' $(seq 1 32))
gateway_pid=''

cleanup() {
  if [[ -n "$gateway_pid" ]]; then
    kill "$gateway_pid" 2>/dev/null || true
    wait "$gateway_pid" 2>/dev/null || true
  fi
  docker stop "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

if [[ ! -x target/debug/nblb-migrate || ! -x target/debug/nvidia-build-lb-gateway ]] \
  || find crates migrations Cargo.toml Cargo.lock -type f -newer target/debug/nvidia-build-lb-gateway -print -quit 2>/dev/null | grep -q .; then
  cargo build -p nvidia-build-lb-gateway --bins
fi

docker run --rm -d --name "$name" \
  -e POSTGRES_USER=nvidia_build_lb \
  -e POSTGRES_PASSWORD=targeted-pass \
  -e POSTGRES_DB=nvidia_build_lb \
  -p "127.0.0.1:${pg_port}:5432" \
  postgres:17-alpine >/dev/null
for _ in $(seq 1 80); do
  if docker exec "$name" pg_isready -h 127.0.0.1 -U nvidia_build_lb -d nvidia_build_lb >/dev/null 2>&1; then
    break
  fi
  sleep .25
done

database_url="postgres://nvidia_build_lb:targeted-pass@127.0.0.1:${pg_port}/nvidia_build_lb"
NBLB_DATABASE_URL="$database_url" target/debug/nblb-migrate
NVIDIA_BUILD_LB_PUBLIC_PORT="$port" \
  NBLB_DATABASE_URL="$database_url" \
  NBLB_VAULT_MASTER_KEY="$master" \
  NBLB_VAULT_PATH="$work/vault.json" \
  NBLB_UPSTREAM_URL=mock://local \
  NVIDIA_BUILD_LB_ADMIN_TOKEN=targeted-admin \
  target/debug/nvidia-build-lb-gateway >"$work/gateway.log" 2>&1 &
gateway_pid=$!
for _ in $(seq 1 80); do
  curl -sS "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break
  sleep .1
done
test "$(curl -sS -o "$work/health" -w '%{http_code}' "http://127.0.0.1:$port/health")" = 503

for pair in aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; do
  first=${pair:0:1}
  curl -fsS -H 'Authorization: Bearer targeted-admin' -H 'Content-Type: application/json' \
    -d "{\"label\":\"key-$first\",\"credential\":\"nvapi-$pair\"}" \
    "http://127.0.0.1:$port/admin/api/v1/upstream-keys" >/dev/null
done
client=$(curl -fsS -H 'Authorization: Bearer targeted-admin' -H 'Content-Type: application/json' \
  -d '{"label":"targeted","scopes":["models:read","chat:write"]}' \
  "http://127.0.0.1:$port/admin/api/v1/downstream-credentials")
token=$(printf '%s' "$client" | jq -er .token)
curl -fsS -H "Authorization: Bearer $token" "http://127.0.0.1:$port/v1/models" \
  | jq -e '.data|length == 7' >/dev/null
curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2","messages":[]}' \
  "http://127.0.0.1:$port/v1/chat/completions" | jq -e '.object == "chat.completion"' >/dev/null
curl -fsS -H 'Authorization: Bearer targeted-admin' "http://127.0.0.1:$port/admin/api/v1/evidence" \
  | jq -e '.source_of_truth == "postgresql" and .persisted_routing_profiles == 7' >/dev/null
counts=$(docker exec "$name" psql -U nvidia_build_lb -d nvidia_build_lb -Atc \
  'select (select count(*) from nblb.upstream_keys),(select count(*) from nblb.downstream_credentials),(select count(*) from nblb.routing_state)')
test "$counts" = '2|1|7'

kill "$gateway_pid"
wait "$gateway_pid" 2>/dev/null || true
gateway_pid=''
NVIDIA_BUILD_LB_PUBLIC_PORT="$port" \
  NBLB_DATABASE_URL="$database_url" \
  NBLB_VAULT_MASTER_KEY="$master" \
  NBLB_VAULT_PATH="$work/vault.json" \
  NBLB_UPSTREAM_URL=mock://local \
  NVIDIA_BUILD_LB_ADMIN_TOKEN=targeted-admin \
  target/debug/nvidia-build-lb-gateway >"$work/restart.log" 2>&1 &
gateway_pid=$!
for _ in $(seq 1 80); do
  curl -sS "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break
  sleep .1
done
curl -fsS -H 'Authorization: Bearer targeted-admin' \
  "http://127.0.0.1:$port/admin/api/v1/overview" \
  | jq -e '.upstream_keys.items|length == 2' >/dev/null
printf '%s\n' '{"status":"PASS","scope":"rust-gateway-postgres","checks":["migration","db-source-of-truth","two-key-restart","scoped-api"]}'
