#!/bin/sh
set -eu
umask 077

fail() {
    printf '%s\n' 'prestart_failed' >&2
    exit 70
}

canonical=/run/canonical-secrets
runtime=/run/nvidia-build-lb/secrets
mode=${NVIDIA_BUILD_LB_MODE:-app}

[ "$(id -u)" -eq 0 ] || fail
case "$mode" in
    app|migrate) ;;
    *) fail ;;
esac

check_source() {
    source_file=$1
    [ -f "$source_file" ] && [ ! -L "$source_file" ] || fail
    metadata=$(stat -c '%u:%g:%a' "$source_file" 2>/dev/null) || fail
    case "$metadata" in
        0:0:400|0:0:444|0:0:600) ;;
        *) fail ;;
    esac
}

copy_secret() {
    name=$1
    source_file=$canonical/$name
    destination=$runtime/$name
    temporary=$runtime/.copy-$name
    check_source "$source_file"
    rm -f "$temporary" "$destination"
    cp "$source_file" "$temporary" 2>/dev/null || fail
    chmod 0400 "$temporary" 2>/dev/null || fail
    chown 65532:65532 "$temporary" 2>/dev/null || fail
    mv "$temporary" "$destination" 2>/dev/null || fail
}

mkdir -p "$runtime" 2>/dev/null || fail
chmod 0700 "$runtime" 2>/dev/null || fail
check_source "$canonical/db_password"
if [ "$mode" = app ]; then
    check_source "$canonical/admin_token"
    check_source "$canonical/vault_master_key"
fi
/app/.venv/bin/python -m nvidia_build_lb.prestart_validate "$mode" >/dev/null 2>&1 || fail

copy_secret db_password
if [ "$mode" = app ]; then
    copy_secret admin_token
    copy_secret vault_master_key
fi
chown 65532:65532 "$runtime" 2>/dev/null || fail

unset NVIDIA_BUILD_LB_MODE
export HOME=/
if [ "$mode" = migrate ]; then
    command=/app/.venv/bin/python
    set -- -m nvidia_build_lb.migrate
else
    command=/app/.venv/bin/python
    set -- -m nvidia_build_lb.production
fi
exec setpriv \
    --reuid=65532 \
    --regid=65532 \
    --clear-groups \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    "$command" "$@"
