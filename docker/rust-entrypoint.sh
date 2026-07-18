#!/bin/sh
set -eu

runtime=/run/nvidia-build-lb/secrets
mkdir -p "$runtime"
for name in admin_token vault_master_key db_password; do
  source="/run/canonical-secrets/$name"
  test -r "$source"
  install -o 65532 -g 65532 -m 0400 "$source" "$runtime/$name"
done

if [ "$#" -gt 0 ]; then
  exec setpriv --reuid=65532 --regid=65532 --clear-groups --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs "$@"
fi
exec setpriv --reuid=65532 --regid=65532 --clear-groups --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs /usr/local/bin/nvidia-build-lb-gateway
