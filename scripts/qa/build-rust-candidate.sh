#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

EVIDENCE_DIR=${EVIDENCE_DIR:-.omo/evidence/rust-candidate}
RUN_TAG=${NBLB_CANDIDATE_TAG:-nvidia-build-lb:rust-candidate-${GITHUB_SHA:-local}}
POSTGRES_TAG=${NBLB_POSTGRES_TAG:-nvidia-build-lb:postgres-candidate-${GITHUB_SHA:-local}}
FIXTURE_TAG=${NBLB_QA_FIXTURE_TAG:-nvidia-build-lb:qa-fixtures-${GITHUB_SHA:-local}}
CLIENT_DIR=$(mktemp -d)
mkdir -p "$EVIDENCE_DIR"

command -v docker >/dev/null
command -v jq >/dev/null

build_flags=(--pull --provenance=false --load)
if [[ "${NBLB_NO_CACHE:-1}" == 1 ]]; then
  build_flags+=(--no-cache)
fi

# Use the affected-scope gate during iteration. CI/release callers leave
# NBLB_FAST unset and retain the complete workspace/admin checks.
if [[ "${NBLB_FAST:-0}" == 1 ]]; then
  cargo fmt --all -- --check
  cargo test -p nvidia-build-lb-core
  cargo check -p nvidia-build-lb-gateway --bins
  npm --prefix apps/admin ci --ignore-scripts --no-audit --no-fund
  npm --prefix apps/admin run check
else
  cargo fmt --all -- --check
  cargo test --workspace
  npm --prefix apps/admin ci --ignore-scripts --no-audit --no-fund
  npm --prefix apps/admin run check
  npm --prefix apps/admin run format
  npm --prefix apps/admin run knip
fi

python3 scripts/qa/source_manifest.py --root "$ROOT" --output "$EVIDENCE_DIR/source-manifest.json"
source_sha256=$(jq -er '.source_tree_sha256' "$EVIDENCE_DIR/source-manifest.json")

docker buildx build "${build_flags[@]}" \
  --file Dockerfile.rust \
  --label "nvidia-build-lb.source-sha256=$source_sha256" \
  --tag "$RUN_TAG" .
docker buildx build "${build_flags[@]}" \
  --file docker/postgres.Dockerfile \
  --label "nvidia-build-lb.source-sha256=$source_sha256" \
  --tag "$POSTGRES_TAG" .
docker buildx build "${build_flags[@]}" \
  --file Dockerfile.qa-fixtures \
  --label "nvidia-build-lb.source-sha256=$source_sha256" \
  --tag "$FIXTURE_TAG" .

# Never publish a receipt for an image built from a source tree that changed
# while the three immutable images were compiling.
post_build_manifest=$(mktemp)
python3 scripts/qa/source_manifest.py --root "$ROOT" --output "$post_build_manifest"
if ! cmp -s "$EVIDENCE_DIR/source-manifest.json" "$post_build_manifest"; then
  printf '%s\n' 'source_manifest_drift_during_build' >&2
  rm -f "$post_build_manifest"
  exit 1
fi
rm -f "$post_build_manifest"

container_name="nblb-rust-image-smoke-$$"
container_port=${NBLB_IMAGE_SMOKE_PORT:-32567}
secret_dir=$(mktemp -d)
cleanup_image_smoke() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rm -rf "$secret_dir"
  rm -rf "$CLIENT_DIR"
}
trap cleanup_image_smoke EXIT
printf 'nblb_admin_%064d' 1 > "$secret_dir/admin_token"
printf '%064d' 2 > "$secret_dir/vault_master_key"
printf '%064d' 3 > "$secret_dir/db_password"
# Bind mounts preserve host ownership. These disposable QA inputs are made
# readable by container root; the entrypoint copies them into 0400 runtime
# files owned by UID 65532 before dropping privileges.
chmod 0444 "$secret_dir"/*
docker run -d --name "$container_name" \
  --publish "127.0.0.1:${container_port}:2456" \
  --read-only --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add SETPCAP \
  --security-opt no-new-privileges:true \
  --tmpfs /run/nvidia-build-lb/secrets:rw,noexec,nosuid,nodev,size=64k,mode=0700,uid=0,gid=0 \
  --mount "type=bind,source=$secret_dir/admin_token,target=/run/canonical-secrets/admin_token,readonly" \
  --mount "type=bind,source=$secret_dir/vault_master_key,target=/run/canonical-secrets/vault_master_key,readonly" \
  --mount "type=bind,source=$secret_dir/db_password,target=/run/canonical-secrets/db_password,readonly" \
  --tmpfs /var/lib/nvidia-build-lb:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=65532,gid=65532 \
  -e NVIDIA_BUILD_LB_BIND_PORT=2456 -e NVIDIA_BUILD_LB_PUBLIC_PORT="${container_port}" \
  -e NBLB_QA_ALLOW_HOST_SECRET_OWNER=1 \
  -e NBLB_UPSTREAM_URL=mock://local \
  "$RUN_TAG" >/dev/null
for _ in $(seq 1 60); do
  curl -sS "http://127.0.0.1:${container_port}/health" >/dev/null 2>&1 && break
  sleep .2
done
test "$(curl -sS -o /dev/null -w '%{http_code}' -H "Host: 127.0.0.1:${container_port}" "http://127.0.0.1:${container_port}/admin/")" = 200
for pair in aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; do
  first=${pair:0:1}
  key_id=$(curl -fsS -H "Host: 127.0.0.1:${container_port}" -H 'Authorization: Bearer nblb_admin_0000000000000000000000000000000000000000000000000000000000000001' \
    -H 'Content-Type: application/json' -d "{\"label\":\"key-$first\",\"credential\":\"nvapi-$pair\"}" \
    "http://127.0.0.1:${container_port}/admin/api/v1/upstream-keys" | jq -er .id)
  curl -fsS -H "Host: 127.0.0.1:${container_port}" -H 'Authorization: Bearer nblb_admin_0000000000000000000000000000000000000000000000000000000000000001' -X POST \
    "http://127.0.0.1:${container_port}/admin/api/v1/upstream-keys/$key_id/probe" \
    | jq -e '.probe_status == "valid"' >/dev/null
  curl -fsS -H "Host: 127.0.0.1:${container_port}" -H 'Authorization: Bearer nblb_admin_0000000000000000000000000000000000000000000000000000000000000001' -H 'Content-Type: application/json' \
    -d '{"enabled":true}' "http://127.0.0.1:${container_port}/admin/api/v1/upstream-keys/$key_id/state" >/dev/null
done
curl -fsS "http://127.0.0.1:${container_port}/health" | jq -e '.traffic_ready == true' >/dev/null
# The runtime deliberately runs as UID 65532 and owns the copied secret
# material. Inspect it as that same non-privileged identity; using Docker's
# default root here would lack CAP_DAC_OVERRIDE because the container drops
# that capability by design.
test "$(docker exec --user 65532 "$container_name" stat -c '%u:%g:%a' /run/nvidia-build-lb/secrets/admin_token)" = 65532:65532:400

# Exercise the release image pair through the same compose topology used by
# deployment. The fake upstream is TLS-backed and reachable through the
# internal integrate.api.nvidia.com alias; this is deliberately separate from
# the mock unit smoke above so endpoint wiring is observable.
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -subj '/CN=nblb-qa-root' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout "$secret_dir/ca_key" -out "$secret_dir/ca_cert" >/dev/null 2>&1
openssl req -newkey rsa:2048 -sha256 -nodes \
  -subj '/CN=integrate.api.nvidia.com' \
  -keyout "$secret_dir/server_key" -out "$secret_dir/server.csr" >/dev/null 2>&1
openssl x509 -req -sha256 -days 1 \
  -in "$secret_dir/server.csr" \
  -CA "$secret_dir/ca_cert" -CAkey "$secret_dir/ca_key" -CAcreateserial \
  -extfile <(printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature,keyEncipherment' 'extendedKeyUsage=serverAuth' 'subjectAltName=DNS:integrate.api.nvidia.com,DNS:ai.api.nvidia.com,DNS:api.nvcf.nvidia.com,DNS:877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com') \
  -out "$secret_dir/server_cert" >/dev/null 2>&1
chmod 0444 "$secret_dir"/*
compose_project="nblb-candidate-${BASHPID}"
compose_cleanup() {
  docker compose -p "$compose_project" -f compose.qa.yml down -v --remove-orphans >/dev/null 2>&1 || true
}
# Keep the first image smoke container cleanup active while the Compose
# candidate runs. A later trap must not orphan the port-bound container when
# Compose fails before the explicit teardown below.
trap 'compose_cleanup; cleanup_image_smoke' EXIT
export NBLB_CANDIDATE_IMAGE="$RUN_TAG"
export NBLB_QA_FIXTURE_IMAGE="$FIXTURE_TAG"
export NBLB_POSTGRES_IMAGE="$POSTGRES_TAG"
export NBLB_QA_SECRET_DIR="$secret_dir"
export NBLB_QA_RUN_ID="$compose_project"
export NBLB_QA_PORT=${NBLB_QA_PORT:-32568}
export NBLB_QA_PUBLIC_PORT=${NBLB_QA_PUBLIC_PORT:-$NBLB_QA_PORT}
docker compose -p "$compose_project" -f compose.qa.yml up -d app loopback
for _ in $(seq 1 60); do
  health_code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${NBLB_QA_PORT}/health" 2>/dev/null || true)
  if [ "$health_code" = 200 ] || [ "$health_code" = 503 ]; then
    break
  fi
  sleep .5
done
for pair in aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; do
  first=${pair:0:1}
  key_id=$(curl -fsS -H "Host: 127.0.0.1:${NBLB_QA_PORT}" -H 'Authorization: Bearer nblb_admin_0000000000000000000000000000000000000000000000000000000000000001' \
    -H 'Content-Type: application/json' \
    -d "{\"label\":\"compose-$first\",\"credential\":\"nvapi-$pair\"}" \
    "http://127.0.0.1:${NBLB_QA_PORT}/admin/api/v1/upstream-keys" | jq -er .id)
  curl -fsS -H "Host: 127.0.0.1:${NBLB_QA_PORT}" -H 'Authorization: Bearer nblb_admin_0000000000000000000000000000000000000000000000000000000000000001' -X POST \
    "http://127.0.0.1:${NBLB_QA_PORT}/admin/api/v1/upstream-keys/$key_id/probe" \
    | jq -e '.probe_status == "valid"' >/dev/null
  curl -fsS -H "Host: 127.0.0.1:${NBLB_QA_PORT}" -H 'Authorization: Bearer nblb_admin_0000000000000000000000000000000000000000000000000000000000000001' -H 'Content-Type: application/json' \
    -d '{"enabled":true}' "http://127.0.0.1:${NBLB_QA_PORT}/admin/api/v1/upstream-keys/$key_id/state" >/dev/null
done
curl -fsS "http://127.0.0.1:${NBLB_QA_PORT}/health" | jq -e '.traffic_ready == true' >/dev/null
curl -fsS -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"compose"}]}' \
  "http://127.0.0.1:${NBLB_QA_PORT}/v1/chat/completions" | jq -e '.object == "chat.completion"' >/dev/null
image_data_url='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
vision_payload=$(jq -cn --arg image "$image_data_url" \
  '{model:"nvidia/vila",messages:[{role:"user",content:[{type:"text",text:"compose"},{type:"image_url",image_url:{url:$image}}]}]}')
printf '%s' "$vision_payload" | curl -fsS -H 'Content-Type: application/json' --data-binary @- \
  "http://127.0.0.1:${NBLB_QA_PORT}/v1/chat/completions" | jq -e '.object == "chat.completion"' >/dev/null
curl -fsS -H 'Content-Type: application/json' \
  -d '{"model":"nvidia/nvclip","input":"compose"}' \
  "http://127.0.0.1:${NBLB_QA_PORT}/v1/embeddings" | jq -e '.object == "list"' >/dev/null
printf '%s' 'audio' > "$CLIENT_DIR/qa-audio.wav"
curl -fsS -F 'model=nvidia/parakeet-ctc-1.1b' \
  -F "file=@$CLIENT_DIR/qa-audio.wav;type=audio/wav" \
  "http://127.0.0.1:${NBLB_QA_PORT}/v1/audio/transcriptions" \
  | jq -e '.text == "candidate fake transcription"' >/dev/null
curl -fsS -H 'Content-Type: application/json' \
  -d '{"model":"nvidia/magpie-tts-multilingual","input":"compose","voice":"Magpie-Multilingual.EN-US.Aria"}' \
  -o "$CLIENT_DIR/compose-audio.wav" \
  -w '%{content_type}' "http://127.0.0.1:${NBLB_QA_PORT}/v1/audio/speech" \
  | grep -Fiq 'audio/wav'
curl -fsS -H 'Content-Type: application/json' \
  -d '{"model":"black-forest-labs/flux.1-kontext-dev","prompt":"a green square"}' \
  "http://127.0.0.1:${NBLB_QA_PORT}/v1/images/generations" \
  | jq -e '.data[0].b64_json | startswith("/9j/")' >/dev/null
curl -fsS -H 'Content-Type: application/json' \
  -d '{"model":"stabilityai/stable-video-diffusion","input":{"image":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}}' \
  "http://127.0.0.1:${NBLB_QA_PORT}/v1/nvidia/inference" \
  | jq -e '.video == "AAAAEGZ0eXBpc29tAAACAAAAAAhtb292"' >/dev/null
compose_cleanup
trap cleanup_image_smoke EXIT

scripts/qa/smoke-rust.sh
scripts/qa/smoke-rust-postgres.sh

image_digest=$(docker image inspect --format '{{.Id}}' "$RUN_TAG")
postgres_image_digest=$(docker image inspect --format '{{.Id}}' "$POSTGRES_TAG")
fixture_image_digest=$(docker image inspect --format '{{.Id}}' "$FIXTURE_TAG")
jq -n \
  --arg status PASS \
  --arg image_digest "$image_digest" \
  --arg postgres_image_digest "$postgres_image_digest" \
  --arg fixture_image_digest "$fixture_image_digest" \
  --arg source_sha256 "$source_sha256" \
  '{schema_version:1,status:$status,image_digest:$image_digest,postgres_image_digest:$postgres_image_digest,fixture_image_digest:$fixture_image_digest,source_tree_sha256:$source_sha256,checks:{rust_workspace:true,svelte_admin:true,compose_image_build:true,mock_smoke:true,postgres_smoke:true}}' \
  > "$EVIDENCE_DIR/candidate.json"
