#!/bin/sh
set -eu

port=${NVIDIA_BUILD_LB_PUBLIC_PORT:-2456}
case "$port" in ''|*[!0-9]*) exit 1 ;; esac
test "$(awk '/^Uid:/{print $2; exit}' /proc/1/status 2>/dev/null)" = 65532
curl --fail --silent --show-error --max-time 1 \
  -H "Host: 127.0.0.1:$port" "http://127.0.0.1:$port/health" \
  | grep -Eq '"ready":true.*"traffic_ready":true'
