#!/bin/bash
set -Eeuo pipefail
umask 077

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

EVIDENCE_DIR=${EVIDENCE_DIR:-.omo/evidence/task-6b-nvidia-build-lb}
IMAGE_DIGEST=${IMAGE_DIGEST:-}
QA_PORT=2456
TASK_LABEL=todo6b-browser-prod
BASE_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
SECRET_DIR=""
CLIENT_DIR=""
NODE_RUNTIME_DIR=""
POSTGRES_TAG="nvidia-build-lb-postgres:todo6b-${BASE_RUN_ID,,}"
PROJECT=""
COMPOSE_STARTED=0
RESUME_MODE=0
QA_SOURCE_DIR=""
BASELINE_BROWSER_PIDS=""
BASELINE_DRIVER_PIDS=""
BASELINE_LIGHTHOUSE_PIDS=""

assert_no_symlink_components() {
    local current

    current=$(realpath -ms "$1")
    while :; do
        [ ! -L "$current" ] || return 1
        [ "$current" = / ] && break
        current=${current%/*}
        [ -n "$current" ] || current=/
    done
}

regular_file_no_symlink() {
    [ -f "$1" ] && [ ! -L "$1" ] && assert_no_symlink_components "$1"
}

expected_evidence=$(realpath -ms "$ROOT/.omo/evidence/task-6b-nvidia-build-lb")
actual_evidence=$(realpath -ms "$EVIDENCE_DIR")
[ "$actual_evidence" = "$expected_evidence" ] || {
    printf '%s\n' 'invalid_evidence_directory' >&2
    exit 64
}
assert_no_symlink_components "$EVIDENCE_DIR" || {
    printf '%s\n' 'evidence_path_is_symlinked' >&2
    exit 64
}
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    printf '%s\n' 'invalid_image_digest' >&2
    exit 64
}

for command in awk chmod cmp cp curl docker find flock git id jq mkdir mktemp mv node npm openssl \
    pgrep realpath rm rmdir sha256sum sort ss stat tr wc; do
    command -v "$command" >/dev/null 2>&1 || {
        printf '%s\n' 'required_command_unavailable' >&2
        exit 69
    }
done
[ -x "$ROOT/.venv/bin/python" ] || {
    printf '%s\n' 'qa_python_unavailable' >&2
    exit 69
}

mkdir -p "$(dirname "$EVIDENCE_DIR")"
claim_path="$EVIDENCE_DIR.claim"
assert_no_symlink_components "$claim_path" || {
    printf '%s\n' 'evidence_claim_is_symlinked' >&2
    exit 64
}
[ ! -e "$claim_path" ] || [ -f "$claim_path" ] || {
    printf '%s\n' 'invalid_evidence_claim' >&2
    exit 64
}
exec 9<>"$claim_path"
[ ! -L "$claim_path" ] && [ -f "$claim_path" ] || exit 64
[ "$(stat -Lc '%d:%i' "/proc/$$/fd/9")" = "$(stat -Lc '%d:%i' "$claim_path")" ] || exit 64
if ! flock -n 9; then
    printf '%s\n' 'evidence_exists' >&2
    exit 64
fi

compose() {
    docker compose --project-directory "$QA_SOURCE_DIR" \
        -f "$QA_SOURCE_DIR/compose.qa.yml" -p "$PROJECT" "$@"
}

qa_python() {
    PYTHONPATH="$QA_SOURCE_DIR/src:$QA_SOURCE_DIR" \
        "$ROOT/.venv/bin/python" -P "$@"
}

root_secret_helper() {
    docker run --rm --network none \
        --label "nvidia-build-lb.task=$TASK_LABEL" \
        --label "nvidia-build-lb.run=$BASE_RUN_ID" \
        --entrypoint sh \
        --mount "type=bind,source=$SECRET_DIR,target=/secrets" \
        postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609 \
        -c "$1"
}

label_count() {
    docker ps -aq --filter "label=nvidia-build-lb.task=$TASK_LABEL" | wc -l | tr -d ' '
}

network_count() {
    docker network ls -q --filter "label=nvidia-build-lb.task=$TASK_LABEL" | wc -l | tr -d ' '
}

volume_count() {
    docker volume ls -q --filter "label=nvidia-build-lb.task=$TASK_LABEL" | wc -l | tr -d ' '
}

postgres_image_count() {
    docker image ls --quiet --no-trunc "$POSTGRES_TAG" \
        | LC_ALL=C sort -u \
        | awk 'NF { count += 1 } END { print count + 0 }'
}

temporary_postgres_image_count() {
    docker image ls --quiet --no-trunc \
        --filter "label=nvidia-build-lb.task=$TASK_LABEL" \
        | LC_ALL=C sort -u \
        | awk 'NF { count += 1 } END { print count + 0 }'
}

port_count() {
    ss -H -ltn "sport = :$QA_PORT" 2>/dev/null | wc -l | tr -d ' '
}

temporary_directory_count() {
    local pattern=$1

    find /tmp -mindepth 1 -maxdepth 1 -name "$pattern" -printf '.\n' \
        | wc -l \
        | tr -d ' '
}

browser_temporary_directory_count() {
    local count
    local pattern
    local total=0

    for pattern in \
        'nblb-lighthouse-*' \
        'nblb-native-zoom-*' \
        'nblb-fontconfig-*' \
        'playwright-artifacts-*' \
        'playwright_chromiumdev_profile-*'; do
        count=$(temporary_directory_count "$pattern") || return 1
        total=$((total + count))
    done
    printf '%s\n' "$total"
}

process_identity() {
    local pid=$1
    local stat_line
    local suffix
    local -a fields

    stat_line=$(<"/proc/$pid/stat") || return 1
    suffix=${stat_line##*) }
    [ "$suffix" != "$stat_line" ] || return 1
    read -r -a fields <<< "$suffix"
    [ "${#fields[@]}" -ge 20 ] || return 1
    [[ "${fields[19]}" =~ ^[0-9]+$ ]] || return 1
    printf '%s:%s\n' "$pid" "${fields[19]}"
}

collect_process_identities() {
    local pattern=$1
    local identity
    local identities=""
    local output
    local pid
    local status=0

    output=$(pgrep -f "$pattern") || status=$?
    case "$status" in
        0) ;;
        1) printf '%s' '' ;;
        *) return "$status" ;;
    esac
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        identity=$(process_identity "$pid") || return 1
        identities+="$identity "
    done <<< "$output"
    printf '%s' "$identities"
}

new_process_count() {
    local pattern=$1
    local baseline=$2
    local output
    local identity
    local pid
    local count=0
    local status=0

    output=$(pgrep -f "$pattern") || status=$?
    case "$status" in
        0) ;;
        1) output="" ;;
        *) return "$status" ;;
    esac
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        identity=$(process_identity "$pid") || return 1
        case " $baseline " in
            *" $identity "*) ;;
            *) count=$((count + 1)) ;;
        esac
    done <<< "$output"
    printf '%s\n' "$count"
}

atomic_open() {
    ATOMIC_DESTINATION=$1
    ATOMIC_TEMPORARY="$ATOMIC_DESTINATION.tmp.$BASE_RUN_ID"
    ATOMIC_FD=""
    [ ! -e "$ATOMIC_TEMPORARY" ] && [ ! -L "$ATOMIC_TEMPORARY" ] || return 1
    set -C
    if ! exec {ATOMIC_FD}>"$ATOMIC_TEMPORARY"; then
        set +C
        return 1
    fi
    set +C
}

atomic_abort() {
    [ -z "$ATOMIC_FD" ] || exec {ATOMIC_FD}>&-
    rm -f -- "$ATOMIC_TEMPORARY"
    ATOMIC_FD=""
}

atomic_close() {
    if [ -n "$ATOMIC_FD" ]; then
        exec {ATOMIC_FD}>&-
        ATOMIC_FD=""
    fi
}

atomic_commit() {
    atomic_close
    mv -T -- "$ATOMIC_TEMPORARY" "$ATOMIC_DESTINATION"
}

commit_cleanup_receipt() {
    atomic_close
    if ! cp --remove-destination "$ATOMIC_TEMPORARY" "$EVIDENCE_DIR/cleanup.json"; then
        rm -f -- "$ATOMIC_TEMPORARY"
        return 1
    fi
    atomic_commit
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
    postgres_images=$(postgres_image_count) \
        || { postgres_images=-1; cleanup_error=1; }
    if [ "$postgres_images" -gt 0 ]; then
        docker image rm "$POSTGRES_TAG" >/dev/null 2>&1 || cleanup_error=1
    fi
    if [ -n "$SECRET_DIR" ] && [ -d "$SECRET_DIR" ]; then
        root_secret_helper 'rm -f /secrets/*' >/dev/null 2>&1 || cleanup_error=1
        rmdir "$SECRET_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi
    if [ -n "$CLIENT_DIR" ] && [ -e "$CLIENT_DIR" ]; then
        chmod -R u+w "$CLIENT_DIR" >/dev/null 2>&1 || cleanup_error=1
        rm -rf -- "$CLIENT_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi
    containers=$(label_count) || { containers=-1; cleanup_error=1; }
    networks=$(network_count) || { networks=-1; cleanup_error=1; }
    volumes=$(volume_count) || { volumes=-1; cleanup_error=1; }
    listeners=$(port_count) || { listeners=-1; cleanup_error=1; }
    postgres_images=$(temporary_postgres_image_count) \
        || { postgres_images=-1; cleanup_error=1; }
    secret_directories=$(temporary_directory_count 'nblb-browser-prod-secrets.*') \
        || { secret_directories=-1; cleanup_error=1; }
    client_directories=$(temporary_directory_count 'nblb-browser-prod-client.*') \
        || { client_directories=-1; cleanup_error=1; }
    browser_directories=$(browser_temporary_directory_count) \
        || { browser_directories=-1; cleanup_error=1; }
    browser_processes=$(new_process_count \
        '/ms-playwright/(chromium|chromium_headless_shell)-1228/' \
        "$BASELINE_BROWSER_PIDS") \
        || { browser_processes=-1; cleanup_error=1; }
    playwright_drivers=$(new_process_count \
        'playwright/driver/package/cli.js run-driver' \
        "$BASELINE_DRIVER_PIDS") \
        || { playwright_drivers=-1; cleanup_error=1; }
    lighthouse_processes=$(new_process_count \
        'node_modules/.bin/lighthouse|lighthouse/cli/index.js' \
        "$BASELINE_LIGHTHOUSE_PIDS") \
        || { lighthouse_processes=-1; cleanup_error=1; }
    final_status=$trigger_status
    cleanup_status=PASS
    cleanup_phase=fresh
    [ "$RESUME_MODE" -eq 0 ] || cleanup_phase=resume
    cleanup_receipt="$EVIDENCE_DIR/cleanup-$cleanup_phase.json"
    if [ "$cleanup_error" -ne 0 ] \
        || [ "$containers" -ne 0 ] \
        || [ "$networks" -ne 0 ] \
        || [ "$volumes" -ne 0 ] \
        || [ "$listeners" -ne 0 ] \
        || [ "$postgres_images" -ne 0 ] \
        || [ "$secret_directories" -ne 0 ] \
        || [ "$client_directories" -ne 0 ] \
        || [ "$browser_directories" -ne 0 ] \
        || [ "$browser_processes" -ne 0 ] \
        || [ "$playwright_drivers" -ne 0 ] \
        || [ "$lighthouse_processes" -ne 0 ]; then
        cleanup_status=FAIL
        final_status=1
    fi
    atomic_open "$cleanup_receipt" || final_status=1
    if [ -n "$ATOMIC_FD" ]; then
        write_status=0
        jq -n \
        --arg status "$cleanup_status" \
        --argjson trigger "$trigger_status" \
        --argjson final "$final_status" \
        --argjson containers "$containers" \
        --argjson networks "$networks" \
        --argjson volumes "$volumes" \
        --argjson listeners "$listeners" \
        --argjson postgres "$postgres_images" \
        --argjson secrets "$secret_directories" \
        --argjson clients "$client_directories" \
        --argjson browser_directories "$browser_directories" \
        --argjson browsers "$browser_processes" \
        --argjson drivers "$playwright_drivers" \
        --argjson lighthouse "$lighthouse_processes" \
        '{schema_version:1,status:$status,trigger_exit_status:$trigger,final_exit_status:$final,remaining:{containers:$containers,networks:$networks,volumes:$volumes,port_listeners:$listeners,temp_secret_directories:$secrets,temp_client_directories:$clients,browser_temporary_directories:$browser_directories,temporary_postgres_images:$postgres,browser_processes:$browsers,playwright_drivers:$drivers,lighthouse_processes:$lighthouse}}' \
            >&"$ATOMIC_FD" || write_status=1
        if [ "$write_status" -eq 0 ]; then
            commit_cleanup_receipt || final_status=1
        else
            atomic_abort
            final_status=1
        fi
    fi
    if [ "$RESUME_MODE" -eq 1 ] && [ "$final_status" -eq 0 ]; then
        printf '%s\n' 'browser_prod_gate=PASS'
    fi
    exit "$final_status"
}

write_process_baseline() {
    jq -n \
        --arg browser "$BASELINE_BROWSER_PIDS" \
        --arg driver "$BASELINE_DRIVER_PIDS" \
        --arg lighthouse "$BASELINE_LIGHTHOUSE_PIDS" \
        '{schema_version:1,browser_processes:($browser|split(" ")|map(select(length > 0))),playwright_driver_processes:($driver|split(" ")|map(select(length > 0))),lighthouse_processes:($lighthouse|split(" ")|map(select(length > 0)))}' \
        > "$EVIDENCE_DIR/process-baseline.json"
}

load_process_baseline() {
    jq -e '
        .schema_version == 1
        and (.browser_processes | type == "array" and all(test("^[0-9]+:[0-9]+$")))
        and (.playwright_driver_processes | type == "array" and all(test("^[0-9]+:[0-9]+$")))
        and (.lighthouse_processes | type == "array" and all(test("^[0-9]+:[0-9]+$")))
    ' "$EVIDENCE_DIR/process-baseline.json" >/dev/null
    BASELINE_BROWSER_PIDS=$(jq -r '.browser_processes | join(" ")' \
        "$EVIDENCE_DIR/process-baseline.json")
    BASELINE_DRIVER_PIDS=$(jq -r '.playwright_driver_processes | join(" ")' \
        "$EVIDENCE_DIR/process-baseline.json")
    BASELINE_LIGHTHOUSE_PIDS=$(jq -r '.lighthouse_processes | join(" ")' \
        "$EVIDENCE_DIR/process-baseline.json")
}

validate_fresh_cleanup() {
    jq -e '. == {
        schema_version: 1,
        status: "PASS",
        trigger_exit_status: 75,
        final_exit_status: 75,
        remaining: {
            containers: 0,
            networks: 0,
            volumes: 0,
            port_listeners: 0,
            temp_secret_directories: 0,
            temp_client_directories: 0,
            browser_temporary_directories: 0,
            temporary_postgres_images: 0,
            browser_processes: 0,
            playwright_drivers: 0,
            lighthouse_processes: 0
        }
    }' "$EVIDENCE_DIR/cleanup-fresh.json" >/dev/null
}

assert_preflight_clean() {
    local browser_processes
    local playwright_drivers
    local lighthouse_processes

    browser_processes=$(new_process_count \
        '/ms-playwright/(chromium|chromium_headless_shell)-1228/' \
        "$BASELINE_BROWSER_PIDS")
    playwright_drivers=$(new_process_count \
        'playwright/driver/package/cli.js run-driver' \
        "$BASELINE_DRIVER_PIDS")
    lighthouse_processes=$(new_process_count \
        'node_modules/.bin/lighthouse|lighthouse/cli/index.js' \
        "$BASELINE_LIGHTHOUSE_PIDS")
    [ "$(label_count)" -eq 0 ]
    [ "$(network_count)" -eq 0 ]
    [ "$(volume_count)" -eq 0 ]
    [ "$(port_count)" -eq 0 ]
    [ "$(temporary_postgres_image_count)" -eq 0 ]
    [ "$(temporary_directory_count 'nblb-browser-prod-secrets.*')" -eq 0 ]
    [ "$(temporary_directory_count 'nblb-browser-prod-client.*')" -eq 0 ]
    [ "$(browser_temporary_directory_count)" -eq 0 ]
    [ "$browser_processes" -eq 0 ]
    [ "$playwright_drivers" -eq 0 ]
    [ "$lighthouse_processes" -eq 0 ]
}

bootstrap_python() {
    PYTHONPATH="$ROOT" "$ROOT/.venv/bin/python" -P "$@"
}

materialize_source_snapshot() {
    local manifest=$1
    local receipt=$2

    QA_SOURCE_DIR="$CLIENT_DIR/source"
    bootstrap_python -m scripts.qa.source_snapshot \
        --root "$ROOT" \
        --manifest "$manifest" \
        --output "$QA_SOURCE_DIR" \
        --receipt "$receipt"
}

assert_source_unchanged() {
    local after="$CLIENT_DIR/source-manifest-after.json"

    qa_python -m scripts.qa.source_manifest --root "$ROOT" --output "$after"
    cmp "$EVIDENCE_DIR/source-manifest.json" "$after"
}

write_stack_cleanup() {
    local run_name=$1
    local containers
    local networks
    local volumes
    local listeners
    local browser_processes
    local playwright_drivers
    local lighthouse_processes
    local browser_directories
    local postgres_images

    containers=$(label_count)
    networks=$(network_count)
    volumes=$(volume_count)
    listeners=$(port_count)
    browser_processes=$(new_process_count \
        '/ms-playwright/(chromium|chromium_headless_shell)-1228/' \
        "$BASELINE_BROWSER_PIDS")
    playwright_drivers=$(new_process_count \
        'playwright/driver/package/cli.js run-driver' \
        "$BASELINE_DRIVER_PIDS")
    lighthouse_processes=$(new_process_count \
        'node_modules/.bin/lighthouse|lighthouse/cli/index.js' \
        "$BASELINE_LIGHTHOUSE_PIDS")
    browser_directories=$(browser_temporary_directory_count)
    postgres_images=$(temporary_postgres_image_count)
    [ "$containers" -eq 0 ] \
        && [ "$networks" -eq 0 ] \
        && [ "$volumes" -eq 0 ] \
        && [ "$listeners" -eq 0 ] \
        && [ "$browser_processes" -eq 0 ] \
        && [ "$playwright_drivers" -eq 0 ] \
        && [ "$lighthouse_processes" -eq 0 ]
    [ "$browser_directories" -eq 0 ]
    [ "$postgres_images" -eq 1 ]
    jq -n \
        --arg run "$run_name" \
        --argjson containers "$containers" \
        --argjson networks "$networks" \
        --argjson volumes "$volumes" \
        --argjson listeners "$listeners" \
        --argjson browsers "$browser_processes" \
        --argjson drivers "$playwright_drivers" \
        --argjson lighthouse "$lighthouse_processes" \
        --argjson browser_directories "$browser_directories" \
        --argjson postgres "$postgres_images" \
        '{schema_version:1,status:"PASS",run:$run,remaining:{containers:$containers,networks:$networks,volumes:$volumes,port_listeners:$listeners,browser_temporary_directories:$browser_directories,temporary_postgres_images:$postgres,browser_processes:$browsers,playwright_drivers:$drivers,lighthouse_processes:$lighthouse}}' \
        > "$EVIDENCE_DIR/runs/$run_name/stack-cleanup.json"
}

if [ -e "$EVIDENCE_DIR" ]; then
    for required in \
        review-request.json \
        visual-review-a.json \
        visual-review-b.json \
        source-manifest.json \
        source-snapshot.json \
        process-baseline.json \
        cleanup-fresh.json; do
        regular_file_no_symlink "$EVIDENCE_DIR/$required" || {
            printf '%s\n' 'evidence_contains_non_regular_input' >&2
            exit 64
        }
    done
    RESUME_MODE=1
    validate_fresh_cleanup
    expected_baseline_sha=$(jq -er .process_baseline_sha256 \
        "$EVIDENCE_DIR/review-request.json")
    observed_baseline_sha=$(sha256sum "$EVIDENCE_DIR/process-baseline.json" \
        | awk 'NR == 1 { print $1 }')
    [ "$observed_baseline_sha" = "$expected_baseline_sha" ] || exit 1
    load_process_baseline
    trap cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    assert_preflight_clean
    CLIENT_DIR=$(mktemp -d /tmp/nblb-browser-prod-client.XXXXXX)
    resume_manifest="$CLIENT_DIR/source-manifest-before.json"
    bootstrap_python -m scripts.qa.source_manifest --root "$ROOT" --output "$resume_manifest"
    cmp "$EVIDENCE_DIR/source-manifest.json" "$resume_manifest"
    current_source=$(jq -er .source_tree_sha256 "$resume_manifest")
    requested_source=$(jq -er .source_tree_sha256 "$EVIDENCE_DIR/review-request.json")
    requested_image=$(jq -er .image_digest "$EVIDENCE_DIR/review-request.json")
    [ "$current_source" = "$requested_source" ] && [ "$IMAGE_DIGEST" = "$requested_image" ] \
        || exit 1
    materialize_source_snapshot "$resume_manifest" "$CLIENT_DIR/source-snapshot.json"
    cmp "$EVIDENCE_DIR/source-snapshot.json" "$CLIENT_DIR/source-snapshot.json"
    qa_python -m tests.ui.browser_prod_verify \
        --evidence-dir "$EVIDENCE_DIR" \
        --source-sha "$current_source" \
        --image-digest "$IMAGE_DIGEST"
    assert_source_unchanged
    exit 0
fi

if ! mkdir "$EVIDENCE_DIR" 2>/dev/null; then
    printf '%s\n' 'evidence_exists' >&2
    exit 64
fi
[ -d "$EVIDENCE_DIR" ] && [ ! -L "$EVIDENCE_DIR" ] \
    && assert_no_symlink_components "$EVIDENCE_DIR" || exit 64

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

BASELINE_BROWSER_PIDS=$(collect_process_identities \
    '/ms-playwright/(chromium|chromium_headless_shell)-1228/')
BASELINE_DRIVER_PIDS=$(collect_process_identities 'playwright/driver/package/cli.js run-driver')
BASELINE_LIGHTHOUSE_PIDS=$(collect_process_identities \
    'node_modules/.bin/lighthouse|lighthouse/cli/index.js')
write_process_baseline
assert_preflight_clean

mkdir "$EVIDENCE_DIR/runs"
SECRET_DIR=$(mktemp -d /tmp/nblb-browser-prod-secrets.XXXXXX)
CLIENT_DIR=$(mktemp -d /tmp/nblb-browser-prod-client.XXXXXX)
[ "$(port_count)" -eq 0 ] || exit 1

source_manifest="$EVIDENCE_DIR/source-manifest.json"
bootstrap_python -m scripts.qa.source_manifest --root "$ROOT" --output "$source_manifest"
source_hash=$(jq -er .source_tree_sha256 "$source_manifest")
materialize_source_snapshot "$source_manifest" "$EVIDENCE_DIR/source-snapshot.json"
[ "$(docker image inspect --format '{{.Id}}' "$IMAGE_DIGEST")" = "$IMAGE_DIGEST" ]
[ "$(docker image inspect --format '{{index .Config.Labels "nvidia-build-lb.source-sha256"}}' "$IMAGE_DIGEST")" = "$source_hash" ]

NODE_RUNTIME_DIR="$CLIENT_DIR/node-runtime"
mkdir "$NODE_RUNTIME_DIR"
cp "$QA_SOURCE_DIR/package.json" "$QA_SOURCE_DIR/package-lock.json" "$NODE_RUNTIME_DIR/"
npm ci --prefix "$NODE_RUNTIME_DIR" --ignore-scripts --no-audit --no-fund >/dev/null
export NBLB_LIGHTHOUSE_PATH="$NODE_RUNTIME_DIR/node_modules/.bin/lighthouse"

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
root_secret_helper 'chown 0:0 /secrets/* && chmod 0600 /secrets/admin_token /secrets/vault_master_key /secrets/db_password /secrets/server_key /secrets/server_cert && chmod 0644 /secrets/ca_cert'
docker run --rm --network none \
    --label "nvidia-build-lb.task=$TASK_LABEL" \
    --entrypoint sh \
    --mount "type=bind,source=$SECRET_DIR,target=/secrets,readonly" \
    --mount "type=bind,source=$CLIENT_DIR,target=/client" \
    postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609 \
    -c 'cp /secrets/admin_token /client/admin_token && chown "$1:$2" /client/admin_token && chmod 0600 /client/admin_token' \
    sh "$(id -u)" "$(id -g)"

docker buildx build --pull --no-cache --provenance=false \
    --build-arg SOURCE_DATE_EPOCH=0 \
    --output "type=docker,dest=$CLIENT_DIR/postgres-image.tar,rewrite-timestamp=true" \
    --file "$QA_SOURCE_DIR/docker/postgres.Dockerfile" \
    --label "nvidia-build-lb.task=$TASK_LABEL" \
    --tag "$POSTGRES_TAG" "$QA_SOURCE_DIR" >/dev/null
docker load --input "$CLIENT_DIR/postgres-image.tar" >/dev/null
rm -f "$CLIENT_DIR/postgres-image.tar"

export NBLB_CANDIDATE_IMAGE=$IMAGE_DIGEST
export NBLB_POSTGRES_IMAGE=$POSTGRES_TAG
export NBLB_QA_SECRET_DIR=$SECRET_DIR
export NBLB_QA_PORT=$QA_PORT
export NBLB_QA_TASK_LABEL=todo6b-browser-prod
export NBLB_QA_RUN_ID="${BASE_RUN_ID}-config"
PROJECT="nblb-todo6b-config-${BASE_RUN_ID,,}"
PROJECT=${PROJECT//[^a-z0-9_-]/-}
compose config --quiet

for run_name in run-a run-b; do
    export NBLB_QA_RUN_ID="${BASE_RUN_ID}-${run_name}"
    PROJECT="nblb-todo6b-${NBLB_QA_RUN_ID,,}"
    PROJECT=${PROJECT//[^a-z0-9_-]/-}
    COMPOSE_STARTED=1
    compose up --detach >/dev/null
    curl --silent --show-error --fail \
        --retry 60 --retry-delay 1 --retry-connrefused --retry-all-errors \
        --retry-max-time 90 \
        --header 'Host: 127.0.0.1:2456' \
        --output /dev/null "http://127.0.0.1:$QA_PORT/admin"
    qa_python -m tests.ui.browser_prod_gate \
        --evidence-dir "$EVIDENCE_DIR/runs/$run_name" \
        --admin-token-file "$CLIENT_DIR/admin_token" \
        --source-sha "$source_hash" \
        --image-digest "$IMAGE_DIGEST" \
        --run-name "$run_name"
    qa_python -m tests.ui.lighthouse_gate \
        --evidence-dir "$EVIDENCE_DIR/runs/$run_name" \
        --image-digest "$IMAGE_DIGEST"
    compose down --volumes --remove-orphans --timeout 20 >/dev/null
    COMPOSE_STARTED=0
    write_stack_cleanup "$run_name"
done

set +e
qa_python -m tests.ui.browser_prod_verify \
    --evidence-dir "$EVIDENCE_DIR" \
    --source-sha "$source_hash" \
    --image-digest "$IMAGE_DIGEST"
verify_status=$?
set -e
[ "$verify_status" -eq 75 ] || exit 1
assert_source_unchanged
printf '%s\n' 'browser_prod_gate=REVIEW_REQUIRED'
exit 75
