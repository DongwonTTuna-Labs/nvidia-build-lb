#!/usr/bin/env bash
set -Eeuo pipefail

port=${NBLB_SMOKE_PORT:-32463}
work=$(mktemp -d)
master=$(printf '0a%.0s' $(seq 1 32))
NVIDIA_BUILD_LB_PUBLIC_PORT=$port NVIDIA_BUILD_LB_ADMIN_TOKEN=smoke-admin NBLB_VAULT_MASTER_KEY=$master \
  NBLB_VAULT_PATH=$work/vault.json NBLB_UPSTREAM_URL=mock://local target/debug/nvidia-build-lb-gateway >"$work/gateway.log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -sS "http://127.0.0.1:$port/health" >/dev/null && break; sleep .1; done
curl -sS -o "$work/health" -w '%{http_code}' "http://127.0.0.1:$port/health" | grep -Fx 503 >/dev/null
for pair in aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; do
  first=${pair:0:1}
  curl -fsS -H 'Authorization: Bearer smoke-admin' -H 'Content-Type: application/json' \
    -d "{\"label\":\"key-$first\",\"credential\":\"nvapi-$pair\"}" \
    "http://127.0.0.1:$port/admin/api/v1/upstream-keys" >/dev/null
done
curl -fsS -H 'Authorization: Bearer smoke-admin' -H 'Content-Type: application/json' \
  -d '{"label":"smoke","scopes":["models:read","chat:write","embeddings:write","images:write","audio:write","media:write"]}' \
  "http://127.0.0.1:$port/admin/api/v1/downstream-credentials" >"$work/client.json"
token=$(jq -er .token "$work/client.json")
curl -fsS -H "Authorization: Bearer $token" "http://127.0.0.1:$port/v1/models" | jq -e '.data|length == 7' >/dev/null
curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2","stream":true,"messages":[]}' \
  "http://127.0.0.1:$port/v1/chat/completions" | grep -Fx 'data: [DONE]' >/dev/null
for path in /v1/embeddings /v1/images/generations /v1/audio/speech /v1/audio/transcriptions /v1/videos/generations /v1/nvidia/inference; do
  curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d '{"model":"nvidia/multimodal","input":"smoke"}' "http://127.0.0.1:$port$path" >/dev/null
done
printf '%s\n' '{"status":"PASS","scope":"rust-gateway-mock","checks":["health","two-key-custody","scoped-token","models","streaming","multimodal"]}'
