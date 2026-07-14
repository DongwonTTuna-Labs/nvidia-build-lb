#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

EVIDENCE_DIR=${EVIDENCE_DIR:-.omo/evidence/task-7-nvidia-build-lb}
IMAGE_DIGEST=${IMAGE_DIGEST:-}
POSTGRES_IMAGE_DIGEST=${POSTGRES_IMAGE_DIGEST:-}
SOURCE_MANIFEST=${SOURCE_MANIFEST:-}
PRIMARY_PORT=${NBLB_VERIFY_PRIMARY_PORT:-32456}
RESTORE_PORT=${NBLB_VERIFY_RESTORE_PORT:-32457}
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
PROJECT_PRIMARY="nblb-todo7-primary-${RUN_ID,,}"
PROJECT_RESTORE="nblb-todo7-restore-${RUN_ID,,}"
PROJECT_PRIMARY=${PROJECT_PRIMARY//[^a-z0-9_-]/-}
PROJECT_RESTORE=${PROJECT_RESTORE//[^a-z0-9_-]/-}
PRIMARY_SECRET_DIR=""
RESTORE_SECRET_DIR=""
BAD_SECRET_DIR=""
CLIENT_DIR=""
OPERATION_LOCK_DIR=""
BACKUP_BASE=""
PRIMARY_STARTED=0
RESTORE_STARTED=0
CODEX_BEFORE=0
CODEX_AFTER=0

fail() {
    printf '%s\n' "${1:-verify_local_failed}" >&2
    exit 1
}

primary_compose() {
    NBLB_QA_RUN_ID="$RUN_ID" \
    NBLB_QA_SECRET_DIR="$PRIMARY_SECRET_DIR" \
    NBLB_QA_PORT="$PRIMARY_PORT" \
    NBLB_POSTGRES_IMAGE="$POSTGRES_IMAGE_DIGEST" \
    NBLB_CANDIDATE_IMAGE="$IMAGE_DIGEST" \
    NBLB_BACKUP_SOURCE=true \
    NBLB_RESTORE_ISOLATED=false \
    docker compose -f compose.qa.yml -p "$PROJECT_PRIMARY" "$@"
}

restore_compose() {
    NBLB_QA_RUN_ID="$RUN_ID" \
    NBLB_QA_SECRET_DIR="$RESTORE_SECRET_DIR" \
    NBLB_QA_PORT="$RESTORE_PORT" \
    NBLB_POSTGRES_IMAGE="$POSTGRES_IMAGE_DIGEST" \
    NBLB_CANDIDATE_IMAGE="$IMAGE_DIGEST" \
    NBLB_BACKUP_SOURCE=false \
    NBLB_RESTORE_ISOLATED=true \
    docker compose -f compose.qa.yml -p "$PROJECT_RESTORE" "$@"
}

root_helper() {
    docker run --rm --network none \
        --label "nvidia-build-lb.run=$RUN_ID" \
        --entrypoint /bin/sh "$@"
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
    local port=$1
    ss -H -ltn "sport = :$port" 2>/dev/null | wc -l | tr -d ' '
}

remove_root_contents() {
    local path=$1
    [ -n "$path" ] && [ -d "$path" ] || return 0
    root_helper --mount "type=bind,source=$path,target=/target" \
        "$IMAGE_DIGEST" -c 'rm -rf -- /target/* /target/.[!.]* /target/..?* 2>/dev/null || true; chown "$1:$2" /target; chmod 0700 /target' \
        helper "$(id -u)" "$(id -g)" \
        >/dev/null 2>&1
}

cleanup() {
    trigger_status=$?
    trap - EXIT HUP INT TERM
    set +e
    cleanup_error=0
    if [ "$RESTORE_STARTED" -eq 1 ]; then
        restore_compose down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1 \
            || cleanup_error=1
    fi
    if [ "$PRIMARY_STARTED" -eq 1 ]; then
        primary_compose down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1 \
            || cleanup_error=1
    fi
    for path in "$PRIMARY_SECRET_DIR" "$RESTORE_SECRET_DIR" "$BAD_SECRET_DIR" "$BACKUP_BASE"; do
        if [ -n "$path" ] && [ -d "$path" ]; then
            remove_root_contents "$path" || cleanup_error=1
            rmdir "$path" >/dev/null 2>&1 || cleanup_error=1
        fi
    done
    if [ -n "$CLIENT_DIR" ] && [ -d "$CLIENT_DIR" ]; then
        rm -rf -- "$CLIENT_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi

    containers=$(label_count) || { containers=-1; cleanup_error=1; }
    networks=$(network_count) || { networks=-1; cleanup_error=1; }
    volumes=$(volume_count) || { volumes=-1; cleanup_error=1; }
    primary_listeners=$(port_count "$PRIMARY_PORT") || { primary_listeners=-1; cleanup_error=1; }
    restore_listeners=$(port_count "$RESTORE_PORT") || { restore_listeners=-1; cleanup_error=1; }
    postgres_images=0
    temp_directories=0
    for path in "$PRIMARY_SECRET_DIR" "$RESTORE_SECRET_DIR" "$BAD_SECRET_DIR" "$CLIENT_DIR" "$BACKUP_BASE"; do
        [ -z "$path" ] || [ ! -e "$path" ] || temp_directories=$((temp_directories + 1))
    done
    cleanup_status=PASS
    final_status=$trigger_status
    if [ "$cleanup_error" -ne 0 ] || [ "$containers" -ne 0 ] \
        || [ "$networks" -ne 0 ] || [ "$volumes" -ne 0 ] \
        || [ "$primary_listeners" -ne 0 ] || [ "$restore_listeners" -ne 0 ] \
        || [ "$postgres_images" -ne 0 ] || [ "$temp_directories" -ne 0 ]; then
        cleanup_status=FAIL
        final_status=1
    fi
    if [ ! -e "$EVIDENCE_DIR/manual-qa.json" ]; then
        jq -n --arg run_id "$RUN_ID" --arg status FAIL \
            '{schema_version:1,run_id:$run_id,status:$status}' \
            > "$EVIDENCE_DIR/manual-qa.json" 2>/dev/null || final_status=1
    fi
    if [ ! -e "$EVIDENCE_DIR/adversarial.json" ]; then
        jq -n --arg run_id "$RUN_ID" --arg status FAIL \
            '{schema_version:1,run_id:$run_id,status:$status}' \
            > "$EVIDENCE_DIR/adversarial.json" 2>/dev/null || final_status=1
    fi
    jq -n \
        --arg run_id "$RUN_ID" --arg status "$cleanup_status" \
        --argjson trigger_status "$trigger_status" --argjson final_status "$final_status" \
        --argjson cleanup_error "$cleanup_error" \
        --argjson containers "$containers" --argjson networks "$networks" \
        --argjson volumes "$volumes" --argjson primary_listeners "$primary_listeners" \
        --argjson restore_listeners "$restore_listeners" \
        --argjson postgres_images "$postgres_images" \
        --argjson temp_directories "$temp_directories" \
        '{schema_version:1,run_id:$run_id,status:$status,trigger_exit_status:$trigger_status,final_exit_status:$final_status,cleanup_command_error:($cleanup_error != 0),remaining:{containers:$containers,networks:$networks,volumes:$volumes,primary_port_listeners:$primary_listeners,restore_port_listeners:$restore_listeners,temporary_postgres_images:$postgres_images,temp_directories:$temp_directories}}' \
        > "$EVIDENCE_DIR/cleanup.json" 2>/dev/null || final_status=1
    exit "$final_status"
}

wait_healthy() {
    local compose_kind=$1
    local service=$2
    local container state
    if [ "$compose_kind" = primary ]; then
        container=$(primary_compose ps -q "$service")
    else
        container=$(restore_compose ps -q "$service")
    fi
    [ -n "$container" ] || return 1
    for _ in $(seq 1 90); do
        state=$(docker inspect \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "$container" 2>/dev/null) || state=missing
        [ "$state" = healthy ] && return 0
        [ "$state" = exited ] || [ "$state" = dead ] && return 1
        sleep 1
    done
    return 1
}

wait_container_exit() {
    local container=$1
    local state
    for _ in $(seq 1 150); do
        state=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null) \
            || state=missing
        case "$state" in
            exited|dead)
                printf '%s\n' "$state"
                return 0
                ;;
            missing)
                return 1
                ;;
        esac
        sleep 0.1
    done
    return 1
}

wait_http() {
    local port=$1
    local expected=$2
    local output=$3
    local observed
    for _ in $(seq 1 90); do
        observed=$(curl --silent --header 'Host: 127.0.0.1:2456' \
            --output "$output" --write-out '%{http_code}' \
            "http://127.0.0.1:$port/health" 2>/dev/null || true)
        [ "$observed" = "$expected" ] && return 0
        sleep 1
    done
    return 1
}

copy_secret_to_client() {
    local source=$1
    local destination=$2
    root_helper \
        --mount "type=bind,source=$source,target=/source/secret,readonly" \
        --mount "type=bind,source=$CLIENT_DIR,target=/client" \
        "$IMAGE_DIGEST" -c 'cp /source/secret "/client/$1"; chown "$2:$3" "/client/$1"; chmod 0600 "/client/$1"' \
        helper "$destination" "$(id -u)" "$(id -g)"
}

write_curl_config() {
    local secret_file=$1
    local output=$2
    {
        printf '%s\n' 'header = "Host: 127.0.0.1:2456"'
        printf '%s' 'header = "Authorization: Bearer '
        tr -d '\n' < "$secret_file"
        printf '%s\n' '"'
    } > "$output"
    chmod 0600 "$output"
}

api_status() {
    local config=$1
    local port=$2
    local path=$3
    curl --silent --config "$config" --output /dev/null --write-out '%{http_code}' \
        "http://127.0.0.1:$port$path" 2>/dev/null || true
}

run_missing_app_secret() {
    local missing=$1
    local output status
    local arguments=(
        docker run --rm --network none --read-only
        --label "nvidia-build-lb.run=$RUN_ID"
        --cap-drop ALL --cap-add CHOWN --cap-add SETGID --cap-add SETUID --cap-add SETPCAP
        --security-opt no-new-privileges:true
        --tmpfs /run/nvidia-build-lb/secrets:rw,noexec,nosuid,nodev,size=64k,mode=0700,uid=0,gid=0
        -e NVIDIA_BUILD_LB_MODE=app
    )
    local name
    for name in admin_token vault_master_key db_password; do
        [ "$name" = "$missing" ] || arguments+=(
            --mount "type=bind,source=$PRIMARY_SECRET_DIR/$name,target=/run/canonical-secrets/$name,readonly"
        )
    done
    arguments+=("$IMAGE_DIGEST")
    set +e
    output=$("${arguments[@]}" 2>&1)
    status=$?
    set -e
    [ "$status" -eq 70 ] && [ "$output" = prestart_failed ]
}

run_wrong_app_secret() {
    local wrong=$1
    local output status
    local arguments=(
        docker run --rm --network none --read-only
        --label "nvidia-build-lb.run=$RUN_ID"
        --cap-drop ALL --cap-add CHOWN --cap-add SETGID --cap-add SETUID --cap-add SETPCAP
        --security-opt no-new-privileges:true
        --tmpfs /run/nvidia-build-lb/secrets:rw,noexec,nosuid,nodev,size=64k,mode=0700,uid=0,gid=0
        -e NVIDIA_BUILD_LB_MODE=app
    )
    local name source
    for name in admin_token vault_master_key db_password; do
        source=$PRIMARY_SECRET_DIR/$name
        [ "$name" = "$wrong" ] && source=$BAD_SECRET_DIR/$name
        arguments+=(
            --mount "type=bind,source=$source,target=/run/canonical-secrets/$name,readonly"
        )
    done
    arguments+=("$IMAGE_DIGEST")
    set +e
    output=$("${arguments[@]}" 2>&1)
    status=$?
    set -e
    [ "$status" -eq 70 ] && [ "$output" = prestart_failed ]
}

for command in cmp curl date docker flock git jq openssl realpath sha256sum ss uv; do
    command -v "$command" >/dev/null 2>&1 || fail required_command_unavailable
done
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail image_digest_invalid
[[ "$POSTGRES_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail postgres_image_digest_invalid
[ -f "$SOURCE_MANIFEST" ] && [ ! -L "$SOURCE_MANIFEST" ] \
    || fail source_manifest_invalid
SOURCE_MANIFEST=$(realpath -e "$SOURCE_MANIFEST")
observed_image=$(docker image inspect --format '{{.Id}}' "$IMAGE_DIGEST" 2>/dev/null) \
    || fail image_unavailable
[ "$observed_image" = "$IMAGE_DIGEST" ] || fail image_digest_mismatch
observed_postgres_image=$(docker image inspect --format '{{.Id}}' \
    "$POSTGRES_IMAGE_DIGEST" 2>/dev/null) || fail postgres_image_unavailable
[ "$observed_postgres_image" = "$POSTGRES_IMAGE_DIGEST" ] \
    || fail postgres_image_digest_mismatch
[ "$(port_count "$PRIMARY_PORT")" -eq 0 ] || fail primary_port_busy
[ "$(port_count "$RESTORE_PORT")" -eq 0 ] || fail restore_port_busy
CODEX_BEFORE=$(curl --silent --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:2455/health 2>/dev/null || true)
[ "$CODEX_BEFORE" = 200 ] || fail codex_lb_unhealthy

mkdir -p "$(dirname "$EVIDENCE_DIR")"
exec 9>"$EVIDENCE_DIR.claim"
flock -n 9 || fail evidence_exists
if [ -e "$EVIDENCE_DIR" ] || ! mkdir "$EVIDENCE_DIR" 2>/dev/null; then
    fail evidence_exists
fi
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

PRIMARY_SECRET_DIR=$(mktemp -d /tmp/nblb-todo7-primary-secrets.XXXXXX)
RESTORE_SECRET_DIR=$(mktemp -d /tmp/nblb-todo7-restore-secrets.XXXXXX)
BAD_SECRET_DIR=$(mktemp -d /tmp/nblb-todo7-bad-secrets.XXXXXX)
CLIENT_DIR=$(mktemp -d /tmp/nblb-todo7-client.XXXXXX)
OPERATION_LOCK_DIR=$CLIENT_DIR/operation-locks
mkdir -m 0700 "$OPERATION_LOCK_DIR"
BACKUP_BASE=$(mktemp -d /tmp/nblb-todo7-backup.XXXXXX)
mkdir "$BACKUP_BASE/database" "$BACKUP_BASE/key" "$BACKUP_BASE/manifest"
root_helper --mount "type=bind,source=$BACKUP_BASE,target=/backup" \
    "$IMAGE_DIGEST" -c 'chown 0:0 /backup/database /backup/key /backup/manifest; chmod 0700 /backup/database /backup/key /backup/manifest'

printf '%s' nblb_admin_ > "$PRIMARY_SECRET_DIR/admin_token"
openssl rand -hex 32 | tr -d '\n' >> "$PRIMARY_SECRET_DIR/admin_token"
openssl rand 32 > "$PRIMARY_SECRET_DIR/vault_master_key"
openssl rand -hex 32 | tr -d '\n' > "$PRIMARY_SECRET_DIR/db_password"
openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
    -subj '/CN=integrate.api.nvidia.com' \
    -addext 'subjectAltName=DNS:integrate.api.nvidia.com' \
    -keyout "$PRIMARY_SECRET_DIR/server_key" \
    -out "$PRIMARY_SECRET_DIR/server_cert" >/dev/null 2>&1
cp "$PRIMARY_SECRET_DIR/server_cert" "$PRIMARY_SECRET_DIR/ca_cert"

cp "$PRIMARY_SECRET_DIR/admin_token" "$RESTORE_SECRET_DIR/admin_token"
cp "$PRIMARY_SECRET_DIR/db_password" "$RESTORE_SECRET_DIR/db_password"
cp "$PRIMARY_SECRET_DIR/server_key" "$RESTORE_SECRET_DIR/server_key"
cp "$PRIMARY_SECRET_DIR/server_cert" "$RESTORE_SECRET_DIR/server_cert"
cp "$PRIMARY_SECRET_DIR/ca_cert" "$RESTORE_SECRET_DIR/ca_cert"

printf '%s' invalid > "$BAD_SECRET_DIR/admin_token"
openssl rand 31 > "$BAD_SECRET_DIR/vault_master_key"
printf '%s\n' invalid > "$BAD_SECRET_DIR/db_password"

root_helper --mount "type=bind,source=$PRIMARY_SECRET_DIR,target=/secrets" \
    "$IMAGE_DIGEST" -c 'chown 0:0 /secrets/*; chmod 0600 /secrets/admin_token /secrets/vault_master_key /secrets/db_password /secrets/server_key /secrets/server_cert; chmod 0644 /secrets/ca_cert'
root_helper --mount "type=bind,source=$RESTORE_SECRET_DIR,target=/secrets" \
    "$IMAGE_DIGEST" -c 'chown 0:0 /secrets /secrets/*; chmod 0700 /secrets; chmod 0600 /secrets/admin_token /secrets/db_password /secrets/server_key /secrets/server_cert; chmod 0644 /secrets/ca_cert'
root_helper --mount "type=bind,source=$BAD_SECRET_DIR,target=/secrets" \
    "$IMAGE_DIGEST" -c 'chown 0:0 /secrets/*; chmod 0600 /secrets/*'

uv run python -m scripts.qa.source_manifest --root "$ROOT" \
    --output "$CLIENT_DIR/source-manifest-current.json"
cmp "$SOURCE_MANIFEST" "$CLIENT_DIR/source-manifest-current.json" \
    || fail source_manifest_mismatch
cp "$SOURCE_MANIFEST" "$CLIENT_DIR/source-manifest.json"
cp "$SOURCE_MANIFEST" "$EVIDENCE_DIR/source-manifest.json"
chmod 0444 "$EVIDENCE_DIR/source-manifest.json"
source_sha256=$(jq -er '.source_tree_sha256' "$SOURCE_MANIFEST")
source_manifest_sha256=$(sha256sum "$SOURCE_MANIFEST" \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}')
image_source_sha256=$(docker image inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.source-sha256"}}' \
    "$IMAGE_DIGEST")
[ "$source_sha256" = "$image_source_sha256" ] || fail image_source_mismatch
postgres_source_sha256=$(docker image inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.source-sha256"}}' \
    "$POSTGRES_IMAGE_DIGEST")
[ "$source_sha256" = "$postgres_source_sha256" ] \
    || fail postgres_image_source_mismatch

uv run ruff check .
uv run ruff format --check .
uv run basedpyright
FULL_TEST_EVIDENCE=".omo/evidence/task-4-nvidia-build-lb/runs/todo7-full-$RUN_ID"
EVIDENCE_DIR="$FULL_TEST_EVIDENCE" uv run pytest -q

NBLB_APP_REGISTRY_DIGEST="${IMAGE_DIGEST#sha256:}" \
NBLB_POSTGRES_REGISTRY_DIGEST="${POSTGRES_IMAGE_DIGEST#sha256:}" \
NBLB_SECRET_DIR="$PRIMARY_SECRET_DIR" \
scripts/ops/production-compose.sh config --quiet
primary_compose config --quiet
restore_compose config --quiet

PRIMARY_STARTED=1
primary_compose up --detach db fake-nvidia
wait_healthy primary db
wait_healthy primary fake-nvidia

set +e
primary_compose run --rm --no-deps migrate > "$CLIENT_DIR/migrate-a.log" 2>&1 &
migration_pid_a=$!
primary_compose run --rm --no-deps migrate > "$CLIENT_DIR/migrate-b.log" 2>&1 &
migration_pid_b=$!
wait "$migration_pid_a"
migration_status_a=$?
wait "$migration_pid_b"
migration_status_b=$?
set -e
[ "$migration_status_a" -eq 0 ] && [ "$migration_status_b" -eq 0 ] \
    || fail concurrent_migration_failed
rm -f "$CLIENT_DIR/migrate-a.log" "$CLIENT_DIR/migrate-b.log"

primary_compose up --detach app loopback
wait_http "$PRIMARY_PORT" 503 "$CLIENT_DIR/bootstrap-health.json"
primary_app=$(primary_compose ps -q app)
primary_db=$(primary_compose ps -q db)
[ -n "$primary_app" ] && [ -n "$primary_db" ] || fail primary_container_missing

copy_secret_to_client "$PRIMARY_SECRET_DIR/admin_token" admin-token-original
write_curl_config "$CLIENT_DIR/admin-token-original" "$CLIENT_DIR/admin-original.curl"
printf '%s' '{"key":"nvapi-synthetic-todo7-key"}' > "$CLIENT_DIR/upstream.json"
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/admin-original.curl" \
    --request POST --header 'Content-Type: application/json' \
    --data-binary "@$CLIENT_DIR/upstream.json" \
    --output "$CLIENT_DIR/upstream-response.json" \
    "http://127.0.0.1:$PRIMARY_PORT/admin/api/v1/upstream-keys"
key_id=$(jq -er '.id' "$CLIENT_DIR/upstream-response.json")
key_fingerprint=$(jq -er '.fingerprint' "$CLIENT_DIR/upstream-response.json")
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/admin-original.curl" \
    --request POST --output /dev/null \
    "http://127.0.0.1:$PRIMARY_PORT/admin/api/v1/upstream-keys/$key_id/enable"
wait_healthy primary app

printf '%s' '{"label":"todo7-revoked","scopes":["models:read"]}' \
    > "$CLIENT_DIR/revoked-token-request.json"
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/admin-original.curl" \
    --request POST --header 'Content-Type: application/json' \
    --data-binary "@$CLIENT_DIR/revoked-token-request.json" \
    --output "$CLIENT_DIR/revoked-token-response.json" \
    "http://127.0.0.1:$PRIMARY_PORT/admin/api/v1/downstream-tokens"
revoked_token_id=$(jq -er '.id' "$CLIENT_DIR/revoked-token-response.json")
jq -er '.token' "$CLIENT_DIR/revoked-token-response.json" > "$CLIENT_DIR/revoked-token"
rm -f "$CLIENT_DIR/revoked-token-response.json"
write_curl_config "$CLIENT_DIR/revoked-token" "$CLIENT_DIR/revoked.curl"
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/admin-original.curl" \
    --request DELETE --output /dev/null \
    "http://127.0.0.1:$PRIMARY_PORT/admin/api/v1/downstream-tokens/$revoked_token_id"
revoked_status=$(api_status "$CLIENT_DIR/revoked.curl" "$PRIMARY_PORT" /v1/models)
[ "$revoked_status" = 401 ] || fail revoked_token_accepted

printf '%s' '{"label":"todo7-persistence","scopes":["models:read","chat:write"]}' \
    > "$CLIENT_DIR/persistence-token-request.json"
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/admin-original.curl" \
    --request POST --header 'Content-Type: application/json' \
    --data-binary "@$CLIENT_DIR/persistence-token-request.json" \
    --output "$CLIENT_DIR/persistence-token-response.json" \
    "http://127.0.0.1:$PRIMARY_PORT/admin/api/v1/downstream-tokens"
persistence_token_id=$(jq -er '.id' "$CLIENT_DIR/persistence-token-response.json")
jq -er '.token' "$CLIENT_DIR/persistence-token-response.json" \
    > "$CLIENT_DIR/persistence-token"
rm -f "$CLIENT_DIR/persistence-token-response.json"
write_curl_config "$CLIENT_DIR/persistence-token" "$CLIENT_DIR/persistence.curl"

curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/persistence.curl" \
    --output "$CLIENT_DIR/models.json" "http://127.0.0.1:$PRIMARY_PORT/v1/models"
jq -e '.data[0].id == "z-ai/glm-5.2"' "$CLIENT_DIR/models.json" >/dev/null
printf '%s' '{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"todo7 qa"}],"stream":false}' \
    > "$CLIENT_DIR/chat.json"
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/persistence.curl" \
    --header 'Content-Type: application/json' --data-binary "@$CLIENT_DIR/chat.json" \
    --output "$CLIENT_DIR/chat-response.json" \
    "http://127.0.0.1:$PRIMARY_PORT/v1/chat/completions"
jq -e '.choices[0].message.content == "candidate fake upstream ok"' \
    "$CLIENT_DIR/chat-response.json" >/dev/null
printf '%s' '{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"todo7 qa"}],"stream":true}' \
    > "$CLIENT_DIR/chat-stream.json"
curl --silent --show-error --fail-with-body --no-buffer \
    --config "$CLIENT_DIR/persistence.curl" --header 'Content-Type: application/json' \
    --data-binary "@$CLIENT_DIR/chat-stream.json" \
    --output "$CLIENT_DIR/chat-stream-response.txt" \
    "http://127.0.0.1:$PRIMARY_PORT/v1/chat/completions"
grep -Fq 'candidate fake upstream ok' "$CLIENT_DIR/chat-stream-response.txt"
[ "$(grep -Fxc 'data: [DONE]' "$CLIENT_DIR/chat-stream-response.txt")" -eq 1 ]

database_failure_app_id=$(primary_compose ps -q app)
[ -n "$database_failure_app_id" ] || fail database_failure_app_missing
stopped_db_status=200
degraded_observed_epoch_ns=0
primary_compose stop db >/dev/null &
db_stop_pid=$!
for _ in $(seq 1 45); do
    stopped_db_status=$(curl --silent --connect-timeout 1 --max-time 1 \
        --header 'Host: 127.0.0.1:2456' \
        --output "$CLIENT_DIR/stopped-db-health-probe.json" --write-out '%{http_code}' \
        "http://127.0.0.1:$PRIMARY_PORT/health" 2>/dev/null || true)
    if [ "$stopped_db_status" = 503 ] && jq -e \
        '. == {"status":"degraded","ready":false}' \
        "$CLIENT_DIR/stopped-db-health-probe.json" >/dev/null 2>&1; then
        mv "$CLIENT_DIR/stopped-db-health-probe.json" \
            "$CLIENT_DIR/stopped-db-health.json"
        degraded_observed_epoch_ns=$(date -u +%s%N)
        break
    fi
    sleep 0.1
done
wait "$db_stop_pid" || fail stopped_database_stop_failed
[ "$stopped_db_status" = 503 ] || fail stopped_database_not_observed
jq -e '. == {"status":"degraded","ready":false}' \
    "$CLIENT_DIR/stopped-db-health.json" >/dev/null \
    || fail stopped_database_health_body_invalid
[ "$degraded_observed_epoch_ns" -gt 0 ] || fail stopped_database_degraded_time_missing
old_app_exit_state=$(wait_container_exit "$database_failure_app_id") \
    || fail stopped_database_old_app_did_not_exit
old_app_finished_at=$(docker inspect --format '{{.State.FinishedAt}}' \
    "$database_failure_app_id") || fail stopped_database_old_app_exit_unavailable
old_app_exit_epoch_ns=$(date -u --date="$old_app_finished_at" +%s%N) \
    || fail stopped_database_old_app_exit_time_invalid
old_app_grace_ns=$((old_app_exit_epoch_ns - degraded_observed_epoch_ns))
old_app_grace_ms=$((old_app_grace_ns / 1000000))
[ "$old_app_grace_ms" -ge 1500 ] && [ "$old_app_grace_ms" -le 15000 ] \
    || fail stopped_database_grace_invalid
[ "$(primary_compose ps -a -q app)" = "$database_failure_app_id" ] \
    || fail stopped_database_old_app_identity_changed
old_app_exit_code=$(docker inspect --format '{{.State.ExitCode}}' \
    "$database_failure_app_id") || fail stopped_database_old_app_exit_unavailable
[[ "$old_app_exit_code" =~ ^[0-9]+$ ]] || fail stopped_database_old_app_exit_invalid
primary_compose start db >/dev/null
wait_healthy primary db
primary_compose up --detach --force-recreate --no-deps app
wait_healthy primary app
recreated_app_id=$(primary_compose ps -q app)
[ -n "$recreated_app_id" ] && [ "$recreated_app_id" != "$database_failure_app_id" ] \
    || fail stopped_database_old_app_not_replaced
[ "$(api_status "$CLIENT_DIR/persistence.curl" "$PRIMARY_PORT" /v1/models)" = 200 ] \
    || fail restart_persistence_failed

printf '%s' nblb_admin_ > "$CLIENT_DIR/admin-token-next"
openssl rand -hex 32 | tr -d '\n' >> "$CLIENT_DIR/admin-token-next"
root_helper \
    --mount "type=bind,source=$CLIENT_DIR/admin-token-next,target=/source/admin_token,readonly" \
    --mount "type=bind,source=$PRIMARY_SECRET_DIR,target=/secrets" \
    "$IMAGE_DIGEST" -c 'cp /source/admin_token /secrets/.admin_token.next; chmod 0600 /secrets/.admin_token.next; chown 0:0 /secrets/.admin_token.next; mv /secrets/.admin_token.next /secrets/admin_token; sync -f /secrets/admin_token; sync -f /secrets'
primary_compose up --detach --force-recreate --no-deps app
wait_healthy primary app
write_curl_config "$CLIENT_DIR/admin-token-next" "$CLIENT_DIR/admin-next.curl"
old_admin_status=$(api_status "$CLIENT_DIR/admin-original.curl" "$PRIMARY_PORT" \
    /admin/api/v1/overview)
new_admin_status=$(api_status "$CLIENT_DIR/admin-next.curl" "$PRIMARY_PORT" \
    /admin/api/v1/overview)
[ "$old_admin_status" = 401 ] && [ "$new_admin_status" = 200 ] \
    || fail admin_rotation_failed

root_helper \
    --mount "type=bind,source=$CLIENT_DIR/admin-token-next,target=/source/admin_token,readonly" \
    --mount "type=bind,source=$RESTORE_SECRET_DIR,target=/secrets" \
    "$IMAGE_DIGEST" -c 'cp /source/admin_token /secrets/.admin_token.next; chmod 0600 /secrets/.admin_token.next; chown 0:0 /secrets/.admin_token.next; mv /secrets/.admin_token.next /secrets/admin_token; sync -f /secrets/admin_token; sync -f /secrets'

run_missing_app_secret admin_token
run_missing_app_secret vault_master_key
run_missing_app_secret db_password
run_wrong_app_secret admin_token
run_wrong_app_secret vault_master_key
run_wrong_app_secret db_password

primary_compose stop app >/dev/null
primary_app=$(primary_compose ps -a -q app)
primary_db=$(primary_compose ps -q db)
backup_id="todo7-${RUN_ID,,}"
NBLB_OPERATION_LOCK_DIR="$OPERATION_LOCK_DIR" "$ROOT/scripts/ops/backup.sh" \
    --db-container "$primary_db" --app-container "$primary_app" \
    --helper-image "$IMAGE_DIGEST" \
    --vault-key-file "$PRIMARY_SECRET_DIR/vault_master_key" \
    --database-root "$BACKUP_BASE/database" \
    --key-root "$BACKUP_BASE/key" \
    --manifest-root "$BACKUP_BASE/manifest" \
    --backup-id "$backup_id" > "$EVIDENCE_DIR/backup.json"
jq -e '.status == "PASS" and .restored_state_matches == false' \
    "$EVIDENCE_DIR/backup.json" >/dev/null
pair_id=$(jq -er '.pair_id' "$EVIDENCE_DIR/backup.json")

primary_compose start app >/dev/null
wait_healthy primary app
[ "$(api_status "$CLIENT_DIR/persistence.curl" "$PRIMARY_PORT" /v1/models)" = 200 ] \
    || fail post_backup_restart_failed

RESTORE_STARTED=1
restore_compose up --detach db
wait_healthy restore db
restore_db=$(restore_compose ps -q db)
NBLB_OPERATION_LOCK_DIR="$OPERATION_LOCK_DIR" "$ROOT/scripts/ops/restore.sh" \
    --db-container "$restore_db" --helper-image "$IMAGE_DIGEST" \
    --database-directory "$BACKUP_BASE/database/$backup_id" \
    --key-directory "$BACKUP_BASE/key/$backup_id" \
    --manifest "$BACKUP_BASE/manifest/$backup_id/manifest.json" \
    --target-secret-dir "$RESTORE_SECRET_DIR" > "$EVIDENCE_DIR/restore.json"
jq -e --arg pair_id "$pair_id" \
    '.status == "PASS" and .restored_state_matches == true and .pair_id == $pair_id' \
    "$EVIDENCE_DIR/restore.json" >/dev/null

restore_compose up --detach fake-nvidia
wait_healthy restore fake-nvidia
restore_compose run --rm --no-deps migrate >/dev/null
restore_compose up --detach app loopback
wait_healthy restore app
[ "$(api_status "$CLIENT_DIR/persistence.curl" "$RESTORE_PORT" /v1/models)" = 200 ] \
    || fail restored_token_auth_failed
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/persistence.curl" \
    --header 'Content-Type: application/json' --data-binary "@$CLIENT_DIR/chat.json" \
    --output "$CLIENT_DIR/restored-chat-response.json" \
    "http://127.0.0.1:$RESTORE_PORT/v1/chat/completions"
jq -e '.choices[0].message.content == "candidate fake upstream ok"' \
    "$CLIENT_DIR/restored-chat-response.json" >/dev/null
curl --silent --show-error --fail-with-body --config "$CLIENT_DIR/admin-next.curl" \
    --output "$CLIENT_DIR/restored-upstreams.json" \
    "http://127.0.0.1:$RESTORE_PORT/admin/api/v1/upstream-keys"
jq -e --arg key_id "$key_id" --arg fingerprint "$key_fingerprint" \
    '.items | length == 1 and .[0].id == $key_id and .[0].fingerprint == $fingerprint' \
    "$CLIENT_DIR/restored-upstreams.json" >/dev/null

restore_compose stop app >/dev/null
docker exec --user 70 "$restore_db" \
    psql --no-psqlrc --set ON_ERROR_STOP=1 --username nvidia_build_lb \
    --dbname nvidia_build_lb \
    --command "UPDATE alembic_version SET version_num='corrupt_revision';" >/dev/null
set +e
corrupt_output=$(restore_compose run --rm --no-deps migrate 2>&1)
corrupt_migration_status=$?
set -e
[ "$corrupt_migration_status" -eq 1 ] \
    && printf '%s\n' "$corrupt_output" | grep -Fq migration_failed \
    || fail corrupt_migration_accepted
unset corrupt_output
docker exec --user 70 "$restore_db" \
    psql --no-psqlrc --set ON_ERROR_STOP=1 --username nvidia_build_lb \
    --dbname nvidia_build_lb \
    --command "UPDATE alembic_version SET version_num='0004_vault_key_verifier';" >/dev/null
restore_compose run --rm --no-deps migrate >/dev/null

CODEX_AFTER=$(curl --silent --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:2455/health 2>/dev/null || true)
[ "$CODEX_AFTER" = 200 ] || fail codex_lb_changed

uv run python -m scripts.qa.source_manifest --root "$ROOT" \
    --output "$CLIENT_DIR/source-manifest-final.json"
cmp "$SOURCE_MANIFEST" "$CLIENT_DIR/source-manifest-final.json" \
    || fail source_manifest_changed
[ "$(docker image inspect --format '{{.Id}}' "$IMAGE_DIGEST")" = "$IMAGE_DIGEST" ] \
    || fail image_digest_changed
[ "$(docker image inspect --format '{{.Id}}' "$POSTGRES_IMAGE_DIGEST")" = "$POSTGRES_IMAGE_DIGEST" ] \
    || fail postgres_image_digest_changed

cmp "$CLIENT_DIR/source-manifest.json" "$EVIDENCE_DIR/source-manifest.json" \
    || fail source_manifest_evidence_changed
jq -n \
    --arg run_id "$RUN_ID" --arg status PASS \
    --arg image_digest "$IMAGE_DIGEST" --arg postgres_image_digest "$POSTGRES_IMAGE_DIGEST" \
    --arg source_sha256 "$source_sha256" --arg key_id "$key_id" \
    --arg source_manifest_sha256 "$source_manifest_sha256" \
    --arg key_fingerprint "$key_fingerprint" --arg token_id "$persistence_token_id" \
    --arg pair_id "$pair_id" \
    '{schema_version:3,run_id:$run_id,status:$status,image_digest:$image_digest,postgres_image_digest:$postgres_image_digest,source_tree_sha256:$source_sha256,source_manifest_sha256:$source_manifest_sha256,checks:{static_analysis:true,full_test_suite:true,production_compose_render:true,concurrent_migration_serialization:true,health:true,models:true,nonstream_chat:true,stream_chat_done_once:true,restart_persistence:true,admin_rotation:true,separate_backup:true,isolated_restore:true,restored_chat:true,existing_codex_lb_preserved:true,exact_database_failure_503:true,database_failure_grace_exit:true,old_app_replaced_after_exit:true,source_manifest_final_byte_exact:true,app_postgres_pair_preserved:true},safe_identity:{upstream_key_id:$key_id,upstream_fingerprint:$key_fingerprint,persistence_downstream_token_id:$token_id,backup_pair_id:$pair_id}}' \
    > "$EVIDENCE_DIR/manual-qa.json"
jq -n \
    --arg run_id "$RUN_ID" --arg status PASS \
    --argjson migration_a "$migration_status_a" --argjson migration_b "$migration_status_b" \
    --arg stopped_db_status "$stopped_db_status" \
    --arg old_app_exit_state "$old_app_exit_state" \
    --argjson old_app_grace_ms "$old_app_grace_ms" \
    --argjson old_app_exit_code "$old_app_exit_code" \
    --argjson revoked_status "$revoked_status" \
    --argjson old_admin_status "$old_admin_status" \
    --argjson new_admin_status "$new_admin_status" \
    --argjson corrupt_migration_status "$corrupt_migration_status" \
    --argjson codex_before "$CODEX_BEFORE" --argjson codex_after "$CODEX_AFTER" \
    '{schema_version:2,run_id:$run_id,status:$status,checks:{concurrent_migration_exit_statuses:[$migration_a,$migration_b],database_failure:{health_status:$stopped_db_status,health_body_exact:true,configured_grace_seconds:2,observed_degraded_to_exit_milliseconds:$old_app_grace_ms,old_app_same_container_exited:true,old_app_exit_state:$old_app_exit_state,old_app_exit_code:$old_app_exit_code,replacement_container_new:true},revoked_downstream_status:$revoked_status,old_admin_after_rotation_status:$old_admin_status,new_admin_after_rotation_status:$new_admin_status,corrupt_migration_exit_status:$corrupt_migration_status,missing_admin_secret_exit:70,missing_vault_secret_exit:70,missing_database_secret_exit:70,wrong_admin_secret_exit:70,wrong_vault_secret_exit:70,wrong_database_secret_exit:70,codex_lb_before:$codex_before,codex_lb_after:$codex_after}}' \
    > "$EVIDENCE_DIR/adversarial.json"
