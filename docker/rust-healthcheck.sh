#!/bin/sh
set -eu

bind_port=${NVIDIA_BUILD_LB_BIND_PORT:-2456}
host_port=${NVIDIA_BUILD_LB_PUBLIC_PORT:-2456}
case "$bind_port" in ''|*[!0-9]*) exit 1 ;; esac
case "$host_port" in ''|*[!0-9]*) exit 1 ;; esac
test "$(awk '/^Uid:/{print $2; exit}' /proc/1/status 2>/dev/null)" = 65532
body=$(mktemp)
trap 'rm -f "$body"' EXIT
status=$(curl --silent --show-error --max-time 1 -o "$body" -w '%{http_code}' \
  -H "Host: 127.0.0.1:$host_port" "http://127.0.0.1:$bind_port/health")
test "$status" = 200
grep -Eq '"ready":true.*"traffic_ready":true' "$body"
