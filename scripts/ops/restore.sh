#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() {
    printf '%s\n' "${1:-restore_failed}" >&2
    exit 1
}

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DB_CONTAINER=""
HELPER_IMAGE=""
DATABASE_DIRECTORY=""
KEY_DIRECTORY=""
MANIFEST_FILE=""
TARGET_SECRET_DIR=""
STATE_DIR=""
LOCK_FILE=""

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    set +e
    cleanup_error=0
    if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
        docker run --rm --network none --read-only --cap-drop ALL --cap-add DAC_OVERRIDE \
            --security-opt no-new-privileges:true --entrypoint /bin/sh \
            --mount "type=bind,source=$STATE_DIR,target=/state" \
            "$HELPER_IMAGE" -c 'rm -f /state/database-state.json' >/dev/null 2>&1 \
            || cleanup_error=1
        rmdir "$STATE_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi
    if [ -n "$LOCK_FILE" ]; then
        rm -f -- "$LOCK_FILE" >/dev/null 2>&1 || cleanup_error=1
    fi
    [ "$cleanup_error" -eq 0 ] || status=1
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --db-container) DB_CONTAINER=${2:-}; shift 2 ;;
        --helper-image) HELPER_IMAGE=${2:-}; shift 2 ;;
        --database-directory) DATABASE_DIRECTORY=${2:-}; shift 2 ;;
        --key-directory) KEY_DIRECTORY=${2:-}; shift 2 ;;
        --manifest) MANIFEST_FILE=${2:-}; shift 2 ;;
        --target-secret-dir) TARGET_SECRET_DIR=${2:-}; shift 2 ;;
        *) fail restore_input_invalid ;;
    esac
done

for command in docker flock mktemp realpath sha256sum; do
    command -v "$command" >/dev/null 2>&1 || fail required_command_unavailable
done
[[ "$DB_CONTAINER" =~ ^[0-9a-f]{64}$ ]] || fail restore_input_invalid
[[ "$HELPER_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] || fail restore_input_invalid
DATABASE_ROOT=$(dirname "$DATABASE_DIRECTORY")
KEY_ROOT=$(dirname "$KEY_DIRECTORY")
MANIFEST_DIRECTORY=$(dirname "$MANIFEST_FILE")
MANIFEST_ROOT=$(dirname "$MANIFEST_DIRECTORY")
DATABASE_BACKUP_ID=$(basename "$DATABASE_DIRECTORY")
KEY_BACKUP_ID=$(basename "$KEY_DIRECTORY")
MANIFEST_BACKUP_ID=$(basename "$MANIFEST_DIRECTORY")
[[ "$DATABASE_BACKUP_ID" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] \
    || fail restore_input_invalid
[ "$DATABASE_BACKUP_ID" = "$KEY_BACKUP_ID" ] \
    && [ "$DATABASE_BACKUP_ID" = "$MANIFEST_BACKUP_ID" ] \
    || fail restore_pair_path_mismatch
for directory in "$DATABASE_ROOT" "$KEY_ROOT" "$MANIFEST_ROOT" "$TARGET_SECRET_DIR"; do
    [ -d "$directory" ] && [ ! -L "$directory" ] || fail restore_input_invalid
done
DATABASE_ROOT=$(realpath -m "$DATABASE_ROOT")
KEY_ROOT=$(realpath -m "$KEY_ROOT")
MANIFEST_ROOT=$(realpath -m "$MANIFEST_ROOT")
TARGET_SECRET_DIR=$(realpath -m "$TARGET_SECRET_DIR")
[ "$(basename "$MANIFEST_FILE")" = manifest.json ] || fail restore_input_invalid
DATABASE_DIRECTORY=$DATABASE_ROOT/$DATABASE_BACKUP_ID
KEY_DIRECTORY=$KEY_ROOT/$DATABASE_BACKUP_ID
MANIFEST_DIRECTORY=$MANIFEST_ROOT/$DATABASE_BACKUP_ID
MANIFEST_FILE=$MANIFEST_DIRECTORY/manifest.json

running=$(docker inspect --format '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null) \
    || fail restore_database_unavailable
[ "$running" = true ] || fail restore_database_unavailable
isolated=$(docker inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.restore-isolated"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail restore_target_not_isolated
[ "$isolated" = true ] || fail restore_target_not_isolated

lock_id=$(printf '%s' "$MANIFEST_FILE" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail restore_lock_failed
LOCK_FILE=/tmp/nblb-restore-$lock_id.lock
exec 9>"$LOCK_FILE"
flock -n 9 || fail restore_lock_busy

docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint /app/.venv/bin/python \
    --mount "type=bind,source=$DATABASE_ROOT,target=/database-root,readonly" \
    --mount "type=bind,source=$KEY_ROOT,target=/key-root,readonly" \
    --mount "type=bind,source=$MANIFEST_ROOT,target=/manifest-root,readonly" \
    "$HELPER_IMAGE" -m nvidia_build_lb.backup_contract verify \
    --database-dump "/database-root/$DATABASE_BACKUP_ID/database.dump" \
    --vault-key "/key-root/$DATABASE_BACKUP_ID/vault_master_key" \
    --manifest "/manifest-root/$DATABASE_BACKUP_ID/manifest.json" >/dev/null \
    || fail backup_pair_invalid

table_count=$(docker exec --user 70 "$DB_CONTAINER" \
    psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --username nvidia_build_lb --dbname nvidia_build_lb \
    --command "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public';") \
    || fail restore_database_unavailable
[ "$table_count" = 0 ] || fail target_database_not_empty

docker run --rm --network none --read-only --cap-drop ALL \
    --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true --entrypoint /bin/sh \
    --mount "type=bind,source=$KEY_ROOT,target=/key-root,readonly" \
    --mount "type=bind,source=$TARGET_SECRET_DIR,target=/target" \
    "$HELPER_IMAGE" -c 'umask 077; test ! -e /target/vault_master_key; cp "/key-root/$1/vault_master_key" /target/.vault_master_key.tmp; chmod 0600 /target/.vault_master_key.tmp; chown 0:0 /target/.vault_master_key.tmp; mv /target/.vault_master_key.tmp /target/vault_master_key; sync -f /target/vault_master_key; sync -f /target' \
    helper "$DATABASE_BACKUP_ID" \
    || fail restore_key_install_failed

docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --entrypoint /bin/cat \
    --mount "type=bind,source=$DATABASE_ROOT,target=/database-root,readonly" \
    "$HELPER_IMAGE" "/database-root/$DATABASE_BACKUP_ID/database.dump" \
    | docker exec --interactive --user 70 "$DB_CONTAINER" \
        pg_restore --username nvidia_build_lb --dbname nvidia_build_lb \
        --exit-on-error --no-owner --no-privileges \
    || fail database_restore_failed

STATE_DIR=$(mktemp -d /tmp/nblb-restore-state.XXXXXX)
"$ROOT/scripts/ops/database-state.sh" "$DB_CONTAINER" \
    | docker run --rm --interactive --network none --read-only --cap-drop ALL \
        --cap-add DAC_OVERRIDE \
        --security-opt no-new-privileges:true --entrypoint /bin/sh \
        --mount "type=bind,source=$STATE_DIR,target=/state" \
        "$HELPER_IMAGE" -c 'umask 077; cat > /state/database-state.json; chmod 0600 /state/database-state.json; chown 0:0 /state/database-state.json; sync -f /state/database-state.json; sync -f /state' \
    || fail restored_state_capture_failed

docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint /app/.venv/bin/python \
    --mount "type=bind,source=$STATE_DIR/database-state.json,target=/state/database-state.json,readonly" \
    --mount "type=bind,source=$MANIFEST_ROOT,target=/manifest-root,readonly" \
    "$HELPER_IMAGE" -m nvidia_build_lb.backup_contract compare-state \
    --state /state/database-state.json \
    --manifest "/manifest-root/$DATABASE_BACKUP_ID/manifest.json" \
    || fail restored_state_mismatch
