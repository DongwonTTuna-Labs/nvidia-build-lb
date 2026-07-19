#!/bin/sh
set -eu

runtime=/run/nvidia-build-lb/secrets
mkdir -p "$runtime"
chmod 0700 "$runtime"
fail() {
  printf 'prestart_failed:%s\n' "$1" >&2
  exit 70
}

check_source() {
  source=$1
  [ -f "$source" ] || fail source_missing
  [ ! -L "$source" ] || fail source_symlink
  metadata=$(stat -c '%u:%a' "$source" 2>/dev/null) || fail source_stat
  case "$metadata" in
    0:400|0:444|0:600) ;;
    *:444)
      [ "${NBLB_QA_ALLOW_HOST_SECRET_OWNER:-0}" = 1 ] || fail source_owner
      ;;
    *) fail source_mode ;;
  esac
}

check_database_password() {
  size=$(wc -c < "$1") || fail source_size
  [ "$size" -ge 1 ] && [ "$size" -le 1024 ] || fail source_size
  LC_ALL=C grep -q '[[:cntrl:]]' "$1" 2>/dev/null && fail source_control
  return 0
}

check_admin_token() {
  size=$(wc -c < "$1") || fail admin_token_size
  case "$size" in 75|76) ;; *) fail admin_token_shape ;; esac
  value=$(cat "$1") || fail admin_token_read
  case "$value" in
    nblb_admin_????????????????????????????????????????????????????????????????)
      suffix=${value#nblb_admin_}
      case "$suffix" in *[!0-9a-f]*) fail admin_token_shape ;; esac ;;
    *) fail admin_token_shape ;;
  esac
}

check_vault_key() {
  [ "$(wc -c < "$1")" -eq 32 ] || fail vault_key_shape
}
case "${NVIDIA_BUILD_LB_MODE:-app}" in
  migrate|db-init) required_secrets="db_password" ;;
  *) required_secrets="admin_token vault_master_key db_password" ;;
esac
for name in $required_secrets; do
  source="/run/canonical-secrets/$name"
  check_source "$source"
  case "$name" in
    admin_token) check_admin_token "$source" ;;
    vault_master_key) check_vault_key "$source" ;;
    db_password) check_database_password "$source" ;;
  esac
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
