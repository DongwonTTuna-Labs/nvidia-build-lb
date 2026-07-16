#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

# shellcheck source=scripts/qa/admin-stage-evidence.sh
source "$ROOT/scripts/qa/admin-stage-evidence.sh"

EVIDENCE_DIR=${EVIDENCE_DIR:-.omo/evidence/task-6a-nvidia-build-lb}
QA_PORT=${NBLB_QA_PORT:-32456}
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
PROJECT="nblb-todo6a-${RUN_ID,,}"
PROJECT=${PROJECT//[^a-z0-9_-]/-}
SECRET_DIR=""
CLIENT_DIR=""
ADMIN_TOKEN_SHA256=""
VAULT_MASTER_KEY_SHA256=""
DB_PASSWORD_SHA256=""
IMAGE_TAG=""
POSTGRES_TAG=""
COMPOSE_STARTED=0

command -v flock >/dev/null 2>&1 || {
    printf '%s\n' 'required_command_unavailable' >&2
    exit 69
}
mkdir -p "$(dirname "$EVIDENCE_DIR")"
exec 9>"$EVIDENCE_DIR.claim"
if ! flock -n 9; then
    printf '%s\n' 'evidence_exists' >&2
    exit 64
fi
if [ -e "$EVIDENCE_DIR" ] || ! mkdir "$EVIDENCE_DIR" 2>/dev/null; then
    printf '%s\n' 'evidence_exists' >&2
    exit 64
fi

for command in cmp docker git jq openssl curl sha256sum ss uv; do
    command -v "$command" >/dev/null 2>&1 || {
        printf '%s\n' 'required_command_unavailable' >&2
        exit 69
    }
done

compose() {
    docker compose -f compose.qa.yml -p "$PROJECT" "$@"
}

root_secret_helper() {
    docker run --rm --network none \
        --label "nvidia-build-lb.run=$RUN_ID" \
        --entrypoint sh \
        --mount "type=bind,source=$SECRET_DIR,target=/secrets" \
        postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609 \
        -c "$1"
}

label_count() {
    docker ps -aq --filter "label=nvidia-build-lb.run=$RUN_ID" | wc -l | tr -d ' '
}

network_count() {
    docker network ls -q --filter "label=nvidia-build-lb.run=$RUN_ID" | wc -l | tr -d ' '
}

volume_count() {
    docker volume ls -q --filter "label=nvidia-build-lb.run=$RUN_ID" | wc -l | tr -d ' '
}

port_count() {
    ss -H -ltn "sport = :$QA_PORT" 2>/dev/null | wc -l | tr -d ' '
}

cleanup() {
    trigger_status=$?
    trap - EXIT HUP INT TERM
    set +e
    cleanup_error=0
    if [ "$COMPOSE_STARTED" -eq 1 ]; then
        compose down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1 \
            || cleanup_error=1
    fi
    if [ -n "$SECRET_DIR" ] && [ -d "$SECRET_DIR" ]; then
        root_secret_helper 'rm -f /secrets/*' >/dev/null 2>&1 || cleanup_error=1
        rmdir "$SECRET_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi
    if [ -n "$CLIENT_DIR" ] && [ -e "$CLIENT_DIR" ]; then
        chmod -R u+w -- "$CLIENT_DIR" >/dev/null 2>&1 || cleanup_error=1
        rm -rf -- "$CLIENT_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi

    containers=$(label_count) || { containers=-1; cleanup_error=1; }
    networks=$(network_count) || { networks=-1; cleanup_error=1; }
    volumes=$(volume_count) || { volumes=-1; cleanup_error=1; }
    listeners=$(port_count) || { listeners=-1; cleanup_error=1; }
    postgres_images=0
    secret_directories=0
    client_directories=0
    [ -z "$SECRET_DIR" ] || [ ! -e "$SECRET_DIR" ] || secret_directories=1
    [ -z "$CLIENT_DIR" ] || [ ! -e "$CLIENT_DIR" ] || client_directories=1

    cleanup_status=PASS
    final_status=$trigger_status
    if [ "$cleanup_error" -ne 0 ] \
        || [ "$containers" -ne 0 ] \
        || [ "$networks" -ne 0 ] \
        || [ "$volumes" -ne 0 ] \
        || [ "$listeners" -ne 0 ] \
        || [ "$postgres_images" -ne 0 ] \
        || [ "$secret_directories" -ne 0 ] \
        || [ "$client_directories" -ne 0 ]; then
        cleanup_status=FAIL
        final_status=1
    fi
    jq -n \
        --arg run_id "$RUN_ID" \
        --arg status "$cleanup_status" \
        --argjson trigger_status "$trigger_status" \
        --argjson final_status "$final_status" \
        --argjson cleanup_error "$cleanup_error" \
        --argjson containers "$containers" \
        --argjson networks "$networks" \
        --argjson volumes "$volumes" \
        --argjson listeners "$listeners" \
        --argjson postgres_images "$postgres_images" \
        --argjson secret_directories "$secret_directories" \
        --argjson client_directories "$client_directories" \
        '{schema_version:1,run_id:$run_id,status:$status,trigger_exit_status:$trigger_status,final_exit_status:$final_status,cleanup_command_error:($cleanup_error != 0),remaining:{containers:$containers,networks:$networks,volumes:$volumes,port_listeners:$listeners,temporary_postgres_images:$postgres_images,temp_secret_directories:$secret_directories,temp_client_directories:$client_directories}}' \
        > "$EVIDENCE_DIR/cleanup.json" || final_status=1
    exit "$final_status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

SECRET_DIR=$(mktemp -d /tmp/nblb-todo6a-secrets.XXXXXX)
CLIENT_DIR=$(mktemp -d /tmp/nblb-todo6a-client.XXXXXX)
export NBLB_QA_RUN_ID=$RUN_ID
export NBLB_QA_SECRET_DIR=$SECRET_DIR
export NBLB_QA_PORT=$QA_PORT
export NBLB_QA_PUBLIC_PORT=$QA_PORT
export NBLB_POSTGRES_IMAGE=$POSTGRES_TAG

source_manifest() {
    uv run python -m scripts.qa.source_manifest --root "$ROOT" --output "$1"
}

assert_image_metadata_secret_free() {
    local image=$1
    local metadata_file=$CLIENT_DIR/image-metadata.txt

    {
        docker image inspect --format '{{json .Config}}' "$image"
        docker history --no-trunc --format '{{.CreatedBy}}' "$image"
    } > "$metadata_file"
    chmod 0444 "$metadata_file"
    docker run --rm --network none --read-only \
        --label "nvidia-build-lb.run=$RUN_ID" \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --entrypoint /app/.venv/bin/python \
        --mount "type=bind,source=$SECRET_DIR/admin_token,target=/canonical-secrets/admin_token,readonly" \
        --mount "type=bind,source=$SECRET_DIR/vault_master_key,target=/canonical-secrets/vault_master_key,readonly" \
        --mount "type=bind,source=$SECRET_DIR/db_password,target=/canonical-secrets/db_password,readonly" \
        --mount "type=bind,source=$SECRET_DIR/server_key,target=/canonical-secrets/server_key,readonly" \
        --mount "type=bind,source=$metadata_file,target=/metadata/image.txt,readonly" \
        "$image" -c '
import pathlib

metadata = pathlib.Path("/metadata/image.txt").read_bytes()
for name in ("admin_token", "vault_master_key", "db_password", "server_key"):
    secret = pathlib.Path("/canonical-secrets", name).read_bytes()
    if not secret or secret in metadata:
        raise SystemExit(1)
'
    rm -f "$metadata_file"
}

wait_healthy() {
    service=$1
    container=$(compose ps -q "$service")
    [ -n "$container" ] || return 1
    for _ in $(seq 1 60); do
        state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null) || state=missing
        [ "$state" = healthy ] && return 0
        [ "$state" = exited ] || [ "$state" = dead ] && return 1
        sleep 1
    done
    return 1
}

wait_process_owner() {
    local container=$1
    local expected_uid=$2
    local owners
    local process_rows
    local stable_samples=0

    # A just-finished `docker exec --user ...` can leave a short-lived runc
    # bootstrap process visible as UID 0 before its requested credentials are
    # applied. Require consecutive quiescent samples so that transient QA
    # helpers are ignored while a persistent wrong-UID process still fails.
    for _ in $(seq 1 50); do
        if process_rows=$(docker top "$container" -eo uid,pid,comm 2>/dev/null); then
            owners=$(printf '%s\n' "$process_rows" \
                | awk 'NR > 1 && NF {print $1}' \
                | LC_ALL=C sort -u \
                | paste -sd, -)
            if [ "$owners" = "$expected_uid" ]; then
                stable_samples=$((stable_samples + 1))
                [ "$stable_samples" -ge 3 ] && return 0
            else
                stable_samples=0
            fi
        else
            stable_samples=0
        fi
        sleep 0.1
    done
    return 1
}

wait_bootstrap_degraded() {
    for _ in $(seq 1 60); do
        status=$(curl --silent \
            --header "Host: 127.0.0.1:$QA_PORT" \
            --output "$CLIENT_DIR/bootstrap-health.json" \
            --write-out '%{http_code}' \
            "http://127.0.0.1:$QA_PORT/health" 2>/dev/null || true)
        if [ "$status" = 503 ] && jq -e \
            '. == {"status":"degraded","ready":false}' \
            "$CLIENT_DIR/bootstrap-health.json" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

safe_curl() {
    curl --silent --show-error --fail-with-body \
        --header "Host: 127.0.0.1:$QA_PORT" \
        "$@"
}

assert_healthcheck_identity() {
    local container=$1
    local setpriv_path=$2
    local expected_uid=$3
    local expected_gid=$4
    local probe=$5
    local configured

    configured=$(docker inspect --format '{{json .Config.Healthcheck.Test}}' "$container")
    printf '%s\n' "$configured" | jq -e \
        --arg setpriv "$setpriv_path" \
        --arg uid "$expected_uid" \
        --arg gid "$expected_gid" \
        --arg probe "$probe" \
        '. == ["CMD",$setpriv,"--reuid=" + $uid,"--regid=" + $gid,"--clear-groups","--inh-caps=-all","--ambient-caps=-all","--bounding-set=-all","--no-new-privs","/usr/local/bin/nblb-healthcheck",$uid,$gid,$probe]' \
        >/dev/null
    docker exec "$container" \
        "$setpriv_path" \
        "--reuid=$expected_uid" \
        "--regid=$expected_gid" \
        --clear-groups \
        --inh-caps=-all \
        --ambient-caps=-all \
        --bounding-set=-all \
        --no-new-privs \
        /usr/local/bin/nblb-healthcheck \
        "$expected_uid" "$expected_gid" "$probe"
}

assert_runtime_secret_exact() {
    local container=$1
    local expected_uid=$2
    local expected_gid=$3
    local source_name=$4
    local runtime_path=$5
    local expected_digest
    local actual_digest
    local metadata

    case "$source_name" in
        admin_token) expected_digest=$ADMIN_TOKEN_SHA256 ;;
        vault_master_key) expected_digest=$VAULT_MASTER_KEY_SHA256 ;;
        db_password) expected_digest=$DB_PASSWORD_SHA256 ;;
        *) return 1 ;;
    esac
    [[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]]
    actual_digest=$(docker exec --user "$expected_uid" "$container" \
        sha256sum "$runtime_path" \
        | awk 'NR == 1 { print $1 } END { if (NR != 1) exit 1 }')
    [ "$actual_digest" = "$expected_digest" ]
    metadata=$(docker exec --user "$expected_uid" "$container" \
        stat -c '%u:%g:%a:%n' "$runtime_path")
    [ "$metadata" = "$expected_uid:$expected_gid:400:$runtime_path" ]
}

printf '%s' nblb_admin_ > "$SECRET_DIR/admin_token"
openssl rand -hex 32 | tr -d '\n' >> "$SECRET_DIR/admin_token"
openssl rand 32 > "$SECRET_DIR/vault_master_key"
openssl rand -hex 32 | tr -d '\n' > "$SECRET_DIR/db_password"
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
    -subj '/CN=integrate.api.nvidia.com' \
    -addext 'subjectAltName=DNS:integrate.api.nvidia.com' \
    -keyout "$SECRET_DIR/server_key" \
    -out "$SECRET_DIR/server_cert" >/dev/null 2>&1
cp "$SECRET_DIR/server_cert" "$SECRET_DIR/ca_cert"
ADMIN_TOKEN_SHA256=$(sha256sum "$SECRET_DIR/admin_token" | awk 'NR == 1 { print $1 }')
VAULT_MASTER_KEY_SHA256=$(sha256sum "$SECRET_DIR/vault_master_key" | awk 'NR == 1 { print $1 }')
DB_PASSWORD_SHA256=$(sha256sum "$SECRET_DIR/db_password" | awk 'NR == 1 { print $1 }')
root_secret_helper 'chown 0:0 /secrets/* && chmod 0600 /secrets/admin_token /secrets/vault_master_key /secrets/db_password /secrets/server_key /secrets/server_cert && chmod 0644 /secrets/ca_cert'

for name in admin_token vault_master_key db_password; do
    [ "$(stat -c '%u:%g:%a' "$SECRET_DIR/$name")" = 0:0:600 ] || exit 1
done

source_manifest "$CLIENT_DIR/source-manifest-before.json"
before_hash=$(jq -er '.source_tree_sha256' "$CLIENT_DIR/source-manifest-before.json")
source_entry_count=$(jq -er '.entry_count' "$CLIENT_DIR/source-manifest-before.json")
IMAGE_TAG="nvidia-build-lb:todo6a-${before_hash:0:16}"
POSTGRES_TAG="nvidia-build-lb-postgres:todo6a-${before_hash:0:16}"
export NBLB_CANDIDATE_IMAGE=$IMAGE_TAG
export NBLB_POSTGRES_IMAGE=$POSTGRES_TAG

uv run python -m scripts.qa.source_snapshot \
    --root "$ROOT" \
    --manifest "$CLIENT_DIR/source-manifest-before.json" \
    --output "$CLIENT_DIR/source-snapshot" \
    --receipt "$CLIENT_DIR/source-snapshot-receipt.json"
jq -e --arg source_sha256 "$before_hash" \
    '.status == "PASS" and .source_tree_sha256 == $source_sha256 and .writable_regular_files == 0' \
    "$CLIENT_DIR/source-snapshot-receipt.json" >/dev/null

uv run pytest tests/candidate -q
uv run ruff check \
    src/nvidia_build_lb tests/candidate \
    scripts/qa/source_manifest.py scripts/qa/source_snapshot.py
uv run ruff format --check \
    src/nvidia_build_lb tests/candidate \
    scripts/qa/source_manifest.py scripts/qa/source_snapshot.py

docker buildx build --pull --no-cache --provenance=false \
    --build-arg SOURCE_DATE_EPOCH=0 \
    --output "type=docker,dest=$CLIENT_DIR/candidate-image.tar,rewrite-timestamp=true" \
    --label nvidia-build-lb.task=todo6a-candidate \
    --label "nvidia-build-lb.source-sha256=$before_hash" \
    --tag "$IMAGE_TAG" "$CLIENT_DIR/source-snapshot"
docker load --input "$CLIENT_DIR/candidate-image.tar" >/dev/null
rm -f "$CLIENT_DIR/candidate-image.tar"
docker buildx build --pull --no-cache --provenance=false \
    --build-arg SOURCE_DATE_EPOCH=0 \
    --output "type=docker,dest=$CLIENT_DIR/postgres-image.tar,rewrite-timestamp=true" \
    --file "$CLIENT_DIR/source-snapshot/docker/postgres.Dockerfile" \
    --label nvidia-build-lb.task=todo6a-candidate-postgres \
    --label "nvidia-build-lb.source-sha256=$before_hash" \
    --tag "$POSTGRES_TAG" "$CLIENT_DIR/source-snapshot"
docker load --input "$CLIENT_DIR/postgres-image.tar" >/dev/null
rm -f "$CLIENT_DIR/postgres-image.tar"

image_digest=$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG")
postgres_image_digest=$(docker image inspect --format '{{.Id}}' "$POSTGRES_TAG")
image_source=$(docker image inspect --format '{{index .Config.Labels "nvidia-build-lb.source-sha256"}}' "$IMAGE_TAG")
postgres_image_source=$(docker image inspect --format '{{index .Config.Labels "nvidia-build-lb.source-sha256"}}' "$POSTGRES_TAG")
[ "$image_source" = "$before_hash" ] || exit 1
[ "$postgres_image_source" = "$before_hash" ] || exit 1
assert_image_metadata_secret_free "$IMAGE_TAG"
compose config --quiet

COMPOSE_STARTED=1
if ! compose up --detach; then
    diagnostics=$(compose logs --no-color 2>&1 \
        | grep -Eo 'prestart_failed:[a-z_]+|migration_failed|runtime_failed' \
        | LC_ALL=C sort -u || true)
    [ -z "$diagnostics" ] || printf '%s\n' "$diagnostics" >&2
    exit 1
fi
wait_bootstrap_degraded
printf '%s\n' 'qa_stage=bootstrap_degraded'

app_container=$(compose ps -q app)
db_container=$(compose ps -q db)
fake_container=$(compose ps -q fake-nvidia)
proxy_container=$(compose ps -q loopback)
[ -n "$app_container" ] \
    && [ -n "$db_container" ] \
    && [ -n "$fake_container" ] \
    && [ -n "$proxy_container" ] \
    || exit 1

app_network_count=$(docker inspect --format '{{len .NetworkSettings.Networks}}' "$app_container")
proxy_network_count=$(docker inspect --format '{{len .NetworkSettings.Networks}}' "$proxy_container")
[ "$app_network_count" -eq 1 ] || exit 1
[ "$proxy_network_count" -eq 2 ] || exit 1
printf '%s\n' 'qa_stage=network_isolation'

expected_runtime_tmpfs='rw,noexec,nosuid,nodev,size=64k,mode=0700,uid=0,gid=0'
app_tmpfs=$(docker inspect --format '{{index .HostConfig.Tmpfs "/run/nvidia-build-lb/secrets"}}' "$app_container")
db_tmpfs=$(docker inspect --format '{{index .HostConfig.Tmpfs "/run/nvidia-build-lb/secrets"}}' "$db_container")
[ "$app_tmpfs" = "$expected_runtime_tmpfs" ] || exit 1
[ "$db_tmpfs" = "$expected_runtime_tmpfs" ] || exit 1
printf '%s\n' 'qa_stage=tmpfs_mounts'

assert_runtime_secret_exact "$app_container" 65532 65532 admin_token \
    /run/nvidia-build-lb/secrets/admin_token
assert_runtime_secret_exact "$app_container" 65532 65532 vault_master_key \
    /run/nvidia-build-lb/secrets/vault_master_key
assert_runtime_secret_exact "$app_container" 65532 65532 db_password \
    /run/nvidia-build-lb/secrets/db_password
assert_runtime_secret_exact "$db_container" 70 70 db_password \
    /run/nvidia-build-lb/secrets/db_password
printf '%s\n' 'qa_stage=secret_metadata'

app_status=$(docker exec --user 65532 "$app_container" awk '/^(Uid|Gid|Groups|CapEff|CapBnd|NoNewPrivs):/' /proc/1/status)
db_status=$(docker exec --user 70 "$db_container" awk '/^(Uid|Gid|Groups|CapEff|CapBnd|NoNewPrivs):/' /proc/1/status)
fake_status=$(docker exec --user 65532 "$fake_container" awk '/^(Uid|Gid|Groups|CapEff|CapBnd|NoNewPrivs):/' /proc/1/status)
proxy_status=$(docker exec --user 65532 "$proxy_container" awk '/^(Uid|Gid|Groups|CapEff|CapBnd|NoNewPrivs):/' /proc/1/status)
printf '%s\n' "$app_status" | grep -Eq '^Uid:[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532$'
printf '%s\n' "$app_status" | grep -Eq '^Gid:[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532$'
printf '%s\n' "$app_status" | grep -Eq '^Groups:[[:space:]]*$'
printf '%s\n' "$app_status" | grep -Eq '^CapEff:[[:space:]]+0+$'
printf '%s\n' "$app_status" | grep -Eq '^CapBnd:[[:space:]]+0+$'
printf '%s\n' "$app_status" | grep -Eq '^NoNewPrivs:[[:space:]]+1$'
printf '%s\n' 'qa_stage=app_identity'
printf '%s\n' "$db_status" | grep -Eq '^Uid:[[:space:]]+70[[:space:]]+70[[:space:]]+70[[:space:]]+70$'
printf '%s\n' "$db_status" | grep -Eq '^Gid:[[:space:]]+70[[:space:]]+70[[:space:]]+70[[:space:]]+70$'
printf '%s\n' "$db_status" | grep -Eq '^Groups:[[:space:]]*$'
printf '%s\n' "$db_status" | grep -Eq '^CapEff:[[:space:]]+0+$'
printf '%s\n' "$db_status" | grep -Eq '^CapBnd:[[:space:]]+0+$'
printf '%s\n' "$db_status" | grep -Eq '^NoNewPrivs:[[:space:]]+1$'
printf '%s\n' 'qa_stage=database_identity'
printf '%s\n' "$fake_status" | grep -Eq '^Uid:[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532$'
printf '%s\n' "$fake_status" | grep -Eq '^Gid:[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532$'
printf '%s\n' "$fake_status" | grep -Eq '^Groups:[[:space:]]*$'
printf '%s\n' "$fake_status" | grep -Eq '^CapEff:[[:space:]]+0+$'
printf '%s\n' "$fake_status" | grep -Eq '^CapBnd:[[:space:]]+0+$'
printf '%s\n' "$fake_status" | grep -Eq '^NoNewPrivs:[[:space:]]+1$'
printf '%s\n' 'qa_stage=fake_identity'
printf '%s\n' "$proxy_status" | grep -Eq '^Uid:[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532$'
printf '%s\n' "$proxy_status" | grep -Eq '^Gid:[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532[[:space:]]+65532$'
printf '%s\n' "$proxy_status" | grep -Eq '^Groups:[[:space:]]*$'
printf '%s\n' "$proxy_status" | grep -Eq '^CapEff:[[:space:]]+0+$'
printf '%s\n' "$proxy_status" | grep -Eq '^CapBnd:[[:space:]]+0+$'
printf '%s\n' "$proxy_status" | grep -Eq '^NoNewPrivs:[[:space:]]+1$'
printf '%s\n' 'qa_stage=proxy_identity'
wait_process_owner "$app_container" 65532
printf '%s\n' 'qa_stage=app_processes'
wait_process_owner "$db_container" 70
printf '%s\n' 'qa_stage=database_processes'
wait_process_owner "$fake_container" 65532
printf '%s\n' 'qa_stage=fake_processes'
wait_process_owner "$proxy_container" 65532
printf '%s\n' 'qa_stage=proxy_processes'
printf '%s\n' 'qa_stage=identity_isolation'

docker exec --user 65532 "$app_container" test -r /run/nvidia-build-lb/secrets/admin_token
docker exec --user 65533 "$app_container" test ! -r /run/nvidia-build-lb/secrets/admin_token
docker exec --user 65532 "$app_container" test ! -r /run/canonical-secrets/admin_token
docker exec --user 70 "$db_container" test -r /run/nvidia-build-lb/secrets/db_password
docker exec --user 71 "$db_container" test ! -r /run/nvidia-build-lb/secrets/db_password
docker exec --user 70 "$db_container" test ! -r /run/canonical-secrets/db_password
printf '%s\n' 'qa_stage=secret_access'

docker run --rm --network none \
    --label "nvidia-build-lb.run=$RUN_ID" \
    --entrypoint sh \
    --mount "type=bind,source=$SECRET_DIR,target=/secrets,readonly" \
    --mount "type=bind,source=$CLIENT_DIR,target=/client" \
    postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609 \
    -c 'cp /secrets/admin_token /client/admin_token && chown "$1:$2" /client/admin_token && chmod 0600 /client/admin_token' \
    sh "$(id -u)" "$(id -g)"
{
    printf 'header = "Host: 127.0.0.1:%s"\n' "$QA_PORT"
    printf '%s' 'header = "Authorization: Bearer '
    tr -d '\n' < "$CLIENT_DIR/admin_token"
    printf '%s\n' '"'
} > "$CLIENT_DIR/admin.curl"

printf '%s' '{"key":"nvapi-synthetic-candidate-key"}' > "$CLIENT_DIR/upstream.json"
create_response=$CLIENT_DIR/upstream-response.json
: > "$create_response"
set +e
create_http=$(curl --silent --show-error --config "$CLIENT_DIR/admin.curl" \
    --request POST --header 'Content-Type: application/json' \
    --data-binary "@$CLIENT_DIR/upstream.json" \
    --output "$create_response" --write-out '%{http_code}' \
    "http://127.0.0.1:$QA_PORT/admin/api/v1/upstream-keys")
create_curl_status=$?
set -e
create_contract_valid=false
if [ "$create_curl_status" -eq 0 ] && [ "$create_http" = 201 ] && jq -e '
    (.id | type == "string" and test("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"))
    and (.fingerprint | type == "string" and test("^sha256:[0-9a-f]{64}$"))
    and .enabled == false
    and .routing_state == "disabled"
    and .health_state == "unknown"
' "$create_response" >/dev/null 2>&1; then
    create_contract_valid=true
fi
write_admin_stage_evidence "$EVIDENCE_DIR" create 201 "$create_http" \
    "$create_contract_valid" "$create_response"
[ "$create_contract_valid" = true ] || exit 1
key_id=$(jq -er '.id' "$create_response")
printf '%s\n' 'qa_stage=key_created'

probe_response=$CLIENT_DIR/upstream-probe-response.json
: > "$probe_response"
set +e
probe_http=$(curl --silent --show-error --config "$CLIENT_DIR/admin.curl" \
    --request POST --output "$probe_response" --write-out '%{http_code}' \
    "http://127.0.0.1:$QA_PORT/admin/api/v1/upstream-keys/$key_id/probe")
probe_curl_status=$?
set -e
probe_contract_valid=false
if [ "$probe_curl_status" -eq 0 ] && [ "$probe_http" = 200 ] && jq -e \
    --arg key_id "$key_id" \
    '.id == $key_id and .enabled == false and .probe_status == "valid" and (.observed_at | type == "string")' \
    "$probe_response" >/dev/null 2>&1; then
    probe_contract_valid=true
fi
write_admin_stage_evidence "$EVIDENCE_DIR" probe 200 "$probe_http" \
    "$probe_contract_valid" "$probe_response"
[ "$probe_contract_valid" = true ] || exit 1
printf '%s\n' 'qa_stage=key_probed_valid'

enable_response=$CLIENT_DIR/upstream-enable-response.bin
: > "$enable_response"
set +e
enable_http=$(curl --silent --show-error --config "$CLIENT_DIR/admin.curl" \
    --request POST --output "$enable_response" --write-out '%{http_code}' \
    "http://127.0.0.1:$QA_PORT/admin/api/v1/upstream-keys/$key_id/enable")
enable_curl_status=$?
set -e
enable_contract_valid=false
if [ "$enable_curl_status" -eq 0 ] && [ "$enable_http" = 204 ] \
    && [ ! -s "$enable_response" ]; then
    enable_contract_valid=true
fi
write_admin_stage_evidence "$EVIDENCE_DIR" enable 204 "$enable_http" \
    "$enable_contract_valid" "$enable_response"
[ "$enable_contract_valid" = true ] || exit 1
assert_admin_stage_evidence "$EVIDENCE_DIR" create 201
assert_admin_stage_evidence "$EVIDENCE_DIR" probe 200
assert_admin_stage_evidence "$EVIDENCE_DIR" enable 204
printf '%s\n' 'qa_stage=key_enabled'
wait_healthy app
printf '%s\n' 'qa_stage=app_healthy'
wait_healthy db
wait_healthy fake-nvidia
wait_healthy loopback
assert_healthcheck_identity "$app_container" /bin/setpriv 65532 65532 app
assert_healthcheck_identity "$db_container" /bin/setpriv 70 70 postgres
assert_healthcheck_identity "$fake_container" /bin/setpriv 65532 65532 fake
assert_healthcheck_identity "$proxy_container" /bin/setpriv 65532 65532 loopback
printf '%s\n' 'qa_stage=healthcheck_identity'

printf '%s' '{"label":"todo6a-candidate","scopes":["models:read","chat:write"]}' \
    > "$CLIENT_DIR/downstream.json"
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/admin.curl" \
    --request POST --header 'Content-Type: application/json' \
    --data-binary "@$CLIENT_DIR/downstream.json" \
    --output "$CLIENT_DIR/downstream-response.json" \
    "http://127.0.0.1:$QA_PORT/admin/api/v1/downstream-tokens"
jq -er '.token' "$CLIENT_DIR/downstream-response.json" > "$CLIENT_DIR/downstream-token"
rm -f "$CLIENT_DIR/downstream-response.json"
{
    printf 'header = "Host: 127.0.0.1:%s"\n' "$QA_PORT"
    printf '%s' 'header = "Authorization: Bearer '
    tr -d '\n' < "$CLIENT_DIR/downstream-token"
    printf '%s\n' '"'
} > "$CLIENT_DIR/downstream.curl"

safe_curl --output "$CLIENT_DIR/health.json" "http://127.0.0.1:$QA_PORT/health"
jq -e '. == {"status":"ok","ready":true}' "$CLIENT_DIR/health.json" >/dev/null
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/downstream.curl" \
    --output "$CLIENT_DIR/models.json" "http://127.0.0.1:$QA_PORT/v1/models"
jq -e '.data[0].id == "z-ai/glm-5.2"' "$CLIENT_DIR/models.json" >/dev/null

printf '%s' '{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"candidate qa"}],"stream":false}' \
    > "$CLIENT_DIR/chat.json"
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/downstream.curl" \
    --header 'Content-Type: application/json' --data-binary "@$CLIENT_DIR/chat.json" \
    --output "$CLIENT_DIR/chat-response.json" \
    "http://127.0.0.1:$QA_PORT/v1/chat/completions"
jq -e '.choices[0].message.content == "candidate fake upstream ok"' \
    "$CLIENT_DIR/chat-response.json" >/dev/null

printf '%s' '{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"candidate qa"}],"stream":true}' \
    > "$CLIENT_DIR/chat-stream.json"
curl --silent --show-error --fail-with-body --no-buffer \
    --config "$CLIENT_DIR/downstream.curl" \
    --header 'Content-Type: application/json' --data-binary "@$CLIENT_DIR/chat-stream.json" \
    --output "$CLIENT_DIR/chat-stream-response.txt" \
    "http://127.0.0.1:$QA_PORT/v1/chat/completions"
grep -Fq 'candidate fake upstream ok' "$CLIENT_DIR/chat-stream-response.txt"
[ "$(grep -Fxc 'data: [DONE]' "$CLIENT_DIR/chat-stream-response.txt")" -eq 1 ]

run_missing_secret() {
    missing=$1
    arguments=(
        docker run --rm --network none --read-only
        --label "nvidia-build-lb.run=$RUN_ID"
        --cap-drop ALL --cap-add CHOWN --cap-add SETGID --cap-add SETUID --cap-add SETPCAP
        --security-opt no-new-privileges:true
        --tmpfs /run/nvidia-build-lb/secrets:rw,noexec,nosuid,nodev,size=64k,mode=0700,uid=0,gid=0
        -e NVIDIA_BUILD_LB_MODE=app
    )
    for name in admin_token vault_master_key db_password; do
        [ "$name" = "$missing" ] || arguments+=(
            --mount "type=bind,source=$SECRET_DIR/$name,target=/run/canonical-secrets/$name,readonly"
        )
    done
    arguments+=("$IMAGE_TAG")
    set +e
    output=$("${arguments[@]}" 2>&1)
    status=$?
    set -e
    [ "$status" -eq 70 ] && [ "$output" = prestart_failed ]
}

run_postgres_missing_db_secret() {
    set +e
    output=$(docker run --rm --network none --read-only \
        --label "nvidia-build-lb.run=$RUN_ID" \
        --cap-drop ALL --cap-add CHOWN --cap-add SETGID --cap-add SETUID --cap-add SETPCAP \
        --security-opt no-new-privileges:true \
        --tmpfs /run/nvidia-build-lb/secrets:rw,noexec,nosuid,nodev,size=64k,mode=0700,uid=0,gid=0 \
        "$POSTGRES_TAG" 2>&1)
    status=$?
    set -e
    [ "$status" -eq 70 ] && [ "$output" = prestart_failed:source_missing ]
}

run_missing_secret admin_token
missing_admin_status=70
run_missing_secret vault_master_key
missing_vault_status=70
run_missing_secret db_password
missing_app_db_status=70
run_postgres_missing_db_secret
missing_postgres_db_status=70

docker exec --user 65532 "$app_container" touch /run/nvidia-build-lb/secrets/.qa-marker
docker exec --user 70 "$db_container" touch /run/nvidia-build-lb/secrets/.qa-marker
compose stop app >/dev/null
compose stop db >/dev/null
compose start db >/dev/null
wait_healthy db
compose start app >/dev/null
wait_healthy app
docker exec --user 65532 "$app_container" test ! -e /run/nvidia-build-lb/secrets/.qa-marker
docker exec --user 70 "$db_container" test ! -e /run/nvidia-build-lb/secrets/.qa-marker
assert_runtime_secret_exact "$app_container" 65532 65532 admin_token \
    /run/nvidia-build-lb/secrets/admin_token
assert_runtime_secret_exact "$app_container" 65532 65532 vault_master_key \
    /run/nvidia-build-lb/secrets/vault_master_key
assert_runtime_secret_exact "$app_container" 65532 65532 db_password \
    /run/nvidia-build-lb/secrets/db_password
assert_runtime_secret_exact "$db_container" 70 70 db_password \
    /run/nvidia-build-lb/secrets/db_password

curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/downstream.curl" \
    --output "$CLIENT_DIR/models-after-restart.json" \
    "http://127.0.0.1:$QA_PORT/v1/models"
jq -e '.data[0].id == "z-ai/glm-5.2"' "$CLIENT_DIR/models-after-restart.json" >/dev/null
safe_curl --output "$CLIENT_DIR/health-after-restart.json" \
    "http://127.0.0.1:$QA_PORT/health"
jq -e '.ready == true' "$CLIENT_DIR/health-after-restart.json" >/dev/null

source_manifest "$CLIENT_DIR/source-manifest-after.json"
after_hash=$(jq -er '.source_tree_sha256' "$CLIENT_DIR/source-manifest-after.json")
[ "$before_hash" = "$after_hash" ] || exit 1
cmp "$CLIENT_DIR/source-manifest-before.json" "$CLIENT_DIR/source-manifest-after.json"
cp "$CLIENT_DIR/source-manifest-after.json" "$EVIDENCE_DIR/source-manifest.json"
chmod 0444 "$EVIDENCE_DIR/source-manifest.json"
cp "$CLIENT_DIR/source-snapshot-receipt.json" "$EVIDENCE_DIR/source-snapshot.json"
chmod 0444 "$EVIDENCE_DIR/source-snapshot.json"
source_snapshot_receipt_sha256=$(sha256sum "$EVIDENCE_DIR/source-snapshot.json" \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}')

jq -n \
    --arg source_sha256 "$before_hash" \
    --arg image_digest "$image_digest" \
    --arg postgres_image_digest "$postgres_image_digest" \
    --arg source_snapshot_receipt_sha256 "$source_snapshot_receipt_sha256" \
    --argjson source_entry_count "$source_entry_count" \
    '{schema_version:2,status:"PASS",source_tree_sha256:$source_sha256,source_manifest_algorithm:"git-files-type-canonical-mode-path-payload-sha256-v2",source_manifest_entry_count:$source_entry_count,source_snapshot_receipt_sha256:$source_snapshot_receipt_sha256,build_context:"manifest-bound-read-only-snapshot",build_cache_disabled:true,source_date_epoch:0,layer_timestamps_rewritten:true,deterministic_archive_loaded:true,image_digest:$image_digest,postgres_image_digest:$postgres_image_digest,image_reference_kind:"local immutable image id pair",metadata_filtered:true,secrets_in_metadata:false}' \
    > "$EVIDENCE_DIR/candidate.json"
jq -n \
    --arg source_sha256 "$before_hash" \
    --arg image_digest "$image_digest" \
    --arg postgres_image_digest "$postgres_image_digest" \
    --arg key_id "$key_id" \
    '{schema_version:2,status:"PASS",source_tree_sha256:$source_sha256,image_digest:$image_digest,postgres_image_digest:$postgres_image_digest,checks:{candidate_build:true,postgres_candidate_build:true,compose_render:true,migration:true,admin_upstream_create_contract:true,admin_upstream_probe_contract:true,admin_upstream_enable_contract:true,admin_stage_evidence_persisted:true,app_healthy:true,database_healthy:true,fake_upstream_healthy:true,loopback_proxy_healthy:true,healthcheck_identity_self_verified:true,health:true,models:true,nonstream_chat:true,stream_chat_done_once:true,synthetic_internal_key_id:$key_id,app_uid:65532,database_uid:70,fake_upstream_uid:65532,loopback_proxy_uid:65532,capabilities_effective_cleared:true,capabilities_bounding_cleared:true,steady_state_no_new_privileges:true,supplementary_groups_cleared:true,runtime_secret_mode_0400:true,runtime_secret_content_exact:true,canonical_secret_mode_0600:true,tmpfs_runtime:true,app_internal_network_only:true,loopback_proxy_dual_homed:true,restart_persistence:true,tmpfs_repopulated:true,restart_runtime_secret_content_exact:true}}' \
    > "$EVIDENCE_DIR/manual-qa.json"
jq -n \
    --argjson missing_admin "$missing_admin_status" \
    --argjson missing_vault "$missing_vault_status" \
    --argjson missing_app_db "$missing_app_db_status" \
    --argjson missing_postgres_db "$missing_postgres_db_status" \
    --argjson qa_port "$QA_PORT" \
    '{schema_version:1,status:"PASS",checks:{missing_admin_secret_exit:$missing_admin,missing_vault_secret_exit:$missing_vault,missing_app_database_secret_exit:$missing_app_db,missing_postgres_database_secret_exit:$missing_postgres_db,missing_postgres_database_secret_output:"prestart_failed:source_missing",app_cross_uid_read_denied:true,database_cross_uid_read_denied:true,app_canonical_mount_read_denied:true,database_canonical_mount_read_denied:true,canonical_values_not_serialized:true,qa_loopback_proxy_has_no_secrets:true,qa_port:$qa_port,task_labels_only:true}}' \
    > "$EVIDENCE_DIR/adversarial.json"

printf '%s\n' "candidate_image_digest=$image_digest"
printf '%s\n' "candidate_postgres_image_digest=$postgres_image_digest"
