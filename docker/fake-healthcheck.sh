#!/bin/sh
set -eu

# The fixture terminates TLS on the NVIDIA-compatible HTTPS port. Keep this
# probe separate from the gateway/loopback HTTP healthcheck.
curl --silent --show-error --insecure --fail --max-time 1 \
  https://127.0.0.1:443/health \
  | grep -F '"status":"ok"' >/dev/null
