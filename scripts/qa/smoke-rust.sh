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
curl -fsS -H 'Authorization: Bearer smoke-admin' "http://127.0.0.1:$port/admin/api/v1/upstream-slots" | jq -e '.slots|length == 2 and all(.[]; (.profiles|length) == 8)' >/dev/null
curl -fsS -H 'Authorization: Bearer smoke-admin' "http://127.0.0.1:$port/admin/api/v1/model-capabilities" | jq -e '.models|length == 8' >/dev/null
curl -fsS -H 'Authorization: Bearer smoke-admin' "http://127.0.0.1:$port/admin/api/v1/generation-readiness" | jq -e '.ready == true and .available_profiles == 0' >/dev/null
curl -fsS -H 'Authorization: Bearer smoke-admin' -H 'Content-Type: application/json' \
  -d '{"label":"smoke","scopes":["models:read","chat:write","embeddings:write","images:write","audio:write","media:write"]}' \
  "http://127.0.0.1:$port/admin/api/v1/downstream-credentials" >"$work/client.json"
token=$(jq -er .token "$work/client.json")
curl -fsS -H "Authorization: Bearer $token" "http://127.0.0.1:$port/v1/models" | jq -e '.data|length == 8' >/dev/null
for _ in 1 2 3 4; do
  curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d '{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"distribution"}]}' \
    "http://127.0.0.1:$port/v1/chat/completions" | jq -e '.object == "chat.completion"' >/dev/null
done
curl -fsS -H 'Authorization: Bearer smoke-admin' "http://127.0.0.1:$port/admin/api/v1/upstream-keys" \
  | jq -e '[.items[].request_count] | length == 2 and min >= 2' >/dev/null
curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2","metadata":{"force_first_upstream_failure":true},"messages":[{"role":"user","content":"failover"}]}' \
  "http://127.0.0.1:$port/v1/chat/completions" | jq -e '.object == "chat.completion"' >/dev/null
curl -fsS -H 'Authorization: Bearer smoke-admin' "http://127.0.0.1:$port/admin/api/v1/upstream-keys" \
  | jq -e '[.items[].failure_count] | max >= 1' >/dev/null
curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2","stream":true,"messages":[{"role":"user","content":"smoke"}]}' \
  "http://127.0.0.1:$port/v1/chat/completions" | grep -Fx 'data: [DONE]' >/dev/null
for path in /v1/embeddings /v1/images/generations /v1/audio/speech /v1/videos/generations /v1/nvidia/inference; do
  payload='{"model":"nvidia/nvclip","input":"smoke"}'
  case "$path" in
    /v1/images/generations) payload='{"model":"black-forest-labs/flux.1-kontext-dev","prompt":"smoke"}' ;;
    /v1/audio/speech) payload='{"model":"nvidia/magpie-tts-multilingual","input":"smoke"}' ;;
    /v1/videos/generations) payload='{"model":"stabilityai/stable-video-diffusion","input_reference":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}' ;;
    /v1/nvidia/inference) payload='{"model":"stabilityai/stable-video-diffusion","input":{"image":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}}' ;;
  esac
  if [[ "$path" == /v1/audio/speech ]]; then
    curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
      -d "$payload" "http://127.0.0.1:$port$path" -o "$work/speech.wav"
    response=''
  else
    response=$(curl -fsS -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
      -d "$payload" "http://127.0.0.1:$port$path")
  fi
  case "$path" in
    /v1/embeddings) jq -e '.object == "list" and (.data[0].embedding | length) == 1024' <<<"$response" >/dev/null ;;
    /v1/images/generations) encoded=$(jq -er '.data[0].b64_json' <<<"$response"); test -n "$encoded"; printf '%s' "$encoded" | base64 -d >/dev/null ;;
    /v1/audio/speech) test "$(wc -c < "$work/speech.wav")" -ge 44; test "$(dd if="$work/speech.wav" bs=1 count=4 2>/dev/null)" = RIFF ;;
    /v1/videos/generations) jq -er '.data[0].b64_json' <<<"$response" | base64 -d >/dev/null ;;
    /v1/nvidia/inference) jq -er '.video' <<<"$response" | base64 -d >/dev/null ;;
  esac
done
test "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d '{not-json' "http://127.0.0.1:$port/v1/embeddings")" = 400
printf 'RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x00\x00\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00' >"$work/audio.wav"
transcription=$(curl -fsS -H "Authorization: Bearer $token" \
  -F 'model=nvidia/parakeet-ctc-1.1b' -F "file=@$work/audio.wav;type=audio/wav" \
  "http://127.0.0.1:$port/v1/audio/transcriptions")
jq -e '.text == "NVIDIA Build LB"' <<<"$transcription" >/dev/null
printf '%s\n' '{"status":"PASS","scope":"rust-gateway-mock","checks":["health","authority","two-key-custody","scoped-token","read-surfaces","distribution","failover","models","streaming","multimodal"]}'
