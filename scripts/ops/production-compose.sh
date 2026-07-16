#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 022

fail() {
    printf '%s\n' "${1:-production_compose_failed}" >&2
    exit 1
}

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
RUNTIME_CONFIG=${NBLB_RUNTIME_CONFIG_FILE:-/etc/nvidia-build-lb/runtime.env}
RUNTIME_LOCK=${NBLB_RUNTIME_LOCK_FILE:-$RUNTIME_CONFIG.lock}
RUNTIME_CONFIG_SHA256=""
RUNTIME_CONFIG_TEMPORARY=""
RECOVERY_APP_MUST_WITHDRAW=0
declare -A RUNTIME_VALUES=()

cleanup_runtime_state() {
    local original_status=$?
    local cleanup_failed=0
    trap - EXIT HUP INT TERM
    set +e
    if [ "$RECOVERY_APP_MUST_WITHDRAW" -eq 1 ]; then
        if compose stop app >/dev/null 2>&1; then
            RECOVERY_APP_MUST_WITHDRAW=0
        else
            printf '%s\n' runtime_recovery_exit_withdraw_failed >&2
            cleanup_failed=1
        fi
    fi
    if [ -n "$RUNTIME_CONFIG_TEMPORARY" ]; then
        if rm -f -- "$RUNTIME_CONFIG_TEMPORARY" >/dev/null 2>&1; then
            RUNTIME_CONFIG_TEMPORARY=""
        else
            printf '%s\n' runtime_config_temporary_cleanup_failed >&2
            cleanup_failed=1
        fi
    fi
    [ "$original_status" -ne 0 ] && return "$original_status"
    [ "$cleanup_failed" -eq 0 ] || return 1
    return "$original_status"
}

trap cleanup_runtime_state EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

file_sha256() {
    local digest remainder
    read -r digest remainder < <(sha256sum -- "$1") || return 1
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "$digest"
}

acquire_runtime_lock() {
    local kind=$1 metadata owner mode links
    case "$RUNTIME_LOCK" in /*) ;; *) fail runtime_config_lock_path_invalid ;; esac
    [ -f "$RUNTIME_LOCK" ] && [ ! -L "$RUNTIME_LOCK" ] \
        || fail runtime_config_lock_invalid
    metadata=$(stat -c '%u:%a:%h' "$RUNTIME_LOCK") \
        || fail runtime_config_lock_invalid
    IFS=: read -r owner mode links <<< "$metadata"
    { [ "$owner" = 0 ] || [ "$owner" = "$(id -u)" ]; } && [ "$links" = 1 ] \
        || fail runtime_config_lock_metadata_invalid
    case "$mode" in 600|640|644) ;; *) fail runtime_config_lock_metadata_invalid ;; esac
    exec 9<"$RUNTIME_LOCK" || fail runtime_config_lock_invalid
    if [ "$kind" = exclusive ]; then
        flock -n -x 9 || fail runtime_config_lock_busy
    else
        flock -n -s 9 || fail runtime_config_lock_busy
    fi
}

validate_caps() {
    local event_rows=$1 attempt_rows=$2 prune_rows=$3
    [[ "$event_rows" =~ ^[1-9][0-9]*$ ]] \
        && [ "$event_rows" -ge 1000 ] && [ "$event_rows" -le 1000000 ] \
        || fail runtime_config_event_cap_invalid
    [[ "$attempt_rows" =~ ^[1-9][0-9]*$ ]] \
        && [ "$attempt_rows" -ge 100 ] && [ "$attempt_rows" -le 400000 ] \
        || fail runtime_config_attempt_cap_invalid
    [[ "$prune_rows" =~ ^[1-9][0-9]*$ ]] \
        && [ "$prune_rows" -ge 6 ] && [ "$prune_rows" -le 5000 ] \
        || fail runtime_config_prune_batch_invalid
    [ $((2 * attempt_rows + 100)) -le "$event_rows" ] \
        || fail runtime_config_capacity_invariant_invalid
}

load_runtime_config() {
    case "$RUNTIME_CONFIG" in /*) ;; *) fail runtime_config_path_invalid ;; esac
    [ -f "$RUNTIME_CONFIG" ] && [ ! -L "$RUNTIME_CONFIG" ] \
        || fail runtime_config_invalid
    local metadata owner mode links line key value initial_sha final_sha
    metadata=$(stat -c '%u:%a:%h' "$RUNTIME_CONFIG") || fail runtime_config_invalid
    IFS=: read -r owner mode links <<< "$metadata"
    { [ "$owner" = 0 ] || [ "$owner" = "$(id -u)" ]; } \
        && [ "$links" = 1 ] \
        || fail runtime_config_metadata_invalid
    case "$mode" in 600|640|644) ;; *) fail runtime_config_metadata_invalid ;; esac
    initial_sha=$(file_sha256 "$RUNTIME_CONFIG") || fail runtime_config_invalid
    while IFS= read -r line || [ -n "$line" ]; do
        [ -z "$line" ] && continue
        case "$line" in \#*) continue ;; esac
        [[ "$line" =~ ^([A-Z0-9_]+)=([^[:space:]]+)$ ]] \
            || fail runtime_config_invalid
        key=${BASH_REMATCH[1]}
        value=${BASH_REMATCH[2]}
        case "$key" in
            NBLB_APP_REGISTRY_DIGEST|NBLB_POSTGRES_REGISTRY_DIGEST|NBLB_SECRET_DIR|NBLB_ADMIN_EVENT_MAX_ROWS|NBLB_ADMIN_ATTEMPT_MAX_ROWS|NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE) ;;
            *) fail runtime_config_unknown_key ;;
        esac
        [ -z "${RUNTIME_VALUES[$key]+present}" ] || fail runtime_config_duplicate_key
        RUNTIME_VALUES[$key]=$value
    done < "$RUNTIME_CONFIG"
    for key in NBLB_APP_REGISTRY_DIGEST NBLB_POSTGRES_REGISTRY_DIGEST \
        NBLB_SECRET_DIR NBLB_ADMIN_EVENT_MAX_ROWS NBLB_ADMIN_ATTEMPT_MAX_ROWS \
        NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE; do
        [ -n "${RUNTIME_VALUES[$key]:-}" ] || fail runtime_config_missing_key
    done
    [[ "${RUNTIME_VALUES[NBLB_APP_REGISTRY_DIGEST]}" =~ ^[0-9a-f]{64}$ ]] \
        || fail app_registry_digest_invalid
    [[ "${RUNTIME_VALUES[NBLB_POSTGRES_REGISTRY_DIGEST]}" =~ ^[0-9a-f]{64}$ ]] \
        || fail postgres_registry_digest_invalid
    case "${RUNTIME_VALUES[NBLB_SECRET_DIR]}" in /*) ;; *) fail runtime_config_secret_dir_invalid ;; esac
    validate_caps \
        "${RUNTIME_VALUES[NBLB_ADMIN_EVENT_MAX_ROWS]}" \
        "${RUNTIME_VALUES[NBLB_ADMIN_ATTEMPT_MAX_ROWS]}" \
        "${RUNTIME_VALUES[NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE]}"
    final_sha=$(file_sha256 "$RUNTIME_CONFIG") || fail runtime_config_invalid
    [ "$initial_sha" = "$final_sha" ] || fail runtime_config_changed_during_read
    RUNTIME_CONFIG_SHA256=$final_sha
}

runtime_config_unchanged() {
    [ "$(file_sha256 "$RUNTIME_CONFIG")" = "$RUNTIME_CONFIG_SHA256" ]
}

write_runtime_config() {
    [ "$#" -eq 6 ] || return 1
    runtime_config_unchanged || return 1
    local directory owner group mode
    directory=$(dirname "$RUNTIME_CONFIG")
    owner=$(stat -c '%u' "$RUNTIME_CONFIG")
    group=$(stat -c '%g' "$RUNTIME_CONFIG")
    mode=$(stat -c '%a' "$RUNTIME_CONFIG")
    RUNTIME_CONFIG_TEMPORARY=$(mktemp "$directory/.nblb-runtime.XXXXXX") || return 1
    printf '%s\n' \
        "NBLB_APP_REGISTRY_DIGEST=$1" \
        "NBLB_POSTGRES_REGISTRY_DIGEST=$2" \
        "NBLB_SECRET_DIR=$3" \
        "NBLB_ADMIN_EVENT_MAX_ROWS=$4" \
        "NBLB_ADMIN_ATTEMPT_MAX_ROWS=$5" \
        "NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=$6" \
        > "$RUNTIME_CONFIG_TEMPORARY" || return 1
    chmod "$mode" "$RUNTIME_CONFIG_TEMPORARY" \
        && chown "$owner:$group" "$RUNTIME_CONFIG_TEMPORARY" \
        && sync -f "$RUNTIME_CONFIG_TEMPORARY" \
        && runtime_config_unchanged \
        && mv -f -- "$RUNTIME_CONFIG_TEMPORARY" "$RUNTIME_CONFIG" \
        && sync -f "$directory" || return 1
    RUNTIME_CONFIG_TEMPORARY=""
    RUNTIME_VALUES[NBLB_APP_REGISTRY_DIGEST]=$1
    RUNTIME_VALUES[NBLB_POSTGRES_REGISTRY_DIGEST]=$2
    RUNTIME_VALUES[NBLB_SECRET_DIR]=$3
    RUNTIME_VALUES[NBLB_ADMIN_EVENT_MAX_ROWS]=$4
    RUNTIME_VALUES[NBLB_ADMIN_ATTEMPT_MAX_ROWS]=$5
    RUNTIME_VALUES[NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE]=$6
    RUNTIME_CONFIG_SHA256=$(file_sha256 "$RUNTIME_CONFIG") || return 1
}

update_ledger_caps() {
    [ "$#" -eq 3 ] || fail runtime_config_update_input_invalid
    validate_caps "$1" "$2" "$3"
    write_runtime_config \
        "${RUNTIME_VALUES[NBLB_APP_REGISTRY_DIGEST]}" \
        "${RUNTIME_VALUES[NBLB_POSTGRES_REGISTRY_DIGEST]}" \
        "${RUNTIME_VALUES[NBLB_SECRET_DIR]}" "$1" "$2" "$3" \
        || fail runtime_config_update_failed
    printf '%s\n' runtime_config_updated
}

update_app_digest() {
    [ "$#" -eq 1 ] && [[ "$1" =~ ^[0-9a-f]{64}$ ]] \
        || fail runtime_config_update_input_invalid
    write_runtime_config "$1" \
        "${RUNTIME_VALUES[NBLB_POSTGRES_REGISTRY_DIGEST]}" \
        "${RUNTIME_VALUES[NBLB_SECRET_DIR]}" \
        "${RUNTIME_VALUES[NBLB_ADMIN_EVENT_MAX_ROWS]}" \
        "${RUNTIME_VALUES[NBLB_ADMIN_ATTEMPT_MAX_ROWS]}" \
        "${RUNTIME_VALUES[NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE]}" \
        || fail runtime_config_update_failed
    printf '%s\n' runtime_config_updated
}

export_runtime_config() {
    export NBLB_APP_REGISTRY_DIGEST=${RUNTIME_VALUES[NBLB_APP_REGISTRY_DIGEST]}
    export NBLB_POSTGRES_REGISTRY_DIGEST=${RUNTIME_VALUES[NBLB_POSTGRES_REGISTRY_DIGEST]}
    export NBLB_ADMIN_EVENT_MAX_ROWS=${RUNTIME_VALUES[NBLB_ADMIN_EVENT_MAX_ROWS]}
    export NBLB_ADMIN_ATTEMPT_MAX_ROWS=${RUNTIME_VALUES[NBLB_ADMIN_ATTEMPT_MAX_ROWS]}
    export NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=${RUNTIME_VALUES[NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE]}
    export NBLB_SECRET_DIR=${NBLB_SECRET_DIR:-${RUNTIME_VALUES[NBLB_SECRET_DIR]}}
}

compose() {
    docker compose --project-directory "$ROOT" -f "$ROOT/compose.yml" "$@"
}

withdraw_recovery_app() {
    local reason=$1
    if ! compose stop app >/dev/null 2>&1; then
        fail "${reason}_and_app_withdraw_failed"
    fi
    RECOVERY_APP_MUST_WITHDRAW=0
    fail "$reason"
}

recovery_capacity_ready() {
    local port=${NBLB_PORT:-2456}
    [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$port" -le 65535 ] \
        || return 1
    python3 "$ROOT/scripts/operator_readiness_probe.py" \
        ledger-capacity "$port" "$NBLB_SECRET_DIR/admin_token" \
        >/dev/null 2>&1
}

wait_recovery_capacity() {
    for _ in {1..30}; do
        if recovery_capacity_ready; then
            return 0
        fi
        sleep 1
    done
    return 1
}

recover_ledger_capacity() {
    [ "$#" -eq 3 ] || fail runtime_config_update_input_invalid
    validate_caps "$1" "$2" "$3"
    for command in docker python3 sleep; do
        command -v "$command" >/dev/null 2>&1 || fail required_command_unavailable
    done
    export_runtime_config
    local app before_ref before_id expected_ref expected_id after after_ref after_id
    local running recreate_status
    app=$(compose ps -a -q app) || fail runtime_recovery_app_unavailable
    [[ "$app" =~ ^[0-9a-f]{64}$ ]] || fail runtime_recovery_app_unavailable
    before_ref=$(docker inspect --format '{{.Config.Image}}' "$app") \
        || fail runtime_recovery_app_unavailable
    before_id=$(docker inspect --format '{{.Image}}' "$app") \
        || fail runtime_recovery_app_unavailable
    running=$(docker inspect --format '{{.State.Running}}' "$app") \
        || fail runtime_recovery_app_unavailable
    expected_ref="ghcr.io/dongwonttuna-labs/nvidia-build-lb@sha256:${RUNTIME_VALUES[NBLB_APP_REGISTRY_DIGEST]}"
    expected_id=$(docker image inspect --format '{{.Id}}' "$expected_ref") \
        || fail runtime_recovery_image_unavailable
    case "$running" in true|false) ;; *) fail runtime_recovery_app_unavailable ;; esac
    [ "$before_ref" = "$expected_ref" ] && [ "$before_id" = "$expected_id" ] \
        || fail runtime_recovery_prestart_image_mismatch

    export NBLB_ADMIN_EVENT_MAX_ROWS=$1
    export NBLB_ADMIN_ATTEMPT_MAX_ROWS=$2
    export NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=$3
    compose config --quiet || fail runtime_recovery_config_invalid
    RECOVERY_APP_MUST_WITHDRAW=1
    set +e
    compose up -d --force-recreate --no-deps app
    recreate_status=$?
    set -e
    [ "$recreate_status" -eq 0 ] \
        || withdraw_recovery_app runtime_recovery_recreate_failed
    after=$(compose ps -q app) || withdraw_recovery_app runtime_recovery_app_unavailable
    [[ "$after" =~ ^[0-9a-f]{64}$ ]] \
        || withdraw_recovery_app runtime_recovery_app_unavailable
    after_ref=$(docker inspect --format '{{.Config.Image}}' "$after") \
        || withdraw_recovery_app runtime_recovery_app_unavailable
    after_id=$(docker inspect --format '{{.Image}}' "$after") \
        || withdraw_recovery_app runtime_recovery_app_unavailable
    running=$(docker inspect --format '{{.State.Running}}' "$after") \
        || withdraw_recovery_app runtime_recovery_app_unavailable
    [ "$running" = true ] && [ "$after_ref" = "$before_ref" ] \
        && [ "$after_id" = "$before_id" ] \
        || withdraw_recovery_app runtime_recovery_same_image_mismatch
    wait_recovery_capacity \
        || withdraw_recovery_app runtime_recovery_capacity_failed
    runtime_config_unchanged \
        || withdraw_recovery_app runtime_recovery_config_changed
    write_runtime_config \
        "${RUNTIME_VALUES[NBLB_APP_REGISTRY_DIGEST]}" \
        "${RUNTIME_VALUES[NBLB_POSTGRES_REGISTRY_DIGEST]}" \
        "${RUNTIME_VALUES[NBLB_SECRET_DIR]}" "$1" "$2" "$3" \
        || withdraw_recovery_app runtime_recovery_config_commit_failed
    RECOVERY_APP_MUST_WITHDRAW=0
    printf '%s\n' ledger_capacity_recreated_same_image
}

for command in flock id sha256sum stat; do
    command -v "$command" >/dev/null 2>&1 || fail required_command_unavailable
done
case "${1:-}" in
    update-ledger-caps|update-app-digest|recover-ledger-capacity) lock_kind=exclusive ;;
    *) lock_kind=shared ;;
esac
acquire_runtime_lock "$lock_kind"
load_runtime_config
if [ "${1:-}" = update-ledger-caps ]; then
    shift
    update_ledger_caps "$@"
    exit 0
fi
if [ "${1:-}" = update-app-digest ]; then
    shift
    update_app_digest "$@"
    exit 0
fi
if [ "${1:-}" = recover-ledger-capacity ]; then
    shift
    recover_ledger_capacity "$@"
    exit 0
fi

command -v docker >/dev/null 2>&1 || fail docker_unavailable
[ "$#" -gt 0 ] || fail compose_command_required

export_runtime_config

compose "$@"
