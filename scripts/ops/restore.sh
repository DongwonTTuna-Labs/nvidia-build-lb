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
RESTORE_STAGE_DIR=""
HOST_UID=""
HOST_GID=""
INSTALL_MARKER_ID=""
KEY_INSTALL_ATTEMPTED=0
RESTORE_RECEIPT=""

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    set +e
    cleanup_error=0
    if [ "$status" -ne 0 ] && [ "$KEY_INSTALL_ATTEMPTED" -eq 1 ] \
        && [ -n "$TARGET_SECRET_DIR" ] && [ -d "$TARGET_SECRET_DIR" ]; then
        docker run --rm --network none --read-only --cap-drop ALL --cap-add DAC_OVERRIDE \
            --security-opt no-new-privileges:true --entrypoint /bin/sh \
            --mount "type=bind,source=$TARGET_SECRET_DIR,target=/target" \
            "$HELPER_IMAGE" -c 'set -eu; marker=/target/.nblb-restore-install; if [ -f "$marker" ] && [ ! -L "$marker" ] && [ "$(cat "$marker")" = "$1" ]; then rm -f /target/.vault_master_key.tmp /target/vault_master_key "$marker"; sync -f /target; fi' \
            helper "$INSTALL_MARKER_ID" \
            >/dev/null 2>&1 || cleanup_error=1
    fi
    if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
        docker run --rm --network none --read-only --cap-drop ALL --cap-add DAC_OVERRIDE \
            --security-opt no-new-privileges:true --entrypoint /bin/sh \
            --mount "type=bind,source=$STATE_DIR,target=/state" \
            "$HELPER_IMAGE" -c 'rm -f /state/database-state.json' >/dev/null 2>&1 \
            || cleanup_error=1
        rmdir "$STATE_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi
    if [ -n "$RESTORE_STAGE_DIR" ] && [ -d "$RESTORE_STAGE_DIR" ]; then
        docker run --rm --network none --read-only --cap-drop ALL \
            --cap-add DAC_OVERRIDE --cap-add CHOWN \
            --security-opt no-new-privileges:true --entrypoint /bin/sh \
            --mount "type=bind,source=$RESTORE_STAGE_DIR,target=/stage" \
            "$HELPER_IMAGE" -c 'rm -f /stage/database.dump /stage/vault_master_key /stage/manifest.json /stage/.*.tmp; chown "$1:$2" /stage; chmod 0700 /stage; sync -f /stage' \
            helper "$HOST_UID" "$HOST_GID" >/dev/null 2>&1 || cleanup_error=1
        rmdir "$RESTORE_STAGE_DIR" >/dev/null 2>&1 || cleanup_error=1
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

for command in docker flock id mktemp realpath rmdir sha256sum; do
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
component=$(docker inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.component"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail restore_target_not_isolated
compose_service=$(docker inspect \
    --format '{{index .Config.Labels "com.docker.compose.service"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail restore_target_not_isolated
compose_project=$(docker inspect \
    --format '{{index .Config.Labels "com.docker.compose.project"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail restore_target_not_isolated
[ "$component" = database ] && [ "$compose_service" = db ] \
    && [ -n "$compose_project" ] && [ "$compose_project" != '<no value>' ] \
    || fail restore_target_not_isolated

lock_id=$(printf '%s' "$MANIFEST_FILE" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail restore_lock_failed
LOCK_FILE=/tmp/nblb-restore-$lock_id.lock
exec 9>"$LOCK_FILE"
flock -n 9 || fail restore_lock_busy

HOST_UID=$(id -u)
HOST_GID=$(id -g)
RESTORE_STAGE_DIR=$(mktemp -d /tmp/nblb-restore-stage.XXXXXX) \
    || fail restore_snapshot_failed
INSTALL_MARKER_ID=$(printf '%s' "$RESTORE_STAGE_DIR" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') \
    || fail restore_marker_failed
docker run --rm --network none --read-only --cap-drop ALL \
    --cap-add DAC_OVERRIDE --cap-add CHOWN \
    --security-opt no-new-privileges:true --entrypoint /bin/sh \
    --mount "type=bind,source=$DATABASE_ROOT,target=/database-root,readonly" \
    --mount "type=bind,source=$KEY_ROOT,target=/key-root,readonly" \
    --mount "type=bind,source=$MANIFEST_ROOT,target=/manifest-root,readonly" \
    --mount "type=bind,source=$RESTORE_STAGE_DIR,target=/stage" \
    "$HELPER_IMAGE" -c 'set -eu; umask 077; database="/database-root/$1/database.dump"; key="/key-root/$1/vault_master_key"; manifest="/manifest-root/$1/manifest.json"; for source in "$database" "$key" "$manifest"; do test -f "$source"; test ! -L "$source"; test "$(stat -c "%u:%g:%a:%h" "$source")" = 0:0:600:1; done; test "$(wc -c < "$key")" -eq 32; test "$(stat -c "%u:%g:%a" /stage)" = "$2:$3:700"; chown 0:0 /stage; cp "$database" /stage/.database.dump.tmp; cp "$key" /stage/.vault_master_key.tmp; cp "$manifest" /stage/.manifest.json.tmp; chmod 0600 /stage/.database.dump.tmp /stage/.vault_master_key.tmp /stage/.manifest.json.tmp; chown 0:0 /stage/.database.dump.tmp /stage/.vault_master_key.tmp /stage/.manifest.json.tmp; mv /stage/.database.dump.tmp /stage/database.dump; mv /stage/.vault_master_key.tmp /stage/vault_master_key; mv /stage/.manifest.json.tmp /stage/manifest.json; sync -f /stage/database.dump; sync -f /stage/vault_master_key; sync -f /stage/manifest.json; sync -f /stage' \
    helper "$DATABASE_BACKUP_ID" "$HOST_UID" "$HOST_GID" \
    || fail restore_snapshot_failed

docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint /app/.venv/bin/python \
    --mount "type=bind,source=$RESTORE_STAGE_DIR,target=/stage,readonly" \
    "$HELPER_IMAGE" -m nvidia_build_lb.backup_contract verify \
    --database-dump /stage/database.dump \
    --vault-key /stage/vault_master_key \
    --manifest /stage/manifest.json >/dev/null \
    || fail backup_pair_invalid

user_object_count=$(docker exec --user 70 "$DB_CONTAINER" \
    psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --username nvidia_build_lb --dbname nvidia_build_lb \
    --command "WITH user_schemas AS (SELECT oid,nspname FROM pg_catalog.pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'), public_objects AS (SELECT oid FROM pg_catalog.pg_class WHERE relnamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_proc WHERE pronamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_type WHERE typnamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_collation WHERE collnamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_conversion WHERE connamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_operator WHERE oprnamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_opclass WHERE opcnamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_opfamily WHERE opfnamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_ts_config WHERE cfgnamespace = 'public'::regnamespace UNION SELECT oid FROM pg_catalog.pg_ts_dict WHERE dictnamespace = 'public'::regnamespace) SELECT (SELECT count(*) FROM user_schemas WHERE nspname <> 'public') + (SELECT count(*) FROM public_objects);") \
    || fail restore_database_unavailable
[[ "$user_object_count" =~ ^[0-9]+$ ]] || fail restore_database_unavailable
[ "$user_object_count" = 0 ] || fail target_database_not_empty

KEY_INSTALL_ATTEMPTED=1
docker run --rm --network none --read-only --cap-drop ALL \
    --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true --entrypoint /bin/sh \
    --mount "type=bind,source=$RESTORE_STAGE_DIR,target=/stage,readonly" \
    --mount "type=bind,source=$TARGET_SECRET_DIR,target=/target" \
    "$HELPER_IMAGE" -c 'set -eu; umask 077; marker=/target/.nblb-restore-install; test "$(stat -c "%u:%g:%a" /target)" = 0:0:700; test ! -e /target/vault_master_key; test ! -e /target/.vault_master_key.tmp; test ! -e "$marker"; (set -C; printf "%s\n" "$1" > "$marker"); chmod 0600 "$marker"; chown 0:0 "$marker"; test "$(cat "$marker")" = "$1"; sync -f "$marker"; sync -f /target; cp /stage/vault_master_key /target/.vault_master_key.tmp; chmod 0600 /target/.vault_master_key.tmp; chown 0:0 /target/.vault_master_key.tmp; mv /target/.vault_master_key.tmp /target/vault_master_key; sync -f /target/vault_master_key; sync -f /target' \
    helper "$INSTALL_MARKER_ID" \
    || fail restore_key_install_failed
docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint /app/.venv/bin/python \
    --mount "type=bind,source=$RESTORE_STAGE_DIR,target=/stage,readonly" \
    --mount "type=bind,source=$TARGET_SECRET_DIR,target=/target,readonly" \
    "$HELPER_IMAGE" -m nvidia_build_lb.backup_contract verify \
    --database-dump /stage/database.dump \
    --vault-key /target/vault_master_key \
    --manifest /stage/manifest.json >/dev/null \
    || fail installed_key_artifact_mismatch

docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --entrypoint /bin/cat \
    --mount "type=bind,source=$RESTORE_STAGE_DIR,target=/stage,readonly" \
    "$HELPER_IMAGE" /stage/database.dump \
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
    --mount "type=bind,source=$TARGET_SECRET_DIR,target=/target,readonly" \
    --mount "type=bind,source=$STATE_DIR/database-state.json,target=/state/database-state.json,readonly" \
    "$HELPER_IMAGE" -m nvidia_build_lb.backup_contract verify-vault-key \
    --vault-key /target/vault_master_key \
    --state /state/database-state.json >/dev/null \
    || fail installed_key_database_mismatch

RESTORE_RECEIPT=$(docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint /app/.venv/bin/python \
    --mount "type=bind,source=$STATE_DIR/database-state.json,target=/state/database-state.json,readonly" \
    --mount "type=bind,source=$RESTORE_STAGE_DIR,target=/stage,readonly" \
    "$HELPER_IMAGE" -m nvidia_build_lb.backup_contract compare-state \
    --state /state/database-state.json \
    --manifest /stage/manifest.json) \
    || fail restored_state_mismatch

docker run --rm --network none --read-only --cap-drop ALL \
    --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true --entrypoint /bin/sh \
    --mount "type=bind,source=$TARGET_SECRET_DIR,target=/target" \
    "$HELPER_IMAGE" -c 'set -eu; marker=/target/.nblb-restore-install; test -f "$marker"; test ! -L "$marker"; test "$(cat "$marker")" = "$1"; sync -f /target/vault_master_key; rm -f "$marker"; sync -f /target' \
    helper "$INSTALL_MARKER_ID" \
    || fail restore_key_commit_failed

printf '%s\n' "$RESTORE_RECEIPT"
