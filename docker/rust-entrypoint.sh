#!/bin/sh
set -eu

runtime=/run/nvidia-build-lb/secrets
mkdir -p "$runtime"
chmod 0700 "$runtime"
case "${NVIDIA_BUILD_LB_MODE:-app}" in
  migrate|db-init) required_secrets="db_password" ;;
  *) required_secrets="admin_token vault_master_key db_password" ;;
esac
for name in $required_secrets; do
  source="/run/canonical-secrets/$name"
  test -r "$source"
  # Apply mode while root owns the new file, then transfer ownership. This
  # avoids requiring CAP_FOWNER under the hardened no-new-privileges profile.
  cp "$source" "$runtime/$name"
  chmod 0400 "$runtime/$name"
  chown 65532:65532 "$runtime/$name"
done
chown 65532:65532 "$runtime"

if [ "$#" -gt 0 ]; then
  if [ "${NVIDIA_BUILD_LB_MODE:-app}" = db-init ]; then
    exec "$@"
  fi
  exec setpriv --reuid=65532 --regid=65532 --clear-groups --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs "$@"
fi
exec setpriv --reuid=65532 --regid=65532 --clear-groups --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs /usr/local/bin/nvidia-build-lb-gateway
