#!/bin/sh
set -eu

[ "$#" -eq 3 ] || exit 64
expected_uid=$1
expected_gid=$2
probe=$3

# The Docker engine starts the configured health command with the container's
# default root user. Every healthcheck therefore enters through setpriv and
# this script refuses to probe unless the credential/capability drop actually
# happened in this process.
awk -v expected_uid="$expected_uid" -v expected_gid="$expected_gid" '
    $1 == "Uid:" {
        seen_uid = 1
        valid = valid && $2 == expected_uid && $3 == expected_uid \
            && $4 == expected_uid && $5 == expected_uid
    }
    $1 == "Gid:" {
        seen_gid = 1
        valid = valid && $2 == expected_gid && $3 == expected_gid \
            && $4 == expected_gid && $5 == expected_gid
    }
    $1 == "Groups:" {
        seen_groups = 1
        valid = valid && NF == 1
    }
    $1 == "CapEff:" {
        seen_effective = 1
        valid = valid && $2 ~ /^0+$/
    }
    $1 == "CapBnd:" {
        seen_bounding = 1
        valid = valid && $2 ~ /^0+$/
    }
    $1 == "NoNewPrivs:" {
        seen_no_new_privs = 1
        valid = valid && $2 == 1
    }
    BEGIN { valid = 1 }
    END {
        exit !(valid && seen_uid && seen_gid && seen_groups \
            && seen_effective && seen_bounding && seen_no_new_privs)
    }
' /proc/self/status

case "$probe" in
    app)
        public_port=${NVIDIA_BUILD_LB_PUBLIC_PORT:-2456}
        exec /app/.venv/bin/python -c \
            "
import json
import sys
import urllib.request

port = sys.argv[1]
canonical = (
    port.isascii()
    and port.isdecimal()
    and not port.startswith('0')
    and 1 <= int(port) <= 65535
)
if not canonical:
    raise SystemExit(1)
request = urllib.request.Request(
    'http://127.0.0.1:2456/health',
    headers={'Host': f'127.0.0.1:{port}'},
)
with urllib.request.urlopen(request, timeout=1) as response:
    healthy = response.status == 200 and json.load(response) == {
        'status': 'ok',
        'ready': True,
    }
raise SystemExit(0 if healthy else 1)
" \
            "$public_port"
        ;;
    postgres)
        exec pg_isready -q -U nvidia_build_lb -d nvidia_build_lb
        ;;
    fake)
        exec /app/.venv/bin/python -c \
            "import socket; s=socket.create_connection(('127.0.0.1',443),1); s.close()"
        ;;
    loopback)
        exec /app/.venv/bin/python -c \
            "import socket; s=socket.create_connection(('127.0.0.1',2456),1); s.close()"
        ;;
    *)
        exit 64
        ;;
esac
