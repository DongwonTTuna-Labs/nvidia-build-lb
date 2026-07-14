#!/bin/sh
set -eu
umask 077

fail() {
    printf '%s\n' 'prestart_failed' >&2
    exit 70
}

canonical=/run/qa-canonical
runtime=/run/nvidia-build-lb/qa-tls

[ "$(id -u)" -eq 0 ] || fail
mkdir -p "$runtime" 2>/dev/null || fail
chmod 0700 "$runtime" 2>/dev/null || fail
for name in server_cert server_key; do
    source_file=$canonical/$name
    destination=$runtime/$name
    [ -f "$source_file" ] && [ ! -L "$source_file" ] || fail
    metadata=$(stat -c '%u:%g:%a' "$source_file" 2>/dev/null) || fail
    case "$metadata" in
        0:0:400|0:0:444|0:0:600|0:0:644) ;;
        *) fail ;;
    esac
    cp "$source_file" "$destination" 2>/dev/null || fail
    chmod 0400 "$destination" 2>/dev/null || fail
    chown 65532:65532 "$destination" 2>/dev/null || fail
done
chown 65532:65532 "$runtime" 2>/dev/null || fail

exec setpriv \
    --reuid=65532 \
    --regid=65532 \
    --clear-groups \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    /app/.venv/bin/python /qa/fake_nvidia_tls.py
