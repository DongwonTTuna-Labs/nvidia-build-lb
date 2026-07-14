#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() {
    printf '%s\n' "${1:-backup_failed}" >&2
    exit 1
}

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DB_CONTAINER=""
APP_CONTAINER=""
HELPER_IMAGE=""
VAULT_KEY_FILE=""
DATABASE_ROOT=""
KEY_ROOT=""
MANIFEST_ROOT=""
BACKUP_ID=""
LOCK_FILE=""

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    set +e
    if [ -n "$LOCK_FILE" ]; then
        rm -f -- "$LOCK_FILE" >/dev/null 2>&1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --db-container) DB_CONTAINER=${2:-}; shift 2 ;;
        --app-container) APP_CONTAINER=${2:-}; shift 2 ;;
        --helper-image) HELPER_IMAGE=${2:-}; shift 2 ;;
        --vault-key-file) VAULT_KEY_FILE=${2:-}; shift 2 ;;
        --database-root) DATABASE_ROOT=${2:-}; shift 2 ;;
        --key-root) KEY_ROOT=${2:-}; shift 2 ;;
        --manifest-root) MANIFEST_ROOT=${2:-}; shift 2 ;;
        --backup-id) BACKUP_ID=${2:-}; shift 2 ;;
        *) fail backup_input_invalid ;;
    esac
done

for command in date docker flock realpath sha256sum; do
    command -v "$command" >/dev/null 2>&1 || fail required_command_unavailable
done
[[ "$DB_CONTAINER" =~ ^[0-9a-f]{64}$ ]] || fail backup_input_invalid
[[ "$APP_CONTAINER" =~ ^[0-9a-f]{64}$ ]] || fail backup_input_invalid
[[ "$HELPER_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] || fail backup_input_invalid
[[ "$BACKUP_ID" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || fail backup_input_invalid
[ -f "$VAULT_KEY_FILE" ] && [ ! -L "$VAULT_KEY_FILE" ] || fail vault_key_unavailable

for root in "$DATABASE_ROOT" "$KEY_ROOT" "$MANIFEST_ROOT"; do
    [ -d "$root" ] && [ ! -L "$root" ] || fail backup_root_invalid
done
DATABASE_ROOT=$(realpath -e "$DATABASE_ROOT")
KEY_ROOT=$(realpath -e "$KEY_ROOT")
MANIFEST_ROOT=$(realpath -e "$MANIFEST_ROOT")
for pair in \
    "$DATABASE_ROOT:$KEY_ROOT" \
    "$DATABASE_ROOT:$MANIFEST_ROOT" \
    "$KEY_ROOT:$MANIFEST_ROOT"; do
    first=${pair%%:*}
    second=${pair#*:}
    [ "$first" != "$second" ] || fail backup_roots_not_separate
    case "$first/" in "$second/"*) fail backup_roots_not_separate ;; esac
    case "$second/" in "$first/"*) fail backup_roots_not_separate ;; esac
done

db_running=$(docker inspect --format '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null) \
    || fail source_database_unavailable
[ "$db_running" = true ] || fail source_database_unavailable
backup_source=$(docker inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.backup-source"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail source_database_unavailable
[ "$backup_source" = true ] || fail source_database_not_authorized
app_running=$(docker inspect --format '{{.State.Running}}' "$APP_CONTAINER" 2>/dev/null) \
    || fail source_app_unavailable
[ "$app_running" = false ] || fail source_app_must_be_stopped

lock_id=$(printf '%s' "$MANIFEST_ROOT" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail backup_lock_failed
LOCK_FILE=/tmp/nblb-backup-$lock_id.lock
exec 9>"$LOCK_FILE"
flock -n 9 || fail backup_lock_busy

root_helper() {
    docker run --rm --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges:true \
        --entrypoint /bin/sh "$@"
}

for mapping in \
    "$DATABASE_ROOT:/output" \
    "$KEY_ROOT:/output" \
    "$MANIFEST_ROOT:/output"; do
    host=${mapping%%:*}
    root_helper --mount "type=bind,source=$host,target=/output" \
        "$HELPER_IMAGE" \
        -c 'umask 077; test "$(stat -c "%u:%g:%a" /output)" = 0:0:700; test ! -e "/output/$1"; mkdir "/output/$1"; chmod 0700 "/output/$1"; chown 0:0 "/output/$1"; sync -f /output' \
        helper "$BACKUP_ID" || fail backup_directory_create_failed
done

database_directory=$DATABASE_ROOT/$BACKUP_ID
key_directory=$KEY_ROOT/$BACKUP_ID
manifest_directory=$MANIFEST_ROOT/$BACKUP_ID
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

docker exec --user 70 "$DB_CONTAINER" \
    pg_dump --username nvidia_build_lb --dbname nvidia_build_lb \
    --format=custom --compress=9 --no-owner --no-privileges \
    | root_helper -i --mount "type=bind,source=$database_directory,target=/output" \
        "$HELPER_IMAGE" \
        -c 'umask 077; test ! -e /output/database.dump; cat > /output/.database.dump.tmp; test -s /output/.database.dump.tmp; chmod 0600 /output/.database.dump.tmp; chown 0:0 /output/.database.dump.tmp; mv /output/.database.dump.tmp /output/database.dump; sync -f /output/database.dump; sync -f /output' \
    || fail database_dump_failed

root_helper \
    --mount "type=bind,source=$VAULT_KEY_FILE,target=/source/vault_master_key,readonly" \
    --mount "type=bind,source=$key_directory,target=/output" \
    "$HELPER_IMAGE" \
    -c 'umask 077; test -f /source/vault_master_key; test ! -L /source/vault_master_key; test "$(wc -c < /source/vault_master_key)" -eq 32; test ! -e /output/vault_master_key; cp /source/vault_master_key /output/.vault_master_key.tmp; chmod 0600 /output/.vault_master_key.tmp; chown 0:0 /output/.vault_master_key.tmp; mv /output/.vault_master_key.tmp /output/vault_master_key; sync -f /output/vault_master_key; sync -f /output' \
    || fail vault_key_backup_failed

"$ROOT/scripts/ops/database-state.sh" "$DB_CONTAINER" \
    | root_helper -i --mount "type=bind,source=$manifest_directory,target=/output" \
        "$HELPER_IMAGE" \
        -c 'umask 077; cat > /output/database-state.json; test -s /output/database-state.json; chmod 0600 /output/database-state.json; chown 0:0 /output/database-state.json; sync -f /output/database-state.json; sync -f /output' \
    || fail database_state_capture_failed

docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint /app/.venv/bin/python \
    --mount "type=bind,source=$database_directory,target=/database,readonly" \
    --mount "type=bind,source=$key_directory,target=/key,readonly" \
    --mount "type=bind,source=$manifest_directory,target=/manifest" \
    "$HELPER_IMAGE" -m nvidia_build_lb.backup_contract create \
    --backup-id "$BACKUP_ID" --created-at "$created_at" \
    --database-dump /database/database.dump \
    --vault-key /key/vault_master_key \
    --state /manifest/database-state.json \
    --manifest /manifest/manifest.json \
    || fail backup_manifest_failed

root_helper --mount "type=bind,source=$manifest_directory,target=/output" "$HELPER_IMAGE" \
    -c 'rm -f /output/database-state.json; sync -f /output' \
    || fail backup_state_cleanup_failed
