#!/bin/sh
set -eu
umask 077

fail() {
    printf 'prestart_failed:%s\n' "$1" >&2
    exit 70
}

source_file=/run/canonical-secrets/db_password
runtime=/run/nvidia-build-lb/secrets
destination=$runtime/db_password
temporary=$runtime/.copy-db_password
pgdata=${PGDATA:-/var/lib/postgresql/data/pgdata}

[ "$(id -u)" -eq 0 ] || fail not_root
[ -f "$source_file" ] || fail source_missing
[ ! -L "$source_file" ] || fail source_symlink
metadata=$(stat -c '%u:%g:%a' "$source_file" 2>/dev/null) || fail source_stat
case "$metadata" in
    0:0:400|0:0:444|0:0:600) ;;
    *:444)
        [ "${NBLB_QA_ALLOW_HOST_SECRET_OWNER:-0}" = 1 ] || fail source_mode
        ;;
    *) fail source_mode ;;
esac
size=$(wc -c < "$source_file") || fail source_size_read
[ "$size" -ge 1 ] && [ "$size" -le 1024 ] || fail source_size
if LC_ALL=C grep -q '[[:cntrl:]]' "$source_file" 2>/dev/null; then
    fail control_byte
fi

mkdir -p "$runtime" 2>/dev/null || fail runtime_mkdir
chmod 0700 "$runtime" 2>/dev/null || fail runtime_mode
rm -f "$temporary" "$destination" 2>/dev/null || fail destination_cleanup
cp "$source_file" "$temporary" 2>/dev/null || fail copy
chmod 0400 "$temporary" 2>/dev/null || fail chmod
chown 70:70 "$temporary" 2>/dev/null || fail chown
mv "$temporary" "$destination" 2>/dev/null || fail rename
chown 70:70 "$runtime" 2>/dev/null || fail runtime_owner

[ "$pgdata" = /var/lib/postgresql/data/pgdata ] || fail pgdata_path
if [ -e "$pgdata" ]; then
    [ -d "$pgdata" ] && [ ! -L "$pgdata" ] || fail pgdata_type
    # db-init may have already handed the directory to PostgreSQL. The
    # hardened runtime intentionally has no CAP_FOWNER, so do not attempt a
    # redundant chmod/chown on an already-correct 70:70/0700 directory.
    pgdata_metadata=$(stat -c '%u:%g:%a' "$pgdata" 2>/dev/null) || fail pgdata_stat
    if [ "$pgdata_metadata" != 70:70:700 ]; then
        chmod 0700 "$pgdata" 2>/dev/null || fail pgdata_mode
        chown 70:70 "$pgdata" 2>/dev/null || fail pgdata_owner
    fi
else
    mkdir -p "$pgdata" 2>/dev/null || fail pgdata_mkdir
    chmod 0700 "$pgdata" 2>/dev/null || fail pgdata_mode
    chown 70:70 "$pgdata" 2>/dev/null || fail pgdata_owner
fi

export HOME=/var/lib/postgresql
export POSTGRES_PASSWORD_FILE=$destination
exec /bin/setpriv \
    --reuid=70 \
    --regid=70 \
    --clear-groups \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    /usr/local/bin/docker-entrypoint.sh postgres
