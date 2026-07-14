#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || exit 70
export HOME=/
exec setpriv \
    --reuid=65532 \
    --regid=65532 \
    --clear-groups \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    /app/.venv/bin/python /qa/loopback_proxy.py
