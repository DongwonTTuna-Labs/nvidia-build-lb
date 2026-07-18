# Backup and Isolated Restore

## Custody contract

A usable backup is a three-location tuple:

1. PostgreSQL custom-format `database.dump` in the database backup root.
2. The exact 32-byte `vault_master_key` in a separate key backup root.
3. `manifest.json` in a third root, binding both artifact hashes to the Alembic
   revision, upstream key IDs/fingerprints, and an aggregate of all downstream
   token IDs/digests.

The state and manifest formats are version-dispatched. V2 remains the strict
format for the complete `0004_vault_key_verifier` schema. A complete
`0005_admin_dashboard_ledger` schema produces V3; a partial or unknown schema
shape fails closed instead of being mislabeled V2 or V3. V3 additionally binds
the count and canonical SHA-256 projection of every admin event, attempt
receipt, and live pin, plus pending-attempt count, logical routed-request
rollup, and the complete ledger singleton. The projections are compact,
ASCII-escaped JSON arrays with fixed field positions, canonical UUID and UTC
timestamp forms, and no plaintext credential or token.

Each generated directory is root-owned mode `0700`; each file is root-owned
mode `0600`. The manifest never contains plaintext keys, tokens, ciphertext, or
database passwords. Its downstream aggregate proves exact equality without
publishing token digests. Store the database and key roots in separate custody
domains. Losing either half makes recovery impossible.

The three parent roots must already exist as distinct, non-nested,
`root:root` mode-`0700` directories. Directory separation is an access-custody
boundary, not a disk-failure boundary. On the current single-LV host, `/srv`,
`/opt`, and the Docker volume share one physical failure domain, so a local tuple
proves application recovery only. Copy the database dump and manifest together
to verified off-host storage and the vault key through a separately controlled,
encrypted channel before claiming disaster-recovery readiness. Verify both
copies by hash and perform this isolated restore from the off-host copy. The
backup script deliberately will not create or repair these custody roots:

```console
sudo install -d -o root -g root -m 0700 \
  /srv/nvidia-build-lb-backup/database \
  /srv/nvidia-build-lb-key-backup/key \
  /srv/nvidia-build-lb-manifest/manifest
```

Backup and restore each hold an exclusive FD-backed lock in the persistent
root-owned mode-`0700` directory `/run/lock/nvidia-build-lb`. Lock files are
mode `0600` and deliberately remain in place after the process exits; unlinking
a contended lock pathname would allow a new inode to bypass the active lock.
`NBLB_OPERATION_LOCK_DIR` is reserved for isolated QA and must name an absolute
mode-`0700` directory owned by the invoking UID/GID.

## Quiesced backup

The backup contract is deliberately quiesced: stop the application first so
the dump and safe state oracle cannot diverge. PostgreSQL stays running. The
script refuses a running source app and refuses a database without the
`nvidia-build-lb.backup-source=true` label.

```console
set -Eeuo pipefail
set +x
export BACKUP_ID="backup-$(date -u +%Y%m%dt%H%M%Sz)"
export APP_CONTAINER="$(scripts/ops/production-compose.sh ps -q app)"
export DB_CONTAINER="$(scripts/ops/production-compose.sh ps -q db)"
export APP_IMAGE_REF="$(scripts/ops/production-compose.sh config --format json | jq -er '.services.app.image')"
export NBLB_HELPER_IMAGE="$(docker image inspect --format '{{.Id}}' "$APP_IMAGE_REF")"
APP_STOPPED=0
restart_source_app() {
  [ "$APP_STOPPED" -eq 1 ] || return 0
  scripts/ops/production-compose.sh start app || return $?
  APP_STOPPED=0
}
backup_exit() {
  original_status=$?
  trap - EXIT HUP INT TERM
  set +e
  restart_source_app
  cleanup_status=$?
  if [ "$original_status" -ne 0 ]; then
    [ "$cleanup_status" -eq 0 ] || printf '%s\n' backup_app_restart_failed >&2
    exit "$original_status"
  fi
  [ "$cleanup_status" -eq 0 ] || { printf '%s\n' backup_app_restart_failed >&2; exit 1; }
  exit 0
}
trap backup_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
APP_STOPPED=1
scripts/ops/production-compose.sh stop app
set +e
BACKUP_RECEIPT="$(sudo scripts/ops/backup.sh \
  --db-container "$DB_CONTAINER" \
  --app-container "$APP_CONTAINER" \
  --helper-image "$NBLB_HELPER_IMAGE" \
  --vault-key-file /opt/nvidia-build-lb/secrets/vault_master_key \
  --database-root /srv/nvidia-build-lb-backup/database \
  --key-root /srv/nvidia-build-lb-key-backup/key \
  --manifest-root /srv/nvidia-build-lb-manifest/manifest \
  --backup-id "$BACKUP_ID")"
BACKUP_STATUS=$?
set -e
[ "$BACKUP_STATUS" -eq 0 ] || exit "$BACKUP_STATUS"
set +e
restart_source_app
START_STATUS=$?
set -e
[ "$START_STATUS" -eq 0 ] || exit "$START_STATUS"
printf '%s\n' "$BACKUP_RECEIPT" | jq -e '.status == "PASS"' >/dev/null
# Runtime mode requires bounded current readiness, or exact stable legacy
# evidence only when the operator-readiness route is absent.
BACKUP_RUNTIME_READY=0
BACKUP_RUNTIME_ATTEMPT=0
while [ "$BACKUP_RUNTIME_ATTEMPT" -lt 30 ]; do
  BACKUP_RUNTIME_ATTEMPT=$((BACKUP_RUNTIME_ATTEMPT + 1))
  if sudo /usr/bin/python3 scripts/operator_readiness_probe.py \
    runtime 2456 /opt/nvidia-build-lb/secrets/admin_token \
    "$APP_CONTAINER" >/dev/null 2>&1; then
    BACKUP_RUNTIME_READY=1
    break
  fi
  sleep 1
done
[ "$BACKUP_RUNTIME_READY" -eq 1 ] || { printf '%s\n' backup_restart_runtime_timeout >&2; exit 1; }
trap - EXIT HUP INT TERM
printf '%s\n' "$BACKUP_RECEIPT"
```

The app restart is attempted even when backup fails, but the original backup
status remains authoritative and a later runtime-probe failure never masks it.
The printed receipt appears only after backup, restart, receipt validation, and
the authenticated runtime probe all succeed. For a current image the probe
requires the exact bounded `operator-readiness` DTO. Only that route's `404`
permits the exact legacy overview: ready passes immediately, while degraded/no-key
must remain exact on the same container ID and `StartedAt` generation beyond the
prior image's complete fail-stop budget. The
probe therefore accepts the restarted source's ready, no-key,
transient-capacity, or permanent-blocker state without treating a lifecycle
withdrawal as operational. Each request has an absolute deadline and response
size cap. If runtime evidence remains unconfirmed, the command exits without
publishing the receipt or continuing. The receipt already binds the database
evidence; preserve its safe digests, counts, IDs, and `pair_id` without changing
either artifact.

## Mandatory isolated restore drill

Never restore over the live volume. Use a distinct Compose project, distinct
volume, alternate loopback port, and a database labeled
`nvidia-build-lb.restore-isolated=true`. Prepare a new root-only secret directory
with copies of the database password and admin token; do not precreate its vault
key. Replace the `BACKUP_ID` placeholder below with the exact ID whose receipt
and three custody locations were verified. The block derives the immutable
helper image from canonical runtime config and validates every persistent input
before it creates the isolated secret directory, database, or volume.

```console
set -Eeuo pipefail
set +x
export BACKUP_ID="replace-with-verified-backup-id"
PRESERVE_RESTORE_GENERATION=${PRESERVE_RESTORE_GENERATION:-0}
case "$PRESERVE_RESTORE_GENERATION" in 0|1) ;; *) printf '%s\n' restore_preserve_mode_invalid >&2; exit 64 ;; esac
[[ "$BACKUP_ID" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] \
  || { printf '%s\n' restore_backup_id_invalid >&2; exit 64; }
APP_IMAGE_REF="$(scripts/ops/production-compose.sh config --format json | jq -er '.services.app.image')"
NBLB_HELPER_IMAGE="$(docker image inspect --format '{{.Id}}' "$APP_IMAGE_REF")"
[[ "$NBLB_HELPER_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || { printf '%s\n' restore_helper_image_invalid >&2; exit 64; }
[ "$(docker image inspect --format '{{.Id}}' "$NBLB_HELPER_IMAGE")" = "$NBLB_HELPER_IMAGE" ] \
  || { printf '%s\n' restore_helper_image_unavailable >&2; exit 1; }
DATABASE_DIRECTORY="/srv/nvidia-build-lb-backup/database/$BACKUP_ID"
KEY_DIRECTORY="/srv/nvidia-build-lb-key-backup/key/$BACKUP_ID"
MANIFEST_FILE="/srv/nvidia-build-lb-manifest/manifest/$BACKUP_ID/manifest.json"
for source in \
  "$DATABASE_DIRECTORY/database.dump" \
  "$KEY_DIRECTORY/vault_master_key" \
  "$MANIFEST_FILE" \
  /opt/nvidia-build-lb/secrets/db_password \
  /opt/nvidia-build-lb/secrets/admin_token; do
  sudo test -f "$source" && sudo test ! -L "$source" \
    && [ "$(sudo stat -c '%u:%g:%a:%h' "$source")" = 0:0:600:1 ] \
    || { printf '%s\n' restore_source_custody_invalid >&2; exit 1; }
done
RESTORE_ATTEMPT_ID="$(date -u +%Y%m%dt%H%M%Sz)-$$"
RESTORE_PROJECT="nblb-restore-$RESTORE_ATTEMPT_ID"
RESTORE_SECRET_DIR="/opt/nvidia-build-lb/restore-secrets-$RESTORE_ATTEMPT_ID"
RESTORE_STARTED=0
RESTORE_SECRET_OWNED=0
cleanup_restore_attempt() {
  cleanup_status=0
  if [ "$RESTORE_STARTED" -eq 1 ]; then
    if scripts/ops/production-compose.sh -p "$RESTORE_PROJECT" down --volumes --remove-orphans; then
      RESTORE_STARTED=0
    else
      cleanup_status=1
    fi
  fi
  if [ "$RESTORE_SECRET_OWNED" -eq 1 ]; then
    if sudo rm -rf -- "$RESTORE_SECRET_DIR"; then
      RESTORE_SECRET_OWNED=0
    else
      cleanup_status=1
    fi
  fi
  return "$cleanup_status"
}
restore_exit() {
  original_status=$?
  trap - EXIT HUP INT TERM
  set +e
  cleanup_restore_attempt
  cleanup_status=$?
  if [ "$original_status" -ne 0 ]; then
    [ "$cleanup_status" -eq 0 ] || printf '%s\n' restore_attempt_cleanup_failed >&2
    exit "$original_status"
  fi
  [ "$cleanup_status" -eq 0 ] || { printf '%s\n' restore_attempt_cleanup_failed >&2; exit 1; }
  exit 0
}
trap restore_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo test ! -e "$RESTORE_SECRET_DIR"
RESTORE_SECRET_OWNED=1
sudo install -d -o root -g root -m 0700 "$RESTORE_SECRET_DIR"
sudo cp --no-preserve=mode,ownership /opt/nvidia-build-lb/secrets/db_password "$RESTORE_SECRET_DIR/db_password"
sudo cp --no-preserve=mode,ownership /opt/nvidia-build-lb/secrets/admin_token "$RESTORE_SECRET_DIR/admin_token"
sudo chown root:root "$RESTORE_SECRET_DIR"/*
sudo chmod 0600 "$RESTORE_SECRET_DIR"/*
export NBLB_SECRET_DIR="$RESTORE_SECRET_DIR"
export NBLB_RESTORE_ISOLATED=true
export NBLB_BACKUP_SOURCE=false
export NBLB_PORT=32458
RESTORE_STARTED=1
scripts/ops/production-compose.sh -p "$RESTORE_PROJECT" up -d db
export RESTORE_DB="$(scripts/ops/production-compose.sh -p "$RESTORE_PROJECT" ps -q db)"
RESTORE_DB_HEALTH=""
RESTORE_DB_HEALTH_ATTEMPT=0
while [ "$RESTORE_DB_HEALTH_ATTEMPT" -lt 120 ]; do
  RESTORE_DB_HEALTH_ATTEMPT=$((RESTORE_DB_HEALTH_ATTEMPT + 1))
  RESTORE_DB_HEALTH="$(docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$RESTORE_DB" 2>/dev/null || printf '%s\n' missing)"
  [ "$RESTORE_DB_HEALTH" = healthy ] && break
  case "$RESTORE_DB_HEALTH" in exited|dead|missing) break ;; esac
  sleep 1
done
[ "$RESTORE_DB_HEALTH" = healthy ] \
  || { printf '%s\n' restore_database_health_timeout >&2; exit 1; }
set +e
RESTORE_RECEIPT="$(sudo scripts/ops/restore.sh \
  --db-container "$RESTORE_DB" \
  --helper-image "$NBLB_HELPER_IMAGE" \
  --database-directory "$DATABASE_DIRECTORY" \
  --key-directory "$KEY_DIRECTORY" \
  --manifest "$MANIFEST_FILE" \
  --target-secret-dir "$RESTORE_SECRET_DIR")"
RESTORE_STATUS=$?
set -e
[ "$RESTORE_STATUS" -eq 0 ] || exit "$RESTORE_STATUS"
printf '%s\n' "$RESTORE_RECEIPT" | jq -e \
  '.status == "PASS" and .restored_state_matches == true' >/dev/null
scripts/ops/production-compose.sh -p "$RESTORE_PROJECT" up -d migrate app
RESTORE_APP="$(scripts/ops/production-compose.sh -p "$RESTORE_PROJECT" ps -q app)"
# The isolated app uses the same selected alternate port for its connection
# and Host header.
RESTORE_RUNTIME_READY=0
RESTORE_RUNTIME_ATTEMPT=0
while [ "$RESTORE_RUNTIME_ATTEMPT" -lt 30 ]; do
  RESTORE_RUNTIME_ATTEMPT=$((RESTORE_RUNTIME_ATTEMPT + 1))
  if sudo /usr/bin/python3 scripts/operator_readiness_probe.py \
    runtime 32458 "$RESTORE_SECRET_DIR/admin_token" \
    "$RESTORE_APP" >/dev/null 2>&1; then
    RESTORE_RUNTIME_READY=1
    break
  fi
  sleep 1
done
[ "$RESTORE_RUNTIME_READY" -eq 1 ] || { printf '%s\n' restore_runtime_timeout >&2; exit 1; }
if [ "$PRESERVE_RESTORE_GENERATION" -eq 1 ]; then
  trap - EXIT HUP INT TERM
  printf '%s\n' "$RESTORE_RECEIPT"
  printf 'Preserved isolated restore project: %s\n' "$RESTORE_PROJECT"
  printf 'Preserved isolated restore secret directory: %s\n' "$RESTORE_SECRET_DIR"
  exit 0
fi
set +e
cleanup_restore_attempt
CLEANUP_STATUS=$?
set -e
[ "$CLEANUP_STATUS" -eq 0 ] || { printf '%s\n' restore_attempt_cleanup_failed >&2; exit 1; }
trap - EXIT HUP INT TERM
printf '%s\n' "$RESTORE_RECEIPT"
```

Any restore failure prevents later steps. EXIT and signal traps recover the
attempt-owned isolated stack, volume, and unique secret directory after restore,
receipt-validation, migration, app-start, or health failure. The original
failure status remains authoritative; a simultaneous cleanup failure is also
reported on stderr. Success is published only after the exact receipt,
migration, authenticated runtime probe, and explicit zero-resource cleanup all
pass. The host probe uses one configured loopback authority. Its selected
connection port and `Host` port are identical. Current images must return the
exact bounded
`operator-readiness` DTO; only that route's `404` can use the exact stable
legacy-overview fallback, whose degraded path also requires one unchanged
container ID and `StartedAt` generation. The probe
accepts the restored routing/ledger state rather than requiring public
readiness. A timeout means operational evidence is unconfirmed, so no later step
or PASS receipt is allowed and the attempt-owned resources are cleaned.
Persistent inputs are validated before those resources are created, and restore
never reaches `psql` until the isolated PostgreSQL container reports healthy
within the bounded wait.

The default drill cleans its isolated project, volume, and secret directory.
Set `PRESERVE_RESTORE_GENERATION=1` only when a separately reviewed production
state-cutover decision needs the verified generation retained. Its printed
project and secret-directory identifiers are safe metadata; keep both root-only
and do not use either as the live pair. After that decision, remove the preserved
project with the same release-matched Compose wrapper and remove only its exact
printed secret directory. A routine drill must leave the default at `0` and
prove zero-resource cleanup.

Restore refuses a nonempty target database, a non-isolated label, mismatched
artifact hash, wrong key length, unsafe root artifact, unknown manifest field,
state/manifest version mismatch, or any version-specific safe-state mismatch.
V2 is compared as V2 before any later migration. V3 must reproduce the exact
event, receipt, pending-attempt, live-pin, rollup, and ledger counts/digests;
equal row counts with different unresolved identities fail. Empty means no user-created schema, relation,
function, or type outside PostgreSQL's built-in objects. The PASS receipt binds
the same Alembic revision, upstream key IDs/fingerprints, downstream digest
aggregate, database vault-key verifier, and vault-key artifact fingerprint.
For V3 it also reports only the safe evidence counts and canonical digests
listed above.
Before verification, restore copies all three source artifacts into one fresh,
root-only private staging directory. Verification, key installation, `pg_restore`,
and state comparison use only that immutable staged tuple, so replacement of a
custody-root path after snapshot creation cannot change the restored bytes. The
network-disabled one-shot key installer receives only `DAC_OVERRIDE` and copies
the staged key into a root-owned mode-0600 file; it has no database or network
access. Restore verifies that installed file against both the staged manifest
and the verifier captured from the restored database before emitting PASS. A
restore replays the archive in one PostgreSQL transaction, so an archive error
rolls back every database change. A failed restore removes any installed vault
key and the entire staging tuple;
an attempt-owned marker prevents cleanup from deleting a key that predated the
restore attempt. Discard the failed attempt's isolated database volume after
any failure. Retry the same verified backup pair only in a fresh isolated
attempt; never reuse the failed target or clean its database in place.
`down --volumes` is permitted only for the unique explicitly isolated drill
project created by this procedure, never for the live project.
