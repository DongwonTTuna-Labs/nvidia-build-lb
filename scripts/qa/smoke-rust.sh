#!/usr/bin/env bash
set -Eeuo pipefail

port=${NBLB_SMOKE_PORT:-32463}
work=$(mktemp -d)
master=$(printf '0a%.0s' $(seq 1 32))
if [[ ! -x target/debug/nvidia-build-lb-gateway ]] \
  || find crates migrations Cargo.toml Cargo.lock -type f -newer target/debug/nvidia-build-lb-gateway -print -quit 2>/dev/null | grep -q .; then
  cargo build -p nvidia-build-lb-gateway --bin nvidia-build-lb-gateway
fi
NVIDIA_BUILD_LB_PUBLIC_PORT=$port NVIDIA_BUILD_LB_ADMIN_TOKEN=smoke-admin NBLB_VAULT_MASTER_KEY=$master \
  NBLB_VAULT_PATH=$work/vault.json NBLB_UPSTREAM_URL=mock://local target/debug/nvidia-build-lb-gateway >"$work/gateway.log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -sS "http://127.0.0.1:$port/health" >/dev/null && break; sleep .1; done
curl -sS -o "$work/health" -w '%{http_code}' "http://127.0.0.1:$port/health" | grep -Fx 503 >/dev/null
for pair in aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; do
  first=${pair:0:1}
  key_id=$(curl -fsS -H 'Authorization: Bearer smoke-admin' -H 'Content-Type: application/json' \
    -d "{\"label\":\"key-$first\",\"credential\":\"nvapi-$pair\"}" \
    "http://127.0.0.1:$port/admin/api/v1/upstream-keys" | jq -er .id)
  curl -fsS -H 'Authorization: Bearer smoke-admin' -X POST \
    "http://127.0.0.1:$port/admin/api/v1/upstream-keys/$key_id/probe" \
    | jq -e '.probe_status == "valid"' >/dev/null
  curl -fsS -H 'Authorization: Bearer smoke-admin' -H 'Content-Type: application/json' \
    -d '{"enabled":true}' "http://127.0.0.1:$port/admin/api/v1/upstream-keys/$key_id/state" >/dev/null
done
curl -fsS "http://127.0.0.1:$port/health" | jq -e '.ready == true and .traffic_ready == true and .eligible_keys == 2' >/dev/null
curl -fsS -H 'Authorization: Bearer smoke-admin' -H 'Content-Type: application/json' \
  -d '{"label":"smoke","scopes":["models:read","chat:write","embeddings:write","images:write","audio:write","media:write"]}' \
  "http://127.0.0.1:$port/admin/api/v1/downstream-credentials" >"$work/client.json"
token=$(jq -er .token "$work/client.json")
curl -fsS -H "Authorization: Bearer $token" "http://127.0.0.1:$port/v1/models" | jq -e '.data|length == 8' >/dev/null
curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2","stream":true,"messages":[{"role":"user","content":"smoke"}]}' \
  "http://127.0.0.1:$port/v1/chat/completions" | grep -Fx 'data: [DONE]' >/dev/null
for path in /v1/embeddings /v1/images/generations /v1/audio/speech /v1/nvidia/inference; do
  payload='{"model":"nvidia/nvclip","input":"smoke"}'
  case "$path" in
    /v1/images/generations) payload='{"model":"black-forest-labs/flux.1-kontext-dev","prompt":"smoke"}' ;;
    /v1/audio/speech) payload='{"model":"nvidia/magpie-tts-multilingual","input":"smoke"}' ;;
    /v1/nvidia/inference) payload='{"model":"stabilityai/stable-video-diffusion","input":{"image":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}}' ;;
  esac
  curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "$payload" "http://127.0.0.1:$port$path" >/dev/null
done
test "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d '{"model":"stabilityai/stable-video-diffusion","input_reference":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}' "http://127.0.0.1:$port/v1/videos/generations")" = 404
test "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d '{not-json' "http://127.0.0.1:$port/v1/embeddings")" = 400
printf 'synthetic-audio' >"$work/audio.bin"
test "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" \
  -F 'model=nvidia/magpie-tts-multilingual' -F "file=@$work/audio.bin;type=audio/wav" \
  "http://127.0.0.1:$port/v1/audio/transcriptions")" = 422
printf '%s\n' '{"status":"PASS","scope":"rust-gateway-mock","checks":["health","two-key-custody","scoped-token","models","streaming","multimodal"]}'
